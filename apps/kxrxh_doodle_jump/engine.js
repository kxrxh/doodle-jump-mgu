'use strict';
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');
const crypto = require('crypto');
const { spawn } = require('child_process');

const MAX_PACKET = 2048 * 2048 * 4 + 12;
const PACK_CHUNK = 1 << 20;
const MAX_PACK_INDEX = 1 << 20;
const MAX_PACK_ENTRY = 16 << 20;

class PacketReader {
    constructor(receive, reuseFrames) {
        this.receive = receive; this.header = Buffer.alloc(8); this.at = 0; this.body = null; this.used = 0;
        this.frames = reuseFrames ? [null, null] : null; this.frameSlot = 0;
    }
    feed(chunk) {
        let offset = 0;
        while (offset < chunk.length) {
            if (!this.body) {
                const n = Math.min(8 - this.at, chunk.length - offset);
                chunk.copy(this.header, this.at, offset, offset + n); this.at += n; offset += n;
                if (this.at !== 8) continue;
                this.type = this.header.toString('ascii', 0, 4);
                if (this.type !== 'FRM1' && this.type !== 'FRM2' && this.type !== 'EVT1' && this.type !== 'TEX1') throw new Error('Unknown engine packet type');
                const length = this.header.readUInt32LE(4);
                if (length > MAX_PACKET) throw new Error('Invalid engine packet size');
                this.reuse = this.frames && (this.type === 'FRM1' || (this.type === 'FRM2' && length > 12));
                if (this.reuse) {
                    // Empty FRM2 updates keep the last changed patches borrowed until their successor arrives.
                    let body = this.frames[this.frameSlot];
                    if (!body || body.length < length) body = this.frames[this.frameSlot] = Buffer.allocUnsafe(length);
                    this.body = body.length === length ? body : body.slice(0, length);
                } else this.body = Buffer.allocUnsafe(length);
                this.used = 0;
            }
            const n = Math.min(this.body.length - this.used, chunk.length - offset);
            chunk.copy(this.body, this.used, offset, offset + n); this.used += n; offset += n;
            if (this.used === this.body.length) {
                const type = this.type, body = this.body;
                this.at = 0; this.body = null;
                if (this.reuse) this.frameSlot ^= 1;
                this.receive(type, body);
            }
        }
    }
}

function mkdir(directory) {
    if (fs.existsSync(directory)) return;
    mkdir(path.dirname(directory));
    try { fs.mkdirSync(directory); } catch (error) { if (error.code !== 'EEXIST') throw error; }
}

function clearCache(directory) {
    if (!fs.existsSync(directory)) return;
    if (fs.lstatSync(directory).isSymbolicLink()) throw new Error('Unsafe engine cache directory');
    fs.readdirSync(directory).forEach((name) => {
        const file=path.join(directory,name);
        if (fs.lstatSync(file).isDirectory()) { clearCache(file); fs.rmdirSync(file); }
        else fs.unlinkSync(file);
    });
}

function readPack(fd, buffer, position, length) {
    let offset = 0;
    while (offset < length) {
        const n = fs.readSync(fd, buffer, offset, Math.min(PACK_CHUNK, length - offset), position + offset);
        if (!n) throw new Error('Truncated engine data pack');
        offset += n;
    }
    return buffer;
}

function restoreExecutable(directory) {
    const file = path.join(directory, 'doodle-host');
    let stat;
    try { stat = fs.lstatSync(file); } catch (error) { if (error.code === 'ENOENT') return; throw error; }
    if (!stat.isFile()) throw new Error('Unsafe native host executable');
    if ((stat.mode & 0o777) !== 0o755) fs.chmodSync(file, 0o755);
}

function unpack(file, directory) {
    const fd = fs.openSync(file, 'r');
    try {
        const stat = fs.fstatSync(fd), root = path.resolve(directory), marker = path.join(root, '.sha256');
        if (!stat.isFile() || !Number.isSafeInteger(stat.size) || stat.size < 12) throw new Error('Invalid or truncated engine data pack');
        try {
            if (!fs.lstatSync(root).isDirectory()) throw new Error('Unsafe engine cache directory');
        } catch (error) { if (error.code !== 'ENOENT') throw error; }
        const hash = crypto.createHash('sha256'), chunk = Buffer.allocUnsafe(Math.min(PACK_CHUNK, stat.size));
        for (let offset = 0; offset < stat.size; offset += chunk.length) {
            const n = Math.min(chunk.length, stat.size - offset);
            hash.update(readPack(fd, chunk, offset, n).slice(0, n));
        }
        const digest = hash.digest('hex');
        let cached = false;
        try {
            const stat = fs.lstatSync(marker);
            cached = stat.isFile() && stat.size === digest.length && fs.readFileSync(marker, 'utf8') === digest;
        } catch (e) { /* first run */ }
        if (cached) { restoreExecutable(root); return; }
        const header = readPack(fd, Buffer.allocUnsafe(12), 0, 12);
        if (header.toString('ascii', 0, 8) !== 'MGUPACK1') throw new Error('Invalid engine data pack');
        const length = header.readUInt32LE(8), base = 12 + length;
        if (!length || length > MAX_PACK_INDEX || base > stat.size) throw new Error('Invalid engine pack index');
        const index = JSON.parse(zlib.inflateRawSync(readPack(fd, Buffer.allocUnsafe(length), 12, length)).toString('utf8'));
        if (!index || !index.files || typeof index.files !== 'object' || Array.isArray(index.files)) throw new Error('Invalid engine pack index');
        const files = Object.keys(index.files);
        let maxEntry = 0;
        files.forEach((name) => {
            const destination = path.resolve(root, name), part = index.files[name];
            if (destination.indexOf(root + path.sep) !== 0 || name.indexOf('\0') >= 0) throw new Error('Unsafe engine pack path');
            if (!Array.isArray(part) || part.length !== 2 || !part.every(Number.isSafeInteger) ||
                part[0] < 0 || part[1] <= 0 || part[1] > MAX_PACK_ENTRY ||
                part[0] > stat.size - base - part[1]) throw new Error('Invalid engine pack range');
            maxEntry = Math.max(maxEntry, part[1]);
        });
        const compressed = Buffer.allocUnsafe(maxEntry);
        clearCache(root);
        files.forEach((name) => {
            const destination = path.resolve(root, name), part = index.files[name];
            mkdir(path.dirname(destination));
            readPack(fd, compressed, base + part[0], part[1]);
            fs.writeFileSync(destination, zlib.inflateRawSync(compressed.slice(0, part[1])));
        });
        restoreExecutable(root);
        fs.writeFileSync(marker, digest);
    } finally { fs.closeSync(fd); }
}

class Session {
    constructor(dataDir, log) {
        this.dataDir = dataDir; this.log = log; this.ready = false; this.dead = false;
        this.onEvent = null; this.onPixels = null; this.onTexture = null; this.onError = null;
    }
    start() {
        if (this.child || this.dead) throw new Error('Native session already started or stopped');
        let directory = process.env.DOODLE_ENGINE;
        if (!directory) {
            directory = path.join(this.dataDir, 'engine');
            unpack(path.join(__dirname, 'engine.pack'), directory);
        }
        directory = path.resolve(directory);
        const executable = process.env.DOODLE_HOST || path.join(directory, 'doodle-host');
        const original = process.env.DOODLE_ORIGINAL ? path.resolve(process.env.DOODLE_ORIGINAL) : path.join(directory, 'original');
        const args = ['--session', '--data', process.env.DOODLE_DATA || path.join(this.dataDir, 'game'), '--original', original, '--runtime', directory];
        const env = Object.assign({}, process.env, { MALLOC_MMAP_THRESHOLD_: '1048576' });
        const child = this.child = spawn(executable, args, { cwd: directory, env, stdio: ['pipe', 'pipe', 'pipe'] });
        const reader = new PacketReader((type, body) => this.packet(type, body), true);
        child.stdout.on('data', (chunk) => { if (!this.dead) try { reader.feed(chunk); } catch (e) { this.fail(e); } });
        let pending = '';
        child.stderr.on('data', (chunk) => {
            pending += chunk.toString('utf8');
            const lines = pending.split('\n'); pending = lines.pop();
            lines.forEach((line) => { if (line.trim()) this.log.info(line); });
        });
        child.on('error', (error) => this.fail(error));
        child.stdin.on('error', (error) => { if (!this.dead) this.fail(error); });
        child.on('exit', (code, signal) => {
            clearTimeout(this.stopTimer);
            if (!this.dead) this.fail(new Error('Native session exited: ' + (signal || code)));
        });
    }
    packet(type, body) {
        if (type === 'TEX1') {
            if (body.length < 12) throw new Error('Short scenery texture');
            const id=body.readUInt32LE(0),w=body.readUInt32LE(4),h=body.readUInt32LE(8);
            if (id<2 || id>100 || !w || !h || w>2048 || h>2048 || body.length!==12+w*h*4) throw new Error('Invalid scenery texture');
            if (this.onTexture) this.onTexture(id,w,h,body.slice(12));
        } else if (type === 'EVT1') {
            const event = JSON.parse(body.toString('utf8'));
            if (!event || Array.isArray(event) || typeof event.type !== 'string') throw new Error('Invalid session event');
            if (event.type === 'ready') this.ready = true;
            if (event.type === 'error') this.fail(new Error(event.message));
            else if (this.onEvent) this.onEvent(event);
        } else {
            const incremental = type === 'FRM2';
            if (!incremental && type !== 'FRM1') throw new Error('Unknown session packet type');
            if (body.length < (incremental ? 12 : 8)) throw new Error('Short engine frame');
            const w = body.readUInt32LE(0), h = body.readUInt32LE(4);
            if (!((w === 544 && h === 960) || (w === 408 && h === 720))) throw new Error('Invalid engine frame dimensions');
            let pixels, patches;
            if (incremental) {
                const count = body.readUInt32LE(8);
                let offset = 12 + count*16;
                if (count > 64 || offset > body.length) throw new Error('Invalid engine frame regions');
                patches = [];
                for (let i=0; i<count; i++) {
                    const at=12+i*16, x=body.readUInt32LE(at), y=body.readUInt32LE(at+4),
                        width=body.readUInt32LE(at+8), height=body.readUInt32LE(at+12);
                    if (!width || !height || x+width>w || y+height>h) throw new Error('Invalid engine frame region');
                    const end=offset+width*height*4;
                    if (end>body.length) throw new Error('Truncated engine frame region');
                    patches.push({x,y,w:width,h:height,data:body.slice(offset,end)});
                    offset=end;
                }
                if (offset!==body.length) throw new Error('Invalid engine frame length');
                let image=this.frameImage;
                if (!image || image.w!==w || image.h!==h) {
                    if (count!==1 || patches[0].x || patches[0].y || patches[0].w!==w || patches[0].h!==h)
                        throw new Error('Engine frame needs an initial key frame');
                    image={w,h,data:Buffer.allocUnsafe(w*h*4)};
                }
                // Validate all regions before mutating the last published image.
                patches.forEach(r=>{
                    for (let row=0; row<r.h; row++)
                        r.data.copy(image.data,((r.y+row)*w+r.x)*4,row*r.w*4,(row+1)*r.w*4);
                });
                this.frameImage=image;pixels=image.data;
            } else {
                if (body.length!==8+w*h*4) throw new Error('Invalid engine frame dimensions');
                this.frameImage=null;pixels=body.slice(8);
            }
            if (this.onPixels) this.onPixels(pixels, w, h, patches);
        }
    }
    fail(error) {
        if (this.dead) return;
        this.dead = true; this.ready = false;
        this.log.error('Doodle Jump: ' + error.message);
        if (this.child) this.child.kill();
        if (this.onError) this.onError(error);
    }
    send(command) {
        if (this.dead || !this.child || !this.child.stdin.writable) return false;
        const message = JSON.stringify(command) + '\n';
        if (Buffer.byteLength(message) > 4096) throw new Error('Session input exceeds its size limit');
        return this.child.stdin.write(message);
    }
    stop() {
        if (this.dead) return;
        this.dead = true; this.ready = false;
        if (!this.child) return;
        if (this.child.stdin.writable) this.child.stdin.end('{"shutdown":true}\n');
        else this.child.kill();
        this.stopTimer = setTimeout(() => { if (this.child.exitCode === null) this.child.kill(); }, 1000);
        this.stopTimer.unref();
    }
}

module.exports = { Session, PacketReader, unpack };

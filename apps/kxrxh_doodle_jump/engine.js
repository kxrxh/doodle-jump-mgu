'use strict';
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');
const crypto = require('crypto');
const { spawn } = require('child_process');

const MAX_PACKET = 2048 * 2048 * 4 + 12;
const MAX_GPU_PACKET = 64 << 20;
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
                if (this.type !== 'FRM1' && this.type !== 'FRM2' && this.type !== 'EVT1' && this.type !== 'TEX1' && this.type !== 'GPF1') throw new Error('Unknown engine packet type');
                const length = this.header.readUInt32LE(4);
                if (length > (this.type === 'GPF1' ? MAX_GPU_PACKET : MAX_PACKET)) throw new Error('Invalid engine packet size');
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

function validateGPU(body) {
    if (!Buffer.isBuffer(body) || body.length > MAX_GPU_PACKET) throw new Error('Invalid GPU scene size');
    let scene = false;
    for (let i = 0; i < body.length;) {
        if (i + 8 > body.length) throw new Error('Truncated GPU message');
        const type = body.readUInt32LE(i), length = body.readUInt32LE(i + 4), end = i + 8 + length;
        if (end > body.length || (length & 3)) throw new Error('Invalid GPU message length');
        const at = i + 8;
        if (type === 0x4d520007) {
            if (length < 12) throw new Error('Truncated GPU texture');
            const id = body.readUInt32LE(at), w = body.readUInt32LE(at + 4), h = body.readUInt32LE(at + 8);
            const remove = w === 0 && h === 0;
            if (!id || (remove ? !scene || length !== 12 : scene || !w || !h || w > 2048 || h > 2048 || length !== 12 + w*h*4))
                throw new Error('Invalid GPU texture');
        } else if (type === 0x4d520008) {
            if (scene || length < 8) throw new Error('Invalid GPU scene');
            const w = body.readUInt32LE(at), h = body.readUInt32LE(at + 4);
            if (!((w === 408 && h === 720) || (w === 544 && h === 960))) throw new Error('Invalid GPU scene dimensions');
            for (let command = at + 8; command < end;) {
                const op = body.readUInt32LE(command);
                let floats, next;
                if (op === 1) { floats = command + 4; next = command + 20; }
                else if (op === 2) {
                    if (command + 28 > end) throw new Error('Truncated GPU scene draw');
                    const flags = body.readUInt32LE(command + 8), source = body.readUInt32LE(command + 12),
                        destination = body.readUInt32LE(command + 16), replace = body.readUInt32LE(command + 20), count = body.readUInt32LE(command + 24);
                    if (flags > 15 || ![0, 1, 770, 771].includes(source) || ![0, 1, 768, 770, 771].includes(destination) ||
                        replace > 2 || !count || count % 3 || count > 65536) throw new Error('Invalid GPU scene draw');
                    floats = command + 28; next = floats + count*32;
                } else throw new Error('Unknown GPU scene command');
                if (next > end) throw new Error('Truncated GPU scene command');
                for (let offset = floats; offset < next; offset += 4)
                    if (!Number.isFinite(body.readFloatLE(offset))) throw new Error('Nonfinite GPU scene vertex');
                command = next;
            }
            scene = true;
        } else throw new Error('Unknown GPU message');
        i = end;
    }
    if (!scene) throw new Error('Missing GPU scene');
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
        try {
            const cached = fs.lstatSync(marker);
            if (cached.isFile() && cached.size === digest.length && fs.readFileSync(marker, 'utf8') === digest) return;
        } catch (e) { /* first run */ }
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
        fs.writeFileSync(marker, digest);
    } finally { fs.closeSync(fd); }
}

class Engine {
    constructor(dataDir, log) {
        this.dataDir = dataDir; this.log = log; this.ready = false; this.busy = false; this.dead = false; this.playing = false;
        this.onFrame = null; this.onGPU = null; this.onError = null; this.onDialog = null; this.onTexture = null;
        this.scenery = { sprites: {} };
    }
    start() {
        let directory = process.env.DOODLE_ENGINE;
        if (!directory) {
            directory = path.join(this.dataDir, 'engine');
            unpack(path.join(__dirname, 'engine.pack'), directory);
        }
        const python = process.env.DOODLE_PYTHON || '/var/opt/mgu-python/current/bin/python3';
        const args = [path.join(directory, 'engine_process.py'), '--data', process.env.DOODLE_DATA || path.join(this.dataDir, 'game')];
        if (!process.env.DOODLE_ENGINE) args.push('--original', path.join(directory, 'original'));
        const env = Object.assign({}, process.env, {
            PYTHONPATH: path.join(directory, 'site-packages'), PYTHONNOUSERSITE: '1', PYTHONDONTWRITEBYTECODE: '1',
            // Bound glibc's retained heap after large image and audio temporaries are freed.
            MALLOC_MMAP_THRESHOLD_: '1048576',
        });
        const child = this.child = spawn(python, args, { cwd: directory, env, stdio: ['pipe', 'pipe', 'pipe'] });
        const reader = new PacketReader((type, body) => this.packet(type,body), true);
        child.stdout.on('data', (chunk) => { try { reader.feed(chunk); } catch (e) { this.fail(e); } });
        let pending = '';
        child.stderr.on('data', (chunk) => {
            pending += chunk.toString('utf8');
            const lines = pending.split('\n'); pending = lines.pop();
            lines.forEach((line) => { if (line.trim()) this.log.info(line); });
        });
        child.on('error', (error) => this.fail(error));
        child.stdin.on('error', (error) => { if (!this.dead) this.fail(error); });
        child.on('exit', (code, signal) => { if (!this.dead) this.fail(new Error('Engine exited: ' + (signal || code))); });
    }
    packet(type,body) {
            if (type === 'GPF1') {
                validateGPU(body);
                this.frameImage = null;
                this.busy = false;
                if (this.onGPU) this.onGPU(body);
            } else if (type === 'TEX1') {
                if (body.length<12) throw new Error('Short scenery texture');
                const id=body.readUInt32LE(0),w=body.readUInt32LE(4),h=body.readUInt32LE(8);
                if (id<2 || id>100 || !w || !h || w>2048 || h>2048 || body.length!==12+w*h*4) throw new Error('Invalid scenery texture');
                if (this.onTexture) this.onTexture(id,w,h,body.slice(12));
            } else if (type === 'EVT1') {
                const event = JSON.parse(body.toString('utf8'));
                if (event.type === 'ready') this.ready = true;
                else if (event.type === 'playing') this.playing = !!event.value;
                else if (event.type === 'error') this.fail(new Error(event.message));
                else if (event.type === 'timing') {
                    if (![event.wallMs, event.cpuMs].every(v => Number.isFinite(v) && v >= 0))
                        throw new Error('Invalid engine timing');
                    this.frameTiming = event;
                }
                else if (event.type === 'dialog' && this.onDialog) this.onDialog(event);
                else if (event.type === 'scenery') Object.assign(this.scenery,event);
            } else {
                const incremental = type === 'FRM2';
                if (!incremental && type !== 'FRM1') throw new Error('Unknown engine packet type');
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
                    // Validate the whole packet before changing the published image.
                    patches.forEach(r=>{
                        for (let row=0; row<r.h; row++)
                            r.data.copy(image.data,((r.y+row)*w+r.x)*4,row*r.w*4,(row+1)*r.w*4);
                    });
                    this.frameImage=image;pixels=image.data;
                } else {
                    if (body.length!==8+w*h*4) throw new Error('Invalid engine frame dimensions');
                    this.frameImage=null;pixels=body.slice(8);
                }
                this.busy = false;
                if (this.onFrame) this.onFrame(pixels, w, h, patches);
            }
    }
    fail(error) {
        if (this.dead) return;
        this.dead = true; this.busy = false; this.ready = false;
        this.log.error('Doodle Jump: ' + error.message);
        if (this.child) this.child.kill();
        if (this.onError) this.onError(error);
    }
    send(command) { if (!this.dead && this.child && this.child.stdin.writable) this.child.stdin.write(JSON.stringify(command) + '\n'); }
    frame(command) {
        if (!this.ready || this.busy || this.dead) return false;
        this.frameTiming = null; this.busy = true; this.send(Object.assign({ frame: true }, command)); return true;
    }
    stop() { this.dead = true; if (this.child) this.child.kill(); }
}

module.exports = { Engine, PacketReader, unpack };

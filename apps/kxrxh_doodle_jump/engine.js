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
    constructor(receive) { this.receive = receive; this.header = Buffer.alloc(8); this.at = 0; this.body = null; this.used = 0; }
    feed(chunk) {
        let offset = 0;
        while (offset < chunk.length) {
            if (!this.body) {
                const n = Math.min(8 - this.at, chunk.length - offset);
                chunk.copy(this.header, this.at, offset, offset + n); this.at += n; offset += n;
                if (this.at !== 8) continue;
                this.type = this.header.toString('ascii', 0, 4);
                if (this.type !== 'FRM1' && this.type !== 'EVT1' && this.type !== 'TEX1') throw new Error('Unknown engine packet type');
                const length = this.header.readUInt32LE(4);
                if (length > MAX_PACKET) throw new Error('Invalid engine packet size');
                this.body = Buffer.alloc(length); this.used = 0;
            }
            const n = Math.min(this.body.length - this.used, chunk.length - offset);
            chunk.copy(this.body, this.used, offset, offset + n); this.used += n; offset += n;
            if (this.used === this.body.length) {
                const type = this.type, body = this.body;
                this.at = 0; this.body = null;
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
        this.onFrame = null; this.onError = null; this.onDialog = null; this.onTexture = null;
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
        });
        const child = this.child = spawn(python, args, { cwd: directory, env, stdio: ['pipe', 'pipe', 'pipe'] });
        const reader = new PacketReader((type, body) => this.packet(type,body));
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
            if (type === 'TEX1') {
                if (body.length<12) throw new Error('Short scenery texture');
                const id=body.readUInt32LE(0),w=body.readUInt32LE(4),h=body.readUInt32LE(8);
                if (id<2 || id>100 || !w || !h || w>2048 || h>2048 || body.length!==12+w*h*4) throw new Error('Invalid scenery texture');
                if (this.onTexture) this.onTexture(id,w,h,body.slice(12));
            } else if (type === 'EVT1') {
                const event = JSON.parse(body.toString('utf8'));
                if (event.type === 'ready') this.ready = true;
                else if (event.type === 'playing') this.playing = !!event.value;
                else if (event.type === 'error') this.fail(new Error(event.message));
                else if (event.type === 'dialog' && this.onDialog) this.onDialog(event);
                else if (event.type === 'scenery') Object.assign(this.scenery,event);
            } else {
                if (body.length < 8) throw new Error('Short engine frame');
                const w = body.readUInt32LE(0), h = body.readUInt32LE(4);
                if (w !== 544 || h !== 960 || body.length !== 8 + w*h*4) throw new Error('Invalid engine frame dimensions');
                this.busy = false;
                if (this.onFrame) this.onFrame(body.slice(8), w, h);
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
        this.busy = true; this.send(Object.assign({ frame: true }, command)); return true;
    }
    stop() { this.dead = true; if (this.child) this.child.kill(); }
}

module.exports = { Engine, PacketReader, unpack };

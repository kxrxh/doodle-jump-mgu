'use strict';
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');
const crypto = require('crypto');
const { spawn } = require('child_process');

const MAX_PACKET = 2048 * 2048 * 4 + 12;

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

function unpack(file, directory) {
    const source = fs.readFileSync(file);
    const digest = crypto.createHash('sha256').update(source).digest('hex');
    const marker = path.join(directory, '.sha256');
    try { if (fs.readFileSync(marker, 'utf8') === digest) return; } catch (e) { /* first run */ }
    if (source.toString('ascii', 0, 8) !== 'MGUPACK1') throw new Error('Invalid engine data pack');
    const length = source.readUInt32LE(8), base = 12 + length;
    if (base > source.length) throw new Error('Invalid engine pack index');
    const index = JSON.parse(zlib.inflateRawSync(source.slice(12, base)).toString('utf8'));
    const root = path.resolve(directory);
    Object.keys(index.files).forEach((name) => {
        const destination = path.resolve(root, name), part = index.files[name];
        if (destination.indexOf(root + path.sep) !== 0 || name.indexOf('\0') >= 0) throw new Error('Unsafe engine pack path');
        if (!Array.isArray(part) || part.length !== 2 || !part.every(Number.isSafeInteger) ||
            part[0] < 0 || part[1] < 0 || base + part[0] + part[1] > source.length) throw new Error('Invalid engine pack range');
        mkdir(path.dirname(destination));
        fs.writeFileSync(destination, zlib.inflateRawSync(source.slice(base + part[0], base + part[0] + part[1])));
    });
    fs.chmodSync(path.join(directory, 'python', 'bin', 'python3.13'), 0o755);
    fs.writeFileSync(marker, digest);
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
        const python = process.env.DOODLE_PYTHON || path.join(directory, 'python', 'bin', 'python3.13');
        const args = [path.join(directory, 'engine_process.py'), '--data', process.env.DOODLE_DATA || path.join(this.dataDir, 'game')];
        if (!process.env.DOODLE_ENGINE) args.push('--original', path.join(directory, 'original'));
        const child = this.child = spawn(python, args, { cwd: directory, stdio: ['pipe', 'pipe', 'pipe'] });
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

'use strict';
const net = require('net');
const crypto = require('crypto');

function message(kind, payload) {
    const header = Buffer.alloc(8);
    header.writeUInt32LE(kind, 0);
    header.writeUInt32LE(payload ? payload.length : 0, 4);
    return payload ? Buffer.concat([header, payload]) : header;
}
const END = message(0x444a0001);

class GpuWorker {
    constructor(log) {
        this.log = log;
        this.ready = false;
        this.native = false;
        this.binding = false;
        this.generation = 0;
        this.closed = false;
        this.pending = null;
        this.retry = null;
        this.socket = null;
        this.lastError = null;
        this.onState = null;
        this.cursorImage = null;
        this.cursorPosition = null;
    }
    connect() {
        if (this.closed || this.socket) return;
        const socket = this.socket = net.createConnection(process.env.DOODLE_GPU_SOCKET || '/run/doodle-gpu/render.sock');
        let input = '';
        socket.on('connect', () => {
            if (this.socket !== socket) return;
            this.ready = true;
            this.native = false;
            this.binding = false;
            this.sequence = 0;
            this.videoPath = null;
            this.presentationMode = null;
            this.lastView = null;
            this.generation++;
            this.lastError = null;
            if (this.onState) this.onState();
        });
        socket.on('data', data => {
            if (this.socket !== socket) return;
            input += data.toString('utf8');
            if (input.length > 4096) return this.disconnect(new Error('GPU worker answer is too large'));
            const newline = input.indexOf('\n');
            if (newline < 0) return;
            let reply;
            try {
                reply = JSON.parse(input.slice(0, newline));
                if (!this.pending || newline !== input.length - 1 || !reply) throw new Error('Invalid GPU worker answer');
                if (this.pending.kind === 'bind') {
                    if (reply.event !== 'bind' || typeof reply.native !== 'boolean') throw new Error('Invalid GPU binding answer');
                } else this.validateFrame(reply);
            } catch (error) { return this.disconnect(error); }
            input = '';
            const pending = this.pending;
            this.pending = null;
            clearTimeout(pending.timer);
            if (pending.kind === 'bind') {
                this.binding = false;
                this.native = reply.native;
                this.lastView = null;
                if (this.native) {
                    if (this.cursorImage) this.socket.write(this.cursorImage);
                    if (this.cursorPosition) this.socket.write(this.cursorPosition);
                }
            } else {
                this.sequence = reply.sequence;
                this.presentationMode = reply.mode === 'native' ? 'native' : 'video';
                this.videoPath = reply.mode === 'native' ? null : reply.path;
            }
            pending.receive(null, reply);
            if (pending.kind === 'bind' && this.onState) this.onState();
        });
        socket.on('error', error => { if (this.socket === socket) this.disconnect(error); });
        socket.on('close', () => { if (this.socket === socket) this.disconnect(new Error('GPU worker disconnected')); });
    }
    validateFrame(frame) {
        if (!Number.isInteger(frame.sequence) || frame.sequence !== (this.sequence === 0xffffffff ? 1 : this.sequence + 1))
            throw new Error('Invalid GPU frame sequence');
        if (frame.mode === 'native') {
            if (!this.native || !Number.isInteger(frame.width) || !Number.isInteger(frame.height) ||
                frame.width < 16 || frame.height < 16 || frame.width > 8192 || frame.height > 8192 ||
                typeof frame.presented !== 'boolean' || !Number.isInteger(frame.timestamp) ||
                frame.timestamp < 0 || frame.timestamp > 0xffffffff) throw new Error('Invalid native GPU frame');
        } else if ((frame.mode !== undefined && frame.mode !== 'video') || typeof frame.path !== 'string' ||
            !/^\/dev\/shm\/mgu-video-doodle-[A-Za-z0-9_.-]{1,48}$/.test(frame.path) ||
            frame.width !== 408 || frame.height !== 720 || (this.presentationMode === 'video' && this.videoPath !== null && frame.path !== this.videoPath))
            throw new Error(typeof frame.error === 'string' ? frame.error : 'Invalid GPU worker answer');
    }
    challenge(width, height) {
        const random = crypto.randomBytes(4);
        return [-8192 - random.readUInt16LE(0), -8192 - random.readUInt16LE(2), width, height];
    }
    bind(path, rectangle, receive) {
        if (!this.ready || !this.socket || this.pending || typeof path !== 'string' || path[0] !== '/' ||
            Buffer.byteLength(path) > 107 || path.indexOf('\0') >= 0 || !Array.isArray(rectangle) || rectangle.length !== 4 ||
            !rectangle.every(Number.isInteger)) return false;
        this.view([0, 0, 408, 720], false);
        this.native = false;
        this.videoPath = null;
        this.binding = true;
        const body = Buffer.alloc(17 + Buffer.byteLength(path));
        rectangle.forEach((value, index) => body.writeInt32LE(value, index * 4));
        body.write(path, 16);
        this.pending = { kind:'bind', receive, timer:setTimeout(() => this.disconnect(new Error('GPU surface binding timed out')), 3000) };
        this.socket.write(message(0x444a0002, body));
        return true;
    }
    view(rectangle, shown) {
        if (!this.ready || !this.socket || !this.native) return;
        const values = rectangle.map(Math.round).concat(shown ? 1 : 0);
        const key = values.join(',');
        if (key === this.lastView) return;
        this.lastView = key;
        const body = Buffer.alloc(20);
        values.forEach((value, index) => body.writeInt32LE(value, index * 4));
        this.socket.write(message(0x444a0003, body));
    }
    setCursorImage(width, height, pixels) {
        if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1 ||
            width > 512 || height > 512 || !Buffer.isBuffer(pixels) || pixels.length !== width * height * 4)
            throw new Error('Invalid GPU cursor image');
        const body = Buffer.alloc(8 + pixels.length);
        body.writeUInt32LE(width, 0); body.writeUInt32LE(height, 4); pixels.copy(body, 8);
        this.cursorImage = message(0x444a0004, body);
        if (this.ready && this.native && this.socket) this.socket.write(this.cursorImage);
    }
    cursor(x, y, hotspotX, hotspotY, shown) {
        const body = Buffer.alloc(20);
        [x, y, hotspotX, hotspotY].forEach((value, index) => body.writeFloatLE(value, index * 4));
        body.writeUInt32LE(shown ? 1 : 0, 16);
        const packet = message(0x444a0005, body);
        if (this.cursorPosition && packet.equals(this.cursorPosition)) return;
        this.cursorPosition = packet;
        if (this.ready && this.native && this.socket) this.socket.write(packet);
    }
    render(data, receive) {
        if (!this.ready || !this.socket || this.pending) return false;
        const timer = setTimeout(() => this.disconnect(new Error('GPU worker frame timed out')), 5000);
        this.pending = { kind:'frame', receive, timer };
        this.socket.cork();
        this.socket.write(data);
        this.socket.write(END);
        this.socket.uncork();
        return true;
    }
    disconnect(error) {
        const wasReady = this.ready;
        this.ready = false;
        this.native = false;
        this.binding = false;
        this.lastError = error.message;
        const socket = this.socket;
        this.socket = null;
        if (socket) socket.destroy();
        const pending = this.pending;
        this.pending = null;
        if (pending) {
            clearTimeout(pending.timer);
            pending.receive(error);
        }
        if (wasReady && !this.closed && this.log && this.log.warn) this.log.warn('GPU worker: ' + error.message);
        if (this.onState) this.onState();
        if (!this.closed && !this.retry) {
            this.retry = setTimeout(() => { this.retry = null; this.connect(); }, 3000);
            this.retry.unref();
        }
    }
    close() {
        this.closed = true;
        if (this.retry) { clearTimeout(this.retry); this.retry = null; }
        this.disconnect(new Error('GPU worker stopped'));
    }
}
module.exports = { GpuWorker };

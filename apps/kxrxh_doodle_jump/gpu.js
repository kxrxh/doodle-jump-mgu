'use strict';
const net = require('net');

const END = Buffer.alloc(8);
END.writeUInt32LE(0x444a0001, 0);

class GpuWorker {
    constructor(log) {
        this.log = log;
        this.ready = false;
        this.generation = 0;
        this.closed = false;
        this.pending = null;
        this.retry = null;
        this.socket = null;
        this.lastError = null;
        this.onState = null;
    }
    connect() {
        if (this.closed || this.socket) return;
        const socket = this.socket = net.createConnection(process.env.DOODLE_GPU_SOCKET || '/run/doodle-gpu/render.sock');
        let input = '';
        socket.on('connect', () => {
            if (this.socket !== socket) return;
            this.ready = true;
            this.sequence = 0;
            this.videoPath = null;
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
            try {
                const frame = JSON.parse(input.slice(0, newline));
                if (!this.pending || newline !== input.length - 1 || !frame ||
                    typeof frame.path !== 'string' || !/^\/dev\/shm\/mgu-video-doodle-[A-Za-z0-9_.-]{1,48}$/.test(frame.path) ||
                    frame.width !== 408 || frame.height !== 720 || !Number.isInteger(frame.sequence) ||
                    frame.sequence !== (this.sequence === 0xffffffff ? 1 : this.sequence + 1) ||
                    (this.videoPath !== null && frame.path !== this.videoPath))
                    throw new Error(frame && typeof frame.error === 'string' ? frame.error : 'Invalid GPU worker answer');
                input = '';
                this.sequence = frame.sequence;
                this.videoPath = frame.path;
                const pending = this.pending;
                this.pending = null;
                clearTimeout(pending.timer);
                pending.receive(null, frame);
            } catch (error) { this.disconnect(error); }
        });
        socket.on('error', error => { if (this.socket === socket) this.disconnect(error); });
        socket.on('close', () => { if (this.socket === socket) this.disconnect(new Error('GPU worker disconnected')); });
    }
    render(data, receive) {
        if (!this.ready || !this.socket || this.pending) return false;
        const timer = setTimeout(() => this.disconnect(new Error('GPU worker frame timed out')), 5000);
        this.pending = { receive, timer };
        this.socket.cork();
        this.socket.write(data);
        this.socket.write(END);
        this.socket.uncork();
        return true;
    }
    disconnect(error) {
        const wasReady = this.ready;
        this.ready = false;
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

'use strict';
const net = require('net');
const endpoint = process.env.DOODLE_GPU_CHECK_SOCKET || '/run/doodle-gpu/render.sock';
const requested = Number(process.env.DOODLE_GPU_CHECK_TIMEOUT_MS || 90000);
const timeout = requested > 0 && requested <= 90000 ? requested : 90000;
const connection = net.connect(endpoint);
let buffer = '', finished = false;
function done(error) {
    if (finished) return;
    finished = true;
    clearTimeout(timer);
    connection.destroy();
    if (error) { console.error(error); process.exitCode = 1; }
    else console.log('Doodle Jump GPU worker ready');
}
const timer = setTimeout(() => done('GPU worker timeout'), timeout);
connection.on('connect', () => {
    // A private offscreen clear checks rendering and frame publication without showing a surface.
    const request = Buffer.alloc(44);
    request.writeUInt32LE(0x4d520008, 0);
    request.writeUInt32LE(28, 4);
    request.writeUInt32LE(544, 8);
    request.writeUInt32LE(960, 12);
    request.writeUInt32LE(1, 16);
    for (let offset = 20; offset < 36; offset += 4) request.writeFloatLE(1, offset);
    request.writeUInt32LE(0x444a0001, 36);
    connection.write(request);
});
connection.on('data', (data) => {
    if (finished) return;
    buffer += data.toString('utf8');
    if (buffer.length > 4096) return done('GPU worker answer is too large');
    const end = buffer.indexOf('\n');
    if (end < 0) return;
    try {
        const frame = JSON.parse(buffer.slice(0, end));
        if (!/^\/dev\/shm\/mgu-video-doodle-[A-Za-z0-9_.-]+$/.test(frame.path) ||
            frame.width !== 408 || frame.height !== 720 || !Number.isSafeInteger(frame.sequence) || frame.sequence < 1)
            throw new Error('GPU worker returned an invalid frame');
        done();
    } catch (error) { done('GPU worker: ' + error.message); }
});
connection.on('error', (error) => done('GPU worker: ' + (error.code || error.message)));
connection.on('close', () => done('GPU worker closed the connection before publishing a frame'));

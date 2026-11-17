'use strict';
const net = require('net');

if (process.argv[2] === '--removed') {
    console.log(JSON.stringify({ reboot: 'Restart to use the Store renderer without Doodle Jump GPU support' }));
} else {
    const endpoint = process.env.DOODLE_GPU_CHECK_SOCKET || '/run/mgu-render/render.sock';
    const requested = Number(process.env.DOODLE_GPU_CHECK_TIMEOUT_MS || 90000);
    const timeout = requested > 0 && requested <= 90000 ? requested : 90000;
    const connection = net.connect(endpoint);
    let buffer = Buffer.alloc(0), supported = false, finished = false;
    function done(error) {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        connection.destroy();
        if (error) { console.error(error); process.exitCode = 1; }
        else console.log('Doodle Jump GPU renderer ready');
    }
    const timer = setTimeout(() => done('GPU renderer timeout'), timeout);
    connection.on('connect', () => {
        const hello = Buffer.from('doodle-gpu-check\0proto=mgu-render/1');
        const packet = Buffer.alloc(8 + ((hello.length + 3) & ~3));
        packet.writeUInt32LE(1, 0); packet.writeUInt32LE(packet.length - 8, 4); hello.copy(packet, 8);
        connection.write(packet);
    });
    connection.on('data', (data) => {
        if (finished) return;
        if (buffer.length + data.length > 65536) return done('GPU renderer answer is too large');
        buffer = Buffer.concat([buffer, data]);
        while (buffer.length >= 8) {
            const kind = buffer.readUInt32LE(0), size = buffer.readUInt32LE(4);
            if (size > 4096) return done('GPU renderer answer is too large');
            if (buffer.length < 8 + size) return;
            const body = buffer.slice(8, 8 + size);
            buffer = buffer.slice(8 + size);
            if (kind === 0x4d520101) {
                if (size < 12 || body.readUInt32LE(0) !== 1 || !(body.readUInt32LE(8) & 256))
                    return done('Renderer does not support GPU scenes');
                supported = true;
            } else if (kind === 101) {
                if (!supported) return done('Renderer does not support GPU scenes');
                if (size < 12 || !body.readUInt32LE(4) || !body.readUInt32LE(8) ||
                    body.readUInt32LE(4) > 16384 || body.readUInt32LE(8) > 16384)
                    return done('GPU renderer returned an invalid screen size');
                return done();
            } else if (kind === 103) return done('GPU renderer: ' + body.toString('utf8'));
        }
    });
    connection.on('error', (error) => done('GPU renderer: ' + (error.code || error.message)));
    connection.on('close', () => done('GPU renderer closed the connection before READY'));
}

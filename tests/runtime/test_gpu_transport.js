'use strict';
const assert = require('assert');
const { Engine, PacketReader } = require('../../apps/kxrxh_doodle_jump/engine');
const record = require('../../kit/runtime/record');
const { RenderClient, SOCKETS } = require('../../kit/runtime/renderd');
assert.deepStrictEqual(SOCKETS, process.env.MGU_RENDERD_SOCKET ? [process.env.MGU_RENDERD_SOCKET] :
    ['/run/mgu-render/render.sock', '/run/mgu-renderd/renderd.sock'], 'prefer the socket checked by the Store GPU add-on');
const TX = 0x4d520007, SCENE = 0x4d520008;
function message(type, body) {
    const header = Buffer.alloc(8); header.writeUInt32LE(type, 0); header.writeUInt32LE(body.length, 4);
    return Buffer.concat([header, body]);
}
function texture(id, values) {
    const body = Buffer.alloc(values ? 16 : 12); body.writeUInt32LE(id, 0);
    if (values) { body.writeUInt32LE(1, 4); body.writeUInt32LE(1, 8); Buffer.from(values).copy(body, 12); }
    return message(TX, body);
}
function scene() {
    const body = Buffer.alloc(8 + 20 + 28 + 96);
    body.writeUInt32LE(408, 0); body.writeUInt32LE(720, 4);
    body.writeUInt32LE(1, 8); [0.1, 0.2, 0.3, 1].forEach((v, i) => body.writeFloatLE(v, 12 + i * 4));
    [2, 7, 3, 1, 771, 0, 3].forEach((v, i) => body.writeUInt32LE(v, 28 + i * 4));
    [0, 0, 0, 0, 1, 1, 1, 1, 408, 0, 1, 0, 1, 1, 1, 1, 0, 720, 0, 1, 1, 1, 1, 1]
        .forEach((v, i) => body.writeFloatLE(v, 56 + i * 4));
    return message(SCENE, body);
}
function frame(parts) { return Buffer.concat(parts || [texture(7, [1, 2, 3, 255]), scene(), texture(5)]); }
function engineWire(body) {
    const header = Buffer.alloc(8); header.write('GPF1', 0); header.writeUInt32LE(body.length, 4);
    return Buffer.concat([header, body]);
}
function readMessages(body) {
    const messages = [];
    for (let i = 0; i < body.length;) {
        const length = body.readUInt32LE(i + 4);
        messages.push({ type:body.readUInt32LE(i), body:body.slice(i + 8, i + 8 + length) }); i += 8 + length;
    }
    return messages;
}
const engine = new Engine('', { info() {}, error() {} }), delivered = [];
engine.onGPU = body => delivered.push(body);
engine.onFrame = () => assert.fail('GPU scenes do not publish CPU pixel frames');
engine.busy = true;
const reader = new PacketReader((type, body) => engine.packet(type, body), true);
const wire = engineWire(frame());
assert.doesNotThrow(() => reader.feed(wire.slice(0, 31)), 'GPU packets may arrive in fragments');
assert.strictEqual(engine.busy, true);
assert.strictEqual(delivered.length, 0);
reader.feed(wire.slice(31));
assert.strictEqual(engine.busy, false);
assert.deepStrictEqual(delivered, [frame()]);
reader.feed(engineWire(frame([scene()])));
assert.deepStrictEqual(delivered[0], frame(), 'later packets cannot overwrite pending texture uploads');

const bad = [];
const corrupt = (packet, offset, value, float) => { const copy = Buffer.from(packet); copy[float ? 'writeFloatLE' : 'writeUInt32LE'](value, offset); return copy; };
bad.push(frame().slice(0, -1), Buffer.concat([frame(), Buffer.from([0])]), frame([texture(0, [1, 2, 3, 4]), scene()]),
    frame([corrupt(texture(7, [1, 2, 3, 4]), 12, 2049), scene()]), frame([texture(7), scene()]),
    frame([scene(), texture(7, [1, 2, 3, 4])]), frame([scene(), scene()]), frame([texture(7, [1, 2, 3, 4])]),
    corrupt(scene(), 8, 4096), corrupt(scene(), 16, 99), corrupt(scene(), 20, NaN, true),
    corrupt(scene(), 44, 16), corrupt(scene(), 48, 9), corrupt(scene(), 52, 9),
    corrupt(scene(), 56, 9), corrupt(scene(), 60, 2), corrupt(scene(), 64, Infinity, true));
for (const body of bad) {
    engine.busy = true;
    assert.throws(() => engine.packet('GPF1', body), /GPU|scene|texture/i);
    assert.strictEqual(engine.busy, true, 'malformed GPU packets do not acknowledge a frame');
    assert.strictEqual(delivered.length, 2, 'validation completes before publishing any GPU resources');
}
const oversizeHeader = engineWire(Buffer.alloc(0)); oversizeHeader.writeUInt32LE((64 << 20) + 1, 4);
assert.throws(() => new PacketReader(() => {}).feed(oversizeHeader), /size/i);
const sampleEvent = data => Buffer.from(JSON.stringify(Object.assign({type:'timing'}, data)));
engine.packet('EVT1', sampleEvent({ wallMs:12.5, cpuMs:7 }));
assert.strictEqual(engine.frameTiming.wallMs, 12.5);
assert.throws(() => engine.packet('EVT1', sampleEvent({ wallMs:-1, cpuMs:7 })), /timing/);
engine.ready = true; engine.busy = false;
assert(engine.frame({}));
assert.strictEqual(engine.frameTiming, null, 'a new request cannot inherit an older timing');
console.log('GPU engine packet ownership and validation: passed');

const client = new RenderClient('game', { info() {} }, { native:true });
const sent = []; client._write = data => { sent.push(Buffer.from(data)); return true; };
const ready = Buffer.alloc(16); ready.writeUInt32LE(1, 0); ready.writeUInt32LE(1920, 4); ready.writeUInt32LE(720, 8);
const nativeReady = Buffer.alloc(56); nativeReady.writeUInt32LE(1, 0); nativeReady.writeUInt32LE(1, 4); nativeReady.writeUInt32LE(264, 8);
client._data(message(0x4d520101, nativeReady.slice(0, 52)));
assert.strictEqual(client.info.scenes, false, 'stock 0.6.25 advertises NV12, not game scenes');
nativeReady.write('GPF0', 52, 'ascii');
client._data(message(0x4d520101, nativeReady));
assert.strictEqual(client.info.scenes, false);
assert.strictEqual(client.info.shaderFailed, true, 'identify a loaded add-on whose scene shader could not start');
nativeReady.write('GPF1', 52, 'ascii');
client._data(message(0x4d520101, nativeReady)); client._data(message(101, ready));
assert.strictEqual(client.info.scenes, true);
assert.strictEqual(client.generation, 1);
client._data(message(101, ready)); assert.strictEqual(client.generation, 2, 'each ready session invalidates cached engine resources');
client.placement = { x:0, y:0, w:1920, h:720, scale:1, ox:0, oy:0 };
sent.length = 0;
record.clearScene(); record.setScene(frame());
const first = new record.Recorder(1920, 720).scene(600, 0, 408, 720);
const parent = new record.Recorder(1920, 720).append(first);
assert.strictEqual(parent.scenes, true, 'appending the fullscreen recorder keeps its scene resources');
assert.strictEqual(parent.words()[0], 20);
const floats = new Float32Array(parent.words().buffer, parent.words().byteOffset, parent.words().length);
assert.deepStrictEqual(Array.from(floats.slice(1, 5)), [600, 0, 408, 720]);
let rectangles = 0;
const overlay = new record.Recorder(1920, 720).append(first).fillRect(0, 0, 20, 10, '#fff');
record.play(overlay.words(), { fillRect() { rectangles++; } });
assert.strictEqual(rectangles, 1, 'software playback preserves overlays after a GPU-only scene');
assert(client.frame(parent));
assert.deepStrictEqual(sent.map(b => b.readUInt32LE(0)), [TX, SCENE, TX, 7], 'upload textures, replace scene, retire old textures, then present');
assert.deepStrictEqual(sent[0], texture(7, [1, 2, 3, 255]));
assert.deepStrictEqual(sent[1], scene());
const rendererStats = Buffer.alloc(12);
rendererStats.writeUInt32LE(30, 0);
const receivedAfter = Date.now();
client._data(message(104, rendererStats));
assert(client.stats.at >= receivedAfter, 'timestamp the renderer report so the overlay can expire it');
const firstVersion = parent.words()[5];
sent.length = 0; client.frame(parent);
assert.deepStrictEqual(sent.map(b => b.readUInt32LE(0)), [7], 'already queued texture pixels and scenes are not retained for later uploads');
record.setScene(frame([texture(9, [10, 20, 30, 40]), scene()]));
record.setScene(frame([scene(), texture(9)]));
const second = new record.Recorder(1920, 720).scene(600, 0, 408, 720);
assert.notStrictEqual(second.words()[5], firstVersion, 'new game frames change the recording even when its destination is fixed');
client.frame(second);
assert.deepStrictEqual(sent.slice(1).map(b => b.readUInt32LE(0)), [SCENE, TX, 7], 'a skipped frame does not upload a texture already retired by its successor');
record.clearScene();
assert.strictEqual(new record.Recorder(1920, 720).scene(600, 0, 408, 720).words().length, 0, 'reconnection hides the old scene until resources are refreshed');
parent.reset(); assert.strictEqual(parent.scenes, false);
const legacy = new RenderClient('game', { info() {} });
assert.throws(() => legacy._words(second.words()), /scene|GPU/i, 'scene commands never reach a legacy renderer');
record.setScene(frame());
const stale = new record.Recorder(1920, 720).scene(600, 0, 408, 720);
client._data(message(101, ready));
assert.strictEqual(client.stats, null, 'a new connection starts without old FPS data');
assert.strictEqual(client.frame(stale), false, 'a reconnect cannot publish a cached scene before the app refreshes its textures');
record.setScene(frame());
const generation = client.generation;
client.place({ x:10, y:20, w:1920, h:720, scale:.9, ox:0, oy:0 });
assert.strictEqual(client.generation, generation, 'moving a surface keeps its GPU resources');
client.place({ x:10, y:20, w:1790, h:638, scale:.9, ox:0, oy:0 });
assert.strictEqual(client.generation, generation + 1, 'resizing invalidates GPU textures held by the renderer');
assert.strictEqual(client.frame(stale), false, 'no obsolete scene is sent while waiting for a fresh texture set');
console.log('GPU capability, resource lifetime and presentation: passed');

client.log.warn = () => {};
record.setScene(frame());
client._data(message(103, Buffer.from('scene GPU: framebuffer allocation failed')));
assert.strictEqual(client.info.sceneFailed, true);
assert.strictEqual(client.info.scenes, false, 'GPU driver failures switch this session to software');
assert.strictEqual(client.ready, true, 'software fallback keeps the existing display connection');
assert.strictEqual(record.sceneState.frame, null, 'failed scenes release pending texture packets');
console.log('GPU driver failure fallback: passed');
client.video = true;
client.info.scenes = false;
record.setVideo(42, '/dev/shm/mgu-video-doodle-test');
const video = new record.Recorder(1920,720).video(42,600,0,408,720);
const containing = new record.Recorder(1920,720).append(video).fillRect(0,0,1,1,'#fff');
assert(containing.videos.has(42), 'appending retains video resources');
sent.length = 0;
client.frame(containing);
assert.deepStrictEqual(sent.map(b=>b.readUInt32LE(0)), [13,7]);
assert.strictEqual(sent[0].readUInt32LE(8),42);
assert.strictEqual(sent[0].toString('utf8',12).replace(/\0+$/,''), '/dev/shm/mgu-video-doodle-test');
assert.strictEqual(sent[1].readUInt32LE(8),19,'the game uses the public VIDEO draw op');
assert.strictEqual(sent[1].readUInt32LE(32),42);
sent.length=0;client.frame(containing);
assert.deepStrictEqual(sent.map(b=>b.readUInt32LE(0)), [7],'a video is registered once per connection');
client._reset();sent.length=0;client.frame(containing);
assert.deepStrictEqual(sent.map(b=>b.readUInt32LE(0)), [13,7],'reconnecting re-registers the existing worker file');
client.place({x:0,y:0,w:1600,h:520,scale:1,ox:0,oy:0});sent.length=0;client.frame(containing);
assert.deepStrictEqual(sent.map(b=>b.readUInt32LE(0)), [13,7],'resizing re-registers the video');
record.setVideo(42, '/dev/shm/mgu-video-doodle-reconnected');sent.length=0;client.frame(containing);
assert.deepStrictEqual(sent.map(b=>b.readUInt32LE(0)), [13,7],'a worker reconnect replaces the video path');
assert.throws(()=>record.setVideo(42,'/etc/passwd'),/video/);
rectangles=0;record.play(containing.words(),{fillRect(){rectangles++;}});
assert.strictEqual(rectangles,1,'software playback skips only the GPU video');
client._data(message(103,Buffer.from('video 42: cannot open frame')));
assert.strictEqual(client.video,false,'a renderer video failure selects the CPU path');
assert.strictEqual(client.ready,true,'video failure keeps the existing display connection');
console.log('Public video transport and renderer reconnect: passed');


{
    const fs = require('fs'), path = require('path'), vm = require('vm'), EventEmitter = require('events');
    const { Controls } = require('../../apps/kxrxh_doodle_jump/controls');
    let now = 1000, seq = 0, draw, focus, game, painting, visible = true, full = true;
    const timers = new Map(), requests = [], events = {};
    let showFps = false, paints = 0, kicks = 0, controllerInput, layoutChanged;
    const labels = [];
    const text = record.Recorder.prototype.text;
    record.Recorder.prototype.text = function(label, x, y, size, color, opts) {
        labels.push(label);
        return text.call(this, label, x, y, size, color, opts);
    };
    const gpu = new EventEmitter(); gpu.ready = true; gpu.native = true; gpu.info = { scenes:false }; gpu.video = true; gpu.generation = 1;
    function later(fn, delay) { const id = ++seq; timers.set(id, { fn, at:now + Math.max(1, delay) }); return id; }
    function advance(ms) {
        const end = now + ms;
        for (;;) {
            let next = null;
            timers.forEach((timer, id) => { if (timer.at <= end && (next === null || timer.at < timers.get(next).at)) next = id; });
            if (next === null) break;
            const timer = timers.get(next); timers.delete(next); now = Math.max(now, timer.at); timer.fn();
        }
        now = end;
    }
    class StubEngine {
        constructor() { game = this; this.ready = true; this.busy = false; this.playing = false; this.scenery = { sprites:{} }; this.worker = {ready:true,generation:1}; }
        start() {} send() {} stop() {}
        frame(command) { this.busy = true; requests.push(command); return true; }
        gpuFrame() { this.busy = false; this.onVideo({path:'/dev/shm/mgu-video-doodle-test',width:408,height:720,sequence:1}); }
        pixelFrame() { this.busy = false; this.onFrame(Buffer.alloc(408*720*4), 408, 720); }
    }
    const ctx = {
        dataDir:'', log:{ info() {}, warn() {} }, settings:{ get(key) { return key === 'showFps' ? showFps : true; } },
        ui:{ isVisible() { return visible; }, hasFocus() { return visible; }, text() {}, image(id, value) { painting = value; paints++; },
            open() {}, onClick() {}, onFocus(fn) { focus = fn; } },
        display:{ gpu, place:{ full:true }, active() { return visible; }, own() { return false; } }, controller:{ grab() {}, onInput(fn) { controllerInput = fn; } }, signals:{ watch() {} },
        hideButton() {}, onLayout(fn) { layoutChanged = fn; }, loop(name, fps, fn) { draw = fn; return { kick() { kicks++; } }; }, canvas(w, h) {
            const cv = new record.Recorder(w, h); cv.place = { full, w:1920, h:720 }; return cv;
        },
    };
    const dependencies = {
        './kit/mgu':{ run(spec) { spec.start(ctx); } }, './kit/record':record,
        './kit/parked':{ watch() { return { moving() { return false; } }; } }, './engine':{ Engine:StubEngine },
        './kit/sheet':{ cardSheet() {} }, './world':{ World:class { draw() {} } }, './controls':{ Controls },
    };
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../apps/kxrxh_doodle_jump/main.js'), 'utf8'), {
        require(name) { return dependencies[name]; }, Date:{ now() { return now; } }, setTimeout:later,
        clearTimeout(id) { timers.delete(id); }, setInterval() { return ++seq; }, clearInterval() {},
        process:{ on(name, fn) { events[name] = fn; }, exit() {} },
    });
    draw(); advance(17);
    assert.strictEqual(requests[0].gpu, true, 'a stock renderer with VIDEO selects the independent GPU worker');
    assert.strictEqual(requests[0].gpu_reset, true, 'the first GPU frame uploads a fresh texture set');
    game.gpuFrame(); draw();
    assert.strictEqual(painting.videos.has(1), true, 'the worker video replaces the center pixel image in the fullscreen recording');
    const fpsLabel = () => labels.filter(label => label.includes(' FPS')).pop();
    assert.strictEqual(fpsLabel(), undefined, 'FPS display is hidden when disabled');
    showFps = true; draw();
    assert.strictEqual(fpsLabel(), 'GPU | Game -- | Render -- FPS');
    ctx.display.lastInterruption = 'Renderer disconnected';
    draw();
    assert(labels.includes('Last | Renderer disconnected'), 'Show FPS retains the last display interruption after recovery');
    for (let i = 0; i < 50; i++) {
        advance(20); game.gpuFrame();
        if (i === 49) gpu.stats = { fps:30, at:now };
        draw();
    }
    assert.strictEqual(fpsLabel(), 'GPU | Game 50 | Render 30 FPS', 'counts completed game frames separately from Store render stats');
    advance(3000); draw();
    assert.strictEqual(fpsLabel(), 'GPU | Game 0 | Render -- FPS', 'stale renderer samples expire');
    showFps = false; labels.length = 0; draw();
    assert.strictEqual(fpsLabel(), undefined);
    game.playing = true; showFps = true; draw();
    advance(1001); draw();
    game.gpuFrame(); advance(1);
    assert.strictEqual(requests[requests.length-1].timing, true, 'sample an engine frame while Show FPS is enabled');
    game.frameTiming = { wallMs:12, cpuMs:7 };
    advance(32);
    const beforeKick = kicks;
    game.gpuFrame(); draw();
    assert(kicks > beforeKick, 'a completed game frame wakes an idle renderer');
    assert(labels.some(label => label === 'Engine 12 | CPU 7 | Trip 32 ms'), 'timings belong to one accepted request');
    const firstPaint = paints;
    for (let i=0;i<4;i++) { advance(16); draw(); }
    assert.strictEqual(paints, firstPaint, 'ticks without a new game frame do not repaint the side panels');
    controllerInput({frontRotaryEncoderDeflection:4}); draw();
    assert.strictEqual(paints, firstPaint+1, 'steering feedback does not wait for a new frame');
    layoutChanged(); draw();
    assert.strictEqual(paints, firstPaint+2, 'a layout change repaints the current frame');
    game.gpuFrame(); draw();
    assert.strictEqual(paints, firstPaint+3, 'a new gameplay frame is presented');
    game.playing = false; showFps = false;
    advance(17);
    assert.strictEqual(requests[1].gpu_reset, false, 'the same renderer session keeps its GPU textures');
    visible = false; focus(); draw();
    game.gpuFrame(); draw();
    visible = true; focus(); draw(); advance(17);
    assert.strictEqual(requests[requests.length - 1].gpu_reset, false, 'a brief focus loss keeps textures on the same renderer');
    game.gpuFrame(); draw(); advance(17);
    visible = false; focus(); gpu.generation++; draw();
    game.gpuFrame(); draw();
    assert.strictEqual(painting.videos.has(1), false, 'a late response from the old renderer session stays hidden');
    visible = true; focus(); draw(); advance(17);
    assert.strictEqual(requests[requests.length - 1].gpu_reset, false, 'a renderer reconnect preserves textures in the independent worker');
    game.gpuFrame(); draw(); advance(17);
    full = false;
    ctx.display.place.full = false;
    draw(); game.gpuFrame(); draw();
    assert(painting.videos.has(1), 'the native renderer can composite scenes inside the standard HMI viewport');
    advance(17);
    assert(requests[requests.length - 1].gpu, 'a page placement keeps GPU game capture enabled');
    game.worker.generation++;draw();game.gpuFrame();advance(17);
    assert.strictEqual(requests[requests.length-1].gpu_reset,true,'a worker reconnect re-uploads the texture set');
    game.gpuFrame();draw();
    game.worker.ready=false;game.worker.lastError='test failure';game.onGPUState();draw();
    assert(painting.videos.has(1),'worker failure keeps the last completed video until the CPU frame arrives');
    advance(17);game.gpuFrame();advance(17);
    assert.strictEqual(requests[requests.length-1].gpu,false,'worker failure requests a CPU frame');
    game.worker.ready=true;game.onGPUState();game.pixelFrame();advance(17);game.gpuFrame();draw();
    gpu.video = false; draw();
    game.gpuFrame(); draw();
    assert.strictEqual(painting.videos.has(1), false, 'loss of the renderer capability immediately removes native scene commands');
    advance(17);
    assert.strictEqual(requests[requests.length - 1].gpu, false, 'older renderers retain the software frame path');
    game.pixelFrame(); draw();
    assert.strictEqual(painting.videos.has(1), false);
    assert(painting.images.has(1), 'software fallback publishes the complete game image');
    showFps = true; draw();
    assert.strictEqual(fpsLabel(), 'CPU | Game -- | Render -- FPS');
    assert(labels.includes('Renderer video unavailable'));
    gpu.video = true; game.worker.ready = false; game.worker.lastError = 'disconnected'; draw();
    assert.strictEqual(labels[labels.length - 1], 'GPU worker unavailable');
    gpu.native = false; draw();
    assert.strictEqual(labels[labels.length - 1], 'Legacy renderer');
    record.Recorder.prototype.text = text;
    events.exit();
}
console.log('GPU selection, reconnect while paused and software fallback: passed');

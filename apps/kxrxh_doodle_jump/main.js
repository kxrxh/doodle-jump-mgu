'use strict';
const mgu = require('./kit/mgu');
const { Recorder, setImage, setScene, clearScene } = require('./kit/record');
const Parked = require('./kit/parked');
const { Engine } = require('./engine');
const { cardSheet } = require('./kit/sheet');
const { World } = require('./world');

const W = 1600, H = 520, IMAGE = 1;
const { Controls } = require('./controls');

mgu.run({
    async start(ctx) {
        const { ui, log, settings } = ctx;
        const parked = Parked.watch(ctx, log);
        const driving = () => settings.get('parkedOnly') !== false && parked.moving();
        const shown = () => ui.isVisible('GAME') && (!ui.hasFocus || ui.hasFocus('GAME'));
        const engine = new Engine(ctx.dataDir, log);
        let error = null, frames = 0, going = false, dialog = null;
        let recorder = null, lastFrame = Date.now(), lastPaint = Date.now(), lastCcu = 0, touchSource = null, lastMenu = '';
        let frameTimer = null, nextFrameAt = lastFrame + 1000/60, sideWidth = Math.ceil((W-H*544/960)/2*960/H);
        let gpuEnabled = false, gpuSource = null, gpuGeneration = 0, gpuReset = false, submitted = null, frameMode = null;
        let fpsSample = null;
        const controls = new Controls(() => engine.playing);
        const world = new World();
        engine.onFrame = (rgba, w, h, patches) => {
            if (acceptFrame(false)) { setImage(IMAGE, w, h, rgba, patches); frameMode = 'pixels'; frames++; }
            queueFrame();
        };
        engine.onGPU = (data) => {
            if (acceptFrame(true)) { setScene(data); frameMode = 'gpu'; frames++; }
            queueFrame();
        };
        engine.onTexture = (id,w,h,rgba) => setImage(id,w,h,rgba);
        engine.onError = (e) => { error = e; sync(); menu(); };
        engine.onDialog = (request) => {
            dialog = request;
            sync();
            let answered = false;
            const cancel = request.buttons.find((button) => button.key === 'negative') || request.buttons[request.buttons.length-1];
            function answer(button) {
                if (answered) return 'close';
                answered = true;
                engine.send({ dialog: { id: request.id, button: button.key } });
                dialog = null; sync();
                return 'close';
            }
            const sheet = cardSheet({ title: request.title, heading: request.title, note: request.message,
                actions: request.buttons.map((button) => ({ label: button.label, run: () => answer(button) })) });
            sheet.onClose = () => answer(cancel);
            if (!ctx.sheet(sheet)) { log.warn('Original game dialog needs the touch renderer: ' + request.message); answer(cancel); }
        };
        try { engine.start(); } catch (e) { engine.fail(e); }
        function menu() {
            const info = error ? 'Engine stopped: ' + error.message : driving() ? 'Parked only: the game waits while driving.' :
                'Ring / joystick: steer. Joystick: move menu cursor. Press: select / shoot. Touch sides: hold to steer; touch game: shoot / select.';
            if (info !== lastMenu) { lastMenu = info; ui.text('MENU_INFO', info); }
        }
        function sync() {
            const go = shown() && !driving() && !error && !dialog;
            if (go !== going) {
                going = go; controls.reset(); fpsSample = null;
                ctx.controller.grab(go);
                engine.send({ action: go ? 'resume' : 'pause' });
                lastFrame = Date.now();
                nextFrameAt = lastFrame + 1000/60;
                if (go) queueFrame(); else cancelFrame();
                if (!go && shown() && driving()) { log.info('car moving: original game paused'); ui.open('MAIN'); }
            }
            menu();
        }
        function cancelFrame() {
            if (frameTimer !== null) { clearTimeout(frameTimer); frameTimer = null; }
        }
        function updateGPU() {
            const display = ctx.display, gpu = display && display.gpu;
            const enabled = !!(gpu && gpu.ready && gpu.native && gpu.info && gpu.info.scenes && display.place && display.place.full && display.active('GAME'));
            const source = enabled ? gpu : null, generation = enabled ? gpu.generation : 0;
            if (enabled === gpuEnabled && source === gpuSource && generation === gpuGeneration) return;
            gpuEnabled = enabled; gpuSource = source; gpuGeneration = generation;
            gpuReset = enabled; frameMode = null; fpsSample = null; clearScene();
        }
        function acceptFrame(gpu) {
            updateGPU();
            return submitted && submitted.gpu === gpu && gpuEnabled === gpu &&
                (!gpu || (submitted.source === gpuSource && submitted.generation === gpuGeneration));
        }
        function queueFrame() {
            if (frameTimer !== null || !going || !engine.ready || engine.busy || engine.dead) return;
            // A completed engine frame need not wait for the next renderer tick.
            frameTimer = setTimeout(requestFrame, Math.max(0, nextFrameAt-Date.now()));
        }
        function requestFrame() {
            frameTimer = null; sync();
            if (!going || !engine.ready || engine.busy || engine.dead) return;
            updateGPU();
            const now = Date.now(), dt = Math.min(.1, Math.max(1/120, (now-lastFrame)/1000));
            const previous = submitted;
            submitted = { gpu:gpuEnabled, source:gpuSource, generation:gpuGeneration };
            if (engine.frame(Object.assign({ dt, side_width:sideWidth, gpu:gpuEnabled, gpu_reset:gpuReset }, controls.take()))) {
                gpuReset = false;
                lastFrame = now;
                nextFrameAt = Math.max(nextFrameAt + 1000/60, now);
            } else submitted = previous;
        }
        const controller = (v) => { if (going) controls.raw(v); else controls.observe(v); };
        ctx.controller.onInput((v) => { lastCcu = Date.now(); controller(v); });
        ctx.signals.watch('de.bmw.infotainment.controlelements.FrontCCU', '1', {
            statusRotaryEncoderState: (v) => { if (Date.now() - lastCcu > 1000) controller(v); },
        }, log);
        function touch(phase, x, y) { if (going) controls.touch(phase, x, y); }
        // Listen to the renderer's existing down/move/up stream for held steering.
        function connectTouch() {
            const gpu = ctx.display && ctx.display.gpu;
            if (gpu === touchSource) return;
            if (touchSource) touchSource.removeListener('touch', touch);
            touchSource = gpu;
            if (gpu) gpu.on('touch', touch);
        }
        ui.onClick('BTN_PLAY', () => { ui.open('GAME'); sync(); });
        ui.onClick('BTN_SELECT', () => { if (going) controls.select(); });
        ui.onClick('BTN_BACK', () => engine.send({ action: 'back' }));
        ui.onClick('BTN_LEFT', () => { if (going) controls.steer(-1); });
        ui.onClick('BTN_RIGHT', () => { if (going) controls.steer(1); });
        ['BTN_LEFT','BTN_SELECT','BTN_RIGHT','BTN_BACK'].forEach((id) => ctx.hideButton(id));
        if (ui.onFocus) ui.onFocus(sync);
        const syncTimer = setInterval(sync, 200);
        function stop() { cancelFrame(); clearInterval(syncTimer); engine.stop(); }
        process.on('exit', stop);
        ['SIGINT', 'SIGTERM'].forEach((signal) => process.on(signal, () => { stop(); process.exit(0); }));
        function drawCursor(cv,w,h) {
            const cursor = engine.scenery.sprites.cursor;
            if (!frames || engine.playing || error || !cursor) return;
            const gw=h*544/960,gx=(w-gw)/2,scale=32*h/720/cursor.height;
            cv.image(cursor.id,gx+controls.cursor.x*gw/544-cursor.hotspotX*scale,
                controls.cursor.y*h/960-cursor.hotspotY*scale,cursor.width*scale,cursor.height*scale);
        }
        function drawFps(cv, side, height, now, renderer) {
            if (!going || !settings.get('showFps') || !frameMode) {
                fpsSample = null;
                return;
            }
            if (!fpsSample || now < fpsSample.at) {
                fpsSample = { start: now, at: now, frames, rate: '--' };
            }
            const elapsed = now - fpsSample.at;
            if (elapsed >= 1000) {
                fpsSample.rate = Math.round((frames - fpsSample.frames) * 1000 / elapsed);
                fpsSample.frames = frames;
                fpsSample.at = now;
            }
            const stats = renderer && renderer.ready && renderer.stats;
            const fresh = stats && stats.at >= fpsSample.start && now >= stats.at && now - stats.at < 2500;
            const mode = frameMode === 'gpu' ? 'GPU' : 'CPU';
            const label = mode + ' | Game ' + fpsSample.rate + ' | Render ' + (fresh ? stats.fps : '--') + ' FPS';
            const size = Math.min(22 * height / 720, side / 20);
            if (size < 9) return;
            cv.fillRect(size / 2, size / 2, 18 * size, 2 * size, '#342b25');
            cv.text(label, size, 1.88 * size, size, '#ffffff', { literal: true });
        }
        ctx.loop('GAME', 60, () => {
            sync(); connectTouch(); updateGPU();
            const now = Date.now();
            const paintDt = Math.min(.1,Math.max(0,(now-lastPaint)/1000));
            lastPaint = now;
            const cv = ctx.canvas(W, H).clear('#fff3e3'), p = cv.place;
            controls.place = p;
            const sw = p && p.full ? p.w : W, sh = p && p.full ? p.h : H;
            sideWidth = Math.max(0,Math.min(2048,Math.ceil((sw-sh*544/960)/2*960/sh)));
            controls.move(paintDt); queueFrame();
            const message = error ? 'Engine stopped: ' + error.message : !frames ? 'Loading original Doodle Jump...' : null;
            if (p && p.full) {
                if (!recorder || recorder.w !== p.w || recorder.h !== p.h) recorder = new Recorder(p.w, p.h);
                recorder.reset();
                world.draw(recorder,p.w,p.h,engine.scenery,controls.steering(),engine.playing,going?paintDt:0);
                const gw = p.h*544/960, gx = (p.w-gw)/2;
                if (frameMode === 'gpu') recorder.scene(gx, 0, gw, p.h);
                else if (frameMode === 'pixels') recorder.image(IMAGE, gx, 0, gw, p.h);
                if (message) recorder.text(message, p.w/2, p.h/2, 24, '#573f2b', { align: 'center', font: 'bold' });
                drawCursor(recorder,p.w,p.h);
                drawFps(recorder, gx, p.h, now, ctx.display.gpu);
                cv.xf(1, 0, 0).append(recorder).xf();
            } else {
                const gw = H*544/960;
                world.draw(cv,W,H,engine.scenery,controls.steering(),engine.playing,going?paintDt:0);
                if (frameMode === 'pixels') cv.image(IMAGE, (W-gw)/2, 0, gw, H);
                if (message) cv.text(message, W/2, H/2, 24, '#573f2b', { align: 'center', font: 'bold' });
                drawCursor(cv,W,H);
                drawFps(cv, (W-gw)/2, H, now, p && ctx.display.gpu);
            }
            return ui.image('GAME_GFX', cv);
        });
        menu();
        log.info('original Doodle Jump app started');
    },
});

'use strict';
const mgu = require('./kit/mgu');
const { Recorder, setImage } = require('./kit/record');
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
        const controls = new Controls(() => engine.playing);
        const world = new World();
        engine.onFrame = (rgba, w, h) => { setImage(IMAGE, w, h, rgba); frames++; };
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
                going = go; controls.reset();
                ctx.controller.grab(go);
                engine.send({ action: go ? 'resume' : 'pause' });
                lastFrame = Date.now();
                if (!go && shown() && driving()) { log.info('car moving: original game paused'); ui.open('MAIN'); }
            }
            menu();
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
        setInterval(sync, 200);
        process.on('exit', () => engine.stop());
        ['SIGINT', 'SIGTERM'].forEach((signal) => process.on(signal, () => { engine.stop(); process.exit(0); }));
        function drawCursor(cv,w,h) {
            const cursor = engine.scenery.sprites.cursor;
            if (!frames || engine.playing || error || !cursor) return;
            const gw=h*544/960,gx=(w-gw)/2,scale=32*h/720/cursor.height;
            cv.image(cursor.id,gx+controls.cursor.x*gw/544-cursor.hotspotX*scale,
                controls.cursor.y*h/960-cursor.hotspotY*scale,cursor.width*scale,cursor.height*scale);
        }
        ctx.loop('GAME', 60, () => {
            sync(); connectTouch();
            const now = Date.now(), dt = Math.min(.1, Math.max(1/120, (now-lastFrame)/1000));
            const paintDt = Math.min(.1,Math.max(0,(now-lastPaint)/1000));
            lastPaint = now;
            const cv = ctx.canvas(W, H).clear('#fff3e3'), p = cv.place;
            controls.place = p;
            const sw = p && p.full ? p.w : W, sh = p && p.full ? p.h : H;
            const sideWidth = Math.max(0,Math.min(2048,Math.ceil((sw-sh*544/960)/2*960/sh)));
            controls.move(dt);
            if (going && engine.ready && !engine.busy && !engine.dead && engine.frame(Object.assign({ dt, side_width:sideWidth }, controls.take()))) lastFrame = now;
            const message = error ? 'Engine stopped: ' + error.message : !frames ? 'Loading original Doodle Jump...' : null;
            if (p && p.full) {
                if (!recorder || recorder.w !== p.w || recorder.h !== p.h) recorder = new Recorder(p.w, p.h);
                recorder.reset();
                world.draw(recorder,p.w,p.h,engine.scenery,controls.steering(),engine.playing,going?paintDt:0);
                const gw = p.h*544/960, gx = (p.w-gw)/2;
                if (frames) recorder.image(IMAGE, gx, 0, gw, p.h);
                if (message) recorder.text(message, p.w/2, p.h/2, 24, '#573f2b', { align: 'center', font: 'bold' });
                drawCursor(recorder,p.w,p.h);
                cv.xf(1, 0, 0).append(recorder).xf();
            } else {
                const gw = H*544/960;
                world.draw(cv,W,H,engine.scenery,controls.steering(),engine.playing,going?paintDt:0);
                if (frames) cv.image(IMAGE, (W-gw)/2, 0, gw, H);
                if (message) cv.text(message, W/2, H/2, 24, '#573f2b', { align: 'center', font: 'bold' });
                drawCursor(cv,W,H);
            }
            return ui.image('GAME_GFX', cv);
        });
        menu();
        log.info('original Doodle Jump app started');
    },
});

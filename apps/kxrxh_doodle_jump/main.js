'use strict';
const mgu = require('./kit/mgu');
const { Recorder, setImage, setVideo } = require('./kit/record');
const { cardSheet } = require('./kit/sheet');
const { Session } = require('./engine');
const W = 1600, H = 520;
const OPS = { alpha:1, clip:4, image:5, line:6, noclip:0, fillRect:5, video:5, text:-1 };

function validateDraw(frame) {
    if (!Number.isInteger(frame.w) || !Number.isInteger(frame.h) || frame.w<1 || frame.h<1 ||
        frame.w>8192 || frame.h>8192 || typeof frame.full!=='boolean' || !Array.isArray(frame.commands) || frame.commands.length>16384)
        throw new Error('Invalid drawing frame');
    frame.commands.forEach(command => {
        if (!Array.isArray(command) || !Object.prototype.hasOwnProperty.call(OPS, command[0]) ||
            (OPS[command[0]]<0 ? command.length<6 || command.length>7 : command.length!==OPS[command[0]]+1))
            throw new Error('Invalid drawing command');
    });
    return frame;
}
function paint(canvas, commands) { commands.forEach(command => canvas[command[0]].apply(canvas, command.slice(1))); }

mgu.run({
    async start(ctx) {
        const { ui, log, settings } = ctx;
        const session = new Session(ctx.dataDir, log), identities = new WeakMap();
        let nextIdentity=0, lastUI='', source=null, binding=null, latest=null, recorder=null, gameLoop=null, stopped=false, failure=null;
        function identity(object) {
            if (!object || (typeof object!=='object' && typeof object!=='function')) return 0;
            if (!identities.has(object)) identities.set(object,++nextIdentity);
            return identities.get(object);
        }
        const touch = (phase,x,y) => session.send({touch:{phase,x,y}});
        function connectRenderer() {
            const gpu=ctx.display && ctx.display.gpu;
            if (gpu===source) return;
            if (source) {
                source.removeListener('touch',touch);
                ['ready','state','stats','close','closed','tick'].forEach(event=>source.removeListener(event,reportUI));
            }
            source=gpu;
            if (gpu) {
                gpu.on('touch',touch);
                ['ready','state','stats','close','closed','tick'].forEach(event=>gpu.on(event,reportUI));
            }
        }
        function snapshot() {
            const display=ctx.display, gpu=display && display.gpu, p=display && display.place;
            return {shown:ui.isVisible('GAME'),focused:!ui.hasFocus || ui.hasFocus('GAME'),
                active:!!(display && display.active('GAME')),sheet:!!(display && display.sheet),
                parkedOnly:settings.get('parkedOnly')!==false,showFps:!!settings.get('showFps'),
                place:p?{x:p.x,y:p.y,w:p.w,h:p.h,scale:p.scale,ox:p.ox,oy:p.oy,full:!!p.full}:null,
                renderer:{ready:!!(gpu && gpu.ready),native:!!(gpu && gpu.native),video:!!(gpu && gpu.video),
                    visible:!!(gpu && gpu.visible),onScreen:!!(gpu && gpu.onScreen),generation:gpu && gpu.generation || 0,
                    source:identity(gpu),socket:identity(gpu && gpu.sock),path:gpu && gpu.path || '',
                    stats:gpu && gpu.stats?{fps:gpu.stats.fps,at:gpu.stats.at}:null,
                    interruption:display && display.lastInterruption || ''}};
        }
        function reportUI(force) {
            if (stopped) return;
            connectRenderer();
            const state=snapshot(), encoded=JSON.stringify(state);
            if (force===true || encoded!==lastUI) { lastUI=encoded; session.send({ui:state}); }
        }
        function bindSurface(request) {
            const display=ctx.display, gpu=display && display.gpu, p=display && display.place;
            let ok=false;
            try {
                if (request.stage==='prepare') {
                    const rect=request.rectangle;
                    if (gpu && gpu.ready && gpu.visible===true && display.active('GAME') && p &&
                        request.source===identity(gpu) && request.socket===identity(gpu.sock) &&
                        Array.isArray(rect) && rect.length===4 && rect.every(Number.isInteger) && rect[2]===p.w && rect[3]===p.h) {
                        binding={id:request.id,gpu,socket:gpu.sock};
                        gpu.show(false);gpu.place(Object.assign({},p,{x:rect[0],y:rect[1]}));ok=true;
                    }
                } else if (request.stage==='restore') {
                    const saved=binding;binding=null;
                    if (saved && saved.id===request.id && saved.gpu===gpu && saved.socket===gpu.sock && p) {
                        gpu.place(Object.assign({},p));gpu.show(!!display.active('GAME'));ok=true;
                    }
                } else throw new Error('Invalid binding stage');
            } catch (error) { log.warn('Store surface binding: '+error.message); }
            reportUI(true);
            session.send({binding:{id:request.id,stage:request.stage==='prepare'?'prepared':'restored',ok}});
        }
        function showDialog(request) {
            let answered=false;
            const cancel=request.buttons.find(button=>button.key==='negative') || request.buttons[request.buttons.length-1];
            function answer(button) {
                if (!answered) { answered=true;session.send({dialog:{id:request.id,button:button.key}}); }
                return 'close';
            }
            const sheet=cardSheet({title:request.title,heading:request.title,note:request.message,
                actions:request.buttons.map(button=>({label:button.label,run:()=>answer(button)}))});
            sheet.onClose=()=>answer(cancel);
            if (!ctx.sheet(sheet)) answer(cancel);
            reportUI(true);
        }
        function kick() {
            const gpu=ctx.display && ctx.display.gpu;
            if (gameLoop && !(gpu && gpu.waiting)) gameLoop.kick();
        }
        session.onPixels=(rgba,w,h,patches)=>setImage(1,w,h,rgba,patches);
        session.onTexture=(id,w,h,rgba)=>setImage(id,w,h,rgba,undefined,id===2?1:0);
        session.onError=error=>{
            if (binding) bindSurface({id:binding.id,stage:'restore'});
            failure='Engine stopped: '+error.message;ctx.controller.grab(false);ui.text('MENU_INFO',failure);kick();
        };
        session.onEvent=event=>{
            switch (event.type) {
            case 'ready': reportUI(true); break;
            case 'draw': {
                const frame=validateDraw(event), display=ctx.display, p=display && display.active('GAME') && display.place;
                if (frame.full!==!!(p && p.full) || (frame.full ? frame.w!==p.w || frame.h!==p.h : frame.w!==W || frame.h!==H)) break;
                latest=frame;kick();break;
            }
            case 'video': setVideo(1,event.path);break;
            case 'lifecycle':
                ctx.controller.grab(event.running);ui.text('MENU_INFO',event.info);
                if (event.open) ui.open(event.open);
                reportUI();break;
            case 'binding': bindSurface(event);break;
            case 'dialog': showDialog(event);break;
            case 'presented': {
                const gpu=ctx.display && ctx.display.gpu;
                if (gpu && event.source===identity(gpu) && event.socket===identity(gpu.sock) && Number.isFinite(event.fps)) {
                    if (gpu.fps) gpu.fps(Math.round(event.fps));kick();
                }
                break;
            }
            }
        };
        ctx.controller.onInput(value=>session.send({ccu:{source:'controller',value}}));
        ctx.signals.watch('de.bmw.infotainment.controlelements.FrontCCU','1',{
            statusRotaryEncoderState:value=>session.send({ccu:{source:'signal',value}}),
        },log);
        Promise.resolve(ctx.signals.watch('de.bmw.infotainment.vehicle.Vehicle2','1',{
            statusCarSpeed2:value=>session.send({vehicle:value}),
        },log,{pollMs:2000})).then(status=>log.info('parked check: speed '+(status && status.error?'error: '+status.error:'subscribed')));
        Object.entries({BTN_PLAY:'play',BTN_SELECT:'select',BTN_BACK:'back',BTN_LEFT:'left',BTN_RIGHT:'right'}).forEach(([id,button])=>ui.onClick(id,()=>session.send({button})));
        ['BTN_LEFT','BTN_SELECT','BTN_RIGHT','BTN_BACK'].forEach(id=>ctx.hideButton(id));
        if (ui.onFocus) ui.onFocus(reportUI);
        if (ui.onVisible) ui.onVisible(reportUI);
        if (settings.onChange) settings.onChange(reportUI);
        if (ctx.onLayout) ctx.onLayout(()=>{latest=null;reportUI(true);});
        const heartbeat=setInterval(()=>reportUI(true),200);
        function stop() {
            if (stopped) return;
            if (binding) bindSurface({id:binding.id,stage:'restore'});
            stopped=true;clearInterval(heartbeat);ctx.controller.grab(false);session.stop();
        }
        process.on('exit',stop);
        ['SIGINT','SIGTERM'].forEach(signal=>process.on(signal,()=>{stop();process.exit(0);}));
        gameLoop=ctx.loop('GAME',60,()=>{
            reportUI();
            const cv=ctx.canvas(W,H).clear('#fff3e3');
            if (latest && !failure) {
                if (latest.full) {
                    if (!recorder || recorder.w!==latest.w || recorder.h!==latest.h) recorder=new Recorder(latest.w,latest.h);
                    recorder.reset();paint(recorder,latest.commands);cv.xf(1,0,0).append(recorder).xf();
                } else paint(cv,latest.commands);
            } else cv.text(failure || 'Loading original Doodle Jump...',W/2,H/2,24,'#573f2b',{align:'center',font:'bold'});
            return ui.image('GAME_GFX',cv);
        });
        try { session.start();reportUI(true); } catch (error) { session.fail(error); }
    },
});

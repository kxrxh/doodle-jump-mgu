'use strict';

// Kit runtime (display.js, renderd.js, mgu.js Settings) against a stand-in mgu-render on a Unix socket: the
// unchanged-frame skip and its keepalive, a sheet that throws, X_STATE front flips, a reconnect after a cut
// message, the theme kept on a fresh install. No SDK, renderer or car needed:
//   node kit/tools/test_runtime.js

const assert = require('assert');
const fs = require('fs');
const net = require('net');
const os = require('os');
const path = require('path');

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'mgu-rt-'));
process.env.MGU_RENDERD_SOCKET = path.join(dir, 'render.sock');
['MGU_RENDERER', 'MGU_RENDERD_FILE', 'MGU_RENDERD_PROTO', 'MGU_NO_GRAB'].forEach((k) => delete process.env[k]);
const { Display } = require('../runtime/display');
const { RenderClient } = require('../runtime/renderd');
const themes = require('../runtime/themes');

const FRAME = 7, READY = 101, TICK = 102, TOUCH = 105, X_READY = 0x4d520101, X_STATE = 0x4d520102, X_CCU = 0x4d520103;
const uncaught = [];
process.on('uncaughtException', (e) => uncaught.push(e));     // (else node exits, as mgu.js does on the car)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const u32 = (...a) => Buffer.from(new Uint32Array(a).buffer);
function msg(type, p) {
    const b = Buffer.alloc(8 + p.length + ((4 - (p.length & 3)) & 3));
    b.writeUInt32LE(type, 0); b.writeUInt32LE(b.length - 8, 4); p.copy(b, 8);
    return b;
}
async function waitFor(fn, ms) {
    for (const t0 = Date.now(); Date.now() - t0 < (ms || 2000); await sleep(10)) if (fn()) return true;
    return false;
}

// the renderer's side: the app's FRAMEs (time of each, a TICK back for each), READY on connect (onConn)
let onConn = null;
const server = net.createServer((s) => {
    const c = { s, frames: [], buf: Buffer.alloc(0), send: (t, p) => s.write(msg(t, p)) };
    s.on('data', (d) => {
        c.buf = Buffer.concat([c.buf, d]);
        while (c.buf.length >= 8) {
            const t = c.buf.readUInt32LE(0), n = c.buf.readUInt32LE(4);
            if (c.buf.length < 8 + n) break;
            c.buf = c.buf.slice(8 + n);
            if (t === FRAME) { c.frames.push(Date.now()); c.send(TICK, u32(c.frames.length, 0)); }
        }
    });
    s.on('error', () => {});
    onConn(c);
});
const ready = (c) => { c.send(X_READY, u32(1, 1, 7)); c.send(READY, u32(4, 1920, 720, 1)); };   // touch, controller, front gate
const ccu = (k, pb, df) => { const b = Buffer.alloc(5); b.writeUInt16BE(k, 1); b[3] = pb; b[4] = df; return b; };
const touch = (phase, x, y) => { const b = Buffer.alloc(12); b.writeUInt32LE(phase, 0); b.writeFloatLE(x, 4); b.writeFloatLE(y, 8); return b; };

/** A Display on the stand-in renderer, the kit running the screen (full-screen layout, touch), shown. */
async function setup(screen) {
    let conn = null;
    onConn = (c) => { conn = c; ready(c); };
    const log = { errors: [], info() {}, warn() {}, debug() {}, error: (...a) => log.errors.push(a) };
    const vis = new Set(['MAIN']), visH = [], focusH = [];
    let focused = true;
    const ui = {
        rhmi: null, close() {}, visible() {}, onVisible: (fn) => visH.push(fn), isVisible: (n) => vis.has(n),
        onFocus: (fn) => focusH.push(fn), hasFocus: () => focused,
        setFocus(v) { focused = v; focusH.forEach(fn => fn('MAIN', v)); },
        setVisible: (v) => { if (v) vis.add('MAIN'); else vis.delete('MAIN'); visH.forEach((fn) => fn('MAIN', v)); },
    };
    const spec = { id: 'test', title: 'Test', screens: [Object.assign({ name: 'MAIN', kind: 'canvas', buttons: [['BTN_SETTINGS', 'Settings']] },screen)] };
    const d = new Display(ui, log, spec, { get: () => undefined, onChange() {} }, () => themes.theme());
    assert(await waitFor(() => conn && d.active() && d.own()), 'display on the renderer');
    return { d, ui, log, c: conn, end() { d.closeSheet(); d.gpu.close(); clearInterval(d.keepTimer); } };
}
/** The app draws its page (v: what changes) and hands it over, as ui.image does. */
function page(d, v) {
    const r = d.canvas(1600, 520);
    r.fillRect(100, 100, 400, 200, '#ff0000'); r.fillRect(0, 300, v, 10, '#00ff00');
    d.present('MAIN', r, () => Promise.resolve());
}

const cases = [];
const test = (name, fn) => cases.push([name, fn]);

test('a chrome-free pixel game opts into the native touch transport', async () => {
    const t=await setup({chrome:false,nativePixels:true});
    try {
        assert.strictEqual(t.d.gpu.opts.native, true);
        assert(t.d.own());
        assert(!t.d.lastInterruption);
        assert(await waitFor(() => t.d.gpu.visible));
        t.ui.setFocus(false);
        assert.strictEqual(t.d.gpu.visible, false, 'an HMI popup immediately hides the game');
        t.ui.setFocus(true);
        assert.strictEqual(t.d.lastInterruption, 'HMI focus lost');
        assert.strictEqual(t.d.gpu.visible, true, 'returning to the same visible game does not replay the page-opening delay');
        t.d.gpu.emit('state', {onScreen:true, front:true});
        t.d.gpu.emit('state', {onScreen:false, front:true});
        assert.strictEqual(t.d.lastInterruption, 'Layer hidden');
        t.d.gpu.emit('close');
        assert.strictEqual(t.d.lastInterruption, 'Renderer disconnected');
        t.ui.setVisible(false); t.ui.setVisible(true); t.ui.setFocus(true);
        assert.strictEqual(t.d.gpu.visible, false, 'opening a new page still waits for its animation');
    }
    finally {t.end();}
});

test('native pixel images reuse a pixmap and keep touch claims with controller grab', () => {
    const g=new RenderClient('pixels',{info(){},warn(){}}),writes=[];
    g.native=true;g.ready=true;g.touch=true;g.info={pixmaps:true};
    g._write=(b)=>{writes.push(Buffer.from(b));return true;};
    const {setImage}=require('../runtime/record');
    setImage(90,2,1,Buffer.from([255,0,0,255,0,100,0,128]));
    g._ensureImage(90);
    assert.deepStrictEqual(writes.map(b=>b.readUInt32LE(0)),[0x4d520003,0x4d520004]);
    assert.strictEqual(writes[1].readUInt32LE(12),1,'one update rectangle');
    assert.deepStrictEqual(Array.from(writes[1].slice(32)),[255,0,0,255,0,100,0,128]);
    g._ensureImage(90);assert.strictEqual(writes.length,2,'unchanged image is cached');
    setImage(90,2,1,Buffer.alloc(8));g._ensureImage(90);
    assert.strictEqual(writes.length,3,'new pixels reuse the existing texture');
    g.grabbing=true;g.claim([{x:0,y:0,w:2,h:1}]);
    assert.strictEqual(writes[3].readUInt32LE(0),9,'claims work beside native pixel images and grab');
    const {Recorder}=require('../runtime/record'),r=new Recorder(1920,720);
    r.clear('#fff').gradientRect(0,0,20,20,[[0,'#fff'],[1,'#000']]).image(90,10,20,30,40).line(1,2,3,4,1,'#000');
    const source=r.words(),native=g._words(source),at=source.indexOf(18);
    assert.deepStrictEqual(Array.from(native.slice(at+1,at+5)),Array.from(source.slice(at+3,at+7)));
    assert.strictEqual(native[at+5],0);assert.strictEqual(native[at+6],90);
    assert.strictEqual(source[at+1],90,'software/upstream recording stays intact');
});

test('software images preserve fade opacity and skip transformed chrome', () => {
    const {Canvas}=require('../runtime/canvas'),{Recorder,setImage,play}=require('../runtime/record');
    setImage(4001,1,1,Buffer.from([255,0,0,255]));
    const r=new Recorder(3,1),cv=new Canvas(3,1);
    r.clear('#0000ff').alpha(0).image(4001,0,0,1,1).alpha(.5).image(4001,1,0,1,1);
    r.alpha(1).xf(1,0,0).image(4001,2,0,1,1).xf();
    play(r.words(),cv);
    assert.deepStrictEqual(Array.from(cv.d.slice(0,4)),[0,0,255,255],'alpha zero draws nothing');
    assert.deepStrictEqual(Array.from(cv.d.slice(4,8)),[128,0,128,255],'half opacity blends over the destination');
    assert.deepStrictEqual(Array.from(cv.d.slice(8,12)),[0,0,255,255],'XF chrome remains skipped');
    const old=new Canvas(1,1);
    old.drawImage(Buffer.from([9,8,7,0]),1,1,0,0,1,1);
    assert.deepStrictEqual(Array.from(old.d),[9,8,7,255],'default drawImage callers keep opaque copy semantics');
});

test('software premultiplied images blend over opaque and transparent canvases', () => {
    const {Canvas}=require('../runtime/canvas'),{Recorder,setImage,play}=require('../runtime/record');
    setImage(4002,1,1,Buffer.from([64,0,0,128]));
    for(const [background,expected] of [[[0,0,200,255],[32,0,150,255]],
                                      [[0,0,0,0],[128,0,0,64]],
                                      [[0,100,0,128],[51,60,0,160]]]){
        const r=new Recorder(1,1),cv=new Canvas(1,1);
        r.clear(background).alpha(.5).image(4002,0,0,1,1);play(r.words(),cv);
        assert.deepStrictEqual(Array.from(cv.d),expected);
    }
    const straight=new Canvas(1,1);
    straight.drawImage(Buffer.from([255,0,0,128]),1,1,0,0,1,1,{alpha:.5});
    assert.deepStrictEqual(Array.from(straight.d),[255,0,0,64],'straight RGBA can opt into source-over');
    const edge=new Canvas(1,1);
    edge.drawImage(Buffer.from([255,0,0,255,0,0,255,0]),2,1,0,0,1,1,{linear:true});
    assert.deepStrictEqual(Array.from(edge.d),[255,0,0,128],'filtering premultiplies each source texel before interpolation');
});

test('software filtered paper clips wrapped borders without seams, including scaled canvases', () => {
    const {Canvas,ScaledCanvas}=require('../runtime/canvas'),{Recorder,setImage,play}=require('../runtime/record');
    // Two content texels (red, blue), with their opposite edge wrapped on both sides.
    const row=Buffer.from([0,0,255,255,255,0,0,255,0,0,255,255,255,0,0,255]);
    setImage(4003,4,3,Buffer.concat([row,row,row]),undefined,1);
    const r=new Recorder(4,1);
    r.clear('#fff').alpha(.5).clip(0,0,2,1).image(4003,-1.5,-1,4,3);
    r.clip(2,0,2,1).image(4003,.5,-1,4,3).noclip();
    const cv=new Canvas(4,1);play(r.words(),cv);
    for(let x=0;x<4;x++)assert.deepStrictEqual(Array.from(cv.d.slice(x*4,x*4+4)),[191,128,191,255]);
    const clipped=new Recorder(4,1),scaled=new ScaledCanvas(4,1,2);
    clipped.clear('#fff').clip(1,0,1,1).image(4003,0,0,4,1);play(clipped.words(),scaled);
    for(let y=0;y<2;y++)for(let x=0;x<8;x++)if(x<2||x>=4)
        assert.deepStrictEqual(Array.from(scaled.canvas.d.slice((y*8+x)*4,(y*8+x)*4+4)),[255,255,255,255],
            'scaled image clipping retains pixels outside the logical clip');
});

test('native pixel image filters survive caching and recreate textures when changed', () => {
    const g=new RenderClient('filters',{info(){},warn(){}}),writes=[];
    g.native=true;g.ready=true;g.info={pixmaps:true};
    g._write=b=>{writes.push(Buffer.from(b));return true;};
    const {setImage}=require('../runtime/record'),data=Buffer.alloc(16,255);
    const first=setImage(97,2,2,data,[],1),version=first.ver;
    assert.strictEqual(first.filter,1);
    g._ensureImage(97);
    assert.deepStrictEqual(writes.map(b=>b.readUInt32LE(0)),[0x4d520003,0x4d520004]);
    assert.strictEqual(writes[0].readUInt32LE(24),1,'public pixmap requests linear filtering');
    writes.length=0;
    assert.strictEqual(setImage(97,2,2,data,[],1).ver,version);
    g._ensureImage(97);assert.strictEqual(writes.length,0,'unchanged filtered image stays cached');
    const changed=setImage(97,2,2,data,[],0);
    assert(changed.ver>version,'changing only the filter changes the image version');
    g._ensureImage(97);
    assert.deepStrictEqual(writes.map(b=>b.readUInt32LE(0)),[0x4d520003,0x4d520004],
        'a changed filter recreates the pixmap and sends complete pixels');
    assert.strictEqual(writes[0].readUInt32LE(24),0);
    assert.strictEqual(g.sent.image.get(97).filter,0);
    assert.throws(()=>setImage(97,2,2,data,[],2),/filter/i);
    writes.length=0;setImage(98,2,2,data);g._ensureImage(98);
    assert.strictEqual(writes[0].readUInt32LE(24),0,'existing callers retain nearest filtering');
});

test('native image regions preserve coordinates, dense pixels and transport ownership', () => {
    const g=new RenderClient('regions',{info(){},warn(){}}),writes=[];
    g.native=true;g.ready=true;g.info={pixmaps:true};
    g._write=(b)=>{writes.push(b);return true;};
    const {setImage}=require('../runtime/record'),data=Buffer.from(Array.from({length:24},(_,i)=>i+1));
    setImage(91,3,2,data);g._ensureImage(91);writes.length=0;
    const patches=[{x:1,y:0,w:1,h:2,data:Buffer.from([41,42,43,44,45,46,47,48])},
        {x:0,y:1,w:1,h:1,data:Buffer.from([51,52,53,54])}];
    data.set([41,42,43,44],4);data.set([45,46,47,48],16);data.set([51,52,53,54],12);
    setImage(91,3,2,data,patches);g._ensureImage(91);
    assert.strictEqual(writes.length,1,'existing texture needs only one region upload');
    const expected=Buffer.concat([u32(0x4d520004,52,91,2,1,0,1,2,0,1,1,1),
        Buffer.from([41,42,43,44,45,46,47,48,51,52,53,54])]);
    assert.deepStrictEqual(writes[0],expected);
    patches[0].data.fill(0);patches[1].data.fill(0);
    assert.deepStrictEqual(writes[0],expected,'socket transport owns its pixel bytes');
    writes.length=0;
    data.set([61,62,63,64],8);
    setImage(91,3,2,data,[{x:2,y:0,w:1,h:1,data:Buffer.from([61,62,63,64])}]);g._ensureImage(91);
    assert.deepStrictEqual(writes[0],Buffer.concat([u32(0x4d520004,28,91,1,2,0,1,1),Buffer.from([61,62,63,64])]));
});

test('unchanged image regions keep the pending changed image and skip uploads', () => {
    const g=new RenderClient('unchanged',{info(){},warn(){}}),writes=[];
    g.native=true;g.ready=true;g.info={pixmaps:true};g._write=(b)=>{writes.push(Buffer.from(b));return true;};
    const {setImage}=require('../runtime/record'),data=Buffer.from([1,2,3,4,5,6,7,8]);
    setImage(92,2,1,data,[]);g._ensureImage(92);writes.length=0;
    const initial=setImage(92,2,1,data,[]).ver;
    g._ensureImage(92);assert.strictEqual(writes.length,0,'initial unchanged image stays cached');
    data.set([11,12,13,14],4);
    const changed=setImage(92,2,1,data,[{x:1,y:0,w:1,h:1,data:Buffer.from([11,12,13,14])}]).ver;
    assert.notStrictEqual(changed,initial);
    for(let i=0;i<3;i++)assert.strictEqual(setImage(92,2,1,data,[]).ver,changed);
    g._ensureImage(92);
    assert.deepStrictEqual(writes[0],Buffer.concat([u32(0x4d520004,28,92,1,1,0,1,1),Buffer.from([11,12,13,14])]));
    writes.length=0;setImage(92,2,1,data,[]);g._ensureImage(92);
    assert.strictEqual(writes.length,0,'unchanged frames do not upload pixels');
});

test('skipped image updates, reconnects, resizes and replacement buffers upload the full image', () => {
    const g=new RenderClient('fallback',{info(){},warn(){}}),writes=[];
    g.native=true;g.ready=true;g.info={pixmaps:true};g._write=(b)=>{writes.push(Buffer.from(b));return true;};
    const {setImage}=require('../runtime/record'),data=Buffer.from([1,2,3,4,5,6,7,8]);
    setImage(93,2,1,data);g._ensureImage(93);writes.length=0;
    data.set([11,12,13,14],0);setImage(93,2,1,data,[{x:0,y:0,w:1,h:1,data:Buffer.from([11,12,13,14])}]);
    data.set([21,22,23,24],4);setImage(93,2,1,data,[{x:1,y:0,w:1,h:1,data:Buffer.from([21,22,23,24])}]);
    g._ensureImage(93);
    const full=Buffer.concat([u32(0x4d520004,32,93,1,0,0,2,1),Buffer.from([11,12,13,14,21,22,23,24])]);
    assert.deepStrictEqual(writes[0],full,'a missed base version cannot apply only the last patch');
    writes.length=0;g._reset();g._ensureImage(93);
    assert.deepStrictEqual(writes.map(b=>b.readUInt32LE(0)),[0x4d520003,0x4d520004]);
    assert.deepStrictEqual(writes[1],full,'a new connection needs the complete current image');
    writes.length=0;setImage(93,1,2,data,[]);g._ensureImage(93);
    assert.deepStrictEqual(writes[0],u32(0x4d520003,20,93,1,2,1,0));
    assert.deepStrictEqual(writes[1],Buffer.concat([u32(0x4d520004,32,93,1,0,0,1,2),data]));
    const replacement=Buffer.from([31,32,33,34,41,42,43,44]);
    writes.length=0;setImage(93,1,2,replacement,[{x:0,y:1,w:1,h:1,data:replacement.slice(4)}]);g._ensureImage(93);
    assert.deepStrictEqual(writes[0],Buffer.concat([u32(0x4d520004,32,93,1,0,0,1,2),replacement]));
});

test('legacy image transport keeps full images when callers supply regions', () => {
    const g=new RenderClient('legacy',{info(){},warn(){}}),writes=[];
    g.ready=true;g._write=(b)=>{writes.push(Buffer.from(b));return true;};
    const {setImage}=require('../runtime/record'),data=Buffer.from([1,2,3,4,5,6,7,8]);
    setImage(94,2,1,data);g._ensureImage(94);writes.length=0;
    data.set([11,12,13,14],4);setImage(94,2,1,data,[{x:1,y:0,w:1,h:1,data:Buffer.from([11,12,13,14])}]);g._ensureImage(94);
    assert.deepStrictEqual(writes[0],Buffer.concat([u32(9,20,94,2,1),data]));
});

test('queued socket uploads keep their pixels while later frames reuse source buffers', () => {
    const g=new RenderClient('queued',{info(){},warn(){}}),pending=[];
    g.native=true;g.ready=true;g.connected=true;g.info={pixmaps:true};
    g.sock={bufferSize:0,write(b){pending.push([b,Buffer.from(b)]);this.bufferSize+=b.length;return false;}};
    const {setImage}=require('../runtime/record'),data=Buffer.alloc(256*128*4,1);
    setImage(96,256,128,data);g._ensureImage(96);
    for(let value=2;value<=5;value++) {
        data.fill(value);
        setImage(96,256,128,data,[{x:0,y:0,w:256,h:128,data}]);g._ensureImage(96);
        pending.forEach(([borrowed,expected])=>assert.deepStrictEqual(borrowed,expected));
    }
});

test('invalid image regions fall back to complete cached pixels', () => {
    const g=new RenderClient('invalid',{info(){},warn(){}}),writes=[];
    g.native=true;g.ready=true;g.info={pixmaps:true};g._write=(b)=>{writes.push(Buffer.from(b));return true;};
    const {setImage}=require('../runtime/record'),data=Buffer.from([1,2,3,4,5,6,7,8]);
    setImage(95,2,1,data);g._ensureImage(95);
    const patch={x:1,y:0,w:1,h:1,data:Buffer.from([5,6,7,8])};
    for(const patches of [[Object.assign({},patch,{x:2})],[Object.assign({},patch,{x:-1})],
            [Object.assign({},patch,{x:.5})],[Object.assign({},patch,{w:0})],
            [Object.assign({},patch,{data:Buffer.alloc(3)})],new Array(2),new Array(65).fill(patch)]) {
        writes.length=0;setImage(95,2,1,data,patches);g._ensureImage(95);
        assert.deepStrictEqual(writes[0],Buffer.concat([u32(0x4d520004,32,95,1,0,0,2,1),data]));
    }
});

test('explicit controller grab reaches the app with touch enabled; sheets keep their input', async () => {
    const t = await setup(), seen = [];
    try {
        page(t.d, 10);
        t.d.onCcu = (v) => seen.push(v);
        t.d.grab(true);
        t.c.send(X_CCU, ccu(0, 0, 10));
        await sleep(50);
        assert.strictEqual(seen.length, 1, 'grabbed joystick input reaches the app');
        let pressed = 0;
        assert(t.d.openSheet({ draw: () => [], move() {}, press() { pressed++; }, back() {} }));
        t.c.send(X_CCU, ccu(0, 0, 0));
        t.c.send(X_CCU, ccu(0, 1, 0));
        await sleep(50);
        assert.strictEqual(seen.length, 1, 'the sheet owns the controller');
        assert.strictEqual(pressed, 1);
    } finally { t.end(); }
});

test('unchanged frames: not sent again, but one a second (the renderer hides an app quiet for 3 s)', async () => {
    const t = await setup(), c = t.c;
    try {
        const t0 = Date.now();
        while (Date.now() - t0 < 2500) { page(t.d, 10); await sleep(16); }
        const f = c.frames.slice(), gaps = f.slice(1).map((x, i) => x - f[i]).concat(Date.now() - f[f.length - 1]);
        console.log('  a static page at 60 fps for 2.5 s: ' + f.length + ' FRAMEs, longest gap ' + Math.max(...gaps) + ' ms');
        assert(f.length >= 2 && f.length <= 6, 'FRAMEs for a static page: ' + f.length);
        assert(Math.max(...gaps) < 1600, 'the longest time without a FRAME: ' + Math.max(...gaps) + ' ms');
        // a changed page: at once
        let n = c.frames.length;
        page(t.d, 20); await sleep(50);
        assert.strictEqual(c.frames.length, n + 1, 'a changed page');
        // the header changed (the knob turned onto a tab), the page did not: at once, and only once
        n = c.frames.length;
        c.send(X_CCU, ccu(0, 0, 0)); c.send(X_CCU, ccu(1, 0, 0)); await sleep(50);
        assert.strictEqual(t.d.focusTab, 0);
        assert.strictEqual(c.frames.length, n + 1, 'the header changed');
        page(t.d, 20); await sleep(50);
        assert.strictEqual(c.frames.length, n + 1, 'the same page with the same header');
        // shown again: the picture comes back once the HMI's page has opened (display.js SHOW_DELAY_MS, 350 ms),
        // then the same page goes out at once
        t.ui.setVisible(false); t.ui.setVisible(true);
        await sleep(450);
        n = c.frames.length;
        page(t.d, 20); await sleep(50);
        assert.strictEqual(c.frames.length, n + 1, 'shown again');
        // a sheet painted every 33 ms that does not change: its first frame, then the keepalive
        n = c.frames.length;
        assert(t.d.openSheet({ draw: (r) => { r.fillRect(0, 0, 50, 50, '#0000ff'); return []; }, move() {} }));
        await sleep(1500);
        console.log('  a static sheet for 1.5 s: ' + (c.frames.length - n) + ' FRAMEs');
        assert(c.frames.length - n >= 2 && c.frames.length - n <= 4, 'FRAMEs for a static sheet: ' + (c.frames.length - n));
        // closed: the page again, at once
        n = c.frames.length;
        t.d.closeSheet(); await sleep(50);
        assert.strictEqual(c.frames.length, n + 1, 'the sheet closed');
    } finally { t.end(); }
});

test('a sheet\'s press, back and touch target that throw: logged, the app runs on', async () => {
    const t = await setup(), c = t.c;
    try {
        page(t.d, 10);
        const boom = (w) => () => { throw new Error(w); };
        assert(t.d.openSheet({ draw: () => [{ x: 0, y: 0, w: 1600, h: 520, act: boom('act') }], move() {}, press: boom('press'), back: boom('back') }));
        const n = uncaught.length;
        c.send(X_CCU, ccu(0, 0, 0)); c.send(X_CCU, ccu(0, 1, 0));                      // press
        c.send(X_CCU, ccu(0, 0, 0)); c.send(X_CCU, ccu(0, 0, 10));                     // push left
        c.send(TOUCH, touch(1, 800, 300)); c.send(TOUCH, touch(3, 800, 300));          // a tap on the target
        await sleep(100);
        // (an uncaught exception ends the app on the car: mgu.js process.exit(1), and the platform wipes its data)
        assert.deepStrictEqual(uncaught.slice(n).map((e) => e.message), [], 'uncaught in the socket\'s data handler');
        assert.deepStrictEqual(t.log.errors.map((a) => a[0] + ' ' + a[1].message), ['sheet press', 'sheet back', 'sheet act']);
        assert(t.d.sheet, 'the sheet stays open');
    } finally { t.end(); }
});

test('not in front, then in front (not on screen yet) within 1.5 s: the app stays on the renderer', async () => {
    const t = await setup(), c = t.c;
    try {
        page(t.d, 10);
        c.send(X_STATE, u32(1, 0, 0)); await sleep(100);
        c.send(X_STATE, u32(1, 0, 1)); await sleep(1700);
        assert(!t.d.blocked, 'blocked though in front');
        assert(t.d.own());
        // not in front for good: blocked after 1.5 s
        c.send(X_STATE, u32(1, 0, 0)); await sleep(1700);
        assert.strictEqual(t.d.blocked, true, 'not in front for 1.5 s');
    } finally { t.end(); }
});

test('a connection cut in the middle of a message: the next connection\'s READY is read', async () => {
    let n = 0;
    onConn = (c) => {
        if (n++) { ready(c); return; }
        c.s.write(Buffer.concat([u32(TICK, 8), u32(1)]));                              // 4 of TICK's 8 bytes
        setTimeout(() => c.s.destroy(), 50);
    };
    const g = new RenderClient('test', { info() {}, warn() {}, error() {} }, { native: true });
    try {
        let readyN = 0;
        g.on('ready', () => readyN++);
        g.connect();
        await waitFor(() => readyN, 5000);
        assert.strictEqual(n, 2, 'connected again');
        assert.strictEqual(readyN, 1, 'READY after the reconnect');
        assert(g.native && g.info && g.info.touch, 'X_READY after the reconnect');
    } finally { g.close(); }
});

test('the one-time move to the new default theme: a theme picked on a fresh install stays', async () => {
    const src = fs.readFileSync(path.join(__dirname, '..', 'runtime', 'mgu.js'), 'utf8').replace(/\r\n/g, '\n');
    const cls = src.match(/class Settings[\s\S]*?\n}\n/)[0];      // (as hu-stub.js: the real class without the SDK)
    const make = (sub) => {
        const dd = path.join(dir, sub);
        fs.mkdirSync(dd);
        return new Function('fs', 'path', 'DATA_DIR', 'log', cls + '\nreturn Settings;')(fs, path, dd, { warn() {}, error() {} });
    };
    const file = (sub) => path.join(dir, sub, 'settings.json');
    const schema = [{ key: 'theme', type: 'choice', default: 'm_tricolour', options: [] }, { key: 'units', type: 'choice', default: 'metric', options: [] }];
    // fresh install: no file until something is set; a theme set then stays
    let S = make('fresh');
    assert.strictEqual(new S(schema).get('theme'), 'm_tricolour');
    assert(!fs.existsSync(file('fresh')), 'a fresh start writes no file');
    new S(schema).set('theme', 'amber');
    assert.strictEqual(new S(schema).get('theme'), 'amber', 'theme picked on a fresh install, next start');
    // another setting set first, the theme later: stays too
    S = make('fresh2');
    new S(schema).set('units', 'imperial');
    new S(schema).set('theme', 'amber');
    assert.strictEqual(new S(schema).get('theme'), 'amber', 'theme picked after another setting, next start');
    // saved before the new default (no mark): moved to it once, the next choice stays
    S = make('old');
    fs.writeFileSync(file('old'), JSON.stringify({ theme: 'amber', units: 'imperial' }));
    const o = new S(schema);
    assert.strictEqual(o.get('theme'), 'm_tricolour', 'an old save moves to the new default');
    assert.strictEqual(o.get('units'), 'imperial');
    assert.strictEqual(JSON.parse(fs.readFileSync(file('old'), 'utf8'))._themeV, 2);
    o.set('theme', 'amber');
    assert.strictEqual(new S(schema).get('theme'), 'amber');
});

test('settings: aliases, "when" and "group" (the drawn settings sheet), "hidden"', async () => {
    const src = fs.readFileSync(path.join(__dirname, '..', 'runtime', 'mgu.js'), 'utf8').replace(/\r\n/g, '\n');
    const cls = src.match(/class Settings[\s\S]*?\n}\n/)[0];
    const dd = path.join(dir, 'when');
    fs.mkdirSync(dd);
    const S = new Function('fs', 'path', 'DATA_DIR', 'log', cls + '\nreturn Settings;')(fs, path, dd, { warn() {}, error() {} });
    fs.writeFileSync(path.join(dd, 'settings.json'), JSON.stringify({ tempUnit: 'F' }));
    const schema = [
        { key: 'tempUnit', label: 'Temperature unit', type: 'choice', default: 'c', options: [['c', 'Celsius'], ['f', 'Fahrenheit']], aliases: { C: 'c', F: 'f' } },
        { key: 'torqueUnit', label: 'Torque unit', type: 'choice', default: 'nm', options: [['nm', 'Nm'], ['kgm', 'kgm']] },
        { key: 'maxKgm', label: 'Torque gauge max', type: 'number', default: 100, steps: [50, 100], when: { torqueUnit: 'kgm' } },
        { key: 'showA', label: 'Tile A', type: 'choice', default: 'auto', options: [['auto', 'Auto'], ['off', 'Hide']], group: 'Tiles' },
        { key: 'showB', label: 'Tile B', type: 'choice', default: 'auto', options: [['auto', 'Auto'], ['off', 'Hide']], group: 'Tiles' },
        { key: 'names', label: 'Names', type: 'choice', default: 'en', options: [['en', 'English']], hidden: true },
        { key: 'advanced', label: 'Advanced settings', type: 'bool', default: false },
        { key: 'rate', label: 'Display rate', type: 'number', default: 10, steps: [5, 10], when: { advanced: true } },
    ];
    const st = new S(schema);
    assert.strictEqual(st.get('tempUnit'), 'f', 'a value saved as F reads as f');
    const { listSheet } = require(path.join(__dirname, '..', 'runtime', 'sheet.js'));
    const sh = listSheet(st, null, {});
    const labels = () => sh.rows().map((r) => r.label);
    assert.deepStrictEqual(labels(), ['Temperature unit', 'Torque unit', 'Tiles', 'Advanced settings'], 'kgm-only, grouped, hidden and advanced left out');
    st.set('torqueUnit', 'kgm');
    st.set('advanced', true);
    assert.deepStrictEqual(labels(), ['Temperature unit', 'Torque unit', 'Torque gauge max', 'Tiles', 'Advanced settings', 'Display rate'], 'offered once their "when" holds');
    // the group: one row that opens its settings; Back returns to it
    sh.focus = 3; sh.choose();
    assert.deepStrictEqual([sh.group, labels()], ['Tiles', ['Tile A', 'Tile B']]);
    sh.focus = 1; sh.choose();
    assert.strictEqual(sh.open.key, 'showB', 'a grouped setting opens its values');
    sh.sub = 1; sh.choose();
    assert.strictEqual(st.get('showB'), 'off');
    assert.deepStrictEqual([sh.back(), sh.group, sh.focus], [null, null, 3], 'Back: the main list, on the group');
    assert.strictEqual(sh.back(), 'close');
    // a switch that hides the setting the focus was on: the focus stays on the list
    sh.focus = 5; st.set('advanced', false);
    const r = new Proxy({}, { get: (o, k) => (k === 'extra' ? 0 : () => 40) });
    sh.draw(r, { bg: '#000', text: '#fff', dim: '#888', sel: '#00f' });
    assert.strictEqual(sh.focus, labels().length - 1);
});

test('a new scale keeps the uploads (the renderer keeps them), a new size sends them again', async () => {
    const t = await setup(), g = t.d.gpu;
    try {
        const pl = Object.assign({}, g.placement);
        g.sent.atlas.add(1); g.sent.ramp.add(2); g.sent.image.set(3, 1); g.sent.layer.add(4);
        g.place(Object.assign({}, pl, { scale: pl.scale * 0.9 }));
        assert.deepStrictEqual([g.sent.atlas.has(1), g.sent.ramp.has(2), g.sent.image.has(3)], [true, true, true], 'a new scale sends everything again');
        assert(!g.sent.layer.has(4), 'layers drawn at the old scale');
        g.place(Object.assign({}, pl, { w: pl.w - 8 }));
        assert.deepStrictEqual([g.sent.atlas.size, g.sent.ramp.size, g.sent.image.size], [0, 0, 0], 'a new size');
    } finally { t.end(); }
});

(async () => {
    await new Promise((r) => server.listen(process.env.MGU_RENDERD_SOCKET, r));
    let failed = 0;
    for (const [name, fn] of cases) {
        try { await fn(); console.log('ok    ' + name); } catch (e) { failed++; console.log('FAIL  ' + name + '\n      ' + e.message.split('\n')[0]); }
    }
    server.close();
    fs.rmSync(dir, { recursive: true, force: true });
    console.log(failed ? failed + ' of ' + cases.length + ' failed' : 'runtime: all ok');
    process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });

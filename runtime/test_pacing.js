'use strict';
const assert = require('assert'), fs = require('fs'), path = require('path'), vm = require('vm');
const { Controls } = require('../apps/kxrxh_doodle_jump/controls');
let now = 1000, sequence = 0, draw, focus, input, engine, sheet, moving = false, visible = true, focused = true;
let replyDelay = null;
const timers = new Map(), events = {}, clicks = {}, sent = [], requests = [];
function later(fn, delay) { const id = ++sequence; timers.set(id, { fn, at:now + Math.max(1, delay) }); return id; }
function advance(ms, lateness = 0) {
    const end = now + ms;
    for (;;) {
        let id = null;
        timers.forEach((timer, key) => { if (timer.at <= end && (id === null || timer.at < timers.get(id).at)) id = key; });
        if (id === null) break;
        const timer = timers.get(id); timers.delete(id); now = Math.max(now, timer.at + lateness); timer.fn();
    }
    now = Math.max(now, end);
}
class Engine {
    constructor() { engine = this; this.ready = true; this.busy = false; this.dead = false; this.playing = false; this.scenery = { sprites:{} }; }
    start() {}
    send(command) { sent.push(command); }
    frame(command) {
        assert(!this.busy, 'only one frame may be in flight'); this.busy = true; requests.push({ at:now, command });
        if (replyDelay !== null) this.reply = later(() => { this.reply = null; this.complete(); }, replyDelay);
        return true;
    }
    complete() { this.busy = false; this.onFrame(Buffer.alloc(4), 1, 1); }
    stop() { this.dead = true; if (this.reply) timers.delete(this.reply); }
}
const canvas = { clear() { return this; }, image() {}, text() {} };
const ctx = {
    dataDir:'', log:{ info() {}, warn() {} }, settings:{ get(key) { return key === 'parkedOnly'; } },
    ui:{ isVisible() { return visible; }, hasFocus() { return focused; }, text() {}, image() {},
        open(name) { visible = name === 'GAME'; }, onClick(id, fn) { clicks[id] = fn; }, onFocus(fn) { focus = fn; } },
    controller:{ grab() {}, onInput(fn) { input = fn; } }, signals:{ watch() {} },
    hideButton() {}, canvas() { return canvas; }, loop(name, fps, fn) { draw = fn; }, sheet(value) { sheet = value; return true; },
};
const dependencies = {
    './kit/mgu':{ run(spec) { spec.start(ctx); } }, './kit/record':{ setImage() {} },
    './kit/parked':{ watch() { return { moving() { return moving; } }; } },
    './engine':{ Engine }, './kit/sheet':{ cardSheet(options) { return options; } },
    './world':{ World:class { draw() {} } }, './controls':{ Controls },
};
function startApp() { vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../apps/kxrxh_doodle_jump/main.js'), 'utf8'), {
    require(name) { assert(dependencies[name], name); return dependencies[name]; },
    Date:{ now() { return now; } }, setTimeout:later, clearTimeout(id) { timers.delete(id); },
    setInterval() { return ++sequence; }, clearInterval() {},
    process:{ on(name, fn) { events[name] = fn; }, exit() {} },
}); }
startApp();
draw(); advance(17);
assert.strictEqual(requests.length, 1);
const first = requests[0].at;
draw();
advance(20); engine.complete(); advance(1);
assert.strictEqual(requests.length, 2, 'a completed slow frame requests its successor without a compositor tick');
assert(requests[1].command.dt >= .02 && requests[1].command.dt < .025, 'dt follows engine submissions');
input({ frontRotaryEncoderPushButton:1 });
advance(2); engine.complete(); advance(5);
assert.strictEqual(requests.length, 2, 'fast replies wait for the 60 Hz deadline');
advance(12);
assert.strictEqual(requests.length, 3);
assert.strictEqual(requests[2].command.touches[0][1], 0, 'queued controls reach the next submitted frame');
assert(Math.abs(requests[2].at - (1000+3*1000/60)) < 1e-9, 'late submissions retain the original deadline');
assert(first >= 1000);

function block(set, release, label) {
    const count = requests.length;
    engine.complete(); set(); focus(); advance(50);
    assert.strictEqual(requests.length, count, label);
    draw(); advance(50); assert.strictEqual(requests.length, count, label);
    release(); visible = true; focus(); draw(); advance(17);
    assert.strictEqual(requests.length, count + 1, label + ' resumes');
    assert(requests[requests.length-1].command.dt <= .018, 'resuming resets dt');
}
block(() => { visible = false; }, () => {}, 'hidden screens pause');
block(() => { focused = false; }, () => { focused = true; }, 'lost focus pauses');
block(() => { moving = true; }, () => { moving = false; }, 'driving pauses');
engine.complete();
engine.onDialog({ id:1, title:'Confirm', message:'Continue?', buttons:[{ key:'negative', label:'Cancel' }] });
const beforeDialog = requests.length; advance(50); draw(); advance(50);
assert.strictEqual(requests.length, beforeDialog, 'dialogs pause scheduled engine frames');
assert(sent.some(command => command.action === 'pause'));
sheet.onClose(); advance(17);
assert.strictEqual(requests.length, beforeDialog + 1, 'answering the dialog resumes the engine');
engine.complete(); assert.strictEqual(timers.size, 1);
events.exit(); assert(engine.dead); assert.strictEqual(timers.size, 0, 'exit clears a pending frame timer');
advance(50); assert.strictEqual(requests.length, beforeDialog + 1, 'shutdown cannot submit another frame');
console.log('Engine pacing, controls, pause gates and cleanup: passed');
replyDelay = 6;
const cadenceStart = requests.length;
startApp(); draw(); advance(2250, 2);
const cadence = requests.slice(cadenceStart, cadenceStart + 120);
assert.strictEqual(cadence.length, 120);
const fps = 119000 / (cadence[119].at - cadence[0].at);
assert(fps >= 59 && fps <= 61, '2 ms event-loop delays must not accumulate: ' + fps.toFixed(2) + ' FPS');
cadence.slice(1).forEach((frame, index) => {
    assert(Math.abs(frame.command.dt - (frame.at-cadence[index].at)/1000) < 1e-9, 'physics dt uses actual submission times');
});
events.exit(); assert.strictEqual(timers.size, 0);
console.log('Absolute 60 Hz engine cadence with late callbacks: passed');

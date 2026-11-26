'use strict';
const assert = require('assert');
const { Controls } = require('../../apps/kxrxh_doodle_jump/controls');
let playing = false;
const c = new Controls(() => playing);
c.place = { w:1920,h:720,scale:1.164,ox:71,oy:-38 };
c.touch('down', (100-71)/1.164, (300+38)/1.164);
playing = true;
c.touch('down', (100-71)/1.164, (300+38)/1.164);
assert.strictEqual(c.take().tilt,-1);
c.touch('up', (100-71)/1.164, (300+38)/1.164);
assert.strictEqual(c.take().tilt,0);
c.raw({frontRotaryEncoderDeflection:4,frontRotaryEncoderPushButton:0});
assert.strictEqual(c.take().tilt,1);
c.raw({frontRotaryEncoderDeflection:0,frontRotaryEncoderPushButton:1});
assert.strictEqual(c.take().touches[0][1],0);
assert.strictEqual(c.take().touches[0][1],2,'release is a separate native frame');
c.reset(); assert.deepStrictEqual(c.take(),{tilt:0,touches:[]});
playing=false;c.touch(1,(892-71)/1.164,(221+38)/1.164);
assert.strictEqual(c.take().touches[0][1],0,'kit numeric touch phase reaches the native engine');
c.touch(2,(1000-71)/1.164,(230+38)/1.164);
assert.strictEqual(c.take().touches[0][1],1,'dragging reaches original theme selection');
c.touch(3,(1000-71)/1.164,(230+38)/1.164);
assert.strictEqual(c.take().touches[0][1],2);
console.log('Touch and iDrive controls: passed');
const pointer=new Controls(()=>false);
pointer.place={w:1920,h:720,scale:1,ox:0,oy:0};
pointer.touch(1,906,150);
assert.deepStrictEqual(pointer.cursor,{x:200,y:200});
pointer.take();
pointer.touch(2,981,300);
assert.deepStrictEqual(pointer.cursor,{x:300,y:400},'cursor follows a held touch');
assert.deepStrictEqual(pointer.take().touches,[[0,1,300,400]],'drag still reaches the original engine');
pointer.touch(3,1011,330);
assert.deepStrictEqual(pointer.cursor,{x:340,y:440},'cursor stays at the release position');
pointer.take();pointer.select();
assert.deepStrictEqual(pointer.take().touches,[[0,0,340,440]],'joystick select uses the latest touch position');
console.log('Cursor follows touch press, drag and release: passed');
c.reset();const before=Object.assign({},c.cursor);
c.observe({frontRotaryEncoderOperation:0});c.raw({frontRotaryEncoderOperation:2});
assert.strictEqual(c.cursor.x,Math.min(543,before.x+32),'ring moves the menu pointer horizontally');
const realNow=Date.now;let now=1000;Date.now=()=>now;
try {
    playing=true;
    function turn(position, extra) {
        c.raw(Object.assign({frontRotaryEncoderOperation:position},extra));
    }
    function startAt(position) {
        c.reset();
        c.observe({frontRotaryEncoderOperation:position});
    }
    // The original game applies one accelerometer sample per engine update.
    function travel(fps) {
        let total=0;
        for (let frame=0;frame<fps;frame++) {
            const tilt=c.take().tilt;
            assert(tilt>=0 && tilt<=1);
            total+=tilt;
            now+=1000/fps;
        }
        assert.strictEqual(c.take().tilt,0);
        return total;
    }
    startAt(65535);turn(0);
    const fine=c.steering();
    assert(fine>0 && fine<0.5,'one clockwise detent gently nudges right across wrap');
    turn(0,{frontRotaryEncoderPushButton:1});
    const shot=c.take();
    assert.strictEqual(shot.tilt,fine,'shooting and reading the HUD do not consume ring movement');
    assert.strictEqual(shot.touches[0][1],0);
    assert.strictEqual(c.take().touches[0][1],2);

    const rates=[30,50,60];
    const single=rates.map(fps=>{startAt(0);turn(1);return travel(fps);});
    assert(single[0]>0.5 && single[0]<=1,'one click has a small, finite travel budget');
    assert(single.every(distance=>distance===single[0]),'one detent travels equally at 30, 50 and 60 FPS');
    startAt(0);turn(4);
    assert(c.steering()>fine,'a quick turn steers harder');
    const batch=travel(60);
    assert(Math.abs(batch-4*single[0])<1e-9,'batched detents retain their movement');
    startAt(0);
    let separate=0;
    for (let pos=1;pos<=4;pos++) {
        turn(pos);separate+=c.take().tilt;now+=20;
    }
    assert(Math.abs(separate+travel(50)-batch)<1e-9,'separate ring reports retain the same movement');
    startAt(0);turn(8);c.take();turn(7);
    assert.strictEqual(c.steering(),-fine,'one reverse click replaces queued clockwise movement');
    startAt(0);turn(65535);
    assert.strictEqual(c.take().tilt,-fine,'counterclockwise wrap nudges left');
    startAt(0);turn(100);
    assert.strictEqual(c.take().tilt,0,'ignore a discontinuous encoder counter');
    startAt(0);turn(64);
    assert(travel(60)<10,'a large encoder burst does not queue a long spin');
    startAt(0);turn(4);now+=500;
    assert.strictEqual(c.take().tilt,0,'a stalled game drops old input');
    startAt(0);turn(4);c.reset();
    assert.strictEqual(c.take().tilt,0,'pause releases queued steering');
    startAt(0);turn(4);playing=false;
    assert.strictEqual(c.take().tilt,0,'gameplay ring input ends at the menu');
    playing=true;
    assert.strictEqual(c.take().tilt,0);
    startAt(0);turn(4,{frontRotaryEncoderDeflection:10});
    assert.strictEqual(c.take().tilt,-1,'joystick steering takes priority');
    turn(4,{frontRotaryEncoderDeflection:0});
    assert.strictEqual(c.take().tilt,0,'releasing the joystick has no deferred ring input');
    startAt(0);turn(4);c.touch('down',20,300);turn(5);
    assert.strictEqual(c.take().tilt,-1,'held touch steering takes priority');
    c.touch('up',20,300);
    assert.strictEqual(c.take().tilt,0,'releasing touch has no deferred ring input');
} finally { Date.now=realNow; }
console.log('iDrive ring precision, frame rates, overrides and release: passed');

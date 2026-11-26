'use strict';
const DEFL = { MITTELSTELLUNG:0, NORD_KURZ_GEDRUECKT:1, NORD_LANG_GEDRUECKT:2,
    OST_KURZ_GEDRUECKT:4, OST_LANG_GEDRUECKT:5, SUED_KURZ_GEDRUECKT:7, SUED_LANG_GEDRUECKT:8,
    WEST_KURZ_GEDRUECKT:10, WEST_LANG_GEDRUECKT:11 };
const PUSH = { KEINE_BETAETIGUNG:0, DREHGEBER_KURZ_GEDRUECKT:1, DREHGEBER_LANG_GEDRUECKT:2 };
const value = (v,names) => typeof v === 'number' ? v : names[v];

// One detent supplies three quarter-strength accelerometer samples to the original engine.
const RING_PER_DETENT = 0.75;
const RING_LIMIT = 8 * RING_PER_DETENT;
const RING_IDLE_MS = 250;

class Controls {
    constructor(playing) { this.playing=playing; this.cursor={x:180,y:295}; this.place=null; this.position=null; this.reset(); }
    reset() {
        this.tilt=0; this.direction=0; this.horizontal=0; this.pressed=false; this.touches=[]; this.finger=null; this.until=0;
        this.ringRemaining=0; this.ringStep=0; this.ringUntil=0;
    }
    observe(v) { const n=v.frontRotaryEncoderOperation; if(Number.isInteger(n)) this.position=n&0xffff; }
    tap(x,y) { if (this.touches.length <= 14) this.touches.push([0,0,x,y],[0,2,x,y]); }
    point(phase,x,y) {
        const last=this.touches[this.touches.length-1];
        if (last && phase===1 && last[1]===1) this.touches[this.touches.length-1]=[0,phase,x,y];
        else if (this.touches.length<16) this.touches.push([0,phase,x,y]);
        else if (phase===2) { this.touches.pop(); this.touches.push([0,phase,x,y]); }
    }
    select() { this.playing() ? this.tap(272,240) : this.tap(this.cursor.x,this.cursor.y); }
    steer(direction) { this.ringRemaining=0; this.tilt=direction; this.until=Date.now()+220; }
    turn(delta) {
        const now=Date.now();
        if (now>=this.ringUntil || Math.sign(delta)!==Math.sign(this.ringRemaining)) this.ringRemaining=0;
        this.ringRemaining=Math.max(-RING_LIMIT,Math.min(RING_LIMIT,this.ringRemaining+delta*RING_PER_DETENT));
        this.ringStep=Math.min(1,Math.abs(this.ringRemaining)/3);
        this.ringUntil=now+RING_IDLE_MS;
    }
    raw(v) {
        const d=value(v.frontRotaryEncoderDeflection,DEFL), p=value(v.frontRotaryEncoderPushButton,PUSH);
        this.direction=d===1||d===2 ? -1 : d===7||d===8 ? 1 : 0;
        this.horizontal=d===4||d===5 ? 1 : d===10||d===11 ? -1 : 0;
        if(this.finger!=='steer'){this.tilt=this.horizontal;this.until=0;}
        if (this.tilt) this.ringRemaining=0;
        const previous=this.position;
        this.observe(v);
        const delta=previous===null?0:((this.position-previous+32768)&0xffff)-32768;
        if(delta && Math.abs(delta)<=64){
            if (!this.playing()) this.cursor.x=Math.max(0,Math.min(543,this.cursor.x+delta*16));
            else if (!this.tilt) this.turn(delta);
        }
        const down=p===1||p===2;
        if (down && !this.pressed) this.select();
        this.pressed=down;
    }
    touch(phase,x,y) {
        if (typeof phase==='number') phase=[null,'down','move','up'][phase];
        const p=this.place;
        if (!p) return;
        const sx=x*p.scale+p.ox, sy=y*p.scale+p.oy, gw=p.h*544/960, gx=(p.w-gw)/2;
        if (this.playing() && (sx<gx || sx>gx+gw)) {
            if (phase==='up') { this.tilt=0; this.finger=null; }
            else { this.ringRemaining=0; this.finger='steer'; this.tilt=sx<gx ? -1 : 1; this.until=0; }
            return;
        }
        if (this.finger==='steer') { if (phase==='up') { this.tilt=0; this.finger=null; } return; }
        const px=Math.max(0,Math.min(544,(sx-gx)*544/gw)), py=Math.max(0,Math.min(960,sy*960/p.h));
        if (phase==='down' && sx>=gx && sx<=gx+gw) {
            this.finger='game'; this.point(0,px,py);
        }
        if (this.finger==='game') this.cursor={x:px,y:py};
        if (phase==='move' && this.finger==='game') this.point(1,px,py);
        if (phase==='up' && this.finger==='game') this.point(2,px,py);
        if (phase==='up') this.finger=null;
    }
    move(dt) {
        if (!this.playing()) {
            this.cursor.x=Math.max(0,Math.min(543,this.cursor.x+this.horizontal*240*dt));
            this.cursor.y=Math.max(0,Math.min(959,this.cursor.y+this.direction*240*dt));
        }
    }
    steering() {
        if (this.until && Date.now()>=this.until) { this.tilt=0; this.until=0; }
        if (this.ringRemaining && (!this.playing() || Date.now()>=this.ringUntil)) this.ringRemaining=0;
        return this.tilt || Math.sign(this.ringRemaining)*Math.min(Math.abs(this.ringRemaining),this.ringStep);
    }
    take() {
        const tilt=this.steering();
        if (this.ringRemaining) {
            this.ringRemaining-=tilt;
            this.ringUntil=Date.now()+RING_IDLE_MS;
        }
        // The original engine consumes one phase per finger in each frame.
        return {tilt,touches:this.touches.splice(0,1)};
    }
}
module.exports={Controls};

'use strict';
const {core,players}=require('./ui');
const DEFL={MITTELSTELLUNG:0,NORD_KURZ_GEDRUECKT:1,NORD_LANG_GEDRUECKT:2,OST_KURZ_GEDRUECKT:4,
    OST_LANG_GEDRUECKT:5,SUED_KURZ_GEDRUECKT:7,SUED_LANG_GEDRUECKT:8,WEST_KURZ_GEDRUECKT:10,WEST_LANG_GEDRUECKT:11};
const PUSH={KEINE_BETAETIGUNG:0,DREHGEBER_KURZ_GEDRUECKT:1,DREHGEBER_LANG_GEDRUECKT:2};
const sdkValue=(v,names)=>typeof v==='number'?v:names[v];
const numeric=v=>typeof v==='number'?v:NaN;
class Controls {
    constructor(playing){
        this._handle=core.controls_create();if(!this._handle)throw new Error('Too many UI controls');
        this.playing=playing;players[this._handle]=()=>this.playing();this.place=null;
        this._state=new Float64Array(core.memory.buffer,core.controls_state(this._handle),17);
        this._touches=new Float64Array(core.memory.buffer,core.controls_state(this._handle)+17*8,16*3);
        this.cursor={};
        ['x','y'].forEach((key,index)=>Object.defineProperty(this.cursor,key,{enumerable:true,
            get:()=>this._state[index],set:v=>{this._state[index]=v;}}));
    }
    reset(){core.controls_reset(this._handle);}
    observe(v){core.controls_observe(this._handle,numeric(v.frontRotaryEncoderOperation));}
    tap(x,y){core.controls_tap(this._handle,x,y);}
    point(phase,x,y){core.controls_point(this._handle,phase,x,y);}
    select(){core.controls_select(this._handle);}
    steer(direction){core.controls_steer(this._handle,direction);}
    turn(delta){core.controls_turn(this._handle,delta);}
    raw(v){core.controls_raw(this._handle,sdkValue(v.frontRotaryEncoderDeflection,DEFL),
        sdkValue(v.frontRotaryEncoderPushButton,PUSH),numeric(v.frontRotaryEncoderOperation));}
    touch(phase,x,y){const p=this.place;if(p)core.controls_touch(this._handle,
        typeof phase==='number'?phase:({down:1,move:2,up:3}[phase]||0),x,y,p.w,p.h,p.scale,p.ox,p.oy);}
    move(dt){core.controls_move(this._handle,dt);}
    steering(){return core.controls_steering(this._handle);}
    take(){const tilt=core.controls_take(this._handle),s=this._state;
        return {tilt,touches:s[13]?[[0,s[14],s[15],s[16]]]:[]};}
    get touches(){const out=[];for(let i=0;i<this._state[12];i++)out.push([0,this._touches[i*3],this._touches[i*3+1],this._touches[i*3+2]]);return out;}
    get finger(){return [null,'game','steer'][this._state[6]];}
    get pressed(){return !!this._state[5];}
    get position(){return Number.isNaN(this._state[11])?null:this._state[11];}
}
Object.entries({tilt:2,direction:3,horizontal:4,until:7,ringRemaining:8,ringStep:9,ringUntil:10}).forEach(([key,index])=>
    Object.defineProperty(Controls.prototype,key,{get(){return this._state[index];},set(v){this._state[index]=v;}}));
module.exports={Controls};

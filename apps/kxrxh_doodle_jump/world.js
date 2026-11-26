'use strict';
const {core,withCanvas}=require('./ui');
const PAPER=['width','height','u','v','du','dv','alpha'];
const SPRITES=['ufo','platform','broken','monster','bat','springs'];
class World {
    constructor(){
        this._handle=core.world_create();if(!this._handle)throw new Error('Too many UI worlds');
        this._state=new Float64Array(core.memory.buffer,core.world_state(this._handle),34);
    }
    get phase(){return this._state[0];}
    set phase(v){this._state[0]=v;}
    get lean(){return this._state[1];}
    set lean(v){this._state[1]=v;}
    draw(cv,w,h,scene,tilt,playing,dt){
        const state=this._state,paper=scene.paper;state[2]=paper?1:0;
        PAPER.forEach((key,i)=>{state[3+i]=paper&&typeof paper[key]==='number'?paper[key]:NaN;});
        SPRITES.forEach((name,i)=>{const sprite=scene.sprites[name],at=10+i*4;state[at]=sprite?1:0;
            if(sprite){state[at+1]=sprite.id;state[at+2]=sprite.width;state[at+3]=sprite.height;}});
        withCanvas(cv,()=>core.world_draw(this._handle,w,h,tilt,playing?1:0,dt));
    }
}
module.exports={World};

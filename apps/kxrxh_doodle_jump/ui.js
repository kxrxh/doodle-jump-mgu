'use strict';
const fs=require('fs'),path=require('path');
const moduleCore=new WebAssembly.Module(fs.readFileSync(path.join(__dirname,'ui.wasm')));
const players=[];let canvas=null;
const target=()=>{if(!canvas)throw new Error('UI drawing has no canvas');return canvas;};
const core=new WebAssembly.Instance(moduleCore,{host:{
    now:()=>Date.now(),playing:id=>players[id]?Number(!!players[id]()):0,
    floor:Math.floor,ceil:Math.ceil,round:Math.round,exp:Math.exp,
    alpha:v=>target().alpha(v),clip:(x,y,w,h)=>target().clip(x,y,w,h),
    image:(id,x,y,w,h)=>target().image(id,x,y,w,h),
    line:(x1,y1,x2,y2,w,c)=>target().line(x1,y1,x2,y2,w,[c>>>24,(c>>>16)&255,(c>>>8)&255,c&255]),
    noclip:()=>target().noclip()
}}).exports;
function withCanvas(cv,draw){const previous=canvas;canvas=cv;try{return draw();}finally{canvas=previous;}}
module.exports={core,players,withCanvas};

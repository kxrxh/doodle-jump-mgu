'use strict';

const LEFT=[['ufo',.24,.20,128,.23],['platform',.71,.37,70,.22],['platform',.30,.59,86,.18],
    ['broken',.67,.81,76,.18],['monster',.16,.85,96,.13]];
const RIGHT=[['bat',.70,.25,140,.20],['platform',.26,.45,76,.20],['springs',.28,.40,26,.20],
    ['platform',.65,.71,90,.18],['broken',.22,.86,70,.17]];

class World {
    constructor(){this.phase=0;this.lean=0;}
    draw(cv,w,h,scene,tilt,playing,dt){
        const scale=h/960,gw=544*scale,gx=(w-gw)/2,sw=Math.max(0,gx);
        this.phase+=dt*(playing?8:1.5);
        this.lean+=(tilt*18-this.lean)*(1-Math.exp(-dt*6));
        if(!sw)return;
        const margins=[[0,LEFT,-1],[gx+gw,RIGHT,1]],paper=scene.paper;
        const clips=margins.map(([x])=>[Math.round(x),0,Math.round(x+sw)-Math.round(x),Math.round(h)]);
        let tiles=null;
        if(paper && ['width','height'].every(k=>Number.isInteger(paper[k])&&paper[k]>0&&paper[k]<=2046) &&
            ['du','dv'].every(k=>Number.isFinite(paper[k])&&paper[k]>0) &&
            ['u','v','alpha'].every(k=>Number.isFinite(paper[k])&&paper[k]>=0) &&
            paper.u<1 && paper.v<1 && paper.alpha<=1){
            const pw=scale/paper.du,ph=scale/paper.dv,bx=gx-paper.u*pw,by=-paper.v*ph;
            const iw=pw*(1+2/paper.width),ih=ph*(1+2/paper.height);
            if([pw,ph,bx,by,iw,ih].every(v=>Number.isFinite(v)&&Number.isFinite(Math.fround(v))) && pw>0 && ph>0){
                const top=Math.floor(-by/ph),bottom=Math.ceil((Math.round(h)-by)/ph);
                const columns=clips.map(([x,y,width])=>[Math.floor((x-bx)/pw),Math.ceil((x+width-bx)/pw)]);
                const count=(bottom-top)*columns.reduce((sum,c)=>sum+c[1]-c[0],0);
                if(Number.isFinite(count)&&count>0&&count<=1024)tiles={pw,ph,bx,by,iw,ih,top,bottom,columns};
            }
        }
        margins.forEach(([x,items,side],index)=>{
            const clip=clips[index];
            cv.clip(...clip);
            if(tiles){
                const {pw,ph,bx,by,iw,ih,top,bottom,columns}=tiles;
                cv.alpha(paper.alpha);
                for(let j=top;j<bottom;j++)for(let k=columns[index][0];k<columns[index][1];k++){
                    const tx=bx+k*pw,ty=by+j*ph;
                    // Neighbouring tiles share a rounded edge, so translucent seams never overlap.
                    const left=Math.max(clip[0],Math.round(tx)),right=Math.min(clip[0]+clip[2],Math.round(bx+(k+1)*pw));
                    const y=Math.max(0,Math.round(ty)),end=Math.min(clip[3],Math.round(by+(j+1)*ph));
                    if(right<=left||end<=y)continue;
                    cv.clip(left,y,right-left,end-y);
                    // Wrapped border texels let GL_LINEAR sample across each repeat seam.
                    cv.image(2,tx-pw/paper.width,ty-ph/paper.height,iw,ih);
                }
                cv.clip(...clip);
            }
            cv.alpha(1);
            items.forEach(([name,px,py,size,opacity])=>{
                const sprite=scene.sprites[name];
                if(!sprite)return;
                const depth=(name==='platform'||name==='broken'||name==='springs') ? .6 : .25;
                const width=size*h/720,height=width*sprite.height/sprite.width;
                const y=((py*h+this.phase*depth+120)%(h+240))-120;
                cv.alpha(opacity);
                cv.image(sprite.id,x+px*sw-width/2-this.lean*depth,y-height/2,width,height);
            });
            const cx=x+sw/2,cy=h*.91,d=14*h/720;
            cv.alpha(.18+Math.max(0,side*tilt)*.45);
            cv.line(cx-side*d,cy-d,cx+side*d,cy,2,'#6b513b');
            cv.line(cx+side*d,cy,cx-side*d,cy+d,2,'#6b513b');
            cv.alpha(1);cv.noclip();
        });
    }
}
module.exports={World};

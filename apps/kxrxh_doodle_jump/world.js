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
        cv.fillRect(0,0,w,h,'#fff3e3');
        if(!sw)return;
        const paper=scene.paperWidth?scene.paperWidth*scale:0;
        const margins=[[0,2,LEFT,-1],[gx+gw,3,RIGHT,1]];
        margins.forEach(([x,id,items,side])=>{
            cv.clip(x,0,sw,h);
            if(paper)cv.image(id,side<0?gx-paper:x,0,paper,h);
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

'use strict';
const assert=require('assert');
const {World}=require('../../apps/kxrxh_doodle_jump/world');

function draw(paper,w=1920,h=720,world=new World(),tilt=0,playing=false,dt=0,sprites={}) {
    const images=[],lines=[],events=[];let alpha=1,clip=null;
    const cv={
        fillRect(){assert.fail('The app already cleared the full surface before drawing scenery');},
        image(id,x,y,width,height){images.push({id,x,y,w:width,h:height,alpha,clip:clip&&clip.slice()});},
        alpha(a){assert(a>=0&&a<=1);alpha=a;events.push(['alpha',a]);},
        clip(...r){clip=r;events.push(['clip',...r]);},noclip(){clip=null;events.push(['noclip']);},
        line(){lines.push({alpha,clip:clip&&clip.slice()});}
    };
    world.draw(cv,w,h,{paper,sprites},tilt,playing,dt);
    return {images,lines,events,alpha,clip,world};
}
function close(actual,expected,message){assert(Math.abs(actual-expected)<1e-8,message+': '+actual+' != '+expected);}
function coverage(paper,w=1920,h=720) {
    const result=draw(paper,w,h),scale=h/960,gx=(w-544*scale)/2,gw=544*scale;
    const gameLeft=Math.round(gx),gameRight=Math.round(gx+gw);
    assert(result.images.length>0,'The source paper must cover both margins');
    assert(result.images.every(image=>image.id===2),'One shared source texture serves both margins');
    result.images.forEach(image=>{
        const [x,y,cw,ch]=image.clip;
        assert(image.clip.every(Number.isInteger),'Shared clip boundaries must own whole physical pixels');
        assert(cw>0&&ch>0&&x>=-1e-8&&y>=-1e-8&&x+cw<=w+1e-8&&y+ch<=h+1e-8);
        assert(x+cw<=gameLeft||x>=gameRight,'Paper must not overlap the game pixels');
        close(image.alpha,paper.alpha,'Paper opacity');
        // Only the content area is visible; the wrapped border supplies filtering samples.
        const sx=(x+.5-image.x)/image.w*(paper.width+2),sy=(y+.5-image.y)/image.h*(paper.height+2);
        const ex=(x+cw-.5-image.x)/image.w*(paper.width+2),ey=(y+ch-.5-image.y)/image.h*(paper.height+2);
        assert(sx>=1-1e-7&&sy>=1-1e-7&&ex<=paper.width+1+1e-7&&ey<=paper.height+1+1e-7,
            'The padding must be clipped out of each tile');
    });
    assert.strictEqual(result.images.reduce((area,image)=>area+image.clip[2]*image.clip[3],0),
        (gameLeft+w-gameRight)*h,'Every physical margin pixel is covered');
    result.images.forEach((image,i)=>result.images.slice(i+1).forEach(other=>{
        const a=image.clip,b=other.clip;
        const width=Math.min(a[0]+a[2],b[0]+b[2])-Math.max(a[0],b[0]);
        const height=Math.min(a[1]+a[3],b[1]+b[3])-Math.max(a[1],b[1]);
        assert(width<=0||height<=0,'No physical pixel may receive paper alpha twice');
    }));
    for(let y=.5;y<h;y+=39)for(let x=.5;x<w;x+=31){
        if(x>=gameLeft&&x<gameRight)continue;
        const hits=result.images.filter(image=>x>=image.clip[0]&&x<image.clip[0]+image.clip[2]&&y>=image.clip[1]&&y<image.clip[1]+image.clip[3]);
        assert.strictEqual(hits.length,1,'Every margin point has exactly one visible tile at '+x+','+y);
        const image=hits[0],u=((x-image.x)/image.w*(paper.width+2)-1)/paper.width;
        const v=((y-image.y)/image.h*(paper.height+2)-1)/paper.height;
        const expectedU=paper.u+(x-gx)*paper.du/scale,expectedV=paper.v+y*paper.dv/scale;
        close(u,expectedU-Math.floor(expectedU),'Horizontal repeat phase');
        close(v,expectedV-Math.floor(expectedV),'Vertical repeat phase');
    }
    assert.strictEqual(result.alpha,1);assert.strictEqual(result.clip,null);
    result.lines.forEach(line=>assert(line.clip&&line.clip[1]===0&&line.clip[3]===h,
        'Decorations restore the full margin clip'));
    return result;
}
const paper={width:544,height:960,u:0,v:0,du:1/544,dv:1/960,alpha:1};
assert.strictEqual(coverage(paper).images.length,4,'Both wide margins repeat one source image');
coverage(Object.assign({},paper,{u:.37,v:.995,alpha:.6}));
coverage(Object.assign({},paper,{u:.37,v:.015,alpha:.6}));
coverage(Object.assign({},paper,{u:.137,v:.271,alpha:.5}),1600,520);
coverage({width:100,height:80,u:.125,v:.875,du:1/240,dv:1/320,alpha:.25},1280,480);

const sprite={platform:{id:80,width:90,height:24}},world=new World();
const paused=draw(paper,1920,720,world,0,false,0,sprite);
assert.strictEqual(world.phase,0,'Paused scenery stays still');
assert.strictEqual(paused.images.filter(image=>image.id===80).length,4,'Decorative platforms remain');
paused.images.filter(image=>image.id===80).forEach(image=>assert(image.alpha<1,'Sprite opacity stays independent of paper'));
const moving=draw(paper,1920,720,world,1,true,1/60,sprite);
assert(world.lean>0&&world.lean<18);assert(world.phase>0);
assert.strictEqual(moving.alpha,1);assert.strictEqual(moving.clip,null);

for(const change of [{width:0},{width:1.5},{height:2047},{height:Infinity},{du:0},{du:1e-300},{dv:NaN},
                     {u:-.1},{v:1},{alpha:Infinity},{du:1e6,dv:1e6}]) {
    const result=draw(Object.assign({},paper,change),1920,720,new World(),0,false,0,sprite);
    assert(!result.images.some(image=>image.id===2),'Invalid or excessive tile plans are skipped');
    assert.strictEqual(result.images.filter(image=>image.id===80).length,4,'Bad paper cannot remove decorations');
}
assert.strictEqual(draw(null).images.length,0);
console.log('Notebook paper tiling, scrolling, filtering bounds and controls: passed');

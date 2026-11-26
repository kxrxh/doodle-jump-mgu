'use strict';
const assert=require('assert'),fs=require('fs'),path=require('path'),vm=require('vm'),EventEmitter=require('events');
const record=require('../../kit/runtime/record');
let session,draw,focus,visibleChange,layout,settingsChange,input,sheet,allowSheet=true,shown=true,focused=true,active=true;
const commands=[],buttons={},subscriptions={},opened=[],grabs=[],images=[],texts=[],timeouts=[],intervals=[],events={};
const values={showFps:true,parkedOnly:true},place={x:0,y:0,w:1920,h:720,scale:1.164,ox:71,oy:-38,full:true};
class Session {
    constructor(){session=this;this.ready=true;this.dead=false;this.playing=false;this.scenery={sprites:{}};}
    start(){this.started=true;}
    send(value){commands.push(JSON.parse(JSON.stringify(value)));return true;}
    stop(){this.stopped=true;}
    frame(){assert.fail('Store JavaScript must not schedule engine frames');}
}
const gpu=new EventEmitter();
Object.assign(gpu,{ready:true,native:true,video:true,visible:true,onScreen:true,generation:7,sock:{},path:'/tmp/store-render.sock',stats:{fps:59,at:1000},waiting:false});
const placements=[],shows=[],heartbeats=[];
gpu.place=value=>placements.push(Object.assign({},value));gpu.show=value=>shows.push(value);gpu.fps=value=>heartbeats.push(value);
const ctx={
    dataDir:'/game-data',log:{info(){},warn(){},error(){}},
    settings:{get:key=>values[key],onChange:fn=>{settingsChange=fn;}},
    ui:{isVisible:()=>shown,hasFocus:()=>focused,onFocus:fn=>{focus=fn;},onVisible:fn=>{visibleChange=fn;},
        onClick:(id,fn)=>{buttons[id]=fn;},open:name=>opened.push(name),text:(id,text)=>texts.push([id,text]),image:(id,canvas)=>images.push([id,Array.from(canvas.words())])},
    display:{gpu,place,active:()=>active,sheet:null,lastInterruption:null},
    controller:{onInput:fn=>{input=fn;},grab:value=>grabs.push(value)},
    signals:{watch:(name,instance,callbacks,log,options)=>{subscriptions[name]={instance,callbacks,options};return Promise.resolve({});}},
    hideButton(){},onLayout:fn=>{layout=fn;},sheet:value=>{sheet=value;return allowSheet;},
    canvas(){const canvas=new record.Recorder(1600,520);canvas.place=place;return canvas;},
    loop(name,fps,fn){assert.strictEqual(name,'GAME');assert.strictEqual(fps,60);draw=fn;return {kick(){}};},
};
const dependencies={
    './kit/mgu':{run:spec=>spec.start(ctx)},'./kit/record':record,'./kit/sheet':{cardSheet:value=>value},
    './engine':{Session},
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../apps/kxrxh_doodle_jump/main.js'),'utf8'),{
    require:name=>{assert(dependencies[name],name);return dependencies[name];},
    Date:{now:()=>1000},setTimeout:fn=>{timeouts.push(fn);return timeouts.length;},clearTimeout(){},
    setInterval:(fn,ms)=>{intervals.push([fn,ms]);return intervals.length;},clearInterval(){},
    process:{on:(name,fn)=>{events[name]=fn;},exit(){}},
});
draw();
assert(commands.some(command=>command.ui),'Store callbacks forward state to the native lifecycle');
assert.strictEqual(timeouts.length,0,'display painting must not create an engine-frame timer');
const first=commands.filter(command=>command.ui).pop().ui;
assert.strictEqual(first.shown,true);assert.strictEqual(first.focused,true);assert.strictEqual(first.active,true);
assert.deepStrictEqual(first.place,place);assert.strictEqual(first.renderer.generation,7);
assert(first.renderer.source>0&&first.renderer.socket>0);
assert.strictEqual(grabs.length,0,'native lifecycle alone decides controller ownership');
input({frontRotaryEncoderOperation:4});
assert.deepStrictEqual(commands.pop(),{ccu:{source:'controller',value:{frontRotaryEncoderOperation:4}}});
subscriptions['de.bmw.infotainment.controlelements.FrontCCU'].callbacks.statusRotaryEncoderState({frontRotaryEncoderOperation:5});
assert.deepStrictEqual(commands.pop(),{ccu:{source:'signal',value:{frontRotaryEncoderOperation:5}}});
subscriptions['de.bmw.infotainment.vehicle.Vehicle2'].callbacks.statusCarSpeed2({speedVehicleCentreOfGravity:320});
assert.deepStrictEqual(commands.pop(),{vehicle:{speedVehicleCentreOfGravity:320}});
assert.strictEqual(subscriptions['de.bmw.infotainment.vehicle.Vehicle2'].options.pollMs,2000);
gpu.emit('touch',2,123,456);assert.deepStrictEqual(commands.pop(),{touch:{phase:2,x:123,y:456}});
buttons.BTN_PLAY();assert.deepStrictEqual(commands.pop(),{button:'play'});assert.strictEqual(opened.length,0);
session.onEvent({type:'lifecycle',running:true,info:'Ready',open:'GAME'});
assert.deepStrictEqual(grabs,[true]);assert.deepStrictEqual(opened,['GAME']);assert.deepStrictEqual(texts.pop(),['MENU_INFO','Ready']);
focused=false;focus();assert.strictEqual(commands.filter(command=>command.ui).pop().ui.focused,false);
shown=false;visibleChange();assert.strictEqual(commands.filter(command=>command.ui).pop().ui.shown,false);
values.showFps=false;settingsChange();assert.strictEqual(commands.filter(command=>command.ui).pop().ui.showFps,false);
shown=true;focused=true;focus();
const source=first.renderer.source,socket=first.renderer.socket;
session.onEvent({type:'binding',id:1,stage:'prepare',source,socket,rectangle:[-9000,-10000,1920,720]});
assert.strictEqual(shows.pop(),false);assert.strictEqual(placements.pop().x,-9000);
assert.deepStrictEqual(commands.pop(),{binding:{id:1,stage:'prepared',ok:true}});
place.w=1700;
session.onEvent({type:'binding',id:1,stage:'restore',source,socket});
assert.strictEqual(placements.pop().w,1700,'restoration uses the latest Store placement');assert.strictEqual(shows.pop(),true);
assert.deepStrictEqual(commands.pop(),{binding:{id:1,stage:'restored',ok:true}});
assert(commands[commands.length-1].ui,'restoration reports the current UI before acknowledging it');
session.onEvent({type:'binding',id:2,stage:'prepare',source,socket,rectangle:[-9000,-10000,1700,720]});
const count=placements.length;gpu.sock={};gpu.emit('state');
session.onEvent({type:'binding',id:2,stage:'restore',source,socket});
assert.strictEqual(placements.length,count,'a stale bind cannot move the new renderer connection');
assert.deepStrictEqual(commands.pop(),{binding:{id:2,stage:'restored',ok:false}});
session.onEvent({type:'presented',source,socket,fps:60});assert.strictEqual(heartbeats.length,0);
const current=commands.filter(command=>command.ui).pop().ui.renderer;
session.onEvent({type:'presented',source:current.source,socket:current.socket,fps:59});assert.deepStrictEqual(heartbeats,[59]);
session.onEvent({type:'dialog',id:4,title:'Original game',message:'Continue?',buttons:[{key:'positive',label:'Yes'},{key:'negative',label:'No'}]});
sheet.actions[0].run();sheet.onClose();assert.deepStrictEqual(commands.pop(),{dialog:{id:4,button:'positive'}});
assert(!commands.some(command=>command.action||command.frame),'lifecycle and engine scheduling stay native');
const drawing={type:'draw',w:1700,h:720,full:true,commands:[['fillRect',1,2,3,4,'#ff0000'],['text','Native',10,20,12,'#ffffff',{literal:true}]]};
session.onEvent(drawing);draw();
const expected=new record.Recorder(1600,520).clear('#fff3e3');
const overlay=new record.Recorder(1700,720);overlay.fillRect(1,2,3,4,'#ff0000');overlay.text('Native',10,20,12,'#ffffff',{literal:true});
expected.xf(1,0,0).append(overlay).xf();
assert.deepStrictEqual(images.pop(),['GAME_GFX',Array.from(expected.words())]);
assert.throws(()=>session.onEvent({type:'draw',w:1920,h:720,full:true,commands:[['constructor']]}),/drawing command/i);
place.w=1500;layout();session.onEvent(drawing);draw();
const loading=new record.Recorder(1600,520).clear('#fff3e3');loading.text('Loading original Doodle Jump...',800,260,24,'#573f2b',{align:'center',font:'bold'});
assert.deepStrictEqual(images.pop(),['GAME_GFX',Array.from(loading.words())],'an old-layout native draw cannot repaint after a Store resize');
active=false;layout();session.onEvent({type:'draw',w:1600,h:520,full:false,commands:[['fillRect',10,20,30,40,'#0000ff']]});draw();
const fallback=new record.Recorder(1600,520).clear('#fff3e3');fallback.fillRect(10,20,30,40,'#0000ff');
assert.deepStrictEqual(images.pop(),['GAME_GFX',Array.from(fallback.words())],'an inactive Store renderer still publishes the logical CPU/HMI canvas');
allowSheet=false;session.onEvent({type:'dialog',id:5,title:'Original game',message:'Unavailable sheet',buttons:[{key:'negative',label:'Cancel'}]});
assert.deepStrictEqual(commands.filter(command=>command.dialog && command.dialog.id===5),[{dialog:{id:5,button:'negative'}}]);
active=true;session.onEvent({type:'binding',id:3,stage:'prepare',source:current.source,socket:current.socket,rectangle:[-9000,-10000,1500,720]});
session.onError(new Error('Native process exited'));draw();
assert.strictEqual(placements[placements.length-1].x,0,'a dead native process restores the temporary Store binding placement');assert.strictEqual(shows[shows.length-1],true);
const failed=new record.Recorder(1600,520).clear('#fff3e3');failed.text('Engine stopped: Native process exited',800,260,24,'#573f2b',{align:'center',font:'bold'});
assert.deepStrictEqual(images.pop(),['GAME_GFX',Array.from(failed.words())],'a dead native process leaves a visible error and releases the controller');assert.strictEqual(grabs[grabs.length-1],false);
session.onEvent({type:'binding',id:6,stage:'prepare',source:current.source,socket:current.socket,rectangle:[-9000,-10000,1500,720]});
const beforeExit=placements.length;
events.exit();assert.strictEqual(session.stopped,true);
assert.strictEqual(placements.length,beforeExit+1,'normal shutdown restores a pending Store placement challenge');
assert.strictEqual(placements[placements.length-1].x,0);assert.strictEqual(shows[shows.length-1],true);
assert.strictEqual(intervals.length,1);assert.strictEqual(intervals[0][1],200,'the only JS timer reports Store liveness');
console.log('Store adapter state, native lifecycle effects, renderer binding, dialogs, drawing and shutdown: passed');

'use strict';
const assert = require('assert');
const { Session } = require('../../apps/kxrxh_doodle_jump/engine');
assert.strictEqual(typeof Session, 'function', 'the native session owns scheduling and GPU communication');
let PacketReader;
try { ({ PacketReader } = require('../../apps/kxrxh_doodle_jump/engine')); }
catch (e) { assert.fail('The MGU original-engine packet reader is missing: ' + e.message); }
const seen = [];
const reader = new PacketReader((type, payload) => seen.push([type, Buffer.from(payload)]));
const data = Buffer.from([1, 2, 3, 255]);
const packet = Buffer.alloc(12);
packet.write('FRM1', 0); packet.writeUInt32LE(data.length, 4); data.copy(packet, 8);
for (let i = 0; i < packet.length; i++) {
    reader.feed(packet.slice(i, i + 1));
    if (i < packet.length - 1) assert.strictEqual(seen.length, 0, 'partial packet bytes stay private');
}
assert.strictEqual(seen.length, 1);
assert.strictEqual(seen[0][0], 'FRM1');
assert.deepStrictEqual(seen[0][1], data);
const invalid = Buffer.alloc(8);
invalid.write('FRM1'); invalid.writeUInt32LE(0xffffffff, 4);
assert.throws(() => new PacketReader(() => {}).feed(invalid), /size/i);
invalid.write('BAD1'); invalid.writeUInt32LE(0, 4);
assert.throws(() => new PacketReader(() => {}).feed(invalid), /type/i);
console.log('Original engine packet boundaries: passed');
function wire(type, body) {
    const header=Buffer.alloc(8);header.write(type);header.writeUInt32LE(body.length,4);
    return Buffer.concat([header,body]);
}
const retained=[];
const immutable=new PacketReader((type,body)=>retained.push(body));
for(let value=1;value<=3;value++)immutable.feed(wire('FRM1',Buffer.alloc(16,value)));
retained.forEach((body,index)=>assert.deepStrictEqual(body,Buffer.alloc(16,index+1),'default readers preserve completed bodies'));
const recycled=[],persistent=[];
const reusable=new PacketReader((type,body)=>(type==='FRM1'?recycled:persistent).push(body),true);
reusable.feed(wire('FRM1',Buffer.alloc(16,1)));
reusable.feed(wire('TEX1',Buffer.alloc(16,99)));
reusable.feed(wire('EVT1',Buffer.alloc(16,88)));
reusable.feed(wire('FRM1',Buffer.alloc(16,2)));
const third=wire('FRM1',Buffer.alloc(16,3));
reusable.feed(third.slice(0,12));
assert.strictEqual(recycled.length,2,'a recycled buffer stays private until complete');
assert.deepStrictEqual(recycled[1],Buffer.alloc(16,2),'the displayed frame survives a partial successor');
reusable.feed(third.slice(12));
assert.strictEqual(recycled[2],recycled[0],'the third frame reuses the first frame buffer');
assert.deepStrictEqual(recycled[2],Buffer.alloc(16,3));
reusable.feed(wire('FRM1',Buffer.alloc(16,4)));
assert.strictEqual(recycled[3],recycled[1],'the fourth frame reuses the second frame buffer');
reusable.feed(wire('FRM1',Buffer.alloc(24,5)));
assert.notStrictEqual(recycled[4],recycled[2],'changed dimensions allocate a matching buffer');
assert.deepStrictEqual(recycled[4],Buffer.alloc(24,5));
assert.deepStrictEqual(recycled[3],Buffer.alloc(16,4),'a resized successor preserves the current frame');
assert.deepStrictEqual(persistent,[Buffer.alloc(16,99),Buffer.alloc(16,88)],'textures and events never enter the frame buffers');
console.log('Optional two-frame recycling and retained packet ownership: passed');
const fs=require('fs'),path=require('path'),os=require('os'),zlib=require('zlib');
const {unpack}=require('../../apps/kxrxh_doodle_jump/engine');
const dir=fs.mkdtempSync(path.join(os.tmpdir(),'doodle-pack-'));
function remove(directory) {
    fs.readdirSync(directory).forEach(name=>{
        const file=path.join(directory,name);
        if(fs.lstatSync(file).isDirectory())remove(file);else fs.unlinkSync(file);
    });
    fs.rmdirSync(directory);
}
function pack(files, editIndex, output) {
    const parts=[], index={files:{}};
    let offset=0;
    Object.keys(files).forEach(name=>{const data=zlib.deflateRawSync(Buffer.from(files[name]));index.files[name]=[offset,data.length];offset+=data.length;parts.push(data);});
    if(editIndex)editIndex(index);
    const meta=zlib.deflateRawSync(Buffer.from(JSON.stringify(index)));
    const header=Buffer.alloc(12);header.write('MGUPACK1');header.writeUInt32LE(meta.length,8);
    const file=output||path.join(dir,'engine.pack');fs.writeFileSync(file,Buffer.concat([header,meta].concat(parts)));return file;
}
try {
    const out=path.join(dir,'output');
    const large=require('crypto').randomBytes(2<<20);
    const largePack=pack({'runtime.bin':large,'assets/sound.ogg':'sound'});
    const readFile=fs.readFileSync,read=fs.readSync,open=fs.openSync;
    let largestRead=0,packFd,bytesRead=0,dataStart;
    const entryBuffers=[];
    try {
        fs.openSync=function(file) { const fd=open.apply(this,arguments);if(file===largePack)packFd=fd;return fd; };
        fs.readFileSync=function(file) {
            assert.notStrictEqual(file,largePack,'unpack must not retain the whole compressed game');
            return readFile.apply(this,arguments);
        };
        fs.readSync=function(fd,buffer,offset,length,position) {
            largestRead=Math.max(largestRead,length);
            assert(length<=1<<20,'pack reads must stay within the 1 MiB I/O budget');
            if(position===12)dataStart=12+length;
            if(dataStart!==undefined&&position>=dataStart)entryBuffers.push(buffer);
            const n=read.apply(this,arguments);if(fd===packFd&&typeof position==='number')bytesRead+=n;return n;
        };
        unpack(largePack,out);
        assert.strictEqual(new Set(entryBuffers).size,1,'compressed entries reuse one scratch buffer');
        dataStart=undefined;
        fs.writeFileSync(path.join(out,'cache-hit'),'retained');
        bytesRead=0;
        unpack(largePack,out);
        assert.strictEqual(bytesRead,fs.statSync(largePack).size,'cache hits still verify the complete pack hash');
        assert.throws(()=>fs.fstatSync(packFd),error=>error.code==='EBADF','a cache hit closes its pack descriptor');
    } finally { fs.readFileSync=readFile;fs.readSync=read;fs.openSync=open; }
    assert(largestRead>0);
    assert.deepStrictEqual(fs.readFileSync(path.join(out,'runtime.bin')),large);
    assert.strictEqual(fs.readFileSync(path.join(out,'assets/sound.ogg'),'utf8'),'sound');
    assert.strictEqual(fs.readFileSync(path.join(out,'cache-hit'),'utf8'),'retained','cache hits do not extract again');

    unpack(pack({'native.so':'dependency','a/b/data':'original'}),out);
    assert.strictEqual(fs.readFileSync(path.join(out,'a/b/data'),'utf8'),'original');
    fs.writeFileSync(path.join(dir,'scores'),'saved score');
    fs.writeFileSync(path.join(out,'a/b/obsolete'),'old runtime');
    unpack(pack({'native.so':'updated','a/data':'new runtime'}),out);
    assert(!fs.existsSync(path.join(out,'a/b/obsolete')),'updating removes obsolete runtime files');
    assert.strictEqual(fs.readFileSync(path.join(dir,'scores'),'utf8'),'saved score','game saves outside the runtime survive');
    assert.throws(()=>unpack(pack({'../outside':'bad'}),out),/Unsafe/);
    assert.strictEqual(fs.readFileSync(path.join(out,'a/data'),'utf8'),'new runtime','invalid packs do not clear the cache');
    assert(!fs.existsSync(path.join(dir,'outside')));
    [ [Number.MAX_SAFE_INTEGER+1,1], [0,Number.MAX_SAFE_INTEGER+1], [-1,1], [0,0], [0,1.5] ].forEach(part=>{
        assert.throws(()=>unpack(pack({'a/data':'bad'},index=>{index.files['a/data']=part;}),out),/range/i);
        assert.strictEqual(fs.readFileSync(path.join(out,'a/data'),'utf8'),'new runtime','invalid numeric ranges do not clear the cache');
    });
    assert.throws(()=>unpack(pack({'a/data':'bad'},index=>{index.files=null;}),out),/index/i);
    assert.strictEqual(fs.readFileSync(path.join(out,'a/data'),'utf8'),'new runtime');

    const cached=pack({'a/data':'cached runtime'});
    unpack(cached,out);
    fs.writeFileSync(path.join(out,'cache-hit'),'retained');
    unpack(cached,out);
    assert.strictEqual(fs.readFileSync(path.join(out,'cache-hit'),'utf8'),'retained','a matching cache is not extracted again');
    const alias=path.join(dir,'cache-link');fs.symlinkSync(out,alias,'dir');
    assert.throws(()=>unpack(cached,alias),/Unsafe/,'a cache marker must not make a symlink root acceptable');

    const nativePack=pack({'doodle-host':'prebuilt native host','native.so':'shared backend'});
    unpack(nativePack,out);
    const executable=path.join(out,'doodle-host'),library=path.join(out,'native.so');
    assert.strictEqual(fs.lstatSync(executable).mode&0o777,0o755,'the unpacked native host is executable');
    assert.strictEqual(fs.lstatSync(library).mode&0o111,0,'data and shared libraries do not become executable');
    fs.chmodSync(executable,0o600);unpack(nativePack,out);
    assert.strictEqual(fs.lstatSync(executable).mode&0o777,0o755,'cache hits restore executable permissions');
    const outside=path.join(dir,'outside-host');fs.writeFileSync(outside,'outside');fs.chmodSync(outside,0o600);
    fs.unlinkSync(executable);fs.symlinkSync(outside,executable);
    assert.throws(()=>unpack(nativePack,out),/unsafe|executable|host/i,'cached symlinks cannot change another file permissions');
    assert.strictEqual(fs.statSync(outside).mode&0o777,0o600);

    const partial=pack({'a/data':'short reads still restore these exact bytes'});
    try {
        fs.readSync=function(fd,buffer,offset,length,position) { return read.call(this,fd,buffer,offset,Math.min(length,7),position); };
        unpack(partial,out);
    } finally { fs.readSync=read; }
    assert.strictEqual(fs.readFileSync(path.join(out,'a/data'),'utf8'),'short reads still restore these exact bytes');

    try {
        fs.readSync=()=>0;
        assert.throws(()=>unpack(partial,out),/Truncated engine data pack/,'an unexpected EOF reports the pack that could not be read');
    } finally { fs.readSync=read; }
    assert.strictEqual(fs.readFileSync(path.join(out,'a/data'),'utf8'),'short reads still restore these exact bytes');

    const broken=fs.readFileSync(partial);fs.writeFileSync(partial,broken.slice(0,broken.length-1));
    let opened;
    try {
        fs.openSync=function(file) { const fd=open.apply(this,arguments);if(file===partial)opened=fd;return fd; };
        assert.throws(()=>unpack(partial,out),/range|truncat/i);
    } finally { fs.openSync=open; }
    assert.throws(()=>fs.fstatSync(opened),error=>error.code==='EBADF','a rejected pack closes its file descriptor');
    assert.strictEqual(fs.readFileSync(path.join(out,'a/data'),'utf8'),'short reads still restore these exact bytes','a truncated pack does not clear the old cache');
    fs.writeFileSync(partial,Buffer.from('MGU'));
    assert.throws(()=>unpack(partial,out),/truncat|invalid/i);
    assert.strictEqual(fs.readFileSync(path.join(out,'a/data'),'utf8'),'short reads still restore these exact bytes');
} finally { remove(dir); }
console.log('Engine pack extraction and traversal rejection: passed');
const engine=new Session('',{info(){},error(){}}),textures=[];
engine.onTexture=(id,w,h,pixels)=>textures.push([id,w,h,Array.from(pixels)]);
const tex=Buffer.alloc(16);tex.writeUInt32LE(2,0);tex.writeUInt32LE(1,4);tex.writeUInt32LE(1,8);tex.set([1,2,3,255],12);
engine.packet('TEX1',tex);
assert.deepStrictEqual(textures,[[2,1,1,[1,2,3,255]]]);
tex.writeUInt32LE(1,0);assert.throws(()=>engine.packet('TEX1',tex),/Invalid/);
tex.writeUInt32LE(2,0);tex.writeUInt32LE(4096,4);assert.throws(()=>engine.packet('TEX1',tex),/Invalid/);
console.log('Scenery texture boundaries: passed');
const frames=[];
engine.onPixels=(pixels,w,h)=>frames.push([pixels.length,w,h,pixels[0],pixels[pixels.length-1]]);
for(const size of [[544,960],[408,720]]) {
    const w=size[0],h=size[1],body=Buffer.alloc(8+w*h*4,0xCD);
    body.writeUInt32LE(w,0);body.writeUInt32LE(h,4);
    engine.packet('FRM1',body);
    assert.deepStrictEqual(frames[frames.length-1],[w*h*4,w,h,0xCD,0xCD]);
    for(const malformed of [body.slice(0,-1),Buffer.concat([body,Buffer.from([0])])]) {

        assert.throws(()=>engine.packet('FRM1',malformed),/dimensions/i);
    }
}
for(const size of [[408,960],[544,720],[0,0],[4096,4096]]) {
    const body=Buffer.alloc(8);body.writeUInt32LE(size[0],0);body.writeUInt32LE(size[1],4);
    assert.throws(()=>engine.packet('FRM1',body),/dimensions/i);
}
console.log('Logical and display-size frame boundaries: passed');

function patchFrame(w,h,rectangles) {
    const header=Buffer.alloc(12+rectangles.length*16);
    header.writeUInt32LE(w,0);header.writeUInt32LE(h,4);header.writeUInt32LE(rectangles.length,8);
    rectangles.forEach((r,i)=>[r.x,r.y,r.w,r.h].forEach((v,j)=>header.writeUInt32LE(v,12+i*16+j*4)));
    return Buffer.concat([header].concat(rectangles.map(r=>r.data)));
}
const partial=new Session('',{info(){},error(){}}),updates=[];
partial.onPixels=(pixels,w,h,patches)=>updates.push({pixels:Buffer.from(pixels),live:pixels,w,h,patches});
const key=patchFrame(408,720,[{x:0,y:0,w:408,h:720,data:Buffer.alloc(408*720*4,7)}]);
assert.throws(()=>partial.packet('FRM2',patchFrame(408,720,[])),/frame|key|initial/i,'a partial stream starts with a complete frame');
assert.throws(()=>partial.packet('FRM2',patchFrame(408,720,[{x:0,y:0,w:1,h:1,data:Buffer.alloc(4)}])),/key|initial/i);
partial.packet('FRM2',key);
assert.deepStrictEqual(updates[0].pixels,Buffer.alloc(408*720*4,7));
const changes=[{x:407,y:719,w:1,h:1,data:Buffer.from([8,9,10,11])},
    {x:2,y:3,w:2,h:2,data:Buffer.from([12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27])}];
const deltaPacket=patchFrame(408,720,changes);
partial.packet('FRM2',deltaPacket);
const expected=Buffer.alloc(408*720*4,7);
expected.set([8,9,10,11],(719*408+407)*4);
expected.set(changes[1].data.slice(0,8),(3*408+2)*4);
expected.set(changes[1].data.slice(8),(4*408+2)*4);
assert.deepStrictEqual(updates[1].pixels,expected,'nonzero origins and packed rows reconstruct the complete frame');
assert.strictEqual(updates[1].live,updates[0].live,'partial frames reuse the separate complete image cache');
assert.deepStrictEqual(updates[1].patches.map(r=>Array.from(r.data)),changes.map(r=>Array.from(r.data)));
partial.packet('FRM2',patchFrame(408,720,[]));
assert.deepStrictEqual(updates[2].pixels,expected);assert.deepStrictEqual(updates[2].patches,[]);
const malformed=[deltaPacket.slice(0,-1),Buffer.concat([deltaPacket,Buffer.from([0])]),
    patchFrame(408,720,[{x:408,y:0,w:1,h:1,data:Buffer.alloc(4)}]),
    patchFrame(408,720,[{x:0,y:720,w:1,h:1,data:Buffer.alloc(4)}]),
    patchFrame(408,720,[{x:0,y:0,w:0,h:1,data:Buffer.alloc(0)}]),
    patchFrame(408,720,[{x:0,y:0,w:1,h:1,data:Buffer.from([99,98,97,96])},
        {x:408,y:0,w:1,h:1,data:Buffer.alloc(4)}]),
    patchFrame(544,960,[]),patchFrame(408,960,[])];
const excessive=Buffer.alloc(12);excessive.writeUInt32LE(408,0);excessive.writeUInt32LE(720,4);excessive.writeUInt32LE(65,8);
malformed.push(excessive);
for(const body of malformed) {

    assert.throws(()=>partial.packet('FRM2',body),/frame|region|key/i);
    assert.deepStrictEqual(updates[2].live,expected,'invalid updates cannot modify published pixels');
    assert.strictEqual(updates.length,3);
}
const resizingKey=patchFrame(544,960,[{x:0,y:0,w:544,h:960,data:Buffer.alloc(544*960*4,29)}]);
partial.packet('FRM2',resizingKey);
assert.deepStrictEqual(updates[3].pixels,Buffer.alloc(544*960*4,29));
assert.notStrictEqual(updates[3].live,updates[2].live,'a new size gets its own complete image');
const framedUpdates=[];
const incremental=new PacketReader((type,body)=>framedUpdates.push([type,body]),true);
const firstWire=wire('FRM2',deltaPacket);
incremental.feed(firstWire.slice(0,31));assert.strictEqual(framedUpdates.length,0);
incremental.feed(firstWire.slice(31));
incremental.feed(wire('FRM2',patchFrame(408,720,[])));
incremental.feed(wire('FRM2',patchFrame(408,720,changes.map(r=>Object.assign({},r,{data:Buffer.alloc(r.data.length,99)})))));
assert.strictEqual(framedUpdates[0][0],'FRM2');
assert.deepStrictEqual(framedUpdates[0][1],deltaPacket,'region payloads stay immutable across unchanged frames');
console.log('Lossless region frame reconstruction and validation: passed');

{
    let published=null,snapshot=null;
    const sameStorage=(a,b)=>a.buffer===b.buffer && a.byteOffset===b.byteOffset;
    const pooled=new PacketReader((type,body)=>{
        if(published)assert.deepStrictEqual(published,snapshot,'the last changed packet survives until its successor is delivered');
        if(type==='FRM1'||body.length!==12){published=body;snapshot=Buffer.from(body);}
    },true);
    pooled.feed(wire('FRM2',Buffer.alloc(2048,1)));
    assert(pooled.frames[0],'changed region packets use a frame slot');
    const first=published;
    for(let i=0;i<25;i++)pooled.feed(wire('FRM2',patchFrame(408,720,[])));
    assert.strictEqual(pooled.frameSlot,1,'unchanged frames do not advance the borrowed slot');
    assert.strictEqual(published,first);
    const successor=wire('FRM2',Buffer.alloc(1500,2));
    pooled.feed(successor.slice(0,500));
    assert.deepStrictEqual(published,snapshot,'a fragmented successor keeps published patches intact');
    pooled.feed(successor.slice(500));
    assert(!sameStorage(published,first));
    const second=published;
    pooled.feed(wire('FRM2',Buffer.alloc(1000,3)));
    assert(sameStorage(published,first),'smaller packets reuse the slot capacity');
    assert.strictEqual(published.length,1000,'a reused packet exposes only its own bytes');
    pooled.feed(wire('FRM1',Buffer.alloc(100,4)));
    assert(sameStorage(published,second),'FRM1 and changed FRM2 share the two slots');
    pooled.feed(wire('FRM2',Buffer.alloc(5000,5)));
    assert(!sameStorage(published,first),'a larger packet grows its slot');
    const larger=published;
    for(let i=0;i<10;i++)pooled.feed(wire('FRM2',patchFrame(408,720,[])));
    pooled.feed(wire('FRM1',Buffer.alloc(3000,6)));
    pooled.feed(wire('FRM2',Buffer.alloc(200,7)));
    assert(sameStorage(published,larger),'the grown slot survives later size changes');
    assert.strictEqual(published.length,200);
}
console.log('Region packet recycling across unchanged and fragmented frames: passed');


assert.throws(()=>new PacketReader(()=>{}).feed(wire('GPF1',Buffer.alloc(0))),/type/i,
    'GPU scene bytes stay between Rust and the GPU worker');
const events=[];engine.onEvent=value=>events.push(value);
engine.packet('EVT1',Buffer.from(JSON.stringify({type:'lifecycle',running:false,info:'Paused'})));
assert.deepStrictEqual(events,[{type:'lifecycle',running:false,info:'Paused'}]);
for(const bad of ['null','[]','{}','{"type":3}'])
    assert.throws(()=>engine.packet('EVT1',Buffer.from(bad)),/event/i);
console.log('Native session events and direct GPU boundary: passed');

const childProcess=require('child_process'),EventEmitter=require('events');
const savedSpawn=childProcess.spawn;
const names=['DOODLE_ENGINE','DOODLE_HOST','DOODLE_ORIGINAL','DOODLE_DATA'];
const environment={};names.forEach(key=>environment[key]=process.env[key]);
const local=fs.mkdtempSync(path.join(os.tmpdir(),'doodle-launch-'));
let launched;
try {
    childProcess.spawn=(command,args,options)=>{
        launched={command,args,options};
        const child=new EventEmitter();
        child.stdout=new EventEmitter();child.stderr=new EventEmitter();child.stdin=new EventEmitter();child.kill=()=>{};
        launched.child=child;return child;
    };
    delete require.cache[require.resolve('../../apps/kxrxh_doodle_jump/engine')];
    const LaunchSession=require('../../apps/kxrxh_doodle_jump/engine').Session;
    process.env.DOODLE_ENGINE=local;delete process.env.DOODLE_HOST;delete process.env.DOODLE_ORIGINAL;delete process.env.DOODLE_DATA;
    const session=new LaunchSession('/game-data',{info(){},error(){}});session.start();
    assert.strictEqual(launched.command,path.join(local,'doodle-host'));
    assert.strictEqual(launched.options.env.MALLOC_MMAP_THRESHOLD_,'1048576');
    assert.deepStrictEqual(launched.args,['--session','--data','/game-data/game','--original',path.join(local,'original'),'--runtime',local]);
    const delivered=[];session.onPixels=pixels=>delivered.push(pixels);
    for(const size of [[408,720],[408,720],[408,720],[544,960],[408,720]]) {
        const body=Buffer.alloc(8+size[0]*size[1]*4,delivered.length+1);body.writeUInt32LE(size[0],0);body.writeUInt32LE(size[1],4);
        launched.child.stdout.emit('data',wire('FRM1',body));
    }
    assert.strictEqual(delivered.length,5);
    assert.strictEqual(delivered[2].buffer,delivered[0].buffer);
    assert.strictEqual(delivered[2].byteOffset,delivered[0].byteOffset);
    assert.strictEqual(delivered[3].length,544*960*4);
    assert.strictEqual(delivered[4][delivered[4].length-1],5);
    process.env.DOODLE_HOST='/local/native-wrapper';process.env.DOODLE_ORIGINAL='/local/original';
    new LaunchSession('/game-data',{info(){},error(){}}).start();
    assert.strictEqual(launched.command,'/local/native-wrapper');
    assert.deepStrictEqual(launched.args,['--session','--data','/game-data/game','--original','/local/original','--runtime',local]);
} finally {
    childProcess.spawn=savedSpawn;
    names.forEach(key=>{if(environment[key]===undefined)delete process.env[key];else process.env[key]=environment[key];});
    remove(local);
}
console.log('Native session launch, development overrides and frame recycling: passed');

async function nativeLaunches() {
    const directory=fs.mkdtempSync(path.join(os.tmpdir(),'doodle-native-launch-'));
    const source=path.resolve(__dirname,'../../apps/kxrxh_doodle_jump');
    const dataDir=path.join(directory,'saved-game'),runtime=path.join(dataDir,'engine'),received=path.join(directory,'received.json');
    const previous={};names.forEach(key=>previous[key]=process.env[key]);
    function program(original,runtime) {
        const args=['--session','--data',path.join(dataDir,'game'),'--original',original,'--runtime',runtime];
        return '#!'+process.execPath+'\n'+
            "'use strict';const fs=require('fs'),assert=require('assert');assert.deepStrictEqual(process.argv.slice(2),"+JSON.stringify(args)+");let commands=[];"+
            "function packet(kind,body){const h=Buffer.alloc(8);h.write(kind);h.writeUInt32LE(body.length,4);return Buffer.concat([h,body]);}"+
            "const ready=packet('EVT1',Buffer.from(JSON.stringify({type:'ready'})));process.stdout.write(ready.slice(0,3));"+
            "setImmediate(()=>process.stdout.write(ready.slice(3)));let input='';"+
            "process.stdin.on('data',bytes=>{input+=bytes;let n;while((n=input.indexOf('\\n'))>=0){const c=JSON.parse(input.slice(0,n));input=input.slice(n+1);commands.push(c);"+
            "if(c.shutdown){fs.writeFileSync("+JSON.stringify(received)+",JSON.stringify(commands));process.exit(0);}"+
            "assert(!c.frame,'JavaScript must not request game frames');"+
            "if(c.button==='select'){const frame=Buffer.alloc(28+408*720*4,17);frame.writeUInt32LE(408,0);frame.writeUInt32LE(720,4);frame.writeUInt32LE(1,8);frame.writeUInt32LE(0,12);frame.writeUInt32LE(0,16);frame.writeUInt32LE(408,20);frame.writeUInt32LE(720,24);process.stdout.write(packet('FRM2',frame));}}});\n";
    }
    async function run(Launcher) {
        const errors=[],session=new Launcher(dataDir,{info(){},error(message){errors.push(message);}});
        let timer,child;
        try {
            await new Promise((resolve,reject)=>{
                timer=setTimeout(()=>reject(new Error('Native session handshake timed out: '+errors.join('; '))),5000);
                session.onError=reject;
                session.onEvent=event=>{if(event.type==='ready'){session.send({ui:{shown:true,focused:true}});session.send({button:'select'});}};
                session.onPixels=(pixels,w,h,patches)=>{
                    try {
                        assert.strictEqual(w,408);assert.strictEqual(h,720);
                        assert.deepStrictEqual(pixels,Buffer.alloc(408*720*4,17));
                        assert.strictEqual(patches.length,1);resolve();
                    } catch(error) { reject(error); }
                };
                session.start();child=session.child;
            });
            assert.throws(()=>session.send({invalid:'x'.repeat(4096)}),/size limit/);
        } finally {
            clearTimeout(timer);session.stop();
            if(child&&child.exitCode===null)await new Promise(resolve=>child.once('exit',resolve));
        }
        assert.strictEqual(child.exitCode,0,'shutdown reaches the native host before its input closes');
        assert.deepStrictEqual(JSON.parse(fs.readFileSync(received,'utf8')),[{ui:{shown:true,focused:true}},{button:'select'},{shutdown:true}]);
    }
    try {
        names.forEach(key=>delete process.env[key]);
        fs.copyFileSync(path.join(source,'engine.js'),path.join(directory,'engine.js'));
        pack({'doodle-host':program(path.join(runtime,'original'),runtime),'native.so':'shared backend'},null,path.join(directory,'engine.pack'));
        const Launcher=require(path.join(directory,'engine')).Session;
        await run(Launcher);
        assert.strictEqual(fs.lstatSync(path.join(runtime,'doodle-host')).mode&0o777,0o755);
        assert.strictEqual(fs.lstatSync(path.join(runtime,'native.so')).mode&0o111,0);
        fs.chmodSync(path.join(runtime,'doodle-host'),0o600);await run(Launcher);
        assert.strictEqual(fs.lstatSync(path.join(runtime,'doodle-host')).mode&0o777,0o755);
        const custom=path.join(directory,'custom-runtime'),wrapper=path.join(directory,'native-wrapper'),original=path.join(directory,'custom-original');
        fs.mkdirSync(custom);fs.writeFileSync(path.join(custom,'doodle-host'),'unexecuted source binary');fs.chmodSync(path.join(custom,'doodle-host'),0o600);
        fs.writeFileSync(wrapper,program(original,custom));fs.chmodSync(wrapper,0o700);
        process.env.DOODLE_ENGINE=custom;process.env.DOODLE_HOST=wrapper;process.env.DOODLE_ORIGINAL=original;await run(Launcher);
        assert.strictEqual(fs.statSync(wrapper).mode&0o777,0o700);
        assert.strictEqual(fs.statSync(path.join(custom,'doodle-host')).mode&0o777,0o600);
    } finally {
        names.forEach(key=>{if(previous[key]===undefined)delete process.env[key];else process.env[key]=previous[key];});
        remove(directory);
    }
}
nativeLaunches().then(()=>console.log('Precompiled session extraction, real process protocol, cached restart and graceful shutdown: passed'))
    .catch(error=>{console.error(error);process.exitCode=1;});

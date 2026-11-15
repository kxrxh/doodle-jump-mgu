'use strict';
const assert = require('assert');
let PacketReader;
try { ({ PacketReader } = require('../apps/kxrxh_doodle_jump/engine')); }
catch (e) { assert.fail('The MGU original-engine packet reader is missing: ' + e.message); }
const seen = [];
const reader = new PacketReader((type, payload) => seen.push([type, Buffer.from(payload)]));
const data = Buffer.from([1, 2, 3, 255]);
const packet = Buffer.alloc(12);
packet.write('FRM1', 0); packet.writeUInt32LE(data.length, 4); data.copy(packet, 8);
for (let i = 0; i < packet.length; i++) reader.feed(packet.slice(i, i + 1));
assert.strictEqual(seen.length, 1);
assert.strictEqual(seen[0][0], 'FRM1');
assert.deepStrictEqual(seen[0][1], data);
const invalid = Buffer.alloc(8);
invalid.write('FRM1'); invalid.writeUInt32LE(0xffffffff, 4);
assert.throws(() => new PacketReader(() => {}).feed(invalid), /size/i);
invalid.write('BAD1'); invalid.writeUInt32LE(0, 4);
assert.throws(() => new PacketReader(() => {}).feed(invalid), /type/i);
console.log('Original engine packet boundaries: passed');
const fs=require('fs'),path=require('path'),os=require('os'),zlib=require('zlib');
const {unpack}=require('../apps/kxrxh_doodle_jump/engine');
const dir=fs.mkdtempSync(path.join(os.tmpdir(),'doodle-pack-'));
function remove(directory) {
    fs.readdirSync(directory).forEach(name=>{
        const file=path.join(directory,name);
        if(fs.lstatSync(file).isDirectory())remove(file);else fs.unlinkSync(file);
    });
    fs.rmdirSync(directory);
}
function pack(files, editIndex) {
    const parts=[], index={files:{}};
    let offset=0;
    Object.keys(files).forEach(name=>{const data=zlib.deflateRawSync(Buffer.from(files[name]));index.files[name]=[offset,data.length];offset+=data.length;parts.push(data);});
    if(editIndex)editIndex(index);
    const meta=zlib.deflateRawSync(Buffer.from(JSON.stringify(index)));
    const header=Buffer.alloc(12);header.write('MGUPACK1');header.writeUInt32LE(meta.length,8);
    const file=path.join(dir,'engine.pack');fs.writeFileSync(file,Buffer.concat([header,meta].concat(parts)));return file;
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

    unpack(pack({'site-packages/dependency.py':'dependency','a/b/data':'original'}),out);
    assert.strictEqual(fs.readFileSync(path.join(out,'a/b/data'),'utf8'),'original');
    assert(!fs.existsSync(path.join(out,'python')),'the runtime uses Store Python');
    fs.writeFileSync(path.join(dir,'scores'),'saved score');
    fs.writeFileSync(path.join(out,'a/b/obsolete'),'old runtime');
    unpack(pack({'site-packages/dependency.py':'updated','a/data':'new runtime'}),out);
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
const {Engine}=require('../apps/kxrxh_doodle_jump/engine');
const engine=new Engine('',{info(){},error(){}}),textures=[];
engine.onTexture=(id,w,h,pixels)=>textures.push([id,w,h,Array.from(pixels)]);
const tex=Buffer.alloc(16);tex.writeUInt32LE(2,0);tex.writeUInt32LE(1,4);tex.writeUInt32LE(1,8);tex.set([1,2,3,255],12);
engine.packet('TEX1',tex);
assert.deepStrictEqual(textures,[[2,1,1,[1,2,3,255]]]);
tex.writeUInt32LE(1,0);assert.throws(()=>engine.packet('TEX1',tex),/Invalid/);
tex.writeUInt32LE(2,0);tex.writeUInt32LE(4096,4);assert.throws(()=>engine.packet('TEX1',tex),/Invalid/);
console.log('Scenery texture boundaries: passed');

const childProcess=require('child_process'),EventEmitter=require('events');
const savedSpawn=childProcess.spawn;
const savedEnv={engine:process.env.DOODLE_ENGINE,python:process.env.DOODLE_PYTHON,path:process.env.PYTHONPATH};
const local=fs.mkdtempSync(path.join(os.tmpdir(),'doodle-launch-'));
let launched;
try {
    childProcess.spawn=(command,args,options)=>{
        launched={command,args,options};
        const child=new EventEmitter();
        child.stdout=new EventEmitter();child.stderr=new EventEmitter();child.stdin=new EventEmitter();child.kill=()=>{};
        return child;
    };
    delete require.cache[require.resolve('../apps/kxrxh_doodle_jump/engine')];
    const LaunchEngine=require('../apps/kxrxh_doodle_jump/engine').Engine;
    process.env.DOODLE_ENGINE=local;delete process.env.DOODLE_PYTHON;process.env.PYTHONPATH='/unrelated/modules';
    new LaunchEngine('/game-data',{info(){},error(){}}).start();
    assert.strictEqual(launched.command,'/var/opt/mgu-python/current/bin/python3','uses the Store interpreter');
    assert.strictEqual(launched.options.env.PYTHONPATH,path.join(local,'site-packages'),'private dependencies reach Store Python');
    assert.strictEqual(launched.options.env.PYTHONNOUSERSITE,'1');
    assert.strictEqual(process.env.PYTHONPATH,'/unrelated/modules','launch does not change the parent environment');
    assert.deepStrictEqual(launched.args,[path.join(local,'engine_process.py'),'--data','/game-data/game']);
    process.env.DOODLE_PYTHON='/local/python';
    new LaunchEngine('/game-data',{info(){},error(){}}).start();
    assert.strictEqual(launched.command,'/local/python','local emulator interpreter override still works');
} finally {
    childProcess.spawn=savedSpawn;
    for(const pair of [['DOODLE_ENGINE',savedEnv.engine],['DOODLE_PYTHON',savedEnv.python],['PYTHONPATH',savedEnv.path]]) {
        if(pair[1]===undefined)delete process.env[pair[0]];else process.env[pair[0]]=pair[1];
    }
    remove(local);
}
console.log('Store Python and private runtime dependency launch: passed');

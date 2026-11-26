'use strict';
const assert = require('assert'), fs = require('fs'), os = require('os'), path = require('path'), net = require('net');
const { GpuWorker } = require('../../apps/kxrxh_doodle_jump/gpu');
const { Engine } = require('../../apps/kxrxh_doodle_jump/engine');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function waitFor(test) {
    for (let n=0;n<100;n++) { if (test()) return; await sleep(10); }
    assert.fail('Timed out waiting for the GPU worker');
}
function frame() {
    const body = Buffer.alloc(36);
    body.writeUInt32LE(0x4d520008,0); body.writeUInt32LE(28,4);
    body.writeUInt32LE(408,8); body.writeUInt32LE(720,12); body.writeUInt32LE(1,16);
    [0.1,0.2,0.3,1].forEach((n,i)=>body.writeFloatLE(n,20+i*4));
    return body;
}
async function legacyWorker() {
    const dir=fs.mkdtempSync(path.join(os.tmpdir(),'dj-worker-')), endpoint=path.join(dir,'gpu.sock');
    const previous=process.env.DOODLE_GPU_SOCKET;
    process.env.DOODLE_GPU_SOCKET=endpoint;
    const clients=new Set();
    let connection=0, received=0;
    const server=net.createServer(socket=>{
        clients.add(socket);socket.on('close',()=>clients.delete(socket));socket.on('error',()=>{});
        const index=++connection;
        let data=Buffer.alloc(0);
        if(index===4) { socket.write(Buffer.alloc(4097,65)); return; }
        socket.on('data',chunk=>{
            data=Buffer.concat([data,chunk]);
            if(data.length<44)return;
            assert.deepStrictEqual(data.slice(0,36),frame(),'the GPU stream reaches the worker without reordering');
            assert.strictEqual(data.readUInt32LE(36),0x444a0001);
            assert.strictEqual(data.readUInt32LE(40),0);
            data=Buffer.alloc(0);received++;
            if(index===3) { socket.destroy(); return; }
            const response=JSON.stringify({path:index===2?'/etc/passwd':'/dev/shm/mgu-video-doodle-test',width:408,height:720,sequence:1})+'\n';
            socket.write(response.slice(0,13));setTimeout(()=>socket.write(response.slice(13)),10);
        });
    });
    const engine=new Engine('',{info(){},warn(){},error(){}});
    const worker=engine.worker=new GpuWorker({warn(){}});
    let result=null, errors=0;
    engine.onVideo=value=>{result=value;};
    engine.onGPUError=()=>{errors++;};
    engine.ready=true;
    try {
        await new Promise(resolve=>server.listen(endpoint,resolve));
        worker.connect();await waitFor(()=>worker.ready);
        assert.strictEqual(worker.generation,1);
        engine.frame({gpu:true});engine.packet('GPF1',frame());
        assert.strictEqual(engine.busy,true,'engine waits for GPU completion before requesting another frame');
        await waitFor(()=>result!==null);
        assert.strictEqual(engine.busy,false);
        assert.strictEqual(result.path,'/dev/shm/mgu-video-doodle-test');
        worker.disconnect(new Error('test reconnect'));worker.connect();await waitFor(()=>worker.ready);
        assert.strictEqual(worker.generation,2,'reconnecting changes the texture generation');
        engine.frame({gpu:true});engine.packet('GPF1',frame());
        await waitFor(()=>errors===1);
        assert.strictEqual(engine.busy,false,'malformed acknowledgement releases the engine');
        assert.strictEqual(engine.dead,false,'GPU failure preserves the CPU engine');
        assert.strictEqual(worker.ready,false);
        worker.connect();await waitFor(()=>worker.ready);
        engine.frame({gpu:true});engine.packet('GPF1',frame());
        await waitFor(()=>errors===2);
        assert.strictEqual(engine.busy,false,'disconnecting during rendering releases the engine');
        assert(engine.frame({gpu:false}),'CPU rendering can resume immediately');
        engine.busy=false;
        worker.connect();await waitFor(()=>connection===4 && !worker.ready);
        assert(/large/.test(worker.lastError));
        assert.strictEqual(received,3);
        assert.strictEqual(engine.dead,false);
        console.log('GPU worker framing, completion, reconnect and CPU fallback: passed');
    } finally {
        engine.stop();clients.forEach(socket=>socket.destroy());
        await new Promise(resolve=>server.close(resolve));
        if(fs.existsSync(endpoint))fs.unlinkSync(endpoint);fs.rmdirSync(dir);
        if(previous===undefined)delete process.env.DOODLE_GPU_SOCKET;else process.env.DOODLE_GPU_SOCKET=previous;
    }

}

async function nativeWorker() {
    const dir=fs.mkdtempSync(path.join(os.tmpdir(),'dj-native-')), endpoint=path.join(dir,'gpu.sock');
    const previous=process.env.DOODLE_GPU_SOCKET;
    process.env.DOODLE_GPU_SOCKET=endpoint;
    const packets=[];
    let peer, sequence=0, native=true, presented=true, malformed=false;
    const server=net.createServer(socket=>{
        peer=socket;
        socket.on('error',()=>{});
        let input=Buffer.alloc(0);
        socket.on('data',chunk=>{
            input=Buffer.concat([input,chunk]);
            while(input.length>=8 && input.length>=8+input.readUInt32LE(4)) {
                const kind=input.readUInt32LE(0),size=input.readUInt32LE(4),body=Buffer.from(input.slice(8,8+size));
                input=input.slice(8+size);packets.push({kind,body});
                if(kind===0x444a0002) {
                    assert.deepStrictEqual([0,4,8,12].map(at=>body.readInt32LE(at)),[-9000,-10000,1920,720]);
                    assert.strictEqual(body.toString('utf8',16),'/run/mgu-render/render.sock\0');
                    const reply=JSON.stringify({event:'bind',native})+'\n';
                    socket.write(reply.slice(0,5));setTimeout(()=>socket.write(reply.slice(5)),5);
                }
                if(kind===0x444a0001) {
                    const reply=native ? {mode:'native',width:408,height:720,sequence:++sequence,presented,timestamp:1234} :
                        {mode:'video',path:'/dev/shm/mgu-video-doodle-new',width:408,height:720,sequence:++sequence};
                    if(malformed)delete reply.presented;
                    socket.write(JSON.stringify(reply)+'\n');
                }
            }
        });
    });
    const engine=new Engine('',{info(){},warn(){},error(){}}), worker=engine.worker=new GpuWorker({warn(){}});
    engine.ready=true;
    const replies=[],errors=[];
    engine.onVideo=reply=>replies.push(reply);
    engine.onGPUError=error=>errors.push(error);
    try {
        await new Promise(resolve=>server.listen(endpoint,resolve));
        const cursor=Buffer.alloc(16);
        cursor.writeUInt32LE(20,0);cursor.writeUInt32LE(1,4);cursor.writeUInt32LE(1,8);
        Buffer.from([255,0,0,255]).copy(cursor,12);
        engine.packet('TEX1',cursor);cursor.fill(0,12);
        worker.cursor(180,295,1,2,true);
        worker.connect();await waitFor(()=>worker.ready);
        async function bind() {
            let result;
            assert(worker.bind('/run/mgu-render/render.sock',[-9000,-10000,1920,720],(error,reply)=>{assert.ifError(error);result=reply;}));
            assert(worker.binding);
            await waitFor(()=>result);
            assert.strictEqual(worker.binding,false);
            return result;
        }
        assert.strictEqual((await bind()).native,true);
        await waitFor(()=>packets.some(packet=>packet.kind===0x444a0004));
        const image=packets.find(packet=>packet.kind===0x444a0004).body;
        assert.deepStrictEqual(Array.from(image.slice(8)),[255,0,0,255],'cached cursor owns its premultiplied pixels');
        worker.view([756,0,408,720],true);worker.view([756,0,408,720],true);
        engine.frame({gpu:true});engine.packet('GPF1',frame());
        await waitFor(()=>replies.length===1);
        assert.strictEqual(replies[0].mode,'native');
        assert.strictEqual(engine.busy,false);
        assert.strictEqual(packets.filter(packet=>packet.kind===0x444a0003&&packet.body.readInt32LE(16)===1).length,1,'unchanged native view is sent once');
        presented=false;
        worker.view([756,0,408,720],false);
        engine.frame({gpu:true});engine.packet('GPF1',frame());await waitFor(()=>replies.length===2);
        assert.strictEqual(replies[1].presented,false,'a hidden frame releases the engine without claiming presentation');
        native=false;assert.strictEqual((await bind()).native,false);
        assert.strictEqual(worker.ready,true,'unsupported native presentation preserves video fallback');
        engine.frame({gpu:true});engine.packet('GPF1',frame());await waitFor(()=>replies.length===3);
        assert.strictEqual(replies[2].path,'/dev/shm/mgu-video-doodle-new');
        native=true;presented=true;await bind();
        engine.frame({gpu:true});engine.packet('GPF1',frame());await waitFor(()=>replies.length===4);
        assert.strictEqual(replies[3].sequence,4,'binding preserves completion order across presentation modes');
        malformed=true;
        engine.frame({gpu:true});engine.packet('GPF1',frame());await waitFor(()=>errors.length===1);
        assert.strictEqual(engine.busy,false);
        assert.strictEqual(worker.ready,false,'malformed native acknowledgement disconnects the worker');
        console.log('Native worker binding, cursor, completion and video fallback: passed');
    } finally {
        engine.stop();if(peer)peer.destroy();
        await new Promise(resolve=>server.close(resolve));
        if(fs.existsSync(endpoint))fs.unlinkSync(endpoint);fs.rmdirSync(dir);
        if(previous===undefined)delete process.env.DOODLE_GPU_SOCKET;else process.env.DOODLE_GPU_SOCKET=previous;
    }
}

(async()=>{await legacyWorker();await nativeWorker();})().catch(error=>{console.error(error);process.exitCode=1;});

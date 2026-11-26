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
(async()=>{
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
})().catch(error=>{console.error(error);process.exitCode=1;});

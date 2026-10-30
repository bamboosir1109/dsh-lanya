// Test-only VM boundary. The replay module body is real; the host is synthetic.
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import vm from 'node:vm';
import os from 'node:os';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';

const [codeRoot,dataRoot,kind]=process.argv.slice(2);
const runtime=path.join(dataRoot,'runtime'),inputs=path.join(dataRoot,'inputs'),outputs=path.join(dataRoot,'outputs'),production=path.join(dataRoot,'production');
for(const dir of [runtime,inputs,outputs,production])fs.mkdirSync(dir,{recursive:true});
for(const file of ['policy.json','consumer.sqlite','session.json','memory.sqlite'])fs.writeFileSync(path.join(production,file),'synthetic production sentinel '+file);
const sha=raw=>createHash('sha256').update(raw).digest('hex');
const sentinels=()=>Object.fromEntries(fs.readdirSync(production).map(name=>[name,sha(fs.readFileSync(path.join(production,name)))]));
const before=sentinels();
const codeNames=['bridge.js','replay.js','inbound.js','lanya-policy.config.json'];
const sourceBefore=Object.fromEntries(codeNames.map(name=>[name,sha(fs.readFileSync(path.join(codeRoot,'lib',name)))]));
const counters={deniedDataAccess:0,configReads:0,consumerAccesses:0,networkAttempts:0,processAttempts:0,tempPolicyCommits:0};
const within=(root,target)=>{const rel=path.relative(root,target);return rel===''||rel!== '..'&&!rel.startsWith('..'+path.sep)&&!path.isAbsolute(rel)};
const fds=new Set(),loaded=new Map(),timers=new Set();
function denied(field){counters[field]++;throw Object.assign(new Error('Synthetic replay boundary denied'),{code:'FIXTURE_BOUNDARY'})}
function check(file,write=false,metadata=false){
    if(typeof file==='number'){if(!fds.has(file))denied('deniedDataAccess');return;}
    const target=path.resolve(file instanceof URL?fileURLToPath(file):String(file));
    if(path.basename(target)==='lanya-policy.config.json')counters.configReads++;
    if(path.basename(target)==='consumer.sqlite')counters.consumerAccesses++;
    if(within(runtime,target)||within(outputs,target))return;
    if(!write&&within(inputs,target))return;
    if(!write&&metadata&&[runtime,inputs,outputs,codeRoot].some(root=>within(target,root)))return;
    if(!write&&within(codeRoot,target)&&(/\.(?:js|mjs|cjs)$/.test(target)||path.basename(target)==='package.json'))return;
    denied('deniedDataAccess');
}
function guardedFs(source,promises=false){
    const result={...source};
    const writes=new Set(['writeFile','writeFileSync','appendFile','appendFileSync','mkdir','mkdirSync','mkdtemp','mkdtempSync','rm','rmSync','unlink','unlinkSync','rename','renameSync','link','linkSync','copyFile','copyFileSync']);
    const metadata=new Set(['existsSync','stat','statSync','lstat','lstatSync','realpath','realpathSync','access','accessSync']);
    for(const name of new Set([...writes,...metadata,'readFile','readFileSync','readdir','readdirSync','open','openSync','writeSync','readSync','fsyncSync','closeSync'])){
        if(typeof source[name]!=='function')continue;
        result[name]=(file,...args)=>{
            const write=name==='open'||name==='openSync'?typeof args[0]==='number'?Boolean(args[0]&3):/[wax+]/.test(String(args[0])):writes.has(name);
            check(file,write,metadata.has(name));
            if(['rename','renameSync','link','linkSync','copyFile','copyFileSync'].includes(name)){
                check(args[0],true);if(String(args[0]).endsWith('decision-policy.json'))counters.tempPolicyCommits++;
            }
            const value=Reflect.apply(source[name],source,[file,...args]);
            if(name==='openSync')fds.add(value);
            if(name==='closeSync')fds.delete(file);
            return value;
        };
    }
    if(!promises)result.promises=guardedFs(fsp,true);
    result.default=result;return result;
}
class Context {on(){return()=>{}}}
const context=vm.createContext({Buffer,URL,URLSearchParams,TextEncoder,TextDecoder,structuredClone,AbortController,
    process:{platform:process.platform,pid:process.pid,env:{},versions:process.versions,cwd:()=>runtime,kill:(pid,signal)=>{assert.equal(signal,0);return process.kill(pid,0)}},
    console:{log(){},warn(){},error(){}},fetch:()=>denied('networkAttempts'),
    setTimeout:(...args)=>{const timer=setTimeout(...args);timers.add(timer);return timer},clearTimeout:timer=>{timers.delete(timer);clearTimeout(timer)},
    setInterval:(...args)=>{const timer=setInterval(...args);timers.add(timer);return timer},clearInterval:timer=>{timers.delete(timer);clearInterval(timer)},setImmediate,clearImmediate});
async function moduleFor(specifier,parent){
    let key=specifier;
    if(specifier.startsWith('.'))key=new URL(specifier,parent).href;
    if(loaded.has(key))return loaded.get(key);
    let exports;
    if(key==='@deepseek-ai/cordis')exports={Context};
    else if(key==='node:fs')exports=guardedFs(fs);
    else if(key==='node:fs/promises')exports=guardedFs(fsp,true);
    else if(key==='node:os')exports={...os,tmpdir:()=>runtime,default:{...os,tmpdir:()=>runtime}};
    else if(key==='node:child_process')exports={execFile:()=>denied('processAttempts'),spawn:()=>denied('processAttempts'),exec:()=>denied('processAttempts'),fork:()=>denied('processAttempts')};
    else if(/^node:(?:http|https|net|tls|dns|dgram)/.test(key))throw new Error('Network module is forbidden in this synthetic fixture');
    else if(key.startsWith('node:'))exports=await import(key);
    if(exports){
        const mod=new vm.SyntheticModule(Object.keys(exports),function(){for(const[name,value]of Object.entries(exports))this.setExport(name,value)},{context,identifier:key});loaded.set(key,mod);return mod;
    }
    if(!key.startsWith('file:'))throw new Error('Unmocked external contract');
    const file=fileURLToPath(key);assert.ok(within(codeRoot,file));
    let source=fs.readFileSync(file,'utf8');
    if(path.basename(file)==='replay.js'){
        // Unique import-only host/registry seam. No replay function body is mirrored.
        assert.equal(source.split("from './bridge.js'").length,2);
        source=source.replace("from './bridge.js'","from './bridge-host-seam.js'");
    }
    const mod=new vm.SourceTextModule(source,{context,identifier:key,initializeImportMeta(meta){meta.url=key}});
    loaded.set(key,mod);return mod;
}
const linker=(specifier,reference)=>moduleFor(specifier,reference.identifier);
try{
    const root=await moduleFor(pathToFileURL(path.join(codeRoot,'lib/replay.js')).href);
    await root.link(linker);await root.evaluate();
    const input=path.join(inputs,'recording.jsonl');
    fs.writeFileSync(input,JSON.stringify({message_type:'private',user_id:101,message_id:'fixture-one',text:'fixed synthetic replay text'})+'\n');
    let result,error;
    try{result=await root.namespace.replayInbox({inboxFile:input,mediaDir:production,traceDir:production,
        ...(kind==='output'?{outFile:path.join(outputs,'explicit-report.json')}:{}),
        log:kind==='throw'?()=>{throw new Error('synthetic log failure')}:()=>{}})}catch(value){error=value}
    await new Promise(resolve=>setTimeout(resolve,30));
    if(kind==='throw')assert.ok(error);else{
        assert.ifError(error);assert.equal(result.recordingAgentFollowups,1);assert.equal(result.modelCalled,false);assert.equal(result.isolated,true);
        assert.ok(counters.tempPolicyCommits>0);
        if(kind==='output')assert.equal(JSON.parse(fs.readFileSync(path.join(outputs,'explicit-report.json'),'utf8')).recordingAgentFollowups,1);
        await assert.rejects(context.replayFixtureBridge.deps.media.downloadUrl('https://example.invalid/a.png'),/disabled/);
        const adapter=context.replayFixtureBridge.lanyaPolicy;
        const injected={chatId:'private:101',userId:'101',messageId:'fixed-injected',text:'fixed injected control',lanyaInjected:true};
        const answer=await adapter.processInbound(injected,()=>adapter.observeInbound(injected));
        assert.equal(answer.accepted,false);assert.equal(answer.reason,'not_live');
    }
    assert.equal(fs.readdirSync(runtime).length,0,'owned replay root was removed');
    assert.deepEqual(sentinels(),before);
    for(const[name,digest]of Object.entries(sourceBefore))assert.equal(sha(fs.readFileSync(path.join(codeRoot,'lib',name))),digest);
    for(const key of ['deniedDataAccess','configReads','consumerAccesses','networkAttempts','processAttempts'])assert.equal(counters[key],0,key);
    for(const timer of timers)clearTimeout(timer);
    console.log(JSON.stringify({success:true,kind,followups:result?.recordingAgentFollowups??0,syntheticSentinels:4,sourceUnchanged:true,cleaned:true,...counters}));
}catch(error){for(const timer of timers)clearTimeout(timer);console.log(JSON.stringify({success:false,error_class:error.constructor.name,message:String(error.message)}));process.exitCode=1}

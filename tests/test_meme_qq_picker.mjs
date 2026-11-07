// Published native store/tool + actual current bridge method, temporary images.
import { after, before, test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync, readdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { execFileSync } from 'node:child_process'
import { DatabaseSync } from 'node:sqlite'
import { getQqCapability } from '../plugin/lib/meme-qq-picker.mjs'
import { OneBotDecisionAdapter } from '../plugin/lib/onebot-policy-adapter.js'

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '..')
let root, native, indexText, bridgeText, Context
const png = Buffer.from('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000b49444154789c636000020000050001a5f645400000000049454e44ae426082', 'hex')
before(async () => {
  root = mkdtempSync(join(tmpdir(), 'lanya-meme-native-'))
  const code = `import sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path(sys.argv[1])/'scripts'))\nimport patch_meme_qq_picker as p\nf=Path(sys.argv[1])/'tests/fixtures/meme_qq_picker';out=Path(sys.argv[2])\nfor name in ('index.js','memes.js','bridge.js'): (out/name).write_bytes(p.transform(name,(f/name).read_bytes()))\n(out/p.MODULE).write_bytes(p.SOURCE.read_bytes())`
  execFileSync(process.env.PYTHON || 'python', ['-B', '-c', code, REPO, root])
  writeFileSync(join(root,'package.json'),' {"type":"module"}')
  const sdk=join(root,'node_modules/@deepseek-ai/dsh-tools');mkdirSync(sdk,{recursive:true})
  writeFileSync(join(sdk,'package.json'),'{"type":"module","exports":"./index.js"}')
  writeFileSync(join(sdk,'index.js'),'export const defineTool = value => value\n')
  for (const name of ['cordis','cosmokit']) {
    const target=join(root,'node_modules/@deepseek-ai',name);mkdirSync(target,{recursive:true})
    writeFileSync(join(target,'package.json'),JSON.stringify({type:'module',exports:'./index.js'}))
    writeFileSync(join(target,'index.js'),readFileSync(join(REPO,'tests/fixtures/meme_qq_picker/sdk',name,'index.js')))
  }
  ;({Context}=await import(pathToFileURL(join(root,'node_modules/@deepseek-ai/cordis/index.js'))))
  native = await import(pathToFileURL(join(root,'memes.js')))
  indexText=readFileSync(join(root,'index.js'),'utf8').replaceAll('\r\n','\n')
  bridgeText=readFileSync(join(root,'bridge.js'),'utf8').replaceAll('\r\n','\n')
})
after(() => rmSync(root,{recursive:true,force:true}))

function pack(t, label, caption='') {
  const directory=join(root,label);mkdirSync(directory)
  mkdirSync(join(directory,'stolen'))
  writeFileSync(join(directory,'stolen/real.png'),png)
  const db=new DatabaseSync(join(directory,'index.db'))
  db.exec('CREATE TABLE memes(path TEXT PRIMARY KEY,tag TEXT,file_name TEXT,caption TEXT,keywords TEXT,mtime REAL,captioned_at REAL)')
  db.prepare('INSERT INTO memes VALUES(?,?,?,?,?,?,?)').run('stolen/real.png','happy','real.png',caption,'猫 开心',1,null)
  db.close()
  const store=native.liveStore(new native.MemesStore(directory));t.after(()=>store._s.close())
  return {directory,store}
}

function sampler(store,settings,packs) {
  const start=indexText.indexOf('  const packRows = () => {')
  const end=indexText.indexOf('  // ---- 发送通道:',start)
  assert.ok(start>=0 && end>start)
  return Function('readSettings','listAllPacks','MemesStore','memes','resolve','join','searchRows','enabledPackIds',
    indexText.slice(start,end)+'\nreturn sampler')(()=>settings,()=>packs,native.MemesStore,store,resolve,join,native.searchRows,native.enabledPackIds)
}

function bound(t, label, { live = false } = {}) {
  const mediaDir=join(root,label);mkdirSync(mediaDir)
  let current=true
  const record={}
  if (live) record.binding = { origin: 'live' }
  const exec={agent:{session:{id:'synthetic-session'}},signal:new AbortController().signal}
  const services=new Map()
  exec.agent.ctx={get:(key)=>services.get(key)}
  const bridge={deps:{config:{mediaDir,maxImageBytes:8*1024*1024}},lanyaPolicy:{
    captureExec(value){if(!current || value!==exec) throw new Error('not current');return record},
    memeSelectionPolicy: OneBotDecisionAdapter.prototype.memeSelectionPolicy,
    connectionFacade(){return {}}
  }}
  const start=bridgeText.indexOf('    installChannelScope(agentCtx) {')
  const end=bridgeText.indexOf('    // ------------------------------------------------------------ typing',start)
  const method=Function('registerTools','buildPlatformPrompt','return ({'+bridgeText.slice(start,end)+'}).installChannelScope')(()=>{},()=> '')
  method.call(bridge,{reflect:{provide:(key,value)=>services.set(key,value)},systemPrompt:{section(){}}})
  return {exec,services,mediaDir,end(){current=false}}
}

test('real Cordis isolates two existing agent contexts, inherits tools, and disposes independently',async t=>{
  const host=new Context()
  t.after(async()=>{await host.fiber.dispose()})
  const tools={sentinel:'existing-tool-scope'}
  const parent=host.isolate('tools')
  parent.reflect.provide('tools',tools)
  const fibers=[parent.plugin(()=>{}),parent.plugin(()=>{}),parent.plugin(()=>{})]
  await Promise.all(fibers.map(fiber=>fiber.await()))
  // Same native createScope pattern: fiber.ctx.extend({scope-tag}); no invented
  // per-service isolation is supplied by these context creations.
  const contexts=fibers.map((fiber,index)=>fiber.ctx.extend({[Symbol('scope')]:index,systemPrompt:{section(){}}}))
  const agents=contexts.slice(0,2).map(ctx=>({ctx,session:{id:'synthetic-session'}}))
  const execs=agents.map(agent=>({agent,signal:new AbortController().signal}))
  const records=new Map(execs.map(exec=>[exec,{}]))
  const beforeMaps=contexts.map(ctx=>ctx[Context.isolate])
  const hostMap=host[Context.isolate]
  const parentMap=parent[Context.isolate]
  const configs=execs.map((_,index)=>{
    const mediaDir=join(root,'sdk-media-'+index);mkdirSync(mediaDir);return {mediaDir,maxImageBytes:8*1024*1024}
  })
  const start=bridgeText.indexOf('    installChannelScope(agentCtx) {')
  const end=bridgeText.indexOf('    // ------------------------------------------------------------ typing',start)
  const install=Function('registerTools','buildPlatformPrompt','return ({'+bridgeText.slice(start,end)+'}).installChannelScope')(()=>{},()=> '')
  for (let index=0;index<2;index++) {
    const ctx=contexts[index]
    const bridge={deps:{config:configs[index]},lanyaPolicy:{connectionFacade(){return {}},captureExec(exec){
      if(exec!==execs[index] || !records.has(exec))throw new Error('foreign or ended')
      return records.get(exec)
    }}}
    install.call(bridge,ctx)
    assert.equal(agents[index].ctx,ctx)
    assert.equal(Object.getPrototypeOf(ctx[Context.isolate]),beforeMaps[index])
    assert.deepEqual(Object.keys(ctx[Context.isolate]),['lanyaQqMemeMedia'])
    assert.equal(ctx.get('tools',false),tools)
  }
  assert.equal(host[Context.isolate],hostMap);assert.equal(parent[Context.isolate],parentMap)
  assert.equal(host.get('lanyaQqMemeMedia',false),undefined)
  assert.equal(contexts[2].get('lanyaQqMemeMedia',false),undefined)
  const first=getQqCapability(execs[0]), second=getQqCapability(execs[1])
  assert.equal(first.mediaDir,configs[0].mediaDir);assert.equal(second.mediaDir,configs[1].mediaDir)
  assert.notEqual(contexts[0].get('lanyaQqMemeMedia',false),contexts[1].get('lanyaQqMemeMedia',false))
  assert.equal(contexts[0].extend().get('lanyaQqMemeMedia',false),contexts[0].get('lanyaQqMemeMedia',false))
  assert.throws(()=>contexts[0].get('lanyaQqMemeMedia',false).forExecution(execs[1]))
  assert.throws(()=>getQqCapability({...execs[0],agent:{...agents[0]}}))
  records.delete(execs[0]);assert.throws(()=>first.assertCurrent())
  await fibers[0].dispose();await fibers[0].dispose()
  assert.equal(contexts[0].get('lanyaQqMemeMedia',false),undefined)
  assert.equal(getQqCapability(execs[1]).mediaDir,configs[1].mediaDir)
  second.assertCurrent()
  assert.equal(contexts[1].get('tools',false),tools)
  assert.equal(host.get('lanyaQqMemeMedia',false),undefined)
  assert.equal(contexts[2].get('lanyaQqMemeMedia',false),undefined)
})

function tool(memes) {
  let value
  native.registerSendMemeTool({tools:{register(v){value=v}}},memes,null,'http://web.invalid/dsh-memes')
  return value
}

function fastTool(memes) {
  const tools = new Map()
  native.registerSendMemeTool({tools:{register(value){tools.set(value.name,value)}}},memes,null,'http://web.invalid/dsh-memes')
  return tools.get('qq_send_meme')
}

test('one or two indexed labels send one blank-caption reaction without a vision or model call', async t => {
  const p=pack(t,'fast-tag'),scope=bound(t,'fast-tag-media',{live:true}),sent=[]
  const old=scope.services.get('lanyaQqMemeMedia')
  scope.services.set('lanyaQqMemeMedia',{forExecution(exec){return {...old.forExecution(exec),
    sendCandidate:async(candidate,textFollows)=>{sent.push({candidate,textFollows});return {ok:true,sent:true}}}}})
  const tool=fastTool(sampler(p.store,{packId:'_custom',enabledPacks:['_custom']},[{id:'_custom',path:p.directory}]))
  assert.equal((await tool.execute({tags:['happy','开心']},scope.exec)).sent,true)
  assert.equal(sent.length,1);assert.equal(sent[0].candidate.caption,'');assert.equal(sent[0].candidate.tag,'happy')
  assert.equal(sent[0].textFollows,false);assert.equal(readdirSync(scope.mediaDir).length,1)
  assert.equal((await tool.execute({tags:['happy']},scope.exec)).sent,false)
  assert.equal(sent.length,1)
})

test('tag tool refuses missing semantic metadata, invented subjects, foreign scope and arbitrary path',async t=>{
  const p=pack(t,'fast-tag-refuse'),scope=bound(t,'fast-tag-refuse-media'),sent=[]
  const old=scope.services.get('lanyaQqMemeMedia')
  scope.services.set('lanyaQqMemeMedia',{forExecution(exec){return {...old.forExecution(exec),sendCandidate:async()=>{sent.push(1);return {sent:true}}}}})
  const tool=fastTool(sampler(p.store,{packId:'_custom',enabledPacks:['_custom']},[{id:'_custom',path:p.directory}]))
  assert.equal((await tool.execute({tags:['不存在的主题']},scope.exec)).sent,false)
  await assert.rejects(tool.execute({tags:['happy'],path:'arbitrary'},scope.exec))
  await assert.rejects(tool.execute({tags:[]},scope.exec))
  await assert.rejects(tool.execute({tags:['a','b','c']},scope.exec))
  await assert.rejects(tool.execute({tags:['happy']},{...scope.exec,agent:{...scope.exec.agent}}))
  scope.end();await assert.rejects(tool.execute({tags:['happy']},scope.exec))
  assert.equal(sent.length,0)
})

test('actual Web branch keeps caption placeholder and no stage/service side effect',t=>{
  const p=pack(t,'web','synthetic caption');const picker=tool(p.store)
  const output=picker.execute({query:'猫'}, {agent:{ctx:{get(){return undefined}}}})
  assert.equal(output.mode,'keyword');assert.match(output.message,/\[表情: synthetic caption\]/)
  assert.deepEqual(output.hits,[{caption:'synthetic caption'}])
})

test('actual scoped native register returns real path in rendered message, no auto send',t=>{
  const p=pack(t,'qq');const scope=bound(t,'media1')
  const memes=sampler(p.store,{packId:'_custom',enabledPacks:['_custom']},[{id:'_custom',path:p.directory}])
  const picker=tool(memes);const output=picker.execute({query:'猫'},scope.exec)
  assert.equal(output.mode,'qq_selection');assert.equal(output.delivered,false);assert.equal(output.selected,true)
  assert.equal(output.candidates[0].caption,'');assert.equal(output.candidates[0].requires_visual_review,true)
  assert.deepEqual(readFileSync(output.candidates[0].path),png)
  const rendered=picker.output.render({},output)[0].text
  const visible=JSON.parse(rendered)
  assert.equal(visible.ok,true);assert.equal(visible.mode,'qq_selection')
  assert.equal(visible.selected,true);assert.equal(visible.delivered,false)
  assert.deepEqual(visible.candidates,output.candidates)
  assert.deepEqual(visible.next_steps.inspect,[{candidate_sha256:output.candidates[0].sha256,
    tool:'read_image',arguments:{file_path:output.candidates[0].path}}])
  assert.equal(visible.next_steps.reply_permission.reuse_current_valid_reply_ticket,true)
  assert.deepEqual(visible.next_steps.reply_permission.only_if_this_turn_is_undecided,
    {tool:'qq_decide',arguments:{verdict:'reply'}})
  assert.equal(visible.next_steps.reply_permission.if_this_turn_has_a_non_reply_decision,'skip_qq_send_without_deciding_again')
  assert.deepEqual(visible.next_steps.send[0].arguments,{sources:[output.candidates[0].path]})
  assert.equal(visible.next_steps.send[0].tool,'qq_send_image')
  assert.equal(visible.next_steps.send[0].when,'only_if_this_candidate_is_chosen')
  assert.match(visible.summary,/不要再次qq_decide/)
  assert.doesNotMatch(output.message,/\[表情:/)
  assert.match(picker.description,/仅实际Web结果且mode不是qq_selection/)
  assert.doesNotMatch(picker.description,/发完短接|发完保持简短/)
})

test('native custom root resolution and multi-pack lookup require exact enabled index row',t=>{
  const p=pack(t,'active');const other=pack(t,'other','other caption')
  const settings={packId:'_custom',memeRoot:p.directory,packsDir:join(root,'absent-packs'),enabledPacks:['_custom','other']}
  assert.equal(native.resolveActiveRoot(settings),p.directory)
  const match=indexText.match(/  const listAllPacks = \(\) => \{[\s\S]*?\n  \}/)
  assert.ok(match)
  const listed=Function('scanPacks','packsDirNow','memes','resolve','readPackMeta','readSettings',
    match[0]+'\nreturn listAllPacks()')(()=>[],()=>'',p.store,resolve,native.readPackMeta,()=>settings)
  assert.equal(listed[0].id,'_custom');assert.equal(listed[0].path,p.directory)
  const m=sampler(p.store,settings,[{id:'_custom',path:p.directory},{id:'other',path:other.directory}])
  assert.equal(m.resolveIndexed('other/stolen/real.png').file,join(other.directory,'stolen/real.png'))
  assert.throws(()=>m.resolveIndexed('other/stolen/not-indexed.png'))
  settings.enabledPacks=['_custom'];assert.throws(()=>m.resolveIndexed('other/stolen/real.png'))
})

test('missing genuine image returns no usable image instead of placeholder',t=>{
  const p=pack(t,'missing');const scope=bound(t,'media2')
  rmSync(join(p.directory,'stolen/real.png'))
  const m=sampler(p.store,{packId:'_custom',enabledPacks:['_custom']},[{id:'_custom',path:p.directory}])
  const output=tool(m).execute({query:'猫'},scope.exec)
  assert.equal(output.ok,false);assert.equal(output.reason,'no_usable_images');assert.equal(output.delivered,false)
  assert.deepEqual(output.candidates,[]);assert.equal(readdirSync(scope.mediaDir).length,0)
  const visible=JSON.parse(tool(m).output.render({},output)[0].text)
  assert.equal(visible.ok,false);assert.equal(visible.mode,'qq_selection')
  assert.equal(visible.selected,false);assert.equal(visible.delivered,false)
  assert.equal(visible.reason,'no_usable_images');assert.deepEqual(visible.next_steps,{choice:'skip_image'})
})

test('visual-review flag describes the returned caption rather than text outside its bounded excerpt',t=>{
  const p=pack(t,'caption-window',' '.repeat(200)+'outside visible caption')
  const scope=bound(t,'caption-window-media')
  const m=sampler(p.store,{packId:'_custom',enabledPacks:['_custom']},[{id:'_custom',path:p.directory}])
  const output=tool(m).execute({tag:'happy',limit:1},scope.exec)
  const visible=JSON.parse(tool(m).output.render({},output)[0].text)
  assert.equal(visible.candidates[0].caption,'')
  assert.equal(visible.candidates[0].requires_visual_review,true)
  assert.equal(visible.next_steps.inspect[0].tool,'read_image')
})

test('capability capture rejects foreign exec and stale turn without Web fallback',t=>{
  const p=pack(t,'stale');const scope=bound(t,'media3');const picker=tool(p.store)
  assert.throws(()=>picker.execute({query:'猫'},{...scope.exec,agent:{...scope.exec.agent}}))
  scope.end();assert.throws(()=>picker.execute({query:'猫'},scope.exec))
  assert.equal(readdirSync(scope.mediaDir).length,0)
})

test('present malformed service or forged cap never becomes Web selection',t=>{
  const p=pack(t,'badcap');const scope=bound(t,'media4');const picker=tool(p.store)
  scope.services.set('lanyaQqMemeMedia',{forExecution(){return {format:'lanya-qq-meme-media/v1',mediaDir:scope.mediaDir,maxBytes:true,assertCurrent(){}}}})
  assert.throws(()=>picker.execute({query:'猫'},scope.exec));assert.equal(readdirSync(scope.mediaDir).length,0)
})

test('QQ caller cannot select arbitrary path or request legacy companion send',t=>{
  const p=pack(t,'params');const scope=bound(t,'media5')
  const picker=tool(p.store)
  assert.throws(()=>picker.execute({action:'send',path:join(p.directory,'stolen/real.png')},scope.exec))
  assert.throws(()=>picker.execute({query:'猫',limit:1.5},scope.exec))
  assert.equal(getQqCapability(scope.exec).mediaDir,scope.mediaDir)
  assert.equal(readdirSync(scope.mediaDir).length,0)
})

test('a live greeting asking for four candidates stages one and cannot turn into repeated image hunting', t => {
  const p=pack(t,'live-greeting'), scope=bound(t,'live-greeting-media',{live:true})
  const db=new DatabaseSync(join(p.directory,'index.db'))
  for (let i=1;i<=3;i++) {
    const name='stolen/choice-'+i+'.png'
    writeFileSync(join(p.directory,name),Buffer.concat([png,Buffer.from([i])]))
    db.prepare('INSERT INTO memes VALUES(?,?,?,?,?,?,?)').run(name,'happy',name,'','猫 开心',i+1,null)
  }
  db.close()
  const m=sampler(p.store,{packId:'_custom',enabledPacks:['_custom']},[{id:'_custom',path:p.directory}])
  let searches=0
  const original=m.search.bind(m);m.search=(...args)=>{searches++;return original(...args)}
  const picker=tool(m),output=picker.execute({query:'猫',limit:4},scope.exec)
  const visible=JSON.parse(picker.output.render({},output)[0].text)
  assert.equal(visible.candidates.length,1)
  assert.equal(visible.next_steps.inspect.length,1)
  assert.equal(visible.live_selection.requested_limit,4)
  assert.equal(visible.live_selection.remaining_searches,0)
  assert.equal(visible.delivered,false)
  assert.equal(readdirSync(scope.mediaDir).length,1)
  assert.equal(searches,1)
  const again=picker.execute({query:'猫',limit:20},scope.exec)
  assert.equal(again.reason,'live_selection_already_used')
  assert.equal(again.selected,false)
  assert.equal(again.delivered,false)
  assert.deepEqual(again.candidates,[])
  assert.equal(searches,1)
  assert.equal(readdirSync(scope.mediaDir).length,1)
  assert.match(again.message,/reuse_previous_candidate_or_reply_with_text/)
})

test('live selection is per claimed turn and a caption does not require a second vision call', t => {
  const p=pack(t,'live-caption','a clear fixture reaction')
  const first=bound(t,'live-caption-first',{live:true}), second=bound(t,'live-caption-second',{live:true})
  const m=sampler(p.store,{packId:'_custom',enabledPacks:['_custom']},[{id:'_custom',path:p.directory}])
  const picker=tool(m)
  for (const scope of [first,second]) {
    const output=picker.execute({query:'猫'},scope.exec)
    assert.equal(output.candidates.length,1)
    assert.equal(output.candidates[0].requires_visual_review,false)
    assert.deepEqual(output.next_steps.inspect,[])
    assert.equal(picker.execute({tag:'happy'},scope.exec).selected,false)
  }
  first.end()
  assert.throws(()=>picker.execute({tag:'happy'},first.exec))
})

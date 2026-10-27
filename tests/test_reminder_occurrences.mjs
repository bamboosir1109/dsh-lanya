import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createHash } from 'node:crypto'
import { processReminders, resolveRuntime } from '../plugin/lib/runtime.js'
import { createReminderObserver } from '../plugin/lib/reminder-observer.js'
import { publishObservation, readObservationCompletion } from '../plugin/lib/observation-producer.js'

function fixture(t, date=new Date(2026,9,27,10,0)) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'reminder-occurrence-fixture-'))
  t.after(() => fs.rmSync(directory, {recursive:true, force:true}))
  const inbox = path.join(directory, 'inbox'), file = path.join(directory, 'reminder-state.json')
  let clock = date.getTime()
  const contexts = []
  const makeObserver = () => createReminderObserver({directory:inbox, workspaceKey:directory, agentKey:'', resolveScope:()=>'private:101', now:()=>clock})
  let observer = makeObserver()
  const files = () => fs.existsSync(path.join(inbox, 'pending')) ? fs.readdirSync(path.join(inbox, 'pending')).filter(name=>name.endsWith('.json')) : []
  const event = name => JSON.parse(fs.readFileSync(path.join(inbox, 'pending', name)))
  const complete = (status='decided', next, patch={}) => {
    const name = files().find(name=>!fs.existsSync(path.join(inbox, 'done', name)))
    const raw = fs.readFileSync(path.join(inbox, 'pending', name)), value = JSON.parse(raw)
    const marker = {version:1, status, decision_id:'fixture-decision', scope:value.scope, event_id:value.event_id,
      completed_at:clock, event_sha256:createHash('sha256').update(raw).digest('hex'), ...patch}
    if (next !== undefined) marker.next_review_at = next
    fs.writeFileSync(path.join(inbox, 'done', name), JSON.stringify(marker))
  }
  const persistState = state => {
    fs.writeFileSync(file + '.tmp', JSON.stringify(state)); fs.renameSync(file + '.tmp', file)
  }
  return {directory,inbox,file,contexts,files,event,complete,persistState,
    get clock(){return clock}, set clock(value){clock=value},
    restart(){observer=makeObserver(); return JSON.parse(fs.readFileSync(file))},
    async send(r, ctx){contexts.push(ctx);return observer(r,ctx)},
    async run(list, previous={}) { return processReminders(list, previous, new Date(clock), this.send.bind(this), {persistState}) }
  }
}

test('daily defer beyond initial grace creates exactly one successor using the original occurrence', async t => {
  const f=fixture(t), list=[{daily:'10:00',text:'fixture'}]
  const first=await f.run(list)
  const key=f.contexts[0].key
  f.complete('decided', f.clock+10*60000)
  f.clock+=10*60000
  const second=await f.run(first.keep, f.restart())
  await f.run(second.keep, second.state)
  assert.equal(f.files().length,2)
  assert.ok(f.contexts.every(ctx=>ctx.key===key && ctx.occurrence.day==='2026-10-27'))
  assert.deepEqual(second.state.daily,{})
  assert.equal(Object.keys(second.state.pending).length,1)
})

test('midnight grace uses yesterday occurrence identity and never invents a today reminder', async t => {
  const f=fixture(t,new Date(2026,9,28,0,1)), list=[{daily:'23:59',text:'fixture'}]
  const first=await f.run(list)
  await f.run(first.keep, f.restart())
  assert.equal(f.files().length,1)
  assert.match(f.contexts[0].key,/\|2026-10-27$/)
  assert.equal(f.contexts[0].occurrence.day,'2026-10-27')
  assert.equal(new Date(f.contexts[0].occurrence.due_at).getDate(),27)
})

test('defer across midnight and restart retains the original key beyond both days grace windows', async t => {
  const f=fixture(t,new Date(2026,9,27,23,59)), list=[{daily:'23:59',text:'fixture'}]
  const first=await f.run(list), key=f.contexts[0].key
  f.complete('decided',f.clock+11*60000)
  f.clock+=11*60000
  await f.run(first.keep,f.restart())
  assert.equal(f.files().length,2)
  assert.equal(f.contexts[1].key,key)
  assert.match(key,/\|2026-10-27$/)
  const ids=f.files().map(name=>f.event(name).event_id).sort()
  assert.equal(ids[1].slice(0,-1),ids[0].slice(0,-1))
  assert.deepEqual(ids.map(id=>id.slice(-1)),['0','1'])
})

test('unknown never retries its old occurrence but does not cancel tomorrow daily reminder', async t => {
  const f=fixture(t), list=[{daily:'10:00',text:'fixture'}]
  const first=await f.run(list), key=f.contexts[0].key
  f.complete('unknown',f.clock+60000)
  f.clock+=24*3600000
  const next=await f.run(first.keep,f.restart())
  assert.equal(f.files().length,2)
  assert.deepEqual(next.keep,list)
  assert.deepEqual(next.state.daily,{})
  assert.equal(next.state.outcomes[key].status,'unknown')
  assert.equal(next.state.pending[key],undefined)
  const ids=f.files().map(name=>f.event(name).event_id)
  assert.equal(ids.filter(id=>id.startsWith('reminder:'+key+':')).length,1)
  assert.ok(ids.some(id=>id.includes('|2026-10-28:0')))
})

test('an intent persistence failure prevents dispatch and does not publish evidence', async t => {
  const f=fixture(t)
  await assert.rejects(processReminders([{daily:'10:00',text:'fixture'}],{},new Date(f.clock),f.send.bind(f),{
    persistState(){throw new Error('fixture disk failure')}
  }),/disk failure/)
  assert.equal(f.contexts.length,0)
  assert.equal(f.files().length,0)
})

test('crash after queue publication resumes the same occurrence after restart without duplicate evidence', async t => {
  const f=fixture(t), list=[{daily:'10:00',text:'fixture'}]
  await processReminders(list,{},new Date(f.clock),async(r,ctx)=>{
    await f.send(r,ctx);throw new Error('fixture interruption after publish')
  },{persistState:f.persistState})
  const key=f.contexts[0].key
  f.clock+=24*3600000+10*60000
  const resumed=await f.run(list,f.restart())
  assert.equal(f.files().length,1)
  assert.equal(f.contexts[1].key,key)
  assert.ok(resumed.state.pending[key])
})

test('a late sent completion marks only the original day delivered', async t => {
  const f=fixture(t,new Date(2026,9,27,23,59)), list=[{daily:'23:59',text:'fixture'}]
  const first=await f.run(list), key=f.contexts[0].key
  f.complete('sent');f.clock+=20*60000
  const final=await f.run(first.keep,f.restart())
  assert.equal(final.state.daily[key],'yes')
  assert.equal(Object.keys(final.state.pending).length,0)
  assert.equal(f.files().length,1)
  assert.deepEqual(final.keep,list)
})

test('corrupt pending state rejects all dispatch before treating the occurrence as new', async t => {
  const f=fixture(t), list=[{daily:'10:00',text:'fixture'}]
  const first=await f.run(list), damaged=JSON.parse(JSON.stringify(first.state))
  damaged.pending[Object.keys(damaged.pending)[0]].key='changed'
  f.contexts.length=0
  await assert.rejects(f.run(list,damaged),/Invalid pending/)
  assert.equal(f.contexts.length,0)
  assert.equal(f.files().length,1)
})

for(const status of ['decided','suppressed']) {
  test('yesterday '+status+' without review stays non-delivered and permits today fresh occurrence',async t=>{
    const f=fixture(t),list=[{daily:'10:00',text:'fixture'}]
    const first=await f.run(list),key=f.contexts[0].key
    f.complete(status)
    const settled=await f.run(first.keep,f.restart())
    assert.equal(settled.state.outcomes[key].status,status)
    assert.equal(settled.state.daily[key],undefined)
    assert.equal(Object.keys(settled.state.pending).length,0)
    f.clock+=24*3600000
    const today=await f.run(settled.keep,f.restart())
    await f.run(today.keep,today.state)
    assert.equal(f.files().length,2)
    assert.ok(Object.keys(today.state.pending).some(key=>key.endsWith('|2026-10-28')))
    assert.equal(Object.keys(today.state.outcomes).length,1)
  })
}

test('old deferred review coexists with a new daily occurrence and both retain their own keys after restart',async t=>{
  const f=fixture(t),list=[{daily:'10:00',text:'fixture'}]
  const first=await f.run(list),oldKey=f.contexts[0].key
  const review=f.clock+24*3600000+10*60000
  f.complete('decided',review)
  f.clock+=24*3600000
  const day2=await f.run(first.keep,f.restart())
  assert.equal(f.files().length,2)
  assert.equal(Object.keys(day2.state.pending).length,2)
  assert.ok(day2.state.pending[oldKey])
  f.clock=review
  const afterReview=await f.run(day2.keep,f.restart())
  assert.equal(f.files().length,3)
  assert.equal(Object.keys(afterReview.state.pending).length,2)
  const oldEvents=f.files().map(name=>f.event(name).event_id).filter(id=>id.startsWith('reminder:'+oldKey+':')).sort()
  assert.deepEqual(oldEvents.map(id=>id.slice(-1)),['0','1'])
  assert.ok(f.contexts.some(ctx=>ctx.key.endsWith('|2026-10-28')))
})

test('settled one-off idle remains on the list and is not woken on every tick',async t=>{
  const f=fixture(t),list=[{at:new Date(f.clock-1000).toISOString(),text:'fixture'}]
  const first=await f.run(list),key=f.contexts[0].key
  f.complete('decided')
  const settled=await f.run(first.keep,f.restart())
  f.clock+=24*3600000
  const final=await f.run(settled.keep,f.restart())
  assert.equal(f.files().length,1)
  assert.deepEqual(final.keep,list)
  assert.deepEqual(final.state.once,{})
  assert.equal(final.state.outcomes[key].status,'decided')
})

test('invalid terminal or review metadata remains pending and cannot release a reminder', async t => {
  for (const change of [{status:'failed'},{decision_id:''},{decision_id:null},{next_review_at:'tomorrow'},
    {next_review_at:-1},{next_review_at:1.5},{next_review_at:1},{completed_at:true},{scope:'private:999'}]) {
    const f=fixture(t), list=[{at:new Date(f.clock-1000).toISOString(),text:'fixture'}]
    const first=await f.run(list)
    f.complete('decided',undefined,change)
    const final=await f.run(first.keep,f.restart())
    assert.deepEqual(final.keep,list)
    assert.deepEqual(final.state.once,{})
    assert.equal(Object.keys(final.state.pending).length,1)
    assert.equal(f.files().length,1)
  }
})

test('completion read validates the same metadata before any producer can accept done', t => {
  const f=fixture(t), event={scope:'private:101',event_id:'fixture',kind:'test',producer:'fixture',
    workspace_key:'',agent_key:'',created_at:1,payload:{text:'fixture'}}
  publishObservation(f.inbox,event)
  f.complete('decided',undefined,{decision_id:'  '})
  assert.throws(()=>readObservationCompletion(f.inbox,event.scope,event.event_id),/Invalid observation completion/)
})

for (const invalid of ['{broken','[]','null','true']) {
  test('explicit invalid project config cannot fall back to legacy: '+invalid,t=>{
    const f=fixture(t), bad=path.join(f.directory,'explicit.json')
    fs.writeFileSync(bad,invalid);fs.mkdirSync(path.join(f.directory,'config'))
    fs.writeFileSync(path.join(f.directory,'config','project.json'),JSON.stringify({decisions:{mode:'legacy'}}))
    assert.throws(()=>resolveRuntime({}, {env:{LANYA_WORKDIR:f.directory,LANYA_PROJECT_CONFIG:bad},cwd:f.directory}),/Project configuration/)
  })
}
test('missing explicit config and unreadable preferred config fail closed',t=>{
  const f=fixture(t), missing=path.join(f.directory,'missing.json')
  assert.throws(()=>resolveRuntime({}, {env:{LANYA_WORKDIR:f.directory,LANYA_PROJECT_CONFIG:missing},cwd:f.directory}),/unavailable/)
  fs.mkdirSync(path.join(f.directory,'napcat','config'),{recursive:true})
  fs.writeFileSync(path.join(f.directory,'napcat','config','project.json'),'{broken')
  fs.mkdirSync(path.join(f.directory,'config'))
  fs.writeFileSync(path.join(f.directory,'config','project.json'),JSON.stringify({decisions:{mode:'legacy'}}))
  assert.throws(()=>resolveRuntime({}, {env:{LANYA_WORKDIR:f.directory},cwd:f.directory}),/invalid/)
})

async function hostFixture(t, {failPersistence=false,agentPreset='fixture-preset',omitPreset=false}={}) {
  const f=fixture(t), data=path.join(f.directory,'role'), project=path.join(f.directory,'project.json')
  fs.mkdirSync(data);fs.mkdirSync(path.join(f.directory,'napcat'))
  fs.writeFileSync(project,JSON.stringify({dataDirName:'role',decisions:{mode:'unified',...(!omitPreset&&{agentPreset})}}))
  fs.writeFileSync(path.join(data,'lanya-config.json'),JSON.stringify({adminQQ:['101']}))
  const list=[{at:new Date(Date.now()-1000).toISOString(),text:'host fixture'}]
  fs.writeFileSync(path.join(data,'reminders.json'),JSON.stringify(list))
  const names=['LANYA_WORKDIR','LANYA_PROJECT_CONFIG'], oldEnv=Object.fromEntries(names.map(name=>[name,process.env[name]]))
  process.env.LANYA_WORKDIR=f.directory;process.env.LANYA_PROJECT_CONFIG=project
  const old={setInterval:globalThis.setInterval,setTimeout:globalThis.setTimeout,
    clearInterval:globalThis.clearInterval,clearTimeout:globalThis.clearTimeout,fetch:globalThis.fetch,
    link:fs.linkSync,rename:fs.renameSync}
  const shutdown=[], failures=[]
  let published=0, observedIntent=null
  globalThis.setInterval=globalThis.setTimeout=()=>1
  globalThis.clearInterval=globalThis.clearTimeout=()=>{}
  globalThis.fetch=()=>{throw new Error('network forbidden in fixture')}
  fs.linkSync=(from,to,...args)=>{
    if (String(to).includes(path.join('decision-inbox','pending'))) {
      const saved=JSON.parse(fs.readFileSync(path.join(data,'reminders.state.json')))
      observedIntent=Object.values(saved.pending)[0]
      assert.equal(observedIntent.reminder.text,'host fixture')
      published++
    }
    return old.link(from,to,...args)
  }
  fs.renameSync=(from,to,...args)=>{
    if (failPersistence && String(to)===path.join(data,'reminders.state.json')) {
      const error=new Error('fixture state persistence failed');error.code='EACCES';throw error
    }
    return old.rename(from,to,...args)
  }
  try {
    const {apply}=await import('../plugin/lib/index.js')
    apply({effect(fn,label){if(label.includes('built-in scheduler')) shutdown.push(fn())},
      inject(){},systemPrompt:{section(){}},logger:{info(){},warn(error){failures.push(error)}}},
      {workDir:f.directory,schedulerEnabled:true})
    for(let tick=0;tick<20;tick++) await new Promise(resolve=>setImmediate(resolve))
    const queue=path.join(data,'decision-inbox','pending')
    const events=fs.existsSync(queue)?fs.readdirSync(queue).filter(name=>name.endsWith('.json')).map(name=>JSON.parse(fs.readFileSync(path.join(queue,name)))):[]
    return {published,observedIntent,directory:f.directory,events,list:JSON.parse(fs.readFileSync(path.join(data,'reminders.json'))),
      count:events.length}
  } finally {
    for(const stop of shutdown) stop?.()
    globalThis.setInterval=old.setInterval;globalThis.setTimeout=old.setTimeout
    globalThis.clearInterval=old.clearInterval;globalThis.clearTimeout=old.clearTimeout;globalThis.fetch=old.fetch
    fs.linkSync=old.link;fs.renameSync=old.rename
    for(const name of names) {if(oldEnv[name]===undefined) delete process.env[name];else process.env[name]=oldEnv[name]}
  }
}

test('actual plugin host caller persists original intent before publishing and keeps a queued reminder',async t=>{
  const result=await hostFixture(t)
  assert.equal(result.published,1)
  assert.equal(result.count,1)
  assert.equal(result.list.length,1)
  assert.equal(result.observedIntent.day,'')
})

test('actual host persistence failure prevents queue publication without consuming a reminder',async t=>{
  const result=await hostFixture(t,{failPersistence:true})
  assert.equal(result.published,0)
  assert.equal(result.count,0)
  assert.equal(result.list.length,1)
})

test('actual host reminder producer normalizes explicit owner with Python platform semantics',async t=>{
  const result=await hostFixture(t,{agentPreset:'  Fixture-Preset  '})
  assert.equal(result.events.length,1)
  const event=result.events[0],workspace=path.resolve(result.directory)
  assert.equal(event.workspace_key,process.platform==='win32'?workspace.toLowerCase():workspace)
  assert.equal(event.agent_key,process.platform==='win32'?'fixture-preset':'Fixture-Preset')
  assert.equal(result.list.length,1)
})

for(const agentPreset of ['', '   ', null]) {
  test('actual host preserves the shared empty reminder key for '+JSON.stringify(agentPreset),async t=>{
    const result=await hostFixture(t,{agentPreset})
    assert.equal(result.published,1)
    assert.equal(result.count,1)
    assert.equal(result.events[0].agent_key,'')
    assert.equal(result.list.length,1)
  })
}

test('actual host retains the existing shared-key semantics when no preset is configured',async t=>{
  const result=await hostFixture(t,{omitPreset:true})
  assert.equal(result.published,1)
  assert.equal(result.count,1)
  assert.equal(result.events[0].agent_key,'')
  assert.equal(result.list.length,1)
})

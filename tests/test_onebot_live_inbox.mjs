/** Actual incrementally patched bridge and adapter. SDK durable queue and
 * transport/provider boundaries are explicit inert seams; no platform calls. */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import vm from 'node:vm'
import { spawnSync } from 'node:child_process'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { createHash } from 'node:crypto'
import nativeIdleWake from './fixtures/native_idle_wake/native-methods.mjs'

if (!vm.SourceTextModule) {
  test('native inbox cases in isolated VM', () => {
    const env = { ...process.env }; delete env.NODE_TEST_CONTEXT
    const child = spawnSync(process.execPath, ['--experimental-vm-modules', '--test', fileURLToPath(import.meta.url)],
      { encoding: 'utf8', windowsHide: true, timeout: 60000, env })
    assert.equal(child.status, 0, child.stdout + child.stderr)
  })
} else {
const repo = path.dirname(path.dirname(fileURLToPath(import.meta.url)))
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-live-inbox-'))
test.after(() => fs.rmSync(temporary, { recursive: true, force: true }))
const hostSource = fs.readFileSync(path.join(repo, 'tests/fixtures/onebot_egress/native-host.mjs'), 'utf8')
const anchor = "  bridge.installChannelScope({ tools: { register: (tool) => { tools.set(tool.name, tool); return noop } },\n    systemPrompt: { section: noop } })"
assert.equal(hostSource.split(anchor).length, 2)
const hostPath = path.join(temporary, 'host.mjs')
fs.writeFileSync(hostPath, hostSource.replace("{ execFile: deny, spawn: deny }", "{ execFile: deny, spawn: deny, spawnSync: deny }").replace(anchor, `  const hooks = new Map(), guards = []
  agent.ctx = { on(name, fn) { const list = hooks.get(name) || []; list.push(fn); hooks.set(name, list) },
    get: name => name === 'costMeter' ? deps.testMeter : undefined,
    tools: { register: tool => { tools.set(tool.name, tool); return noop }, guard: fn => { guards.push(fn); return noop } },
    systemPrompt: { section: value => { (deps.nativePrompts ||= []).push(value); return noop } } }
  bridge.installChannelScope(agent.ctx)
  deps.nativeHooks = hooks; deps.nativeGuards = guards`))
const { nativeHost } = await import(pathToFileURL(hostPath).href)
let serial = 0
const defer = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }
async function fixture(t, { admission = true, mislabeledImage = false, contextualGroupScope, burst = true } = {}) {
  const root = path.join(temporary, 'case-' + ++serial), plugin = path.join(root, 'plugin')
  fs.mkdirSync(root)
  const prepare = String.raw`
import shutil,sys
from pathlib import Path
repo,out=map(Path,sys.argv[1:3]);sys.path.insert(0,str(repo/'scripts'))
import patch_onebot_policy as p,patch_onebot_replay as r,patch_onebot_egress as e,patch_onebot_live_inbox as n,patch_onebot_inbound_context as q
shutil.copytree(repo/'tests/fixtures/onebot_egress',out)
for name in ['bridge.js','outbound.js','tools.js','prompt.js']:
 f=out/'lib'/name;raw=p.transform(name,f.read_bytes())
 if name=='bridge.js':raw=r.transform('lib/bridge.js',raw)
 if name in ['bridge.js','outbound.js']:raw=e.transform('lib/'+name,raw)
 if name=='bridge.js':raw=n.transform(raw)
 if name=='bridge.js':
  raw=q.transform(name,raw)
  import patch_onebot_group_context as g
  raw=g.transform(name,raw)
  if sys.argv[4]=='1':
   import patch_onebot_burst_intake as b
   raw=b.transform(name,raw)
 f.write_bytes(raw)
f=out/'lib/inbound.js';s=f.read_text(encoding='utf-8');a='        const nickname = sanitizeNickname(inbound.nickname);';assert s.count(a)==1
raw=q.transform('inbound.js',('// LANYA_POLICY_PATCH_V1\n'+s.replace(a,'        const observed = await this.ctx.observeInbound(inbound);\n        if (!observed.accepted) return;\n'+a)).encode('utf-8'))
if sys.argv[3]=='1':
 import patch_onebot_inbound_admission as admission
 raw=admission.transform(raw)
 import patch_onebot_group_context as g
 raw=g.transform('inbound.js',raw)
 if sys.argv[4]=='1':
  import patch_onebot_burst_intake as b
  raw=b.transform('inbound.js',raw)
f.write_bytes(raw)
for name,raw in p.module_outputs({'stateFile':str(out.parent/'state.json')}).items():
 if name=='lanya-policy-adapter.js':raw=e.transform('lib/'+name,raw)
 (out/'lib'/name).write_bytes(raw)
(out/e.GENERATED).write_bytes((repo/'plugin/lib/qq-egress.js').read_bytes())
(out/'lib/qq-inbox-recovery.js').write_bytes((repo/'plugin/lib/qq-inbox-recovery.js').read_bytes())
`
  const built = spawnSync('python', ['-B', '-c', prepare, repo, plugin, admission ? '1' : '0', burst && admission ? '1' : '0'], { encoding: 'utf8', windowsHide: true })
  assert.equal(built.status, 0, built.stderr)
  fs.writeFileSync(path.join(plugin, 'lib/lanya-policy.config.json'), JSON.stringify({ stateFile: path.join(root, 'state.json'), budgetRuntimeScript: path.join(root, 'inert.py'), contextualGroupScope }))
  let nativeMedia
  if (mislabeledImage) {
    fs.mkdirSync(path.join(root,'media'))
    const file=path.join(root,'media','incoming.jpg')
    fs.writeFileSync(file,Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7','base64'))
    nativeMedia={resolve:async ref=>({kind:ref.kind,path:file})}
  }
  const h = await nativeHost(plugin, root, {nativeMedia})
  const { createDecisionPolicy } = await import(pathToFileURL(path.join(plugin, 'lib/lanya-decision-policy.js')).href)
  let clock = Date.parse('2026-11-03T01:00:00Z')
  h.bridge.lanyaPolicy.now = () => clock
  h.bridge.lanyaPolicy.policy = createDecisionPolicy({ stateFile: path.join(root, 'state.json'), now: () => clock })
  t.after(() => h.close())
  const pending = [], history = [], events = [], requests = []
  h.agent.status = 'running'
  let turn = 0, seq = 10
  h.agent.session.snapshotEvents = () => events
  // Same synchronous durable splice / replace / claim order as the SDK.
  h.agent.inbox = { nextStep: [], get nextTurn() { return pending.slice() }, replace(id, message) {
    const index = pending.findIndex(item => item.id === id)
    if (index < 0) return false
    const removed = pending.splice(index, 1, structuredClone(message))
    history.push({ removed, inserted: [structuredClone(message)], canceled: true })
    return true
  } }
  h.agent.followup = message => {
    pending.push(structuredClone(message)); history.push({ removed: [], inserted: [structuredClone(message)] })
    h.followups.push(message)
  }
  h.deps.testMeter = { getTurnCost: async () => ({ found: true, apiCost: 0.01, calls: [{ priced: true }] }) }
  h.bridge.lanyaPolicy.budgetProvider = async request => {
    requests.push(structuredClone(request))
    if (request.operation === 'reserve') return { allowed: true, category: request.origin === 'observation' ? 'autonomy' : request.conversation_key.startsWith('group:') ? 'group' : 'private', limited: true, decision_only: false }
    if (request.operation === 'check') return { allowed: true, reason: 'within_reservation' }
    return { bound: true, settled: true }
  }
  const hooks = h.deps.nativeHooks
  const emitClaim = message => { for (const hook of hooks.get('agent/inbox/claimed') || []) hook({ agent: h.agent, message, turn }) }
  const start = (messages) => {
    const event = { type: 'turn/start', seq: ++seq, data: { turn: ++turn } }; events.push(event)
    h.bridge.onSessionEvent(h.agent.session, event)
    const claimed = messages || pending.splice(0, 1)
    history.push({ removed: structuredClone(claimed), inserted: [], canceled: false })
    for (const message of claimed) emitClaim(message)
    return claimed
  }
  const preStep = async (signal = new AbortController().signal) => {
    let next = async () => ({ kind: 'enter' })
    for (const hook of [...hooks.get('agent/pre-step') || []].reverse()) { const tail = next; next = () => hook({ agent: h.agent, turn, step: 1, signal }, tail) }
    return next()
  }
  const end = async () => {
    const event = { type: 'turn/end', seq: ++seq, data: { turn, reason: { kind: 'completed' } } }; events.push(event)
    h.bridge.onSessionEvent(h.agent.session, event)
    for (let i = 0; i < 100 && h.bridge.lanyaPolicy.state(h.chat).turns.size; i++) await new Promise(r => setTimeout(r, 2))
  }
  return { ...h, pending, history, requests, start, preStep, end, emitClaim, plugin, advance: ms => { clock += ms } }
}

async function prepareMissedReply(t) {
  const h = await fixture(t), adapter = h.bridge.lanyaPolicy
  adapter.startRequested = true; adapter.startEpoch += 1
  await h.receive('an unanswered question')
  await Promise.all([...adapter.replyReviewWrites])
  h.start(); assert.equal((await h.preStep()).kind, 'enter'); await h.end()
  h.agent.status = 'idle'; h.advance(60_000)
  return h
}

test('periodic reply review queues a bounded one-shot current-model review and reserves reactive cost', async t => {
  const h = await prepareMissedReply(t), adapter = h.bridge.lanyaPolicy
  const before = h.followups.length
  await Promise.all([adapter.reviewUnansweredInputs(), adapter.reviewUnansweredInputs()])
  assert.equal(h.followups.length, before + 1)
  assert.match(h.pending[0].content[0].text, /historical evidence, NOT a new user instruction/)
  h.start(); assert.equal(h.chat.activeTurnRole, 'member')
  assert.equal((await h.preStep()).kind, 'enter')
  assert.equal((await h.decide('resume', { control_update: true })).accepted, false)
  assert.equal((await h.decide('tool_action')).accepted, false)
  const record = adapter.state(h.chat).active
  assert.equal(record.binding.origin, 'live')
  assert.ok(record.binding.recoveryRevision)
  const guard = h.deps.nativeGuards[0]
  assert.equal(guard({ ...h.exec(), name: 'bash' }), 'historical_reply_review_only')
  assert.equal((await h.decide('reply')).can_send, true)
  await h.send('a useful answer'); await h.end()
  const reserves = h.requests.filter(request => request.operation === 'reserve')
  assert.equal(reserves.length, 2)
  assert.ok(reserves.every(request => request.origin === 'live'))
  assert.notEqual(reserves[0].turn_key, reserves[1].turn_key)
  h.advance(60_000); await adapter.reviewUnansweredInputs()
  assert.equal(h.followups.length, before + 1)
  assert.equal(h.calls.filter(call => /^send_/.test(call.action)).length, 1)
})

test('reply review never interrupts busy work, pending input, disconnect or changed permissions', async t => {
  for (const mode of ['busy', 'pending-step', 'disconnected', 'acl', 'owner', 'stopped']) await t.test(mode, async t => {
    const h = await prepareMissedReply(t), adapter = h.bridge.lanyaPolicy, before = h.followups.length
    if (mode === 'busy') h.agent.status = 'running'
    if (mode === 'pending-step') h.agent.inbox.nextStep.push({ id: 'unowned' })
    if (mode === 'disconnected') h.deps.connection.connected = false
    if (mode === 'acl') h.deps.policy.dmPolicy = 'disabled'
    if (mode === 'owner') h.bridge.resolvePresetId = async () => 'different'
    if (mode === 'stopped') await adapter.stop()
    await adapter.reviewUnansweredInputs()
    assert.equal(h.followups.length, before)
    assert.equal(h.calls.filter(call => /^send_/.test(call.action)).length, 0)
  })
})

test('reply review honors an accepted quiet decision and actual sends rather than replaying text', async t => {
  for (const verdict of ['idle', 'defer', 'silence', 'sent']) await t.test(verdict, async t => {
    const h = await fixture(t), adapter = h.bridge.lanyaPolicy
    adapter.startRequested = true; adapter.startEpoch += 1
    await h.receive('current live input'); await Promise.all([...adapter.replyReviewWrites])
    h.start(); await h.preStep()
    if (verdict === 'sent') { await h.decide('reply'); await h.send('already answered') }
    else await h.decide(verdict, verdict === 'silence' ? { control_update: true } : {})
    await h.end(); h.agent.status = 'idle'; h.advance(90_000)
    const before = h.followups.length
    await adapter.reviewUnansweredInputs()
    assert.equal(h.followups.length, before)
  })
})

test('only a direct real bot mention or private receipt is staged; ordinary configured-group chat is not', async t => {
  const h = await fixture(t), adapter = h.bridge.lanyaPolicy
  const binding = { origin: 'live', scope: 'group:202', messageId: 'g', revision: 1, owner: {} }
  let staged = 0
  adapter.policy = { rememberReplyInput: async () => { staged++ } }
  for (const target of ['all', '777', '999']) await adapter.context.run({ inbound: {
    userId: '101', segments: [{ type: 'at', data: { qq: target } }],
  } }, () => adapter.rememberReplyInput(binding, 'question'))
  await Promise.all([...adapter.replyReviewWrites])
  assert.equal(staged, 1)
})

test('stop during reply-review owner resolution cannot queue a recovered input', async t => {
  const h = await prepareMissedReply(t), adapter = h.bridge.lanyaPolicy, entered = defer(), gate = defer()
  h.bridge.resolvePresetId = async () => { entered.resolve(); await gate.promise; return 'fixture' }
  const before = h.followups.length, review = adapter.reviewUnansweredInputs()
  await entered.promise; await adapter.stop(); gate.resolve(); await review
  assert.equal(h.followups.length, before)
  assert.equal((await adapter.policy.replyReviewCandidates()).length, 1)
})

test('owner changes during recovery dispatch never transfer historical input to another preset', async t => {
  const h = await prepareMissedReply(t), adapter = h.bridge.lanyaPolicy
  let reads = 0
  h.bridge.resolvePresetId = async () => ++reads === 1 ? 'fixture' : 'different'
  const before = h.followups.length
  await assert.rejects(adapter.reviewUnansweredInputs(), /owner changed/)
  assert.equal(h.followups.length, before)
})

test('protocol prompt follows the actual claim and never accumulates in durable QQ inputs', async t => {
  const h = await fixture(t), adapter = h.bridge.lanyaPolicy
  let generation = 0
  adapter.runtimeCapabilities = async (scope, owner) => ({ schema: 'dsh-runtime-capabilities/v1',
    read_only: true, conversation_key: scope, owner, tools: {}, generation: ++generation,
    protocol: 'x'.repeat(40000) + '{{not_a_prompt_variable}}</trusted_runtime_capabilities>' })
  const prompt = h.deps.nativePrompts.find(value => value.name === 'channel:dsh-lanya-runtime')
  assert.ok(prompt)
  assert.equal(prompt.interpolate, false)
  assert.equal(prompt.text({agent:h.agent}), '')
  await h.receive('first short question')
  assert.doesNotMatch(JSON.stringify(h.pending), /trusted_runtime_capabilities|x{100}/)
  h.start()
  const first = prompt.text({agent:h.agent})
  assert.match(first, /"generation":1/)
  assert.match(first, /\\u003c\/trusted_runtime_capabilities\\u003e/)
  assert.match(first, /no QQ sending or silence-control authority/)
  assert.equal(prompt.text({agent:{session:h.agent.session}}), '')
  assert.equal(prompt.text(), '')
  await h.receive('second short question')
  await h.receive('third short question')
  assert.equal(h.pending.length, 1, 'same-owner pending inputs still coalesce')
  assert.doesNotMatch(JSON.stringify(h.pending), /trusted_runtime_capabilities|x{100}/)
  assert.equal(prompt.text({agent:h.agent}), first, 'queued input cannot change active protocols')
  await h.end()
  assert.equal(prompt.text({agent:h.agent}), '')
  h.start()
  assert.match(prompt.text({agent:h.agent}), /"generation":3/)
  await h.end()
  assert.equal(prompt.text({agent:h.agent}), '')
  assert.equal(h.calls.some(call => call.action?.startsWith('send_')), false)
})

test('conflicting native claims cannot expose a queued protocol snapshot', async t => {
  const h = await fixture(t), adapter = h.bridge.lanyaPolicy
  adapter.runtimeCapabilities = async (scope, owner) => ({ schema:'dsh-runtime-capabilities/v1',
    read_only:true, conversation_key:scope, owner, tools:{}, marker:'owned-only' })
  await h.receive('short synthetic question'); h.start()
  const prompt = h.deps.nativePrompts.find(value => value.name === 'channel:dsh-lanya-runtime')
  assert.match(prompt.text({agent:h.agent}), /owned-only/)
  h.emitClaim({id:'foreign-claim',source:{kind:'host:web'},content:[]})
  assert.equal(prompt.text({agent:h.agent}), '')
  assert.equal((await h.preStep()).reason, 'unbound_native_input')
  await h.end()
})

test('direct and quoted inbound GIF bytes with JPG names reach the actual bridge as correct image paths', async t => {
  for(const quoted of [false,true])await t.test(quoted?'quoted':'direct',async t=>{
    const h=await fixture(t,{mislabeledImage:true})
    const image={type:'image',data:{file:'synthetic-hash',sub_type:'1'}}
    if(quoted)h.deps.connection.call=async()=>({message_id:41,message_type:'private',
      sender:{user_id:101},message:[image]})
    await h.receive('look at this image',{message:[{type:'text',data:{text:'look at this image'}},
      quoted?{type:'reply',data:{id:'41'}}:image]})
    assert.equal(h.pending.length,1)
    const text=h.pending[0].content[0].text
    assert.match(text,/media_format_[a-f0-9]{64}\.gif/)
    assert.doesNotMatch(text,/incoming\.jpg/)
    const original=path.join(h.directory,'media','incoming.jpg')
    const copies=fs.readdirSync(path.dirname(original)).filter(n=>n.startsWith('media_format_'))
    assert.equal(copies.length,1)
    assert.deepEqual(fs.readFileSync(path.join(path.dirname(original),copies[0])),fs.readFileSync(original))
    assert.equal(h.requests.length,0,'format correction does not enter a model or reserve a turn')
    assert.equal(h.calls.filter(call=>/^(send_|upload_)/.test(call.action)).length,0,'no platform transmission')
  })
})

test('fresh protocol reads preserve the prompt prefix when only their read timestamp changes', async t => {
  const h = await fixture(t), adapter = h.bridge.lanyaPolicy
  let reads = 0, ready = 'unverified'
  adapter.runtimeCapabilities = async (scope, owner) => ({ schema:'dsh-runtime-capabilities/v1',
    read_only:true, conversation_key:scope, owner, tools:{browser:{worker:ready}}, generated_at:++reads })
  const prompt = h.deps.nativePrompts.find(value => value.name === 'channel:dsh-lanya-runtime')
  await h.receive('first'); h.start(); const first = prompt.text({agent:h.agent}); await h.end()
  await h.receive('second'); h.start(); const second = prompt.text({agent:h.agent}); await h.end()
  assert.equal(reads, 2, 'configuration is read afresh, not cached')
  assert.equal(first, second, 'read timestamp must not invalidate the model prefix')
  assert.doesNotMatch(first, /generated_at/)
  ready = 'offline'
  await h.receive('third'); h.start(); const changed = prompt.text({agent:h.agent}); await h.end()
  assert.equal(reads, 3)
  assert.notEqual(changed, first, 'actual status changes must reach the next claimed turn')
  assert.match(changed, /offline/)
})

test('native next-step host context preserves one transport claim across reminder and compaction boundaries', async t => {
  const h = await fixture(t)
  await h.receive('synthetic question')
  h.start(); assert.equal((await h.preStep()).kind,'enter')
  const original = h.bridge.lanyaPolicy.state(h.chat).active.binding
  for(const kind of ['repeat-tool-reminder','agent-instructions','runtime-context','compact-checkpoint']) {
    h.emitClaim({id:'native-'+kind,role:'user',source:{kind},content:[{type:'text',text:'synthetic host context'}]})
    assert.equal((await h.preStep()).kind,'enter')
    assert.equal(h.bridge.lanyaPolicy.state(h.chat).active.binding,original)
  }
  assert.equal(h.requests.filter(row=>row.operation==='reserve').length,1)
  await h.end()
})

test('host context before a queued live claim is neutral but cannot authorize a host-only turn', async t => {
  const h = await fixture(t)
  await h.receive('synthetic new question')
  const input=h.pending.splice(0,1)[0]
  const host={id:'native-instructions',role:'user',source:{kind:'agent-instructions'},content:[{type:'text',text:'fixture'}]}
  h.start([host,input]);assert.equal((await h.preStep()).kind,'enter');await h.end()
  h.start([host]);assert.equal((await h.preStep()).reason,'unbound_native_input')
  assert.equal(h.requests.filter(row=>row.operation==='reserve').length,1)
  await h.end()
})

test('unknown source or a changed registered transport ID cannot borrow the host-context exception', async t => {
  for(const changedId of [false,true]) {
    const h=await fixture(t);await h.receive('synthetic question');const input=h.start()[0]
    assert.equal((await h.preStep()).kind,'enter')
    const record=h.bridge.lanyaPolicy.state(h.chat).active
    if(changedId) h.bridge.lanyaPolicy.state(h.chat).pending.set(input.id,{agent:h.agent,fingerprint:'wrong',binding:record.binding,role:'member'})
    h.emitClaim({id:changedId?input.id:'unknown-input',role:'user',source:{kind:changedId?'agent-instructions':'unreviewed-plugin'},content:[]})
    assert.equal((await h.preStep()).reason,'unbound_native_input')
    await h.end()
  }
})

test('legacy early admission reproduces rate-drop revoking the only queued answer', async t => {
  const h = await fixture(t, { admission: false })
  h.deps.config.rateLimitPerMinute = 1
  await h.receive('synthetic first question')
  h.start(); await h.preStep()
  const revision = (await h.bridge.lanyaPolicy.policy.snapshot(h.chat.chatId)).revision
  await h.receive('synthetic rate-dropped followup')
  assert.equal(h.pending.length, 0)
  assert.equal((await h.bridge.lanyaPolicy.policy.snapshot(h.chat.chatId)).revision, revision + 1)
  assert.equal((await h.decide()).reason, 'stale_revision')
  assert.equal(h.calls.filter(call => /^send_/.test(call.action)).length, 0)
})

for (const dropped of ['rate', 'empty', 'command']) {
  test('rejected ' + dropped + ' input cannot revoke the valid active answer', async t => {
    const h = await fixture(t)
    if(dropped==='rate')h.bridge.inbound.ctx.nativeBurstEnabled=()=>false
    h.deps.config.rateLimitPerMinute = dropped === 'rate' ? 1 : 0
    await h.receive('synthetic first question')
    h.start(); await h.preStep()
    const policy = h.bridge.lanyaPolicy.policy, before = await policy.snapshot(h.chat.chatId)
    if (dropped === 'command') h.bridge.inbound.ctx.tryHandleCommand = async () => true
    await h.receive(dropped === 'empty' ? '  ' : dropped === 'command' ? '/synthetic-command' : 'synthetic followup')
    const after = await policy.snapshot(h.chat.chatId)
    assert.equal(after.revision, before.revision)
    assert.equal(after.mode, before.mode)
    assert.equal(h.pending.length, 0)
    const decision = await h.decide()
    assert.equal(decision.accepted, true)
    assert.equal(decision.can_send, true)
    assert.equal(h.calls.filter(call => /^send_/.test(call.action)).length, 0)
  })
}

test('a filtered newer arrival does not prevent an older valid media input from admission', async t => {
  const h = await fixture(t), gate = defer(), entered = defer()
  const original = h.bridge.inbound.ctx.buildBody
  h.bridge.inbound.ctx.buildBody = async (...args) => {
    if (args[0] === 'synthetic slow media') { entered.resolve(); await gate.promise }
    return original(...args)
  }
  const older = h.receive('synthetic slow media')
  await entered.promise
  await h.receive('  ')
  assert.equal(await h.bridge.lanyaPolicy.policy.snapshot(h.chat.chatId), null)
  gate.resolve(); await older
  assert.equal(h.pending.length, 1)
  h.start(); await h.preStep()
  assert.equal((await h.decide()).accepted, true)
})

test('admission commits serialize per scope and a rejected storage call releases its tail', async t => {
  const h = await fixture(t), adapter = h.bridge.lanyaPolicy, policy = adapter.policy
  const gate = defer(), entered = defer(), original = policy.recordInbound
  let calls = 0
  policy.recordInbound = async value => {
    calls++
    if (calls === 1) { entered.resolve(); await gate.promise; throw new Error('synthetic storage failure') }
    return original(value)
  }
  const inbound = id => ({ chatId: h.chat.chatId, messageId: id, userId: '101', text: id })
  const admit = value => adapter.processInbound(value, () => adapter.observeInbound(value))
  const first = assert.rejects(admit(inbound('failed-commit')), /synthetic storage failure/)
  await entered.promise
  const next = admit(inbound('newer-commit'))
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(calls, 1, 'newer policy commit must wait for the same scope')
  gate.resolve(); await first
  assert.equal((await next).accepted, true)
  assert.equal(calls, 2)
  assert.equal(adapter.inboundAdmissions.size, 0)
  assert.equal((await policy.snapshot(h.chat.chatId)).revision, 1)
})

test('busy burst collapses pending inputs, preserves canceled originals, reserves only claimed native turn', async t => {
  const h = await fixture(t)
  for (let i = 0; i < 10; i++) await h.receive('synthetic question ' + i)
  assert.equal(h.pending.length, 1)
  assert.equal(h.history.filter(row => row.canceled).length, 9)
  assert.equal(h.requests.length, 0)
  assert.equal(h.chat.pendingTurnRoles.length, 0)
  const newest = h.pending[0]
  assert.match(newest.content[0].text, /synthetic question 0/)
  assert.match(newest.content[0].text, /synthetic question 9/)
  h.start()
  assert.equal(h.bridge.lanyaPolicy.state(h.chat).active.claimedId, newest.id)
  assert.equal(h.chat.activeTurnRole, 'admin')
  assert.equal((await h.preStep()).kind, 'enter')
  await h.preStep()
  assert.equal(h.requests.filter(row => row.operation === 'reserve').length, 1)
  assert.equal((await h.decide()).accepted, true)
  await h.end()
  assert.equal(h.requests.filter(row => row.operation === 'settle').length, 1)
})

test('group member burst coalesces but a later administrator does not lend control authority', async t => {
  const h = await fixture(t), scope = 'group:303'
  h.bridge.chats.delete(h.chat.chatId); h.chat.chatId = scope
  h.bridge.chats.set(scope, h.chat); h.bridge.bySession.set(h.agent.session.id, scope)
  for (let i = 0; i < 10; i++) await h.receive('member ' + i, { message_type: 'group', group_id: 303, user_id: 202 })
  assert.equal(h.pending.length, 1)
  await h.receive('admin fresh', { message_type: 'group', group_id: 303, user_id: 101 })
  assert.equal(h.pending.length, 2)
  h.start(); await h.preStep()
  assert.equal(h.chat.activeTurnRole, 'member')
  assert.equal(h.bridge.canEditFiles(h.agent.session.id), false)
  assert.equal((await h.decide('resume', { control_update: true, evidenceSource: 'current' })).accepted, false)
  await h.end(); h.start(); await h.preStep()
  assert.equal(h.chat.activeTurnRole, 'admin')
  assert.equal(h.bridge.canEditFiles(h.agent.session.id), true)
  assert.equal((await h.decide('silence', { control_update: true, evidenceSource: 'current' })).accepted, true)
  assert.equal(h.requests.filter(row => row.operation === 'reserve').length, 2)
})

test('turn/start alone, reloaded unknown IDs and modified native content cannot grant authority', async t => {
  const h = await fixture(t)
  await h.receive('synthetic')
  const message = h.pending.shift()
  h.start([])
  await assert.rejects(h.decide(), /unbound_or_aborted_turn/)
  assert.equal((await h.preStep()).kind, 'reject')
  h.emitClaim({ ...message, id: 'unknown-after-reload' })
  assert.equal((await h.preStep()).kind, 'reject')
  await h.end()
  h.start([{ ...message, content: [{ type: 'text', text: 'changed' }] }])
  assert.equal(h.chat.activeTurnRole, 'member')
  assert.equal((await h.preStep()).kind, 'reject')
  assert.equal(h.requests.length, 0)
})

test('pending never merges across foreign item or role boundary', async t => {
  const h = await fixture(t)
  await h.receive('admin first')
  h.pending.push({ id: 'foreign', source: { kind: 'host:web' }, content: [] })
  await h.receive('admin second')
  assert.equal(h.pending.length, 3)
  // A trusted producer can choose the conservative member role even for the
  // administrator. It must still not merge into an admin-authorized slot.
  const adapter = h.bridge.lanyaPolicy
  const scope = h.chat.chatId
  await adapter.processInbound({ chatId: scope, messageId: 'member-raw', userId: '101', text: 'member' }, async () => {
    await adapter.observeInbound(adapter.context.getStore().inbound)
    const final = await adapter.enrichContext(scope, 'member')
    adapter.dispatchNative(h.chat, { id: 'member-native', source: { kind: 'plugin:dsh-onebot' }, content: [{ type: 'text', text: final }] }, 'member', 'member')
  })
  assert.equal(h.pending.at(-1).id, 'member-native')
  assert.equal(h.pending.at(-2).id, h.history.findLast(row => row.inserted?.[0]?.content?.[0]?.text?.includes('admin second')).inserted[0].id)
})

test('late enrichment cannot replace a newer revision and no pending budget is leaked', async t => {
  const h = await fixture(t), gate = defer(), entered = defer()
  h.bridge.lanyaPolicy.memoryContext = async (_scope, text) => { if (text.includes('slow older')) { entered.resolve(); await gate.promise } return null }
  const slow = h.receive('slow older')
  await entered.promise
  await h.receive('fast newer')
  const latest = h.pending[0].id
  assert.match(h.pending[0].content[0].text,/slow older/,'earlier admitted text survives slow enrichment')
  const freshMetadata={lastFollowup:h.chat.lastFollowup,lastNickname:h.chat.lastNickname}
  gate.resolve(); await slow
  assert.equal(h.pending.length, 1)
  assert.equal(h.pending[0].id, latest)
  assert.deepEqual({lastFollowup:h.chat.lastFollowup,lastNickname:h.chat.lastNickname},freshMetadata,
    'stale completion cannot overwrite retry metadata or reset the current conversation')
  assert.equal(h.requests.length, 0)
  h.start(); await h.preStep()
  assert.equal((await h.decide()).accepted, true)
})

test('native private and group bursts preserve the newest input beyond the old thirty-message cutoff', async t => {
  for(const group of [false,true])await t.test(group?'group':'private',async t=>{
    const h=await fixture(t),scope=group?'group:303':h.chat.chatId
    h.deps.config.rateLimitPerMinute=30
    if(group){h.bridge.chats.delete(h.chat.chatId);h.chat.chatId=scope;h.bridge.chats.set(scope,h.chat);h.bridge.bySession.set(h.agent.session.id,scope)}
    const extra=group?{message_type:'group',group_id:303,user_id:202}:{}
    for(let i=0;i<80;i++)await h.receive('synthetic burst '+i,extra)
    assert.equal(h.pending.length,1,'same-role native suffix coalesces rather than dropping the final message')
    assert.match(h.pending[0].content[0].text,/synthetic burst 79/)
    assert.equal((await h.bridge.lanyaPolicy.policy.snapshot(scope)).revision,80)
    assert.equal(h.requests.length,0,'pending inputs have no individual model reservations')
    assert.equal(h.calls.filter(x=>/send_/.test(x.action)).length,0)
    const log=fs.readFileSync(path.join(h.directory,'interaction-health.jsonl'),'utf8')
    assert.doesNotMatch(log,/synthetic burst|private:|group:|user_message/)
    const rows=log.trim().split('\n').map(JSON.parse)
    assert.equal(rows.filter(x=>x.phase==='queued').length,80)
    assert.equal(rows.filter(x=>x.phase==='not_admitted').length,0)
  })
})

test('accepted quiet decisions use the actual SDK conclusion hook, rejected controls never do', async t => {
  for(const verdict of ['idle','defer','silence','reply'])await t.test(verdict,async t=>{
    const h=await fixture(t);await h.receive('synthetic current question');h.start();await h.preStep()
    let concluded=0;const exec={...h.exec(),concludeTurn:()=>{concluded++}}
    const result=await h.tools.get('qq_decide').execute({verdict,...(verdict==='silence'?{control_update:true}:{})},exec)
    assert.equal(result.accepted,true)
    assert.equal(concluded,verdict==='reply'?0:1)
    assert.equal(h.calls.filter(x=>/send_/.test(x.action)).length,0)
    await h.end()
  })
  const h=await fixture(t);await h.receive('synthetic question');h.start();await h.preStep()
  let concluded=0
  const result=await h.tools.get('qq_decide').execute({verdict:'silence'},{...h.exec(),concludeTurn:()=>{concluded++}})
  assert.equal(result.accepted,false);assert.equal(concluded,0);await h.end()
})

test('native quiet decision ends before another model preparation or pressure compaction', async t => {
  let preparations=0,steps=0
  const h=await nativeDriverFixture(t,{assemble:async()=>{preparations++}})
  h.agent.step=async()=>{
    steps++;let concluded=false
    const result=await h.tools.get('qq_decide').execute({verdict:'idle'},
      {...h.exec(),concludeTurn:()=>{concluded=true}})
    assert.equal(result.accepted,true)
    return concluded||steps>2?{kind:'completed'}:null
  }
  await h.receive('synthetic unaddressed observation');await driverSettled(h)
  assert.equal(steps,1);assert.equal(preparations,1)
  assert.equal(h.nativeEvents.findLast(e=>e.type==='turn/end').data.reason.kind,'completed')
  assert.equal(h.calls.filter(x=>/send_/.test(x.action)).length,0)
})

test('preparing administrator context cannot give a later group member control authority', async t => {
  const h=await fixture(t),gate=defer(),entered=defer(),scope='group:303'
  h.bridge.chats.delete(h.chat.chatId);h.chat.chatId=scope;h.bridge.chats.set(scope,h.chat);h.bridge.bySession.set(h.agent.session.id,scope)
  h.bridge.lanyaPolicy.memoryContext=async(_scope,text)=>{if(text.includes('earlier administrator')){entered.resolve();await gate.promise}return null}
  const old=h.receive('earlier administrator silence request',{message_type:'group',group_id:303,user_id:101})
  await entered.promise
  await h.receive('current member follow-up',{message_type:'group',group_id:303,user_id:202})
  assert.match(h.pending[0].content[0].text,/earlier administrator/)
  h.start();assert.equal((await h.preStep()).kind,'enter');assert.equal(h.chat.activeTurnRole,'member')
  assert.equal((await h.decide('silence',{control_update:true})).reason,'unauthorized_control')
  gate.resolve();await old;await h.end()
})

test('slow preparation in one conversation cannot block other private and group conversations', async t => {
  const h=await fixture(t),held=defer(),entered=defer(),queues=new Map()
  for(const scope of ['private:202','private:404','group:303']){
    const pending=[];queues.set(scope,pending)
    const agent={...h.agent,session:{...h.agent.session,id:'parallel-'+scope},
      inbox:{get nextTurn(){return pending.slice()},replace(id,message){const index=pending.findIndex(x=>x.id===id);if(index<0)return false;pending.splice(index,1,structuredClone(message));return true}},
      followup:message=>pending.push(structuredClone(message))}
    const chat=h.bridge.registry.createChatAgent(scope,{agent,dispose:()=>{}},{},'synthetic peer')
    h.bridge.chats.set(scope,chat);h.bridge.bySession.set(agent.session.id,scope)
  }
  h.bridge.lanyaPolicy.memoryContext=async scope=>{if(scope==='private:202'){entered.resolve();await held.promise}return null}
  const blocked=h.receive('synthetic held input',{user_id:202});await entered.promise
  await Promise.all([h.receive('other private current',{user_id:404}),
    h.receive('group current',{message_type:'group',group_id:303,user_id:202}),h.receive('primary current')])
  assert.equal(queues.get('private:202').length,0)
  assert.equal(queues.get('private:404').length,1)
  assert.equal(queues.get('group:303').length,1)
  assert.equal(h.pending.length,1)
  held.resolve();await blocked;assert.equal(queues.get('private:202').length,1)
  assert.equal(h.requests.length,0);assert.equal(h.calls.filter(x=>/send_/.test(x.action)).length,0)
})

test('late partially resolved quoted images cannot supersede a newer native input or borrow its authority', async t => {
  const h = await fixture(t), gate = defer(), entered = defer(), reads = []
  h.deps.connection.call = async (action, params) => {
    assert.equal(action, 'get_msg'); assert.equal(params.message_id, 41)
    return { message_id: 41, message_type: 'private', sender: { user_id: 101, nickname: 'Quoted author' },
      message: ['one.png', 'two.png'].map(file => ({ type: 'image', data: { file, sub_type: '1' } })) }
  }
  h.deps.media.resolve = async ref => {
    reads.push(ref.file)
    if (ref.file === 'two.png') { entered.resolve(); await gate.promise; return undefined }
    return { kind: 'image', path: path.join(h.directory, 'synthetic-quote.png') }
  }
  const older = h.receive('older quote', { message: [{ type: 'reply', data: { id: '41' } },
    { type: 'text', data: { text: 'older quote' } }] })
  await entered.promise
  await h.receive('fresh current request')
  const newest = JSON.stringify(h.pending[0]), revision = h.bridge.lanyaPolicy.state(h.chat).liveRevision
  gate.resolve(); await older
  assert.deepEqual(reads, ['one.png', 'two.png'])
  assert.equal(h.pending.length, 1); assert.equal(JSON.stringify(h.pending[0]), newest)
  assert.equal(h.bridge.lanyaPolicy.state(h.chat).liveRevision, revision)
  assert.equal(h.requests.length, 0); assert.equal(h.calls.length, 0)
  assert.doesNotMatch(h.pending[0].content[0].text, /synthetic-quote|older quote/)
  h.start(); assert.equal((await h.preStep()).kind, 'enter')
  assert.equal((await h.decide()).accepted, true)
  assert.equal(h.requests.filter(row => row.operation === 'reserve').length, 1)
})

test('quote-only admin input cannot authorize control; fresh current text with quote still can', async t => {
  const h = await fixture(t)
  h.deps.connection.call = async () => ({ message_id: 41, message_type: 'private', sender: { user_id: 101 },
    message: [{ type: 'text', data: { text: 'synthetic quoted resume instruction' } }] })
  const reply = { type: 'reply', data: { id: '41' } }
  await h.receive('', { message: [reply], raw_message: '[CQ:reply,id=41]' })
  h.start(); await h.preStep()
  assert.equal((await h.decide('resume', { control_update: true, evidenceSource: 'current' })).accepted, false)
  await h.end()
  await h.receive('resume now', { message: [reply, { type: 'text', data: { text: 'resume now' } }] })
  h.start(); await h.preStep()
  assert.equal((await h.decide('resume', { control_update: true, evidenceSource: 'current' })).accepted, true)
  assert.equal(h.calls.length, 0)
})

test('only a confirmed current-scope send ACK seeds metadata for a bot-authored private quote', async t => {
  const h = await fixture(t)
  h.deps.connection.call = async () => ({ message_id: 41 })
  await h.receive('fresh question'); h.start(); await h.preStep(); await h.decide()
  await h.raw('send_private_msg', { user_id: 101, message: [{ type: 'text', data: { text: 'synthetic response' } }] })
  assert.equal(h.bridge.inbound.messageScopes.get('41'), 'private:101')
  assert.equal(h.bridge.inbound.messageScopes.has('unknown'), false)
  h.deps.connection.call = async action => {
    assert.equal(action, 'get_msg')
    return { message_id: 41, message_type: 'private', sender: { user_id: 999 },
      message: [{ type: 'text', data: { text: 'synthetic response' } }] }
  }
  assert.equal((await h.bridge.expandQuote('41', 'private:101')).status, 'available')
  h.deps.connection.call = async () => { throw new Error('synthetic network failure') }
  await assert.rejects(h.raw('send_private_msg', { user_id: 101, message: [{ type: 'text', data: { text: 'uncertain' } }] }))
  assert.equal(h.bridge.inbound.messageScopes.size, 1)
})

test('active reservation is never borrowed, old reply remains stale, and silence persists', async t => {
  const h = await fixture(t)
  await h.receive('first'); h.start(); await h.preStep()
  const oldBudget = h.bridge.lanyaPolicy.state(h.chat).active.binding.budget
  await h.receive('second'); await h.receive('third')
  assert.equal(h.pending.length, 1)
  assert.equal(h.requests.filter(row => row.operation === 'reserve').length, 1)
  assert.equal((await h.decide()).reason, 'stale_revision')
  await h.end(); h.start(); await h.preStep()
  assert.notEqual(h.bridge.lanyaPolicy.state(h.chat).active.binding.budget.request.turn_key, oldBudget.request.turn_key)
  const quiet = await h.decide('silence', { control_update: true, evidenceSource: 'current' })
  assert.equal(quiet.accepted, true)
  await h.end(); await h.receive('another question'); h.start(); await h.preStep()
  const reply = await h.decide('reply')
  assert.equal(reply.can_send, false)
})

test('second claimed input cannot upgrade role and aborted asynchronous admission cannot enter', async t => {
  const h = await fixture(t)
  await h.receive('first'); h.start()
  h.emitClaim({ id: 'foreign', source: { kind: 'host:web' }, content: [] })
  // Later valid notifications cannot undo the conflict latch.
  await h.receive('later valid')
  h.emitClaim(h.pending.shift())
  assert.equal(h.chat.activeTurnRole, 'member')
  await assert.rejects(h.decide(), /unbound_or_aborted_turn/)
  assert.equal((await h.preStep()).kind, 'reject')
  await h.end(); await h.receive('next'); h.start()
  const gate = defer(), controller = new AbortController()
  h.bridge.lanyaPolicy.budgetProvider = async () => { await gate.promise; return { allowed: true, category: 'private', limited: true, decision_only: false } }
  const admission = h.preStep(controller.signal)
  controller.abort(); gate.resolve()
  assert.equal((await admission).kind, 'reject')
})

test('idle pending uses followup wake rather than non-waking replace', async t => {
  const h = await fixture(t)
  await h.receive('first')
  h.agent.status = 'idle'
  await h.receive('second')
  assert.equal(h.pending.length, 2)
  assert.equal(h.followups.length, 2)
  assert.equal(h.history.filter(row => row.canceled).length, 0)
})

test('owner and observation boundaries preserve separate native inputs', async t => {
  const h = await fixture(t), adapter = h.bridge.lanyaPolicy, scope = h.chat.chatId
  await h.receive('first')
  const enqueue = async (id, origin, ownerChange = false) => {
    const current = await adapter.policy.recordInbound({ scope, messageId: id, sender: '101', origin: 'live', quoted: false, controlAllowed: true })
    await adapter.context.run({ binding: Object.freeze({ ...current, origin }) }, async () => {
      const text = await adapter.enrichContext(scope, id)
      if (ownerChange) adapter.context.getStore().binding = Object.freeze({ ...adapter.context.getStore().binding,
        owner: Object.freeze({ ...adapter.context.getStore().binding.owner, agent_key: 'different-owner' }) })
      adapter.dispatchNative(h.chat, { id, source: { kind: origin === 'live' ? 'plugin:dsh-onebot' : 'plugin:dsh-lanya-observation' }, content: [{ type: 'text', text }] }, 'admin', id)
    })
  }
  await enqueue('changed-owner', 'live', true)
  await enqueue('observation-native', 'observation')
  await h.receive('after observation')
  assert.equal(h.pending.length, 4)
  assert.equal(h.history.filter(row => row.canceled).length, 0)
})

test('conflicting observation claim rejects its completion without acknowledging or granting authority', async t => {
  const h = await fixture(t), adapter = h.bridge.lanyaPolicy, scope = h.chat.chatId
  let completionError
  const completion = { reject(error) { completionError = error }, resolve() { assert.fail('Conflict must not acknowledge an observation') } }
  const observed = await adapter.policy.recordObservation({ scope, eventId: 'synthetic-observation', kind: 'activity.opportunity', producer: 'mind', createdAt: adapter.now() })
  await adapter.context.run({ binding: Object.freeze({ ...observed, origin: 'observation', completion }) }, async () => {
    const text = await adapter.enrichContext(scope, 'synthetic evidence')
    adapter.dispatchNative(h.chat, { id: 'observation-with-completion', source: { kind: 'plugin:dsh-lanya-observation' }, content: [{ type: 'text', text }] }, 'member', 'synthetic evidence')
  })
  h.start()
  h.emitClaim({ id: 'foreign-input', source: { kind: 'host:web' }, content: [] })
  assert.equal((await h.preStep()).kind, 'reject')
  await h.end()
  assert.match(completionError?.message || '', /claim conflict/)
  assert.equal(h.requests.length, 0)
  assert.equal(h.calls.filter(call => /^(send_|upload_)/.test(call.action)).length, 0)
})

test('conflicting owned observation batch rejects every consumed completion including a modified claim', async t => {
  for (const modified of [false, true]) await t.test(modified ? 'modified-second' : 'two-owned', async t => {
    const h = await fixture(t), adapter = h.bridge.lanyaPolicy, scope = h.chat.chatId
    const failures = [[], []]
    for (let index = 0; index < failures.length; index++) {
      const completion = { reject(error) { failures[index].push(error) }, resolve() { assert.fail('Conflict cannot resolve') } }
      const observed = await adapter.policy.recordObservation({ scope, eventId: 'owned-batch-' + index,
        kind: 'activity.opportunity', producer: 'mind', createdAt: adapter.now() })
      await adapter.context.run({ binding: Object.freeze({ ...observed, origin: 'observation', completion }) }, async () => {
        const text = await adapter.enrichContext(scope, 'synthetic batch evidence')
        adapter.dispatchNative(h.chat, { id: 'owned-batch-input-' + index,
          source: { kind: 'plugin:dsh-lanya-observation' }, content: [{ type: 'text', text }] }, 'member', 'synthetic batch')
      })
    }
    const claimed = h.pending.splice(0)
    if (modified) claimed[1] = { ...claimed[1], content: [{ type: 'text', text: 'modified synthetic evidence' }] }
    h.start(claimed)
    assert.equal((await h.preStep()).kind, 'reject')
    await h.end()
    for (const errors of failures) {
      assert.equal(errors.length, 1)
      assert.match(errors[0].message, /claim conflict/)
    }
    assert.equal(adapter.state(h.chat).pending.size, 0)
    assert.equal(h.chat.activeTurnRole, 'member')
    assert.equal(h.requests.length, 0)
    assert.equal(h.calls.filter(call => /^(send_|upload_)/.test(call.action)).length, 0)
  })
})

async function nativeDriverFixture(t, options = {}) {
  const h = await fixture(t, options), steps = [], nativeEvents = []
  const hash = text => createHash('sha256').update(text).digest('hex')
  assert.equal(nativeIdleWake.source_module_sha256, '00c4814d63f3d1e2754eb09144832d19b30499d6ea444730473f740b587948c2')
  for (const [key, value] of Object.entries(nativeIdleWake.agentMethods)) assert.equal(hash(value), nativeIdleWake.snippet_sha256[key])
  assert.equal(hash(nativeIdleWake.inboxClass), nativeIdleWake.snippet_sha256.inboxClass)
  assert.equal(hash(nativeIdleWake.helper), nativeIdleWake.snippet_sha256.helper)
  // Unchanged native driver/inbox methods; assembly, Session persistence,
  // scoped notifications and model execution are explicit inert host seams.
  // Session.append notifies synchronously, matching the reviewed native code.
  const { Driver, Inbox } = vm.runInNewContext(nativeIdleWake.helper + '\n' + nativeIdleWake.inboxClass +
    '\nclass Driver {\n' + Object.values(nativeIdleWake.agentMethods).join('\n') + '\n}\n({ Driver, Inbox: ReactLoopInbox })', {
    AbortController, assembleContextFor: () => ({}), renderContextSections: () => [], joinContextSections: () => '',
    assertNever: value => { throw new Error('Unexpected fixture branch: ' + String(value)) },
    errorChain: error => error.message, LlmError: class extends Error {},
    ToolCallRecovery: class { observe() {} results() { return [] } },
  })
  for (const name of Reflect.ownKeys(Driver.prototype)) {
    if (name !== 'constructor') Object.defineProperty(h.agent, name, Object.getOwnPropertyDescriptor(Driver.prototype, name))
  }
  const hooks = h.deps.nativeHooks, nativePending = { 'next-turn': [], 'next-step': [] }
  h.agent.ctx.on = (name, hook) => {
    const list = hooks.get(name) || []; list.push(hook); hooks.set(name, list)
    return () => { const index = list.indexOf(hook); if (index >= 0) list.splice(index, 1) }
  }
  h.agent.dispatch = {
    emit(name, data) { for (const hook of [...hooks.get(name) || []]) hook({ agent: h.agent, ...data }) },
    async serial(name, data) { for (const hook of [...hooks.get(name) || []]) await hook({ agent: h.agent, ...data }) },
    async waterfall(name, data, fallback) {
      let next = fallback
      for (const hook of [...hooks.get(name) || []].reverse()) { const tail = next; next = () => hook({ agent: h.agent, ...data }, tail) }
      return next()
    },
  }
  h.agent.session.snapshotEvents = () => nativeEvents
  h.agent.session.append = (type, data) => {
    const event = { type, data: structuredClone(data), seq: nativeEvents.length + 1 }
    nativeEvents.push(event)
    if (type === 'agent/inbox/spliced') nativePending[data.target].splice(data.start, data.removedCount || 0, ...structuredClone(data.inserted))
    h.bridge.onSessionEvent(h.agent.session, event)
    options.onEvent?.(event, h)
    return event
  }
  h.agent.phase = { kind: 'idle', lastTurn: 0 }
  h.agent.activityDone = Promise.resolve()
  h.agent.loopCtx = { agents: { withInitiator: (_agent, run) => run() },
    systemPrompt: { assemble: async () => { await options.assemble?.(); return {} } } }
  h.agent.runtimeContext = { project: () => undefined }
  h.agent.inbox = new Inbox({ stateOf: () => nativePending }, h.agent.session, h.agent.dispatch)
  h.agent.step = async () => {
    const active = h.bridge.lanyaPolicy.state(h.chat).active
    assert.ok(active?.binding, 'inert step must still pass native claim and admission')
    steps.push({ turn: h.agent.phase.turn, role: h.chat.activeTurnRole, claimedId: active.claimedId })
    return { kind: 'completed' }
  }
  return { ...h, steps, nativeEvents, nativePending }
}

async function driverSettled(h, predicate = () => h.agent.status === 'idle') {
  for (let attempt = 0; attempt < 200; attempt++) {
    if (predicate()) return
    await new Promise(resolve => setTimeout(resolve, 2))
  }
  assert.fail('native fixture did not settle')
}

test('native terminal draft without a decision gets one same-turn reminder, never automatic send permission', async t => {
  const h = await nativeDriverFixture(t)
  const adapter = h.bridge.lanyaPolicy
  let steps = 0, firstClaim
  h.agent.step = async () => {
    const record = adapter.state(h.chat).active
    steps++
    if (steps === 1) firstClaim = record.claimedId
    assert.equal(record.claimedId, firstClaim, 'reminder must not replace the live claim')
    h.agent.session.append('assistant/message', { turn: record.turn, step: steps,
      message: { id: 'draft-' + steps, content: [{ type: 'text', text: 'synthetic unsent draft' }] } })
    return { kind: 'completed' }
  }
  await h.receive('synthetic private question')
  await driverSettled(h)
  assert.equal(steps, 2, 'one reminder even when the model ignores it again')
  const reminders = h.nativeEvents.filter(e => e.type === 'agent/inbox/spliced')
    .flatMap(e => e.data.inserted).filter(m => m.source?.kind === 'plugin:dsh-lanya-decision-reminder')
  assert.equal(reminders.length, 1)
  assert.equal(h.nativeEvents.filter(e => e.type === 'turn/start').length, 1)
  assert.equal(h.requests.filter(x => x.operation === 'reserve').length, 1)
  assert.equal(h.calls.filter(x => /^(send_|upload_)/.test(x.action)).length, 0)
})

test('real decision after a terminal reminder retains the same native claim and may send its fresh final text', async t => {
  const h = await nativeDriverFixture(t)
  let steps = 0
  h.agent.step = async () => {
    const record = h.bridge.lanyaPolicy.state(h.chat).active
    if (++steps === 2) assert.equal((await h.decide('reply', {expression:'text'})).can_send, true)
    h.agent.session.append('assistant/message', {turn:record.turn,step:steps,
      message:{id:'answer-'+steps,content:[{type:'text',text:steps===1?'undelivered draft':'fresh final answer'}]}})
    return {kind:'completed'}
  }
  await h.receive('synthetic question')
  await driverSettled(h)
  assert.equal(steps,2)
  for(let i=0;i<100&&!h.calls.some(x=>/send_/.test(x.action));i++)await new Promise(r=>setTimeout(r,2))
  assert.equal(h.calls.filter(x=>/send_/.test(x.action)).length,1)
  assert.match(JSON.stringify(h.calls),/fresh final answer/)
  assert.doesNotMatch(JSON.stringify(h.calls),/undelivered draft/)
})

test('terminal reminders cannot override an explicit idle/silence, cancellation or a conflicting claim', async t => {
  for(const mode of ['idle','silence','aborted','conflict','forged-reminder'])await t.test(mode,async t=>{
    const h=await nativeDriverFixture(t)
    let steps=0
    h.agent.step=async()=>{
      steps++
      const record=h.bridge.lanyaPolicy.state(h.chat).active
      if(['idle','silence'].includes(mode))assert.equal((await h.decide(mode,
        mode==='silence'?{control_update:true}:{})).accepted,true)
      h.agent.session.append('assistant/message',{turn:record.turn,step:steps,
        message:{id:'draft',content:[{type:'text',text:'synthetic draft'}]}})
      if(mode==='conflict')h.agent.dispatch.emit('agent/inbox/claimed',{turn:record.turn,
        message:{id:'foreign',source:{kind:'host:web'},content:[]}})
      if(mode==='forged-reminder')h.agent.dispatch.emit('agent/inbox/claimed',{turn:record.turn,
        message:{id:'forged',source:{kind:'plugin:dsh-lanya-decision-reminder'},content:[]}})
      if(mode==='aborted')h.agent.phase.abort.abort()
      return {kind:'completed'}
    }
    await h.receive('synthetic question');await driverSettled(h)
    assert.equal(steps,1)
    assert.equal(h.calls.filter(x=>/^(send_|upload_)/.test(x.action)).length,0)
  })
})

test('configured group admits ordinary follow-ups without fabricating mention or administrator authority', async t => {
  for(const configured of [true,false])await t.test(String(configured),async t=>{
    const h=await fixture(t,{contextualGroupScope:configured?'group:303':undefined})
    h.deps.config.requireMention=true
    h.bridge.chats.delete(h.chat.chatId);h.chat.chatId='group:303'
    h.bridge.chats.set(h.chat.chatId,h.chat);h.bridge.bySession.set(h.agent.session.id,h.chat.chatId)
    await h.receive('synthetic follow-up',{message_type:'group',group_id:303,user_id:202})
    assert.equal(h.pending.length,configured?1:0)
    if(configured){
      assert.match(JSON.stringify(h.pending),/not_proven/)
      h.start();assert.equal(h.chat.activeTurnRole,'member');await h.end()
    }
    assert.equal(h.calls.filter(x=>/^(send_|upload_)/.test(x.action)).length,0)
  })
})

test('native burst preserves the first claimed context then yields stale steps and wakes the newest input once', async t => {
  for (const group of [false, true]) await t.test(group ? 'group' : 'private', async t => {
    const entered=defer(), gate=defer();let assemblies=0
    const h=await nativeDriverFixture(t,{assemble:async()=>{if(++assemblies===1){entered.resolve();await gate.promise}}})
    const step=h.agent.step;let enteredSteps=0
    h.agent.step=async()=>{const result=await step();return ++enteredSteps===1?null:result}
    let args={}
    if(group){
      const scope='group:303';h.bridge.chats.delete(h.chat.chatId);h.chat.chatId=scope
      h.bridge.chats.set(scope,h.chat);h.bridge.bySession.set(h.agent.session.id,scope)
      args={message_type:'group',group_id:303,user_id:202}
    }
    await h.receive('first burst input',args);await entered.promise
    for(let i=0;i<12;i++)await h.receive('new burst input '+i,args)
    const newest=h.agent.inbox.nextTurn[0]
    assert.equal(h.agent.inbox.nextTurn.length,1)
    const historyBefore=structuredClone(h.nativeEvents)
    gate.resolve()
    await driverSettled(h,()=>h.steps.length===2&&h.agent.status==='idle')
    assert.notEqual(h.steps[0].claimedId,newest.id,'first input enters native history before yielding')
    assert.equal(h.steps[1].claimedId,newest.id)
    assert.equal(h.steps[1].role,group?'member':'admin')
    assert.deepEqual(h.nativeEvents.slice(0,historyBefore.length),historyBefore,'raw insertions remain intact')
    assert.deepEqual(h.nativeEvents.filter(e=>e.type==='turn/end').map(e=>e.data.reason.kind),['blocked','completed'])
    assert.equal(h.requests.filter(r=>r.operation==='reserve').length,2)
    assert.equal(h.agent.inbox.hasPending,false)
    assert.equal(h.calls.filter(c=>/^(send_|upload_)/.test(c.action)).length,0)
  })
})

test('native burst stops stale model steps and waits for old accounting before waking its successor', async t => {
  const h=await nativeDriverFixture(t),entered=defer(),modelGate=defer(),settling=defer(),settleGate=defer()
  const step=h.agent.step;let count=0
  h.agent.step=async()=>{const result=await step();if(++count===1){entered.resolve();await modelGate.promise;return null}return result}
  const adapter=h.bridge.lanyaPolicy,provider=adapter.budgetProvider
  adapter.budgetProvider=async request=>{
    const result=await provider(request)
    if(request.operation==='settle'&&count===1){settling.resolve();await settleGate.promise}
    return result
  }
  await h.receive('active burst input');await entered.promise
  await h.receive('new burst input one');await h.receive('new burst input two')
  const newest=h.agent.inbox.nextTurn[0],original=structuredClone(h.agent.inbox.nextTurn)
  modelGate.resolve();await settling.promise;await h.agent.whenIdle()
  assert.equal(h.steps.length,1,'no stale second model step')
  assert.deepEqual(h.agent.inbox.nextTurn,original,'successor stays durably pending until accounting ends')
  settleGate.resolve()
  await driverSettled(h,()=>h.steps.length===2&&h.agent.status==='idle')
  assert.equal(h.steps[1].claimedId,newest.id)
  assert.deepEqual(h.requests.map(r=>r.operation),['reserve','settle','reserve','settle'])
  assert.deepEqual(h.nativeEvents.filter(e=>e.type==='turn/end').map(e=>e.data.reason.kind),['blocked','completed'])
  assert.equal(h.agent.inbox.hasPending,false)
})

test('canceling a superseded native turn does not cause an automatic wake or replay', async t => {
  const entered=defer(),gate=defer();let assemblies=0
  const h=await nativeDriverFixture(t,{assemble:async()=>{if(++assemblies===1){entered.resolve();await gate.promise}},
    onEvent:(event,host)=>{if(event.type==='turn/end'&&event.data.reason.kind==='blocked')host.agent.cancel({kind:'user'},{keepInbox:true})}})
  const step=h.agent.step;h.agent.step=async()=>{await step();return null}
  await h.receive('old fixture');await entered.promise;await h.receive('new fixture')
  const pending=structuredClone(h.agent.inbox.nextTurn);gate.resolve();await h.agent.whenIdle()
  await new Promise(resolve=>setTimeout(resolve,20))
  assert.equal(h.steps.length,1);assert.deepEqual(h.agent.inbox.nextTurn,pending)
  assert.equal(h.requests.filter(r=>r.operation==='reserve').length,1)
})

async function primaryEvent(h, eventId) {
  const adapter = h.bridge.lanyaPolicy, scope = h.chat.chatId
  adapter.runtimeCapabilities = async (conversation, owner) => ({
    schema: 'dsh-runtime-capabilities/v1', read_only: true, conversation_key: conversation,
    owner: { workspace_key: owner.workspace_key, agent_key: owner.agent_key },
    tools: { entertainment: { primary_identity_valid: true, primary_internal_leisure: true,
      cadence: { valid: true, configured_opportunity_min: 3 } } },
  })
  const owner = await adapter.trustedOwner(scope)
  return { version: 1, event_id: eventId, scope, workspace_key: owner.workspace_key, agent_key: owner.agent_key,
    kind: 'activity.opportunity', producer: 'mind', created_at: adapter.now(), payload: {},
    origin: 'observation', control_allowed: false }
}

test('actual native inbox keeps host instructions and repeated-tool context separate from transport authority', async t => {
  const h = await nativeDriverFixture(t, {onEvent(event,host) {
    if(event.type==='step/end' && event.data.step===1) host.agent.inbox.append('next-step',
      {id:'sdk-repeat-reminder',role:'user',source:{kind:'repeat-tool-reminder'},content:[{type:'text',text:'fixture reminder'}]})
  }})
  h.agent.inbox.append('next-step',{id:'sdk-compaction-instructions',role:'user',source:{kind:'agent-instructions'},
    content:[{type:'text',text:'fixture reassembled context'}]})
  await h.receive('synthetic directed question')
  await driverSettled(h,()=>h.nativeEvents.some(item=>item.type==='turn/end') && h.agent.status==='idle')
  assert.equal(h.steps.length,2)
  assert.equal(h.steps[0].claimedId,h.steps[1].claimedId)
  assert.equal(h.requests.filter(item=>item.operation==='reserve').length,1)
  assert.deepEqual(h.nativeEvents.filter(item=>item.type==='turn/end').map(item=>item.data.reason.kind),['completed'])
  assert.equal(h.agent.inbox.hasPending,false)
  assert.equal(h.calls.filter(call=>/^(send_|upload_)/.test(call.action)).length,0)
})

test('native mixed restored and owned observation claim rejects the consumed completion instead of hanging', async t => {
  const h = await nativeDriverFixture(t), adapter = h.bridge.lanyaPolicy, scope = h.chat.chatId
  // The actual native inbox claims next-step history BEFORE the fresh queued
  // turn. No synthetic h.start()/emitClaim call substitutes this ordering.
  h.agent.inbox.append('next-step', { id: 'unbound-restored-step', source: { kind: 'host:web' },
    content: [{ type: 'text', text: 'synthetic restored input' }] })
  const event = await primaryEvent(h, 'synthetic-mixed-observation')
  let outcome = 'pending', failure
  const completion = adapter.dispatchObservation(event).then(
    () => { outcome = 'resolved' }, error => { outcome = 'rejected'; failure = error })
  await driverSettled(h, () => h.nativeEvents.some(item => item.type === 'turn/end') && h.agent.status === 'idle')
  // The defect left this promise pending forever even though the SDK already
  // consumed both physical inputs and ended the rejected native turn.
  await Promise.race([completion, new Promise(resolve => setTimeout(resolve, 30))])
  assert.equal(outcome, 'rejected')
  assert.match(failure.message, /claim conflict/)
  assert.equal(h.steps.length, 0)
  assert.equal(h.requests.filter(item => item.operation === 'reserve').length, 0)
  assert.deepEqual(h.nativeEvents.filter(item => item.type === 'turn/end').map(item => item.data.reason.kind), ['blocked'])
  assert.equal(h.agent.inbox.hasPending, false)
  assert.equal(adapter.state(h.chat).pending.size, 0)
  assert.equal(adapter.observations.size, 0)
  assert.equal(h.calls.filter(call => /^(send_|upload_)/.test(call.action)).length, 0)
  assert.equal((await adapter.policy.snapshot(scope)).current.decided, false)
})

test('native first-step unresolved prior reservation closes only the unused observation lifecycle', async t => {
  const h = await nativeDriverFixture(t), adapter = h.bridge.lanyaPolicy
  const event = await primaryEvent(h, 'synthetic-unresolved-reservation')
  const priorReservation = Object.freeze({ status: 'reserved', reserve_cny: 0.05, instance_id: 'other-instance' })
  const priorBytes = JSON.stringify(priorReservation)
  h.bridge.lanyaPolicy.budgetProvider = async request => {
    h.requests.push(structuredClone(request))
    assert.equal(request.operation, 'reserve', 'refusal must not settle or mark the old reservation unknown')
    return { allowed: false, category: 'autonomy', limited: true, reason: 'previous_turn_unresolved' }
  }
  const outcome = await adapter.dispatchObservation(event)
  await driverSettled(h)
  assert.equal(outcome.completed, true)
  assert.equal(outcome.status, 'no_decision')
  assert.equal(outcome.reason, 'native_admission_refused')
  assert.equal(outcome.native_turn_completed, false)
  assert.equal('decision_id' in outcome, false)
  assert.equal('next_review_at' in outcome, false)
  assert.equal(JSON.stringify(priorReservation), priorBytes)
  assert.deepEqual(h.requests.map(item => item.operation), ['reserve'])
  assert.equal(h.steps.length, 0)
  assert.deepEqual(h.nativeEvents.filter(item => item.type === 'turn/end').map(item => item.data.reason.kind), ['blocked'])
  const snapshot = await adapter.policy.snapshot(event.scope)
  assert.equal(snapshot.current.decided, false)
  assert.equal(snapshot.current.ticket, null)
  assert.equal(snapshot.deliveries.length, 0)
  assert.equal(h.calls.filter(call => /^(send_|upload_)/.test(call.action)).length, 0)
})

test('other native admission failures reject rather than claim the unresolved-reservation lifecycle', async t => {
  for (const mode of ['budget_exhausted', 'cost_meter_unavailable', 'provider-error', 'live-input',
    'other-kind', 'other-producer', 'cancel-after-refusal']) await t.test(mode, async t => {
    const h = await nativeDriverFixture(t, { onEvent: (event, state) => {
      if (mode === 'cancel-after-refusal' && event.type === 'turn/end' && event.data.reason.kind === 'blocked') {
        state.agent.cancel({ kind: 'user' }, { keepInbox: true })
      }
    } }), adapter = h.bridge.lanyaPolicy
    const event = await primaryEvent(h, 'synthetic-other-refusal-' + mode)
    if (mode === 'other-kind') event.kind = 'system.maintenance'
    if (mode === 'other-producer') event.producer = 'observer-repair'
    h.bridge.lanyaPolicy.budgetProvider = async request => {
      h.requests.push(structuredClone(request))
      assert.equal(request.operation, 'reserve')
      if (mode === 'provider-error') throw Object.assign(new Error('synthetic'), { reason: 'previous_turn_unresolved' })
      return { allowed: false, category: mode === 'live-input' ? 'private' : 'autonomy', limited: true,
        reason: ['live-input', 'other-kind', 'other-producer', 'cancel-after-refusal'].includes(mode)
          ? 'previous_turn_unresolved' : mode }
    }
    let lifecycleCalls = 0
    const original = adapter.policy.completeObservationWithoutDecision
    adapter.policy.completeObservationWithoutDecision = (...args) => { lifecycleCalls++; return original(...args) }
    if (mode === 'live-input') await h.receive('synthetic live refusal')
    else await assert.rejects(adapter.dispatchObservation(event), /before a real decision/)
    await driverSettled(h)
    assert.equal(lifecycleCalls, 0)
    assert.equal(h.steps.length, 0)
    assert.deepEqual(h.requests.map(item => item.operation), ['reserve'])
    assert.equal(h.calls.filter(call => /^(send_|upload_)/.test(call.action)).length, 0)
  })
})

test('reviewed native driver wakes an owned pending live input after its unbound restored turn blocks', async t => {
  const entered = defer(), gate = defer(); let assemblies = 0
  const h = await nativeDriverFixture(t, { assemble: async () => { if (++assemblies === 1) { entered.resolve(); await gate.promise } } })
  const restored = { id: 'restored-without-binding', source: { kind: 'plugin:dsh-onebot' }, content: [{ type: 'text', text: 'historical fixture' }] }
  h.agent.inbox.append('next-turn', restored)
  h.agent.wakeDriver()
  await entered.promise
  await h.receive('new owned fixture')
  const owned = h.agent.inbox.nextTurn[0]
  assert.equal(h.agent.phase.wakeRequested, false, 'ordinary running followup does not latch a native wake')
  gate.resolve()
  await driverSettled(h, () => h.steps.length === 1 && h.agent.status === 'idle')
  assert.equal(h.steps[0].claimedId, owned.id)
  assert.equal(h.steps[0].role, 'admin')
  assert.equal(h.requests.filter(request => request.operation === 'reserve').length, 1)
  assert.deepEqual(h.nativeEvents.filter(event => event.type === 'turn/end').map(event => event.data.reason.kind), ['blocked', 'completed'])
  assert.equal(h.nativeEvents.filter(event => event.type === 'agent/inbox/spliced' && event.data.inserted.some(message => message.id === owned.id)).length, 1)
  assert.equal(h.agent.inbox.hasPending, false)
  assert.equal(h.calls.filter(call => /^(send_|upload_)/.test(call.action)).length, 0)
})

test('internal idle wake refuses a foreign front, canceled rejected turn and changed SDK method', async t => {
  for (const mode of ['foreign-front', 'user-cancel', 'sdk-method-changed']) await t.test(mode, async t => {
    const entered = defer(), gate = defer(); let assemblies = 0
    const h = await nativeDriverFixture(t, {
      assemble: async () => { if (++assemblies === 1) { entered.resolve(); await gate.promise } },
      onEvent: (event, state) => {
        if (mode === 'user-cancel' && event.type === 'turn/end' && event.data.reason.kind === 'blocked') state.agent.cancel({ kind: 'user' }, { keepInbox: true })
      },
    })
    h.agent.inbox.append('next-turn', { id: 'restored', source: { kind: 'plugin:dsh-onebot' }, content: [] })
    h.agent.wakeDriver()
    await entered.promise
    if (mode === 'foreign-front') h.agent.inbox.append('next-turn', { id: 'foreign', source: { kind: 'host:web' }, content: [] })
    await h.receive('owned fixture remains pending')
    const before = structuredClone(h.agent.inbox.nextTurn)
    if (mode === 'sdk-method-changed') h.agent.wakeDriver = () => assert.fail('unreviewed internal method must not run')
    gate.resolve()
    await h.agent.whenIdle()
    await new Promise(resolve => setTimeout(resolve, 20))
    assert.deepEqual(h.agent.inbox.nextTurn, before)
    assert.equal(h.steps.length, 0)
    assert.equal(h.requests.length, 0)
    assert.equal(h.nativeEvents.filter(event => event.type === 'turn/start').length, 1)
  })
})

test('budget refusal cannot mint an internal idle wake for another pending input', async t => {
  const h = await nativeDriverFixture(t), entered = defer(), gate = defer()
  h.bridge.lanyaPolicy.budgetProvider = async request => {
    assert.equal(request.operation, 'reserve')
    entered.resolve(); await gate.promise
    return { allowed: false }
  }
  await h.receive('budget denied fixture')
  await entered.promise
  await h.receive('next owned fixture')
  const before = structuredClone(h.agent.inbox.nextTurn)
  gate.resolve()
  await h.agent.whenIdle()
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.deepEqual(h.agent.inbox.nextTurn, before)
  assert.equal(h.steps.length, 0)
  assert.equal(h.nativeEvents.filter(event => event.type === 'turn/start').length, 1)
})

test('owned observation input uses the same reviewed wake and native admission', async t => {
  const entered = defer(), gate = defer(); let assemblies = 0
  const h = await nativeDriverFixture(t, { assemble: async () => { if (++assemblies === 1) { entered.resolve(); await gate.promise } } })
  h.agent.inbox.append('next-turn', { id: 'restored', source: { kind: 'plugin:dsh-onebot' }, content: [] })
  h.agent.wakeDriver()
  await entered.promise
  const adapter = h.bridge.lanyaPolicy, scope = h.chat.chatId
  const observed = await adapter.policy.recordObservation({ scope, eventId: 'owned-observation', kind: 'activity.opportunity', producer: 'mind', createdAt: adapter.now() })
  await adapter.context.run({ binding: Object.freeze({ ...observed, origin: 'observation', observationCreatedAt: adapter.now() }) }, async () => {
    const text = await adapter.enrichContext(scope, 'observation fixture')
    adapter.dispatchNative(h.chat, { id: 'owned-native-observation', source: { kind: 'plugin:dsh-lanya-observation' },
      content: [{ type: 'text', text }] }, 'member', 'observation fixture')
  })
  gate.resolve()
  await driverSettled(h, () => h.steps.length === 1 && h.agent.status === 'idle')
  assert.equal(h.steps[0].claimedId, 'owned-native-observation')
  assert.equal(h.steps[0].role, 'member')
  assert.equal(h.requests.filter(request => request.operation === 'reserve' && request.origin === 'observation').length, 1)
})

test('idle wake rechecks owner, queue front and newer turns after asynchronous owner resolution', async t => {
  for (const mode of ['owner-changed', 'front-changed', 'newer-turn']) await t.test(mode, async t => {
    const entered = defer(), assemblyGate = defer(); let assemblies = 0
    const h = await nativeDriverFixture(t, { assemble: async () => { if (++assemblies === 1) { entered.resolve(); await assemblyGate.promise } } })
    h.agent.inbox.append('next-turn', { id: 'restored', source: { kind: 'plugin:dsh-onebot' }, content: [] })
    h.agent.wakeDriver()
    await entered.promise
    await h.receive('owned input')
    const validating = defer(), ownerGate = defer()
    h.bridge.resolvePresetId = async () => { validating.resolve(); await ownerGate.promise; return mode === 'owner-changed' ? 'changed-fixture' : 'fixture' }
    assemblyGate.resolve()
    await validating.promise
    if (mode === 'front-changed') h.agent.inbox.prepend('next-turn', { id: 'foreign-after-await', source: { kind: 'host:web' }, content: [] })
    if (mode === 'newer-turn') {
      h.agent.wakeDriver()
      await h.agent.whenIdle()
    }
    const before = structuredClone(h.agent.inbox.nextTurn)
    ownerGate.resolve()
    await new Promise(resolve => setTimeout(resolve, 20))
    assert.deepEqual(h.agent.inbox.nextTurn, before)
    assert.equal(h.nativeEvents.filter(event => event.type === 'turn/start').length, mode === 'newer-turn' ? 2 : 1)
    assert.equal(h.steps.length, mode === 'newer-turn' ? 1 : 0)
  })
})

test('actual installed SDK inbox and preStep method await claim-bound admission', {
  skip: !process.env.LANYA_TEST_AGENT_LOOP_FILE && 'Explicit readonly SDK source fixture not supplied',
}, async t => {
  const h = await fixture(t), raw = fs.readFileSync(process.env.LANYA_TEST_AGENT_LOOP_FILE, 'utf8')
  const inboxStart = raw.indexOf('var ReactLoopInbox = class {'), inboxEnd = raw.indexOf('//#endregion', inboxStart)
  const stepStart = raw.indexOf('\tasync preStep(target, position) {'), stepEnd = raw.indexOf('\n\t/** Whether the assembled tool schemas', stepStart)
  assert.ok(inboxStart >= 0 && inboxEnd > inboxStart && stepStart >= 0 && stepEnd > stepStart)
  // Execute the unchanged installed class/method bytes. Projection storage,
  // assembly and fused plugin dispatch are inert, explicit host seams.
  const Inbox = vm.runInNewContext(raw.slice(inboxStart, inboxEnd) + '\nReactLoopInbox')
  const Step = vm.runInNewContext('class Step {\n' + raw.slice(stepStart, stepEnd) + '\n}; Step', {
    assembleContextFor: () => ({}), renderContextSections: () => [], joinContextSections: () => '',
  })
  const state = { 'next-turn': [], 'next-step': [] }, durable = []
  let claimed = false
  const session = { id: h.agent.session.id, append(type, data) {
    assert.equal(type, 'agent/inbox/spliced')
    durable.push(structuredClone({ type, data }))
    state[data.target].splice(data.start, data.removedCount || 0, ...data.inserted)
    return { type, data }
  } }
  const dispatch = { emit(name, data) {
    if (name === 'agent/inbox/claimed') claimed = true
    for (const hook of h.deps.nativeHooks.get(name) || []) hook({ agent: h.agent, ...data })
  }, async waterfall(name, data, fallback) {
    let next = fallback
    for (const hook of [...h.deps.nativeHooks.get(name) || []].reverse()) { const tail = next; next = () => hook({ agent: h.agent, ...data }, tail) }
    return next()
  } }
  const inbox = new Inbox({ stateOf: () => state }, session, dispatch)
  h.agent.inbox = inbox
  h.agent.followup = message => inbox.append('next-turn', message)
  await h.receive('sdk earlier'); await h.receive('sdk latest')
  assert.equal(inbox.nextTurn.length, 1)
  assert.ok(durable.some(row => row.data.outcome === 'canceled'))
  h.start([])
  const gate = defer(), reserved = defer()
  h.bridge.lanyaPolicy.budgetProvider = async request => {
    assert.equal(claimed, true)
    assert.equal(request.operation, 'reserve')
    reserved.resolve(); await gate.promise
    return { allowed: true, category: 'private', limited: true, decision_only: false }
  }
  const step = Object.assign(new Step(), {
    id: 'inert-sdk', phase: { kind: 'running', abort: new AbortController() }, inbox,
    loopCtx: { systemPrompt: { assemble: async () => ({}) } },
    runtimeContext: { project: () => undefined }, dispatch,
  })
  let entered = false
  const result = step.preStep('next-turn', { turn: 1, step: 1 }).then(value => { entered = true; return value })
  await reserved.promise
  assert.equal(entered, false)
  assert.equal(inbox.nextTurn.length, 0)
  assert.equal(h.chat.activeTurnRole, 'admin')
  gate.resolve()
  assert.equal((await result).kind, 'enter')
})
}

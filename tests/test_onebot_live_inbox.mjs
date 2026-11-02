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
    systemPrompt: { section: noop } }
  bridge.installChannelScope(agent.ctx)
  deps.nativeHooks = hooks; deps.nativeGuards = guards`))
const { nativeHost } = await import(pathToFileURL(hostPath).href)
let serial = 0
const defer = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }
async function fixture(t) {
  const root = path.join(temporary, 'case-' + ++serial), plugin = path.join(root, 'plugin')
  fs.mkdirSync(root)
  const prepare = String.raw`
import shutil,sys
from pathlib import Path
repo,out=map(Path,sys.argv[1:3]);sys.path.insert(0,str(repo/'scripts'))
import patch_onebot_policy as p,patch_onebot_replay as r,patch_onebot_egress as e,patch_onebot_live_inbox as n
shutil.copytree(repo/'tests/fixtures/onebot_egress',out)
for name in ['bridge.js','outbound.js','tools.js','prompt.js']:
 f=out/'lib'/name;raw=p.transform(name,f.read_bytes())
 if name=='bridge.js':raw=r.transform('lib/bridge.js',raw)
 if name in ['bridge.js','outbound.js']:raw=e.transform('lib/'+name,raw)
 if name=='bridge.js':raw=n.transform(raw)
 f.write_bytes(raw)
f=out/'lib/inbound.js';s=f.read_text(encoding='utf-8');a='        const nickname = sanitizeNickname(inbound.nickname);';assert s.count(a)==1
f.write_text(s.replace(a,'        const observed = await this.ctx.observeInbound(inbound);\n        if (!observed.accepted) return;\n'+a),encoding='utf-8')
for name,raw in p.module_outputs({'stateFile':str(out.parent/'state.json')}).items():
 if name=='lanya-policy-adapter.js':raw=e.transform('lib/'+name,raw)
 (out/'lib'/name).write_bytes(raw)
(out/e.GENERATED).write_bytes((repo/'plugin/lib/qq-egress.js').read_bytes())
(out/'lib/qq-inbox-recovery.js').write_bytes((repo/'plugin/lib/qq-inbox-recovery.js').read_bytes())
`
  const built = spawnSync('python', ['-B', '-c', prepare, repo, plugin], { encoding: 'utf8', windowsHide: true })
  assert.equal(built.status, 0, built.stderr)
  fs.writeFileSync(path.join(plugin, 'lib/lanya-policy.config.json'), JSON.stringify({ stateFile: path.join(root, 'state.json'), budgetRuntimeScript: path.join(root, 'inert.py') }))
  const h = await nativeHost(plugin, root)
  t.after(() => h.close())
  const pending = [], history = [], events = [], requests = []
  h.agent.status = 'running'
  let turn = 0, seq = 10
  h.agent.session.snapshotEvents = () => events
  // Same synchronous durable splice / replace / claim order as the SDK.
  h.agent.inbox = { get nextTurn() { return pending.slice() }, replace(id, message) {
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
  return { ...h, pending, history, requests, start, preStep, end, emitClaim, plugin }
}

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
  gate.resolve(); await slow
  assert.equal(h.pending.length, 1)
  assert.equal(h.pending[0].id, latest)
  assert.equal(h.requests.length, 0)
  h.start(); await h.preStep()
  assert.equal((await h.decide()).accepted, true)
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
  const observed = await adapter.policy.recordObservation({ scope, eventId: 'synthetic-observation', kind: 'activity.opportunity', producer: 'mind', createdAt: Date.now() })
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

async function nativeDriverFixture(t, options = {}) {
  const h = await fixture(t), steps = [], nativeEvents = []
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
  const observed = await adapter.policy.recordObservation({ scope, eventId: 'owned-observation', kind: 'activity.opportunity', producer: 'mind', createdAt: Date.now() })
  await adapter.context.run({ binding: Object.freeze({ ...observed, origin: 'observation' }) }, async () => {
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

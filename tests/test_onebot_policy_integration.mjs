/** Synthetic host events + the actual replayed adapter; no model or QQ calls. */
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { spawnSync } from 'node:child_process'
import { pathToFileURL, fileURLToPath } from 'node:url'
import { createHash } from 'node:crypto'

const repo = path.dirname(path.dirname(fileURLToPath(import.meta.url)))
const prefix = path.join(os.tmpdir(), 'lanya-onebot-integration-')
const temporary = fs.mkdtempSync(prefix)
after(() => {
  assert.ok(path.resolve(temporary).startsWith(path.resolve(prefix)))
  fs.rmSync(temporary, { recursive: true, force: true })
})
const template = path.join(temporary, 'patched-template')
fs.cpSync(path.join(repo, 'tests', 'fixtures', 'onebot_policy'), template, { recursive: true })
const patchArgs = ['-B', path.join(repo, 'scripts', 'patch_onebot_policy.py'), '--plugin-dir', template,
  '--state-file', path.join(temporary, 'template-state.json')]
const preview = spawnSync('python', patchArgs, { encoding: 'utf8', windowsHide: true })
assert.equal(preview.status, 0, preview.stderr)
const apply = spawnSync('python', [...patchArgs, '--apply', '--host-stopped', '--backup-dir', path.join(temporary, 'backups'),
  '--expected-fingerprint', JSON.parse(preview.stdout).fingerprint], { encoding: 'utf8', windowsHide: true })
assert.equal(apply.status, 0, apply.stderr)

const waitUntil = async (condition, label) => {
  for (let i = 0; i < 300; i++) {
    if (condition()) return
    await new Promise(resolve => setTimeout(resolve, 5))
  }
  assert.fail('Timed out waiting for synthetic ' + label)
}
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes }); return { promise, resolve } }
const workspaceKey = cwd => process.platform === 'win32' ? path.resolve(cwd).toLowerCase() : path.resolve(cwd)
let sequence = 0

async function setup(t) {
  const dir = path.join(temporary, 'case-' + ++sequence)
  const plugin = path.join(dir, 'plugin')
  fs.cpSync(template, plugin, { recursive: true })
  const stateFile = path.join(dir, 'private', 'decision-policy.json')
  fs.mkdirSync(path.dirname(stateFile), { recursive: true })
  fs.writeFileSync(path.join(plugin, 'lib', 'lanya-policy.config.json'), JSON.stringify({ stateFile }))
  const calls = [], logs = [], tools = new Map(), prompts = []
  const deps = { config: { workspacePath: dir }, policy: { adminUsers: ['101'] }, cwd: dir,
    log: (level, message) => logs.push({ level, message }),
    connection: { connected: true, call: (action, params) => {
      calls.push({ action, params })
      return Promise.resolve({ message_id: 'mock-' + calls.length })
    } } }
  const { ChatBridge } = await import(pathToFileURL(path.join(plugin, 'lib', 'bridge.js')).href)
  const bridge = new ChatBridge(deps)
  bridge.lanyaPolicy.memoryContext = async scope => ({ schema: 'dsh-memento-context/v1', read_only: true,
    conversation_key: scope, owner: { workspace_key: dir, agent_key: 'fixture-preset' }, memories: [] })
  // Most transport tests isolate this independent enrichment; dedicated cases
  // below execute the real capability helper against an empty temporary workspace.
  bridge.lanyaPolicy.runtimeCapabilities = async () => null
  const ctx = { tools: { register: definition => { tools.set(definition.name, definition); return () => {} } },
    systemPrompt: { section: value => prompts.push(value.text) }, waitMedia: () => deps.waitMedia?.() }
  bridge.installChannelScope(ctx)
  t.after(async () => { await bridge.stop() })
  const turns = new Map()
  const receive = async ({ text = 'fixture question', id = 'm-' + ++sequence, scope = 'private:101', sender = '101', media = [] } = {}) => {
    const [kind, target] = scope.split(':')
    await bridge.handleInboundOnce({ message_type: kind, user_id: kind === 'private' ? target : sender,
      group_id: kind === 'group' ? target : undefined, message_id: id, text, media })
    return bridge.chats.get(scope)
  }
  const start = chat => {
    const turn = (turns.get(chat.chatId) || 0) + 1
    turns.set(chat.chatId, turn)
    bridge.onSessionEvent(chat.agent.session, { type: 'turn/start', data: { turn } })
    return turn
  }
  const exec = (chat, callId = 'call-' + ++sequence) => ({ agent: chat.agent, callId, signal: new AbortController().signal })
  const decide = (chat, value = 'reply', extra = {}) => tools.get('qq_decide').execute({ verdict: value, ...extra }, exec(chat))
  const assistant = (chat, turn, text) => bridge.onSessionEvent(chat.agent.session, { type: 'assistant/message',
    data: { turn, message: { id: 'assistant-' + ++sequence, content: [{ type: 'text', text }] } } })
  const end = async (chat, turn, wait = true) => {
    bridge.onSessionEvent(chat.agent.session, { type: 'turn/end', data: { turn, reason: { kind: 'completed' } } })
    if (wait) await waitUntil(() => !bridge.lanyaPolicy.state(chat).turns.has(turn), 'turn settlement')
  }
  return { dir, plugin, stateFile, calls, logs, tools, prompts, bridge, deps, receive, start, exec, decide, assistant, end }
}

test('patched bridge registers exclusive qq_decide and starts with no speech permit', async t => {
  const h = await setup(t)
  assert.equal(h.tools.get('qq_decide').isConcurrencySafe(), false)
  assert.match(h.prompts.join(''), /SAME chat LLM/)
  const chat = await h.receive()
  const turn = h.start(chat)
  await assert.rejects(() => h.tools.get('qq_send_image').execute({}, h.exec(chat)), /decision_required/)
  h.assistant(chat, turn, 'This undecided text must not leave')
  await h.end(chat, turn)
  assert.equal(h.calls.length, 0)
  assert.doesNotMatch(chat.messages[0].content[0].text, /SYNTHETIC_CROSS_CHAT|SYNTHETIC_PROFILE/)
  assert.match(chat.messages[0].content[0].text, /scoped_memory_context/)
})

test('the same bound chat LLM decides, then tools and final text use its fixed ticket', async t => {
  const h = await setup(t)
  const chat = await h.receive()
  const turn = h.start(chat)
  h.assistant(chat, turn, 'Pre-decision narration')
  await assert.rejects(() => h.tools.get('qq_decide').execute({ verdict: 'reply', scope: 'group:200' }, h.exec(chat)), /accepts only/)
  const decision = await h.decide(chat)
  assert.equal(decision.can_send, true)
  const imageExec = h.exec(chat, 'stable-image-call')
  assert.equal((await h.tools.get('qq_send_image').execute({}, imageExec)).sent, true)
  await assert.rejects(() => h.tools.get('qq_send_image').execute({}, imageExec), error => error.status === 'duplicate')
  await h.tools.get('qq_send_forward').execute({ messages: [{ name: 'fixture', content: 'forward' }] }, h.exec(chat))
  h.assistant(chat, turn, 'Final one\nFinal two')
  await h.end(chat, turn)
  assert.equal(h.calls.filter(item => item.action === 'send_msg').length, 3)
  assert.equal(h.calls.filter(item => item.action.includes('forward')).length, 1)
  assert.equal(h.calls.some(item => JSON.stringify(item.params).includes('Pre-decision')), false)
})

test('cross-chat text/media/forward/raw-API targets cannot borrow a ticket', async t => {
  const h = await setup(t)
  const chat = await h.receive()
  h.start(chat)
  await h.decide(chat)
  await assert.rejects(() => h.tools.get('qq_send_image').execute({ chat_id: 'group:200' }, h.exec(chat)), /scope_mismatch/)
  await assert.rejects(() => h.tools.get('qq_send_forward').execute({ chat_id: 'group:200', messages: [] }, h.exec(chat)), /scope_mismatch/)
  await assert.rejects(() => h.tools.get('qq_napcat_api').execute({ action: 'send_group_ai_record', params: { group_id: 200 } }, h.exec(chat)), /scope_mismatch/)
  assert.equal(h.calls.length, 0)
})

test('new inbound during media I/O invalidates its already captured permit', async t => {
  const h = await setup(t)
  const chat = await h.receive()
  h.start(chat)
  await h.decide(chat)
  const gate = deferred(), entered = deferred()
  h.deps.waitMedia = () => { entered.resolve(); return gate.promise }
  t.after(() => gate.resolve())
  const sending = h.tools.get('qq_send_image').execute({}, h.exec(chat))
  await entered.promise
  await h.receive({ text: 'new current message' })
  gate.resolve()
  await assert.rejects(sending, error => error.reason === 'stale_revision')
  assert.equal(h.calls.length, 0)
})

test('each split slice rechecks the original ticket after an intervening inbound', async t => {
  const h = await setup(t)
  const chat = await h.receive()
  const turn = h.start(chat)
  await h.decide(chat)
  h.deps.pauseSlice = async () => { await h.receive({ text: 'stop this stale reply' }) }
  h.assistant(chat, turn, 'first slice\nsecond slice')
  await h.end(chat, turn)
  assert.equal(h.calls.length, 1)
  assert.equal(h.calls[0].params.message[0].data.text, 'first slice')
})

test('reconnect retains the original envelope and cannot invent a fresh permit', async t => {
  const h = await setup(t)
  h.deps.connection.connected = false
  const chat = await h.receive()
  const turn = h.start(chat)
  await h.decide(chat)
  h.assistant(chat, turn, 'parked final')
  await h.end(chat, turn)
  assert.equal(h.bridge.outbound.pendingSends.get(chat.chatId).length, 1)
  const saved = h.bridge.outbound.pendingSends.get(chat.chatId)[0].options.lanyaEnvelope
  assert.ok(saved.ticket.id)
  await h.receive({ text: 'new input supersedes the parked final' })
  h.deps.connection.connected = true
  h.bridge.outbound.drainPendingSends()
  await h.bridge.outbound.chainTail(chat.chatId)
  assert.equal(h.calls.length, 0)
})

test('an unexpired reconnect envelope can deliver without any ambient/current turn', async t => {
  const h = await setup(t)
  h.deps.connection.connected = false
  const chat = await h.receive()
  const turn = h.start(chat)
  await h.decide(chat)
  h.assistant(chat, turn, 'parked final')
  await h.end(chat, turn)
  h.deps.connection.connected = true
  h.bridge.outbound.drainPendingSends()
  await h.bridge.outbound.chainTail(chat.chatId)
  assert.equal(h.calls.length, 1)
  assert.equal(h.calls[0].params.message[0].data.text, 'parked final')
})

test('an older async interim settlement cannot clear or borrow the next turn text', async t => {
  const h = await setup(t)
  const gate = deferred(), entered = deferred()
  h.deps.waitSettlement = () => { entered.resolve(); return gate.promise }
  t.after(() => gate.resolve())
  const chat = await h.receive()
  const first = h.start(chat)
  await h.decide(chat)
  h.assistant(chat, first, 'old final')
  await h.end(chat, first, false)
  await entered.promise
  await h.receive({ text: 'new user turn' })
  const second = h.start(chat)
  await h.decide(chat)
  h.assistant(chat, second, 'new final')
  gate.resolve()
  await h.end(chat, second)
  await waitUntil(() => !h.bridge.lanyaPolicy.state(chat).turns.has(first), 'old settlement')
  assert.deepEqual(h.calls.map(item => item.params.message[0].data.text), ['new final'])
})

test('persistent silence survives a fresh adapter and quoted/member/observation resume cannot reopen it', async t => {
  const h = await setup(t)
  const chat = await h.receive({ scope: 'group:200', sender: '101' })
  let turn = h.start(chat)
  assert.equal((await h.decide(chat, 'silence', { control_update: true })).mode, 'silent')
  await h.end(chat, turn)
  await h.receive({ scope: 'group:200', sender: '202', text: 'resume' })
  turn = h.start(chat)
  assert.equal((await h.decide(chat, 'resume', { control_update: true })).reason, 'unauthorized_control')
  await h.end(chat, turn)
  await h.receive({ scope: 'group:200', sender: '101', text: '' })
  turn = h.start(chat)
  assert.equal((await h.decide(chat, 'resume', { control_update: true })).reason, 'unauthorized_control')
  await h.end(chat, turn)
  const { OneBotDecisionAdapter } = await import(pathToFileURL(path.join(h.plugin, 'lib', 'lanya-policy-adapter.js')).href)
  const fresh = new OneBotDecisionAdapter(h.bridge, h.deps, { stateFile: h.stateFile })
  assert.equal((await fresh.policy.snapshot(chat.chatId)).mode, 'silent')
  await fresh.stop()
  await h.receive({ scope: 'group:200', sender: '101', text: 'resume now' })
  turn = h.start(chat)
  assert.equal((await h.decide(chat, 'resume', { control_update: true })).can_send, true)
  h.assistant(chat, turn, 'back')
  await h.end(chat, turn)
  assert.equal(h.calls.length, 1)
})

test('read-only APIs remain available while tool_action never permits a raw QQ send', async t => {
  const h = await setup(t)
  const chat = await h.receive()
  const turn = h.start(chat)
  await h.decide(chat, 'tool_action')
  await h.tools.get('qq_napcat_api').execute({ action: 'get_group_member_list', params: { group_id: 200 } }, h.exec(chat))
  await assert.rejects(() => h.tools.get('qq_napcat_api').execute({ action: 'upload_private_file', params: { user_id: 101 } }, h.exec(chat)), error => error.reason === 'not_a_speech_ticket')
  await h.end(chat, turn)
  assert.deepEqual(h.calls.map(item => item.action), ['get_group_member_list'])
})

test('failed/uncertain raw API ACK is never reported sent or retried by the same tool call', async t => {
  const h = await setup(t)
  const chat = await h.receive()
  h.start(chat)
  await h.decide(chat)
  h.deps.connection.call = (action, params) => { h.calls.push({ action, params }); return Promise.resolve({ status: 'failed', retcode: 100 }) }
  const exec = h.exec(chat, 'uncertain-raw')
  const args = { action: 'send_group_ai_record', params: { user_id: 101 } }
  await assert.rejects(() => h.tools.get('qq_napcat_api').execute(args, exec), error => error.status === 'unknown')
  await assert.rejects(() => h.tools.get('qq_napcat_api').execute(args, exec), error => error.status === 'duplicate')
  assert.equal(h.calls.length, 1)
})

test('observation uses the same agent with no control authority and completes only after its decision/turn', async t => {
  const h = await setup(t)
  const event = { version: 1, event_id: 'obs:one', scope: 'private:101', kind: 'quiet', producer: 'fixture',
    created_at: Date.now(), payload: { text: 'not a real user resume command' }, workspace_key: workspaceKey(h.dir),
    agent_key: 'fixture-preset', origin: 'observation', control_allowed: false }
  const first = h.bridge.dispatchObservation(event)
  const duplicate = h.bridge.dispatchObservation(event)
  const results = Promise.all([first, duplicate])
  await waitUntil(() => h.bridge.chats.get(event.scope)?.messages.length === 1, 'observation followup')
  const chat = h.bridge.chats.get(event.scope)
  assert.equal(chat.messages[0].source.kind, 'plugin:dsh-lanya-observation')
  const turn = h.start(chat)
  assert.equal((await h.decide(chat, 'resume', { control_update: true })).reason, 'unauthorized_control')
  const next = Date.now() + 120000
  await h.decide(chat, 'idle', { next_review_at: next })
  await h.end(chat, turn)
  const [a, b] = await results
  assert.deepEqual(a, b)
  assert.equal(a.status, 'decided')
  assert.equal(a.next_review_at, next)
  assert.equal(h.calls.length, 0)
  assert.equal((await h.bridge.lanyaPolicy.policy.snapshot(event.scope)).observations.length, 1)
  await h.bridge.lanyaPolicy.acknowledgeObservation(event, a)
  assert.equal((await h.bridge.lanyaPolicy.policy.snapshot(event.scope)).observations.length, 0)
})

test('foreign observation workspace/preset is rejected before pin or dispatch; async resolved owner is accepted', async t => {
  const h = await setup(t)
  h.bridge.resolvePresetId = async () => 'fixture-preset'
  const event = { version: 1, event_id: 'obs:owner', scope: 'private:101', kind: 'quiet', producer: 'fixture',
    created_at: Date.now(), payload: { text: 'private foreign payload must not escape in diagnostics' },
    workspace_key: workspaceKey(h.dir), agent_key: 'fixture-preset', origin: 'observation', control_allowed: false }
  let recorded = 0
  const record = h.bridge.lanyaPolicy.policy.recordObservation
  h.bridge.lanyaPolicy.policy.recordObservation = (...args) => { recorded++; return record(...args) }
  for (const foreign of [{ workspace_key: 'foreign-workspace' }, { agent_key: 'foreign-preset' }]) {
    await assert.rejects(() => h.bridge.dispatchObservation({ ...event, ...foreign }), error => {
      assert.match(error.message, /Observation owner mismatch/)
      assert.doesNotMatch(error.message, /private foreign payload/)
      return true
    })
    assert.equal(recorded, 0)
    assert.equal(fs.existsSync(h.stateFile), false)
    assert.equal(h.bridge.lanyaPolicy.observations.size, 0)
    assert.equal(h.bridge.chats.size, 0)
    assert.equal(h.calls.length, 0)
  }
  const completion = h.bridge.dispatchObservation(event)
  await waitUntil(() => h.bridge.chats.get(event.scope)?.messages.length === 1, 'trusted async owner followup')
  const chat = h.bridge.chats.get(event.scope)
  const turn = h.start(chat)
  await h.decide(chat, 'idle')
  await h.end(chat, turn)
  assert.equal((await completion).status, 'decided')
  assert.equal(recorded, 1)
  assert.equal(chat.messages.length, 1)
  assert.equal(h.calls.length, 0)
})

test('observation snapshot survives caller mutation while async preset resolution is pending', async t => {
  const h = await setup(t), preset = deferred(), resolving = deferred()
  h.bridge.resolvePresetId = () => { resolving.resolve(); return preset.promise }
  const scope = 'private:101', eventId = 'obs:snapshot'
  const event = { version: 1, event_id: eventId, scope, kind: 'quiet', producer: 'fixture',
    created_at: Date.now(), payload: { text: 'original detached evidence' }, workspace_key: workspaceKey(h.dir),
    agent_key: 'fixture-preset', origin: 'observation', control_allowed: false }
  const completion = h.bridge.dispatchObservation(event)
  await resolving.promise
  Object.assign(event, { scope: 'group:202', event_id: 'obs:mutated', workspace_key: 'foreign-workspace',
    agent_key: 'foreign-preset' })
  event.payload.text = 'mutated evidence must not be dispatched'
  preset.resolve('fixture-preset')
  await waitUntil(() => h.bridge.chats.get(scope)?.messages.length === 1, 'detached observation followup')
  const chat = h.bridge.chats.get(scope)
  assert.equal(h.bridge.chats.has('group:202'), false)
  const text = chat.messages[0].content[0].text
  assert.match(text, /original detached evidence/)
  assert.doesNotMatch(text, /mutated evidence|obs:mutated|foreign-workspace|foreign-preset/)
  const pinned = (await h.bridge.lanyaPolicy.policy.snapshot(scope)).observations
  assert.equal(pinned.length, 1)
  assert.equal(pinned[0].eventId, eventId)
  const turn = h.start(chat)
  await h.decide(chat, 'idle')
  await h.end(chat, turn)
  assert.equal((await completion).status, 'decided')
  assert.equal(h.calls.length, 0)
})

test('background evidence cannot revoke queued or currently sending real-user turns', async t => {
  const h = await setup(t)
  const chat = await h.receive()
  const event = { version: 1, event_id: 'obs:foreground', scope: 'private:101', kind: 'quiet', producer: 'fixture',
    created_at: Date.now(), payload: {}, workspace_key: workspaceKey(h.dir), agent_key: 'fixture-preset', origin: 'observation', control_allowed: false }
  const before = await h.bridge.lanyaPolicy.policy.snapshot(event.scope)
  await assert.rejects(() => h.bridge.dispatchObservation(event), /foreground_busy/)
  assert.equal((await h.bridge.lanyaPolicy.policy.snapshot(event.scope)).revision, before.revision)
  const turn = h.start(chat)
  await h.decide(chat)
  await assert.rejects(() => h.bridge.dispatchObservation(event), /foreground_busy/)
  h.assistant(chat, turn, 'real user answer')
  await h.end(chat, turn)
  assert.equal(h.calls.length, 1)
  const observation = h.bridge.dispatchObservation(event)
  await waitUntil(() => chat.messages.length === 2, 'deferred observer after foreground')
  const next = h.start(chat)
  await h.decide(chat, 'idle')
  await h.end(chat, next)
  assert.equal((await observation).status, 'decided')
})

test('inbound enrichment awaiting I/O has foreground priority before its turn is queued', async t => {
  const h = await setup(t)
  const entered = deferred(), release = deferred()
  h.bridge.lanyaPolicy.memoryContext = async () => { entered.resolve(); await release.promise; return {} }
  const receiving = h.receive()
  await entered.promise
  const event = { version: 1, event_id: 'obs:inbound-io', scope: 'private:101', kind: 'quiet', producer: 'fixture',
    created_at: Date.now(), payload: {}, workspace_key: workspaceKey(h.dir), agent_key: 'fixture-preset', origin: 'observation', control_allowed: false }
  try { await assert.rejects(() => h.bridge.dispatchObservation(event), /foreground_busy/) }
  finally { release.resolve() }
  const chat = await receiving
  const turn = h.start(chat)
  await h.decide(chat)
  h.assistant(chat, turn, 'answer after I/O')
  await h.end(chat, turn)
  assert.equal(h.calls.length, 1)
})

test('the attached durable inbox pump invokes the bound agent and acknowledges only a settled verdict', async t => {
  const h = await setup(t)
  const event = { version: 1, event_id: 'obs:pump', scope: 'private:101', kind: 'quiet', producer: 'fixture',
    created_at: Date.now(), payload: { observation: 'test only' }, workspace_key: workspaceKey(h.dir),
    agent_key: 'fixture-preset', origin: 'observation', control_allowed: false }
  const base = path.join(path.dirname(h.stateFile), 'decision-inbox')
  const key = createHash('sha256').update(event.scope + '\0' + event.event_id).digest('hex') + '.json'
  fs.mkdirSync(path.join(base, 'pending'), { recursive: true })
  fs.writeFileSync(path.join(base, 'pending', key), JSON.stringify(event) + '\n')
  h.bridge.start()
  await h.bridge.registry.mappingLoaded
  await waitUntil(() => h.bridge.chats.get(event.scope)?.messages.length === 1, 'pump dispatch')
  assert.equal(fs.existsSync(path.join(base, 'done', key)), false)
  const chat = h.bridge.chats.get(event.scope)
  const turn = h.start(chat)
  await h.decide(chat, 'idle')
  await h.end(chat, turn)
  await waitUntil(() => fs.existsSync(path.join(base, 'done', key)), 'durable done marker')
  assert.equal(fs.existsSync(path.join(base, 'pending', key)), true)
  assert.equal(JSON.parse(fs.readFileSync(path.join(base, 'done', key))).status, 'decided')
  await waitUntil(() => JSON.parse(fs.readFileSync(h.stateFile)).scopes[event.scope].observations.length === 0, 'pin acknowledgement')
})

test('memory CLI receives trusted scope/cwd/preset and never falls back to raw cross-chat text', async t => {
  const h = await setup(t)
  // The installed registry returns a Promise, including for its default preset.
  h.bridge.resolvePresetId = async () => 'fixture-preset'
  const script = path.join(h.dir, 'context-fixture.py')
  fs.writeFileSync(script, `import sys,json,os\nv=json.load(sys.stdin)\nassert set(v)=={'cwd','agent_preset','query','recent_events','conversation_key','limit'}\nw=os.path.abspath(v['cwd']);a=v['agent_preset'].strip()\nif os.name=='nt':w=w.lower();a=a.lower()\nprint(json.dumps({'schema':'dsh-memento-context/v1','read_only':True,'conversation_key':v['conversation_key'],'owner':{'workspace_key':w,'agent_key':a},'memories':[{'text':'scoped fixture'}]}))\n`)
  h.bridge.lanyaPolicy.config.memoryContextScript = script
  h.bridge.lanyaPolicy.memoryContext = (scope, text) => h.bridge.lanyaPolicy.readMemoryContext(scope, text)
  const chat = await h.receive({ text: 'memory question' })
  assert.match(chat.messages[0].content[0].text, /scoped fixture/)
  assert.match(chat.messages[0].content[0].text, /fixture-preset/)
  fs.writeFileSync(script, `import sys,json\nv=json.load(sys.stdin)\nprint(json.dumps({'schema':'dsh-memento-context/v1','read_only':True,'conversation_key':v['conversation_key'],'owner':{'workspace_key':'foreign-workspace','agent_key':'foreign-agent'},'memories':[{'text':'foreign fixture must never be injected'}]}))\n`)
  await h.receive({ text: 'same scope wrong owner' })
  assert.equal(chat.messages[1].content[0].text, 'same scope wrong owner')
  h.bridge.lanyaPolicy.memoryContext = async () => { throw new Error('fixture helper failure') }
  await h.receive({ text: 'plain safe fallback' })
  assert.equal(chat.messages[2].content[0].text, 'plain safe fallback')
  assert.equal(h.logs.some(item => item.message === 'scoped_memory_context_unavailable:Error'), true)
})

test('ordinary QQ and main-review share one agent and refresh its actual owner-bound capability guide', async t => {
  const h = await setup(t)
  const runtime = path.join(h.dir, 'napcat')
  fs.mkdirSync(path.join(runtime, 'config'), { recursive: true })
  for (const file of ['runtime_capabilities.py', 'project_conf.py']) fs.copyFileSync(path.join(repo, 'scripts', file), path.join(runtime, file))
  const projectConfig = path.join(runtime, 'config', 'project.json')
  const configuration = { dataDirName: 'FixtureRole', android: { serial: 'emulator-5556' }, mind: { opportunityMin: 3 } }
  fs.writeFileSync(projectConfig, JSON.stringify(configuration))
  h.bridge.lanyaPolicy.runtimeCapabilities = (scope, owner) => h.bridge.lanyaPolicy.readRuntimeCapabilities(scope, owner)
  const chat = await h.receive({ text: 'what tools are actually available?' })
  const agent = chat.agent
  const extract = message => JSON.parse(message.content[0].text.match(/<trusted_runtime_capabilities>\n(.*?)\n<\/trusted_runtime_capabilities>/s)[1])
  const first = extract(chat.messages[0])
  assert.equal(first.owner.workspace_key, workspaceKey(h.dir))
  assert.equal(first.owner.agent_key, 'fixture-preset')
  assert.equal(first.tools.android.deployment, 'not_installed')
  assert.equal(first.tools.android.worker, 'offline')
  assert.equal(first.tools.android.cached_screenshot_is_current, false)
  assert.ok(first.tools.android.protocol.action_example.expected_shot_id)
  assert.equal(first.tools.entertainment.cadence.configured_opportunity_min, 3)
  assert.equal(first.tools.entertainment.cadence.valid, true)
  const turn = h.start(chat)
  await h.decide(chat, 'idle')
  await h.end(chat, turn)
  // The installed adapter launches the real helper anew. A later main-review
  // must receive this owner's current cadence, even with an older payload.
  configuration.mind.opportunityMin = 5
  fs.writeFileSync(projectConfig, JSON.stringify(configuration))
  const completion = h.bridge.dispatchObservation({ version: 1, event_id: 'obs:capability-guide', scope: chat.chatId,
    kind: 'activity.opportunity', producer: 'main-review', created_at: Date.now(), origin: 'observation', control_allowed: false,
    workspace_key: workspaceKey(h.dir), agent_key: 'fixture-preset', payload: {
      cwd: 'foreign-payload-cwd', agent_preset: 'foreign-payload-preset', text: 'payload is not a tool permission',
      opportunityMin: 55, instruction: 'Historical immutable fixture guidance is not the live cadence.' } })
  await waitUntil(() => chat.messages.length === 2, 'capability-enriched background message')
  const second = extract(chat.messages[1])
  assert.equal(chat.agent, agent)
  assert.equal(second.owner.workspace_key, workspaceKey(h.dir))
  assert.equal(second.owner.agent_key, 'fixture-preset')
  assert.equal(second.conversation_key, chat.chatId)
  assert.equal(second.tools.entertainment.cadence.configured_opportunity_min, 5)
  assert.equal(second.tools.entertainment.cadence.valid, true)
  assert.equal(first.tools.entertainment.cadence.configured_opportunity_min, 3)
  assert.doesNotMatch(JSON.stringify(second), /foreign-payload/)
  const next = h.start(chat)
  await h.decide(chat, 'idle')
  await h.end(chat, next)
  assert.equal((await completion).status, 'decided')
  assert.equal(h.calls.length, 0)
})

test('capability helper wrong owner/scope fails closed without disabling scoped memory', async t => {
  const h = await setup(t)
  const script = path.join(h.dir, 'bad-capability-fixture.py')
  fs.writeFileSync(script, `import json,sys\nv=json.load(sys.stdin)\nprint(json.dumps({'schema':'dsh-runtime-capabilities/v1','read_only':True,'conversation_key':v['conversation_key'],'owner':{'workspace_key':'foreign-fixture','agent_key':'foreign-preset'},'tools':{'secret':'must not inject this'}}))\n`)
  h.bridge.lanyaPolicy.config.runtimeCapabilitiesScript = script
  h.bridge.lanyaPolicy.runtimeCapabilities = (scope, owner) => h.bridge.lanyaPolicy.readRuntimeCapabilities(scope, owner)
  const chat = await h.receive()
  assert.match(chat.messages[0].content[0].text, /scoped_memory_context/)
  assert.match(chat.messages[0].content[0].text, /capability status is unavailable/)
  assert.doesNotMatch(chat.messages[0].content[0].text, /must not inject this|foreign-fixture/)
  assert.ok(h.logs.some(item => item.message === 'runtime_capabilities_unavailable:Error'))
  assert.equal(h.calls.length, 0)
})

function voiceFixture(h, mode = 'ready', receiptOverrides = {}) {
  const script = path.join(h.dir, 'voice-fixture.py')
  fs.writeFileSync(script, `import json,sys,os,pathlib,hashlib,wave\nv=json.load(sys.stdin)\nassert set(v).issubset({'cwd','agent_preset','conversation_key','request_id','text','speed'})\nroot=pathlib.Path(v['cwd']);w=os.path.abspath(v['cwd']);a=v['agent_preset'].strip()\nif os.name=='nt':w=w.lower();a=a.lower()\ncalls=root/'voice-fixture-calls.json'\nitems=json.loads(calls.read_text()) if calls.exists() else []\nitems.append(v);calls.write_text(json.dumps(items))\nf=root/'fixture-audio.wav'\nwith wave.open(str(f),'wb') as out:\n out.setnchannels(1);out.setsampwidth(2);out.setframerate(8000);out.writeframes(b'\\0\\0'*800)\nb=f.read_bytes()\nr={'schema':'dsh-voice-result/v1','owner':{'workspace_key':w,'agent_key':a},'conversation_key':v['conversation_key'],'request_id':v['request_id'],'status':'ready','delivered':False,'file':str(f),'sha256':hashlib.sha256(b).hexdigest(),'bytes':len(b),'duration_seconds':0.1}\nmode=${JSON.stringify(mode)}\nif mode=='foreign':r['owner']['agent_key']='foreign-fixture'\nif mode=='delivered':r['delivered']=True\nif mode=='wrong-request':r['request_id']='wrong-fixture-request'\nif mode=='bad-hash':r['sha256']='f'*64\nif mode=='unknown':r={'schema':r['schema'],'owner':r['owner'],'conversation_key':r['conversation_key'],'request_id':r['request_id'],'status':'unknown','delivered':False,'code':'fixture_unknown'}\nprint(json.dumps(r))\n`)
  h.bridge.lanyaPolicy.config.voiceExecutorScript = script
  if (Object.keys(receiptOverrides).length) fs.writeFileSync(script, fs.readFileSync(script, 'utf8').replace(
    'print(json.dumps(r))', 'r.update(json.loads(' + JSON.stringify(JSON.stringify(receiptOverrides)) + '))\nprint(json.dumps(r))'))
  return () => JSON.parse(fs.readFileSync(path.join(h.dir, 'voice-fixture-calls.json'), 'utf8'))
}

test('same main agent receives a real helper fixture audio result; synthesis itself is never QQ delivery', async t => {
  const h = await setup(t), requests = voiceFixture(h)
  const chat = await h.receive()
  const turn = h.start(chat)
  await h.decide(chat, 'tool_action')
  const voiceExec = h.exec(chat, 'fixture-voice-call')
  const result = await h.tools.get('qq_synthesize_voice').execute({ text: 'neutral audio fixture', speed: 1 }, voiceExec)
  assert.equal(voiceExec.agent, chat.agent)
  assert.equal(result.status, 'ready')
  assert.equal(result.delivered, false)
  assert.ok(fs.existsSync(result.file))
  assert.equal(result.sha256, createHash('sha256').update(fs.readFileSync(result.file)).digest('hex'))
  const request = requests()[0]
  assert.equal(request.cwd, h.dir)
  assert.equal(request.agent_preset, 'fixture-preset')
  assert.equal(request.conversation_key, chat.chatId)
  assert.equal(h.calls.length, 0)
  assert.equal((await h.bridge.lanyaPolicy.policy.snapshot(chat.chatId)).deliveries.length, 0)
  await assert.rejects(() => h.tools.get('qq_send_voice').execute({ path: result.file }, h.exec(chat)),
    error => error.reason === 'not_a_speech_ticket')
  await h.end(chat, turn)
})

test('reply synthesis produces only an artifact; newer silence suppresses the separately attempted QQ send', async t => {
  const h = await setup(t)
  voiceFixture(h)
  const chat = await h.receive()
  h.start(chat)
  await h.decide(chat)
  const result = await h.tools.get('qq_synthesize_voice').execute({ text: 'a local fixture result' }, h.exec(chat))
  assert.equal(result.delivered, false)
  assert.equal(h.calls.length, 0)
  await h.receive({ text: 'new authorized silence request' })
  h.start(chat)
  await h.decide(chat, 'silence', { control_update: true })
  await assert.rejects(() => h.tools.get('qq_send_voice').execute({ path: result.file }, h.exec(chat)),
    error => error.reason === 'decision_required')
  assert.equal(h.calls.length, 0)
})

test('voice binds ticket before asynchronous owner lookup and rejects stale or model-controlled authority', async t => {
  const h = await setup(t)
  voiceFixture(h)
  const chat = await h.receive()
  h.start(chat)
  await h.decide(chat)
  for (const args of [{ text: 'fixture', cwd: 'foreign' }, { text: 'fixture', speed: true }, { text: 'fixture', speed: 9 }]) {
    await assert.rejects(() => h.bridge.lanyaPolicy.synthesizeVoice(args, h.exec(chat)), /accepts text/)
  }
  const gate = deferred(), entered = deferred()
  h.bridge.resolvePresetId = async () => { entered.resolve(); return gate.promise }
  const pending = h.bridge.lanyaPolicy.synthesizeVoice({ text: 'stale fixture' }, h.exec(chat))
  await entered.promise
  // Direct policy inbound models the intervening authoritative revision without
  // blocking its own guide enrichment on the deliberately paused registry.
  await h.bridge.lanyaPolicy.policy.recordInbound({ scope: chat.chatId, messageId: 'fixture-new-revision',
    sender: '101', origin: 'live', quoted: false, controlAllowed: true })
  gate.resolve('fixture-preset')
  await assert.rejects(pending, error => error.reason === 'stale_revision')
  assert.equal(fs.existsSync(path.join(h.dir, 'voice-fixture-calls.json')), false)
  assert.equal(h.calls.length, 0)
})

test('voice receipts validate owner, request, no-delivery and actual bytes; unknown is returned without retry', async t => {
  const h = await setup(t)
  const chat = await h.receive()
  h.start(chat)
  await h.decide(chat, 'tool_action')
  for (const mode of ['foreign', 'delivered', 'wrong-request', 'bad-hash']) {
    voiceFixture(h, mode)
    await assert.rejects(() => h.bridge.lanyaPolicy.synthesizeVoice({ text: 'fixture' }, h.exec(chat)), /Invalid|changed/)
  }
  const requests = voiceFixture(h, 'unknown')
  const before = requests().length
  const result = await h.bridge.lanyaPolicy.synthesizeVoice({ text: 'fixture' }, h.exec(chat, 'unknown-voice-call'))
  assert.equal(result.status, 'unknown')
  assert.equal(result.delivered, false)
  assert.equal(requests().length, before + 1)
  assert.equal(h.calls.length, 0)
})

test('voice receipt bounds match the executor limit of 20 MiB and 120 seconds', async t => {
  const h = await setup(t)
  const chat = await h.receive()
  h.start(chat)
  await h.decide(chat, 'tool_action')
  for (const receipt of [{ bytes: 20 * 1024 * 1024 + 1 }, { duration_seconds: 120.01 }]) {
    voiceFixture(h, 'ready', receipt)
    await assert.rejects(() => h.bridge.lanyaPolicy.synthesizeVoice({ text: 'bounded fixture' }, h.exec(chat)), /Invalid voice synthesis artifact/)
  }
  voiceFixture(h, 'ready', { duration_seconds: 120 })
  assert.equal((await h.bridge.lanyaPolicy.synthesizeVoice({ text: 'boundary fixture' }, h.exec(chat))).duration_seconds, 120)
  assert.equal(h.calls.length, 0)
})

test('registered synthesis tool uses the production executor with inert PCM and durable same-call reuse', async t => {
  const h = await setup(t)
  const runtime = path.join(h.dir, 'napcat'), ref = path.join(h.dir, 'tts', 'ref', 'ref-main.wav')
  fs.mkdirSync(path.join(runtime, 'config'), { recursive: true })
  fs.mkdirSync(path.dirname(ref), { recursive: true })
  fs.writeFileSync(path.join(runtime, 'config', 'project.json'), JSON.stringify({ dataDirName: 'FixtureRole' }))
  const wav = Buffer.alloc(44 + 3200)
  const sampleRate = 16 * 1000
  wav.write('RIFF', 0); wav.writeUInt32LE(wav.length - 8, 4); wav.write('WAVEfmt ', 8)
  wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22)
  wav.writeUInt32LE(sampleRate, 24); wav.writeUInt32LE(sampleRate * 2, 28); wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34)
  wav.write('data', 36); wav.writeUInt32LE(3200, 40)
  fs.writeFileSync(ref, wav)
  const script = path.join(runtime, 'voice-executor-fixture.py')
  fs.writeFileSync(script, `import sys,json,pathlib\nsys.path.insert(0,${JSON.stringify(path.join(repo, 'scripts'))})\nimport voice_executor as voice\nr=json.load(sys.stdin);root=pathlib.Path(r['cwd'])\ndef inert_fetch(port,payload):\n p=root/'inert-tts-call-count';p.write_text(str(int(p.read_text())+1) if p.exists() else '1');return (root/'tts/ref/ref-main.wav').read_bytes()\nprint(json.dumps(voice.synthesize(r,fetch=inert_fetch)))\n`)
  h.bridge.lanyaPolicy.config.voiceExecutorScript = script
  const chat = await h.receive()
  const turn = h.start(chat)
  await h.decide(chat)
  const tool = h.tools.get('qq_synthesize_voice')
  assert.ok(tool, 'the actual replay patch registers the synthesis tool')
  const exec = h.exec(chat, 'production-executor-fixture-call')
  const result = await tool.execute({ text: 'neutral fixture audio' }, exec)
  assert.equal(exec.agent, chat.agent)
  assert.equal(result.status, 'ready')
  assert.equal(result.delivered, false)
  assert.deepEqual(await tool.execute({ text: 'neutral fixture audio' }, exec), result)
  assert.equal(fs.readFileSync(path.join(h.dir, 'inert-tts-call-count'), 'utf8'), '1')
  assert.equal(h.calls.length, 0)
  assert.equal((await h.bridge.lanyaPolicy.policy.snapshot(chat.chatId)).deliveries.length, 0)
  const delivery = await h.tools.get('qq_send_voice').execute({ path: result.file }, h.exec(chat))
  assert.equal(delivery.sent, true)
  assert.equal(h.calls.length, 1)
  assert.equal(h.calls[0].action, 'send_msg')
  assert.deepEqual(h.calls[0].params.message, [{ type: 'record', data: { file: result.file } }])
  await h.end(chat, turn)
})

test('voice subprocess follows the invoking AbortSignal and is not automatically retried', async t => {
  const h = await setup(t)
  const script = path.join(h.dir, 'aborted-voice-fixture.py'), marker = path.join(h.dir, 'aborted-voice-entered')
  fs.writeFileSync(script, `import json,sys,pathlib,time\nr=json.load(sys.stdin)\n(pathlib.Path(r['cwd'])/'aborted-voice-entered').write_text('one inert attempt')\ntime.sleep(60)\n`)
  h.bridge.lanyaPolicy.config.voiceExecutorScript = script
  const chat = await h.receive()
  h.start(chat)
  await h.decide(chat, 'tool_action')
  const controller = new AbortController(), exec = { ...h.exec(chat), signal: controller.signal }
  const pending = h.bridge.lanyaPolicy.synthesizeVoice({ text: 'abort fixture' }, exec)
  await waitUntil(() => fs.existsSync(marker), 'inert voice subprocess')
  controller.abort()
  await assert.rejects(pending, error => error.name === 'AbortError')
  assert.equal(fs.readFileSync(marker, 'utf8'), 'one inert attempt')
  assert.equal(h.calls.length, 0)
})

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
    created_at: Date.now(), payload: { text: 'not a real user resume command' }, workspace_key: h.dir,
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

test('background evidence cannot revoke queued or currently sending real-user turns', async t => {
  const h = await setup(t)
  const chat = await h.receive()
  const event = { version: 1, event_id: 'obs:foreground', scope: 'private:101', kind: 'quiet', producer: 'fixture',
    created_at: Date.now(), payload: {}, workspace_key: h.dir, agent_key: 'fixture-preset', origin: 'observation', control_allowed: false }
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
    created_at: Date.now(), payload: {}, workspace_key: h.dir, agent_key: '', origin: 'observation', control_allowed: false }
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
    created_at: Date.now(), payload: { observation: 'test only' }, workspace_key: h.dir,
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

/** Real policy/adapter/queue on disposable files; native notifications and
 * transport are inert seams. No provider, QQ or running workspace is touched. */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createHash } from 'node:crypto'
import { OneBotDecisionAdapter } from '../plugin/lib/onebot-policy-adapter.js'
import { autonomousRestState } from '../plugin/lib/decision-policy.js'
import { observationKey, readObservationEvidence } from '../plugin/lib/observation-queue.js'

const DAY = Date.parse('2026-11-03T04:00:00Z')
const MIDNIGHT = Date.parse('2026-11-03T16:00:00Z')
const WAKE = MIDNIGHT + 8 * 60 * 60 * 1000
const scope = 'private:101'
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
function fixture(t, at = DAY) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-rest-'))
  let clock = at, dispatches = 0, transports = 0, turn = 0
  const chat = { chatId: scope, sessionId: 'synthetic-session', agent: { session: { id: 'synthetic-session' } } }
  const owner = { cwd: root, preset: 'synthetic-preset', workspace_key: 'synthetic-workspace', agent_key: 'synthetic-agent' }
  const bridge = { stopping: false, chats: new Map([[scope, chat]]), chatForSession: id => id === chat.agent.session.id ? scope : undefined,
    installChannelScope() {}, interim: { onTurnStart() {}, onTurnEnd() {} },
    dispatchFollowup: async () => { dispatches += 1 } }
  const adapter = new OneBotDecisionAdapter(bridge, { policy: { adminUsers: [] }, config: {}, log() {} },
    { stateFile: path.join(root, 'policy.json'), now: () => clock, memoryContext: async () => null, runtimeCapabilities: async () => null })
  adapter.trustedOwner = async () => owner
  const hooks = new Map(), guards = []
  const ctx = { on(name, hook) { hooks.set(name, hook) }, tools: { guard(fn) { guards.push(fn); return () => {} } },
    systemPrompt: { section() { return () => {} } } }
  adapter.installNativeScope(ctx)
  chat.agent.ctx = ctx
  t.after(async () => {
    await adapter.stop()
    assert.ok(path.resolve(root).startsWith(path.join(os.tmpdir(), 'lanya-rest-')))
    fs.rmSync(root, { recursive: true, force: true })
  })
  const event = (id, extra = {}) => ({ version: 1, event_id: id, scope, kind: 'activity.opportunity', producer: 'mind',
    created_at: clock - 1, payload: { text: 'synthetic evidence only' }, workspace_key: owner.workspace_key, agent_key: owner.agent_key,
    origin: 'observation', control_allowed: false, ...extra })
  const start = async (value, origin = 'observation') => {
    const observed = origin === 'live'
      ? await adapter.policy.recordInbound({ scope, messageId: value.event_id, sender: '101', origin: 'live', controlAllowed: false })
      : await adapter.policy.recordObservation({ scope, eventId: value.event_id, kind: value.kind, producer: value.producer, createdAt: value.created_at })
    const completion = deferred(), binding = Object.freeze({ ...observed, origin, owner,
      ...(origin === 'observation' ? { observationKind: value.kind, observationProducer: value.producer,
        observationEventId: value.event_id, observationCreatedAt: value.created_at, completion } : {}) })
    adapter.context.run({ binding }, () => adapter.queueTurn(chat))
    adapter.onTurnStart(chat, ++turn)
    const record = adapter.state(chat).active
    return { record, completion, exec: { agent: chat.agent, signal: new AbortController().signal, name: 'bili_reply', callId: 'synthetic-call' } }
  }
  return { root, adapter, bridge, chat, owner, hooks, guards, event, start,
    clock: () => clock, at: time => { clock = time }, counts: () => ({ dispatches, transports }),
    transmit: async () => { transports += 1; return { message_id: 'synthetic-receipt' } } }
}

test('Shanghai rest has exact midnight/wake boundaries independent of host timezone', () => {
  assert.equal(autonomousRestState(MIDNIGHT - 1).resting, false)
  assert.equal(autonomousRestState(MIDNIGHT).resting, true)
  assert.equal(autonomousRestState(WAKE - 1).resting, true)
  assert.equal(autonomousRestState(WAKE).resting, false)
  assert.equal(autonomousRestState(MIDNIGHT).next_wake_at, WAKE)
  assert.equal(autonomousRestState(WAKE).current_day_wake_at, WAKE)
})

test('all queued observation kinds close locally at night without a model or revoking a live ticket', async t => {
  const h = fixture(t, MIDNIGHT), policy = h.adapter.policy
  const live = await policy.recordInbound({ scope, messageId: 'real-live-fixture', sender: '101', origin: 'live', controlAllowed: false })
  const reply = await policy.decide({ scope, messageId: live.messageId, expectedRevision: live.revision, verdict: 'reply' })
  const before = await policy.snapshot(scope)
  for (const [index, kind] of ['activity.opportunity', 'reminder.due', 'social.notification', 'entertainment.review'].entries()) {
    const value = h.event('night-' + index, { kind, producer: 'synthetic-producer' })
    const inbox = path.join(h.root, 'decision-inbox'), name = observationKey(scope, value.event_id) + '.json'
    fs.mkdirSync(path.join(inbox, 'pending'), { recursive: true })
    fs.writeFileSync(path.join(inbox, 'pending', name), JSON.stringify(value) + '\n')
  }
  assert.equal(await h.adapter.pump.start({ poll: false }), true)
  assert.equal((await h.adapter.pump.drain()).processed, 4)
  for (let index = 0; index < 4; index += 1) {
    const read = readObservationEvidence(path.join(h.root, 'decision-inbox'), observationKey(scope, 'night-' + index))
    assert.equal(read.completion.reason, 'autonomy_rest')
    assert.equal(read.completion.native_turn_completed, false)
    assert.equal('decision_id' in read.completion, false)
  }
  const after = await policy.snapshot(scope)
  assert.equal(after.revision, before.revision)
  assert.deepEqual(after.current, before.current)
  assert.equal((await policy.check(reply.ticket)).allowed, true)
  assert.deepEqual(h.counts(), { dispatches: 0, transports: 0 })
  h.at(WAKE)
  assert.equal((await h.adapter.pump.drain()).processed, 0)
  assert.equal(h.counts().dispatches, 0)
})

test('08:00 closes pre-wake greetings rather than replaying the nighttime backlog', async t => {
  const h = fixture(t, WAKE)
  const result = await h.adapter.dispatchObservation(h.event('stale-greeting', { created_at: MIDNIGHT - 1 }))
  assert.equal(result.completed, true)
  assert.equal(result.reason, 'observation_expired')
  assert.equal(result.native_turn_completed, false)
  assert.equal(h.counts().dispatches, 0)
})

test('a fresh-looking queue review at 08:00 cannot renew a previous-day greeting', async t => {
  const h = fixture(t, WAKE), inbox = path.join(h.root, 'decision-inbox')
  for (const sub of ['pending', 'done']) fs.mkdirSync(path.join(inbox, sub), { recursive: true })
  const first = h.event('old-real-opportunity', { created_at: MIDNIGHT - 1 }), raw = JSON.stringify(first) + '\n'
  const key = observationKey(scope, first.event_id), hash = createHash('sha256').update(raw).digest('hex')
  fs.writeFileSync(path.join(inbox, 'done', key + '.json'), JSON.stringify({ version: 2, event_raw: raw,
    event_id: first.event_id, scope, event_sha256: hash, completed_at: MIDNIGHT - 1,
    status: 'decided', decision_id: 'prior-idle', next_review_at: WAKE }) + '\n')
  const next = { ...first, event_id: 'main-review:' + key + ':' + WAKE, producer: 'main-review', created_at: WAKE,
    payload: { review_context: { parent_observation_key: key, parent_event_id: first.event_id,
      previous_decision_id: 'prior-idle', scheduled_at: WAKE, freshness_anchor: { key, sha256: hash, created_at: first.created_at } } } }
  const result = await h.adapter.dispatchObservation(next)
  assert.equal(result.reason, 'observation_expired')
  assert.deepEqual(h.counts(), { dispatches: 0, transports: 0 })
  next.payload.review_context.freshness_anchor.sha256 = '0'.repeat(64)
  await assert.rejects(h.adapter.dispatchObservation(next), /freshness/)
})

test('quiet-hour closure still verifies the actual scope owner', async t => {
  const h = fixture(t, MIDNIGHT)
  await assert.rejects(h.adapter.dispatchObservation(h.event('foreign', { workspace_key: 'foreign-owner' })), /owner mismatch/)
  assert.equal(await h.adapter.policy.snapshot(scope), null)
  assert.equal(h.counts().dispatches, 0)
})

test('an 81-link legacy review can expire without replaying old activity or rewriting its evidence', async t => {
  const h = fixture(t, WAKE), inbox = path.join(h.root, 'decision-inbox')
  for (const sub of ['pending','done']) fs.mkdirSync(path.join(inbox, sub), {recursive:true})
  let previous, last
  const originals = new Map()
  for (let i=0;i<82;i++) {
    const at=WAKE-100000+i, value=i===0 ? h.event('legacy-root',{created_at:at}) : {
      ...h.event('unused',{created_at:at}), producer:'main-review',
      event_id:'main-review:'+previous.key+':'+at,
      payload:{review_context:{parent_observation_key:previous.key,parent_event_id:previous.value.event_id,
        previous_decision_id:'legacy-decision-'+(i-1),scheduled_at:at}} }
    const raw=JSON.stringify(value)+'\n', key=observationKey(scope,value.event_id)
    const file=path.join(inbox,'pending',key+'.json');fs.writeFileSync(file,raw);originals.set(file,raw)
    if(i<81) {
      const marker=JSON.stringify({version:2,event_raw:raw,event_id:value.event_id,scope,
        event_sha256:createHash('sha256').update(raw).digest('hex'),completed_at:at,
        status:'decided',decision_id:'legacy-decision-'+i,next_review_at:at+1})+'\n'
      const done=path.join(inbox,'done',key+'.json');fs.writeFileSync(done,marker);originals.set(done,marker)
    }
    previous={key,value};last=value
  }
  const result=await h.adapter.dispatchObservation(last)
  assert.equal(result.reason,'observation_expired')
  assert.equal(result.native_turn_completed,false)
  assert.deepEqual(h.counts(),{dispatches:0,transports:0})
  for(const [file,raw] of originals) assert.equal(fs.readFileSync(file,'utf8'),raw)
})

test('the synchronous native tool guard denies new autonomous tools at midnight even without a budget gate', async t => {
  const h = fixture(t, MIDNIGHT - 1), value = h.event('active-tools'), active = await h.start(value)
  assert.equal(h.guards[0](active.exec), undefined)
  h.at(MIDNIGHT)
  assert.equal(h.guards[0](active.exec), 'autonomy_rest')
  assert.throws(() => h.adapter.captureExec(active.exec), error => error.reason === 'autonomy_rest')
  const live = await h.start(h.event('fresh-human'), 'live')
  assert.equal(h.guards[0](live.exec), undefined)
})

test('an admitted step crossing midnight ends with a truthful interrupted local receipt, not an invented idle decision', async t => {
  const h = fixture(t, MIDNIGHT - 1), active = await h.start(h.event('crossing-step'))
  const request = { agent: h.chat.agent, turn: active.record.turn, step: 1, signal: active.exec.signal }
  assert.equal((await h.hooks.get('agent/pre-step')(request, async () => ({ kind: 'enter' }))).kind, 'enter')
  assert.equal(active.record.nativeStepEntered, true)
  h.at(MIDNIGHT)
  assert.deepEqual(await h.hooks.get('agent/pre-step')({ ...request, step: 2 }, () => assert.fail('new model step')), { kind: 'reject', reason: 'autonomy_rest' })
  h.adapter.onTurnEnd(scope, h.chat, { seq: 3, data: { turn: active.record.turn, reason: { kind: 'blocked' } } })
  const result = await active.completion.promise
  assert.equal(result.reason, 'autonomy_rest')
  assert.equal(result.native_turn_completed, false)
  assert.equal('decision_id' in result, false)
  assert.equal(h.counts().transports, 0)
})

test('a real 07:59 rest interruption remains valid when local settlement finishes after 08:00', async t => {
  const h = fixture(t, MIDNIGHT - 1), active = await h.start(h.event('delayed-settlement'))
  h.at(WAKE - 1)
  const request = { agent: h.chat.agent, turn: active.record.turn, step: 2, signal: active.exec.signal }
  assert.equal((await h.hooks.get('agent/pre-step')(request, () => assert.fail('new model step'))).reason, 'autonomy_rest')
  h.at(WAKE)
  h.adapter.onTurnEnd(scope, h.chat, { seq: 3, data: { turn: active.record.turn, reason: { kind: 'blocked' } } })
  const result = await active.completion.promise
  assert.equal(result.reason, 'autonomy_rest')
  assert.equal(result.native_turn_completed, false)
  assert.equal(result.completed_at, WAKE)
})

test('a same-event dispatch already in flight joins its original promise across midnight instead of closing it early', async t => {
  const h = fixture(t, MIDNIGHT - 1), value = h.event('one-owned-event'), queued = deferred()
  h.bridge.dispatchFollowup = async () => { queued.resolve() }
  const original = h.adapter.dispatchObservation(value)
  await queued.promise
  const before = await h.adapter.policy.snapshot(scope)
  h.at(MIDNIGHT)
  const joined = h.adapter.dispatchObservation(value)
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(await h.adapter.policy.snapshot(scope), before)
  let settled = false; joined.then(() => { settled = true }, () => { settled = true })
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(settled, false)
  for (const pending of h.adapter.observations.values()) pending.resolve({ completed: true, status: 'unknown', decision_id: 'synthetic-existing-outcome' })
  assert.equal((await original).status, 'unknown')
  assert.equal((await joined).status, 'unknown')
})

test('a reply authorized before midnight cannot begin QQ transport after the final guard time changes', async t => {
  const h = fixture(t, MIDNIGHT - 1), active = await h.start(h.event('outbound-crossing'))
  const result = await h.adapter.decide({ verdict: 'reply' }, active.exec)
  assert.equal(result.can_send, true)
  const envelope = h.adapter.envelope(active.record, 'synthetic-output')
  const actual = h.adapter.policy.runGuarded
  h.adapter.policy.runGuarded = (...args) => { h.at(MIDNIGHT); return actual(...args) }
  await assert.rejects(h.adapter.context.run({ envelope }, () => h.adapter.guardConnectionCall('send_msg',
    { message_type: 'private', user_id: '101', message: [{ type: 'text', data: { text: 'synthetic output' } }] }, scope, h.transmit)),
  error => error.reason === 'autonomy_rest')
  assert.equal(h.counts().transports, 0)
})

test('reply receipt reports the persistent unanswered gate instead of claiming the ticket can send', async t => {
  const h = fixture(t, DAY), first = await h.start(h.event('first-contact'))
  assert.equal((await h.adapter.decide({ verdict: 'reply' }, first.exec)).can_send, true)
  assert.equal((await h.adapter.policy.runGuarded(first.record.ticket,
    { scope, operationId: 'synthetic-contact', autonomous: true }, async () => ({ ok: true }))).status, 'sent')
  h.at(DAY + 61 * 60 * 1000)
  const second = await h.start(h.event('unanswered-contact'))
  const result = await h.adapter.decide({ verdict: 'reply', expression: 'meme' }, second.exec)
  assert.equal(result.accepted, true)
  assert.equal(result.can_send, false)
  assert.equal(result.reason, 'proactive_contact_unanswered')
  assert.equal('expression_plan' in result, false)
  assert.equal(h.counts().transports, 0)
})

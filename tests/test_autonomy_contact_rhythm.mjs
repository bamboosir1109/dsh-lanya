import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { autonomousRestState, createDecisionPolicy } from '../plugin/lib/decision-policy.js'

const hour = 3600_000
const local = text => Date.parse('2026-11-03T' + text + '+08:00')
const privateScope = 'private:101'
function fixture(t, initial = local('09:00:00'), options = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-contact-rhythm-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const file = path.join(directory, 'policy.json')
  let at = initial, serial = 0, calls = 0
  const make = () => createDecisionPolicy({ stateFile: file, now: () => at, maxInbound: 1, ...options })
  const policy = make()
  const metadata = (id, scope = privateScope, createdAt = at) => ({ scope, eventId: id,
    kind: 'activity.opportunity', producer: 'mind', createdAt })
  async function observed(id, scope = privateScope) {
    const admitted = await policy.recordObservation(metadata(id, scope))
    assert.equal(admitted.accepted, true)
    const decided = await policy.decide({ scope, messageId: id, expectedRevision: admitted.revision, verdict: 'reply' })
    return decided.ticket
  }
  async function live(id, scope = privateScope) {
    const admitted = await policy.recordInbound({ scope, messageId: id, sender: scope.slice(8), controlAllowed: true, origin: 'live' })
    assert.equal(admitted.accepted, true)
    return (await policy.decide({ scope, messageId: id, expectedRevision: admitted.revision, verdict: 'reply' })).ticket
  }
  const send = (ticket, transport = () => { calls++; return { message_id: 'fixture-' + calls } }) =>
    policy.runGuarded(ticket, { scope: ticket.scope, kind: 'text', operationId: 'operation-' + ++serial }, transport)
  return { policy, make, metadata, observed, live, send, time: value => { at = value }, calls: () => calls }
}

test('Shanghai rest includes midnight and excludes 08:00 independently of host timezone', () => {
  for (const [time, resting] of [['00:00:00', true], ['07:59:59.999', true], ['08:00:00', false], ['23:59:59.999', false]]) {
    const value = autonomousRestState(local(time))
    assert.equal(value.resting, resting)
    assert.equal(value.timezone, 'Asia/Shanghai')
    assert.equal(value.current_day_wake_at, local('08:00:00'))
    assert.ok(value.next_wake_at > local(time))
  }
  for (const invalid of [NaN, Infinity, -1, '0', 0.1]) assert.throws(() => autonomousRestState(invalid))
})

test('a model reply decision is not contact; all acknowledged parts of one contact share one interval', async t => {
  const f = fixture(t), ticket = await f.observed('first')
  assert.equal((await f.policy.proactiveStatus(privateScope)).last_contact_at, null)
  for (let index = 0; index < 3; index++) assert.equal((await f.send(ticket)).status, 'sent')
  assert.equal(f.calls(), 3)
  const state = await f.policy.snapshot(privateScope)
  assert.equal(state.proactiveContact.at, local('09:00:00'))
  assert.equal(new Set(state.deliveries.map(item => item.ticketId)).size, 1)
})

test('active private conversation can reply freely but a new autonomous contact waits a full hour', async t => {
  const f = fixture(t)
  await f.send(await f.observed('contact'))
  f.time(local('09:10:00'))
  for (let index = 0; index < 3; index++) assert.equal((await f.send(await f.live('answer-' + index))).status, 'sent')
  const tooSoon = await f.observed('too-soon')
  assert.equal((await f.send(tooSoon)).reason, 'proactive_contact_interval')
  f.time(local('10:00:00'))
  assert.equal((await f.send(await f.observed('hour-later'))).status, 'sent')
  assert.equal(f.calls(), 5)
})

test('no human reply means no further autonomous contact even after the hour or a restart', async t => {
  const f = fixture(t)
  await f.send(await f.observed('contact'))
  f.time(local('11:00:00'))
  assert.equal((await f.send(await f.observed('still-unanswered'))).reason, 'proactive_contact_unanswered')
  const restarted = f.make()
  assert.equal((await restarted.proactiveStatus(privateScope)).waiting_for_live_reply, true)
  await f.live('real-new-user-message')
  assert.equal((await restarted.proactiveStatus(privateScope)).waiting_for_live_reply, false)
  assert.equal((await f.send(await f.observed('after-real-answer'))).status, 'sent')
  assert.equal(f.calls(), 2)
})

test('explicit personal autonomy permits a new topic after an hour without imposing a daily quota', async t => {
  const f=fixture(t,local('09:00:00'),{autonomousPrivateScope:privateScope})
  await f.send(await f.observed('first-topic'))
  f.time(local('09:59:59.999'))
  assert.equal((await f.send(await f.observed('too-soon'))).reason,'proactive_contact_interval')
  f.time(local('10:00:00'))
  const status=await f.make().proactiveStatus(privateScope)
  assert.equal(status.allowed,true)
  assert.equal(status.wait_for_live_reply,false)
  assert.equal(status.waiting_for_live_reply,true,'absence of a human reply remains factual context')
  assert.equal(status.contact_choice,'main_llm_no_daily_quota')
  assert.equal(f.calls(),1,'a timer or status query never sends anything')
  assert.equal((await f.send(await f.observed('fresh-new-topic'))).status,'sent')
  await f.send(await f.observed('other-person-first','private:303'))
  f.time(local('11:00:00'))
  assert.equal((await f.send(await f.observed('other-person-second','private:303'))).reason,'proactive_contact_unanswered')
  assert.equal((await f.send(await f.live('actual-live-answer'))).status,'sent')
})

test('personal autonomous choice preserves uncertain receipts and still rests at midnight', async t => {
  const f=fixture(t,local('09:00:00'),{autonomousPrivateScope:privateScope})
  assert.equal((await f.send(await f.observed('uncertain'),()=>{throw new Error('unknown')})).status,'unknown')
  const original=(await f.policy.snapshot(privateScope)).deliveries[0]
  f.time(local('10:00:00'))
  assert.equal((await f.send(await f.observed('different-new-topic'))).status,'sent')
  assert.deepEqual((await f.policy.snapshot(privateScope)).deliveries[0],original)
  f.time(local('23:59:59.999'));const ticket=await f.observed('late-opportunity')
  f.time(Date.parse('2026-11-04T00:00:00+08:00'))
  assert.equal((await f.send(ticket)).reason,'autonomy_rest')
  assert.throws(()=>createDecisionPolicy({stateFile:'synthetic',autonomousPrivateScope:'group:202'}))
  assert.throws(()=>createDecisionPolicy({stateFile:'synthetic',autonomousPrivateScope:true}))
})

test('unknown first contact retains the interval and unanswered state without rewriting its receipt', async t => {
  const f = fixture(t), ticket = await f.observed('uncertain-contact')
  assert.equal((await f.send(ticket, () => { throw new Error('synthetic uncertain delivery') })).status, 'unknown')
  const before = (await f.policy.snapshot(privateScope)).deliveries
  f.time(local('11:00:00'))
  assert.equal((await f.send(await f.observed('no-compensation'))).reason, 'proactive_contact_unanswered')
  assert.deepEqual((await f.policy.snapshot(privateScope)).deliveries, before)
})

test('legacy deliveries supply only a conservative first interval, not invented autonomous or unread status', async t => {
  const f = fixture(t)
  await f.send(await f.live('legacy-shaped-live-contact'))
  const status = await f.policy.proactiveStatus(privateScope)
  assert.equal(status.history_basis, 'legacy_delivery_time_only')
  assert.equal(status.waiting_for_live_reply, false)
  f.time(local('09:59:59.999'))
  assert.equal((await f.send(await f.observed('before-first-hour'))).reason, 'proactive_contact_interval')
  f.time(local('10:00:00'))
  assert.equal((await f.send(await f.observed('first-known-autonomous'))).status, 'sent')
})

test('observation traffic never counts as a human answer and groups retain their own daytime activity', async t => {
  const f = fixture(t)
  await f.send(await f.observed('private-contact'))
  for (let index = 0; index < 5; index++) assert.equal((await f.send(await f.observed('group-' + index, 'group:202'))).status, 'sent')
  f.time(local('11:00:00'))
  assert.equal((await f.send(await f.observed('another-observation'))).reason, 'proactive_contact_unanswered')
  await f.live('first-real-answer'); await f.live('second-real-answer')
  assert.equal((await f.send(await f.observed('after-seen-lru'))).status, 'sent')
  assert.equal((await f.policy.snapshot(privateScope)).lastLiveRevision > 0, true)
})

test('a ticket crossing midnight cannot start autonomous output or action; actual live questions still reply', async t => {
  const f = fixture(t, local('23:59:59.999')), ticket = await f.observed('near-midnight', 'group:202')
  f.time(Date.parse('2026-11-04T00:00:00+08:00'))
  for (const kind of ['text', 'media', 'forward', 'api', 'tool_action']) {
    assert.equal((await f.policy.check(ticket, { scope: 'group:202', kind })).reason, 'autonomy_rest')
  }
  assert.equal((await f.send(ticket)).reason, 'autonomy_rest')
  assert.equal((await f.send(await f.live('a-real-night-question'))).status, 'sent')
})

test('rest-window local completion preserves a foreground live ticket and acknowledges an event-local revision', async t => {
  const f = fixture(t, local('01:00:00')), ticket = await f.live('night-human-question')
  const before = await f.policy.snapshot(privateScope)
  const result = await f.policy.finishUnstartedObservation(f.metadata('quiet-opportunity'))
  assert.equal(result.finished, true)
  assert.equal(result.local_outcome.reason, 'autonomy_rest')
  const after = await f.policy.snapshot(privateScope)
  assert.equal(after.revision, before.revision)
  assert.deepEqual(after.current, before.current)
  assert.equal((await f.send(ticket)).status, 'sent')
  const outcome = result.local_outcome
  assert.equal((await f.policy.ackObservation({ scope: privateScope, eventId: 'quiet-opportunity',
    localOutcomeId: outcome.local_outcome_id, expectedRevision: outcome.observation_revision })).acknowledged, true)
})

test('rest closure never replaces an accepted decision and checks an in-flight callback under the state lock', async t => {
  const f = fixture(t), ticket = await f.observed('old-decided')
  await f.send(ticket)
  f.time(Date.parse('2026-11-04T01:00:00+08:00'))
  assert.equal((await f.policy.finishUnstartedObservation(f.metadata('old-decided'))).reason, 'already_decided')
  assert.equal((await f.policy.finishUnstartedObservation({ ...f.metadata('still-in-flight'), canFinish: () => false })).reason, 'observation_in_flight')
  assert.equal((await f.policy.snapshot(privateScope)).observations.some(item => item.eventId === 'still-in-flight'), false)
})

test('08:00 closes old quiet-period opportunities but a fresh one can be reviewed without a morning backlog', async t => {
  const f = fixture(t, local('08:00:00'))
  const old = await f.policy.finishUnstartedObservation(f.metadata('missed-night-opportunity', privateScope, local('03:00:00')))
  assert.equal(old.local_outcome.reason, 'observation_expired')
  assert.equal((await f.policy.finishUnstartedObservation(f.metadata('fresh-morning-opportunity'))).reason, 'within_autonomous_hours')
  assert.equal((await f.make().recordObservation(f.metadata('missed-night-opportunity'))).reason, 'already_completed_without_decision')
})

test('a real night interruption closes an undecided turn without claiming that its earlier internal work never happened', async t => {
  const f = fixture(t, local('23:59:00'))
  const observed = await f.policy.recordObservation(f.metadata('interrupted-night-work'))
  f.time(Date.parse('2026-11-04T00:01:00+08:00'))
  const outcome = await f.policy.completeObservationWithoutDecision({ scope: privateScope, eventId: 'interrupted-night-work',
    expectedRevision: observed.revision, nativeTurnCompleted: false, nativeRestInterrupted: true, nativeModelSteps: 3 })
  assert.equal(outcome.reason, 'autonomy_rest')
  assert.equal(outcome.native_turn_completed, false)
  assert.equal((await f.policy.snapshot(privateScope)).deliveries.length, 0)
})

test('a trusted pre-08:00 interruption can settle after wake without replaying the interrupted work', async t => {
  const f = fixture(t, local('07:59:59.900'))
  const admitted = await f.policy.recordObservation(f.metadata('just-before-wake'))
  const interruptedAt = local('07:59:59.999')
  f.time(local('08:00:00.100'))
  const result = await f.policy.completeObservationWithoutDecision({ scope: privateScope, eventId: 'just-before-wake',
    expectedRevision: admitted.revision, nativeTurnCompleted: false, nativeRestInterrupted: true,
    nativeRestInterruptedAt: interruptedAt })
  assert.equal(result.reason, 'autonomy_rest')
  assert.equal(result.native_turn_completed, false)
})

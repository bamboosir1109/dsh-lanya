import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createDecisionPolicy } from '../plugin/lib/decision-policy.js'
import { createObservationPump } from '../plugin/lib/observation-queue.js'
import { readObservationCompletion } from '../plugin/lib/observation-producer.js'
import { createReminderObserver } from '../plugin/lib/reminder-observer.js'
import { processReminders, reminderKey } from '../plugin/lib/runtime.js'

function fixture(t, initial = Date.parse('2026-11-02T16:00:30Z'), firstReview = null, firstVerdict = 'idle') {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-reminder-rest-'))
  let clock = initial, dispatches = 0
  const directory = path.join(root, 'inbox'), scope = 'group:202'
  const policy = createDecisionPolicy({ stateFile: path.join(root, 'policy.json'), now: () => clock })
  const observer = createReminderObserver({ directory, workspaceKey: 'fixture', agentKey: '', resolveScope: () => scope, now: () => clock })
  const pump = createObservationPump({ directory, now: () => clock,
    async dispatch(event) {
      dispatches++
      if (firstReview !== null && dispatches === 1) {
        const recorded = await policy.recordObservation({ scope: event.scope, eventId: event.event_id,
          kind: event.kind, producer: event.producer, createdAt: event.created_at })
        const decided = await policy.decide({ scope: event.scope, messageId: event.event_id,
          expectedRevision: recorded.revision, verdict: firstVerdict, nextReviewAt: firstReview })
        assert.equal(decided.accepted, true)
        return { completed: true, status: 'decided', decision_id: decided.decisionId, next_review_at: firstReview }
      }
      const result = await policy.finishUnstartedObservation({ scope: event.scope, eventId: event.event_id,
        kind: event.kind, producer: event.producer, createdAt: event.created_at, canFinish: () => true })
      assert.equal(result.finished, true)
      return { completed: true, ...result.local_outcome }
    },
    acknowledge(event, result) {
      return policy.ackObservation({ scope: event.scope, eventId: event.event_id,
        ...(result.status === 'no_decision'
          ? { localOutcomeId: result.local_outcome_id, expectedRevision: result.observation_revision }
          : { decisionId: result.decision_id }) })
    }
  })
  t.after(async () => { await pump.stop(); fs.rmSync(root, { recursive: true, force: true }) })
  const files = () => fs.existsSync(path.join(directory, 'pending'))
    ? fs.readdirSync(path.join(directory, 'pending')).filter(name => name.endsWith('.json')) : []
  return { directory, policy, observer, pump, files, scope,
    get clock() { return clock }, set clock(value) { clock = value },
    get dispatches() { return dispatches },
    run(list, state = {}, send = observer) { return processReminders(list, state, new Date(clock), send) },
    async drain() { assert.equal(await pump.start({ poll: false }), true); return pump.drain() }
  }
}

test('real reminder policy, durable queue and scheduler close night occurrence without delivery or replay', async t => {
  const f = fixture(t), list = [{ at: new Date(f.clock - 1000).toISOString(), text: 'fixture reminder' }]
  const original = structuredClone(list), key = reminderKey(list[0])
  const first = await f.run(list)
  assert.equal(Object.keys(first.state.pending).length, 1)
  const pendingFile = path.join(f.directory, 'pending', f.files()[0])
  const originalRaw = fs.readFileSync(pendingFile)
  assert.equal((await f.drain()).processed, 1)
  const completion = readObservationCompletion(f.directory, f.scope, 'reminder:' + key + ':0')
  assert.equal(completion.reason, 'autonomy_rest')
  assert.equal(completion.native_turn_completed, false)
  assert.equal('decision_id' in completion, false)
  const observerReceipt = await f.observer(list[0], { key })
  assert.equal(observerReceipt.status, 'no_decision')
  assert.equal(observerReceipt.local_outcome_id, completion.local_outcome_id)
  const settled = await f.run(first.keep, first.state)
  assert.equal(settled.state.pending[key], undefined)
  assert.deepEqual(settled.state.once, {})
  assert.deepEqual(settled.state.daily, {})
  assert.equal(settled.state.outcomes[key].reason, 'autonomy_rest')
  assert.equal('decision_id' in settled.state.outcomes[key], false)
  assert.deepEqual(settled.keep, original)
  f.clock = Date.parse('2026-11-03T00:00:30Z')
  for (let index = 0; index < 3; index++) {
    const later = await f.run(settled.keep, JSON.parse(JSON.stringify(settled.state)), () => assert.fail('settled reminder dispatched again'))
    assert.deepEqual(later.state, settled.state)
    await f.pump.drain()
  }
  assert.equal(f.files().length, 1)
  assert.equal(f.dispatches, 1)
  assert.deepEqual(fs.readFileSync(pendingFile), originalRaw)
  assert.deepEqual(list, original)
})

test('a local rest result closes only one daily occurrence and preserves its original date', async t => {
  const now = new Date(2026, 10, 3, 0, 0, 30), list = [{ daily: '00:00', text: 'fixture' }]
  const local = { delivered: false, status: 'no_decision', terminal: true, reason: 'autonomy_rest',
    local_outcome_id: 'a'.repeat(64), observation_revision: 1, completed_at: now.getTime(), native_turn_completed: false }
  let calls = 0
  const send = async () => { calls++; return local }
  const first = await processReminders(list, {}, now, send)
  const oldKey = Object.keys(first.state.outcomes)[0], old = structuredClone(first.state.outcomes[oldKey])
  assert.equal(first.state.outcomes[oldKey].day, '2026-11-03')
  assert.deepEqual(first.state.daily, {})
  await processReminders(list, first.state, new Date(2026, 10, 3, 8), send)
  assert.equal(calls, 1)
  const next = await processReminders(list, first.state, new Date(2026, 10, 4, 0, 0, 30), send)
  assert.equal(calls, 2)
  assert.equal(Object.keys(next.state.outcomes).length, 2)
  assert.deepEqual(next.state.outcomes[oldKey], old)
  assert.equal(Object.keys(next.state.pending).length, 0)
  assert.deepEqual(next.keep, list)
})

test('an old occurrence queued before sleep completes as expired after wake without a model decision', async t => {
  const f = fixture(t, Date.parse('2026-11-02T15:59:30Z'))
  const list = [{ at: new Date(f.clock - 1000).toISOString(), text: 'fixture' }], key = reminderKey(list[0])
  const queued = await f.run(list)
  f.clock = Date.parse('2026-11-03T00:00:30Z')
  assert.equal((await f.drain()).processed, 1)
  const settled = await f.run(list, queued.state)
  assert.equal(settled.state.outcomes[key].reason, 'observation_expired')
  assert.equal(settled.state.outcomes[key].native_turn_completed, false)
  assert.equal('decision_id' in settled.state.outcomes[key], false)
  assert.equal(settled.state.pending[key], undefined)
  assert.deepEqual(settled.state.once, {})
})

test('a reminder first discovered after wake keeps its original due time and expires instead of catching up', async t => {
  const f = fixture(t, Date.parse('2026-11-03T00:00:30Z'))
  const list = [{ at: '2026-11-02T16:00:00Z', text: 'fixture' }], key = reminderKey(list[0])
  const queued = await f.run(list)
  assert.equal((await f.drain()).processed, 1)
  const settled = await f.run(list, queued.state)
  assert.equal(settled.state.outcomes[key].reason, 'observation_expired')
  assert.equal(settled.state.pending[key], undefined)
  assert.deepEqual(settled.state.once, {})
})

for (const verdict of ['idle', 'reply']) test('an accepted old ' + verdict + ' reminder review cannot rejuvenate the same occurrence after a sleep window', async t => {
  const morning = Date.parse('2026-11-03T00:00:30Z')
  const f = fixture(t, Date.parse('2026-11-02T15:59:30Z'), morning, verdict)
  const list = [{ at: new Date(f.clock - 1000).toISOString(), text: 'fixture' }], key = reminderKey(list[0])
  const first = await f.run(list)
  assert.equal((await f.drain()).processed, 1)
  const initial = readObservationCompletion(f.directory, f.scope, 'reminder:' + key + ':0')
  assert.equal(initial.status, 'decided')
  const firstRaw = fs.readFileSync(path.join(f.directory, 'pending', f.files()[0]))
  f.clock = morning
  const reviewing = await f.run(list, first.state)
  assert.equal(f.files().length, 2)
  assert.equal((await f.pump.drain()).processed, 1)
  const settled = await f.run(list, reviewing.state)
  assert.equal(settled.state.outcomes[key].reason, 'observation_expired')
  assert.equal(settled.state.pending[key], undefined)
  assert.deepEqual(settled.state.once, {})
  assert.equal(readObservationCompletion(f.directory, f.scope, 'reminder:' + key + ':0').decision_id, initial.decision_id)
  assert.ok(f.files().some(name => fs.readFileSync(path.join(f.directory, 'pending', name)).equals(firstRaw)))
})

test('a legacy caller without occurrence context preserves the validated original review event time', async t => {
  const morning = Date.parse('2026-11-03T00:00:30Z')
  const f = fixture(t, Date.parse('2026-11-02T15:59:30Z'), morning)
  const reminder = { daily: '23:59', text: 'fixture' }, key = 'legacy-occurrence'
  assert.equal((await f.observer(reminder, { key })).status, 'pending')
  assert.equal((await f.drain()).processed, 1)
  f.clock = morning
  assert.equal((await f.observer(reminder, { key })).status, 'pending')
  assert.equal((await f.pump.drain()).processed, 1)
  const result = await f.observer(reminder, { key })
  assert.equal(result.reason, 'observation_expired')
  assert.equal(result.native_turn_completed, false)
})

test('invalid occurrence context cannot silently become a newly created reminder opportunity', async t => {
  const f = fixture(t), key = 'fixture-occurrence', reminder = { text: 'fixture' }
  for (const occurrence of [null, [], { key: 'other', due_at: new Date(f.clock).toISOString() },
    { key, due_at: 'invalid' }, { key, due_at: new Date(f.clock + 1000).toISOString() }]) {
    await assert.rejects(f.observer(reminder, { key, occurrence }), /Exact due reminder occurrence/)
    assert.equal(f.files().length, 0)
  }
})

test('invalid local completion cannot settle, mark delivered or lose the original pending occurrence', async () => {
  const now = new Date('2026-11-02T16:00:30Z'), list = [{ at: new Date(now - 1000).toISOString(), text: 'fixture' }]
  const valid = { delivered: false, terminal: true, status: 'no_decision', reason: 'autonomy_rest',
    local_outcome_id: 'a'.repeat(64), observation_revision: 1, completed_at: now.getTime(), native_turn_completed: false }
  for (const patch of [{ reason: 'model_completed_without_decision' }, { local_outcome_id: 'bad' },
    { native_turn_completed: true }, { observation_revision: 0 }, { completed_at: true },
    { decision_id: 'model' }, { next_review_at: now.getTime() + 60000 }, { delivered: true }, { terminal: false }]) {
    const result = await processReminders(list, {}, now, async () => ({ ...valid, ...patch }))
    assert.equal(Object.keys(result.state.pending).length, 1)
    assert.deepEqual(result.state.outcomes, {})
    assert.deepEqual(result.state.once, {})
    assert.deepEqual(result.keep, list)
  }
})

test('invalid persisted local outcome rejects before any callback; valid one survives JSON restart', async () => {
  const now = new Date('2026-11-02T16:00:30Z'), list = [{ at: new Date(now - 1000).toISOString(), text: 'fixture' }]
  const receipt = { delivered: false, terminal: true, status: 'no_decision', reason: 'autonomy_rest',
    local_outcome_id: 'a'.repeat(64), observation_revision: 1, completed_at: now.getTime(), native_turn_completed: false }
  const settled = await processReminders(list, {}, now, async () => receipt)
  const key = Object.keys(settled.state.outcomes)[0]
  for (const patch of [{ decision_id: 'model' }, { native_turn_completed: true }, { reason: 'suppressed' }, { local_outcome_id: 'bad' }]) {
    const corrupt = structuredClone(settled.state)
    Object.assign(corrupt.outcomes[key], patch)
    await assert.rejects(processReminders(list, corrupt, now, () => assert.fail('invalid state dispatched')), /Invalid settled/)
  }
  const reloaded = await processReminders(list, JSON.parse(JSON.stringify(settled.state)), now, () => assert.fail('settled occurrence reopened'))
  assert.deepEqual(reloaded.state, settled.state)
})

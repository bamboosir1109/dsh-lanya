/** Actual adapter/policy/SQLite pump on disposable files; synthetic native events. */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { OneBotDecisionAdapter } from '../plugin/lib/onebot-policy-adapter.js'
import { observationKey, readObservationEvidence, createObservationPump } from '../plugin/lib/observation-queue.js'

const scope = 'private:101'
const resources = new Map()
const event = (id = 'fixture-opportunity') => ({ version: 1, scope, event_id: id, kind: 'activity.opportunity',
  producer: 'mind', payload: {}, created_at: 1, workspace_key: 'fixture-workspace', agent_key: 'fixture-agent',
  origin: 'observation', control_allowed: false })
function fixture(t, options = {}) {
  const root = options.root || fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-no-decision-'))
  if (!options.root) {
    resources.set(root, [])
    t.after(async () => {
      for (const adapter of resources.get(root)) await adapter.stop()
      fs.rmSync(root, { recursive: true, force: true }); resources.delete(root)
    })
  }
  const stateFile = path.join(root, 'policy.json'), directory = path.join(root, 'decision-inbox')
  let turns = 0, nativeDispatches = 0, sends = 0
  const chat = { chatId: scope, agent: { session: { id: 'fixture-session' } } }
  const bridge = { chats: new Map([[scope, chat]]), chatForSession: () => scope,
    interim: { onTurnStart() {}, onTurnEnd() { sends++; throw new Error('No send permitted') } },
    async dispatchFollowup() {
      nativeDispatches++
      adapter.queueTurn(chat); const turn = ++turns; adapter.onTurnStart(chat, turn)
      const record = adapter.state(chat).active
      await options.beforeEnd?.({ adapter, chat, record })
      if (options.outcome) record.outcomes.push(options.outcome)
      adapter.onTurnEnd(scope, chat, { type: 'turn/end', data: { turn,
        ...(options.missingReason ? {} : { reason: { kind: options.reason || 'completed' } }) } })
    } }
  const adapter = new OneBotDecisionAdapter(bridge, { policy: { adminUsers: [] }, config: { workspacePath: root }, log() {} },
    { stateFile, now: () => 1000, log() {} })
  adapter.trustedOwner = async () => ({ cwd: root, preset: 'fixture-agent', workspace_key: 'fixture-workspace', agent_key: 'fixture-agent' })
  resources.get(root).push(adapter)
  t.after(() => adapter.stop())
  const write = value => {
    fs.mkdirSync(path.join(directory, 'pending'), { recursive: true })
    const name = observationKey(value.scope, value.event_id) + '.json'
    fs.writeFileSync(path.join(directory, 'pending', name), JSON.stringify(value) + '\n')
    return name
  }
  return { root, stateFile, directory, chat, adapter, write, counts: () => ({ nativeDispatches, sends }) }
}
const completion = (h, name) => JSON.parse(fs.readFileSync(path.join(h.directory, 'done', name), 'utf8'))

test('normal native completion without qq_decide persists one explicit local terminal and releases only its pin', async t => {
  const h = fixture(t), value = event(), name = h.write(value)
  assert.equal(await h.adapter.pump.start({ poll: false }), true)
  assert.equal((await h.adapter.pump.drain()).processed, 1)
  const done = completion(h, name)
  assert.equal(done.version, 3); assert.equal(done.status, 'no_decision')
  assert.equal(done.reason, 'model_completed_without_decision'); assert.equal(done.native_turn_completed, true)
  for (const key of ['decision_id', 'next_review_at', 'verdict', 'ticket']) assert.equal(key in done, false)
  const state = await h.adapter.policy.snapshot(scope)
  assert.equal(state.observations.length, 0); assert.equal(state.current.decided, false)
  assert.equal(state.current.ticket, null); assert.equal(state.mode, 'open')
  await h.adapter.pump.drain()
  assert.deepEqual(h.counts(), { nativeDispatches: 1, sends: 0 })
  assert.equal(fs.readdirSync(path.join(h.directory, 'pending')).length, 1)
})

test('error, abort, missing completion evidence and unknown sends remain pending without local terminal', async t => {
  for (const options of [{ reason: 'error' }, { reason: 'aborted' }, { missingReason: true }, { outcome: 'unknown' }]) {
    const h = fixture(t, options), name = h.write(event())
    await h.adapter.pump.start({ poll: false })
    assert.equal((await h.adapter.pump.drain()).processed, 0)
    assert.equal(fs.existsSync(path.join(h.directory, 'done', name)), false)
    assert.equal((await h.adapter.policy.snapshot(scope)).observations.length, 1)
    assert.equal(h.counts().sends, 0); await h.adapter.stop()
  }
})

test('crash after policy local outcome and before done publication recovers without another native turn', async t => {
  const h = fixture(t), value = event(), name = h.write(value)
  const observed = await h.adapter.policy.recordObservation({ scope, eventId: value.event_id, kind: value.kind,
    producer: value.producer, createdAt: value.created_at })
  const prior = await h.adapter.policy.completeObservationWithoutDecision({ scope, eventId: value.event_id,
    expectedRevision: observed.revision, nativeTurnCompleted: true })
  await h.adapter.pump.start({ poll: false }); await h.adapter.pump.drain()
  assert.equal(completion(h, name).local_outcome_id, prior.local_outcome_id)
  assert.deepEqual(h.counts(), { nativeDispatches: 0, sends: 0 })
  assert.equal((await h.adapter.policy.snapshot(scope)).observations.length, 0)
})

test('durable done followed by acknowledgement failure replays only exact acknowledgement after restart', async t => {
  const h = fixture(t), name = h.write(event())
  const ack = h.adapter.acknowledgeObservation.bind(h.adapter)
  h.adapter.acknowledgeObservation = async () => { throw new Error('Synthetic crash before pin acknowledgement') }
  await h.adapter.pump.start({ poll: false }); await assert.rejects(h.adapter.pump.drain(), /Synthetic crash/)
  assert.equal(completion(h, name).status, 'no_decision')
  assert.equal((await h.adapter.policy.snapshot(scope)).observations.length, 1)
  await h.adapter.stop()
  const next = fixture(t, { root: h.root }); await next.adapter.pump.start({ poll: false })
  await next.adapter.pump.drain()
  assert.equal((await next.adapter.policy.snapshot(scope)).observations.length, 0)
  assert.deepEqual(next.counts(), { nativeDispatches: 0, sends: 0 })
  h.adapter.acknowledgeObservation = ack
})

test('local outcome acknowledgement rejects wrong revision/id/scope and cannot masquerade as a model decision', async t => {
  const h = fixture(t), value = event()
  const observed = await h.adapter.policy.recordObservation({ scope, eventId: value.event_id, kind: value.kind, producer: value.producer, createdAt: 1 })
  const local = await h.adapter.policy.completeObservationWithoutDecision({ scope, eventId: value.event_id, expectedRevision: observed.revision, nativeTurnCompleted: true })
  for (const extra of [{ localOutcomeId: '0'.repeat(64), expectedRevision: observed.revision },
    { localOutcomeId: local.local_outcome_id, expectedRevision: observed.revision + 1 }, { decisionId: local.local_outcome_id }]) {
    assert.equal((await h.adapter.policy.ackObservation({ scope, eventId: value.event_id, ...extra })).acknowledged, false)
  }
  assert.equal((await h.adapter.policy.ackObservation({ scope: 'group:202', eventId: value.event_id,
    localOutcomeId: local.local_outcome_id, expectedRevision: observed.revision })).acknowledged, false)
  assert.equal((await h.adapter.policy.snapshot(scope)).observations.length, 1)
})

test('exact v3 original fallback works, while edited original and forged authority fields are rejected', async t => {
  const h = fixture(t), value = event(), name = h.write(value), key = name.slice(0, -5)
  await h.adapter.pump.start({ poll: false }); await h.adapter.pump.drain()
  const pending = path.join(h.directory, 'pending', name), donePath = path.join(h.directory, 'done', name)
  const before = fs.readFileSync(pending), marker = completion(h, name)
  fs.unlinkSync(pending)
  assert.equal(readObservationEvidence(h.directory, key).completion.status, 'no_decision')
  for (const extra of [{ decision_id: 'invented' }, { next_review_at: 2000 }, { observation_revision: 0 },
    { native_turn_completed: false }, { local_outcome_id: '0'.repeat(64) }]) {
    fs.writeFileSync(donePath, JSON.stringify({ ...marker, ...extra }))
    assert.throws(() => readObservationEvidence(h.directory, key))
  }
  fs.writeFileSync(donePath, JSON.stringify(marker)); fs.writeFileSync(pending, Buffer.from('{}\n'))
  assert.throws(() => readObservationEvidence(h.directory, key))
  fs.writeFileSync(pending, before)
})

test('local terminal is unavailable to legacy or foreign observation producers', async t => {
  for (const value of [{ ...event(), kind: 'activity_due' }, { ...event(), producer: 'other' }]) {
    const h = fixture(t), name = h.write(value)
    await h.adapter.pump.start({ poll: false }); await h.adapter.pump.drain()
    assert.equal(fs.existsSync(path.join(h.directory, 'done', name)), false)
    await h.adapter.stop()
  }
})

test('a real qq_decide remains a real decision and receives no local no-decision substitute', async t => {
  const h = fixture(t, { beforeEnd: async ({ adapter, chat }) => {
    const result = await adapter.decide({ verdict: 'idle' }, { agent: chat.agent, callId: 'fixture-decide', signal: new AbortController().signal })
    assert.equal(result.accepted, true)
  } }), name = h.write(event())
  await h.adapter.pump.start({ poll: false }); await h.adapter.pump.drain()
  const done = completion(h, name)
  assert.equal(done.version, 2); assert.equal(done.status, 'decided'); assert.equal(typeof done.decision_id, 'string')
  assert.equal('local_outcome_id' in done, false); assert.equal(h.counts().sends, 0)
})

test('local acknowledgement preserves a newer authorized live silence and other observation pins', async t => {
  const h = fixture(t, { beforeEnd: async ({ adapter }) => {
    await adapter.policy.recordObservation({ scope, eventId: 'other-observation', kind: 'activity.opportunity', producer: 'mind', createdAt: 1 })
    const live = await adapter.policy.recordInbound({ scope, messageId: 'new-real-user', sender: '101', controlAllowed: true, origin: 'live' })
    await adapter.policy.decide({ scope, messageId: 'new-real-user', expectedRevision: live.revision, verdict: 'silence', controlUpdate: true })
  } }), name = h.write(event())
  await h.adapter.pump.start({ poll: false }); await h.adapter.pump.drain()
  assert.equal(completion(h, name).status, 'no_decision')
  const state = await h.adapter.policy.snapshot(scope)
  assert.equal(state.mode, 'silent'); assert.equal(state.current.messageId, 'new-real-user')
  assert.deepEqual(state.observations.map(row => row.eventId), ['other-observation'])
})

test('an already decided or sent/unknown observation cannot be locally reclassified or acknowledged', async t => {
  const h = fixture(t), value = event()
  const seen = await h.adapter.policy.recordObservation({ scope, eventId: value.event_id, kind: value.kind, producer: value.producer, createdAt: 1 })
  const result = await h.adapter.policy.decide({ scope, messageId: value.event_id, expectedRevision: seen.revision, verdict: 'reply' })
  await h.adapter.policy.runGuarded(result.ticket, { operationId: 'fixture-unknown', kind: 'text' },
    async () => { throw new Error('uncertain fake transport') })
  await assert.rejects(h.adapter.policy.completeObservationWithoutDecision({ scope, eventId: value.event_id,
    expectedRevision: seen.revision, nativeTurnCompleted: true }))
  assert.equal((await h.adapter.policy.snapshot(scope)).observations.length, 1)
})

/** Actual intent store/planner/policy/adapter, with registration, model, host,
 * relationship API and transport boundaries replaced by explicit inert seams.
 * All records live in this test's temporary directory. This is not a live-host
 * or human QQ-client acceptance test. */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { createHash } from 'node:crypto'
import { spawn } from 'node:child_process'

const repo = path.dirname(path.dirname(fileURLToPath(import.meta.url)))
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-intentions-'))
test.after(() => { assert.ok(path.basename(root).startsWith('lanya-intentions-')); fs.rmSync(root, { recursive: true, force: true }) })
const lib = path.join(root, 'lib')
fs.mkdirSync(lib)
fs.writeFileSync(path.join(root, 'package.json'), '{"type":"module"}')
for (const name of ['autonomy-intents.js', 'narrative-diary.js', 'onebot-policy-adapter.js', 'decision-policy.js', 'observation-queue.js']) {
  fs.copyFileSync(path.join(repo, 'plugin/lib', name), path.join(lib, name))
}
const { createIntentStore, intentOwnerScopeKey, AutonomyPlanner, readIntentReceipts } = await import(pathToFileURL(path.join(lib, 'autonomy-intents.js')).href)
const { createDecisionPolicy } = await import(pathToFileURL(path.join(lib, 'decision-policy.js')).href)
const { OneBotDecisionAdapter } = await import(pathToFileURL(path.join(lib, 'onebot-policy-adapter.js')).href)
const { readObservationEvidence, observationKey } = await import(pathToFileURL(path.join(lib, 'observation-queue.js')).href)
const hash = (...parts) => createHash('sha256').update(parts.map(String).join('\0')).digest('hex')
let serial = 0
const defer = () => { let resolve; const promise = new Promise(yes => { resolve = yes }); return { promise, resolve } }
const waitUntil = async condition => {
  for (let i = 0; i < 500; i++) { if (condition()) return; await new Promise(resolve => setTimeout(resolve, 2)) }
  assert.fail('Timed out at an inert test boundary')
}

async function fixture(t) {
  const directory = path.join(root, 'case-' + ++serial)
  fs.mkdirSync(directory)
  let now = Date.now(), call = 0
  const owner = { cwd: directory, preset: 'fixture', workspace_key: process.platform === 'win32' ? directory.toLowerCase() : directory, agent_key: 'fixture' }
  const stateFile = path.join(directory, 'decision-policy.json')
  const policy = createDecisionPolicy({ stateFile, now: () => now })
  const reads = [], sends = [], dispatched = [], chats = new Map(), owners = new Map()
  const relation = { friends: [{ user_id: 202, nickname: 'Synthetic friend' }], groups: [{ group_id: 303, group_name: 'Synthetic group' }],
    members: [{ user_id: 404, nickname: 'Synthetic known member' }, { user_id: 505, nickname: 'Synthetic unfamiliar member' }] }
  const deps = { config: { workspacePath: directory, botQQ: '999' },
    policy: { adminUsers: ['101'], dmPolicy: 'open', allowAllUsers: true, allowFrom: [], groupPolicy: 'open', groupAllowFrom: [] },
    connection: { selfId: '999', connected: true }, log: () => {} }
  const bridge = { chats, stopping: false, installChannelScope: () => {},
    chatForSession: sid => [...chats.values()].find(chat => chat.sessionId === sid)?.chatId,
    effectiveCwd: scope => (owners.get(scope) || owner).cwd,
    resolvePresetId: async scope => (owners.get(scope) || owner).preset,
    interim: { onTurnStart() {}, onTurnEnd() {}, onAssistantMessage() {} },
    dispatchFollowup: async (scope, text, role) => { const chat = ensure(scope); adapter.queueTurn(chat); dispatched.push({ scope, text, role }) },
    guardedConnectionCall: async (action, params) => {
      reads.push({ action, params: structuredClone(params) })
      if (deps.beforeRead) await deps.beforeRead(action, params)
      if (action === 'get_friend_list') return structuredClone(relation.friends)
      if (action === 'get_group_list') return structuredClone(relation.groups)
      if (action === 'get_group_member_list') return structuredClone(relation.members)
      if (action === 'get_group_member_info') return structuredClone(relation.members.find(row => String(row.user_id) === String(params.user_id)) || {})
      throw new Error('Non-read relationship action refused by test')
    } }
  const adapter = new OneBotDecisionAdapter(bridge, deps, { stateFile, policy, now: () => now,
    memoryContext: async () => null, runtimeCapabilities: async (scope, boundOwner) => ({ schema: 'dsh-runtime-capabilities/v1', read_only: true,
      conversation_key: scope, owner: { workspace_key: boundOwner.workspace_key, agent_key: boundOwner.agent_key },
      tools: { entertainment: { primary_identity_valid: scope === 'private:101', primary_internal_leisure: scope === 'private:101' } } }),
    pumpFactory: () => ({ start: async () => false, stop() {} }) })
  t.after(() => adapter.stop())
  function ensure(scope) {
    if (!chats.has(scope)) chats.set(scope, { chatId: scope, sessionId: 'session-' + scope,
      agent: { session: { id: 'session-' + scope, header: { cwd: directory, agentPreset: 'fixture' }, seq: 0, inheritedEventCount: 0, snapshotEvents: () => [] } } })
    return chats.get(scope)
  }
  const exec = scope => ({ agent: ensure(scope).agent, callId: 'call-' + ++call, signal: new AbortController().signal })
  const source = ensure('private:101')
  const observed = await policy.recordInbound({ scope: source.chatId, messageId: 'source-1', sender: '101', origin: 'live', quoted: false, controlAllowed: true })
  adapter.state(source).queue.push(Object.freeze({ ...observed, origin: 'live' }))
  adapter.onTurnStart(source, 1)
  const planner = adapter.intentPlanner
  const create = async (goal = 'A small synthetic intention') => (await planner.intent({ action: 'create', goal, reason: 'Synthetic reason', next_step: 'Use an actual tool' }, exec(source.chatId))).intent
  const candidates = async action => planner.socialCandidates({ action }, exec(source.chatId))
  const propose = (intent, candidate, extra = {}) => planner.propose({ intent_id: intent.intent_id, expected_revision: intent.revision,
    candidate_id: candidate.candidate_id, reason: 'A synthetic one-off contact', topic: 'A small synthetic topic', ...extra }, exec(source.chatId))
  const eventFor = (requestId, scope = 'private:202') => JSON.parse(fs.readFileSync(path.join(directory, 'decision-inbox/pending', hash(scope, 'social-intent:' + requestId) + '.json'), 'utf8'))
  const guarded = (record, scope, params, label = 'send-' + ++call) => adapter.runEnvelope(adapter.envelope(record, label),
    () => adapter.guardConnectionCall('send_msg', params, scope, async () => { sends.push(structuredClone(params)); return { message_id: 'synthetic-' + sends.length } }))
  return { directory, owner, policy, adapter, planner, deps, bridge, source, reads, sends, dispatched, relation, owners, ensure, exec, create, candidates, propose, eventFor, guarded,
    now: () => now, advance: ms => { now += ms } }
}

test('scoped intentions retain CAS history and agent-report status without writing memory facts', async t => {
  const h = await fixture(t), intent = await h.create()
  assert.equal(intent.summary_is_agent_report, true)
  const edited = await h.planner.intent({ action: 'update', intent_id: intent.intent_id, expected_revision: 1,
    agent_summary: 'I report that work is complete', status: 'completed' }, h.exec('private:101'))
  assert.equal(edited.memory_fact_written, false); assert.equal(edited.automatic_send, false)
  assert.equal(edited.intent.revision, 2); assert.deepEqual(edited.intent.tool_receipts, [])
  await assert.rejects(() => h.planner.intent({ action: 'update', intent_id: intent.intent_id, expected_revision: 1, status: 'active' }, h.exec('private:101')), /Stale/)
  assert.deepEqual(h.planner.store.list(h.owner, 'private:202'), [])
  assert.deepEqual(h.planner.store.list({ ...h.owner, agent_key: 'other' }, 'private:101'), [])
  assert.equal(h.sends.length, 0)
})

test('active capacity includes reactivation and foreign binding refusal preserves the existing database', async t => {
  const h = await fixture(t), completed = await h.create('Completed synthetic intention')
  await h.planner.intent({ action: 'update', intent_id: completed.intent_id, expected_revision: 1, status: 'completed' }, h.exec('private:101'))
  for (let i = 0; i < 16; i++) await h.create('Synthetic active intention ' + i)
  await assert.rejects(() => h.planner.intent({ action: 'update', intent_id: completed.intent_id, expected_revision: 2, status: 'active' }, h.exec('private:101')), /Complete or abandon/)
  const before = fs.readFileSync(h.planner.store.file)
  const foreign = createIntentStore({ file: h.planner.store.file, botBinding: 'foreign-synthetic-binding' })
  assert.throws(() => foreign.update({ owner: h.owner, scope: 'private:101', operationKey: hash('foreign'), action: 'create', fields: { goal: 'Must not write' } }), /binding mismatch/)
  assert.deepEqual(fs.readFileSync(h.planner.store.file), before)
})

test('only actual expiring relationship handles and recent live known shared-group members may be proposed', async t => {
  const h = await fixture(t), intent = await h.create()
  const friends = await h.candidates('friends')
  assert.equal(friends.candidates[0].relationship, 'friend')
  assert.equal(friends.candidates[0].reachability, 'unverified')
  await assert.rejects(() => h.propose(intent, { candidate_id: hash('forged') }), /fresh familiar/)
  h.advance(120001)
  await assert.rejects(() => h.propose(intent, friends.candidates[0]), /fresh familiar/)
  const groups = await h.candidates('groups')
  const members = await h.planner.socialCandidates({ action: 'members', group_ref: groups.groups[0].group_ref }, h.exec('private:101'))
  assert.ok(members.candidates.every(row => row.can_propose === false))
  await assert.rejects(() => h.propose(intent, members.candidates[0]), /fresh familiar/)
  h.planner.observeLive('group:303', h.owner, { chatId: 'group:303', userId: '404', messageId: 'seen-1', nickname: 'Synthetic known member' }, { origin: 'live' })
  const known = await h.planner.socialCandidates({ action: 'members', group_ref: groups.groups[0].group_ref }, h.exec('private:101'))
  assert.equal(known.candidates[0].relationship, 'shared_group'); assert.equal(known.candidates[0].can_propose, true)
  assert.equal(known.candidates[1].can_propose, false)
  assert.equal(known.group_search, 'unsupported'); assert.equal(known.group_join_application, 'unsupported')
  const proposed = await h.propose(intent, known.candidates[0])
  assert.equal(proposed.known_nonfriend, null); assert.equal(proposed.friendship, 'not_checked')
  assert.equal(proposed.transport, 'private_user_only')
  assert.equal(h.eventFor(proposed.request_id, 'private:404').scope, 'private:404')
  assert.equal(h.sends.length, 0)
})

test('a proposal queues one immutable recipient observation rather than borrowing the source ticket', async t => {
  const h = await fixture(t), intent = await h.create(), friend = (await h.candidates('friends')).candidates[0]
  await h.adapter.decide({ verdict: 'reply' }, h.exec('private:101'))
  const sourceRecord = h.adapter.state(h.source).active
  const proposed = await h.propose(intent, friend), event = h.eventFor(proposed.request_id)
  assert.equal(proposed.sent, false); assert.equal(proposed.recipient_decision_required, true)
  assert.equal(event.scope, 'private:202'); assert.equal(event.kind, 'social.opportunity'); assert.equal(event.control_allowed, false)
  assert.equal(h.planner.store.socialForEvent(event.event_id).target_key, intentOwnerScopeKey(h.owner, event.scope))
  assert.equal(h.sends.length, 0)
  await assert.rejects(() => h.guarded(sourceRecord, 'private:202', { user_id: 202, message: 'Forbidden borrowed ticket' }), /cross_scope|scope/)
  const completion = h.adapter.dispatchObservation(event)
  await waitUntil(() => h.dispatched.length === 1)
  assert.equal(h.dispatched[0].role, 'member')
  const target = h.ensure('private:202')
  h.adapter.onTurnStart(target, 1)
  const targetRecord = h.adapter.state(target).active
  await assert.rejects(() => h.guarded(targetRecord, 'private:202', { user_id: 202, message: 'No decision yet' }), /ticket|decision/)
  const decision = await h.adapter.decide({ verdict: 'reply', next_review_at: h.now() + 60000 }, h.exec('private:202'))
  assert.equal(decision.accepted, true); assert.equal(decision.next_review_at, null); assert.equal(decision.one_off, true)
  assert.notEqual(targetRecord.ticket.id, sourceRecord.ticket.id)
  await assert.rejects(() => h.guarded(targetRecord, 'private:202', { group_id: 303, message: 'No group fallback' }), /scope|route/)
  await h.guarded(targetRecord, 'private:202', { user_id: 202, message: 'A real guarded synthetic private send' })
  h.adapter.onTurnEnd('private:202', target, { data: { turn: 1, reason: { kind: 'completed' } } })
  const outcome = await completion
  assert.equal(outcome.status, 'sent'); assert.equal(h.sends.length, 1)
  await h.adapter.acknowledgeObservation(event, outcome)
  assert.equal(h.planner.store.socialForEvent(event.event_id).state, 'sent')
  await assert.rejects(() => h.adapter.dispatchObservation(event), /active immutable/)
})

test('recipient silence unknown and reserved sends block proposals without clearing any evidence', async t => {
  for (const state of ['silence', 'unknown', 'reserved']) {
    const h = await fixture(t), intent = await h.create(), friend = (await h.candidates('friends')).candidates[0]
    const inbound = await h.policy.recordInbound({ scope: 'private:202', messageId: 'prior-' + state, sender: '202', origin: 'live', quoted: false, controlAllowed: true })
    const prior = await h.policy.decide({ scope: 'private:202', messageId: inbound.messageId, expectedRevision: inbound.revision,
      verdict: state === 'silence' ? 'silence' : 'reply', controlUpdate: state === 'silence', evidenceSource: 'current' })
    const wait = defer(); let inFlight
    if (state === 'unknown') assert.equal((await h.policy.runGuarded(prior.ticket, { scope: 'private:202', operationId: 'uncertain' }, async () => { throw new Error('Synthetic uncertain ACK') })).status, 'unknown')
    if (state === 'reserved') {
      inFlight = h.policy.runGuarded(prior.ticket, { scope: 'private:202', operationId: 'reserved' }, () => wait.promise)
      await waitUntil(() => JSON.parse(fs.readFileSync(h.policy.stateFile || path.join(h.directory, 'decision-policy.json'), 'utf8')).scopes['private:202'].deliveries.length === 1)
    }
    const before = JSON.stringify(await h.policy.snapshot('private:202'))
    await assert.rejects(() => h.propose(intent, friend), /silent|unresolved/)
    assert.equal(JSON.stringify(await h.policy.snapshot('private:202')), before)
    if (inFlight) { wait.resolve({ message_id: 'synthetic-prior' }); await inFlight }
    assert.equal(h.sends.length, 0)
  }
})

test('source-turn guards survive a planner restart and unknown is never cleared', async t => {
  const h = await fixture(t), intent = await h.create(), friend = (await h.candidates('friends')).candidates[0]
  const result = await h.propose(intent, friend), event = h.eventFor(result.request_id)
  await assert.rejects(() => h.propose(intent, friend, { topic: 'Changed proposal payload' }), /arguments changed/)
  const other = await h.create('A second source intention')
  await assert.rejects(() => h.propose(other, friend), /source turn|earlier|Recipient/)
  h.planner.store.finishSocial(result.request_id, 'unknown', { status: 'unknown' })
  const restarted = new AutonomyPlanner(h.adapter)
  assert.equal(restarted.store.socialForEvent(event.event_id).state, 'unknown')
  assert.equal(restarted.store.get(h.owner, 'private:101', intent.intent_id).status, 'blocked_unknown')
  assert.throws(() => restarted.store.finishSocial(result.request_id, 'sent'), /Uncertain/)
  assert.throws(() => restarted.store.update({ owner: h.owner, scope: 'private:101', operationKey: hash('clear-unknown'), action: 'update',
    intentId: intent.intent_id, expectedRevision: 2, fields: { status: 'active' } }), /Uncertain/)
  assert.equal(h.sends.length, 0)
})

test('the durable recipient review cooldown holds across source turns and then expires', async t => {
  const h = await fixture(t), intent = await h.create(), friend = (await h.candidates('friends')).candidates[0]
  const result = await h.propose(intent, friend)
  h.planner.store.finishSocial(result.request_id, 'decided')
  const inbound = await h.policy.recordInbound({ scope: 'private:101', messageId: 'source-2', sender: '101', origin: 'live', quoted: false, controlAllowed: true })
  h.adapter.state(h.source).queue.push({ ...inbound, origin: 'live' }); h.adapter.onTurnStart(h.source, 2)
  const next = await h.create('A different synthetic intention')
  await assert.rejects(() => h.propose(next, friend), /recent contact review/)
  h.advance(300001)
  const fresh = (await h.candidates('friends')).candidates[0]
  const proposed = await h.propose(next, fresh)
  assert.equal(proposed.status, 'queued'); assert.equal(proposed.sent, false); assert.equal(h.sends.length, 0)
})

test('social no-decision completion and exact zero-step admission refusal are terminal without an invented verdict', async t => {
  for (const refused of [false, true]) {
    const h = await fixture(t), intent = await h.create(), friend = (await h.candidates('friends')).candidates[0]
    const proposed = await h.propose(intent, friend), event = h.eventFor(proposed.request_id)
    const completion = h.adapter.dispatchObservation(event)
    await waitUntil(() => h.dispatched.length === 1)
    const target = h.ensure('private:202'); h.adapter.onTurnStart(target, 1)
    const record = h.adapter.state(target).active
    if (refused) record.nativeAdmissionRefused = Object.freeze({ binding: record.binding, turn: 1,
      signal: new AbortController().signal, reason: 'previous_turn_unresolved' })
    h.adapter.onTurnEnd(target.chatId, target, { data: { turn: 1, reason: { kind: refused ? 'blocked' : 'completed' } } })
    const outcome = await completion
    assert.equal(outcome.status, 'no_decision'); assert.equal(outcome.native_turn_completed, !refused)
    assert.equal(outcome.reason, refused ? 'native_admission_refused' : 'model_completed_without_decision')
    for (const key of ['decision_id', 'verdict', 'ticket', 'next_review_at']) assert.equal(key in outcome, false)
    await h.adapter.acknowledgeObservation(event, outcome)
    assert.equal(h.planner.store.socialForEvent(event.event_id).state, 'no_decision')
    const state = await h.policy.snapshot(target.chatId)
    assert.equal(state.observations.length, 0); assert.equal(state.current.decided, false)
    assert.equal(state.current.ticket, null); assert.equal(state.deliveries.length, 0); assert.equal(h.sends.length, 0)
  }
})

test('a real uncertain recipient transport immediately blocks a new operation and preserves its old ledger', async t => {
  const h = await fixture(t), intent = await h.create(), friend = (await h.candidates('friends')).candidates[0]
  const proposed = await h.propose(intent, friend), event = h.eventFor(proposed.request_id)
  const completion = h.adapter.dispatchObservation(event)
  await waitUntil(() => h.dispatched.length === 1)
  const target = h.ensure('private:202'); h.adapter.onTurnStart(target, 1)
  const record = h.adapter.state(target).active
  await h.adapter.decide({ verdict: 'reply' }, h.exec(target.chatId))
  let attempts = 0
  await assert.rejects(() => h.adapter.runEnvelope(h.adapter.envelope(record, 'uncertain-transport'),
    () => h.adapter.guardConnectionCall('send_msg', { user_id: 202, message: 'Synthetic uncertain delivery' }, target.chatId,
      async () => { attempts++; throw new Error('Synthetic lost ACK') })))
  assert.equal(h.planner.store.socialForEvent(event.event_id).state, 'unknown')
  const before = JSON.stringify((await h.policy.snapshot(target.chatId)).deliveries)
  await assert.rejects(() => h.guarded(record, target.chatId, { user_id: 202, message: 'Must not retry with another operation' }, 'different-operation'))
  assert.equal(attempts, 1); assert.equal(h.sends.length, 0)
  assert.equal(JSON.stringify((await h.policy.snapshot(target.chatId)).deliveries), before)
  h.adapter.onTurnEnd(target.chatId, target, { data: { turn: 1, reason: { kind: 'completed' } } })
  const outcome = await completion; assert.equal(outcome.status, 'unknown')
  await h.adapter.acknowledgeObservation(event, outcome)
  assert.equal(JSON.stringify((await h.policy.snapshot(target.chatId)).deliveries), before)
})

test('a target uncertainty arising after proposal prevents dispatch and is never reassigned to this request', async t => {
  const h = await fixture(t), intent = await h.create(), friend = (await h.candidates('friends')).candidates[0]
  const proposed = await h.propose(intent, friend), event = h.eventFor(proposed.request_id)
  const live = await h.policy.recordInbound({ scope: event.scope, messageId: 'new-target-live', sender: '202', origin: 'live', quoted: false, controlAllowed: false })
  const decision = await h.policy.decide({ scope: event.scope, messageId: live.messageId, expectedRevision: live.revision, verdict: 'reply' })
  assert.equal((await h.policy.runGuarded(decision.ticket, { scope: event.scope, operationId: 'unrelated-uncertain' }, async () => { throw new Error('Synthetic unrelated lost ACK') })).status, 'unknown')
  const before = JSON.stringify(await h.policy.snapshot(event.scope))
  await assert.rejects(() => h.adapter.dispatchObservation(event), /unresolved|uncertain/i)
  assert.equal(h.dispatched.length, 0); assert.equal(h.sends.length, 0)
  assert.equal(JSON.stringify(await h.policy.snapshot(event.scope)), before)
})

test('forged social events cannot enter the internal once method or gain a local no-decision marker', async t => {
  const h = await fixture(t), intent = await h.create(), friend = (await h.candidates('friends')).candidates[0]
  const proposed = await h.propose(intent, friend), event = h.eventFor(proposed.request_id)
  await assert.rejects(() => h.adapter.dispatchObservationOnce({ ...event, payload: { ...event.payload, topic: 'Tampered context' } }), /immutable/)
  await assert.rejects(() => h.adapter.dispatchObservationOnce({ ...event, producer: 'mind' }), /immutable/)
  assert.equal(h.dispatched.length, 0); assert.equal((await h.policy.snapshot(event.scope))?.observations?.length || 0, 0)
})

test('the final policy lock independently suppresses social sends over an old uncertain delivery', async t => {
  const h = await fixture(t)
  const old = await h.policy.recordInbound({ scope: 'private:202', messageId: 'old-target', sender: '202', origin: 'live', quoted: false, controlAllowed: false })
  const oldReply = await h.policy.decide({ scope: 'private:202', messageId: old.messageId, expectedRevision: old.revision, verdict: 'reply' })
  assert.equal((await h.policy.runGuarded(oldReply.ticket, { scope: 'private:202', operationId: 'old-uncertain' }, async () => { throw new Error('Synthetic old uncertain ACK') })).status, 'unknown')
  const current = await h.policy.recordObservation({ scope: 'private:202', eventId: 'new-social', kind: 'social.opportunity', producer: 'social-planner', createdAt: h.now() })
  const fresh = await h.policy.decide({ scope: 'private:202', messageId: current.messageId, expectedRevision: current.revision, verdict: 'reply' })
  const before = JSON.stringify((await h.policy.snapshot('private:202')).deliveries)
  let transports = 0
  const result = await h.policy.runGuarded(fresh.ticket, { scope: 'private:202', operationId: 'final-social', socialOneOff: true },
    async () => { transports++; return { message_id: 'must-not-send' } })
  assert.equal(result.status, 'suppressed'); assert.equal(result.reason, 'recipient_unresolved_delivery'); assert.equal(transports, 0)
  assert.equal(JSON.stringify((await h.policy.snapshot('private:202')).deliveries), before)
})

test('queue completion validation accepts only exact social-planner local outcomes, without review or send authority', t => {
  const directory = path.join(root, 'completion-' + ++serial)
  for (const name of ['pending', 'done']) fs.mkdirSync(path.join(directory, name), { recursive: true })
  for (const [kind, producer, accepted] of [['social.opportunity', 'social-planner', true], ['social.opportunity', 'mind', false],
    ['activity.opportunity', 'social-planner', false], ['reminder.due', 'social-planner', false]]) {
    const event = { version: 1, scope: 'private:202', event_id: 'local-' + ++serial, kind, producer, created_at: 1,
      workspace_key: 'fixture-workspace', agent_key: 'fixture', payload: {}, origin: 'observation', control_allowed: false }
    const key = observationKey(event.scope, event.event_id), raw = JSON.stringify(event) + '\n'
    fs.writeFileSync(path.join(directory, 'pending', key + '.json'), raw)
    for (const [reason, completed] of [['model_completed_without_decision', true], ['native_admission_refused', false]]) {
      const marker = { version: 3, event_raw: raw, event_id: event.event_id, scope: event.scope, event_sha256: createHash('sha256').update(raw).digest('hex'),
        completed_at: 2, status: 'no_decision', reason, local_outcome_id: hash('no-decision', event.scope, event.event_id, 1), observation_revision: 1, native_turn_completed: completed }
      fs.writeFileSync(path.join(directory, 'done', key + '.json'), JSON.stringify(marker))
      if (accepted) assert.equal(readObservationEvidence(directory, key).completion.status, 'no_decision')
      else assert.throws(() => readObservationEvidence(directory, key), /no-decision/)
      fs.writeFileSync(path.join(directory, 'done', key + '.json'), JSON.stringify({ ...marker, decision_id: 'invented' }))
      assert.throws(() => readObservationEvidence(directory, key), /no-decision/)
    }
  }
})

test('deployment private and group policies cannot be bypassed through social observations', async t => {
  const h = await fixture(t), intent = await h.create(), friend = (await h.candidates('friends')).candidates[0]
  h.deps.policy.dmPolicy = 'disabled'
  await assert.rejects(() => h.propose(intent, friend), /policy|recipient|allowed|disabled/i)
  h.deps.policy.dmPolicy = 'allowlist'; h.deps.policy.allowFrom = ['101']
  await assert.rejects(() => h.propose(intent, friend), /policy|recipient|allowed|allowlist/i)
  h.deps.policy.groupPolicy = 'disabled'; h.deps.policy.groupAllowFrom = ['303']
  assert.deepEqual((await h.candidates('groups')).groups, [])
  assert.equal(h.sends.length, 0)
})

test('a new source inbound during a relationship await revokes the old proposal before publication', async t => {
  const h = await fixture(t), intent = await h.create(), friend = (await h.candidates('friends')).candidates[0]
  const wait = defer(); let reached = false
  h.deps.beforeRead = async action => { if (action === 'get_friend_list') { reached = true; await wait.promise } }
  const proposing = h.propose(intent, friend)
  await waitUntil(() => reached)
  await h.policy.recordInbound({ scope: 'private:101', messageId: 'new-live', sender: '101', origin: 'live', quoted: false, controlAllowed: true })
  wait.resolve()
  await assert.rejects(() => proposing, /Stale|revision|source/i)
  assert.equal(fs.existsSync(path.join(h.directory, 'decision-inbox/pending')), false)
  assert.equal(h.sends.length, 0)
})

test('the final source transaction rejects a revision changed by another process after the last async snapshot', async t => {
  const h = await fixture(t), intent = await h.create(), friend = (await h.candidates('friends')).candidates[0]
  const original = h.policy.runCurrentRevision.bind(h.policy)
  const marker = path.join(h.directory, 'competing-writer-locked')
  let competed = false
  h.policy.runCurrentRevision = async (args, run) => {
    const code = `import fs from 'node:fs';
      const {createDecisionPolicy,createJsonPolicyStorage}=await import(process.argv[1]);
      const stateFile=process.argv[2], marker=process.argv[3];
      const underlying=createJsonPolicyStorage({stateFile});
      const storage={transact:fn=>underlying.transact((state,commit)=>{
        const result=fn(state,commit);fs.writeFileSync(marker,'locked',{flag:'wx'});
        Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0,120);return result;
      })};
      await createDecisionPolicy({stateFile,storage}).recordInbound({scope:'private:101',messageId:'competing-live',sender:'101',origin:'live',quoted:false,controlAllowed:true});`
    const child = spawn(process.execPath, ['--input-type=module', '-e', code, pathToFileURL(path.join(lib, 'decision-policy.js')).href,
      path.join(h.directory, 'decision-policy.json'), marker], { windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'] })
    let stderr = ''; child.stderr.on('data', data => { stderr += data })
    const exited = new Promise((resolve, reject) => { child.once('error', reject); child.once('exit', status => status === 0 ? resolve() : reject(new Error(stderr))) })
    await waitUntil(() => fs.existsSync(marker))
    competed = true
    try { return await original(args, run) } finally { await exited }
  }
  await assert.rejects(() => h.propose(intent, friend), /Stale|source revision/)
  assert.equal(competed, true)
  assert.equal((await h.policy.snapshot('private:101')).current.messageId, 'competing-live')
  assert.equal(fs.existsSync(path.join(h.directory, 'decision-inbox/pending')), false)
  assert.equal(h.sends.length, 0)
})

test('tool factory failure rolls back registrations and successful disposal allows a clean new registration', async t => {
  const h = await fixture(t), registered = [], disposed = []
  const ctx = { tools: { register: tool => { registered.push(tool.name); return () => disposed.push(tool.name) } } }
  let calls = 0
  assert.throws(() => h.adapter.registerIntentTools(ctx, definition => { if (++calls === 2) throw new Error('Synthetic real-factory failure'); return definition }), /factory failure/)
  assert.deepEqual(registered, ['qq_intent']); assert.deepEqual(disposed, ['qq_intent'])
  const closers = h.adapter.registerIntentTools(ctx, definition => definition)
  assert.equal(closers.length, 1); assert.deepEqual(h.adapter.registerIntentTools(ctx, definition => definition), [])
  closers[0](); closers[0]()
  assert.deepEqual(disposed.slice(-3), ['qq_social_propose', 'qq_social_candidates', 'qq_intent'])
  const again = h.adapter.registerIntentTools(ctx, definition => definition)
  assert.equal(again.length, 1); again[0]()
})

test('only strict own-turn native tool metadata becomes returned receipts, never a model success claim', () => {
  const call = { seq: 1, type: 'tool/call', data: { turn: 2, step: 1, callId: 'c1', name: 'synthetic_tool' } }
  const result = { seq: 2, time: 100, type: 'tool/result', surfaceOp: 'append', sourceEventSeqs: [1], data: { turn: 2, step: 1,
    message: { role: 'tool', source: { kind: 'tool', callId: 'c1' }, toolCallId: 'c1', content: [{ type: 'text', text: '{"sent":true,"success":true}' }] } } }
  const session = events => ({ seq: 3, inheritedEventCount: 0, snapshotEvents: () => events })
  assert.equal(readIntentReceipts(session([call, result]), 2)[0].outcome, 'returned')
  for (const changed of [ { ...result, surfaceOp: { op: 'replace' } }, { ...result, data: { ...result.data,
    message: { ...result.data.message, source: { kind: 'other', callId: 'c1' } } } }, { ...result, sourceEventSeqs: [0] } ]) {
    assert.deepEqual(readIntentReceipts(session([call, changed]), 2), [])
  }
  assert.deepEqual(readIntentReceipts(session([call, result]), 3), [])
})

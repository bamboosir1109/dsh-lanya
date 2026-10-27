import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { createDecisionPolicy, createJsonPolicyStorage, canonicalScope, repairPolicyLocks, PolicyStateError } from '../plugin/lib/decision-policy.js'

const scope = 'private:101'
const inbound = (messageId, extra = {}) => ({ scope, messageId, sender: '101', controlAllowed: true, origin: 'live', ...extra })

function temporary(t) {
  const prefix = path.join(os.tmpdir(), 'lanya-decision-')
  const dir = fs.mkdtempSync(prefix)
  t.after(() => {
    // Cleanup is confined to the exact disposable test directory we created.
    assert.ok(path.resolve(dir).startsWith(path.resolve(prefix)))
    fs.rmSync(dir, { recursive: true, force: true })
  })
  const stateFile = path.join(dir, 'policy.json')
  return { dir, stateFile, policy: createDecisionPolicy({ stateFile }) }
}

async function verdict(policy, event, value = 'reply', extra = {}) {
  const observed = await policy.recordInbound(event)
  assert.equal(observed.accepted, true)
  return policy.decide({ scope: event.scope, messageId: event.messageId, expectedRevision: observed.revision, verdict: value, ...extra })
}

test('explicit storage path and exact canonical string IDs are required', () => {
  assert.throws(() => createDecisionPolicy(), PolicyStateError)
  assert.throws(() => createJsonPolicyStorage(), PolicyStateError)
  for (const invalid of ['101', 'private:0101', 'private:1:2', 'group:', 'group:1 ', 'private:1\n']) {
    assert.throws(() => canonicalScope(invalid), PolicyStateError)
  }
  const largeId = 'group:' + '9'.repeat(21)
  assert.equal(canonicalScope(largeId), largeId)
})

test('new inbound invalidates an old permit for every queued output kind', async (t) => {
  const { policy } = temporary(t)
  const { ticket } = await verdict(policy, inbound('m1'))
  assert.equal((await policy.check(ticket)).allowed, true)
  await policy.recordInbound(inbound('m2'))
  for (const kind of ['text', 'media', 'forward', 'api', 'tool_action']) {
    assert.deepEqual(await policy.check(ticket, { kind }), { allowed: false, reason: 'stale_revision' })
  }
})

test('idle/defer mean this turn stays quiet and do not persist silence', async (t) => {
  const { policy } = temporary(t)
  for (const value of ['idle', 'defer']) {
    const result = await verdict(policy, inbound(value), value)
    assert.equal(result.mode, 'open')
    assert.equal(result.ticket, null)
  }
  const seen = await policy.recordInbound(inbound('not-control'))
  const result = await policy.decide({ scope, messageId: 'not-control', expectedRevision: seen.revision, verdict: 'silence' })
  assert.equal(result.accepted, false)
  assert.equal((await policy.snapshot(scope)).mode, 'open')
})

test('explicit authorized silence persists across restart and has no timeout', async (t) => {
  const { policy, stateFile } = temporary(t)
  await verdict(policy, inbound('mute'), 'silence', { controlUpdate: true })
  const restarted = createDecisionPolicy({ stateFile, now: () => 9_000_000_000_000 })
  assert.equal((await restarted.snapshot(scope)).mode, 'silent')
  const result = await verdict(restarted, inbound('ordinary'), 'reply')
  assert.equal(result.ticket, null)
  assert.equal(result.reason, 'silent')
})

test('only a current live authorized unquoted control_update can resume', async (t) => {
  const { policy } = temporary(t)
  await verdict(policy, inbound('mute'), 'silence', { controlUpdate: true })
  assert.equal((await policy.recordInbound(inbound('replay', { origin: 'replay' }))).accepted, false)
  const deniedQuote = await verdict(policy, inbound('quoted', { quoted: true }), 'resume', { controlUpdate: true })
  assert.equal(deniedQuote.reason, 'unauthorized_control')
  const deniedMember = await verdict(policy, inbound('member', { controlAllowed: false }), 'resume', { controlUpdate: true })
  assert.equal(deniedMember.reason, 'unauthorized_control')
  const current = await policy.recordInbound(inbound('current'))
  const quotedVerdict = await policy.decide({ scope, messageId: 'current', expectedRevision: current.revision,
    verdict: 'resume', controlUpdate: true, evidenceSource: 'quote' })
  assert.equal(quotedVerdict.reason, 'unauthorized_control')
  assert.equal((await policy.snapshot(scope)).mode, 'silent')
  const resumed = await policy.decide({ scope, messageId: 'current', expectedRevision: current.revision,
    verdict: 'resume', controlUpdate: true })
  assert.equal(resumed.mode, 'open')
  assert.equal((await policy.check(resumed.ticket, { kind: 'media' })).allowed, true)
  const replayDecision = await policy.decide({ scope, messageId: 'current', expectedRevision: current.revision,
    verdict: 'resume', controlUpdate: true })
  assert.equal(replayDecision.reason, 'already_decided')
})

test('silence and resume cannot control a different conversation', async (t) => {
  const { policy } = temporary(t)
  await verdict(policy, inbound('mute'), 'silence', { controlUpdate: true })
  const other = inbound('other', { scope: 'group:200', sender: '300' })
  await verdict(policy, other, 'resume', { controlUpdate: true })
  assert.equal((await policy.snapshot(scope)).mode, 'silent')
  assert.equal((await policy.snapshot('group:200')).mode, 'open')
})

test('an unauthorized or quoted silence cannot change persistent mode', async (t) => {
  const { policy } = temporary(t)
  const member = await verdict(policy, inbound('member-mute', { controlAllowed: false }), 'silence', { controlUpdate: true })
  assert.equal(member.reason, 'unauthorized_control')
  const quote = await verdict(policy, inbound('quoted-mute', { quoted: true }), 'silence', { controlUpdate: true })
  assert.equal(quote.reason, 'unauthorized_control')
  assert.equal((await policy.snapshot(scope)).mode, 'open')
  await assert.rejects(() => policy.recordInbound(inbound('bad-quote', { quoted: 'true' })), PolicyStateError)
})

test('random persisted decision ID prevents forged and stolen tickets', async (t) => {
  const { policy } = temporary(t)
  const { ticket } = await verdict(policy, inbound('real'))
  const { ticket: other } = await verdict(policy, inbound('other', { scope: 'group:200', sender: '300' }))
  for (const forged of [{ ...ticket, id: 'invented' }, { ...ticket, messageId: 'other' },
    { ...ticket, id: other.id }, { ...ticket, scope: other.scope }, { ...ticket, revision: 0 }]) {
    assert.equal((await policy.check(forged)).allowed, false)
  }
  assert.deepEqual(await policy.check(ticket, { scope: 'group:200' }), { allowed: false, reason: 'scope_mismatch' })
  assert.equal((await policy.check(null)).reason, 'missing_ticket')
  assert.equal((await policy.check(ticket, { kind: 'unclassified_write' })).reason, 'unknown_kind')
})

test('dedup survives restart, remains bounded and never grants a repeat event', async (t) => {
  const { stateFile } = temporary(t)
  const policy = createDecisionPolicy({ stateFile, maxInbound: 2 })
  const first = await verdict(policy, inbound('one'))
  const restarted = createDecisionPolicy({ stateFile, maxInbound: 2 })
  const duplicate = await restarted.recordInbound(inbound('one'))
  assert.equal(duplicate.reason, 'duplicate')
  const repeatDecision = await restarted.decide({ scope, messageId: 'one', expectedRevision: first.ticket.revision, verdict: 'reply' })
  assert.equal(repeatDecision.reason, 'already_decided')
  await restarted.recordInbound(inbound('two'))
  await restarted.recordInbound(inbound('three'))
  assert.equal((await restarted.snapshot(scope)).seen.length, 2)
  assert.equal((await restarted.recordInbound(inbound('three'))).reason, 'duplicate')
  assert.equal((await restarted.recordInbound(inbound('one', { origin: 'replay' }))).reason, 'not_live')
})

test('one fixed ticket covers text, media, forward and raw API slices', async (t) => {
  const { policy } = temporary(t)
  const { ticket } = await verdict(policy, inbound('reply'))
  const sent = []
  for (const [index, kind] of ['text', 'media', 'forward', 'api'].entries()) {
    const result = await policy.runGuarded(ticket, { scope, kind, operationId: 'answer', sliceId: String(index) },
      () => { sent.push(kind); return { message_id: 'mock-' + index } })
    assert.equal(result.status, 'sent')
  }
  assert.deepEqual(sent, ['text', 'media', 'forward', 'api'])
  const duplicate = await policy.runGuarded(ticket, { kind: 'media', operationId: 'answer', sliceId: '1' }, () => sent.push('wrong'))
  assert.equal(duplicate.status, 'duplicate')
  await policy.recordInbound(inbound('new-control'))
  const stale = await policy.runGuarded(ticket, { kind: 'api', operationId: 'answer', sliceId: '4' }, () => sent.push('wrong'))
  assert.equal(stale.status, 'suppressed')
  assert.equal(stale.reason, 'stale_revision')
  assert.equal(sent.length, 4)
})

test('a durable reservation exists before initiating transport; waiting does not hold the lock', async (t) => {
  const { policy, stateFile } = temporary(t)
  const { ticket } = await verdict(policy, inbound('reply'))
  let finish
  const acknowledgement = new Promise((resolve) => { finish = resolve })
  let started
  const initiated = new Promise((resolve) => { started = resolve })
  const sending = policy.runGuarded(ticket, { operationId: 'slow' }, () => {
    const state = JSON.parse(fs.readFileSync(stateFile, 'utf8'))
    assert.equal(state.scopes[scope].deliveries[0].status, 'reserved')
    started()
    return acknowledgement
  })
  await initiated
  const newEvent = await policy.recordInbound(inbound('during-send'))
  assert.equal(newEvent.revision, ticket.revision + 1)
  finish('ack')
  assert.equal((await sending).status, 'sent')
})

test('uncertain delivery is retained on restart and never automatically replayed', async (t) => {
  const { policy, stateFile } = temporary(t)
  const { ticket } = await verdict(policy, inbound('reply'))
  let calls = 0
  const failed = await policy.runGuarded(ticket, { operationId: 'ambiguous' }, () => {
    calls += 1
    throw new Error('mock acknowledgement timeout')
  })
  assert.equal(failed.status, 'unknown')
  const restarted = createDecisionPolicy({ stateFile })
  const again = await restarted.runGuarded(ticket, { operationId: 'ambiguous' }, () => { calls += 1 })
  assert.equal(again.status, 'duplicate')
  assert.equal(again.previousStatus, 'unknown')
  assert.equal(calls, 1)
})

test('the transport adapter rejects negative OneBot ACK fixtures', async (t) => {
  const { policy } = temporary(t)
  const { ticket } = await verdict(policy, inbound('reply'))
  const normalize = (ack) => {
    if (ack.status === 'failed' || ack.retcode !== 0) throw new Error('mock OneBot negative ACK')
    return ack.data
  }
  for (const [index, ack] of [
    { status: 'failed', retcode: 100, data: {} },
    { status: 'ok', retcode: 100, data: {} },
    { status: 'failed', retcode: 0, data: {} },
  ].entries()) {
    const result = await policy.runGuarded(ticket, { operationId: 'negative-' + index },
      () => Promise.resolve(ack).then(normalize))
    assert.equal(result.sent, false)
    assert.equal(result.status, 'unknown')
  }
  const success = await policy.runGuarded(ticket, { operationId: 'positive' },
    () => Promise.resolve({ status: 'ok', retcode: 0, data: { message_id: 'mock' } }).then(normalize))
  assert.equal(success.status, 'sent')
})

test('ack bookkeeping failure cannot become a replay of a sent operation', async (t) => {
  const { stateFile } = temporary(t)
  const base = createJsonPolicyStorage({ stateFile })
  let fail = false
  const policy = createDecisionPolicy({ stateFile, storage: { transact: (fn) => {
    if (fail) throw new PolicyStateError('mock disk unavailable')
    return base.transact(fn)
  } } })
  const { ticket } = await verdict(policy, inbound('reply'))
  const result = await policy.runGuarded(ticket, { operationId: 'sent-once' }, () => { fail = true; return 'ack' })
  assert.equal(result.sent, true)
  assert.equal(result.status, 'unknown')
  const restarted = createDecisionPolicy({ stateFile })
  const duplicate = await restarted.runGuarded(ticket, { operationId: 'sent-once' }, () => assert.fail('must not resend'))
  assert.equal(duplicate.status, 'duplicate')
  assert.equal(duplicate.previousStatus, 'reserved')
})

test('internal tool permission while silent never authorizes QQ media or raw sends', async (t) => {
  const { policy } = temporary(t)
  await verdict(policy, inbound('mute'), 'silence', { controlUpdate: true })
  const { ticket } = await verdict(policy, inbound('look-only'), 'tool_action')
  assert.equal((await policy.check(ticket, { kind: 'tool_action' })).allowed, true)
  for (const kind of ['text', 'media', 'forward', 'api']) assert.equal((await policy.check(ticket, { kind })).allowed, false)
  assert.equal((await policy.snapshot(scope)).mode, 'silent')
})

test('mock clock expiry revokes a permit but never expires persistent silence', async (t) => {
  const { stateFile } = temporary(t)
  let time = 1000
  const policy = createDecisionPolicy({ stateFile, now: () => time, ticketTtlMs: 100 })
  const { ticket } = await verdict(policy, inbound('reply'))
  time = 1100
  assert.equal((await policy.check(ticket)).reason, 'ticket_expired')
  await verdict(policy, inbound('mute'), 'silence', { controlUpdate: true })
  time = 99_999_999
  assert.equal((await policy.snapshot(scope)).mode, 'silent')
})

test('bounded ledger refuses extra current-ticket writes instead of forgetting dedup', async (t) => {
  const { stateFile } = temporary(t)
  const policy = createDecisionPolicy({ stateFile, maxDeliveries: 1 })
  const { ticket } = await verdict(policy, inbound('reply'))
  assert.equal((await policy.runGuarded(ticket, { operationId: 'one' }, () => 'ack')).status, 'sent')
  assert.equal((await policy.runGuarded(ticket, { operationId: 'two' }, () => assert.fail('over capacity'))).reason, 'delivery_capacity')
  assert.equal((await policy.runGuarded(ticket, { operationId: 'one' }, () => assert.fail('duplicate'))).status, 'duplicate')
  const next = await verdict(policy, inbound('next'))
  assert.equal((await policy.runGuarded(next.ticket, { operationId: 'next' }, () => 'ack')).status, 'sent')
})

test('scope capacity fails closed without evicting a saved silence', async (t) => {
  const { stateFile } = temporary(t)
  const policy = createDecisionPolicy({ stateFile, maxScopes: 1 })
  await verdict(policy, inbound('mute'), 'silence', { controlUpdate: true })
  await assert.rejects(() => policy.recordInbound(inbound('other', { scope: 'group:200' })), PolicyStateError)
  assert.equal((await policy.snapshot(scope)).mode, 'silent')
})

test('observations have no user/control authority and a separate pinned dedup namespace', async (t) => {
  const { stateFile } = temporary(t)
  const policy = createDecisionPolicy({ stateFile, maxInbound: 1, maxObservations: 1 })
  const event = { scope, eventId: 'obs:one', kind: 'quiet', producer: 'fixture', createdAt: 100 }
  const observed = await policy.recordObservation(event)
  assert.equal(observed.accepted, true)
  assert.equal((await policy.decide({ scope, messageId: event.eventId, expectedRevision: observed.revision,
    verdict: 'resume', controlUpdate: true })).reason, 'unauthorized_control')
  const decided = await policy.decide({ scope, messageId: event.eventId, expectedRevision: observed.revision, verdict: 'idle' })
  assert.equal(decided.accepted, true)
  for (const messageId of ['obs:one', 'other', 'new']) await policy.recordInbound(inbound(messageId))
  const duplicate = await policy.recordObservation({ ...event, redrive: true })
  assert.equal(duplicate.reason, 'already_decided')
  assert.equal(duplicate.decision.verdict, 'idle')
  await assert.rejects(() => policy.recordObservation({ ...event, eventId: 'obs:two' }), /Pinned observation capacity/)
  const badAck = await policy.ackObservation({ scope, eventId: event.eventId, decisionId: 'not-the-decision' })
  assert.equal(badAck.acknowledged, false)
  const ack = await policy.ackObservation({ scope, eventId: event.eventId, decisionId: duplicate.decision.decisionId })
  assert.equal(ack.acknowledged, true)
  assert.equal((await policy.recordObservation({ ...event, eventId: 'obs:two' })).accepted, true)
})

test('only an undecided observation may redrive after an interrupted turn', async (t) => {
  const { stateFile } = temporary(t)
  const event = { scope, eventId: 'obs:retry', kind: 'quiet', producer: 'fixture', createdAt: 100 }
  const first = createDecisionPolicy({ stateFile })
  const initial = await first.recordObservation(event)
  const restarted = createDecisionPolicy({ stateFile })
  assert.equal((await restarted.recordObservation(event)).reason, 'duplicate')
  const retry = await restarted.recordObservation({ ...event, redrive: true })
  assert.equal(retry.revision, initial.revision + 1)
  const result = await restarted.decide({ scope, messageId: event.eventId, expectedRevision: retry.revision, verdict: 'reply' })
  assert.equal((await restarted.recordObservation({ ...event, redrive: true })).reason, 'already_decided')
  assert.equal((await restarted.check(result.ticket)).allowed, true)
})

test('a model review time is optional, bounded, and retained with the pinned verdict', async (t) => {
  const { stateFile } = temporary(t)
  const policy = createDecisionPolicy({ stateFile, now: () => 1000 })
  const event = { scope, eventId: 'obs:review', kind: 'quiet', producer: 'fixture', createdAt: 100 }
  const observed = await policy.recordObservation(event)
  for (const nextReviewAt of [1000, 60999, 61000.5, 1000 + 7 * 86400_000 + 1, '61000']) {
    await assert.rejects(() => policy.decide({ scope, messageId: event.eventId, expectedRevision: observed.revision,
      verdict: 'defer', nextReviewAt }), /next_review_at/)
  }
  const result = await policy.decide({ scope, messageId: event.eventId, expectedRevision: observed.revision,
    verdict: 'defer', nextReviewAt: 61000 })
  assert.equal(result.next_review_at, 61000)
  assert.equal((await policy.recordObservation(event)).decision.next_review_at, 61000)
  const next = await policy.recordObservation({ ...event, eventId: 'obs:no-review' })
  assert.equal((await policy.decide({ scope, messageId: next.messageId, expectedRevision: next.revision, verdict: 'idle' })).next_review_at, null)
})

test('corrupt JSON and schema never fall back to an open state', async (t) => {
  const { policy, stateFile } = temporary(t)
  for (const corrupt of ['{broken', JSON.stringify({ version: 999, scopes: {} }),
    JSON.stringify({ version: 1, scopes: { [scope]: { revision: 1, mode: 'invalid', current: null, seen: [], deliveries: [] } } })]) {
    fs.writeFileSync(stateFile, corrupt)
    await assert.rejects(() => policy.recordInbound(inbound('new')), PolicyStateError)
    assert.equal(fs.readFileSync(stateFile, 'utf8'), corrupt)
  }
})

test('a living or unknown lock owner is never reclaimed', async (t) => {
  const { stateFile } = temporary(t)
  const store = createJsonPolicyStorage({ stateFile, lockTimeoutMs: 20, retryMs: 1 })
  for (const contents of [JSON.stringify({ pid: process.pid, nonce: 'live' }), 'unreadable-owner']) {
    fs.writeFileSync(stateFile + '.lock', contents)
    await assert.rejects(() => store.transact(() => undefined), /lock unavailable/)
    assert.equal(fs.readFileSync(stateFile + '.lock', 'utf8'), contents)
    fs.unlinkSync(stateFile + '.lock')
  }
})

test('only a confirmed dead lock owner is reclaimed', async (t) => {
  const { stateFile } = temporary(t)
  fs.writeFileSync(stateFile + '.lock', JSON.stringify({ pid: 123456789, nonce: 'dead-owner' }))
  const store = createJsonPolicyStorage({ stateFile, isOwnerAlive: () => false })
  const policy = createDecisionPolicy({ stateFile, storage: store })
  assert.equal((await policy.recordInbound(inbound('new'))).accepted, true)
  assert.equal(fs.existsSync(stateFile + '.lock'), false)
})

function abandonedClaim(stateFile, claimOwner = { pid: 123_456_788, nonce: 'dead-reclaimer' }) {
  const contents = JSON.stringify({ pid: 123456789, nonce: 'dead-lock' })
  const lock = stateFile + '.lock'
  const claim = lock + '.reclaim-' + createHash('sha256').update(contents).digest('hex')
  fs.writeFileSync(lock, contents)
  fs.writeFileSync(claim, JSON.stringify(claimOwner))
  return { lock, claim }
}

test('a dead reclaimer stays fail-closed until explicit stopped-writer repair', async (t) => {
  const { stateFile, policy } = temporary(t)
  await verdict(policy, inbound('mute'), 'silence', { controlUpdate: true })
  const savedState = fs.readFileSync(stateFile, 'utf8')
  const { lock, claim } = abandonedClaim(stateFile)
  const isOwnerAlive = () => false
  const storage = createJsonPolicyStorage({ stateFile, isOwnerAlive, lockTimeoutMs: 20, retryMs: 1 })
  await assert.rejects(() => storage.transact(() => assert.fail('lock must stay closed')), { code: 'POLICY_LOCK_UNAVAILABLE' })
  const preview = repairPolicyLocks({ stateFile, isOwnerAlive })
  assert.equal(preview.status, 'repair_preview')
  assert.equal(preview.files.length, 2)
  assert.equal(fs.existsSync(lock), true)
  assert.equal(fs.existsSync(claim), true)
  assert.throws(() => repairPolicyLocks({ stateFile, isOwnerAlive, apply: true,
    expectedFingerprint: preview.fingerprint }), /every writer to be stopped/)
  const repaired = repairPolicyLocks({ stateFile, isOwnerAlive, apply: true, writersStopped: true,
    expectedFingerprint: preview.fingerprint })
  assert.equal(repaired.status, 'repaired')
  assert.deepEqual(repaired.removed, [claim, lock])
  assert.equal(fs.readFileSync(stateFile, 'utf8'), savedState)
  assert.equal((await policy.snapshot(scope)).mode, 'silent')
})

test('concurrent contenders and stopped-writer repair cannot steal a living claim', async (t) => {
  const { stateFile } = temporary(t)
  const { lock, claim } = abandonedClaim(stateFile, { pid: process.pid, nonce: 'living-reclaimer' })
  const saved = [fs.readFileSync(lock, 'utf8'), fs.readFileSync(claim, 'utf8')]
  const isOwnerAlive = (pid) => pid === process.pid
  const a = createJsonPolicyStorage({ stateFile, isOwnerAlive, lockTimeoutMs: 25, retryMs: 1 })
  const b = createJsonPolicyStorage({ stateFile, isOwnerAlive, lockTimeoutMs: 25, retryMs: 1 })
  const results = await Promise.allSettled([a.transact(() => assert.fail('must not enter')), b.transact(() => assert.fail('must not enter'))])
  assert.equal(results.every((entry) => entry.status === 'rejected' && entry.reason.code === 'POLICY_LOCK_UNAVAILABLE'), true)
  const preview = repairPolicyLocks({ stateFile, isOwnerAlive })
  assert.equal(preview.status, 'blocked')
  assert.equal(preview.files[1].ownerState, 'alive_or_unknown')
  assert.throws(() => repairPolicyLocks({ stateFile, isOwnerAlive, apply: true, writersStopped: true,
    expectedFingerprint: preview.fingerprint }), /alive\/unknown/)
  assert.deepEqual([fs.readFileSync(lock, 'utf8'), fs.readFileSync(claim, 'utf8')], saved)
})

test('repair refuses a replaced preview target and an unknown claim owner', (t) => {
  const { stateFile } = temporary(t)
  const { lock, claim } = abandonedClaim(stateFile)
  const isOwnerAlive = () => false
  const preview = repairPolicyLocks({ stateFile, isOwnerAlive })
  // Identical bytes on a different file are still not the reviewed lock object.
  fs.writeFileSync(claim + '.replacement', fs.readFileSync(claim))
  fs.renameSync(claim + '.replacement', claim)
  assert.throws(() => repairPolicyLocks({ stateFile, isOwnerAlive, apply: true, writersStopped: true,
    expectedFingerprint: preview.fingerprint }), /fingerprint mismatch/)
  assert.equal(fs.existsSync(lock), true)
  assert.equal(fs.existsSync(claim), true)
  fs.writeFileSync(claim, 'unknown-owner')
  assert.equal(repairPolicyLocks({ stateFile, isOwnerAlive }).status, 'blocked')
  const blocked = repairPolicyLocks({ stateFile, isOwnerAlive })
  assert.throws(() => repairPolicyLocks({ stateFile, isOwnerAlive, apply: true, writersStopped: true,
    expectedFingerprint: blocked.fingerprint }), /alive\/unknown/)
})

test('failure to persist a reservation prevents transport from being called', async () => {
  let state = { version: 1, scopes: {} }
  let failCommit = false
  const storage = { transact(fn) {
    const value = fn(state, () => { if (failCommit) throw new PolicyStateError('mock write failed') })
    return Promise.resolve(value)
  } }
  const policy = createDecisionPolicy({ stateFile: 'injected-only.json', storage })
  const { ticket } = await verdict(policy, inbound('reply'))
  failCommit = true
  await assert.rejects(() => policy.runGuarded(ticket, { operationId: 'never' }, () => assert.fail('not persisted')), PolicyStateError)
})

test('parallel policy instances keep separate scopes and revisions', async (t) => {
  const { stateFile } = temporary(t)
  const a = createDecisionPolicy({ stateFile })
  const b = createDecisionPolicy({ stateFile })
  await Promise.all([a.recordInbound(inbound('a')), b.recordInbound(inbound('b', { scope: 'group:200', sender: '300' }))])
  assert.equal((await a.snapshot(scope)).revision, 1)
  assert.equal((await b.snapshot('group:200')).revision, 1)
})

test('Windows pending-delete access failures wait without reclaiming a lock', { skip: process.platform !== 'win32' }, async t => {
  const { stateFile } = temporary(t)
  const open = fs.openSync
  let attempts = 0
  t.mock.method(fs, 'openSync', (file, flags, ...rest) => {
    if (file === stateFile + '.lock' && flags === 'wx' && ++attempts === 1) {
      const error = new Error('synthetic Windows pending-delete conflict')
      error.code = 'EPERM'
      throw error
    }
    return open(file, flags, ...rest)
  })
  const policy = createDecisionPolicy({ stateFile })
  const result = await policy.recordInbound({ scope: 'private:101', sender: '101',
    messageId: 'after-conflict', origin: 'live', controlAllowed: false })
  assert.equal(result.accepted, true)
  assert.ok(attempts >= 2)
})

test('real child processes serialize shared-file writes without losing revisions', async (t) => {
  const { stateFile, policy } = temporary(t)
  const modulePath = fileURLToPath(new URL('../plugin/lib/decision-policy.js', import.meta.url))
  const script = `import { pathToFileURL } from 'node:url';
    const { createDecisionPolicy } = await import(pathToFileURL(process.argv[1]).href);
    const policy=createDecisionPolicy({stateFile:process.argv[2]});
    for(let n=0;n<8;n++) await policy.recordInbound({scope:'group:200',sender:'300',controlAllowed:false,origin:'live',messageId:process.argv[3]+'-'+n});`
  const child = (name) => new Promise((resolve, reject) => {
    const processHandle = spawn(process.execPath, ['--input-type=module', '-e', script, modulePath, stateFile, name], { windowsHide: true })
    let errors = ''
    processHandle.stderr.on('data', (chunk) => { errors += chunk })
    processHandle.once('error', reject)
    processHandle.once('exit', (code) => code === 0 ? resolve() : reject(new Error(errors || 'child failed')))
  })
  await Promise.all([child('a'), child('b')])
  const state = await policy.snapshot('group:200')
  assert.equal(state.revision, 16)
  assert.equal(state.seen.length, 16)
})

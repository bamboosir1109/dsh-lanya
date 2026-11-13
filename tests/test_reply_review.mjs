import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createDecisionPolicy } from '../plugin/lib/decision-policy.js'

const scope = 'private:101', owner = { cwd: '/fixture', preset: 'fixture', workspace_key: '/fixture', agent_key: 'fixture' }
async function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-reply-review-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  let at = Date.parse('2026-11-03T01:00:00Z')
  const stateFile = path.join(root, 'policy.json'), options = { stateFile, now: () => at }
  const policy = createDecisionPolicy(options)
  const observed = await policy.recordInbound({ scope, messageId: 'original', sender: '101', controlAllowed: true, origin: 'live' })
  const source = { scope, expectedRevision: observed.revision, messageId: 'original', owner }
  assert.equal(await policy.rememberReplyInput({ ...source, text: 'original question' }), true)
  const decide = verdict => policy.decide({ ...source, verdict, controlUpdate: true })
  return { policy, options, source, decide, advance: ms => { at += ms }, claim: () => policy.claimReplyReview({ ...source, canClaim: () => true }) }
}

test('only matured current input qualifies; a restart retains evidence and atomic one-shot claim', async t => {
  const h = await fixture(t)
  assert.deepEqual(await h.policy.replyReviewCandidates(), [])
  h.advance(60_000)
  const restart = createDecisionPolicy(h.options)
  assert.equal((await restart.replyReviewCandidates()).length, 1)
  const claims = await Promise.all([h.claim(), restart.claimReplyReview({ ...h.source, canClaim: () => true })])
  assert.equal(claims.filter(row => row.accepted).length, 1)
  assert.deepEqual(await restart.replyReviewCandidates(), [])
  const result = claims.find(row => row.accepted)
  assert.equal(result.revision, h.source.expectedRevision + 1)
  assert.equal((await h.policy.snapshot(scope)).current.controlAllowed, false)
  assert.equal((await h.policy.decide({ scope, expectedRevision: result.revision, messageId: result.messageId, verdict: 'resume', controlUpdate: true })).accepted, false)
})

test('accepted quiet, tool actions and persistent silence never become missed replies', async t => {
  for (const verdict of ['idle', 'defer', 'silence', 'tool_action']) await t.test(verdict, async t => {
    const h = await fixture(t); await h.decide(verdict); h.advance(60_000)
    assert.equal((await h.claim()).accepted, false)
    assert.deepEqual(await h.policy.replyReviewCandidates(), [])
  })
})

test('an unqueued claim can be released by exact revision, but a stale release cannot undo the next claim', async t => {
  const h = await fixture(t)
  h.advance(60_000)
  const first = await h.claim()
  const released = { ...h.source, expectedRevision: first.revision }
  assert.equal(await h.policy.releaseUnqueuedReplyReview(released), true)
  const [candidate] = await h.policy.replyReviewCandidates()
  const second = await h.policy.claimReplyReview({ ...candidate, expectedRevision: candidate.revision, canClaim: () => true })
  assert.equal(second.accepted, true)
  assert.equal(await h.policy.releaseUnqueuedReplyReview(released), false)
  await h.policy.decide({ ...h.source, expectedRevision: second.revision, verdict: 'idle' })
  assert.equal(await h.policy.releaseUnqueuedReplyReview({ ...released, expectedRevision: second.revision }), false)
  assert.deepEqual(await h.policy.replyReviewCandidates(), [])
})

test('reply decision with zero egress can be reconsidered, but every actual egress state bars recovery', async t => {
  for (const delivery of ['none', 'sent', 'unknown', 'reserved', 'suppressed']) await t.test(delivery, async t => {
    const h = await fixture(t), decision = await h.decide('reply')
    if (delivery !== 'none') {
      if (delivery === 'sent') await h.policy.runGuarded(decision.ticket, { operationId: 'op', kind: 'text' }, async () => ({}))
      if (delivery === 'unknown') await h.policy.runGuarded(decision.ticket, { operationId: 'op', kind: 'text' }, async () => { throw new Error('ack lost') })
      if (['reserved', 'suppressed'].includes(delivery)) {
        const state = JSON.parse(fs.readFileSync(h.options.stateFile, 'utf8'))
        state.scopes[scope].deliveries.push({ key: 'op', status: delivery, at: 1, revision: h.source.expectedRevision })
        fs.writeFileSync(h.options.stateFile, JSON.stringify(state))
      }
    }
    h.advance(60_000)
    assert.equal((await h.claim()).accepted, delivery === 'none')
    if (delivery === 'none') assert.equal((await h.policy.check(decision.ticket)).allowed, false)
  })
})

test('new input, changed owner, in-flight guard, expiry and backward clock refuse a claim', async t => {
  for (const mode of ['new-input', 'owner', 'busy', 'expired', 'backward']) await t.test(mode, async t => {
    const h = await fixture(t); h.advance(60_000)
    if (mode === 'new-input') await h.policy.recordInbound({ scope, messageId: 'newer', sender: '101', controlAllowed: false, origin: 'live' })
    if (mode === 'expired') h.advance(86400_000)
    if (mode === 'backward') h.advance(-120_000)
    const result = await h.policy.claimReplyReview({ ...h.source,
      owner: mode === 'owner' ? { ...owner, agent_key: 'changed' } : owner, canClaim: () => mode !== 'busy' })
    assert.equal(result.accepted, false)
  })
})

test('an unresolved current receipt holds background observations until review, without blocking other scopes', async t => {
  const h = await fixture(t)
  const event = { scope, eventId: 'background', kind: 'activity.opportunity', producer: 'mind', createdAt: 1 }
  assert.equal((await h.policy.recordObservation(event)).reason, 'unanswered_live_input')
  assert.equal((await h.policy.recordObservation({ ...event, scope: 'group:202' })).accepted, true)
  await h.decide('idle')
  assert.equal((await h.policy.recordObservation(event)).accepted, true)
})

test('corrupt review evidence fails closed and staging cannot overwrite current input evidence', async t => {
  const h = await fixture(t)
  assert.equal(await h.policy.rememberReplyInput({ ...h.source, text: 'replacement' }), false)
  const state = JSON.parse(fs.readFileSync(h.options.stateFile, 'utf8'))
  state.scopes[scope].current.replyReview.attempted = 'false'
  fs.writeFileSync(h.options.stateFile, JSON.stringify(state))
  await assert.rejects(h.policy.replyReviewCandidates(), /Corrupt reply review/)
})

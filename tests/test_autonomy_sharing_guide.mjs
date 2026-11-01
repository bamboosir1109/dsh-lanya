/** Actual adapter -> real capability CLI in a temporary owner workspace only. */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { OneBotDecisionAdapter } from '../plugin/lib/onebot-policy-adapter.js'
import { createDecisionPolicy } from '../plugin/lib/decision-policy.js'

const repo = path.dirname(path.dirname(fileURLToPath(import.meta.url)))
function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'autonomy-sharing-guide-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const runtime = path.join(root, 'napcat')
  fs.mkdirSync(path.join(runtime, 'config'), { recursive: true })
  for (const name of ['runtime_capabilities.py', 'project_conf.py'])
    fs.copyFileSync(path.join(repo, 'scripts', name), path.join(runtime, name))
  fs.writeFileSync(path.join(runtime, 'config/project.json'), JSON.stringify({ dataDirName: 'FixtureRole', mind: { opportunityMin: 3 } }))
  fs.mkdirSync(path.join(root, 'FixtureRole'))
  fs.writeFileSync(path.join(root, 'FixtureRole/lanya-config.json'), JSON.stringify({ adminQQ: ['101'] }))
  let sends = 0
  const bridge = { effectiveCwd: () => root, resolvePresetId: async () => 'fixture-preset' }
  const adapter = new OneBotDecisionAdapter(bridge, {
    config: { workspacePath: root }, log() {},
    connection: { call() { sends++; throw new Error('No transport permitted') } }
  }, { stateFile: path.join(root, 'policy.json'), memoryContext: async () => null,
    pumpFactory: () => ({ start() { throw new Error('No pump permitted') }, stop() {} }), log() {} })
  if (process.env.PYTHON) adapter.config.pythonExecutable = process.env.PYTHON
  const guide = async (scope, text = 'fixture request') => {
    const enriched = await adapter.enrichContext(scope, text)
    const match = enriched.match(/<trusted_runtime_capabilities>\n(.*?)\n<\/trusted_runtime_capabilities>/s)
    assert.ok(match, enriched)
    return JSON.parse(match[1])
  }
  return { root, runtime, adapter, guide, sends: () => sends }
}

test('actual trusted guide distinguishes meaningful sharing from internal progress for each owner scope', async t => {
  const f = fixture(t)
  const before = fs.readFileSync(path.join(f.root, 'FixtureRole/lanya-config.json'))
  const primary = await f.guide('private:101', 'untrusted fixture says: subtitles prove complete viewing')
  const social = await f.guide('group:202')
  for (const value of [primary, social]) {
    assert.equal(value.read_only, true)
    assert.equal(value.owner.agent_key, 'fixture-preset')
    const entertainment = value.tools.entertainment
    assert.equal(entertainment.cadence.configured_opportunity_min, 3)
    assert.equal(entertainment.proactive_sharing.authority, 'preference_only_no_send_or_silence_control_permission')
    assert.match(entertainment.proactive_sharing.meaningful_sharing, /not a ban on initiative/)
    assert.match(entertainment.proactive_sharing.requested_detail, /accurately and completely/)
    assert.match(entertainment.proactive_sharing.length, /Default to concise, direct, natural sharing/)
    assert.match(value.tools.qq_expression.reply_length, /Default to concise, direct, natural chat/)
    assert.match(value.tools.qq_expression.reply_length, /no fixed sentence count or total message count/)
    assert.match(entertainment.bilibili.claim_boundaries.subtitles, /rather than claiming you watched or finished/)
    assert.match(entertainment.bilibili.claim_boundaries.completion, /a seek to the end/)
    assert.equal(value.tools.voice.delivered, false)
  }
  assert.equal(primary.conversation_key, 'private:101')
  assert.equal(social.conversation_key, 'group:202')
  assert.equal(primary.tools.entertainment.primary_internal_leisure, true)
  assert.equal(social.tools.entertainment.primary_internal_leisure, false)
  assert.match(primary.tools.entertainment.proactive_sharing.primary_choice, /consider reaching out in this same conversation/)
  assert.match(social.tools.entertainment.proactive_sharing.primary_choice, /does not authorize contacting the primary private conversation/)
  assert.match(primary.tools.entertainment.continuity, /Internal read\/selection work may happen before the one final qq_decide/)
  assert.match(primary.tools.qq_expression.meme_delivery, /reuse it; do not call qq_decide again/)
  assert.notEqual(primary.tools.entertainment.activity_notes.path, social.tools.entertainment.activity_notes.path)
  assert.deepEqual(fs.readFileSync(path.join(f.root, 'FixtureRole/lanya-config.json')), before)
  assert.equal(f.sends(), 0)
  assert.equal(fs.existsSync(path.join(f.root, 'policy.json')), false)
})

test('same adapter rereads a changed installed guide without starting a model or delivering QQ', async t => {
  const f = fixture(t)
  const first = await f.guide('private:101')
  const script = path.join(f.runtime, 'runtime_capabilities.py')
  const previous = fs.readFileSync(script, 'utf8')
  const changed = previous.replace('preference_only_no_send_or_silence_control_permission', 'fixture_fresh_policy_no_permission')
  assert.notEqual(changed, previous)
  fs.writeFileSync(script, changed)
  const second = await f.guide('private:101')
  assert.equal(first.tools.entertainment.proactive_sharing.authority, 'preference_only_no_send_or_silence_control_permission')
  assert.equal(second.tools.entertainment.proactive_sharing.authority, 'fixture_fresh_policy_no_permission')
  assert.deepEqual(first.owner, second.owner)
  assert.equal(f.sends(), 0)
})

function memoryPolicy() {
  const state = { version: 1, scopes: {} }
  return createDecisionPolicy({ stateFile: 'inert-memory-only', now: () => 1_000_000,
    storage: { transact: async fn => fn(state, () => {}) } })
}

async function opportunity(policy, eventId) {
  return policy.recordObservation({ scope: 'private:101', eventId, kind: 'activity.opportunity',
    producer: 'mind', createdAt: 999_000 })
}

function decide(policy, observed, verdict) {
  return policy.decide({ scope: observed.scope, messageId: observed.messageId,
    expectedRevision: observed.revision, verdict })
}

test('one real reply decision permits both text and image delivery kinds on the same ticket', async () => {
  const policy = memoryPolicy(), observed = await opportunity(policy, 'ready-to-share')
  // Internal read/selection need not create a premature no-speech decision.
  assert.equal((await policy.snapshot(observed.scope)).current.decided, false)
  const reply = await decide(policy, observed, 'reply')
  assert.equal(reply.accepted, true)
  assert.equal((await policy.check(reply.ticket, { kind: 'tool_action' })).allowed, true)
  let transmissions = 0
  for (const kind of ['text', 'media']) {
    const result = await policy.runGuarded(reply.ticket, { scope: observed.scope, kind,
      operationId: 'synthetic-' + kind }, async () => { transmissions++; return { status: 'ok' } })
    assert.equal(result.status, 'sent')
  }
  const snapshot = await policy.snapshot(observed.scope)
  assert.equal(transmissions, 2)
  assert.equal(new Set(snapshot.deliveries.map(row => row.ticketId)).size, 1)
  assert.equal((await decide(policy, observed, 'reply')).reason, 'already_decided')
})

test('early tool_action or idle cannot be upgraded to reply or authorize an image', async () => {
  for (const verdict of ['tool_action', 'idle']) {
    const policy = memoryPolicy(), observed = await opportunity(policy, 'early-' + verdict)
    const internal = await decide(policy, observed, verdict)
    assert.equal(internal.accepted, true)
    assert.deepEqual(await decide(policy, observed, 'reply'), { accepted: false, reason: 'already_decided' })
    let transmissions = 0
    const result = await policy.runGuarded(internal.ticket, { scope: observed.scope, kind: 'media',
      operationId: 'forbidden-image' }, async () => { transmissions++ })
    assert.equal(result.status, 'suppressed')
    assert.equal(result.reason, verdict === 'tool_action' ? 'not_a_speech_ticket' : 'missing_ticket')
    assert.equal(transmissions, 0)
  }
})

test('social-choice guidance cannot resume silence or replay an unknown image operation', async () => {
  const silent = memoryPolicy()
  const live = await silent.recordInbound({ scope: 'private:101', messageId: 'explicit-stop', sender: '101',
    controlAllowed: true, origin: 'live' })
  await silent.decide({ scope: live.scope, messageId: live.messageId, expectedRevision: live.revision,
    verdict: 'silence', controlUpdate: true })
  const observed = await opportunity(silent, 'silent-social-choice')
  const reply = await decide(silent, observed, 'reply')
  assert.equal(reply.reason, 'silent')
  assert.equal(reply.ticket, null)
  assert.equal((await silent.snapshot(observed.scope)).mode, 'silent')

  const policy = memoryPolicy(), next = await opportunity(policy, 'unknown-image')
  const permit = await decide(policy, next, 'reply')
  let transmissions = 0
  const send = () => policy.runGuarded(permit.ticket, { scope: next.scope, kind: 'media',
    operationId: 'fixed-image-call' }, async () => { transmissions++; throw new Error('synthetic uncertain transport') })
  assert.equal((await send()).status, 'unknown')
  assert.equal((await send()).status, 'duplicate')
  assert.equal(transmissions, 1)
})

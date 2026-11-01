/** Actual adapter -> real capability CLI in a temporary owner workspace only. */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { OneBotDecisionAdapter } from '../plugin/lib/onebot-policy-adapter.js'

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

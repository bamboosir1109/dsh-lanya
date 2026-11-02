/** Actual transformed native bridge/policy/adapter; fake meter + no network. */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import vm from 'node:vm'
import { createHash } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import { fileURLToPath, pathToFileURL } from 'node:url'

if (!vm.SourceTextModule) {
  test('native budget guard cases (isolated VM child)', () => {
    const env = { ...process.env }; delete env.NODE_TEST_CONTEXT
    const child = spawnSync(process.execPath, ['--experimental-vm-modules','--test',fileURLToPath(import.meta.url)],
      { encoding:'utf8',windowsHide:true,timeout:30000,env })
    assert.equal(child.status, 0, child.stdout + child.stderr)
    assert.match(child.stdout, /(?:# |ℹ )pass 5/)
    assert.match(child.stdout, /(?:# |ℹ )fail 0/)
  })
} else {
const repo = path.dirname(path.dirname(fileURLToPath(import.meta.url)))
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-budget-native-'))
test.after(() => fs.rmSync(temporary, { recursive: true, force: true }))
const fixtureHost = fs.readFileSync(path.join(repo, 'tests/fixtures/onebot_egress/native-host.mjs'), 'utf8')
const anchor = "  bridge.installChannelScope({ tools: { register: (tool) => { tools.set(tool.name, tool); return noop } },\n    systemPrompt: { section: noop } })"
assert.equal(fixtureHost.split(anchor).length, 2)
const hostSource = fixtureHost.replace(anchor, `  const budgetHooks = [], budgetGuards = []
  agent.ctx = { on: (name, fn) => { if(name !== 'tools/pre-execute') deny(); budgetHooks.push(fn) },
    get: (name) => name === 'costMeter' ? deps.testMeter : undefined,
    tools: { register: (tool) => { tools.set(tool.name, tool); return noop }, guard: fn => { budgetGuards.push(fn); return noop } },
    systemPrompt: { section: noop } }
  bridge.installChannelScope(agent.ctx)
  deps.budgetHooks = budgetHooks; deps.budgetGuards = budgetGuards`)
const hostPath = path.join(temporary, 'native-budget-host.mjs')
fs.writeFileSync(hostPath, hostSource)
const { nativeHost } = await import(pathToFileURL(hostPath).href)
const sent = h => h.calls.filter(call => /^(send_|upload_)/.test(call.action))
let sequence = 0
async function fixture(t, { exhausted = false, decisionOnly = false } = {}) {
  const root = path.join(temporary, String(++sequence)), plugin = path.join(root, 'plugin')
  fs.mkdirSync(root)
  const prepare = String.raw`
import shutil,sys
from pathlib import Path
repo,out=map(Path,sys.argv[1:3]);sys.path.insert(0,str(repo/'scripts'))
import patch_onebot_policy as policy,patch_onebot_replay as replay,patch_onebot_egress as egress
shutil.copytree(repo/'tests/fixtures/onebot_egress',out)
for name in ['bridge.js','outbound.js','tools.js','prompt.js']:
 p=out/'lib'/name;raw=policy.transform(name,p.read_bytes())
 if name=='bridge.js':raw=replay.transform('lib/bridge.js',raw)
 if name in ['bridge.js','outbound.js']:raw=egress.transform('lib/'+name,raw)
 p.write_bytes(raw)
p=out/'lib/inbound.js';s=p.read_text(encoding='utf-8');a='        const nickname = sanitizeNickname(inbound.nickname);';assert s.count(a)==1
p.write_text(s.replace(a,'        const observed = await this.ctx.observeInbound(inbound);\n        if (!observed.accepted) return;\n'+a),encoding='utf-8')
for name,raw in policy.module_outputs({'stateFile':str(out.parent/'state.json')}).items():
 if name=='lanya-policy-adapter.js':raw=egress.transform('lib/'+name,raw)
 (out/'lib'/name).write_bytes(raw)
(out/egress.GENERATED).write_bytes((repo/'plugin/lib/qq-egress.js').read_bytes())
`
  const assembled = spawnSync('python', ['-B','-c',prepare,repo,plugin], { encoding:'utf8',windowsHide:true })
  assert.equal(assembled.status, 0, assembled.stderr)
  const configPath = path.join(plugin, 'lib/lanya-policy.config.json')
  fs.writeFileSync(configPath, JSON.stringify({ stateFile: path.join(root, 'state.json'), budgetRuntimeScript: path.join(root, 'inert-budget.py') }))
  const h = await nativeHost(plugin, root)
  t.after(() => h.close())
  const requests = [], meterCalls = [], events = []
  h.agent.session.snapshotEvents = () => events
  h.deps.testMeter = { getTurnCost: async (...args) => { meterCalls.push(args); return { found: true, apiCost: 0.023, calls: [{ priced: true }] } } }
  let blocked = exhausted
  h.bridge.lanyaPolicy.budgetProvider = async request => {
    requests.push(structuredClone(request))
    if (request.operation === 'heartbeat') return { bound: true }
    const category = request.origin === 'observation' ? 'autonomy' : request.conversation_key.startsWith('group:') ? 'group' : 'private'
    if (request.operation === 'reserve') return { allowed: !blocked || decisionOnly, category, limited: true,
      decision_only: decisionOnly, reason: blocked ? 'budget_exhausted' : 'reserved', state: {spent_cny:0,reserved_cny:0.05,remaining_cny:0.95,exhausted:false,unknown_count:1} }
    if (request.operation === 'check') return { allowed: !blocked && !decisionOnly, reason: blocked || decisionOnly ? 'budget_exhausted' : 'within_reservation' }
    return { settled: request.operation === 'settle', status: request.operation === 'settle' ? 'settled' : 'unknown' }
  }
  const runTool = async (name, execute = async () => 'ran') => {
    const exec = { ...h.exec(), name, arguments: {} }
    let next = async () => ({ kind: 'allow' })
    for (const fn of [...h.deps.budgetHooks].reverse()) { const tail = next; next = () => fn(exec, tail) }
    const gate = await next()
    if (gate.kind !== 'allow') return { denied: gate.reason }
    for (const guard of h.deps.budgetGuards) { const reason = guard(exec); if (reason !== undefined) return { denied: reason } }
    return execute(exec)
  }
  const start = () => { events.push({ seq: 100, type: 'turn/start', data: { turn: 1 } }); return h.start() }
  const end = async () => {
    const ended = { seq: 141, type: 'turn/end', data: { turn: 1, reason: { kind: 'completed' } } }
    events.push(ended); h.bridge.onSessionEvent(h.agent.session, ended)
    for (let i = 0; i < 200 && h.bridge.lanyaPolicy.state(h.chat).turns.size; i++) await new Promise(r => setTimeout(r, 2))
    assert.equal(h.bridge.lanyaPolicy.state(h.chat).turns.size, 0)
  }
  return { ...h, requests, meterCalls, runTool, start, end, block: value => { blocked = value } }
}

test('native dispatch reserves by trusted live origin and settles exact native seq once', async t => {
  const h = await fixture(t)
  const adapterFile = path.join(h.directory,'plugin/lib/lanya-policy-adapter.js')
  const original = fs.readFileSync(adapterFile)
  const digest = createHash('sha256').update(original).digest('hex')
  fs.appendFileSync(adapterFile, '\n// synthetic on-disk future module\n')
  await h.bridge.lanyaPolicy.budgetHeartbeat()
  assert.equal(h.requests.at(-1).adapter_source_sha256, digest)
  const cachedRequests = []
  const cached = new h.bridge.lanyaPolicy.constructor(h.bridge, h.deps, { stateFile:path.join(h.directory,'cached-state.json'),
    budgetProvider:async request => { cachedRequests.push(request); return {bound:true} } })
  await cached.budgetHeartbeat()
  assert.equal(cachedRequests[0].adapter_source_sha256, digest)
  await cached.stop()
  fs.writeFileSync(adapterFile, original)
  h.requests.length = 0
  await h.receive('ordinary fixture request')
  assert.equal(h.requests[0].origin, 'live')
  assert.ok(h.followups[0].content[0].text.includes('trusted_turn_budget'))
  h.start(); await h.decide('tool_action')
  assert.equal(await h.runTool('bili_recommend'), 'ran')
  assert.equal(await h.runTool('browser'), 'ran')
  await h.end()
  assert.deepEqual(h.meterCalls, [['synthetic-session', 100, 141]])
  assert.equal(h.requests.filter(r => r.operation === 'settle').length, 1)
  assert.equal(h.requests.find(r => r.operation === 'settle').receipt.api_cost_cny, 0.023)
  const settled = h.requests.find(r => r.operation === 'settle')
  assert.equal(settled.budget_contract.currency, 'CNY')
  assert.equal(settled.receipt.budget_contract.numeric_scale, 1)
  assert.equal(settled.receipt.budget_contract.basis, 'owner_confirmed_numeric_cny_estimate')
  assert.equal('api_cost_usd' in settled.receipt, false)
  const trustedPrompt = h.followups[0].content[0].text.split('<trusted_turn_budget>')[1].split('</trusted_turn_budget>')[0]
  assert.equal(JSON.parse(trustedPrompt).currency, 'CNY')
  assert.equal(JSON.parse(trustedPrompt).source_estimate_currency, 'USD')
  assert.equal(sent(h).length, 0)
})

test('finite autonomous turn is refused before native followup and foreign owner before budget', async t => {
  const h = await fixture(t, { exhausted: true })
  const event = { version:1,event_id:'budget-autonomous',scope:'private:101',kind:'activity.opportunity',producer:'mind',created_at:Date.now(),
    workspace_key:process.platform==='win32'?path.resolve(h.directory).toLowerCase():path.resolve(h.directory),agent_key:'fixture',origin:'observation',control_allowed:false,payload:{} }
  h.bridge.lanyaPolicy.runtimeCapabilities = async (scope, owner) => ({ schema:'dsh-runtime-capabilities/v1',read_only:true,conversation_key:scope,owner,
    tools:{entertainment:{primary_identity_valid:true,primary_internal_leisure:true,cadence:{valid:true,configured_opportunity_min:3}}} })
  await assert.rejects(h.bridge.dispatchObservation(event), /budget_exhausted/)
  assert.equal(h.followups.length, 0)
  assert.equal(h.requests.filter(r => r.operation === 'reserve').length, 1)
  h.requests.length = 0
  await assert.rejects(h.bridge.dispatchObservation({ ...event, event_id:'foreign',agent_key:'foreign' }), /owner mismatch/)
  assert.equal(h.requests.length, 0)
  assert.equal(sent(h).length, 0)
})

test('all actual agent-scoped tools and final native send are checked; no alternate allow bypass', async t => {
  const h = await fixture(t)
  await h.receive('fixture request'); h.start(); await h.decide('reply')
  h.block(true)
  for (const name of ['bili_recommend','bili_subtitle','browser','cmd','edit','qq_send_image']) {
    assert.deepEqual(await h.runTool(name, () => assert.fail('blocked body reached')), { denied:'budget_exhausted' })
  }
  const record = h.bridge.lanyaPolicy.state(h.chat).active
  const directExec = { ...h.exec(), name:'bili_recommend' }
  assert.equal(h.deps.budgetGuards[0](directExec), 'unverified_budget_tool')
  await assert.rejects(h.send('must not transmit'), /budget_exhausted/)
  assert.equal(sent(h).length, 0)
  await h.end()
  assert.equal(record.binding.origin, 'live')
})

test('decision-only human control and local context remain possible with zero QQ/media delivery', async t => {
  const h = await fixture(t, { exhausted:true,decisionOnly:true })
  await h.receive('explicit user control fixture'); h.start()
  assert.equal(await h.runTool('read'), 'ran')
  assert.equal(await h.runTool('read_image'), 'ran')
  assert.deepEqual(await h.runTool('qq_send_image'), { denied:'budget_exhausted' })
  assert.deepEqual(await h.runTool('bili_recommend'), { denied:'budget_exhausted' })
  const denied = await h.decide('reply'); assert.equal(denied.accepted, false)
  const silenced = await h.runTool('qq_decide', exec => h.tools.get('qq_decide').execute({verdict:'silence',control_update:true,evidenceSource:'current'},exec))
  assert.equal(silenced.accepted, true)
  assert.equal((await h.bridge.lanyaPolicy.policy.snapshot('private:101')).mode, 'silent')
  assert.equal(sent(h).length, 0)
  await h.end()
  const later = await fixture(t, { exhausted:true,decisionOnly:true })
  await later.receive('explicit resume control fixture'); later.start()
  const resumed = await later.decide('resume', {control_update:true,evidenceSource:'current'})
  assert.equal(resumed.accepted, true)
  const adapter = later.bridge.lanyaPolicy
  const queued = adapter.envelope(adapter.state(later.chat).active, 'late-original-budget-ticket')
  await later.end()
  assert.equal(later.requests.filter(request => request.operation === 'settle').length, 1)
  await assert.rejects(adapter.runEnvelope(queued, () => later.bridge.sendToChat('private:101','must remain unsent after settlement')), /budget_exhausted/)
  assert.equal(sent(later).length, 0)
})

test('unpriced or missing sequence evidence retains unknown reservation without blocking completion', async t => {
  const h = await fixture(t)
  h.deps.testMeter.getTurnCost = async () => ({ found:true,apiCost:0,calls:[{priced:false}] })
  await h.receive('fixture request'); h.start(); await h.decide('idle'); await h.end()
  assert.equal(h.requests.filter(r => r.operation === 'unknown').length, 1)
  assert.equal(h.requests.filter(r => r.operation === 'settle').length, 0)
  assert.equal(sent(h).length, 0)
})
}

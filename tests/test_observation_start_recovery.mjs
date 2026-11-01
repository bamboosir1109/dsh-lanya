/** Actual transformed bridge/adapter + temporary SQLite; fake interval clock.
 * No production state, model, helper subprocess or transport is reachable.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import vm from 'node:vm'
import { spawnSync } from 'node:child_process'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { DatabaseSync } from 'node:sqlite'
import { createObservationPump, observationKey } from '../plugin/lib/observation-queue.js'

if (!vm.SourceTextModule) {
  test('observation startup recovery (isolated native VM child)', () => {
    const env = { ...process.env }; delete env.NODE_TEST_CONTEXT
    const child = spawnSync(process.execPath, ['--experimental-vm-modules', '--test', fileURLToPath(import.meta.url)],
      { encoding: 'utf8', windowsHide: true, timeout: 60000, env })
    assert.equal(child.status, 0, child.stdout + child.stderr)
    assert.match(child.stdout, /(?:# |ℹ )pass 7/)
    assert.match(child.stdout, /(?:# |ℹ )fail 0/)
  })
} else {
const repo = path.dirname(path.dirname(fileURLToPath(import.meta.url)))
const prefix = path.join(os.tmpdir(), 'lanya-observation-recovery-')
const temporary = fs.mkdtempSync(prefix)
test.after(() => { assert.ok(temporary.startsWith(prefix)); fs.rmSync(temporary, { recursive: true, force: true }) })
let hostSource = fs.readFileSync(path.join(repo, 'tests/fixtures/onebot_egress/native-host.mjs'), 'utf8')
function replaceOnce(from, to) { assert.equal(hostSource.split(from).length, 2); hostSource = hostSource.replace(from, to) }
replaceOnce('tools = new Map(), timers = new Set()', 'tools = new Map(), timers = new Set(), intervals = new Map()')
replaceOnce('setInterval: (...args) => { const timer = setInterval(...args); timers.add(timer); return timer },',
  'setInterval: (fn, ms) => { const timer = { unref() {} }; intervals.set(timer, {fn, ms}); return timer },')
replaceOnce('clearInterval: (timer) => { timers.delete(timer); clearInterval(timer) },',
  'clearInterval: (timer) => { intervals.delete(timer) },')
replaceOnce("  bridge.installChannelScope({ tools: { register: (tool) => { tools.set(tool.name, tool); return noop } },\n    systemPrompt: { section: noop } })",
  "  bridge.installChannelScope({ on: noop, tools: { register: (tool) => { tools.set(tool.name, tool); return noop }, guard: () => noop }, systemPrompt: { section: noop } })")
replaceOnce('send, raw, directory,\n', 'send, raw, directory, intervals,\n')
const hostFile = path.join(temporary, 'startup-native-host.mjs')
fs.writeFileSync(hostFile, hostSource)
const { nativeHost } = await import(pathToFileURL(hostFile).href)
const template = path.join(temporary, 'template')
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
for name,raw in policy.module_outputs({'stateFile':str(out.parent/'state.json')}).items():
 if name=='lanya-policy-adapter.js':raw=egress.transform('lib/'+name,raw)
 (out/'lib'/name).write_bytes(raw)
(out/egress.GENERATED).write_bytes((repo/'plugin/lib/qq-egress.js').read_bytes())
`
const assembled = spawnSync('python', ['-B', '-c', prepare, repo, template], { encoding: 'utf8', windowsHide: true })
assert.equal(assembled.status, 0, assembled.stderr)
const flush = async () => { for (let n = 0; n < 30; n++) await new Promise(resolve => setImmediate(resolve)) }
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes }); return { promise, resolve } }
let sequence = 0
async function fixture(t, { budget = true } = {}) {
  const root = path.join(temporary, 'case-' + ++sequence), plugin = path.join(root, 'plugin')
  fs.mkdirSync(root); fs.cpSync(template, plugin, { recursive: true })
  fs.writeFileSync(path.join(plugin, 'lib/lanya-policy.config.json'), JSON.stringify({ stateFile: path.join(root, 'state.json'),
    ...(budget ? { budgetRuntimeScript: path.join(root, 'inert-budget.py') } : {}) }))
  const h = await nativeHost(plugin, root)
  const logs = [], requests = []; h.deps.log = (level, code) => logs.push({ level, code })
  let heartbeat = async () => ({ bound: true })
  h.bridge.lanyaPolicy.budgetProvider = async request => {
    requests.push(request.operation); assert.equal(request.operation, 'heartbeat'); return heartbeat()
  }
  // The original native start method is executed; only registry/host SDK I/O
  // boundaries are inert, so startup still reaches its real adapter hook.
  h.deps.ctx = { on: () => () => {} }; h.bridge.ready = async () => {}
  for (const name of ['loadRetired', 'loadSwitchable', 'loadMapping']) h.bridge.registry[name] = async () => {}
  const tick = async () => { for (const timer of [...h.intervals.values()]) if (timer.ms === 30000) timer.fn(); await flush() }
  t.after(async () => { await h.close(); assert.equal(h.intervals.size, 0); assert.equal(h.calls.length, 0); assert.equal(h.followups.length, 0) })
  return { ...h, logs, requests, tick, heartbeat: fn => { heartbeat = fn } }
}
function consumer(h) {
  const file = path.join(h.directory, 'decision-inbox/consumer.sqlite')
  if (!fs.existsSync(file)) return null
  const db = new DatabaseSync(file, { readOnly: true })
  try { const row = db.prepare('SELECT pid,nonce FROM consumer WHERE id=1').get(); return row ? { ...row } : null } finally { db.close() }
}
function seedOwner(h, nonce = 'synthetic-existing-owner', pid = process.pid) {
  const directory = path.join(h.directory, 'decision-inbox'); fs.mkdirSync(directory, { recursive: true })
  const db = new DatabaseSync(path.join(directory, 'consumer.sqlite'))
  db.exec('CREATE TABLE consumer(id INTEGER PRIMARY KEY CHECK(id=1),pid INTEGER NOT NULL,nonce TEXT NOT NULL);CREATE TABLE retry(event_key TEXT PRIMARY KEY,attempts INTEGER NOT NULL,next_after INTEGER NOT NULL);PRAGMA user_version=1')
  db.prepare('INSERT INTO consumer VALUES (1,?,?)').run(pid, nonce); db.close()
}

test('real native startup recovers a first budget failure without another bridge start', async t => {
  const h = await fixture(t); let attempts = 0
  h.heartbeat(async () => { if (++attempts === 1) throw new Error('TransientFixtureBudget'); return { bound: true } })
  h.bridge.start(); await h.bridge.registry.mappingLoaded; await flush()
  assert.equal(attempts, 1); assert.equal(h.intervals.size, 1); assert.equal(consumer(h), null)
  assert.ok(h.logs.some(row => row.code === 'observation_pump_start_failed:Error'))
  await h.tick(); assert.equal(attempts, 2); assert.equal(consumer(h).pid, process.pid)
  const row = consumer(h); await h.tick(); assert.deepEqual(consumer(h), row)
})

test('living consumer is never stolen and its explicit release permits a later claim', async t => {
  const h = await fixture(t, { budget: false }); seedOwner(h)
  assert.equal(await h.bridge.lanyaPolicy.start(), false); await h.tick()
  assert.equal(consumer(h).nonce, 'synthetic-existing-owner'); assert.equal(h.bridge.lanyaPolicy.pumpRunning, false)
  const db = new DatabaseSync(path.join(h.directory, 'decision-inbox/consumer.sqlite'))
  db.prepare('DELETE FROM consumer WHERE id=1 AND nonce=?').run('synthetic-existing-owner'); db.close()
  await h.tick(); assert.equal(h.bridge.lanyaPolicy.pumpRunning, true)
  assert.notEqual(consumer(h).nonce, 'synthetic-existing-owner')
})

test('invalid owner stays refused across recovery ticks without lease deletion', async t => {
  const h = await fixture(t, { budget: false }); seedOwner(h, '', -1)
  assert.equal(await h.bridge.lanyaPolicy.start(), false)
  for (let n = 0; n < 3; n++) await h.tick()
  assert.deepEqual(consumer(h), { pid: -1, nonce: '' }); assert.equal(h.bridge.lanyaPolicy.pumpRunning, false)
})

test('transient claim error recovers but successful pump is not started twice', async t => {
  const h = await fixture(t, { budget: false }), adapter = h.bridge.lanyaPolicy
  const start = adapter.pump.start.bind(adapter.pump); let attempts = 0
  adapter.pump.start = async (...args) => { if (++attempts === 1) throw new Error('TransientFixtureClaim'); return start(...args) }
  await assert.rejects(adapter.start(), /TransientFixtureClaim/); assert.equal(h.intervals.size, 1)
  await h.tick(); assert.equal(attempts, 2); assert.equal(adapter.pumpRunning, true)
  await h.tick(); assert.equal(attempts, 2)
})

test('concurrent startup is serialized and stop prevents a late heartbeat claim', async t => {
  const h = await fixture(t), adapter = h.bridge.lanyaPolicy, wait = deferred(); let attempts = 0
  h.heartbeat(async () => { attempts++; return wait.promise })
  const first = adapter.start(), second = adapter.start(); await h.tick()
  assert.equal(attempts, 1); assert.equal(h.intervals.size, 1)
  await adapter.stop(); wait.resolve({ bound: true })
  assert.equal(await first, false); assert.equal(await second, false); assert.equal(consumer(h), null)
  await h.tick(); assert.equal(attempts, 1); assert.equal(h.intervals.size, 0)
})

test('later heartbeat failure retains the same living lease and can recover', async t => {
  const h = await fixture(t); assert.equal(await h.bridge.lanyaPolicy.start(), true)
  const original = consumer(h)
  h.heartbeat(async () => { throw new Error('TransientFixtureHeartbeat') }); await h.tick()
  assert.deepEqual(consumer(h), original); assert.equal(h.bridge.lanyaPolicy.pumpRunning, true)
  assert.ok(h.logs.some(row => row.code === 'observation_pump_recovery_failed:Error'))
  h.heartbeat(async () => ({ bound: true })); await h.tick(); assert.deepEqual(consumer(h), original)
})

test('recovery never replays an in-flight or unknown observation', async t => {
  const h = await fixture(t, { budget: false }), adapter = h.bridge.lanyaPolicy, wait = deferred()
  const directory = path.join(h.directory, 'decision-inbox'); fs.mkdirSync(path.join(directory, 'pending'), { recursive: true })
  const event = { version: 1, event_id: 'synthetic-inflight', scope: 'private:101', kind: 'activity_due', producer: 'fixture',
    created_at: 1, payload: {}, workspace_key: 'fixture', agent_key: '', origin: 'observation', control_allowed: false }
  const name = observationKey(event.scope, event.event_id) + '.json'
  fs.writeFileSync(path.join(directory, 'pending', name), JSON.stringify(event) + '\n')
  let dispatched = 0
  adapter.pump = createObservationPump({ directory, dispatch: () => { dispatched++; return wait.promise }, intervalMs: 5 })
  assert.equal(await adapter.start(), true); await flush(); assert.equal(dispatched, 1)
  const owner = consumer(h); for (let n = 0; n < 3; n++) await h.tick()
  assert.equal(dispatched, 1); assert.deepEqual(consumer(h), owner)
  await adapter.stop(); assert.deepEqual(consumer(h), owner)
  assert.equal(await adapter.start(), false); assert.equal(dispatched, 1)
  wait.resolve({ completed: true, status: 'unknown', decision_id: 'synthetic-unknown' }); await flush()
  await h.tick(); assert.equal(adapter.pumpRunning, true); assert.equal(dispatched, 1)
  assert.equal(JSON.parse(fs.readFileSync(path.join(directory, 'done', name), 'utf8')).status, 'unknown')
})
}

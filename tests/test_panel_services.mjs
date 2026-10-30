/** Service supervision with synthetic metadata, child events and clock only. */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createHash } from 'node:crypto'
import { EventEmitter } from 'node:events'
import { createPanelServiceSupervisor, panelServiceConfig, panelProcessPattern,
  inspectPanelService, decoratePanelServices, componentAction } from '../plugin/lib/index.js'

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-service-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  fs.mkdirSync(path.join(root, 'napcat'))
  const runtime = { napcatDir: path.join(root, 'napcat'), dataDir: path.join(root, 'role'), project: {} }
  const settings = panelServiceConfig(runtime)
  fs.writeFileSync(settings.script, '# synthetic panel program\n')
  const sourceHash = createHash('sha256').update(fs.readFileSync(settings.script)).digest('hex')
  let clock = 100000, current = { processes: [], listeners: [] }, mode = 'ok', spawns = []
  const child = () => Object.assign(new EventEmitter(), { unref() {} })
  const fetchImpl = async url => ({ ok: mode !== '500', text: async () => {
    if (mode === 'timeout') throw new Error('timeout')
    if (mode === 'html') return '<html>not the panel</html>'
    const port = Number(new URL(url).port), owner = current.listeners.find(row => row.port === port)
    return JSON.stringify({ ok: true, service: 'lanya-panel', version: 1, pid: owner?.pid,
      port, role: port === settings.works ? 'works' : 'api', script_sha256: mode === 'old' ? '0'.repeat(64) : sourceHash })
  } })
  const options = { now: () => clock, readRuntime: async () => current, fetchImpl, acquire: () => () => {},
    spawnImpl: (...args) => { const c = child(); spawns.push({ args, child: c }); return c } }
  const supervisor = createPanelServiceSupervisor(runtime, options)
  t.after(() => supervisor.close())
  return { runtime, settings, sourceHash, options, supervisor, spawns, fetchImpl,
    set(rows) { current = rows }, mode(value) { mode = value }, advance(ms) { clock += ms } }
}
const one = (port, pid = 101) => ({ processes: [{ port, pid, ws: 10 * 1048576 }], listeners: [{ port, pid }] })

test('healthy existing services keep genuine separate counts and never spawn', async t => {
  const f = fixture(t)
  f.set({ processes: [...one(19388).processes, ...one(19389, 102).processes], listeners: [...one(19388).listeners, ...one(19389, 102).listeners] })
  const works = await f.supervisor.status('works'), api = await f.supervisor.status('api')
  assert.equal(works.healthy, true); assert.equal(works.count, 1); assert.equal(works.mb, 10)
  assert.equal(api.count, 1)
  await f.supervisor.ensure('works'); await f.supervisor.ensure('api'); await f.supervisor.ensure('works')
  assert.equal(f.spawns.length, 0)
  const state = decoratePanelServices({ memory: { lanyaMb: 20 }, components: [{ tag: 'works', count: 2, mb: 20 }] }, { works, api })
  assert.equal(state.components.find(row => row.tag === 'works').count, 1)
  assert.equal(state.memory.lanyaMb, 20)
})

test('concurrent works controls share one launch and never start the API port', async t => {
  const f = fixture(t); f.set(one(19389))
  const result = await Promise.all(Array.from({ length: 8 }, () => f.supervisor.ensure('works')))
  assert.equal(f.spawns.length, 1)
  assert.deepEqual(f.spawns[0].args.slice(0, 2), ['python', [f.settings.script, '--port', '19388']])
  assert.equal(f.spawns[0].args[2].windowsHide, true)
  assert.ok(result.every(row => row.status === 'starting' && row.healthy === false))
  assert.equal((await f.supervisor.ensure('works')).status, 'starting')
  assert.equal(f.spawns.length, 1)
})

test('single owner and lock-time recheck prevent independent supervisors from double launch', async t => {
  const f = fixture(t); let owner = false
  const acquire = () => { if (owner) return null; owner = true; return () => { owner = false } }
  const first = createPanelServiceSupervisor(f.runtime, { ...f.options, acquire })
  const second = createPanelServiceSupervisor(f.runtime, { ...f.options, acquire })
  t.after(() => { first.close(); second.close() })
  assert.equal((await first.ensure('works')).status, 'starting')
  assert.equal((await second.ensure('works')).status, 'owned_elsewhere')
  assert.equal(f.spawns.length, 1)
  first.close(); second.close()
  const third = createPanelServiceSupervisor(f.runtime, { ...f.options, acquire: () => {
    f.set(one(19388)); return () => {}
  } })
  t.after(() => third.close())
  assert.equal((await third.ensure('works')).healthy, true)
  assert.equal(f.spawns.length, 1)
})

test('disposing during the final probe releases ownership and cannot spawn afterward', async t => {
  const f = fixture(t)
  let reads = 0, unblock, reached, released = 0
  const waiting = new Promise(resolve => { reached = resolve })
  const s = createPanelServiceSupervisor(f.runtime, { ...f.options,
    acquire: () => () => { released++ }, readRuntime: async () => {
      if (++reads === 2) { reached(); return new Promise(resolve => { unblock = resolve }) }
      return { processes: [], listeners: [] }
    } })
  t.after(() => s.close())
  const pending = s.ensure('works'); await waiting; s.close()
  unblock({ processes: [], listeners: [] })
  assert.equal((await pending).status, 'closed')
  assert.equal(released, 1); assert.equal(f.spawns.length, 0)
  await s.ensure('works'); assert.equal(reads, 2)
})

test('unknown listener, bad HTTP, old module and duplicate processes never become green or spawn', async t => {
  const f = fixture(t)
  f.set({ processes: [], listeners: [{ port: 19388, pid: 999 }] })
  assert.equal((await f.supervisor.ensure('works')).status, 'blocked')
  f.set(one(19388))
  for (const mode of ['500', 'html', 'timeout', 'old']) {
    f.mode(mode); const value = await f.supervisor.status('works')
    assert.equal(value.healthy, false); assert.equal(value.count, 1)
    assert.equal((await f.supervisor.ensure('works')).ok, false)
  }
  f.mode('ok'); f.set({ processes: [...one(19388).processes, { port: 19388, pid: 102, ws: 1 }], listeners: one(19388).listeners })
  assert.equal((await f.supervisor.status('works')).healthy, false)
  assert.equal(f.spawns.length, 0)
})

test('natural exit and startup failure obey backoff before one later recovery', async t => {
  const f = fixture(t)
  await f.supervisor.ensure('works')
  f.spawns[0].child.emit('exit', 1)
  assert.equal((await f.supervisor.ensure('works')).status, 'backoff')
  f.advance(29999); await f.supervisor.ensure('works'); assert.equal(f.spawns.length, 1)
  f.advance(1); await f.supervisor.ensure('works'); assert.equal(f.spawns.length, 2)
  f.spawns[1].child.emit('error', new Error('synthetic start failure'))
  assert.equal((await f.supervisor.ensure('works')).status, 'backoff')
  f.advance(30000); await f.supervisor.ensure('works'); assert.equal(f.spawns.length, 3)
  f.set(one(19388, 303)); assert.equal((await f.supervisor.status('works')).healthy, true)
})

test('synchronous spawn errors and unavailable process metadata fail closed without loops', async t => {
  const f = fixture(t); let calls = 0
  const s = createPanelServiceSupervisor(f.runtime, { ...f.options, spawnImpl: () => { calls++; throw new Error('failed') } })
  t.after(() => s.close())
  await s.ensure('works'); await s.ensure('works'); assert.equal(calls, 1)
  const unavailable = createPanelServiceSupervisor(f.runtime, { ...f.options, readRuntime: async () => { throw new Error('denied') } })
  t.after(() => unavailable.close())
  assert.equal((await unavailable.ensure('works')).status, 'unknown')
  assert.equal(f.spawns.length, 0)
  const denied = createPanelServiceSupervisor(f.runtime, { ...f.options, acquire: () => { throw new Error('synthetic ACL refusal') } })
  t.after(() => denied.close())
  assert.equal((await denied.ensure('works')).status, 'unknown')
  assert.equal(f.spawns.length, 0)
})

test('fixed Windows argv distinguishes static/API instances and excludes other workspaces', () => {
  const settings = { script: 'E:\\fixture space\\napcat\\lanya-panel-server.py' }
  const re = new RegExp(panelProcessPattern(settings), 'i')
  assert.equal(re.exec('"C:\\Python314\\python.exe" "' + settings.script + '" --port 19388').groups.port, '19388')
  const deployed = re.exec('"C:\\Python314\\python.exe" -B "' + settings.script + '" --port 19388')
  assert.equal(deployed.groups.port, '19388'); assert.equal(deployed.groups.pythonflag, '-B')
  assert.equal(re.test('python -c "' + settings.script + '" --port 19388'), false)
  assert.equal(re.test('python -m "' + settings.script + '" --port 19388'), false)
  assert.equal(re.exec('python "' + settings.script + '" --port 19389').groups.port, '19389')
  assert.equal(re.exec('python "' + settings.script + '"').groups.port, undefined)
  assert.equal(re.test('python "E:\\other\\napcat\\lanya-panel-server.py" --port 19388'), false)
  assert.equal(re.test('python "' + settings.script + '" --port 19388 --extra'), false)
  assert.throws(() => panelServiceConfig({ ...settings, project: { panelPort: 19388, panelStaticPort: 19388 } }), /Invalid/)
})

test('Works button delegates to the shared supervisor and refuses stop in both modes', async () => {
  let calls = 0
  for (const mode of ['unified', 'legacy']) {
    const runtime = { project: { decisions: { mode } } }
    const deps = { ensureWorks: async () => { calls++; return { ok: true, service_check: true, healthy: false } },
      launch() { throw new Error('old script forbidden') }, stop() { throw new Error('stop forbidden') } }
    assert.equal((await componentAction(runtime, { target: 'works', op: 'restart' }, deps)).service_check, true)
    await assert.rejects(componentAction(runtime, { target: 'works', op: 'stop' }, deps), /不停止/)
  }
  assert.equal(calls, 2)
})

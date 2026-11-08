/** Synthetic files and socket seams only; never contacts QQ or a model. */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { readBackendSelection, resolveOneBotEndpoint, createBackendGuard } from '../plugin/lib/qq-backend.js'

function fixture(t) {
  const originalOverride = process.env.LANYA_PROJECT_CONFIG
  delete process.env.LANYA_PROJECT_CONFIG
  t.after(() => { if(originalOverride === undefined) delete process.env.LANYA_PROJECT_CONFIG; else process.env.LANYA_PROJECT_CONFIG=originalOverride })
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'qq backend '))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const write = (name, value) => { const file = path.join(root, name); fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, JSON.stringify(value)); return file }
  const select = (provider = 'napcat', revision = 1, providers = {}) => write('napcat/config/project.json',
    { dataDirName: 'fixture', onebotPort: 8721, qqBackend: { provider, revision, providers } })
  write('napcat/framework/config/onebot11.json', { network: { httpServers: [{ port: 8721, token: 'fixture-napcat' }] } })
  write('snowluma/config/onebot.json', { networks: { httpServers: [{ name: 'http-default', port: 8730, accessToken: 'fixture-snow' }] } })
  write('fixture/lanya-config.json', {botQQ:'303'})
  const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }
  return { root, write, select, deferred }
}

test('shared Python and JavaScript configuration vectors', t => {
  const vectors = JSON.parse(fs.readFileSync(new URL('./fixtures/qq_backend/contracts.json', import.meta.url), 'utf8'))
  for (const c of vectors.cases) {
    const f = fixture(t), files = { ...structuredClone(vectors.files), ...c.files }
    const project = { ...vectors.project, ...c.project, qqBackend: { provider: c.provider, providers: { [c.provider]: c.settings ?? {} } } }
    f.write('napcat/config/project.json', project)
    if (c.row) Object.assign(files['snowluma/config/onebot.json'].networks.httpServers[0], c.row)
    for (const [name, value] of Object.entries(files)) {
      if (value === null) fs.rmSync(path.join(f.root, name), { force: true })
      else f.write(name, value)
    }
    if (c.error) assert.throws(() => resolveOneBotEndpoint(f.root), undefined, c.name)
    else {
      const value = resolveOneBotEndpoint(f.root)
      assert.deepEqual({ base: value.base, token: value.token }, c.expected, c.name)
    }
  }
})

test('old installation stays compatible while invalid explicit selections fail', t => {
  const f = fixture(t); assert.equal(readBackendSelection(f.root).explicit, false)
  for (const value of ['invalid', null, false, []]) {
    f.select(value); assert.throws(() => readBackendSelection(f.root), /configuration is unavailable/)
  }
  f.select('napcat', -1); assert.throws(() => readBackendSelection(f.root))
})

test('explicit environment project configuration matches shared-reader priority and never falls back', t=>{
  const f=fixture(t);f.select('napcat')
  const operator=f.write('operator-project.json',{dataDirName:'fixture',qqBackend:{provider:'snowluma'}})
  const fallback=path.join(f.root,'napcat/config/project.json')
  process.env.LANYA_PROJECT_CONFIG=operator
  assert.equal(readBackendSelection(f.root).provider,'snowluma')
  assert.equal(readBackendSelection(f.root,{projectFile:fallback}).provider,'snowluma')
  assert.equal(resolveOneBotEndpoint(f.root).token,'fixture-snow')
  for(const raw of ['{','null','[]']) {fs.writeFileSync(operator,raw);assert.throws(()=>readBackendSelection(f.root))}
  fs.unlinkSync(operator);assert.throws(()=>readBackendSelection(f.root))
  process.env.LANYA_PROJECT_CONFIG=path.relative(process.cwd(),operator)
  f.write('operator-project.json',{dataDirName:'fixture',qqBackend:{provider:'snowluma'}})
  assert.equal(readBackendSelection(f.root).provider,'snowluma')
})

test('history anchor capability follows only the trusted selection and retains signed IDs', t=>{
  const f=fixture(t),guard=createBackendGuard({workspace:f.root})
  f.select('napcat');assert.deepEqual(guard.historyAnchor('-41'),{message_seq:'-41',parse_mult_msg:false,quick_reply:true})
  f.select('snowluma',2);assert.deepEqual(guard.historyAnchor('-41'),{message_id:-41})
  for(const id of ['../41','9007199254740992',{},41])assert.throws(()=>guard.historyAnchor(id))
  f.select('invalid',3);assert.throws(()=>guard.historyAnchor('41'))
})

test('native guard derives account from role identity, invalidates changed identity and refuses unbound or conflicting setup',async t=>{
  const f=fixture(t);f.select('snowluma');let calls=0
  const guard=createBackendGuard({workspace:f.root})
  const connection={socket:{},connected:true,lanyaBackendTransportVersion:1,async call(action){calls++;return action==='get_version_info'?{app_name:'SnowLuma'}:{user_id:303}}}
  const stamp=guard.selection().stamp
  assert.equal((await guard.verify(connection)).allowed,true);assert.equal(calls,2)
  f.write('fixture/lanya-config.json',{botQQ:'404'})
  assert.equal(guard.current(stamp),false);assert.equal(guard.edge(connection,stamp),false)
  assert.equal((await guard.verify(connection)).reason,'backend_account_mismatch')
  f.write('fixture/lanya-config.json',{})
  assert.equal((await guard.verify(connection)).reason,'backend_account_unconfigured')
  f.select('snowluma',2,{snowluma:{account:'303'}})
  assert.equal((await guard.verify(connection)).allowed,true)
  f.write('fixture/lanya-config.json',{botQQ:'404'})
  assert.throws(()=>guard.selection());assert.equal(calls,6)
})

test('explicit selection requires loaded transport before RPC and at every final edge', async t => {
  const f = fixture(t); f.select('snowluma'); let calls = 0
  const guard = createBackendGuard({ workspace: f.root })
  const connection = { socket: {}, connected: true, async call(action) {
    calls++; return action === 'get_version_info' ? { app_name: 'SnowLuma' } : { user_id: 303 }
  } }
  assert.equal((await guard.verify(connection)).reason, 'backend_runtime_unverified')
  assert.equal(calls, 0)
  connection.lanyaBackendTransportVersion = 1
  assert.equal((await guard.verify(connection)).allowed, true); assert.equal(calls, 2)
  assert.equal(guard.edge(connection), true)
  delete connection.lanyaBackendTransportVersion
  assert.equal(guard.edge(connection), false)
  assert.equal((await guard.verify(connection)).reason, 'backend_runtime_unverified')
  assert.equal(calls, 2)
})

test('NapCat and SnowLuma endpoints use independent credentials excluded from diagnostics', t => {
  const f = fixture(t)
  for (const [provider, port, token] of [['napcat', 8721, 'fixture-napcat'], ['snowluma', 8730, 'fixture-snow']]) {
    f.select(provider); const result = resolveOneBotEndpoint(f.root)
    assert.equal(result.base, 'http://127.0.0.1:' + port); assert.equal(result.token, token)
    assert.doesNotMatch(JSON.stringify(result), /fixture-(?:napcat|snow)/)
  }
})

test('readiness lost during a real handshake cannot authorize that completed handshake', async t => {
  const f = fixture(t); f.select('snowluma'); const wait = f.deferred()
  const connection = { socket: {}, connected: true, lanyaBackendTransportVersion: 1,
    call: async action => action === 'get_version_info' ? wait.promise : { user_id: 303 } }
  const guard = createBackendGuard({ workspace: f.root }), pending = guard.verify(connection)
  delete connection.lanyaBackendTransportVersion; wait.resolve({ app_name: 'SnowLuma' })
  assert.equal((await pending).reason, 'backend_runtime_unverified')
  assert.equal(guard.edge(connection), false)
})

test('SnowLuma complete adapter overrides, snapshots, disabled and ambiguous adapters match the contract', t => {
  const f = fixture(t); f.select('snowluma'); f.write('fixture/lanya-config.json', { botQQ: '303' })
  f.write('snowluma/config/onebot_303.json', { networks: { httpServers: [{ name: 'http-default', port: 8731, path: '/api' }] } })
  let result = resolveOneBotEndpoint(f.root); assert.equal(result.token, ''); assert.equal(result.base, 'http://127.0.0.1:8731/api')
  f.write('snowluma/config/onebot_303.json', { mode: 'snapshot', networks: { httpServers: [
    { name: 'disabled', port: 8733, enabled: false }, { name: 'account', port: 8732, accessToken: 'fixture-account' }] } })
  result = resolveOneBotEndpoint(f.root); assert.equal(result.token, 'fixture-account')
  f.write('snowluma/config/onebot_303.json', { networks: { httpServers: [{ name: 'extra', port: 8734 }] } })
  assert.throws(() => resolveOneBotEndpoint(f.root))
  f.select('snowluma', 2, { snowluma: { httpServer: 'extra' } }); assert.equal(resolveOneBotEndpoint(f.root).base, 'http://127.0.0.1:8734')
})

test('selected invalid or missing SnowLuma never falls back to available NapCat', t => {
  const f = fixture(t); f.select('snowluma'); const file = path.join(f.root, 'snowluma/config/onebot.json')
  for (const raw of ['{', '[]', 'null']) { fs.writeFileSync(file, raw); assert.throws(() => resolveOneBotEndpoint(f.root)) }
  fs.unlinkSync(file); assert.throws(() => resolveOneBotEndpoint(f.root))
})

test('remote hosts, credentials in URLs and paths escaping the action prefix are refused', t => {
  const f = fixture(t); f.select('snowluma')
  for (const row of [{ host: 'example.test' }, { port: 0 }, { path: '/..' }, { path: '/%2e' }, { path: '/?access_token=x' }, { path: '/\\else' }]) {
    f.write('snowluma/config/onebot.json', { networks: { httpServers: [{ name: 'fixture', port: 8730, ...row }] } })
    assert.throws(() => resolveOneBotEndpoint(f.root))
  }
})

test('one successful provider/account handshake serves repeated checks on the exact socket', async t => {
  const f = fixture(t); f.select('snowluma'); let calls = 0
  const guard = createBackendGuard({ workspace: f.root, expectedAccount: '303' })
  const connection = { socket: {}, connected: true, lanyaBackendTransportVersion: 1, async call(action) { calls++; return action === 'get_version_info' ? { app_name: 'SnowLuma' } : { user_id: 303 } } }
  const stamp = guard.selection().stamp
  for (let i = 0; i < 30; i++) assert.equal((await guard.verify(connection, stamp)).allowed, true)
  assert.equal(calls, 2); assert.equal(guard.edge(connection, stamp), true)
  connection.socket = {}; assert.equal(guard.edge(connection, stamp), false)
  assert.equal((await guard.verify(connection, stamp)).allowed, true); assert.equal(calls, 4)
})

test('wrong provider, wrong account, disconnected and failed envelopes cannot authorize a socket', async t => {
  const f = fixture(t); f.select('snowluma')
  const cases = [
    { app_name: 'NapCat.Onebot' },
    { status: 'failed', retcode: 100, data: { app_name: 'SnowLuma' } },
    { status: 'ok', retcode: 0, data: { app_name: 'SnowLuma' }, wrongAccount: true }
  ]
  for (const value of cases) {
    const guard = createBackendGuard({ workspace: f.root, expectedAccount: '303' })
    const connection = { socket: {}, connected: true, lanyaBackendTransportVersion: 1, async call(action) { return action === 'get_version_info' ? value : { user_id: 404 } } }
    assert.equal((await guard.verify(connection)).allowed, false); assert.equal(guard.edge(connection), false)
  }
  const guard = createBackendGuard({ workspace: f.root }); assert.equal((await guard.verify({ connected: false })).allowed, false)
})

test('selection and socket changes while a handshake is in flight invalidate the old result', async t => {
  const f = fixture(t); f.select('snowluma'); const wait = f.deferred()
  const guard = createBackendGuard({ workspace: f.root }), old = guard.selection().stamp
  const connection = { socket: {}, connected: true, lanyaBackendTransportVersion: 1, call: action => action === 'get_login_info' ? Promise.resolve({user_id:303}) : wait.promise }
  const pending = guard.verify(connection, old); f.select('napcat', 2); wait.resolve({ app_name: 'SnowLuma' })
  assert.equal((await pending).allowed, false); assert.equal(guard.current(old), false); assert.equal(guard.edge(connection, old), false)
  f.select('snowluma', 3); assert.equal(guard.current(old), false)
  const later = f.deferred(); connection.call = () => later.promise
  const task = guard.verify(connection); connection.socket = {}; later.resolve({ app_name: 'SnowLuma' })
  assert.equal((await task).allowed, false)
})

test('a fixed socket mismatch is checked once and a transient failure has a bounded cooldown', async t => {
  const f = fixture(t); f.select('snowluma'); let calls = 0, clock = 0, fails = true
  const guard = createBackendGuard({ workspace: f.root, now: () => clock })
  const connection = { socket: {}, connected: true, lanyaBackendTransportVersion: 1, async call(action) { calls++; if (fails) throw new Error('fixture transient'); return action==='get_version_info'?{ app_name: 'SnowLuma' }:{user_id:303} } }
  for (let i = 0; i < 20; i++) assert.equal((await guard.verify(connection)).allowed, false)
  assert.equal(calls, 1); clock = 1000; fails = false
  assert.equal((await guard.verify(connection)).allowed, true); assert.equal(calls, 3)
  connection.socket = {}; connection.call = async () => { calls++; return { app_name: 'NapCat.Onebot' } }
  for (let i = 0; i < 20; i++) { clock += 10000; assert.equal((await guard.verify(connection)).allowed, false) }
  assert.equal(calls, 4); assert.equal(guard.edge(connection), false)
  connection.socket = {}; connection.call = async action => { calls++; return action==='get_version_info'?{ app_name: 'SnowLuma' }:{user_id:303} }
  assert.equal((await guard.verify(connection)).allowed, true); assert.equal(calls, 6)
})

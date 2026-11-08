/** Guard negative cases launch only inert Node entries, never Edge or services. */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { pathToFileURL, fileURLToPath } from 'node:url'
import { requireBrowserFixture, installFixtureBrowserNetwork, fixtureRequestAllowed, fetchFixtureApi, PRODUCTION_PORTS } from './browser-fixture-guard.mjs'

const origin = 'http://127.0.0.1:60001', key = 'a'.repeat(64)
const outDir = path.join(os.tmpdir(), 'lanya-browser-fixture-' + 'b'.repeat(32))
const args = ['--fixture-url', origin, '--fixture-key', key, '--fixture-out', outDir, '--fixture-debug-port', '60002']
const now = () => 1000000
const health = () => ({ schema: 'lanya-isolated-browser-fixture/v1', isolated: true, temporary_data: true,
  origin, fixture_key: key, browser_policy: 'fixture-origin-only', expires_at: now() + 60000 })
const response = value => ({ ok: true, text: async () => JSON.stringify(value) })

test('default, environment-only switches and production ports are refused before fetch or filesystem', async () => {
  let calls = 0
  const denied = () => { calls++; throw new Error('I/O must not run') }
  const previous = process.env.LANYA_ALLOW_LIVE_TESTS
  process.env.LANYA_ALLOW_LIVE_TESTS = '1'
  try {
    await assert.rejects(requireBrowserFixture({ args: [], fetchImpl: denied, exists: denied }), /refused/)
    for (const port of PRODUCTION_PORTS) {
      const value = args.slice(); value[1] = 'http://127.0.0.1:' + port
      await assert.rejects(requireBrowserFixture({ args: value, fetchImpl: denied, exists: denied }), /refused/)
    }
    assert.equal(calls, 0)
  } finally {
    if (previous === undefined) delete process.env.LANYA_ALLOW_LIVE_TESTS; else process.env.LANYA_ALLOW_LIVE_TESTS = previous
  }
})

test('fixture identity must be temporary, current, exact-origin and match the explicit key', async () => {
  for (const change of [{ isolated: false }, { temporary_data: false }, { expires_at: now() },
    { expires_at: now() + 300001 }, { origin: 'http://127.0.0.1:19389' }, { fixture_key: 'c'.repeat(64) },
    { browser_policy: 'anything' }, { schema: 'lanya-panel' }]) {
    await assert.rejects(requireBrowserFixture({ args, now, fetchImpl: async () => response({ ...health(), ...change }), exists: () => false }), /refused/)
  }
  const fixture = await requireBrowserFixture({ args, now, fetchImpl: async (url, options) => {
    assert.equal(url, origin + '/__lanya_test_fixture__/health'); assert.equal(options.redirect, 'error')
    return response(health())
  }, exists: () => false })
  assert.deepEqual(fixture, { origin, outDir, debugPort: 60002 })
  await assert.rejects(requireBrowserFixture({ args, now, fetchImpl: async () => response(health()), exists: () => true }), /refused/)
})

test('non-loopback, redirects, output outside temporary fixture and duplicate flags fail closed', async () => {
  for (const value of [args.concat('--fixture-key', key), args.map((v, i) => i === 1 ? 'http://localhost:60001' : v),
    args.map((v, i) => i === 1 ? 'https://127.0.0.1:60001' : v),
    args.map((v, i) => i === 5 ? path.resolve('tests') : v), args.map((v, i) => i === 7 ? '60001' : v)]) {
    await assert.rejects(requireBrowserFixture({ args: value, fetchImpl: async () => { throw new Error('must not fetch') } }), /refused/)
  }
  await assert.rejects(requireBrowserFixture({ args, now, fetchImpl: async () => { throw new Error('redirect refused') }, exists: () => false }), /refused/)
})

test('Node fixture API reads and POSTs refuse 307/308 before any secondary transport', async () => {
  const fixture = { origin, outDir, debugPort: 60002 }
  for (const status of [307, 308]) {
    for (const method of ['GET', 'POST']) {
      const transports = []
      const fetchImpl = async (url, options) => {
        transports.push({ url, method: options.method })
        assert.equal(options.redirect, 'error')
        // A transport honoring redirect:error stops at the first response.
        // Any accidental follow option would reproduce the unsafe second hop.
        if (options.redirect !== 'error') transports.push({ url: 'http://127.0.0.1:19389/api/persona', method })
        return { status, redirected: false, url, headers: { location: 'http://127.0.0.1:19389/api/persona' } }
      }
      await assert.rejects(fetchFixtureApi(fixture, '/api/persona',
        { method, body: method === 'POST' ? '{"text":"fixture-only"}' : undefined, redirect: 'follow' }, fetchImpl), /refused/)
      assert.deepEqual(transports, [{ url: origin + '/api/persona', method }])
    }
  }
  let calls = 0
  await assert.rejects(fetchFixtureApi(fixture, 'http://127.0.0.1:19389/api/persona',
    { method: 'POST' }, async () => { calls++; throw new Error('production transport') }), /refused/)
  assert.equal(calls, 0)
  const ok = { status: 200, redirected: false, url: origin + '/api/budget' }
  assert.equal(await fetchFixtureApi(fixture, '/api/budget', {}, async (_url, options) => {
    assert.equal(options.redirect, 'error'); return ok
  }), ok)
})

test('effect entry routes every Node API request through the fixture guard', () => {
  const source = fs.readFileSync(new URL('./effect_test.mjs', import.meta.url), 'utf8')
  assert.match(source, /fetchFixtureApi\(fixture, API \+ p,/)
  assert.match(source, /fetchFixtureApi\(fixture, API \+ '\/api\/persona',/)
  assert.doesNotMatch(source, /\bfetch\(API/)
  assert.match(source, /fetch\(`http:\/\/127\.0\.0\.1:\$\{PORT\}\/json\/list`, \{ redirect: 'error' \}\)/)
})

class FakeSocket {
  constructor(fixture, wrongProfile = false) { this.fixture = fixture; this.wrongProfile = wrongProfile; this.listeners = new Set(); this.commands = [] }
  addEventListener(_name, callback) { this.listeners.add(callback) }
  removeEventListener(_name, callback) { this.listeners.delete(callback) }
  emit(value) { for (const callback of this.listeners) callback({ data: JSON.stringify(value) }) }
  send(raw) {
    const value = JSON.parse(raw); this.commands.push(value)
    const result = value.method === 'Browser.getBrowserCommandLine' ? { arguments: [
      '--remote-debugging-port=' + this.fixture.debugPort,
      '--user-data-dir=' + path.join(this.wrongProfile ? os.tmpdir() : this.fixture.outDir, '_profile')
    ] } : {}
    queueMicrotask(() => this.emit({ id: value.id, result }))
  }
}

test('browser interception blocks a copied page hardcoded to real panel APIs', async () => {
  const fixture = { origin, outDir, debugPort: 60002 }, ws = new FakeSocket(fixture)
  const remove = await installFixtureBrowserNetwork(ws, fixture)
  assert.equal(ws.commands[0].method, 'Browser.getBrowserCommandLine')
  assert.equal(ws.commands[1].method, 'Fetch.enable')
  ws.emit({ method: 'Fetch.requestPaused', params: { requestId: 'production', request: { url: 'http://127.0.0.1:19389/api/budget' } } })
  ws.emit({ method: 'Fetch.requestPaused', params: { requestId: 'fixture', request: { url: origin + '/api/budget' } } })
  await new Promise(resolve => setImmediate(resolve))
  assert.ok(ws.commands.some(row => row.method === 'Fetch.failRequest' && row.params.requestId === 'production'))
  assert.ok(ws.commands.some(row => row.method === 'Fetch.continueRequest' && row.params.requestId === 'fixture'))
  assert.equal(fixtureRequestAllowed('http://localhost:19389/api/action', fixture), false)
  assert.equal(fixtureRequestAllowed('https://example.invalid/image.png', fixture), false)
  remove()
})

test('a preexisting browser with another profile is refused before navigation or request continuation', async () => {
  const fixture = { origin, outDir, debugPort: 60002 }, ws = new FakeSocket(fixture, true)
  await assert.rejects(installFixtureBrowserNetwork(ws, fixture), /refused/)
  assert.deepEqual(ws.commands.map(row => row.method), ['Browser.getBrowserCommandLine'])
})

test('actual five manual entrypoints refuse default and production targets before mkdir/spawn/fetch', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'manual-guard-test-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const preload = path.join(root, 'no-side-effects.mjs')
  fs.writeFileSync(preload, `import fs from 'node:fs';import cp from 'node:child_process';import {syncBuiltinESMExports} from 'node:module';
const denied=()=>{throw new Error('SIDE_EFFECT_BOUNDARY_CALLED')};fs.mkdirSync=denied;cp.spawn=denied;globalThis.fetch=denied;syncBuiltinESMExports();`)
  const tests = path.dirname(fileURLToPath(import.meta.url))
  for (const name of ['effect_test.mjs', 'browser_test.mjs', 'click_all.mjs', 'probe_memes.mjs', 'visual_check.mjs']) {
    for (const extra of [[], ...[19388, 19389, 19387, 8643].map(port => {
      const value = args.slice(); value[1] = 'http://127.0.0.1:' + port; return value
    })]) {
      const result = spawnSync(process.execPath, ['--import', pathToFileURL(preload).href, path.join(tests, name), ...extra],
        { encoding: 'utf8', timeout: 10000, windowsHide: true })
      assert.notEqual(result.status, 0, name)
      assert.match(result.stderr, /Manual browser check refused/)
      assert.doesNotMatch(result.stdout + result.stderr, /SIDE_EFFECT_BOUNDARY_CALLED/)
    }
  }
})

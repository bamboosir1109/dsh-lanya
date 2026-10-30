/** Manual browser checks may write only to an explicitly identified fixture. */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

export const PRODUCTION_PORTS = Object.freeze([8643, 19387, 19388, 19389, 6099, 8720, 9880, 19400])
const refused = () => new Error('Manual browser check refused: explicit verified isolated fixture required')

export async function requireBrowserFixture({ args = process.argv.slice(2), fetchImpl = globalThis.fetch,
  now = Date.now, tempDir = os.tmpdir(), exists = fs.existsSync } = {}) {
  // Environment switches deliberately grant no permission. Every input must be
  // explicit, and a real production API cannot satisfy this fixture handshake.
  const values = {}
  for (let i = 0; i < args.length; i += 2) {
    const key = args[i]
    if (!['--fixture-url', '--fixture-key', '--fixture-out', '--fixture-debug-port'].includes(key) ||
        key in values || typeof args[i + 1] !== 'string') throw refused()
    values[key] = args[i + 1]
  }
  if (Object.keys(values).length !== 4) throw refused()
  let target
  try { target = new URL(values['--fixture-url']) } catch { throw refused() }
  const port = Number(target.port), debugPort = Number(values['--fixture-debug-port'])
  if (target.protocol !== 'http:' || target.hostname !== '127.0.0.1' || target.username || target.password ||
      target.pathname !== '/' || target.search || target.hash ||
      !Number.isInteger(port) || port < 49152 || port > 65535 || PRODUCTION_PORTS.includes(port) ||
      !Number.isInteger(debugPort) || debugPort < 49152 || debugPort > 65535 || debugPort === port ||
      PRODUCTION_PORTS.includes(debugPort) || !/^[a-f0-9]{64}$/.test(values['--fixture-key'])) throw refused()
  const outDir = path.resolve(values['--fixture-out'])
  if (!path.isAbsolute(values['--fixture-out']) || path.dirname(outDir) !== path.resolve(tempDir) ||
      !/^lanya-browser-fixture-[a-f0-9]{16,64}$/.test(path.basename(outDir))) throw refused()
  try {
    const response = await fetchImpl(target.origin + '/__lanya_test_fixture__/health', {
      redirect: 'error', signal: AbortSignal.timeout(2000), cache: 'no-store',
      headers: { 'x-lanya-test-fixture': values['--fixture-key'] }
    })
    if (!response.ok) throw refused()
    const raw = await response.text()
    if (raw.length > 4096) throw refused()
    const health = JSON.parse(raw)
    if (health.schema !== 'lanya-isolated-browser-fixture/v1' || health.isolated !== true ||
        health.temporary_data !== true || health.origin !== target.origin ||
        health.fixture_key !== values['--fixture-key'] || health.browser_policy !== 'fixture-origin-only' ||
        !Number.isSafeInteger(health.expires_at) || health.expires_at <= now() || health.expires_at > now() + 300000) throw refused()
    if (exists(outDir)) throw refused()
  } catch { throw refused() }
  return Object.freeze({ origin: target.origin, outDir, debugPort })
}

export function fixtureRequestAllowed(url, fixture) {
  try {
    const target = new URL(url)
    return target.origin === fixture.origin || target.protocol === 'data:' || url === 'about:blank'
  } catch { return false }
}

export async function fetchFixtureApi(fixture, requestPath, options = {}, fetchImpl = globalThis.fetch) {
  // Node requests do not pass through the browser's CDP interceptor. Keep both
  // their first destination and every redirect inside the verified boundary.
  let target
  try { target = new URL(requestPath, fixture.origin) } catch { throw refused() }
  if (target.origin !== fixture.origin || target.username || target.password ||
      target.protocol !== 'http:') throw refused()
  const response = await fetchImpl(target.href, { ...options, redirect: 'error' })
  if (response.redirected || (response.status >= 300 && response.status < 400) ||
      (response.url && new URL(response.url).origin !== fixture.origin)) throw refused()
  return response
}

export async function installFixtureBrowserNetwork(ws, fixture) {
  // A copied panel can still contain a hardcoded production API URL. Intercept
  // browser requests before navigation so the page cannot escape its fixture.
  let id = 1000000000
  const pending = new Map()
  function command(method, params) {
    return new Promise((resolve, reject) => {
      const key = ++id
      const timer = setTimeout(() => { pending.delete(key); reject(refused()) }, 3000)
      pending.set(key, { resolve, reject, timer })
      ws.send(JSON.stringify({ id: key, method, params }))
    })
  }
  const message = event => {
    let value
    try { value = JSON.parse(event.data) } catch { return }
    const item = pending.get(value.id)
    if (item) {
      clearTimeout(item.timer); pending.delete(value.id)
      if (value.error) item.reject(refused()); else item.resolve(value.result)
    }
    if (value.method === 'Fetch.requestPaused') {
      const requestId = value.params?.requestId
      const allowed = fixtureRequestAllowed(value.params?.request?.url, fixture)
      void command(allowed ? 'Fetch.continueRequest' : 'Fetch.failRequest',
        allowed ? { requestId } : { requestId, errorReason: 'BlockedByClient' }).catch(() => {})
    }
  }
  ws.addEventListener('message', message)
  const browser = await command('Browser.getBrowserCommandLine', {})
  const argv = browser?.arguments
  const profiles = Array.isArray(argv) ? argv.filter(value => value.startsWith('--user-data-dir=')) : []
  if (!Array.isArray(argv) || argv.filter(value => value === '--remote-debugging-port=' + fixture.debugPort).length !== 1 ||
      profiles.length !== 1 || path.dirname(path.resolve(profiles[0].slice('--user-data-dir='.length))) !== fixture.outDir) throw refused()
  await command('Fetch.enable', { patterns: [{ urlPattern: '*', requestStage: 'Request' }] })
  return () => {
    ws.removeEventListener('message', message)
    for (const item of pending.values()) { clearTimeout(item.timer); item.reject(refused()) }
    pending.clear()
  }
}

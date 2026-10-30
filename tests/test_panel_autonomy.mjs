/** Local API/DOM fixtures only; no real process, network, model or delivery. */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { componentAction, panelAutonomy, sanitizeAutonomy, decorateAutonomyComponents } from '../plugin/lib/index.js'

const runtime = { napcatDir: '/synthetic/napcat', project: { decisions: { mode: 'unified' }, panelPort: 22345 } }
const observer = { schema_version: 2, scope_count: 2, pending_count: 0, scheduled_review_count: 0,
  unknown_count: 0, error_count: 0, last_completed_at: 1000, next_review_at: null, next_opportunity_at: 2000 }
const healthy = { mode: 'unified', status: 'waiting', reason: 'waiting_for_opportunity', can_request_check: true,
  scheduler_verified: true, consumer_verified: true, observer }
const never = () => { throw new Error('Independent loop must never be touched') }

test('both unified Life aliases and restart route only to canonical observation producer', async () => {
  const calls = []
  const deps = { launch: never, stop: never, autonomy: async (r, opts) => {
    calls.push([r, opts]); return { ok: true, observation_only: true, decision_completed: false }
  } }
  for (const target of ['lifeloop', 'mind']) for (const op of ['run', 'start', 'restart', 'check']) {
    const result = await componentAction(runtime, { target, op }, deps)
    assert.equal(result.observation_only, true)
    assert.equal(result.decision_completed, false)
  }
  assert.equal(calls.length, 8)
  assert.ok(calls.every(([r, opts]) => r === runtime && opts.check === true))
  await assert.rejects(componentAction(runtime, { target: 'mind', op: 'stop' }, deps), /不停止/)
  for (const target of ['voicedaemon', 'supervisor', 'tick']) {
    await assert.rejects(componentAction(runtime, { target }, deps), /停用/)
  }
})

test('legacy launch and stop remain explicit and invalid mode fails closed', async () => {
  const legacy = { ...runtime, project: { decisions: { mode: 'legacy' } } }
  const calls = []
  const launched = await componentAction(legacy, { target: 'lifeloop' }, {
    launch: async (...args) => { calls.push(args); return 25 }, autonomy: never
  })
  assert.deepEqual(launched, { ok: true, ran: 'lifeloop.ps1', pid: 25 })
  assert.deepEqual(calls, [['lifeloop.ps1', '/synthetic/napcat', 'python']])
  assert.equal((await componentAction(legacy, { target: 'mind', op: 'stop' }, { stop: async () => 0 })).stopped, 0)
  await assert.rejects(componentAction({ ...runtime, project: { decisions: { mode: 'invalid' } } }, { target: 'lifeloop' }), /Invalid/)
})

test('one loopback request has no caller-controlled owner and does not claim decision completion', async () => {
  const calls = []
  const result = await panelAutonomy(runtime, { check: true, fetchImpl: async (url, options) => {
    calls.push([url, options]); return { ok: true, json: async () => ({ ok: true, status: 'requested',
      observation_only: true, decision_completed: false, producer_started: true, note: 'UNTRUSTED PRIVATE TEXT' }) }
  } })
  assert.equal(calls[0][0], 'http://127.0.0.1:22345/api/autonomy/check')
  assert.equal(calls[0][1].body, '{}')
  assert.equal(result.observation_only, true)
  assert.equal(result.decision_completed, false)
  assert.doesNotMatch(JSON.stringify(result), /UNTRUSTED/)
})

test('API offline and forged completion cannot fall back to old Life', async () => {
  const down = await panelAutonomy(runtime, { fetchImpl: async () => { throw new Error('offline') } })
  assert.equal(down.status, 'unknown')
  assert.equal(down.can_request_check, false)
  const response = await panelAutonomy(runtime, { check: true, fetchImpl: async () => ({ ok: true,
    json: async () => ({ ok: true, status: 'requested', observation_only: true, producer_started: true, decision_completed: true }) }) })
  assert.equal(response.ok, false)
})

test('sanitized status preserves real zero counts and removes identifiers/private text', () => {
  const value = sanitizeAutonomy({ ...healthy, pid: 123, scope: 'private:101', plan: 'PRIVATE',
    observer: { ...observer, scopes: ['private:101'], event_id: 'PRIVATE EVENT' } })
  const state = decorateAutonomyComponents({ components: [{ tag: 'mind', count: 0, mb: 0 }] }, value)
  assert.equal(state.components[0].count, 0)
  assert.equal(state.components[0].control, 'request_check')
  assert.equal(state.components[0].status, 'waiting')
  assert.doesNotMatch(JSON.stringify(state), /private:101|PRIVATE|"pid"/)
  assert.equal(sanitizeAutonomy({ ...healthy, consumer_verified: false }).can_request_check, false)
})

const panel = fs.readFileSync(new URL('../panel/panel.html', import.meta.url), 'utf8')
const plugin = fs.readFileSync(new URL('../plugin/lib/index.js', import.meta.url), 'utf8')

function mainContext(api) {
  const messages = []
  const context = vm.createContext({ api, show: (_id, text) => messages.push(text), setTimeout() {},
    refreshOverview() {}, loadActivities() {}, loadLive() {} })
  vm.runInContext(panel.slice(panel.indexOf('let componentActionPending = false;'), panel.indexOf('const PART = {')), context)
  return { context, messages }
}

test('main UI repeated click submits once and displays request semantics', async () => {
  let finish, count = 0
  const { context, messages } = mainContext(async () => { count++; return new Promise(resolve => { finish = resolve }) })
  const first = vm.runInContext("doAction('lifeloop','partsOut')", context)
  await vm.runInContext("doAction('lifeloop','partsOut')", context)
  assert.equal(count, 1)
  finish({ ok: true, observation_only: true, producer_started: true, decision_completed: false, note: '已提交观察，不代表已开始活动' })
  await first
  assert.match(messages.at(-1), /不代表已开始活动/)
  assert.doesNotMatch(messages.at(-1), /进程 undefined|已启动：/)
})

test('main overview renders zero process managed status and request-check control', () => {
  const nodes = new Map(), rendered = []
  const element = () => ({ innerHTML: '', appendChild(row) { rendered.push(row.innerHTML) } })
  const { context } = mainContext(async () => {})
  Object.assign(context, { s: { components: decorateAutonomyComponents({ components: [{ tag: 'lifeloop', count: 0, mb: 0 }] }, healthy).components },
    PART: { lifeloop: ['自主活动', 'legacy'] }, setText() {}, $: id => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id) },
    document: { createElement: element } })
  vm.runInContext(panel.slice(panel.indexOf('const comps = s.components || [];'), panel.indexOf("const pb = $('pluginsBody');")), context)
  assert.match(rendered[0], /主 agent 托管，等待自主机会/)
  assert.match(rendered[0], /请求检查/)
  assert.match(rendered[0], /class="num">0<\/td>/)
  assert.doesNotMatch(rendered[0], />启动<|>重启</)
})

test('observer evidence errors remain aggregate and render a warning before scheduled review', () => {
  const status = sanitizeAutonomy({ ...healthy, status: 'uncertain', reason: 'observer_evidence_missing',
    observer: { ...observer, error_count: 1, next_review_at: 1000, private_scope: 'private:101', error_class: 'FileNotFoundError' } })
  assert.equal(status.observer.error_count, 1)
  assert.doesNotMatch(JSON.stringify(status), /private:101|FileNotFoundError/)
  const { context } = mainContext(async () => {})
  const rendered = [], table = { innerHTML: '', appendChild(row) { rendered.push(row.innerHTML) } }
  Object.assign(context, { s: { components: decorateAutonomyComponents({ components: [{ tag: 'lifeloop', count: 0, mb: 0 }] }, status).components },
    PART: { lifeloop: ['自主活动', 'legacy'] }, setText() {}, $: () => table, document: { createElement: () => ({ innerHTML: '' }) } })
  vm.runInContext(panel.slice(panel.indexOf('const comps = s.components || [];'), panel.indexOf("const pb = $('pluginsBody');")), context)
  assert.match(rendered[0], /部分观察证据缺失/)
  assert.match(rendered[0], /dot multi/)
  assert.doesNotMatch(rendered[0], /dot up/)
})

test('embedded plugin panel displays managed zero and deduplicates click while request pending', async () => {
  const nodes = new Map(), rendered = []
  const get = id => { if (!nodes.has(id)) nodes.set(id, { textContent: '', innerHTML: '',
    appendChild(row) { rendered.push(row.innerHTML) }, querySelectorAll() { return [] } }); return nodes.get(id) }
  let finish, requests = 0
  const context = vm.createContext({ document: { getElementById: get, createElement: () => ({ innerHTML: '' }) },
    fetch: async () => { requests++; return new Promise(resolve => { finish = resolve }) }, refresh() {} })
  vm.runInContext(plugin.slice(plugin.indexOf('const $ = (id) => document.getElementById(id);'), plugin.indexOf('async function refresh(){')), context)
  context.s = { components: decorateAutonomyComponents({ components: [{ tag: 'mind', count: 0, mb: 0, aging: 0 }] }, healthy).components }
  vm.runInContext(plugin.slice(plugin.indexOf("const tb = $('parts');"), plugin.indexOf("tb.querySelectorAll('button').forEach")), context)
  assert.match(rendered[0], /主 agent 托管，等待自主机会/)
  assert.match(rendered[0], /请求检查/)
  assert.match(rendered[0], /class="num">0<\/td>/)
  vm.runInContext(plugin.slice(plugin.indexOf('async function act(target, op){'), plugin.indexOf('async function plugin(op){')), context)
  const first = vm.runInContext("act('mind','check')", context)
  await vm.runInContext("act('mind','check')", context)
  assert.equal(requests, 1)
  finish({ json: async () => ({ observation_only: true, note: '只提交观察' }) })
  await first
  assert.equal(get('out').textContent, '只提交观察')
})

test('all inline frontend scripts parse without browser execution', () => {
  const inline = [...panel.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(match => match[1])
  assert.ok(inline.length)
  inline.forEach(code => new vm.Script(code))
})

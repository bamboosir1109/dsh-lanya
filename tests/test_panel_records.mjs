// Actual panel functions with synthetic DOM, fetch and clock only.
// Cases are added against the finalized refresh contract; no live API is used.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'
import { test } from 'node:test'

const PANEL = fileURLToPath(new URL('../panel/panel.html', import.meta.url))

function panelFunction(source, name) {
  const match = new RegExp('(?:async )?function ' + name + '\\(').exec(source)
  assert.ok(match, 'Actual panel function exists: ' + name)
  const end = source.indexOf('\n}', match.index)
  assert.ok(end > match.index, 'Top-level function boundary exists: ' + name)
  return source.slice(match.index, end + 2)
}

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function response(data, status = 200) {
  return { ok: status >= 200 && status < 300, status,
    headers: { get() { return 'application/json' } },
    async json() { return structuredClone(data) },
    async text() { return JSON.stringify(data) } }
}

function element() {
  const classes = new Set(), listeners = new Map()
  return { style: {}, dataset: {}, value: '', textContent: '', innerHTML: '',
    hidden: false, disabled: false, checked: false, children: [],
    classList: { add(...names) { names.forEach(name => classes.add(name)) },
      remove(...names) { names.forEach(name => classes.delete(name)) },
      contains(name) { return classes.has(name) },
      toggle(name, value) {
        const on = value ?? !classes.has(name)
        if (on) classes.add(name); else classes.delete(name)
        return on
      } },
    addEventListener(name, callback) { listeners.set(name, callback) },
    appendChild(child) { this.children.push(child); return child },
    querySelector() { return null }, querySelectorAll() { return [] },
    setAttribute(name, value) { this[name] = value },
    removeAttribute(name) { delete this[name] },
    listeners }
}

function harness({ fetch: mockFetch, now = Date.UTC(2026, 10, 1, 12), extra = {}, functions = [] } = {}) {
  const source = readFileSync(PANEL, 'utf8').replaceAll('\r\n', '\n')
  const nodes = new Map(), calls = [], intervals = [], timeouts = [], events = new Map()
  const node = id => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id) }
  const document = { hidden: false, visibilityState: 'visible', activeElement: null,
    getElementById: node, querySelector(selector) { return selector === 'main' ? node('main') : null }, querySelectorAll() { return [] },
    createElement: element, addEventListener(name, callback) { events.set(name, callback) } }
  class FixtureDate extends Date {
    constructor(...args) { super(...(args.length ? args : [now])) }
    static now() { return now }
  }
  const context = vm.createContext({ document, location: { hash: '#/overview', origin: 'http://fixture.invalid', protocol: 'http:' },
    Date: FixtureDate, URL, Promise, structuredClone, encodeURIComponent, decodeURIComponent,
    AbortController, API: 'http://127.0.0.1:19389', APIX: '/api',
    currentPage: 'bili', recordStates: new Map(), recordRequests: new Map(),
    refreshingPages: new Set(), dirtyPages: new Set(), pageEdits: new Map(),
    autonomySettingsRevision: null, budgetSettingsRevision: null,
    autonomySettingsEpoch: 0, autonomySettingsSaving: false,
    budgetSettingsEpoch: 0, budgetSettingsSaving: false,
    searchOn: false, selected: {}, workSel: {}, currentDir: 'diary', currentWorkDir: 'made',
    console: { log() {}, warn() {}, error() {} },
    $: node, setText(id, value) { node(id).textContent = value },
    setHtml(id, value) { node(id).innerHTML = value },
    hide(id) { node(id).style.display = 'none' },
    show(id, value) { node(id).style.display = 'block'; node(id).textContent = value },
    setInterval(callback, delay) { intervals.push({ callback, delay }); return intervals.length },
    clearInterval() {},
    setTimeout(callback, delay) { timeouts.push({ callback, delay }); return timeouts.length },
    clearTimeout() {},
    fetch: async (url, options = {}) => {
      calls.push({ url: String(url), options })
      if (!mockFetch) throw new Error('Unexpected fixture fetch')
      return mockFetch(String(url), options, calls.length)
    }, ...extra })
  for (const name of functions) vm.runInContext(panelFunction(source, name), context)
  return { source, context, nodes, node, calls, intervals, timeouts, events,
    setNow(value) { now = value },
    load(...names) { for (const name of names) vm.runInContext(panelFunction(source, name), context) },
    run(code) { return vm.runInContext(code, context) } }
}

const API_FUNCTIONS = ['recordPage', 'noteRecord', 'renderRecordSync', 'staticFor', 'api']

test('actual API prefers same-origin live GET and never reads an old snapshot on success', async () => {
  const h = harness({ functions: API_FUNCTIONS, fetch: async url => {
    assert.equal(url, '/api/bili')
    return response({ ok: true, bili: { activity_count: 2 } })
  } })
  const value = await h.context.api('/bili')
  assert.equal(value.bili.activity_count, 2)
  assert.equal(h.calls.length, 1)
  assert.equal(h.calls[0].options.cache, 'no-store')
  assert.equal(h.node('recordSync').dataset.state, 'live')
})

test('actual GET tries the alternate local API after a same-origin transport failure', async () => {
  const h = harness({ functions: API_FUNCTIONS, fetch: async (_url, _opts, number) => {
    if (number === 1) throw new TypeError('synthetic transport failure')
    return response({ ok: true, budget: { spent: 0 } })
  } })
  const value = await h.context.api('/budget')
  assert.equal(value.budget.spent, 0)
  assert.deepEqual(h.calls.map(call => call.url), ['/api/budget', 'http://127.0.0.1:19389/api/budget'])
  assert.equal(h.calls.some(call => call.url.includes('.json')), false)
})

test('recent schema-one snapshot is an explicit offline fallback after both HTTP failures', async () => {
  const now = Date.UTC(2026, 10, 1, 12)
  const h = harness({ now, functions: API_FUNCTIONS, fetch: async (url, _opts, number) => number < 3
    ? response({ error: 'synthetic unavailable' }, 503)
    : response({ ok: true, bili: { activity_count: 1 }, _snapshot: { schema: 1, generated_at: new Date(now - 60000).toISOString() } }) })
  const value = await h.context.api('/bili')
  assert.equal(value.bili.activity_count, 1)
  assert.equal(h.calls.length, 3)
  assert.match(h.calls[2].url, /^\.\/lanya-bili\.json\?t=/)
  assert.equal(h.node('recordSync').dataset.state, 'snapshot')
  assert.match(h.node('recordSyncText').textContent, /离线快照/)
})

test('missing wrong expired invalid and future snapshot metadata are rejected', async t => {
  const now = Date.UTC(2026, 10, 1, 12)
  const cases = [undefined, { schema: 2, generated_at: new Date(now).toISOString() },
    { schema: 1, generated_at: new Date(now - 120001).toISOString() },
    { schema: 1, generated_at: 'not a date' },
    { schema: 1, generated_at: new Date(now + 60000).toISOString() }]
  for (const meta of cases) await t.test(JSON.stringify(meta) ?? 'missing', async () => {
    const h = harness({ now, functions: API_FUNCTIONS, fetch: async (_url, _opts, number) => number < 3
      ? response({}, 503) : response({ ok: true, _snapshot: meta }) })
    await assert.rejects(h.context.api('/bili'), /有效快照|不可用/)
    assert.equal(h.calls.length, 3)
    assert.equal(h.node('recordSync').dataset.state, 'error')
  })
})

test('live application error is returned and marked without hiding it behind another endpoint', async () => {
  const h = harness({ functions: API_FUNCTIONS, fetch: async () => response({ ok: false, error: 'synthetic source failure' }) })
  const result = await h.context.api('/bili')
  assert.equal(result.ok, false)
  assert.equal(h.calls.length, 1)
  assert.equal(h.node('recordSync').dataset.state, 'error')
})

test('budget read failures mark the actual visible activity overview and system pages', async () => {
  for (const currentPage of ['activity', 'overview', 'system']) {
    const h = harness({ functions: API_FUNCTIONS, extra: { currentPage },
      fetch: async () => response({ ok: false, error: 'synthetic budget unavailable' }) })
    assert.equal((await h.context.api('/budget', null, true)).ok, false)
    assert.equal(h.node('recordSync').dataset.state, 'error', currentPage)
    assert.equal([...h.context.recordStates.values()][0].page, currentPage)
    assert.equal(h.calls.length, 1)
  }
})

test('live-only read does not fall back to an offline snapshot', async () => {
  const h = harness({ functions: API_FUNCTIONS, fetch: async () => response({}, 503) })
  await assert.rejects(h.context.api('/memory', null, true))
  assert.equal(h.calls.length, 2)
  assert.equal(h.calls.some(call => call.url.includes('.json')), false)
})

test('identical concurrent GETs share a single transport promise', async () => {
  const waiting = deferred()
  const h = harness({ functions: API_FUNCTIONS, fetch: () => waiting.promise })
  const one = h.context.api('/bili'), two = h.context.api('/bili')
  assert.equal(h.calls.length, 1)
  waiting.resolve(response({ ok: true, bili: { activity_count: 0 } }))
  assert.equal((await one).bili.activity_count, 0)
  assert.equal((await two).bili.activity_count, 0)
  assert.equal(h.context.recordRequests.size, 0)
})

test('mutation failure makes one original API call and never retries or reads a snapshot', async () => {
  const h = harness({ functions: API_FUNCTIONS, fetch: async () => { throw new TypeError('synthetic network failure') } })
  await assert.rejects(h.context.api('/action', { method: 'POST', body: '{"target":"fixture"}' }))
  assert.equal(h.calls.length, 1)
  assert.equal(h.calls[0].url, 'http://127.0.0.1:19389/api/action')
  assert.equal(h.calls[0].options.method, 'POST')
})

test('late successful save clears only its originating page edit marker', async () => {
  const waiting = deferred()
  const h = harness({ functions: API_FUNCTIONS, extra: { currentPage: 'persona' }, fetch: () => waiting.promise })
  h.context.dirtyPages.add('persona')
  const save = h.context.api('/persona', { method: 'POST', body: '{"text":"synthetic"}' })
  h.context.currentPage = 'identity'; h.context.dirtyPages.add('identity')
  waiting.resolve(response({ ok: true })); await save
  assert.equal(h.context.dirtyPages.has('persona'), false)
  assert.equal(h.context.dirtyPages.has('identity'), true)
})

test('edits made while a save is waiting remain dirty after its old response', async () => {
  const waiting = deferred()
  const h = harness({ functions: API_FUNCTIONS, extra: { currentPage: 'persona' }, fetch: () => waiting.promise })
  h.context.dirtyPages.add('persona'); h.context.pageEdits.set('persona', 1)
  const save = h.context.api('/persona', { method: 'POST', body: '{"text":"earlier fixture"}' })
  h.context.pageEdits.set('persona', 2)
  waiting.resolve(response({ ok: true })); await save
  assert.equal(h.context.dirtyPages.has('persona'), true)
})

const LOADERS = ['refreshOverview', 'loadBili', 'loadLive', 'loadAutonomySettings', 'loadActivityBudget', 'loadBudget', 'loadTools',
  'loadMemes', 'loadMemoryOverview', 'loadFiles', 'loadWorks', 'loadSchedule', 'loadConversationPolicy',
  'loadNapcat', 'loadPersona', 'loadConfig', 'loadBrand', 'loadSystem', 'loadMemeConfig', 'loadBiliCred']
function refreshHarness(extra = {}) {
  const counts = new Map(), args = new Map()
  const h = harness({ extra: { searchOn: false, selected: {}, workSel: {}, currentDir: 'notes', ...extra },
    functions: ['recordPage', 'renderRecordSync', 'refreshCurrentPage'] })
  for (const name of LOADERS) h.context[name] = async (...values) => {
    counts.set(name, (counts.get(name) ?? 0) + 1); args.set(name, values)
  }
  return Object.assign(h, { counts, args })
}

test('visible-page refresh is deduplicated until the actual loader completes', async () => {
  const h = refreshHarness(), waiting = deferred(); let calls = 0
  h.context.loadBili = () => { calls++; return waiting.promise }
  const one = h.context.refreshCurrentPage(false), two = h.context.refreshCurrentPage(false)
  assert.equal(calls, 1)
  assert.equal(h.node('recordRefresh').disabled, true)
  waiting.resolve(); await Promise.all([one, two])
  assert.equal(h.context.refreshingPages.size, 0)
  assert.equal(h.node('recordRefresh').disabled, false)
})

test('hidden page refresh is paused and visible page can refresh again', async () => {
  const h = refreshHarness()
  h.context.document.hidden = true; await h.context.refreshCurrentPage(false)
  assert.equal(h.counts.get('loadBili') ?? 0, 0)
  h.context.document.hidden = false; await h.context.refreshCurrentPage(false)
  assert.equal(h.counts.get('loadBili'), 1)
})

test('dirty forms and selected or expanded file lists avoid destructive background reloads', async () => {
  const h = refreshHarness({ currentPage: 'memory', selected: { 'synthetic.md': true } })
  await h.context.refreshCurrentPage(false)
  assert.equal(h.counts.get('loadMemoryOverview'), 1)
  assert.deepEqual(h.args.get('loadMemoryOverview'), [true])
  assert.equal(h.counts.get('loadFiles') ?? 0, 0)
  h.context.dirtyPages.add('memory'); await h.context.refreshCurrentPage(false)
  assert.equal(h.counts.get('loadMemoryOverview'), 1)
  h.context.dirtyPages.clear(); h.context.selected = {}
  h.context.document.querySelector = selector => selector === '#fileList .open' ? element() : null
  await h.context.refreshCurrentPage(false)
  assert.equal(h.counts.get('loadFiles') ?? 0, 0)
  h.context.currentPage = 'persona'; h.context.dirtyPages.add('persona')
  await h.context.refreshCurrentPage(true)
  assert.equal(h.counts.get('loadPersona') ?? 0, 0)
})

test('late old-directory response cannot replace the currently selected directory', async () => {
  const requests = { diary: deferred(), notes: deferred() }, rendered = []
  const h = harness({ extra: { currentDir: 'diary', fileCache: [], fileShown: 0, selected: {}, FILE_PAGE: 60,
    fileLoadGeneration: 0, updateBulk() {}, api: route => requests[decodeURIComponent(route.match(/dir=([^&]+)/)[1])].promise },
    functions: ['loadFiles'] })
  h.context.renderFileRows = dir => rendered.push({ dir, files: h.context.fileCache.map(file => file.name) })
  const old = h.context.loadFiles('diary')
  h.context.currentDir = 'notes'; const fresh = h.context.loadFiles('notes')
  requests.notes.resolve({ ok: true, files: [{ name: 'synthetic-note.md' }] }); await fresh
  requests.diary.resolve({ ok: true, files: [{ name: 'synthetic-diary.md' }] }); await old
  assert.equal(rendered.length, 1)
  assert.equal(rendered[0].dir, 'notes')
  assert.equal(h.context.fileCache[0].name, 'synthetic-note.md')
})

test('late old-directory success cannot mark the failed current directory as live', async () => {
  const waiting = deferred()
  const h = harness({ functions: API_FUNCTIONS, extra: { currentPage: 'memory', currentDir: 'diary' },
    fetch: async url => url.includes('dir=diary') ? waiting.promise
      : response({ ok: false, error: 'synthetic notes failure' }) })
  const old = h.context.api('/files?dir=diary')
  h.context.currentDir = 'notes'
  const current = await h.context.api('/files?dir=notes')
  assert.equal(current.ok, false)
  assert.equal(h.node('recordSync').dataset.state, 'error')
  waiting.resolve(response({ ok: true, files: [{ name: 'synthetic-diary.md' }] })); await old
  assert.equal(h.node('recordSync').dataset.state, 'error')
})

test('background budget refresh updates read-only totals without resetting an edited limit', async () => {
  const h = harness({ extra: { api: async () => ({ ok: true, budget: budgetData() }) },
    functions: BUDGET_RENDERERS.concat('loadBudget') })
  h.node('bAutonomy').value = 'unsaved fixture limit'
  await h.context.loadBudget(true)
  assert.equal(h.node('bAutonomy').value, 'unsaved fixture limit')
  assert.match(treeText(h.node('budgetBuckets')), /已计 \$0\.0000 · 预留 \$0\.0500/)
})

test('background memory read leaves the explicitly selected owner control alone', async () => {
  let ownersReloaded = 0
  const h = harness({ extra: { memOwnersLoad: async () => { ownersReloaded++ },
    api: async () => ({ ok: true, memory: { counts: { active: 0 }, entries: [] } }) },
    functions: ['loadMemoryOverview'] })
  h.node('memOwner').value = 'explicit synthetic owner'
  await h.context.loadMemoryOverview(true)
  assert.equal(ownersReloaded, 0)
  assert.equal(h.node('memOwner').value, 'explicit synthetic owner')
})

test('unchanged background file data preserves expansion pagination and selection', async () => {
  const files = [{ name: 'synthetic-note.md', size: 3 }]
  let redraws = 0, selectionsReset = 0
  const selected = { 'synthetic-note.md': true }
  const h = harness({ extra: { currentDir: 'notes', fileCache: files, fileShown: 60, selected,
    FILE_PAGE: 60, fileLoadGeneration: 0, api: async () => ({ ok: true, files }),
    renderFileRows() { redraws++ }, updateBulk() { selectionsReset++ } }, functions: ['loadFiles'] })
  await h.context.loadFiles('notes', true)
  assert.equal(redraws, 0); assert.equal(selectionsReset, 0)
  assert.equal(h.context.fileShown, 60)
  assert.equal(h.context.selected, selected)
})

test('unchanged background works data avoids redrawing a preview or selection', async () => {
  const files = [{ name: 'fixture/index.html', size: 12, mtime: 1, url: './fixture/index.html' }]
  let redraws = 0, selectionsReset = 0
  const selected = { 'fixture/index.html': true }
  const h = harness({ extra: { currentWorkDir: 'made', workExt: 'all', workLoadGeneration: 0,
    worksTruncated: false, workData: files, workSel: selected,
    api: async () => ({ ok: true, files }), renderWorks() { redraws++ }, updateWorkBulk() { selectionsReset++ } },
    functions: ['loadWorks'] })
  h.node('worksViewer').classList.add('on')
  await h.context.loadWorks(true)
  assert.equal(redraws, 0); assert.equal(selectionsReset, 0)
  assert.equal(h.context.workSel, selected)
  assert.equal(h.node('worksViewer').classList.contains('on'), true)
})

test('unchanged background meme groups avoid rebuilding the image grid', async () => {
  let redraws = 0
  const groups = [{ source: 'fixture', name: 'fixture', tag: 'happy', count: 1, files: ['image.png'] }]
  const h = harness({ extra: { memeFingerprint: JSON.stringify(groups),
    api: async () => ({ ok: true, total: 1, groups }), renderMemes() { redraws++ } },
    functions: ['loadMemes'] })
  await h.context.loadMemes(true)
  assert.equal(redraws, 0)
})

test('actual boot polls the visible records every fifteen seconds and pauses while hidden', async () => {
  const h = refreshHarness({ currentPage: 'bili', currentDir: 'notes' })
  h.context.location.hash = '#/bili'
  h.load('goto')
  const begin = h.source.indexOf('(async function boot(){')
  const end = h.source.indexOf('\n</script>', begin)
  assert.ok(begin >= 0 && end > begin)
  await h.run(h.source.slice(begin, end))
  assert.equal(h.counts.get('loadBili'), 1)
  assert.equal(h.intervals.length, 1)
  assert.equal(h.intervals[0].delay, 15000)
  h.intervals[0].callback(); await Promise.resolve(); await Promise.resolve()
  assert.equal(h.counts.get('loadBili'), 2)
  h.context.document.hidden = true
  h.intervals[0].callback(); await Promise.resolve()
  assert.equal(h.counts.get('loadBili'), 2)
  h.context.document.hidden = false
  h.events.get('visibilitychange')(); await Promise.resolve(); await Promise.resolve()
  assert.equal(h.counts.get('loadBili'), 3)
})

test('first memory status render uses the real top-level selection initialization before a file response', async () => {
  const h = refreshHarness({ currentPage: 'memory', currentDir: 'diary' })
  delete h.context.selected
  const declaration = h.source.match(/^(?:let|const|var)\s+selected\s*=\s*\{\}\s*;\s*$/m)
  assert.ok(declaration, 'The real page must initialize selected before navigation and async file results')
  h.run(declaration[0])
  assert.equal(h.run('Object.keys(selected).length'), 0)
  assert.doesNotThrow(() => h.context.renderRecordSync())
  await h.context.refreshCurrentPage(false)
  assert.equal(h.counts.get('loadFiles'), 1)
})


const BILI_RENDERERS = ['biliRecordTime', 'biliVideoLink', 'renderMainBili', 'renderBiliArchive']
const treeText = node => [node.textContent || '', ...node.children.map(treeText)].join(' ')
const descendants = node => [node, ...node.children.flatMap(descendants)]
const BUDGET_RENDERERS = ['appendPolicyRow', 'budgetAmount', 'renderBudgetSummary', 'renderBudget']
const AUTONOMY_RENDERERS = ['appendPolicyRow', 'renderAutonomySettings']
const budgetData = overrides => ({ schema: 'unified-budget/v1', availability: 'available', revision: 'a'.repeat(64),
  effective: { currency: 'USD', basis: 'meter_api_usd_estimate', limits: { autonomy: 1, private: 0, group: 0 }, reserve_usd: 0.05 },
  usage: { date: '2026-11-01', coverage_start: 1793520000000, buckets: {
    autonomy: { spent_usd: 0, reserved_usd: 0.05, remaining_usd: 0.95, unknown_count: 0, exhausted: false },
    private: { spent_usd: 0.12, reserved_usd: 0, remaining_usd: null, unknown_count: 0, exhausted: false },
    group: { spent_usd: 0.25, reserved_usd: 0, remaining_usd: null, unknown_count: 0, exhausted: false } } },
  enforcement: { before_turn: true, before_action: true, runtime_gate_bound: true, provider_hard_cap: false }, ...overrides })
const autonomyData = overrides => ({ schema: 'dsh-panel-autonomy-settings/v1', availability: 'available',
  mode: 'unified', revision: 'b'.repeat(64), writable_fields: ['opportunity_minutes'],
  effective: { opportunity_minutes: 3 }, existing_reviews_unchanged: true,
  group_observation: { availability: 'available', read_only: true, running_values_verified: false,
    activation: 'observer_startup_configuration', settings: { poll_seconds: 30, quiet_minutes: 20 } }, ...overrides })
const biliData = overrides => ({ status: 'ok', coverage: { owner_verified_scopes: 2 },
  records: [], recent_videos: [], replies: [], ...overrides })

test('new USD budget shows reserved and uncertain cost separately and never promises a provider hard cap', () => {
  const data = budgetData()
  data.usage.buckets.autonomy = { spent_usd: 0.2, reserved_usd: 0.3, remaining_usd: 0.5, unknown_count: 2, exhausted: false }
  const h = harness({ functions: BUDGET_RENDERERS })
  h.context.renderBudget(data, false, 0)
  const text = treeText(h.node('budgetBuckets')) + treeText(h.node('budgetEnforcement'))
  assert.match(text, /已计 \$0\.2000 · 预留 \$0\.3000/)
  assert.match(text, /2 轮费用待确认/)
  assert.match(text, /未知费用保留预留，不自动重试或伪造退款/)
  assert.match(h.node('mAccounting').textContent, /(?:真实|实际)模型轮次.*API.*估值/)
  assert.match(h.node('mAccounting').textContent, /历史费用不回填为零/)
  assert.equal(h.node('sMoney').textContent, '部分费用待确认')
  const meter = descendants(h.node('budgetBuckets')).find(node => node.role === 'progressbar')
  assert.equal(meter['aria-valuenow'], '50')
  assert.equal(meter.dataset.state, 'unknown')
  assert.equal(h.node('bAutonomy').value, 1)
  assert.equal(h.node('bReserve').value, 0.05)
  assert.doesNotMatch(text, /￥|人民币|免费|账户硬限额已生效/)
})

test('configured budget without a fresh runtime gate is never shown as an active spending guard', () => {
  const h = harness({ functions: BUDGET_RENDERERS })
  h.context.renderBudget(budgetData({ enforcement: { before_turn: true, before_action: true, runtime_gate_bound: false } }), false, 0)
  assert.equal(h.node('sMoney').textContent, '预算门控未确认')
  assert.equal(h.node('mState').textContent, '运行门控未确认')
  assert.doesNotMatch(treeText(h.node('budgetEnforcement')), /已接入/)
})

test('remaining balance below a new reservation is not labeled available or fully exhausted', () => {
  const data = budgetData()
  data.usage.buckets.autonomy = { spent_usd: 0.98, reserved_usd: 0, remaining_usd: 0.02,
    unknown_count: 0, exhausted: false, can_start_new_turn: false, blocked_reason: 'insufficient_reserve' }
  const h = harness({ functions: BUDGET_RENDERERS })
  h.context.renderBudget(data, false, 0)
  assert.equal(h.node('sMoney').textContent, '新轮次预留不足')
  assert.match(treeText(h.node('budgetBuckets')), /余额不足以预留下一轮/)
  assert.doesNotMatch(h.node('sMoney').textContent, /额度可用|额度已用完/)
  assert.match(treeText(h.node('budgetBuckets')), /必要的?控制判断/)
})

test('not-started budget permits current settings without displaying fabricated historic zero usage', () => {
  const data = budgetData({ availability: 'not_started', usage: { status: 'not_started', coverage_start: null, buckets: {} } })
  const h = harness({ functions: BUDGET_RENDERERS })
  h.context.renderBudget(data, false, 0)
  assert.equal(h.node('bAutonomy').disabled, false)
  assert.equal(h.node('budgetSave').disabled, false)
  assert.equal(h.node('bAutonomy').value, 1)
  assert.match(h.node('mAvailability').textContent, /尚未起算/)
  assert.match(treeText(h.node('budgetBuckets')), /尚无新版计费记录/)
  assert.doesNotMatch(treeText(h.node('budgetBuckets')), /已计 \$0|剩余 \$|已使用和预留 0/)
  for (const meter of descendants(h.node('budgetBuckets')).filter(node => node.role === 'progressbar')) {
    assert.equal(meter['aria-valuenow'], undefined)
    assert.equal(meter.dataset.state, 'unknown')
  }
})

test('unavailable budget and failed refresh retain the previous editable values without inventing zero cost', async () => {
  const h = harness({ functions: BUDGET_RENDERERS.concat('loadBudget'), extra: {
    api: async () => ({ ok: false, error: 'synthetic ledger failure' }) } })
  h.context.renderBudget(budgetData(), false, 0)
  const before = treeText(h.node('budgetBuckets'))
  h.node('bAutonomy').value = '0.7'
  await h.context.loadBudget(false)
  assert.equal(h.node('bAutonomy').value, '0.7')
  assert.equal(treeText(h.node('budgetBuckets')), before)
  assert.equal(h.node('budgetSave').disabled, true)
  assert.equal(h.node('mState').textContent, '预算状态未确认')
  assert.match(h.node('moneyOut').textContent, /读取失败/)
})

test('a late read cannot reset budget fields that were edited while it was waiting', async () => {
  const waiting = deferred()
  const h = harness({ functions: BUDGET_RENDERERS.concat('loadBudget'), extra: { api: () => waiting.promise } })
  const read = h.context.loadBudget(false)
  h.node('bAutonomy').value = '0.8'
  h.context.pageEdits.set('money', 1); h.context.dirtyPages.add('money')
  waiting.resolve({ ok: true, budget: budgetData() }); await read
  assert.equal(h.node('bAutonomy').value, '0.8')
  assert.equal(h.context.budgetSettingsRevision, null)
  assert.equal(h.node('budgetSave').disabled, true)
})

test('a budget save sends only new bucket settings and its exact revision', async () => {
  const requests = []
  const h = harness({ functions: BUDGET_RENDERERS.concat('saveBudget'), extra: { api: async (...args) => {
    requests.push(args); return { ok: true, budget: budgetData({ revision: 'c'.repeat(64) }) }
  } } })
  h.context.budgetSettingsRevision = 'a'.repeat(64)
  for (const [id, value] of Object.entries({ bAutonomy: '0.8', bPrivate: '0', bGroup: '2', bReserve: '0.07' })) h.node(id).value = value
  await h.context.saveBudget()
  assert.equal(requests.length, 1)
  assert.equal(requests[0][0], '/budget')
  assert.equal(requests[0][1].method, 'POST')
  assert.deepEqual(JSON.parse(requests[0][1].body), { revision: 'a'.repeat(64), settings: {
    limits: { autonomy: 0.8, private: 0, group: 2 }, reserve_usd: 0.07 } })
  assert.equal(h.context.budgetSettingsRevision, 'c'.repeat(64))
})

test('new edits during a budget save remain dirty and are not replaced by the saved response', async () => {
  const waiting = deferred()
  const h = harness({ functions: BUDGET_RENDERERS.concat('saveBudget'), extra: { api: () => waiting.promise } })
  h.context.budgetSettingsRevision = 'a'.repeat(64)
  for (const id of ['bAutonomy', 'bPrivate', 'bGroup', 'bReserve']) h.node(id).value = '0.1'
  const save = h.context.saveBudget()
  h.node('bAutonomy').value = '0.9'; h.context.pageEdits.set('money', 1); h.context.dirtyPages.add('money')
  waiting.resolve({ ok: true, budget: budgetData({ revision: 'c'.repeat(64) }) }); await save
  assert.equal(h.node('bAutonomy').value, '0.9')
  assert.equal(h.context.dirtyPages.has('money'), true)
  assert.match(h.node('moneyOut').textContent, /还有未保存/)
})

test('a budget read started before a completed save cannot roll the saved revision back', async () => {
  const oldRead = deferred(), saved = budgetData({ revision: 'c'.repeat(64) })
  saved.effective.limits.autonomy = 0.8
  const h = harness({ functions: BUDGET_RENDERERS.concat('loadBudget', 'saveBudget'), extra: {
    api: async (_path, options) => options?.method === 'POST' ? { ok: true, budget: saved } : oldRead.promise } })
  h.context.budgetSettingsRevision = 'a'.repeat(64)
  const read = h.context.loadBudget(false)
  for (const [id, value] of Object.entries({ bAutonomy: '0.8', bPrivate: '0', bGroup: '0', bReserve: '0.05' })) h.node(id).value = value
  await h.context.saveBudget()
  oldRead.resolve({ ok: true, budget: budgetData() }); await read
  assert.equal(h.context.budgetSettingsRevision, 'c'.repeat(64))
  assert.equal(h.node('bAutonomy').value, 0.8)
})

test('budget mutation transport failure is not retried and never writes the legacy topup route', async () => {
  const h = harness({ functions: API_FUNCTIONS.concat(BUDGET_RENDERERS, 'saveBudget'),
    extra: { currentPage: 'money' }, fetch: async () => { throw new TypeError('synthetic ambiguous transport') } })
  h.context.budgetSettingsRevision = 'a'.repeat(64)
  for (const id of ['bAutonomy', 'bPrivate', 'bGroup', 'bReserve']) h.node(id).value = '0.1'
  await h.context.saveBudget()
  assert.equal(h.calls.length, 1)
  assert.equal(h.calls[0].url, 'http://127.0.0.1:19389/api/budget')
  assert.match(h.node('moneyOut').textContent, /重新读取/)
})

test('invalid reserve values never reach a budget mutation', async () => {
  for (const reserve of ['0', '-1', 'NaN', 'Infinity', '']) {
    let writes = 0
    const h = harness({ functions: ['saveBudget'], extra: { api: () => { writes++; throw new Error('must not call') } } })
    h.context.budgetSettingsRevision = 'a'.repeat(64)
    for (const id of ['bAutonomy', 'bPrivate', 'bGroup']) h.node(id).value = '0'
    h.node('bReserve').value = reserve
    await h.context.saveBudget()
    assert.equal(writes, 0, reserve)
  }
})

test('unified settings expose only opportunity cadence and label group values as unverified startup configuration', () => {
  const h = harness({ functions: AUTONOMY_RENDERERS })
  h.context.renderAutonomySettings(autonomyData(), false, 0)
  assert.equal(h.node('activityInterval').value, 3)
  assert.equal(h.context.autonomySettingsRevision, 'b'.repeat(64))
  assert.match(treeText(h.node('talkObservation')), /启动时读取的配置.*尚未逐项核验/)
  assert.doesNotMatch(treeText(h.node('talkObservation')), /当前进程.*已生效|每轮必须/)
})

test('settings background refresh and delayed reads preserve unsaved cadence and revision', async () => {
  const waiting = deferred()
  const h = harness({ functions: AUTONOMY_RENDERERS.concat('loadAutonomySettings'), extra: { api: () => waiting.promise } })
  const read = h.context.loadAutonomySettings(false)
  h.node('activityInterval').value = '4'
  h.context.pageEdits.set('activity', 1); h.context.dirtyPages.add('activity')
  waiting.resolve({ ok: true, settings: autonomyData() }); await read
  assert.equal(h.node('activityInterval').value, '4')
  assert.equal(h.context.autonomySettingsRevision, null)
})

test('settings save changes only cadence and keeps persistent silence outside the request', async () => {
  const requests = []
  const h = harness({ functions: AUTONOMY_RENDERERS.concat('saveAutonomySettings'), extra: { api: async (...args) => {
    requests.push(args); return { ok: true, settings: autonomyData() }
  } } })
  h.context.autonomySettingsRevision = 'b'.repeat(64); h.node('activityInterval').value = '4'
  await h.context.saveAutonomySettings()
  assert.equal(requests.length, 1)
  assert.equal(requests[0][0], '/autonomy/settings')
  assert.deepEqual(JSON.parse(requests[0][1].body), { revision: 'b'.repeat(64), settings: { opportunity_minutes: 4 } })
  assert.match(h.node('actOut').textContent, /已有复查保持原计划/)
})

test('a settings read started before a completed save cannot replace the saved cadence', async () => {
  const oldRead = deferred(), saved = autonomyData({ revision: 'd'.repeat(64), effective: { opportunity_minutes: 4 } })
  const h = harness({ functions: AUTONOMY_RENDERERS.concat('loadAutonomySettings', 'saveAutonomySettings'), extra: {
    api: async (_path, options) => options?.method === 'POST' ? { ok: true, settings: saved } : oldRead.promise } })
  h.context.autonomySettingsRevision = 'b'.repeat(64)
  const read = h.context.loadAutonomySettings(false)
  h.node('activityInterval').value = '4'; await h.context.saveAutonomySettings()
  oldRead.resolve({ ok: true, settings: autonomyData() }); await read
  assert.equal(h.context.autonomySettingsRevision, 'd'.repeat(64))
  assert.equal(h.node('activityInterval').value, 4)
})

test('explicit discard-and-reload recovers a stale revision while background reads preserve unsaved edits', async () => {
  for (const target of ['budget', 'autonomy']) {
    const money = target === 'budget', page = money ? 'money' : 'activity'
    const field = money ? 'budget' : 'settings', input = money ? 'bAutonomy' : 'activityInterval'
    const revisionKey = money ? 'budgetSettingsRevision' : 'autonomySettingsRevision'
    const load = money ? 'loadBudget' : 'loadAutonomySettings', save = money ? 'saveBudget' : 'saveAutonomySettings'
    const oldRevision = (money ? 'a' : 'b').repeat(64), newRevision = (money ? 'c' : 'd').repeat(64)
    const fresh = money ? budgetData({ revision: newRevision }) : autonomyData({ revision: newRevision, effective: { opportunity_minutes: 4 } })
    const calls = []
    const h = harness({ functions: (money ? BUDGET_RENDERERS : AUTONOMY_RENDERERS).concat(load, save, 'reloadCurrentSettings', 'renderRecordSync'),
      extra: { currentPage: page, api: async (...args) => {
        calls.push(args)
        if (args[1]?.method === 'POST' && JSON.parse(args[1].body).revision === oldRevision) return { ok: false, error: 'synthetic revision conflict' }
        return { ok: true, [field]: fresh }
      } } })
    h.context[revisionKey] = oldRevision; h.context.dirtyPages.add(page)
    if (money) for (const [id, value] of Object.entries({ bPrivate: '0', bGroup: '0', bReserve: '0.05' })) h.node(id).value = value
    h.node(input).value = money ? '0.7' : '7'
    await h.context[save]()
    assert.match(h.node(money ? 'moneyOut' : 'actOut').textContent, /重新读取/)
    assert.equal(h.context.dirtyPages.has(page), true)
    await h.context[load](true)
    assert.equal(h.node(input).value, money ? '0.7' : '7')
    assert.equal(h.context[revisionKey], oldRevision)
    const view = h.source.split('<div class="page" id="page-' + page + '">')[1].split('<div class="page"')[0]
    const handler = view.match(/<button\b[^>]*onclick="([^"]+)"[^>]*>放弃修改并重读<\/button>/)?.[1]
    assert.ok(handler, 'The real button must name its discard intent: ' + page)
    await h.run(handler)
    assert.equal(h.context.dirtyPages.has(page), false)
    assert.equal(h.context[revisionKey], newRevision)
    assert.equal(h.node(input).value, money ? 1 : 4)
    for (const id of money ? ['bAutonomy', 'bPrivate', 'bGroup', 'bReserve'] : ['activityInterval']) h.node(id).value = money ? '0.1' : '5'
    h.context.dirtyPages.add(page); await h.context[save]()
    const writes = calls.filter(([, options]) => options?.method === 'POST')
    assert.equal(writes.length, 2)
    assert.equal(JSON.parse(writes[0][1].body).revision, oldRevision)
    assert.equal(JSON.parse(writes[1][1].body).revision, newRevision)
    assert.equal(h.context.dirtyPages.has(page), false)
  }
})

for (const target of ['budget', 'autonomy']) test(target + ' real GET/save/new GET use distinct generations and old cleanup preserves the new pending read', async () => {
  const old = deferred(), fresh = deferred(), route = target === 'budget' ? '/budget' : '/autonomy/settings'
  const page = target === 'budget' ? 'money' : 'activity'
  const load = target === 'budget' ? 'loadBudget' : 'loadAutonomySettings'
  const save = target === 'budget' ? 'saveBudget' : 'saveAutonomySettings'
  const field = target === 'budget' ? 'budget' : 'settings'
  const saved = target === 'budget' ? budgetData({ revision: 'c'.repeat(64) }) : autonomyData({ revision: 'd'.repeat(64), effective: { opportunity_minutes: 4 } })
  let reads = 0
  const h = harness({ functions: API_FUNCTIONS.concat(target === 'budget' ? BUDGET_RENDERERS : AUTONOMY_RENDERERS, load, save),
    extra: { currentPage: page }, fetch: async (_url, options) => {
      if (options.method === 'POST') return response({ ok: true, [field]: saved })
      reads++; assert.ok(reads <= 2, 'Only the original and fresh GET should reach transport')
      return reads === 1 ? old.promise : fresh.promise
    } })
  if (target === 'budget') {
    h.context.budgetSettingsRevision = 'a'.repeat(64)
    for (const id of ['bAutonomy', 'bPrivate', 'bGroup', 'bReserve']) h.node(id).value = '0.1'
  } else {
    h.context.autonomySettingsRevision = 'b'.repeat(64); h.node('activityInterval').value = '4'
  }
  h.context.noteRecord(route, 'live', Date.now())
  const firstRead = h.context[load](false)
  assert.equal(reads, 1)
  await h.context[save]()
  const secondRead = h.context[load](false)
  assert.equal(reads, 2, 'Post-save GET cannot reuse the invalidated pre-save promise')
  old.resolve(response({ ok: false, error: 'old synthetic failure' })); await firstRead
  assert.equal(h.node('recordSync').dataset.state, 'live')
  assert.equal(h.context.recordRequests.has('live:' + route), true, 'Old finally must not remove the new read')
  const sharedRead = h.context.api(route, null, true)
  assert.equal(reads, 2)
  fresh.resolve(response({ ok: true, [field]: saved })); await Promise.all([secondRead, sharedRead])
  assert.equal(h.context.recordRequests.size, 0)
  assert.equal(h.node('recordSync').dataset.state, 'live')
})

test('current budget and autonomy views expose no legacy frequency, activity toggle or topup mutations', () => {
  const source = readFileSync(PANEL, 'utf8')
  assert.doesNotMatch(source, /function (?:saveTunables|saveActivities|topup|addActivity|deleteActivity)\(/)
  assert.doesNotMatch(source, /api\(['"]\/(?:activities(?:\/|['"])|tunables['"]|budget\/topup)/)
  assert.match(source, /单位 USD/)
  assert.match(source, /不是服务商账户硬限额/)
})

test('Bili view separates targeted video reads from recommendation candidates and action-only targets', () => {
  const h = harness({ functions: BILI_RENDERERS })
  h.context.renderMainBili(biliData({
    recent_videos: [
      { bvid: 'BV1234567890', title: 'Actual subtitle target', at: 1793520000000, kinds: ['subtitle'], statuses: ['succeeded'] },
      { bvid: 'BVABCDEFGHIJ', title: 'Comment-only target', at: 1793520000000, kinds: ['action'], statuses: ['succeeded'] }],
    records: [
      { tool: 'bili_recommend', kind: 'recommendation', status: 'succeeded', item_count: 1, videos: [{ bvid: 'BV0123456789', title: 'Candidate only' }] },
      { tool: 'bili_subtitle', kind: 'subtitle', status: 'succeeded', at: 1793520000000, video: { bvid: 'BV1234567890' }, subtitle_chars: 120 }]
  }))
  assert.equal(h.node('bMainCount').textContent, 1)
  assert.match(treeText(h.node('bMainVideos')), /Actual subtitle target/)
  assert.doesNotMatch(treeText(h.node('bMainVideos')), /Candidate only|Comment-only/)
  assert.match(treeText(h.node('bMainVideos')), /字幕 · 成功/)
  assert.match(treeText(h.node('bMainRecords')), /Candidate only/)
  assert.equal(h.calls.length, 0)
})

test('Bili replies show attempted body, uncertain status and accepted-receipt visibility limits', () => {
  const h = harness({ functions: BILI_RENDERERS })
  h.context.renderMainBili(biliData({ replies: [
    { type: 'comment', status: 'accepted', text: '<img src=x onerror=alert(1)>', at: 1793520000000,
      video: { bvid: 'BV1234567890', title: 'Safe title' } },
    { type: 'comment_reply', status: 'unknown', text: 'Uncertain attempt' },
    { type: 'comment', status: 'failed', text: 'Rejected attempt', text_truncated: true },
    { type: 'private_message', status: 'accepted', text: 'SHOULD NOT RENDER', target_uid: '999' }]
  }))
  const body = treeText(h.node('bMainReplyRecords'))
  assert.equal(h.node('bMainReplies').textContent, 4)
  assert.match(body, /接口已接受/)
  assert.match(body, /公开可见性尚未核验/)
  assert.match(body, /未验证接收方可见/)
  assert.match(body, /结果未知/)
  assert.match(body, /不能认定已发出/)
  assert.match(body, /失败.*Rejected attempt/)
  assert.match(body, /前 2000 字/)
  assert.doesNotMatch(body, /SHOULD NOT RENDER|999|已公开|发送成功/)
  const text = descendants(h.node('bMainReplyRecords')).find(row => row.textContent === '<img src=x onerror=alert(1)>')
  assert.ok(text)
  assert.equal(text.innerHTML, '')
  assert.equal(h.calls.length, 0)
})

test('Bili source failure and older projection snapshots never masquerade as an empty successful history', () => {
  for (const data of [{ status: 'unavailable' }, { status: 'ok', records: [] }]) {
    const h = harness({ functions: BILI_RENDERERS })
    h.context.renderMainBili(data)
    assert.equal(h.node('bMainCount').textContent, '—')
    assert.equal(h.node('bMainReplies').textContent, '—')
    assert.match(treeText(h.node('bMainReplyRecords')), /暂不可用|快照未含新版记录/)
    assert.doesNotMatch(treeText(h.node('bMainReplyRecords')), /从未发|还没发过/)
  }
})

test('Bili links are constructed only from validated video ids; titles remain text', () => {
  const h = harness({ functions: BILI_RENDERERS })
  const valid = h.context.biliVideoLink({ bvid: 'BV1234567890', title: '<script>bad</script>', url: 'javascript:bad()' })
  assert.equal(valid.children[0].href, 'https://www.bilibili.com/video/BV1234567890')
  assert.equal(valid.children[0].textContent, '<script>bad</script>')
  assert.equal(valid.children[0].innerHTML, '')
  const invalid = h.context.biliVideoLink({ bvid: 'javascript:bad()', title: 'Unsafe ID' })
  assert.equal(invalid.children.length, 0)
  assert.equal(invalid.textContent, 'Unsafe ID')
})

test('Bili legacy archive is read-only text and does not imply current delivery or public visibility', () => {
  const h = harness({ functions: BILI_RENDERERS })
  h.context.renderBiliArchive({ videos: [{ bv: 'BV1234567890', title: '<b>old</b>', date: '2026-01-01' }],
    comments: [{ bv: 'BV1234567890', text: '<img src=x>', time: '2026-01-02' }] })
  const body = treeText(h.node('bLegacyRecords'))
  assert.match(body, /旧评论记录 · 结果未复核/)
  assert.match(body, /<img src=x>/)
  assert.equal(descendants(h.node('bLegacyRecords')).some(row => row.onclick || row.src), false)
  assert.equal(h.calls.length, 0)
})

test('Bili page removes obsolete duty editors, launch and delete controls while preserving login and archives', () => {
  const source = readFileSync(PANEL, 'utf8')
  const view = source.split('<div class="page" id="page-bili">')[1].split('<div class="page" id="page-schedule">')[0]
  assert.match(view, /id="bMainVideos"/)
  assert.match(view, /id="bMainReplyRecords"/)
  assert.match(view, /旧记录存档/)
  assert.match(view, /startBiliLogin/)
  assert.doesNotMatch(view, /id="(?:biliDuty|biliSeen|biliLog)"|doOneRun|deleteOnBili|deleteBiliRecord|bili\/onerun/)
  assert.doesNotMatch(source, /function (?:runBiliNow|doOneRun|deleteOnBili|deleteBiliRecord|verifyComment|loadBiliDuty|saveBiliDuty)\(/)
})

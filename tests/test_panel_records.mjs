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

const LOADERS = ['refreshOverview', 'loadBili', 'loadLive', 'loadActivities', 'loadBudget', 'loadTools',
  'loadMemes', 'loadMemoryOverview', 'loadFiles', 'loadWorks', 'loadSchedule', 'loadTunables',
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
  const h = harness({ extra: { api: async () => ({ ok: true, budget: { limit: 3, spent: 0, date: 'fixture' } }) },
    functions: ['loadBudget'] })
  h.node('bLimit').value = 'unsaved fixture limit'
  await h.context.loadBudget(true)
  assert.equal(h.node('bLimit').value, 'unsaved fixture limit')
  assert.match(h.node('mSpent').innerHTML, /^0<small>/)
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

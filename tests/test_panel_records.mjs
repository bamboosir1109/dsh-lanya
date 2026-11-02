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
  const lineEnd = source.indexOf('\n', match.index)
  if(source.slice(match.index, lineEnd).trimEnd().endsWith('}')) return source.slice(match.index, lineEnd)
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

function element(tag = 'div') {
  const classes = new Set(), listeners = new Map()
  const node = { style: {}, dataset: {}, value: '', textContent: '', tagName: String(tag).toUpperCase(),
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
    appendChild(child) { this.children.push(child); child.parentElement = child.parentNode = this; return child },
    insertBefore(child, before) { const i = this.children.indexOf(before); if(i < 0) return this.appendChild(child); this.children.splice(i, 0, child); child.parentElement = child.parentNode = this; return child },
    remove() { if(this.parentNode) this.parentNode.children = this.parentNode.children.filter(node => node !== this) },
    querySelector() { return null }, querySelectorAll(selector) {
      const all = this.children.flatMap(child => [child, ...child.querySelectorAll('*')])
      if(selector === '*') return all
      if(selector === 'details[data-record-key]') return all.filter(child => child.tagName === 'DETAILS' && child.dataset.recordKey !== undefined)
      return []
    },
    setAttribute(name, value) { this[name] = value },
    removeAttribute(name) { delete this[name] },
    listeners }
  let html = ''
  Object.defineProperty(node, 'innerHTML', { get() { return html }, set(value) { html = value; this.children = []; this.textContent = '' } })
  Object.defineProperty(node, 'className', { get() { return [...classes].join(' ') }, set(value) { classes.clear(); String(value).split(/\s+/).filter(Boolean).forEach(name => classes.add(name)) } })
  Object.defineProperty(node, 'firstChild', { get() { return this.children[0] || null } })
  return node
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
    searchOn: false, selected: {}, workSel: {}, currentDir: 'diary', currentWorkDir: 'made', FILE_LIMIT: 1000,
    memeSel: null, memeMutationPending: false, memeCur: 0, memeShown: 0, MEME_PAGE: 120, memeData: [],
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

test('voice status separates demand-based synthesis, process evidence and delivery', () => {
  const h = harness({ functions: ['renderVoiceStatus'] })
  const value = { schema:'dsh-voice-status/v1', mode:'unified', execution:'main_agent_on_demand',
    daemon_required:false, executor:{installed:true}, read_only:true,
    engine:{type:'local_tts_process_observation', process_count:1, observation_state:'fresh'},
    synthesis:{state:'not_probed'}, delivery:{state:'unverified'} }
  h.context.renderVoiceStatus(value)
  assert.match(h.node('voiceMode').textContent, /同一主 Agent 按需合成/)
  assert.equal(h.node('voiceExecutor').textContent, '已安装')
  assert.equal(h.node('voiceEngine').textContent, '已发现进程')
  assert.equal(h.node('voiceEvidence').textContent, '按次核验')
  h.context.renderVoiceStatus({ ...value, engine:{...value.engine, observation_state:'stale'} })
  assert.equal(h.node('voiceEngine').textContent, '待确认')
  assert.match(h.node('voiceEngineNote').textContent, /过期/)
  h.context.renderVoiceStatus(null)
  assert.equal(h.node('voiceExecutor').textContent, '待确认')
  assert.equal(h.node('voiceEngine').textContent, '待确认')
})

test('voice status never renders untrusted service claims or HTML as readiness', () => {
  const h = harness({ functions: ['renderVoiceStatus'] })
  h.context.renderVoiceStatus({schema:'dsh-voice-status/v1', mode:'unified', read_only:true,
    executor:{installed:'<img src=x onerror=bad()>'}, engine:{type:'local_tts_process_observation',
      process_count:'1', observation_state:'fresh'}, synthesis:{state:'ready'}, delivery:{state:'sent'}})
  for (const id of ['voiceMode','voiceExecutor','voiceEngine','voiceEngineNote','voiceEvidence']) {
    assert.doesNotMatch(h.node(id).textContent, /img|已送达|已发现进程/)
    assert.equal(h.node(id).innerHTML, '')
  }
})

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
  assert.match(treeText(h.node('budgetBuckets')), /已计 ¥0\.0000 · 预留 ¥0\.0500/)
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


const RECORD_HELPERS = ['recordOpenDetails', 'recordDetails', 'recordText', 'recordState', 'recordContent', 'recordReply', 'biliRecordTime', 'biliVideoLink']
const BILI_RENDERERS = [...RECORD_HELPERS, 'renderMainBili']
const ACTIVITY_RENDERERS = [...RECORD_HELPERS, 'renderActivityHistory', 'renderAutonomyPlans']
const JOURNAL_READERS = ['biliRecordTime', 'activityJournalStatus', 'loadActivityJournal']
const treeText = node => [node.textContent || '', ...node.children.map(treeText)].join(' ')

test('autonomy plans are plain agent reports and unavailable refresh cannot keep stale plans', () => {
  const h = harness({ functions: ['renderAutonomyPlans'] })
  h.context.renderAutonomyPlans({schema:'dsh-autonomy-plans-panel/v1',read_only:true,private:true,
    summary_is_agent_report:true,status:'ok',plans:[{role:'primary',status:'active',goal:'<img src=x onerror=bad()>',
      reason:'想看看',next_step:'读取真实资料',agent_summary:'尚未完成',updated_at:1800000000000}]})
  assert.match(treeText(h.node('autonomyPlansList')), /<img src=x onerror=bad\(\)>/)
  assert.doesNotMatch(h.node('autonomyPlansList').innerHTML, /<img/)
  assert.match(h.node('autonomyPlansNote').textContent, /不代表事情已经完成/)
  h.context.renderAutonomyPlans(null)
  assert.equal(h.node('autonomyPlansList').children.length, 0)
  assert.match(h.node('autonomyPlansNote').textContent, /读取失败/)
})

test('activity displays a local no-decision outcome without inventing an LLM verdict or hiding uncertain scheduling', async () => {
  for (const count of [0, 1, '1', -1]) {
    const h = harness({ functions: ['autonomyLabel', 'appendPolicyRow', ...ACTIVITY_RENDERERS, 'loadLive'], extra: {
      componentActionPending: false, loadActivityBudget: async () => {}, loadActivityJournal: async () => {},
      api: async () => ({ ok: true, live: { autonomy: { mode: 'unified', status: 'uncertain', can_request_check: true,
        observer: { no_decision_count: count, next_opportunity_at: 1793535000000 } }, recentActivity: { status: 'ok', records: [] } } })
    } })
    await h.context.loadLive()
    const text = treeText(h.node('activitySchedule'))
    assert.match(text, /上次结果未知/)
    assert.match(text, /下次普通机会/)
    if (count === 1) assert.match(text, /1 个观察范围的上次活动未提交决策，已收尾并等待新机会/)
    else assert.doesNotMatch(text, /未提交决策/)
    assert.doesNotMatch(text, /模型决定安静|已经发送/)
  }
})
const descendants = node => [node, ...node.children.flatMap(descendants)]

test('activity keeps actual scheduler offline distinct from a living background watcher and clears stale watch labels on error', async () => {
  let fail = false
  const h = harness({ functions: ['autonomyLabel', 'appendPolicyRow', ...ACTIVITY_RENDERERS, 'loadLive'], extra: {
    componentActionPending: false, loadActivityBudget: async () => {}, loadActivityJournal: async () => {},
    api: async () => {
      if (fail) throw new Error('fixture unavailable')
      return { ok: true, live: { autonomy: { mode: 'unified', status: 'offline', can_request_check: false,
        watch: { status: 'healthy', last_seen_at: 1793535000000 } }, recentActivity: { status: 'ok', records: [] } } }
    }
  } })
  await h.context.loadLive()
  assert.match(h.node('liveLast').textContent, /调度离线/)
  assert.equal(h.node('liveWatch').textContent, '看护在线')
  assert.equal(h.node('liveCheck').disabled, true)
  fail = true
  await h.context.loadLive()
  assert.equal(h.node('liveWatch').textContent, '看护状态未确认')
})
const activityEpisode = (id, overrides = {}) => ({ id, day: '2026-11-01', started_at: Date.UTC(2026, 10, 1, 10),
  updated_at: Date.UTC(2026, 10, 1, 10, 1), title: '读取一段视频字幕', status: 'observed', summary: '字幕读取 1 次',
  trigger_origin: 'observation', conversation_role: 'primary',
  videos: [{ bvid: 'BV1234567890', title: 'Fixture video', author: 'Fixture creator' }],
  evidence: [{ id: id + '-read', tool: 'bili_subtitle', kind: 'subtitle', at: Date.UTC(2026, 10, 1, 10),
    status: 'succeeded', label: '读取字幕', summary: '取得字幕；不代表完整播放', content_excerpt: 'Fixture subtitle content', content_truncated: false }], ...overrides })

test('actual activity renderer groups facts and content while quiet decisions cannot crowd out real activity', () => {
  const h = harness({ functions: ACTIVITY_RENDERERS })
  const history = { status: 'partial', episodes: [activityEpisode('fixture-video')],
    decisions: Array.from({ length: 30 }, (_, i) => ({ id: 'quiet-' + i, at: Date.UTC(2026, 10, 1, 11), status: 'accepted', verdict: 'idle' })),
    counts: { episodes: 1, decisions: 80 }, coverage: { decisions_truncated: true } }
  const before = structuredClone(history)
  h.context.renderActivityHistory(history)
  const text = treeText(h.node('liveLog'))
  assert.match(text, /2026-11-01.*读取一段视频字幕.*Fixture video.*Fixture creator.*Fixture subtitle content/)
  assert.doesNotMatch(text, /qq_decide|quiet-|本轮保持安静/)
  assert.match(h.node('activityDecisionSummary').textContent, /最近 30 次 \/ 共 80 次.*30 次选择安静/)
  assert.match(treeText(h.node('activityDecisionList')), /本轮保持安静.*判断已接受/)
  assert.match(h.node('activityHistoryNote').textContent, /只覆盖部分记录/)
  assert.equal(h.node('activityDecisions').open, undefined)
  assert.deepEqual(history, before)
  assert.equal(h.calls.length, 0)
})

test('actual activity evidence stays plain text with unknown outcomes and no private-message bodies', () => {
  const h = harness({ functions: ACTIVITY_RENDERERS })
  h.context.renderActivityHistory({ status: 'ok', episodes: [activityEpisode('fixture-unknown', {
    status: 'partial', evidence_truncated: true, evidence_total: 9, evidence: [
      { tool: 'bili_subtitle', kind: 'subtitle', label: '读取字幕', status: 'succeeded', content_excerpt: '<img onerror=fixture()>\nSubtitle only', content_truncated: true },
      { tool: 'bili_send_message', status: 'unknown', reply: { type: 'private_message', status: 'unknown', text: 'PRIVATE_FIXTURE_NOT_FOR_PANEL', recipient: 'PRIVATE_FIXTURE_RECIPIENT' } },
      { tool: 'qq_send_image', status: 'unknown', summary: '客户端送达未核验' }
    ] })], decisions: [] })
  const text = treeText(h.node('liveLog'))
  assert.match(text, /部分结果待确认.*3 \/ 9 项证据/)
  assert.match(text, /字幕摘录，未证明完整播放 · 部分内容/)
  assert.match(text, /结果未确认.*不能认定已发出；不自动重发/)
  assert.doesNotMatch(text, /PRIVATE_FIXTURE_NOT_FOR_PANEL|PRIVATE_FIXTURE_RECIPIENT|已完整观看|已送达/)
  const content = descendants(h.node('liveLog')).find(row => row.textContent.startsWith('<img onerror='))
  assert.ok(content); assert.equal(content.innerHTML, '')
})

test('actual activity details and incremental pagination survive subsequent refreshes', () => {
  const h = harness({ functions: ACTIVITY_RENDERERS })
  const history = { status: 'ok', episodes: Array.from({ length: 25 }, (_, i) => activityEpisode('fixture-' + i)), decisions: [] }
  h.context.renderActivityHistory(history)
  assert.equal(h.node('liveLog').children.filter(row => row.tagName === 'ARTICLE').length, 20)
  h.node('liveLog').querySelectorAll('details[data-record-key]')[0].open = true
  h.node('liveLog').children.find(row => row.tagName === 'BUTTON').onclick()
  assert.equal(h.node('liveLog').children.filter(row => row.tagName === 'ARTICLE').length, 25)
  assert.equal(h.node('liveLog').querySelectorAll('details[data-record-key]')[0].open, true)
  h.context.renderActivityHistory(structuredClone(history))
  assert.equal(h.node('liveLog').children.filter(row => row.tagName === 'ARTICLE').length, 25)
  assert.equal(h.node('liveLog').querySelectorAll('details[data-record-key]')[0].open, true)
})

test('activity origin is independent of private or group scope and rejected quiet choices never count as applied', () => {
  const h = harness({ functions: ACTIVITY_RENDERERS })
  h.context.renderActivityHistory({ status: 'ok', episodes: [
    activityEpisode('fixture-inbound', { trigger_origin: 'live', conversation_role: 'primary' }),
    activityEpisode('fixture-autonomous', { trigger_origin: 'observation', conversation_role: 'social' }),
    activityEpisode('fixture-no-origin', { trigger_origin: 'unknown', conversation_role: 'primary' })
  ], decisions: [{ id: 'rejected-quiet', status: 'rejected', verdict: 'idle' }] })
  const articles = h.node('liveLog').children.filter(row => row.tagName === 'ARTICLE').map(treeText)
  assert.match(articles[0], /会话中活动.*管理员私聊范围/)
  assert.doesNotMatch(articles[0], /自主活动/)
  assert.match(articles[1], /自主活动.*配置群范围/)
  assert.match(articles[2], /触发来源未确认/)
  assert.doesNotMatch(h.node('activityDecisionSummary').textContent, /选择安静/)
  assert.match(treeText(h.node('activityDecisionList')), /尝试：本轮保持安静.*判断未接受/)
  assert.match(h.node('activityHistoryNote').textContent, /管理员私聊与配置群的当前绑定记录/)
})

test('source gaps and ungrouped compatibility records never invent an empty successful activity timeline', () => {
  const h = harness({ functions: ACTIVITY_RENDERERS })
  h.context.renderActivityHistory({ status: 'ok', records: [{ tool: 'qq_decide', status: 'returned' }, { tool: 'browser_fixture', status: 'unknown' }] })
  assert.match(h.node('activityHistoryNote').textContent, /尚未提供归并记录/)
  assert.match(treeText(h.node('liveLog')), /browser_fixture.*结果未确认/)
  assert.doesNotMatch(treeText(h.node('liveLog')), /qq_decide/)
  h.context.renderActivityHistory({ status: 'unavailable', episodes: [] })
  assert.match(h.node('activityHistoryNote').textContent, /不能据此判断没有活动/)
  assert.doesNotMatch(treeText(h.node('liveLog')), /当前范围暂无实际动作/)
})

const journalList = (days, status = days.length ? 'ok' : 'empty') => ({ schema: 'activity-journal/v1', status, read_only: true, private: true,
  days: days.map(date => ({ date, status: 'open', updated_at: Date.UTC(2026, 10, 1, 12), entries: 1, coverage: { complete: false } })), selected: null })
const journalPage = (date, markdown = 'Fixture factual diary') => ({ ...journalList([date]), selected: { date, status: 'partial',
  updated_at: Date.UTC(2026, 10, 1, 12), markdown, coverage: { complete: false }, entries: 1 } })

test('actual journal loader requests generated dates only and renders markdown as inert local text', async () => {
  const requests = []
  const h = harness({ functions: JOURNAL_READERS, extra: { api: async (route, options, live) => {
    requests.push({ route, options, live })
    if(route.includes('day=')) return journalPage('2026-11-01', '# Fixture diary\n<script>fixture()</script>\n[example](javascript:fixture())')
    return journalList(['2026-10-31', '2026-11-01'])
  } } })
  await h.context.loadActivityJournal()
  assert.equal(h.node('activityJournalDate').value, '2026-11-01')
  assert.match(h.node('activityJournalNote').textContent, /今天已有事实记录.*仅覆盖当前可信来源/)
  assert.match(h.node('activityJournalState').textContent, /部分来源或结果待确认.*覆盖不完整/)
  assert.match(h.node('activityJournalBody').textContent, /<script>fixture\(\)<\/script>/)
  assert.equal(h.node('activityJournalBody').innerHTML, '')
  assert.equal(h.node('activityJournalBody').children.length, 0)
  assert.deepEqual(requests.map(row => row.route), ['/activity/journal?limit=90', '/activity/journal?day=2026-11-01'])
  assert.ok(requests.every(row => row.options === null && row.live === true))
  assert.equal(h.context.dirtyPages.size, 0)
})

test('journal history selection and expanded body survive refresh while today remains explicitly not generated', async () => {
  const h = harness({ functions: JOURNAL_READERS, extra: { api: async route => route.includes('day=') ? journalPage('2026-10-31') : journalList(['2026-10-31']) } })
  await h.context.loadActivityJournal('2026-10-31')
  h.node('activityJournalDetails').open = true
  await h.context.loadActivityJournal()
  assert.equal(h.node('activityJournalDate').value, '2026-10-31')
  assert.equal(h.node('activityJournalDetails').open, true)
  assert.match(h.node('activityJournalNote').textContent, /今天尚未生成事实日记/)
  assert.match(h.node('activityJournalTitle').textContent, /2026-10-31/)
})

test('journal empty and unavailable states differ and a failed refresh preserves prior body with a warning', async () => {
  let status = 'ok'
  const h = harness({ functions: JOURNAL_READERS, extra: { api: async route => {
    if(status === 'error') return { ...journalList([]), status: 'error' }
    if(status === 'empty') return journalList([])
    return route.includes('day=') ? journalPage('2026-11-01') : journalList(['2026-11-01'])
  } } })
  await h.context.loadActivityJournal()
  status = 'error'; await h.context.loadActivityJournal()
  assert.equal(h.node('activityJournalBody').textContent, 'Fixture factual diary')
  assert.match(h.node('activityJournalState').textContent, /读取失败.*不能据此判断今天没有记录/)
  status = 'empty'; await h.context.loadActivityJournal()
  assert.equal(h.node('activityJournalDate').disabled, true)
  assert.match(h.node('activityJournalState').textContent, /尚无已生成/)
  assert.doesNotMatch(h.node('activityJournalBody').textContent, /Fixture factual diary/)
})

test('a degraded journal scan retains the last successful text and timestamp without presenting the scan time as a fresh diary', async () => {
  let degraded = false
  const savedAt = Date.UTC(2026, 10, 1, 12), scanAt = Date.UTC(2026, 10, 5, 14)
  const h = harness({ now: scanAt, functions: JOURNAL_READERS, extra: { api: async route => {
    const result = route.includes('day=') ? journalPage('2026-11-01', 'Previous successful fixture facts') : journalList(['2026-11-01'])
    if(result.selected){ result.selected.status = 'closed_snapshot'; result.selected.updated_at = savedAt; result.selected.coverage.complete = true }
    if(degraded){
      result.generated_at = scanAt
      result.freshness = { status: 'degraded', reasons: ['source_coverage_regressed'], blocked_dates: ['2026-11-01'] }
      if(result.selected) result.selected.coverage.latest_scan_degraded = true
    }
    return result
  } } })
  await h.context.loadActivityJournal('2026-11-01')
  degraded = true; await h.context.loadActivityJournal()
  assert.equal(h.node('activityJournalBody').textContent, 'Previous successful fixture facts')
  assert.match(h.node('activityJournalNote').textContent, /保留上次成功日记，当前来源不足/)
  const state = h.node('activityJournalState').textContent
  assert.match(state, /原记录状态：日终事实快照.*最后成功记录于/)
  assert.ok(state.includes(h.context.biliRecordTime(savedAt)))
  assert.ok(!state.includes(h.context.biliRecordTime(scanAt)))
  assert.doesNotMatch(state, /更新于/)
})

test('late journal response for a previously selected date cannot replace the current date or body', async () => {
  const old = deferred()
  const h = harness({ functions: JOURNAL_READERS, extra: { api: async route => {
    if(route.includes('day=2026-10-31')) return old.promise
    if(route.includes('day=')) return journalPage('2026-11-01', 'Current fixture day')
    return journalList(['2026-11-01', '2026-10-31'])
  } } })
  const previous = h.context.loadActivityJournal('2026-10-31')
  await Promise.resolve(); await Promise.resolve()
  await h.context.loadActivityJournal('2026-11-01')
  old.resolve(journalPage('2026-10-31', 'Stale fixture day')); await previous
  assert.equal(h.node('activityJournalDate').value, '2026-11-01')
  assert.equal(h.node('activityJournalBody').textContent, 'Current fixture day')
})

test('a previous journal date cannot overwrite the selected date refresh indicator', () => {
  const h = harness({ functions: API_FUNCTIONS, extra: { currentPage: 'activity' } })
  h.node('activityJournal').dataset.date = '2026-11-01'
  h.context.noteRecord('/activity/journal?day=2026-10-31', 'error')
  assert.equal(h.context.recordStates.size, 0)
  h.context.noteRecord('/activity/journal?day=2026-11-01', 'live', Date.UTC(2026, 10, 1, 12))
  assert.equal(h.node('recordSync').dataset.state, 'live')
  assert.equal(h.context.recordStates.size, 1)
})

const MONEY_FORMATTERS = ['requireCnyBudget', 'budgetAmount', 'budgetDecimal', 'renderBudgetInput', 'budgetInputCny']
const DAILY_RENDERERS = [...MONEY_FORMATTERS, 'spendingDayState', 'spendingAmount', 'nativeSpendingReady', 'renderTodaySpending', 'selectSpendingDate', 'renderDailySpending']
const BUDGET_RENDERERS = ['appendPolicyRow', 'renderBudgetSummary', ...DAILY_RENDERERS, 'renderBudget']
const AUTONOMY_RENDERERS = ['appendPolicyRow', 'renderAutonomySettings']
const budgetData = overrides => ({ schema: 'unified-budget/v2', currency: 'CNY', basis: 'owner_confirmed_numeric_cny_estimate', numeric_scale: 1, availability: 'available', revision: 'a'.repeat(64),
  effective: { currency: 'CNY', basis: 'owner_confirmed_numeric_cny_estimate', numeric_scale: 1, limits: { autonomy: 1, private: 0, group: 0 }, reserve_cny: 0.05 },
  usage: { date: '2026-11-01', coverage_start: 1793520000000, buckets: {
    autonomy: { spent_cny: 0, reserved_cny: 0.05, remaining_cny: 0.95, unknown_count: 0, exhausted: false },
    private: { spent_cny: 0.12, reserved_cny: 0, remaining_cny: null, unknown_count: 0, exhausted: false },
    group: { spent_cny: 0.25, reserved_cny: 0, remaining_cny: null, unknown_count: 0, exhausted: false } } },
  enforcement: { before_turn: true, before_action: true, runtime_gate_bound: true, provider_hard_cap: false }, ...overrides })
const autonomyData = overrides => ({ schema: 'dsh-panel-autonomy-settings/v1', availability: 'available',
  mode: 'unified', revision: 'b'.repeat(64), writable_fields: ['opportunity_minutes'],
  effective: { opportunity_minutes: 3 }, existing_reviews_unchanged: true,
  group_observation: { availability: 'available', read_only: true, running_values_verified: false,
    activation: 'observer_startup_configuration', settings: { poll_seconds: 30, quiet_minutes: 20 } }, ...overrides })
const biliData = overrides => ({ status: 'ok', coverage: { owner_verified_scopes: 2 },
  records: [], recent_videos: [], replies: [], ...overrides })
const spendingDay = (date, overrides = {}) => ({ date, autonomy: 0.12, private: 0.34, group: 0.56, total: 1.02,
  status: 'partial', turn_count: 3, settled_count: 3, pending_count: 0, unknown_count: 0, ...overrides })
const spendingData = days => ({ schema: 'daily-spending/v2', currency: 'CNY', basis: 'owner_confirmed_numeric_cny_estimate', numeric_scale: 1, timezone: 'Asia/Shanghai',
  today: days.at(-1)?.date, days, coverage: { complete_days: false, scope: 'current_mapped_main_agent_native_turns',
    owner_verified_scopes: 2, configured_scopes: 2 } })

test('daily spending shows the confirmed CNY numbers unchanged in cards chart and accessible table', () => {
  const h = harness({ functions: DAILY_RENDERERS })
  h.context.renderDailySpending(spendingData([spendingDay('2026-11-01')]))
  const bar = h.node('spendChart').children[0]
  assert.match(bar['aria-label'], /2026-11-01.*合计 ¥1\.0200.*自主 ¥0\.1200.*私聊 ¥0\.3400.*群聊 ¥0\.5600/)
  const pieces = descendants(bar).filter(node => ['autonomy', 'private', 'group'].includes(node.className))
  assert.equal(pieces.length, 3)
  assert.ok(parseFloat(pieces[0].style.height) > 0)
  assert.ok(parseFloat(pieces[2].style.height) > parseFloat(pieces[0].style.height))
  assert.match(treeText(h.node('spendTable')), /2026-11-01.*¥0\.1200.*¥0\.3400.*¥0\.5600.*¥1\.0200/)
  assert.equal(bar['aria-pressed'], 'true')
  assert.equal(h.node('spendSelectedDate').value, '2026-11-01')
})

test('whole-day spending and the budget gate use independent sources without adding either amount twice', () => {
  const h = harness({ functions: BUDGET_RENDERERS })
  const budget = budgetData({ daily_spending: spendingData([spendingDay('2026-11-01', { autonomy: 1.2, total: 2.1 })]) })
  budget.gate_usage = structuredClone(budget.usage)
  budget.gate_usage.buckets.autonomy.spent_cny = 0.07
  budget.usage.buckets.autonomy.spent_cny = 9
  h.context.renderBudget(budget, false, 0)
  assert.match(treeText(h.node('dailyTodayBuckets')), /自主活动.*今日已计 ¥1\.2000/)
  assert.doesNotMatch(treeText(h.node('dailyTodayBuckets')), /¥0\.0700|¥9\.0000/)
  assert.match(treeText(h.node('budgetBuckets')), /执行已计 ¥0\.0700/)
  assert.equal(h.node('mDailyTotal').textContent, '¥2.1000')
  assert.equal(h.node('mDailyTotalNote').textContent, '本机估算：2.1 元，未核对服务商实扣')
  assert.equal(h.node('mDate').textContent, '2026-11-01')
  assert.match(h.node('mCoverage').textContent, /已核验 2\/2 个绑定范围/)
  assert.match(h.node('mGateAccounting').textContent, /不与上方每日花销相加/)
  assert.match(h.node('sMoneySub').textContent, /¥0\.0700/)
})

test('a partial gate ledger is never a substitute for missing native whole-day costs', () => {
  for (const daily of [undefined, { ...spendingData([spendingDay('2026-11-01')]), coverage: { scope: 'trusted_main_agent_turns' } },
    { ...spendingData([spendingDay('2026-11-01')]), availability: 'unavailable' }]) {
    const h = harness({ functions: BUDGET_RENDERERS })
    h.context.renderBudget(budgetData({ daily_spending: daily }), false, 0)
    assert.equal(h.node('mDailyTotal').textContent, '待核算')
    assert.equal(h.node('mDailyAvailability').textContent, '来源未确认')
    assert.doesNotMatch(treeText(h.node('dailyTodayBuckets')), /\$0\.0000|\$0\.1200/)
    assert.match(treeText(h.node('spendChart')), /暂不可用/)
    assert.equal(h.node('spendTable').children.length, 0)
    assert.match(treeText(h.node('budgetBuckets')), /执行已计 ¥0\.1200/)
  }
})

test('native whole-day unknown categories stay unpriced in the daily summary', () => {
  const h = harness({ functions: DAILY_RENDERERS })
  const today = spendingDay('2026-11-01', { autonomy: null, total: null, unknown_count: 1,
    buckets: { autonomy: { unknown_count: 1 } } })
  h.context.renderDailySpending(spendingData([today]))
  assert.match(treeText(h.node('dailyTodayBuckets')), /自主活动.*今日已计 待核算/)
  assert.match(treeText(h.node('dailyTodayBuckets')), /1 轮待核算/)
  assert.equal(h.node('mDailyTotal').textContent, '待核算')
})

test('missing daily coverage renders unknown instead of a zero-height zero-cost day, and unsettled cost stays separate', () => {
  const h = harness({ functions: DAILY_RENDERERS })
  h.context.renderDailySpending(spendingData([
    spendingDay('2026-10-31', { autonomy: null, private: null, group: null, total: null, status: 'unknown' }),
    spendingDay('2026-11-01', { autonomy: 0, total: 0.9, pending_count: 1, unknown_count: 2 })]))
  const [missing, pending] = h.node('spendChart').children
  assert.doesNotMatch(treeText(missing), /¥0\.0000/)
  assert.equal(descendants(missing).find(node => node.className === 'spend-stack').dataset.state, 'unknown')
  assert.match(treeText(pending), /¥0\.9000 \+ \?/)
  assert.match(treeText(h.node('spendTable')), /未覆盖.*1 轮计费中.*2 轮待核算/)
  assert.match(h.node('spendHistoryNote').textContent, /未覆盖记录不能当作零费用/)
  assert.match(treeText(h.node('spendDetail')), /1 轮计费中.*2 轮待核算/)
})

test('daily range and actual date button select details without editing settings or calling an API', () => {
  const h = harness({ functions: DAILY_RENDERERS })
  const days = Array.from({ length: 30 }, (_, i) => spendingDay('2026-10-' + String(i + 1).padStart(2, '0'), { total: i + 1 }))
  h.context.renderDailySpending(spendingData(days))
  assert.equal(h.node('spendChart').children.length, 14)
  h.node('spendRange').value = '30'; h.context.renderDailySpending()
  assert.equal(h.node('spendChart').children.length, 30)
  h.node('spendChart').children[3].onclick()
  assert.equal(h.node('spendSelectedDate').value, '2026-10-04')
  assert.match(treeText(h.node('spendDetail')), /2026-10-04.*¥4\.0000/)
  assert.equal(h.node('spendChart').children[3]['aria-pressed'], 'true')
  assert.equal(h.node('spendTable').children[3]['aria-current'], 'date')
  assert.equal(h.context.budgetSettingsRevision, null)
  assert.equal(h.calls.length, 0)
})

test('actual input and change listeners exclude chart navigation while retaining real settings edits on every page', () => {
  const h = harness({ functions: ['renderRecordSync'] })
  for (const event of ['input', 'change']) {
    const start = h.source.indexOf("document.addEventListener('" + event + "',")
    const end = h.source.indexOf('\n});', start)
    assert.ok(start >= 0 && end > start)
    h.run(h.source.slice(start, end + 4))
    const listener = h.events.get(event)
    assert.equal(typeof listener, 'function')
    for (const [id, tag, page, shouldEdit] of [
      ['spendRange', 'select', 'money', false], ['spendSelectedDate', 'select', 'money', false], ['activityJournalDate', 'select', 'activity', false],
      ['bAutonomy', 'input', 'money', true], ['bReserve', 'input', 'money', true],
      ['syntheticBudgetOption', 'select', 'money', true], ['activityInterval', 'input', 'activity', true],
      ['memOwner', 'select', 'memory', true], ['persona', 'textarea', 'persona', true]
    ]) {
      h.context.currentPage = page; h.context.dirtyPages.clear(); h.context.pageEdits.clear()
      const target = { matches(selectors) { return selectors.split(',').some(selector => selector === tag || selector === '#' + id) } }
      listener({ target })
      assert.equal(h.context.dirtyPages.has(page), shouldEdit, event + ' ' + id)
      assert.equal(h.context.pageEdits.get(page), shouldEdit ? 1 : undefined, event + ' ' + id)
    }
  }
})

test('daily history follows a new Beijing day by default but preserves an explicitly selected day on refresh', () => {
  for (const explicit of [false, true]) {
    const h = harness({ functions: DAILY_RENDERERS })
    const yesterday = spendingDay('2026-11-01'), today = spendingDay('2026-11-02', { total: 0.7 })
    h.context.renderDailySpending(spendingData([yesterday]))
    if (explicit) h.context.selectSpendingDate(yesterday.date)
    h.context.renderDailySpending(spendingData([yesterday, today]))
    assert.equal(h.node('spendSelectedDate').value, explicit ? yesterday.date : today.date)
    assert.equal(h.node('spendChart').children.length, 2)
  }
})

test('daily history failure and empty history clear previous bars without claiming zero spending', () => {
  for (const bad of [null, spendingData([]), { ...spendingData([]), availability: 'unavailable' }]) {
    const h = harness({ functions: DAILY_RENDERERS })
    h.context.renderDailySpending(spendingData([spendingDay('2026-11-01')]))
    h.context.renderDailySpending(bad)
    assert.equal(h.node('spendSelectedDate').disabled, true)
    assert.equal(h.node('spendTable').children.length, 0)
    assert.equal(h.node('spendDetail').children.length, 0)
    assert.match(treeText(h.node('spendChart')), /暂无可核验|暂不可用/)
    assert.doesNotMatch(treeText(h.node('spendChart')), /¥0\.0000|spend-day/)
  }
})

test('missing current bucket values never produce a measured zero progressbar', () => {
  const h = harness({ functions: BUDGET_RENDERERS })
  const budget = budgetData()
  budget.usage.buckets.autonomy.spent_cny = null
  h.context.renderBudget(budget, false, 0)
  const meter = descendants(h.node('budgetBuckets')).find(node => node.role === 'progressbar')
  assert.equal(meter['aria-valuenow'], undefined)
  assert.equal(meter.dataset.state, 'unknown')
  assert.equal(meter['aria-valuetext'], '费用待核算')
  assert.equal(h.node('sMoney').textContent, '费用待确认')
  assert.match(treeText(h.node('budgetBuckets')), /执行已计 待核算/)
})

test('CNY budget view keeps estimation reservation and unknown costs separate without promising a provider hard cap', () => {
  const data = budgetData()
  data.usage.buckets.autonomy = { spent_cny: 0.2, reserved_cny: 0.3, remaining_cny: 0.5, unknown_count: 2, exhausted: false }
  const h = harness({ functions: BUDGET_RENDERERS })
  h.context.renderBudget(data, false, 0)
  const text = treeText(h.node('budgetBuckets')) + treeText(h.node('budgetEnforcement'))
  assert.match(text, /已计 ¥0\.2000 · 预留 ¥0\.3000/)
  assert.match(text, /2 轮费用待确认/)
  assert.match(text, /未知费用保留预留，不自动重试或伪造退款/)
  assert.match(h.node('mAccounting').textContent, /原生模型轮次.*本机费用估算/)
  assert.match(h.node('mAccounting').textContent, /未覆盖记录不能当作零费用/)
  assert.equal(h.node('sMoney').textContent, '部分费用待确认')
  const meter = descendants(h.node('budgetBuckets')).find(node => node.role === 'progressbar')
  assert.equal(meter['aria-valuenow'], '50')
  assert.equal(meter.dataset.state, 'unknown')
  assert.equal(h.node('bAutonomy').value, '1')
  assert.equal(h.node('bReserve').value, '0.05')
  assert.doesNotMatch(text, /免费|账户硬限额已生效/)
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
  data.usage.buckets.autonomy = { spent_cny: 0.98, reserved_cny: 0, remaining_cny: 0.02,
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
  assert.equal(h.node('bAutonomy').value, '1')
  assert.match(h.node('mAvailability').textContent, /等待首条计费回执/)
  assert.match(treeText(h.node('budgetBuckets')), /等待计费回执，费用未确认/)
  assert.doesNotMatch(treeText(h.node('budgetBuckets')), /执行已计 ¥0|记录内剩余 ¥|已使用和预留 0/)
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
    limits: { autonomy: 0.8, private: 0, group: 2 }, reserve_cny: 0.07 } })
  assert.equal(h.context.budgetSettingsRevision, 'c'.repeat(64))
})

test('actual CNY budget renders and repeated refreshes preserve source numbers and baseline', async () => {
  const data = budgetData({ daily_spending: spendingData([spendingDay('2026-11-01')]) })
  const before = structuredClone(data)
  const h = harness({ functions: BUDGET_RENDERERS.concat('loadBudget'), extra: {
    api: async () => ({ ok: true, budget: data }) } })
  await h.context.loadBudget(false)
  const expected = treeText(h.node('dailyTodayBuckets'))
  for(let i = 0; i < 3; i++) await h.context.loadBudget(true)
  h.context.renderDailySpending()
  assert.equal(h.node('bAutonomy').value, '1')
  assert.equal(h.node('bReserve').value, '0.05')
  assert.equal(h.node('bReserve').dataset.originalCny, '0.05')
  assert.equal(treeText(h.node('dailyTodayBuckets')), expected)
  assert.match(expected, /¥0\.1200/)
  assert.equal(h.node('mDailyTotal').textContent, '¥1.0200')
  assert.equal(h.node('sMoneySub').textContent, h.node('liveMoneySub').textContent)
  assert.match(h.node('sMoneySub').textContent, /预留 ¥0\.0500/)
  assert.match(h.node('liveMoneySub').title, /人民币口径.*本机估算.*未核对服务商实扣/)
  assert.deepEqual(data, before)
})

test('editing only the actual CNY autonomy input sends 3 directly and preserves every untouched precise CNY value', async () => {
  const original = budgetData({ effective: { currency: 'CNY', basis: 'owner_confirmed_numeric_cny_estimate', numeric_scale: 1,
    limits: { autonomy: 0.987_654_321_098_7, private: 0.123_456_789_012_3, group: 0 }, reserve_cny: 0.050_000_123_456_7 } })
  const writes = []
  const h = harness({ functions: BUDGET_RENDERERS.concat('saveBudget'), extra: { api: async (_route, options) => {
    const request = JSON.parse(options.body); writes.push(request)
    return { ok: true, budget: { ...original, revision: 'c'.repeat(64), effective: { ...original.effective, ...request.settings } } }
  } } })
  h.context.renderBudget(original, false, 0)
  assert.equal(h.node('bPrivate').value, '0.1235')
  assert.equal(h.node('bReserve').value, '0.05')
  assert.equal(h.node('bPrivateNote').textContent, '载入金额：0.123' + '456' + '789' + '0123 元')
  h.node('bAutonomy').value = '3.0'
  h.context.dirtyPages.add('money'); h.context.pageEdits.set('money', 1)
  await h.context.saveBudget()
  assert.deepEqual(writes, [{ revision: 'a'.repeat(64), settings: {
    limits: { autonomy: 3, private: original.effective.limits.private, group: 0 },
    reserve_cny: original.effective.reserve_cny } }])
  assert.equal(h.context.budgetSettingsRevision, 'c'.repeat(64))
  assert.equal(h.node('bAutonomy').value, '3')
  assert.equal(h.context.dirtyPages.has('money'), false)
})

test('an untouched precision budget round-trips exact original CNY and zero remains unlimited', async () => {
  const original = budgetData({ effective: { currency: 'CNY', basis: 'owner_confirmed_numeric_cny_estimate', numeric_scale: 1,
    limits: { autonomy: 0.100_000_123_456_7, private: 0, group: 0.000_001 }, reserve_cny: 0.05 } })
  let request
  const h = harness({ functions: BUDGET_RENDERERS.concat('saveBudget'), extra: { api: async (_route, options) => {
    request = JSON.parse(options.body); return { ok: true, budget: original }
  } } })
  h.context.renderBudget(original, false, 0)
  assert.equal(h.node('bPrivate').value, '0')
  assert.equal(h.node('bGroup').value, '0.000' + '001')
  await h.context.saveBudget()
  assert.deepEqual(request.settings, { limits: original.effective.limits, reserve_cny: 0.05 })
  assert.match(treeText(h.node('budgetBuckets')), /每日不限额/)
})

test('edited decimal and exponent reserve inputs use exact arithmetic at the micro-CNY boundary', async () => {
  for(const [text, expected] of [['0.0500', 0.05], ['5e-2', 0.05], ['.05', 0.05], ['0.000' + '001', 0.000_001], ['1e-6', 0.000_001]]){
    let request
    const h = harness({ functions: BUDGET_RENDERERS.concat('saveBudget'), extra: { api: async (_route, options) => {
      request = JSON.parse(options.body); return { ok: true, budget: budgetData() }
    } } })
    h.context.renderBudget(budgetData(), false, 0)
    h.node('bReserve').value = text
    await h.context.saveBudget()
    assert.equal(request.settings.reserve_cny, expected, text)
    assert.deepEqual(request.settings.limits, { autonomy: 1, private: 0, group: 0 }, text)
  }
})

test('too-small positive yuan limits or reserves are rejected before mutation and never silently become unlimited', async () => {
  for(const [id, text] of [['bAutonomy', '0.000' + '0001'], ['bPrivate', '1e-7'], ['bGroup', '9.99e-7'], ['bReserve', '9.99e-7'], ['bReserve', '0']]){
    let writes = 0
    const h = harness({ functions: BUDGET_RENDERERS.concat('saveBudget'), extra: {
      api: async () => { writes++; throw new Error('unexpected mutation') } } })
    h.context.renderBudget(budgetData(), false, 0)
    h.node(id).value = text
    h.context.dirtyPages.add('money')
    await h.context.saveBudget()
    assert.equal(writes, 0, id + ': ' + text)
    assert.equal(h.context.dirtyPages.has('money'), true)
    assert.match(h.node('moneyOut').textContent, /正金额至少为|每轮预留必须大于/)
  }
})

test('yuan edits and original CNY baselines survive a newer background snapshot and a completed in-flight save', async () => {
  const waiting = deferred(), original = budgetData()
  let request
  const h = harness({ functions: BUDGET_RENDERERS.concat('saveBudget'), extra: { api: async (_route, options) => {
    request = JSON.parse(options.body); return waiting.promise
  } } })
  h.context.renderBudget(original, false, 0)
  h.node('bAutonomy').value = '3'
  h.context.pageEdits.set('money', 1); h.context.dirtyPages.add('money')
  const save = h.context.saveBudget()
  h.node('bAutonomy').value = '4.5'; h.context.pageEdits.set('money', 2)
  const newer = budgetData({ revision: 'c'.repeat(64), effective: { currency: 'CNY', basis: 'owner_confirmed_numeric_cny_estimate', numeric_scale: 1,
    limits: { autonomy: 3, private: 0, group: 0 }, reserve_cny: 0.07 } })
  h.context.renderBudget(newer, true, 2)
  waiting.resolve({ ok: true, budget: newer }); await save
  assert.equal(request.settings.limits.autonomy, 3)
  assert.equal(request.settings.reserve_cny, 0.05)
  assert.equal(h.node('bAutonomy').value, '4.5')
  assert.equal(h.node('bReserve').value, '0.05')
  assert.equal(h.node('bReserve').dataset.originalCny, '0.05')
  assert.equal(h.context.budgetSettingsRevision, 'a'.repeat(64))
  assert.equal(h.context.dirtyPages.has('money'), true)
  assert.match(h.node('moneyOut').textContent, /还有未保存/)
})

test('actual CNY labels and accessible amounts describe direct estimates without currency conversion', () => {
  const h = harness({ functions: BUDGET_RENDERERS })
  h.context.renderBudget(budgetData({ daily_spending: spendingData([spendingDay('2026-11-01')]) }), false, 0)
  const view = h.source.split('<div class="page" id="page-money">')[1].split('<div class="page"')[0]
  assert.match(view, /按你确认的人民币口径记账/)
  assert.match(view, /费用为本机估算，未核对服务商实扣/)
  assert.match(view, /不作汇率换算/)
  assert.match(view, /单位人民币元/)
  assert.doesNotMatch(view, /USD|7\.2|美元/)
  for(const id of ['bAutonomy', 'bPrivate', 'bGroup', 'bReserve']){
    assert.match(view, new RegExp('for="' + id + '">[^<]*（人民币）'))
    assert.match(view, new RegExp('id="' + id + '" aria-describedby="' + id + 'Note moneyUnitNote"'))
    assert.match(h.node(id + 'Note').textContent, /载入金额：.* 元/)
  }
  assert.match(h.node('spendChart').children[0]['aria-label'], /¥1\.0200/)
  assert.match(treeText(h.node('spendDetail')), /¥1\.0200/)
})

test('budget reads reject incompatible units before replacing current CNY amounts inputs or history', async () => {
  for(const mismatch of [
    { schema: 'unified-budget/v1' },
    { currency: 'USD' }, { numeric_scale: 7.2 },
    { effective: { currency: 'USD', basis: 'owner_confirmed_numeric_cny_estimate' } },
    { effective: { currency: 'CNY', basis: 'meter_api_usd_estimate' } },
    { effective: { basis: 'owner_confirmed_numeric_cny_estimate' } }
  ]){
    const initial = budgetData({ daily_spending: spendingData([spendingDay('2026-11-01')]) })
    const h = harness({ functions: BUDGET_RENDERERS.concat('loadBudget'), extra: {
      api: async () => ({ ok: true, budget: { ...initial, ...mismatch, revision: 'f'.repeat(64) } }) } })
    h.context.renderBudget(initial, false, 0)
    const cards = treeText(h.node('budgetBuckets')), daily = treeText(h.node('dailyTodayBuckets'))
    const chart = h.node('spendChart').children[0]
    h.node('bAutonomy').value = '5'; h.context.dirtyPages.add('money'); h.context.pageEdits.set('money', 1)
    await h.context.loadBudget(true)
    assert.equal(treeText(h.node('budgetBuckets')), cards)
    assert.equal(treeText(h.node('dailyTodayBuckets')), daily)
    assert.equal(h.node('spendChart').children[0], chart)
    assert.equal(h.node('bAutonomy').value, '5')
    assert.equal(h.node('bReserve').value, '0.05')
    assert.equal(h.context.budgetSettingsRevision, initial.revision)
    assert.equal(h.context.dirtyPages.has('money'), true)
    assert.equal(h.node('budgetSave').disabled, true)
    assert.match(h.node('moneyOut').textContent, /单位接口不匹配/)
  }
})

test('incompatible daily unit responses cannot replace selected CNY bars table or summary', () => {
  const h = harness({ functions: DAILY_RENDERERS })
  const initial = spendingData([spendingDay('2026-10-31'), spendingDay('2026-11-01')])
  h.context.renderDailySpending(initial); h.context.selectSpendingDate('2026-10-31')
  const chart = h.node('spendChart').children[0], table = treeText(h.node('spendTable'))
  const summary = h.node('mDailyTotal').textContent
  for(const mismatch of [{schema:'daily-spending/v1'}, {currency:'USD'}, {numeric_scale:7.2}, {basis:'native_turn_api_usd_estimate'}]){
    h.context.renderDailySpending({ ...initial, ...mismatch, days: [spendingDay('2026-11-02', {total:99})] })
    assert.equal(h.node('spendChart').children[0], chart)
    assert.equal(treeText(h.node('spendTable')), table)
    assert.equal(h.node('spendSelectedDate').value, '2026-10-31')
    assert.equal(h.node('mDailyTotal').textContent, summary)
    assert.match(h.node('spendHistoryNote').textContent, /单位接口不匹配/)
  }
  const empty = harness({ functions: DAILY_RENDERERS })
  empty.context.renderDailySpending({ ...initial, currency: 'USD' })
  assert.equal(empty.node('mDailyTotal').textContent, '待核算')
  assert.equal(empty.node('spendTable').children.length, 0)
})

test('an incompatible save acknowledgement retains the CNY revision and unsaved input', async () => {
  const initial = budgetData(), writes = []
  const h = harness({ functions: BUDGET_RENDERERS.concat('saveBudget'), extra: { api: async (_route, options) => {
    writes.push(JSON.parse(options.body))
    return { ok: true, budget: budgetData({ schema: 'unified-budget/v1', revision: 'f'.repeat(64) }) }
  } } })
  h.context.renderBudget(initial, false, 0)
  h.node('bAutonomy').value = '5'; h.context.dirtyPages.add('money'); h.context.pageEdits.set('money', 1)
  await h.context.saveBudget()
  assert.deepEqual(writes, [{ revision: initial.revision, settings: { limits: {autonomy:5, private:0, group:0}, reserve_cny:0.05 } }])
  assert.equal(h.context.budgetSettingsRevision, initial.revision)
  assert.equal(h.node('bAutonomy').value, '5')
  assert.equal(h.context.dirtyPages.has('money'), true)
  assert.match(h.node('moneyOut').textContent, /单位接口不匹配/)
})

test('activity budget summary rejects an incompatible unit without replacing the latest CNY values', async () => {
  const initial = budgetData()
  initial.usage.buckets.autonomy.spent_cny = 0.9
  const h = harness({ functions: ['requireCnyBudget','budgetAmount','renderBudgetSummary','loadActivityBudget'], extra: {
    api: async () => ({ok:true, budget:budgetData({schema:'unified-budget/v1'})}) } })
  h.context.renderBudgetSummary(initial)
  const amount = h.node('liveMoneySub').textContent
  await h.context.loadActivityBudget()
  assert.match(amount, /¥0\.9000/)
  assert.equal(h.node('liveMoneySub').textContent, amount)
  assert.equal(h.node('sMoneySub').textContent, amount)
  assert.equal(h.node('liveMoney').textContent, '预算单位未确认')
  assert.match(h.node('liveMoneySub').title, /单位接口不匹配/)
})

test('a delayed v1 USD read cannot replace or disable a completed CNY save', async () => {
  const waiting = deferred(), initial = budgetData(), writes = []
  const current = budgetData({ revision: 'c'.repeat(64), effective: { ...initial.effective,
    limits: {autonomy:5, private:0, group:0} } })
  const h = harness({ functions: BUDGET_RENDERERS.concat('loadBudget','saveBudget'), extra: {
    api: async (_route, options) => {
      if(!options) return waiting.promise
      writes.push(JSON.parse(options.body)); return {ok:true, budget:current}
    } } })
  h.context.renderBudget(initial, false, 0)
  const previousRead = h.context.loadBudget(true)
  h.node('bAutonomy').value = '5'; h.context.dirtyPages.add('money'); h.context.pageEdits.set('money', 1)
  await h.context.saveBudget()
  waiting.resolve({ok:true, budget:{...initial, schema:'unified-budget/v1', currency:'USD'}})
  await previousRead
  assert.equal(writes.length, 1)
  assert.equal(writes[0].settings.limits.autonomy, 5)
  assert.equal(writes[0].settings.reserve_cny, 0.05)
  assert.equal(h.context.budgetSettingsRevision, current.revision)
  assert.equal(h.node('bAutonomy').value, '5')
  assert.equal(h.node('bReserve').value, '0.05')
  assert.equal(h.node('budgetSave').disabled, false)
  assert.match(h.node('moneyOut').textContent, /已保存/)
  assert.doesNotMatch(h.node('moneyOut').textContent, /单位接口不匹配/)
})

for(const [yuan, cny] of [['3.0', 3], ['5.0', 5]]) test('the complete actual budget script keeps its revision and original CNY over two saves of ' + yuan + ' yuan', async () => {
  const requests = [], initial = budgetData()
  const h = harness({ functions: ['appendPolicyRow'], extra: {
    api: async (_route, options) => {
      const request = JSON.parse(options.body); requests.push(request)
      return { ok: true, budget: { ...initial, revision: String(requests.length + 1).repeat(64),
        effective: { ...initial.effective, ...request.settings } } }
    }
  } })
  const start = h.source.indexOf('let budgetSettingsRevision = null;')
  const end = h.source.indexOf('\nlet memeData =', start)
  assert.ok(start > 0 && end > start)
  h.run(h.source.slice(start, end))
  h.context.initialBudget = initial
  h.run('renderBudget(initialBudget, false, 0)')
  h.node('bAutonomy').value = yuan
  h.context.dirtyPages.add('money'); h.context.pageEdits.set('money', 1)
  await h.run('saveBudget()')
  assert.equal(h.run('budgetSettingsRevision'), '2'.repeat(64))
  assert.equal(h.node('bAutonomy').value, String(Number(yuan)))
  assert.equal(h.node('bAutonomy').dataset.originalCny, String(cny))
  await h.run('saveBudget()')
  assert.deepEqual(requests.map(row => row.settings), [
    { limits: { autonomy: cny, private: 0, group: 0 }, reserve_cny: 0.05 },
    { limits: { autonomy: cny, private: 0, group: 0 }, reserve_cny: 0.05 }
  ])
  assert.deepEqual(requests.map(row => row.revision), ['a'.repeat(64), '2'.repeat(64)])
  assert.equal(h.run('budgetSettingsRevision'), '3'.repeat(64))
  assert.equal(h.node('bReserve').value, '0.05')
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
  const oldRead = deferred(), saved = budgetData({ revision: 'c'.repeat(64),
    daily_spending: spendingData([spendingDay('2026-11-01', { total: 0.8 })]) })
  saved.effective.limits.autonomy = 0.8
  const h = harness({ functions: BUDGET_RENDERERS.concat('loadBudget', 'saveBudget'), extra: {
    api: async (_path, options) => options?.method === 'POST' ? { ok: true, budget: saved } : oldRead.promise } })
  h.context.budgetSettingsRevision = 'a'.repeat(64)
  const read = h.context.loadBudget(false)
  for (const [id, value] of Object.entries({ bAutonomy: '0.8', bPrivate: '0', bGroup: '0', bReserve: '0.05' })) h.node(id).value = value
  await h.context.saveBudget()
  oldRead.resolve({ ok: true, budget: budgetData({ daily_spending: spendingData([spendingDay('2026-11-01')]) }) }); await read
  assert.equal(h.context.budgetSettingsRevision, 'c'.repeat(64))
  assert.equal(h.node('bAutonomy').value, '0.8')
  assert.match(treeText(h.node('spendChart')), /¥0\.8000/)
  assert.doesNotMatch(treeText(h.node('spendChart')), /¥1\.0200/)
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
    const h = harness({ functions: MONEY_FORMATTERS.concat('saveBudget'), extra: { api: () => { writes++; throw new Error('must not call') } } })
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
    assert.equal(h.node(input).value, money ? '1' : 4)
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
  assert.match(source, /输入和保存单位均为人民币元/)
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

test('Bili video cards expose real author and expandable subtitle evidence without claiming playback and retain expansion', () => {
  const h = harness({ functions: BILI_RENDERERS })
  const excerpt = 'Fixture subtitle '.repeat(20) + '<script>fixture()</script>'
  const history = biliData({ recent_videos: [{ bvid: 'BV1234567890', title: 'Fixture detail title', author: 'Fixture public author',
    at: Date.UTC(2026, 10, 1, 10), kinds: ['details','subtitle','frames'], content_excerpt: excerpt, content_truncated: true }],
    records: [{ kind: 'subtitle', tool: 'bili_subtitle', status: 'succeeded', video: { bvid: 'BV1234567890' }, subtitle_chars: 900,
      segment_count: 15, content_excerpt: excerpt, content_truncated: true },
      { kind: 'frames', status: 'succeeded', video: { bvid: 'BV1234567890' }, frame_count: 2 }] })
  h.context.renderMainBili(history)
  const text = treeText(h.node('bMainVideos'))
  assert.match(text, /Fixture public author.*字幕摘录/)
  assert.match(text, /字幕内容摘录，未证明完整播放 · 部分内容/)
  assert.match(text, /原始字幕 900 字，15 段/)
  assert.match(text, /2 个视频帧；不等于连续播放/)
  assert.doesNotMatch(text, /已看完|已完整观看/)
  const body = descendants(h.node('bMainVideos')).find(row => row.tagName === 'PRE')
  assert.equal(body.textContent, excerpt); assert.equal(body.innerHTML, '')
  h.node('bMainVideos').querySelectorAll('details[data-record-key]')[0].open = true
  h.context.renderMainBili(structuredClone(history))
  assert.equal(h.node('bMainVideos').querySelectorAll('details[data-record-key]')[0].open, true)
  assert.equal(h.calls.length, 0)
})

test('Bili public reply details show the exact attempted body and receipt evidence without opening private content', () => {
  const h = harness({ functions: BILI_RENDERERS })
  const text = 'Fixture public comment '.repeat(20)
  h.context.renderMainBili(biliData({ replies: [{ id: 'fixture-public-comment', type: 'comment_reply', status: 'accepted',
    receipt_present: true, evidence: 'platform_receipt', text }, { id: 'fixture-private', type: 'private_message', status: 'unknown', text: 'PRIVATE_FIXTURE_HIDDEN' }] }))
  assert.match(treeText(h.node('bMainReplyRecords')), /平台回执：存在.*平台接口返回了接受回执/)
  assert.match(treeText(h.node('bMainReplyRecords')), /公开可见性尚未核验/)
  assert.doesNotMatch(treeText(h.node('bMainReplyRecords')), /PRIVATE_FIXTURE_HIDDEN|已公开/)
  const details = h.node('bMainReplyRecords').querySelectorAll('details[data-record-key]')
  assert.equal(details.length, 1)
  assert.equal(descendants(details[0]).find(row => row.tagName === 'PRE').textContent, text)
})

test('Bili selected-part observations show actual subtitle range and partial content, without inventing frame inspection', () => {
  const h = harness({ functions: BILI_RENDERERS })
  h.context.renderMainBili(biliData({ recent_videos: [{ bvid: 'BV1234567890', kinds: ['observation'] }],
    records: [{ tool: 'bili_observe_video', kind: 'observation', status: 'succeeded', video: { bvid: 'BV1234567890' },
      page: 2, subtitle_status: 'available', segment_count: 2, subtitle_chars: 20,
      segment_range: { start_sec: 30.5, end_sec: 52 }, content_partial: true,
      content_excerpt: '<script>source text</script>', content_truncated: true }] }))
  const text = treeText(h.node('bMainVideos'))
  assert.match(text, /分P：第 2 段/)
  assert.match(text, /已读取字幕区间：30.5–52 秒；不代表连续播放/)
  assert.match(text, /本次内容只覆盖部分片段/)
  assert.doesNotMatch(text, /取得.*视频帧|已看完|已完整观看/)
  assert.equal(descendants(h.node('bMainVideos')).find(row => row.tagName === 'PRE').innerHTML, '')
  assert.equal(h.calls.length, 0)
})

test('Bili explicit mention status never claims the administrator received a notification', () => {
  const h = harness({ functions: BILI_RENDERERS })
  h.context.renderMainBili(biliData({ replies: [{ type: 'comment', status: 'accepted',
    text: 'Fixture public comment', mention_admin_requested: true, mention_applied: true },
    { type: 'private_message', status: 'unknown', mention_admin_requested: true, mention_applied: true }] }))
  const text = treeText(h.node('bMainReplyRecords'))
  assert.match(text, /本次请求已附 @管理员；提醒是否送达仍未知/)
  assert.doesNotMatch(text, /管理员已收到|提醒送达成功/)
  assert.equal(h.calls.length, 0)
})

test('Bili source failure and older projection snapshots never masquerade as an empty successful history', () => {
  for (const data of [{ status: 'unavailable' }, { status: 'ok', records: [] }]) {
    const h = harness({ functions: BILI_RENDERERS })
    h.context.renderMainBili(data)
    assert.equal(h.node('bMainCount').textContent, '—')
    assert.equal(h.node('bMainReplies').textContent, '—')
    assert.match(treeText(h.node('bMainReplyRecords')), /暂不可用|快照未含完整读取记录/)
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

test('Bili page uses current main-agent records and removes obsolete archives and duty controls while preserving login', () => {
  const source = readFileSync(PANEL, 'utf8')
  const view = source.split('<div class="page" id="page-bili">')[1].split('<div class="page" id="page-schedule">')[0]
  assert.match(view, /id="bMainVideos"/)
  assert.match(view, /id="bMainReplyRecords"/)
  assert.doesNotMatch(view, /旧|bLegacyRecords|renderBiliArchive/)
  assert.match(view, /startBiliLogin/)
  assert.doesNotMatch(view, /id="(?:biliDuty|biliSeen|biliLog)"|doOneRun|deleteOnBili|deleteBiliRecord|bili\/onerun/)
  assert.doesNotMatch(source, /function (?:runBiliNow|doOneRun|deleteOnBili|deleteBiliRecord|verifyComment|loadBiliDuty|saveBiliDuty)\(/)
})

function loadCalendar(h) {
  const start = h.source.indexOf('const GROUPS = [')
  h.run(h.source.slice(start, h.source.indexOf('\n];', start) + 3))
  h.load('groupOf')
}

test('memory groups use Beijing calendar day week and month across midnight rather than rolling hours', () => {
  const h = harness({ now: Date.parse('2026-11-02T00:09:00+08:00') })
  loadCalendar(h)
  const group = date => h.context.groupOf(Date.parse(date) / 1000).key
  assert.equal(group('2026-11-02T00:01:00+08:00'), 'today')
  assert.equal(group('2026-11-01T21:17:00+08:00'), 'month')
  assert.equal(group('2026-10-31T23:59:00+08:00'), 'past')
  assert.equal(group('2026-11-03T00:01:00+08:00'), 'future')
  assert.equal(h.context.groupOf(NaN).key, 'unknown')
  h.setNow(Date.parse('2027-01-01T12:00:00+08:00'))
  assert.equal(group('2026-12-31T19:00:00+08:00'), 'week')
})

test('real file pagination appends beyond sixty without replacing expanded rows or selection', () => {
  const h = harness({ functions: ['appendPlainRow', 'renderFileRows', 'appendFileRows', 'updateBulk', 'fmtSize', 'fmtTime'],
    extra: { FILE_PAGE: 60, fileCache: [], fileShown: 0, currentDir: 'notes', openFile() {}, deleteFile() {} } })
  loadCalendar(h)
  h.context.fileCache = Array.from({ length: 110 }, (_, index) => ({ name: index === 0 ? '<readable & literal>.md' : 'note-' + index + '.md', size: 5, mtime: Date.now() / 1000 }))
  h.context.renderFileRows('notes')
  const list = h.node('fileList'), first = Object.values(list.fileGroups).find(group => group.shown).wrap.children[0]
  const firstIdentity = first
  first.classList.add('open')
  first.children.find(node => node.className === 'fchk').onchange({ target: { checked: true } })
  const expansion = element(); expansion.textContent = 'Preserved expanded content'; first.appendChild(expansion)
  assert.equal(h.context.fileShown, 60)
  assert.match(treeText(first), /<readable & literal>\.md/)
  list.fileMoreButton.onclick()
  assert.equal(h.context.fileShown, 110)
  assert.equal(list.fileMoreButton, null)
  assert.equal(Object.values(list.fileGroups).find(group => group.shown).wrap.children[0], firstIdentity)
  assert.equal(h.context.selected['<readable & literal>.md'], true)
  assert.equal(first.classList.contains('open'), true)
  assert.match(treeText(first), /Preserved expanded content/)
  assert.equal(Object.values(list.fileGroups).reduce((count, group) => count + group.shown, 0), 110)
  assert.equal(h.calls.length, 0)
})

test('file loading requests bounded metadata beyond the first sixty instead of an incomplete preview cache', async () => {
  const calls = []
  const h = harness({ functions: ['loadFiles'], extra: { fileLoadGeneration: 0, FILE_LIMIT: 1000,
    fileCache: [], updateBulk() {}, renderFileRows() {}, api: async path => { calls.push(path); return { ok: true, files: [] } } } })
  await h.context.loadFiles('diary')
  assert.deepEqual(calls, ['/files?dir=diary&preview=0&limit=1000'])
})

const MEME_FUNCTIONS = ['memeItems', 'memeList', 'appendMemes', 'selectMeme', 'updateMemeSelection', 'moveSelectedMeme', 'editTags', 'moveMeme', 'deleteMeme']
test('real meme card selects the exact physical item and enables page tags with movement restricted to collected images', () => {
  const items = [{ key: 'first', source: 'plugin', tag: 'physical-a', file: 'same.png' },
    { key: 'second', source: 'stolen', tag: 'physical-b', file: 'same.png' }]
  const h = harness({ functions: MEME_FUNCTIONS, extra: { memeData: [{ source: 'merged', tag: 'display-alias', count: 2, items }] } })
  h.context.document.querySelectorAll = selector => descendants(h.node('memeGrid')).filter(node => selector === '.meme-select' ? node.className === 'meme-select' : selector === '.meme-delete' ? node.classList.contains('meme-delete') : false)
  h.context.appendMemes()
  const cards = h.node('memeGrid').children
  assert.equal(cards.length, 2)
  const button = cards[0].children[0]
  assert.equal(button.tagName, 'BUTTON')
  assert.match(button.children[0].src, /\/plugin\/physical-a\/same\.png$/)
  button.onclick()
  assert.equal(h.context.memeSel.key, 'first')
  assert.equal(h.node('memeTagSave').disabled, false)
  assert.equal(h.node('memeMove').disabled, true)
  cards[1].children[0].onclick()
  assert.equal(h.context.memeSel.key, 'second')
  assert.equal(h.node('memeMove').disabled, false)
  assert.equal(button['aria-pressed'], 'false')
  assert.equal(cards[1].children[0]['aria-pressed'], 'true')
  assert.equal(h.calls.length, 0)
})

test('meme tag save preserves the selected physical tuple and concurrent clicks submit only one mutation', async () => {
  const waiting = deferred(), requests = []
  let reloads = 0
  const h = harness({ functions: MEME_FUNCTIONS, extra: { loadMemes: async () => { reloads++ },
    api: async (...args) => { requests.push(args); return waiting.promise } } })
  h.context.selectMeme({ key: 'selected', source: 'stolen', tag: 'physical', file: 'same.png' })
  h.node('tagInput').value = 'friendly,calm'
  const save = h.context.editTags()
  await h.context.editTags()
  assert.equal(requests.length, 1)
  assert.deepEqual(JSON.parse(requests[0][1].body), { source: 'stolen', tag: 'physical', file: 'same.png', tags: ['friendly', 'calm'] })
  waiting.resolve({ ok: true, tags: ['friendly', 'calm'] }); await save
  assert.equal(reloads, 1)
  assert.equal(h.context.memeMutationPending, false)
})

test('new meme items keep same-name files separate and refuse ambiguous legacy merged metadata', () => {
  const h = harness({ functions: ['memeItems'] })
  assert.equal(h.context.memeItems({ source: 'merged', tag: 'alias', files: ['same.png'], refs: { 'same.png': 'plugin' } }).length, 0)
  const group = { source: 'merged', tag: 'alias', items: [
    { key: 'one', source: 'plugin', tag: 'first', file: 'same.png' },
    { key: 'two', source: 'stolen', tag: 'second', file: 'same.png' }] }
  assert.equal(h.context.memeItems(group).length, 2)
})

test('picture review uses confirmed main-agent observation receipt and makes no claim of independent finished classification', async () => {
  const requests = []
  const h = harness({ functions: ['loadMemeConfig', 'startClassify'], extra: { api: async (...args) => {
    requests.push(args)
    if(args[0] === '/memes/config') return { ok: true, config: { maxPerRun: 12 },
      behavior: { mode: 'unified', managed_by: 'main_agent', collection: 'existing-files-only' } }
    return { ok: true, managed_by: 'main_agent', queued: true }
  } } })
  await h.context.loadMemeConfig(); await h.context.startClassify()
  assert.equal(h.node('memeReview').disabled, false)
  assert.match(h.node('classifyOut').textContent, /观察已排队.*主 agent 决定/)
  assert.doesNotMatch(h.node('classifyOut').textContent, /分类完成|已经开始在后台归类|500/)
  assert.equal(requests.length, 2)
})

test('picture settings reject fractional and out-of-range batches before any mutation', async () => {
  for (const value of ['1.5', '21', '0', '', 'NaN']) {
    let calls = 0
    const h = harness({ functions: ['saveMemeConfig'], extra: { api() { calls++; throw Error('must not call') } } })
    h.node('mcfgSave').disabled = false; h.node('mcfgMax').value = value
    await h.context.saveMemeConfig()
    assert.equal(calls, 0)
  }
})

test('NapCat refresh loads real profile and waits for its four startup readers', async () => {
  const waiting = deferred(); let profiles = 0
  const h = harness({ functions: ['loadNapcat'], extra: { loadNapcat2: () => waiting.promise,
    loadQQFriends: async () => {}, loadQQGroups: async () => {}, loadNapcatProfile: async () => { profiles++ },
    api: async () => ({ ok: true, napcat: { found: false } }) } })
  let finished = false
  const loading = h.context.loadNapcat().then(() => { finished = true })
  await Promise.resolve(); assert.equal(profiles, 1); assert.equal(finished, false)
  waiting.resolve(); await loading; assert.equal(finished, true)
})

test('current schedule omits only verified inactive retired tasks and treats reminder and QQ names as text', async () => {
  const h = harness({ functions: ['loadSchedule', 'loadQQFriends', 'loadQQGroups', 'appendPlainRow', 'plainMessage'], extra: { api: async path =>
    path === '/schedule' ? { ok: true, mode: 'unified', reminders: [{ i: 0, text: '<img onerror=unsafe()>' }], tasks: [
      { name: 'inactive', retired: true, inactive_verified: true }, { name: 'unknown', retired: true },
      { name: 'running', retired: true, inactive_verified: false }] }
      : path === '/qq/friends' ? { ok: true, friends: [{ nickname: '<script>nickname</script>' }] }
      : { ok: true, groups: [{ group_name: '<img src=untrusted>' }] } } })
  await h.context.loadSchedule(); await h.context.loadQQFriends(); await h.context.loadQQGroups()
  assert.equal(h.node('taskBody').children.length, 2)
  assert.doesNotMatch(treeText(h.node('taskBody')), /inactive/)
  assert.match(treeText(h.node('remBody')), /<img onerror=unsafe\(\)>/)
  assert.match(treeText(h.node('qqFriendList')), /<script>nickname<\/script>/)
  assert.match(treeText(h.node('qqGroupList')), /<img src=untrusted>/)
  for (const node of descendants(h.node('remBody')).concat(descendants(h.node('qqFriendList')), descendants(h.node('qqGroupList')))) assert.doesNotMatch(node.innerHTML, /unsafe|nickname|untrusted/)
})

test('visible controls expose current capabilities without archive browsers or an online pull pretending to deploy', () => {
  const source = readFileSync(PANEL, 'utf8')
  assert.doesNotMatch(source, /doUpdate|api\('\/update'|data-ext="\.archive"|memTrimSeen|memCorpusLoad|500 多张|能看、能审批|没它就没法斗图|文字会原样发出去/)
  assert.match(source, /运行部署和实际加载需要分别核验/)
  assert.match(source, /主 agent.*静默状态和发送许可/)
})

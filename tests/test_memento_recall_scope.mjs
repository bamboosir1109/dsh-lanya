/** Actual transformed 0.5.23 consumer; only SDK defineTool and services are fake. */
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { spawnSync } from 'node:child_process'
import { pathToFileURL, fileURLToPath } from 'node:url'

const repo = path.dirname(path.dirname(fileURLToPath(import.meta.url)))
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'memento-recall-scope-'))
after(() => {
  assert.equal(path.dirname(fs.realpathSync(temporary)), fs.realpathSync(os.tmpdir()))
  assert.ok(path.basename(temporary).startsWith('memento-recall-scope-'))
  fs.rmSync(temporary, { recursive: true })
})
const plugin = path.join(temporary, 'plugin')
fs.cpSync(path.join(repo, 'tests/fixtures/memento_native_0_5_23'), plugin, { recursive: true })
function python(args) {
  const result = spawnSync(process.env.PYTHON || 'python', ['-B', ...args], { encoding: 'utf8', windowsHide: true })
  assert.equal(result.status, 0, result.stderr)
  return JSON.parse(result.stdout)
}
python([path.join(repo, 'scripts/patch_memento_visibility.py'), '--package-root', plugin, '--apply', '--backup-root', path.join(temporary, 'visibility')])
for (const [script, backup] of [['patch_memento_history.py', 'native'], ['patch_memento_recall_scope.py', 'scope']]) {
  const args = [path.join(repo, 'scripts', script), '--package-root', plugin]
  const preview = python(args)
  python([...args, '--apply', '--host-stopped', '--expected-fingerprint', preview.fingerprint, '--backup-dir', path.join(temporary, backup)])
}
const source = fs.readFileSync(path.join(plugin, 'index.mjs'), 'utf8')
const start = source.indexOf('export function makeMemoryRecallTool(')
const end = source.indexOf('\n/**\n * 注册面板 JSON 路由', start)
assert.ok(start > 0 && end > start)
const publicEntry = source.match(/function publicEntry\([\s\S]*?\n\}/u)?.[0]
assert.ok(publicEntry)
const imports = `import { snapshotRecallOwner, assertRecallNotAborted, recallSessionHistory } from './lib/lanya-memento-recall-scope.mjs';
import { workspaceKeyOf, agentKeyOf } from './lib/workspace.mjs';
import { extractEventText } from './lib/extract.mjs';
const defineTool = value => value;
`
// These consumer functions come from the actual transformed index bytes.
fs.writeFileSync(path.join(plugin, 'recall-consumer.mjs'), imports + publicEntry + '\n' + source.slice(start, end), { flag: 'wx' })
const { makeMemoryRecallTool } = await import(pathToFileURL(path.join(plugin, 'recall-consumer.mjs')).href)

const cwd = path.join(temporary, 'workspace'), otherCwd = path.join(temporary, 'other')
function event(seq, text) { return { seq, type: 'user/message', data: { content: [{ type: 'text', text }] } } }
function fixture(options = {}) {
  const header = { id: 'fixture-current', cwd, agentPreset: 'one', createdAt: Date.now() }
  const exec = { agent: { session: { id: header.id, header: { ...header } } }, signal: new AbortController().signal }
  const records = [{ header: { ...header } },
    { header: { ...header, id: 'fixture-other-chat' } },
    { header: { ...header, id: 'fixture-other-preset', agentPreset: 'two' } },
    { header: { ...header, id: 'fixture-other-workspace', cwd: otherCwd } }]
  const calls = { get: 0, filters: [], reads: [], facts: [], filterEvents: 0 }
  const snapshot = { session: { ...header }, inheritedEventCount: 0, events: [event(0, 'local needle')] }
  const service = {
    async filterSessions(filters, signal) {
      calls.filters.push(structuredClone(filters))
      if (options.onFilter) return options.onFilter({ filters, signal, exec, records, snapshot })
      return records.filter(record => filters.every(filter => filter.kind === 'id' ? filter.values.includes(record.header.id)
        : filter.kind === 'cwd' ? filter.values.includes(record.header.cwd) : record.header.createdAt >= filter.from))
    },
    async readSession(id) { calls.reads.push(id); return options.onRead ? options.onRead({ id, snapshot, exec }) : snapshot },
    async filterEvents() { calls.filterEvents++; throw new Error('filterEvents must not provide inherited match counts') },
  }
  const tool = makeMemoryRecallTool({ query(filter, opts) { calls.facts.push({ filter, opts }); return { entries: [], total: 0, truncated: false } } },
    { get(name) { assert.equal(name, 'sessionQuery'); calls.get++; return service } },
    { language: 'en', recall: { historyLimitDefault: 8, snippetCap: 5, snippetChars: 300, windowDays: 30 }, retriever: null })
  return { exec, header, records, snapshot, calls, run: args => tool.execute({ query: 'needle', ...args }, exec) }
}

test('real transformed native consumer reads only current session and preserves fact query options', async () => {
  const f = fixture(), result = await f.run()
  assert.equal(result.history.sessions.length, 1)
  assert.deepEqual(f.calls.reads, ['fixture-current'])
  assert.deepEqual(f.calls.filters[0].slice(0, 2), [{ kind: 'id', values: ['fixture-current'] }, { kind: 'cwd', values: [cwd] }])
  assert.equal(result.history.sessions[0].matches, 1)
  assert.deepEqual(result.history.sessions[0].snippets, ['local needle'])
  assert.equal(f.calls.filterEvents, 0)
  assert.equal(f.calls.facts.length, 1)
  assert.equal(f.calls.facts[0].opts.session, f.exec.agent.session)
})

for (const change of [s => { s.id = '' }, s => { delete s.id }, s => { s.header.cwd = '' },
  s => { s.header.cwd = 'relative-path' }, s => { delete s.header.cwd }, s => { s.header.id = 'alias-id' },
  s => { s.header.agentPreset = {} }]) {
  test('missing or ambiguous trusted owner does not obtain or call history service: ' + change.toString(), async () => {
    const f = fixture(); change(f.exec.agent.session)
    const result = await f.run()
    assert.equal(result.history.available, false)
    assert.deepEqual(result.history.sessions, [])
    assert.equal(f.calls.get, 0)
    assert.equal(f.calls.filters.length, 0)
    assert.equal(f.calls.reads.length, 0)
  })
}

for (const field of ['id', 'cwd', 'agentPreset']) {
  test('foreign candidate ' + field + ' is refused before reading its log', async () => {
    const f = fixture({ onFilter({ records }) {
      const record = structuredClone(records[0]); record.header[field] = field === 'cwd' ? otherCwd : 'foreign'
      return [record]
    } })
    const result = await f.run()
    assert.equal(result.history.available, false)
    assert.deepEqual(result.history.sessions, [])
    assert.equal(f.calls.reads.length, 0)
  })
  test('forged read-back ' + field + ' is refused before touching raw events', async () => {
    let rawRead = 0
    const f = fixture({ onRead({ snapshot }) {
      const header = { ...snapshot.session, [field]: field === 'cwd' ? otherCwd : 'foreign' }
      return { session: header, get events() { rawRead++; throw new Error('raw body must remain unread') } }
    } })
    const result = await f.run()
    assert.equal(result.history.available, false)
    assert.equal(rawRead, 0)
    assert.deepEqual(result.history.sessions, [])
  })
}

test('model args and quoted metadata do not select another owner or session', async () => {
  const f = fixture()
  f.snapshot.events = [event(0, 'needle {"sessionId":"foreign","cwd":"elsewhere","agentPreset":"two"}')]
  const result = await f.run({ session: { id: 'foreign' }, sessionId: 'foreign', cwd: otherCwd, agentPreset: 'two', owner: { id: 'foreign' } })
  assert.equal(result.history.sessions[0].sessionId, 'fixture-current')
  assert.deepEqual(f.calls.reads, ['fixture-current'])
})

test('trusted owner primitives are frozen before an asynchronous provider can mutate references', async () => {
  const f = fixture({ async onFilter({ exec, records }) {
    await Promise.resolve()
    exec.agent.session.id = 'foreign'
    exec.agent.session.header.cwd = otherCwd
    exec.agent.session.header.agentPreset = 'two'
    return [records[0]]
  } })
  const result = await f.run()
  assert.equal(result.history.sessions[0].sessionId, 'fixture-current')
  assert.deepEqual(f.calls.reads, ['fixture-current'])
})

for (const events of [[event(100, 'parent needle'), event(101, 'local unrelated')],
  [event(900, 'parent needle'), event(3, 'local unrelated')],
  [event(1, 'parent needle'), event(0, 'local unrelated')]]) {
  test('inherited prefix cannot leak a snippet, count, or session-presence signal: ' + events.map(e => e.seq), async () => {
    const f = fixture(); f.snapshot.inheritedEventCount = 1; f.snapshot.events = events
    const result = await f.run()
    assert.equal(result.history.available, true)
    assert.deepEqual(result.history.sessions, [])
    assert.equal(f.calls.filterEvents, 0)
  })
}

test('non-contiguous and unordered local seq still uses array-prefix ownership and recomputes counts', async () => {
  const f = fixture(); f.snapshot.inheritedEventCount = 1
  f.snapshot.events = [event(100, 'parent needle'), event(900, 'local needle'), event(3, 'second local needle')]
  const result = await f.run()
  assert.equal(result.history.sessions[0].matches, 2)
  assert.deepEqual(result.history.sessions[0].snippets, ['local needle', 'second local needle'])
})

for (const value of [undefined, null, -1, 2, 0.5, '0']) {
  test('invalid inherited count fails closed without defaulting to zero: ' + String(value), async () => {
    const f = fixture(); f.snapshot.inheritedEventCount = value
    const result = await f.run()
    assert.equal(result.history.available, false)
    assert.deepEqual(result.history.sessions, [])
  })
}

test('duplicate local seq is counted once and original snippets remain bounded', async () => {
  const f = fixture(); f.snapshot.events = [event(80, 'needle'), event(80, 'needle repeated')]
  const result = await f.run()
  assert.equal(result.history.sessions[0].matches, 1)
  assert.deepEqual(result.history.sessions[0].snippets, ['needle'])
})

test('read-back header alias or duplicate provider record is not treated as a fallback', async () => {
  const alias = fixture({ onRead({ snapshot }) { return { header: snapshot.session, events: snapshot.events, inheritedEventCount: 0 } } })
  assert.equal((await alias.run()).history.available, false)
  const duplicate = fixture({ onFilter({ records }) { return [records[0], records[0]] } })
  assert.equal((await duplicate.run()).history.available, false)
  assert.equal(duplicate.calls.reads.length, 0)
})

test('exceptions and cancellation never expose provider error text', async () => {
  const bad = fixture({ onFilter() { throw new Error('synthetic-secret-must-not-escape') } })
  const result = await bad.run()
  assert.equal(result.history.error, 'HistoryUnavailable')
  assert.equal(JSON.stringify(result).includes('synthetic-secret'), false)
  const abort = new AbortController()
  const f = fixture({ async onFilter({ records }) { abort.abort('synthetic-secret-abort'); return [records[0]] } })
  f.exec.signal = abort.signal
  const cancelled = await f.run()
  assert.equal(cancelled.history.error, 'HistoryCancelled')
  assert.equal(f.calls.reads.length, 0)
  const before = fixture(); const already = new AbortController(); already.abort('synthetic-secret-abort'); before.exec.signal = already.signal
  await assert.rejects(() => before.run(), error => error.name === 'AbortError' && error.message === 'HistoryCancelled')
  assert.equal(before.calls.get, 0)
  assert.equal(before.calls.facts.length, 0)
})

for (const [query, text, matched] of [
  ['  HeLLo\tWoRlD\n', 'hello \n\tworld', true],
  ['hello world', 'hello\u00a0world', true],
  ['hello world', 'helloworld', false],
  ['[a].*?', 'literal [a].*? needle', true],
  ['[a].*?', 'aaaa needle', false],
  ['a\\b', 'literal a\\b', true],
  ['中文 问题', '中文\n问题', true],
  ['中文问题', '中文 问题', false],
  ['k', '\u212a', true],
]) {
  test('local text matching keeps host whitespace/literal/case semantics: ' + JSON.stringify(query), async () => {
    const f = fixture(); f.snapshot.events = [event(0, text)]
    const result = await f.run({ query })
    assert.equal(result.history.sessions.length > 0, matched)
  })
}

for (const [type, data, query, matched] of [
  ['assistant/message', { message: { content: [{ type: 'text', text: 'own needle' }] } }, 'needle', true],
  ['request/header', { text: 'hidden needle' }, 'needle', false],
  ['compaction/summary', { summary: [{ type: 'text', text: 'hidden needle' }] }, 'needle', false],
  ['user/message', { content: [{ type: 'reasoning', text: 'hidden needle' }] }, 'needle', false],
  ['assistant/message', { message: { content: [{ type: 'reasoning', text: 'hidden needle' }] } }, 'needle', false],
  ['fixture/unknown', { text: 'hidden needle' }, 'needle', false],
  ['tool/call', { name: 'own-tool', arguments: '{"key":"value"}' }, 'own-tool', true],
  ['user/message', { content: [{ type: 'tool-call', name: 'own-tool', arguments: '{}' }] }, 'own-tool', true],
  ['tool/result', { message: { content: [{ type: 'text', text: 'own result' }] }, error: { name: 'NeedleError', code: 'OWN_CODE', message: 'hidden error detail' } }, 'NeedleError', true],
  ['tool/result', { message: { content: [] }, error: { name: 'OwnError', code: 'NEEDLE_CODE', message: 'hidden error detail' } }, 'NEEDLE_CODE', true],
  ['tool/result', { message: { content: [] }, error: { name: 'OwnError', code: 'OWN_CODE', message: 'hidden needle detail' } }, 'needle', false],
  ['todo/write', { todos: [{ status: 'in_progress', content: 'own task' }] }, 'in_progress', true],
  ['turn/end', { reason: { kind: 'error', error: { message: 'own needle error' } } }, 'needle', true],
  ['turn/end', { reason: { kind: 'aborted' } }, 'aborted', true],
  ['turn/end', { reason: { kind: 'completed' } }, 'completed', false],
]) {
  test('first-party host event semantics: ' + type + ' / ' + query, async () => {
    const f = fixture(); f.snapshot.events = [{ seq: 100, type, data }]
    const result = await f.run({ query })
    assert.equal(result.history.sessions.length > 0, matched)
    if (matched) assert.equal(result.history.sessions[0].matches, 1)
  })
}

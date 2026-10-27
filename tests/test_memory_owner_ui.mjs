import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'

const html = fs.readFileSync(new URL('../panel/panel.html', import.meta.url), 'utf8')
const start = html.indexOf('let memOwners = [];')
const end = html.indexOf('async function memSmartSearch()', start)
assert.ok(start >= 0 && end > start)
const section = html.slice(start, end)

function fixture() {
  const elements = new Map()
  const select = { value: '', children: [], appendChild(option) { this.children.push(option) } }
  Object.defineProperty(select, 'innerHTML', { set() { this.children = []; this.value = '' } })
  elements.set('memOwner', select)
  for (const id of ['memMergePreview', 'memMergeApply', 'memCompressPreview', 'memCompressApply']) {
    elements.set(id, { disabled: true })
  }
  for (const id of ['memOwnerInfo', 'memCorpusOut', 'memCorpus']) elements.set(id, { textContent: '' })
  let owners = [
    { track: 'agent', scope: 'workspace', workspace_key: 'fixture-workspace', agent_key: 'fixture-agent', count: 2 },
    { track: 'user', scope: 'user-global', workspace_key: '', agent_key: '', count: 3 }
  ]
  const requests = []
  let refreshes = 0
  const sandbox = {
    $: id => elements.get(id),
    document: { createElement: () => ({ value: '', textContent: '' }) },
    api: async (url, options) => {
      if (url === '/memory/owners') return { ok: true, owners }
      requests.push({ url, body: JSON.parse(options.body) })
      return { ok: true, merges: 1, before: 3, merged: 1, trimmed: 1, left: 1, batch_id: 'fixture-batch' }
    },
    loadMemoryOverview: () => { refreshes++ },
    fetch: () => { throw new Error('network forbidden') }
  }
  vm.runInNewContext(section + '\nthis.functions = {memSelectedOwner, memOwnerChange, memOwnersLoad, memConsolidate, memCompress};', sandbox)
  return { select, elements, requests, functions: sandbox.functions,
    setOwners(value) { owners = value }, get refreshes() { return refreshes } }
}

test('all inline panel scripts remain syntactically valid', () => {
  for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) {
    new vm.Script(match[1])
  }
})

test('maintenance has no default owner and refuses requests without a choice', async () => {
  const f = fixture()
  await f.functions.memOwnersLoad()
  assert.equal(f.select.value, '')
  assert.equal(f.functions.memSelectedOwner(), null)
  assert.ok(f.select.children.some(option => option.textContent.includes('agent共享空键')))
  await f.functions.memConsolidate(false)
  await f.functions.memCompress(false)
  assert.equal(f.requests.length, 0)
  for (const id of ['memMergePreview', 'memMergeApply', 'memCompressPreview', 'memCompressApply']) {
    assert.equal(f.elements.get(id).disabled, true)
    assert.match(html, new RegExp('id="' + id + '"[^>]*disabled'))
  }
})

test('explicit shared owner retains all four strings for preview and apply', async () => {
  const f = fixture()
  await f.functions.memOwnersLoad()
  f.select.value = '1'
  f.functions.memOwnerChange()
  assert.equal(f.elements.get('memMergeApply').disabled, false)
  await f.functions.memConsolidate(true)
  await f.functions.memConsolidate(false)
  await f.functions.memCompress(true)
  await f.functions.memCompress(false)
  assert.equal(f.requests.length, 4)
  assert.deepEqual(f.requests.map(request => request.body.dry), [true, false, true, false])
  for (const request of f.requests) {
    assert.deepEqual(request.body.selection,
      { track: 'user', scope: 'user-global', workspace_key: '', agent_key: '' })
  }
  assert.equal(f.refreshes, 2)
  assert.match(f.elements.get('memCorpusOut').textContent, /fixture-batch/)
})

test('refresh preserves an explicit owner by its four keys and resets missing owners', async () => {
  const f = fixture()
  await f.functions.memOwnersLoad()
  f.select.value = '1'
  f.setOwners([{ track: 'user', scope: 'user-global', workspace_key: '', agent_key: '', count: 1 }])
  await f.functions.memOwnersLoad()
  assert.equal(f.select.value, '0')
  f.setOwners([])
  await f.functions.memOwnersLoad()
  assert.equal(f.select.value, '')
  assert.equal(f.elements.get('memMergeApply').disabled, true)
})

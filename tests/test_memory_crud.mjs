/** Execute the real panel functions against an isolated API fixture. */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'

const html = fs.readFileSync(new URL('../panel/panel.html', import.meta.url), 'utf8')
function fixture() {
  const requests = [], output = []
  let owner = null, refreshes = 0
  const elements = new Map([['memNew', { value: 'fixture fact' }]])
  const sandbox = { $: id => elements.get(id), show: (...args) => output.push(args),
    memSelectedOwner: () => owner, loadMemoryOverview: () => { refreshes++ },
    api: async (url, options) => { requests.push({ url, body: JSON.parse(options.body) }); return { ok: true, change_id: 'fixture-change' } } }
  function section(first, last) { return html.slice(html.indexOf(first), html.indexOf(last, html.indexOf(first))) }
  const source = section('function memoryEntrySelection(', '// 记忆搜索/筛选：') + '\n'
    + section('async function addMemory()', '// 服务端检索：')
  vm.runInNewContext(source + '\nthis.actions = {saveMemoryEdit,addMemory,deleteMemory};', sandbox)
  const entry = { id: 'fixture-id', version: 3, track: 'agent', scope: 'workspace', workspace_key: 'fixture-workspace', agent_key: '' }
  return { actions: sandbox.actions, entry, requests, output, elements, setOwner(value) { owner = value }, get refreshes() { return refreshes } }
}
test('edit and delete carry the original full ownership and expected version', async () => {
  const f = fixture()
  await f.actions.saveMemoryEdit(f.entry, 'corrected fact')
  await f.actions.deleteMemory(f.entry)
  assert.equal(f.requests.length, 2)
  for (const request of f.requests) {
    assert.equal(request.body.id, f.entry.id)
    assert.equal(request.body.expected_version, 3)
    assert.deepEqual(request.body.selection, { track: 'agent', scope: 'workspace', workspace_key: 'fixture-workspace', agent_key: '' })
  }
  assert.equal(f.refreshes, 2)
  assert.ok(f.output.at(-1)[1].includes('fixture-change'))
})
test('missing ownership/version cannot mutate a memory by bare id', async () => {
  const f = fixture()
  await f.actions.saveMemoryEdit({ id: 'fixture-id' }, 'text')
  await f.actions.deleteMemory({ ...f.entry, version: undefined })
  assert.equal(f.requests.length, 0)
  assert.equal(f.refreshes, 0)
  assert.ok(f.output.every(item => item[1].includes('范围或版本')))
})
test('add refuses without an explicit partition and preserves shared empty keys when selected', async () => {
  const f = fixture()
  await f.actions.addMemory()
  assert.equal(f.requests.length, 0)
  f.setOwner({ track: 'user', scope: 'user-global', workspace_key: '', agent_key: '' })
  await f.actions.addMemory()
  assert.equal(f.requests.length, 1)
  assert.deepEqual(f.requests[0].body.selection, { track: 'user', scope: 'user-global', workspace_key: '', agent_key: '' })
  assert.equal(f.elements.get('memNew').value, '')
})

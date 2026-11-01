/** Synthetic state only; no host reload, process control, model or QQ calls. */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { decorateAutonomyComponents } from '../plugin/lib/index.js'

const autonomy = { mode: 'unified', status: 'waiting', reason: 'waiting_for_opportunity',
  can_request_check: true }
const component = (tag, count = 0) => ({ tag, count, mb: count * 10, aging: 0, desc: 'synthetic' })

test('current component view omits inactive independent loops while retaining the main entry and services', () => {
  const components = ['mind', 'lifeloop', 'voicedaemon', 'supervisor', 'tick', 'groupwatch', 'works'].map(tag => component(tag))
  const before = structuredClone(components)
  const state = decorateAutonomyComponents({ components }, autonomy)
  assert.deepEqual(state.components.map(row => row.tag), ['mind', 'lifeloop', 'groupwatch', 'works'])
  assert.equal(state.components[0].control, 'request_check')
  assert.equal(state.components[1].control, 'request_check')
  assert.deepEqual(components, before)
})

test('real independent processes and unavailable counts remain visible without launch controls', () => {
  const components = [component('voicedaemon', 1), component('supervisor', 2),
    { tag: 'tick', mb: null, count: null, desc: 'synthetic' }]
  const state = decorateAutonomyComponents({ components }, autonomy)
  assert.equal(state.components.length, 3)
  assert.deepEqual(state.components.map(row => row.count), [1, 2, null])
  assert.deepEqual(state.components.map(row => row.mb), [10, 20, null])
  assert.ok(state.components.every(row => row.control === 'retired'))
  assert.match(state.components[0].desc, /仍在运行/)
  assert.match(state.components[2].desc, /待确认/)
})

test('an unconfirmed scheduler cannot suppress process evidence and explicit independent mode is preserved', () => {
  const components = [component('tick'), component('supervisor')]
  const unknown = decorateAutonomyComponents({ components }, { mode: 'unknown', status: 'unknown' })
  assert.equal(unknown.components.length, 2)
  const explicit = decorateAutonomyComponents({ components }, { mode: 'legacy', status: 'legacy' })
  assert.deepEqual(explicit.components, components)
})

test('embedded console renders current architecture and any actual residual process', () => {
  const source = fs.readFileSync(new URL('../plugin/lib/index.js', import.meta.url), 'utf8')
  const rows = [], nodes = new Map()
  const context = vm.createContext({ document: {
    getElementById(id) { if (!nodes.has(id)) nodes.set(id, { innerHTML: '', appendChild(row) { rows.push(row.innerHTML) } }); return nodes.get(id) },
    createElement: () => ({ innerHTML: '' })
  } })
  vm.runInContext(source.slice(source.indexOf('const $ = (id) => document.getElementById(id);'),
    source.indexOf('async function refresh(){')), context)
  context.s = decorateAutonomyComponents({ components: [component('mind'), component('tick'), component('supervisor', 1)] }, autonomy)
  vm.runInContext(source.slice(source.indexOf("const tb = $('parts');"),
    source.indexOf("tb.querySelectorAll('button').forEach")), context)
  assert.equal(rows.length, 2)
  assert.match(rows[0], /mind.*主 agent 托管/s)
  assert.match(rows[1], /supervisor.*独立进程仍在运行/s)
  assert.doesNotMatch(rows.join(''), /旧|>tick<|data-t="supervisor"/)
})

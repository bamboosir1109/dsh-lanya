import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createObservationPump, observationKey } from '../plugin/lib/observation-queue.js'

const settled = (id, extra = {}) => ({ completed: true, status: 'decided', decision_id: 'decision-' + id, ...extra })
const gate = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
async function waitFor(check) {
  const deadline = Date.now() + 5000
  while (!check()) {
    if (Date.now() >= deadline) assert.fail('bounded fixture wait expired')
    await new Promise(resolve => setTimeout(resolve, 5))
  }
}
function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-scope-lanes-'))
  const pumps = [], tasks = [], gates = []
  t.after(async () => {
    for (const pump of pumps) pump.stop()
    for (const held of gates) held.resolve(settled('cleanup'))
    await Promise.allSettled(tasks)
    // Poll-driven drains may still finish their bounded terminal writes.
    await new Promise(resolve => setImmediate(resolve))
    assert.ok(path.resolve(root).startsWith(path.join(os.tmpdir(), 'lanya-scope-lanes-')))
    fs.rmSync(root, { recursive: true, force: true })
  })
  const write = (id, scope, created = 1, extra = {}) => {
    const event = { version: 1, event_id: id, scope, kind: 'activity_due', producer: 'fixture',
      created_at: created, payload: {}, workspace_key: 'fixture-workspace', agent_key: '',
      origin: 'observation', control_allowed: false, ...extra }
    fs.mkdirSync(path.join(root, 'pending'), { recursive: true })
    const name = observationKey(scope, id) + '.json'
    fs.writeFileSync(path.join(root, 'pending', name), JSON.stringify(event) + '\n', 'utf8')
    return name
  }
  return {
    root, write, done: name => fs.existsSync(path.join(root, 'done', name)),
    held() { const held = gate(); gates.push(held); return held },
    make(dispatch, options = {}) {
      const pump = createObservationPump({ directory: root, dispatch, ...options })
      pumps.push(pump); return pump
    },
    drain(pump) { const task = pump.drain(); tasks.push(task); return task },
  }
}

test('poll admits a later different scope while slow scope and its successor remain serial', async t => {
  const f = fixture(t), hold = f.held(), calls = []
  const first = f.write('slow-a', 'private:101')
  const p = f.make(async event => {
    calls.push(event.event_id)
    if (event.event_id === 'slow-a') return hold.promise
    return settled(event.event_id)
  }, { intervalMs: 10 })
  assert.equal(await p.start(), true)
  await waitFor(() => calls.length === 1)
  f.write('a-successor', 'private:101', 2)
  const later = f.write('later-b', 'group:202', 3)
  await waitFor(() => f.done(later))
  assert.deepEqual(calls, ['slow-a', 'later-b'])
  assert.equal(f.done(first), false)
  hold.resolve(settled('slow-a'))
  await waitFor(() => calls.includes('a-successor'))
  assert.deepEqual(calls, ['slow-a', 'later-b', 'a-successor'])
})

test('cross-scope work is bounded to four while freed slots admit a fifth', async t => {
  const f = fixture(t), calls = [], holds = new Map()
  let active = 0, maximum = 0
  for (let index = 0; index < 8; index += 1) {
    const id = 'scope-' + index
    f.write(id, 'private:' + (101 + index), index + 1)
    holds.set(id, f.held())
  }
  const p = f.make(async event => {
    calls.push(event.event_id); active += 1; maximum = Math.max(maximum, active)
    try { return await holds.get(event.event_id).promise } finally { active -= 1 }
  })
  await p.start({ poll: false })
  const running = f.drain(p)
  await waitFor(() => calls.length === 4)
  await new Promise(resolve => setTimeout(resolve, 15))
  assert.equal(calls.length, 4)
  holds.get('scope-0').resolve(settled('scope-0'))
  await waitFor(() => calls.length === 5)
  assert.equal(maximum, 4)
  for (const [id, held] of holds) held.resolve(settled(id))
  assert.equal((await running).processed, 8)
  assert.equal(maximum, 4)
})

test('a retry head blocks only its own scope and preserves FIFO across later scans', async t => {
  const f = fixture(t), calls = []
  let clock = 100, firstAttempt = true
  f.write('a-head', 'private:101', 1)
  f.write('a-tail', 'private:101', 2)
  f.write('b-independent', 'group:202', 3)
  const p = f.make(async event => {
    calls.push(event.event_id)
    if (event.event_id === 'a-head' && firstAttempt) { firstAttempt = false; throw new Error('fixture-unavailable') }
    return settled(event.event_id)
  }, { now: () => clock })
  await p.start({ poll: false })
  await f.drain(p)
  assert.deepEqual(calls, ['a-head', 'b-independent'])
  await f.drain(p)
  assert.deepEqual(calls, ['a-head', 'b-independent'])
  clock = 2101
  await f.drain(p)
  assert.deepEqual(calls, ['a-head', 'b-independent', 'a-head', 'a-tail'])
})

test('a newly completed scope schedules its due review while another scope remains in flight', async t => {
  const f = fixture(t), hold = f.held(), calls = []
  let clock = 100
  f.write('slow-a', 'private:101')
  const parent = f.write('review-b', 'group:202', 2, { kind: 'activity.opportunity', producer: 'mind' })
  const p = f.make(async event => {
    calls.push({ id: event.event_id, producer: event.producer })
    if (event.event_id === 'slow-a') return hold.promise
    return settled(event.event_id, event.producer === 'mind' ? { next_review_at: 200 } : {})
  }, { now: () => clock, intervalMs: 10 })
  await p.start()
  await waitFor(() => f.done(parent))
  assert.equal(calls.length, 2)
  clock = 201
  await waitFor(() => calls.some(call => call.producer === 'main-review'))
  assert.equal(calls.filter(call => call.producer === 'main-review').length, 1)
  assert.equal(calls.filter(call => call.id === 'slow-a').length, 1)
  assert.equal(f.done(observationKey('private:101', 'slow-a') + '.json'), false)
  await new Promise(resolve => setTimeout(resolve, 30))
  assert.equal(calls.filter(call => call.producer === 'main-review').length, 1)
  hold.resolve(settled('slow-a'))
})

test('stop holds the sole lease until every parallel dispatch settles; unknown never replays', async t => {
  const f = fixture(t), calls = [], holds = new Map()
  for (let index = 0; index < 3; index += 1) {
    const id = 'held-' + index
    f.write(id, 'private:' + (101 + index), index + 1); holds.set(id, f.held())
  }
  const first = f.make(event => { calls.push(event.event_id); return holds.get(event.event_id).promise })
  await first.start({ poll: false })
  const running = f.drain(first)
  await waitFor(() => calls.length === 3)
  first.stop()
  const secondCalls = []
  const second = f.make(async event => { secondCalls.push(event.event_id); return settled(event.event_id) })
  assert.equal(await second.start({ poll: false }), false)
  holds.get('held-0').resolve({ completed: true, status: 'unknown', decision_id: 'uncertain-fixture' })
  await waitFor(() => f.done(observationKey('private:101', 'held-0') + '.json'))
  assert.equal(await second.start({ poll: false }), false)
  holds.get('held-1').resolve(settled('held-1'))
  holds.get('held-2').resolve(settled('held-2'))
  await running
  f.write('later', 'group:202', 4)
  assert.equal(await second.start({ poll: false }), true)
  assert.equal((await f.drain(second)).processed, 1)
  assert.deepEqual(secondCalls, ['later'])
})

test('terminal acknowledgement failure waits for other lanes and never replays their deliveries', async t => {
  const f = fixture(t), holds = [f.held(), f.held(), f.held()], calls = []
  let acknowledgements = 0
  for (let index = 0; index < 5; index += 1) f.write('item-' + index, 'private:' + (101 + index), index + 1)
  const first = f.make(async event => {
    calls.push(event.event_id)
    if (['item-1', 'item-2', 'item-3'].includes(event.event_id)) return holds[Number(event.event_id.slice(-1)) - 1].promise
    return settled(event.event_id)
  }, { acknowledge: async () => { acknowledgements += 1; if (acknowledgements === 1) throw new Error('fixture-ack-failed') } })
  await first.start({ poll: false })
  const running = f.drain(first)
  const rejected = assert.rejects(running, /fixture-ack-failed/)
  await waitFor(() => acknowledgements === 1)
  first.stop()
  const secondCalls = []
  const second = f.make(async event => { secondCalls.push(event.event_id); return settled(event.event_id) })
  assert.equal(await second.start({ poll: false }), false)
  assert.deepEqual(calls, ['item-0', 'item-1', 'item-2', 'item-3'])
  holds.forEach((held, index) => held.resolve(settled('item-' + (index + 1))))
  await rejected
  assert.equal(await second.start({ poll: false }), true)
  assert.equal((await f.drain(second)).processed, 1)
  assert.deepEqual(secondCalls, ['item-4'])
})


test('permanently parked terminal claims never replay or freeze later executable scope work', async t => {
  const f = fixture(t), calls = []
  const parked = f.write('parked-head', 'private:101', 1)
  f.write('later-in-scope', 'private:101', 2)
  const first = f.make(async event => {
    calls.push(event.event_id)
    if (event.event_id === 'parked-head') return { completed: true, status: 'sent', decision_id: '' }
    return settled(event.event_id)
  })
  await first.start({ poll: false })
  assert.equal((await f.drain(first)).processed, 1)
  assert.deepEqual(calls, ['parked-head', 'later-in-scope'])
  assert.equal(f.done(parked), false)
  assert.equal(fs.existsSync(path.join(f.root, 'pending', parked)), true)
  first.stop()
  f.write('after-restart', 'private:101', 3)
  const second = f.make(async event => { calls.push(event.event_id); return settled(event.event_id) })
  await second.start({ poll: false })
  assert.equal((await f.drain(second)).processed, 1)
  assert.deepEqual(calls, ['parked-head', 'later-in-scope', 'after-restart'])
  assert.equal(f.done(parked), false)
})

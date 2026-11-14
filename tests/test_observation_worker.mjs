import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import net from 'node:net'
import { once } from 'node:events'
import { createHash } from 'node:crypto'
import { pathToFileURL } from 'node:url'
import { createObservationPump, observationKey } from '../plugin/lib/observation-queue.js'

function fixture(t) {
  const prefix = path.join(os.tmpdir(), 'lanya-worker-'), root = fs.mkdtempSync(prefix), pumps = []
  fs.mkdirSync(path.join(root, 'pending')); fs.mkdirSync(path.join(root, 'done'))
  t.after(async () => {
    await Promise.all(pumps.map(pump => pump.stop()))
    assert.ok(path.resolve(root).startsWith(path.resolve(prefix)))
    fs.rmSync(root, { recursive: true, force: true })
  })
  return { root,
    make(options = {}, factory = createObservationPump) {
      const pump = factory({ directory: root, dispatch: event => ({ completed: true, status: 'decided', decision_id: 'decision-' + event.event_id }), ...options })
      pumps.push(pump); return pump
    },
    seed(id, terminal = true, extra = {}) {
      const event = { version: 1, event_id: id, scope: 'group:202', kind: 'activity_due', producer: 'fixture',
        created_at: 1, payload: {}, workspace_key: 'fixture-workspace', agent_key: 'fixture-agent',
        origin: 'observation', control_allowed: false, ...extra }
      const name = observationKey(event.scope, event.event_id) + '.json', raw = JSON.stringify(event) + '\n'
      fs.writeFileSync(path.join(root, 'pending', name), raw)
      const done = path.join(root, 'done', name)
      const marker = { version: 2, event_raw: raw, event_id: id, scope: event.scope,
        event_sha256: createHash('sha256').update(raw).digest('hex'), completed_at: 10, status: 'decided', decision_id: 'decision-' + id }
      if (terminal) fs.writeFileSync(done, JSON.stringify(marker) + '\n')
      return { name, done, marker }
    },
    async crashFactory(method) {
      const original = fs.readFileSync(new URL('../plugin/lib/observation-queue.js', import.meta.url), 'utf8')
      const seam = "const value = files[message.method](message.args)"
      assert.equal(original.split(seam).length, 2)
      const altered = original.replace(seam, `if (message.method === '${method}' && fs.existsSync(path.join(workerData.directory, 'crash-worker'))) process.exit(23)\n      ${seam}`)
      const module = path.join(root, 'scanner-fixture.mjs')
      fs.writeFileSync(module, altered)
      return (await import(pathToFileURL(module))).createObservationPump
    },
  }
}

test('production worker owns history IO while timers and loopback input remain responsive', async t => {
  const f = fixture(t), count = 64
  for (let i = 0; i < count; i++) f.seed('history-' + i)
  let acknowledged = 0, timerAt, inputAt
  const p = f.make({ acknowledge: () => { acknowledged++; return { acknowledged: true } } })
  await p.start({ poll: false })
  assert.equal(p.status().mode, 'worker'); assert.ok(p.status().thread_id > 0)
  const server = net.createServer(socket => { socket.on('data', () => { inputAt = acknowledged }); socket.on('end', () => socket.end()) })
  server.listen(0, '127.0.0.1'); await once(server, 'listening')
  const client = net.createConnection(server.address().port, '127.0.0.1'); await once(client, 'connect')
  const original = Object.fromEntries(['readFileSync', 'readdirSync', 'lstatSync'].map(key => [key, fs[key]]))
  for (const key of Object.keys(original)) fs[key] = function (target, ...args) {
    if (typeof target === 'string' && (target.startsWith(path.join(f.root, 'pending')) || target.startsWith(path.join(f.root, 'done')))) {
      throw new Error('History filesystem work leaked onto parent event loop')
    }
    return original[key].call(this, target, ...args)
  }
  try {
    const timer = new Promise(resolve => setTimeout(() => { timerAt = acknowledged; resolve() }, 0))
    client.write('synthetic inbound QQ-like message')
    await p.drain(); await timer
    assert.equal(acknowledged, count)
    assert.ok(timerAt < count); assert.ok(inputAt < count)
  } finally {
    for (const [key, fn] of Object.entries(original)) fs[key] = fn
    client.end(); await once(client, 'close'); await new Promise(resolve => server.close(resolve))
  }
})

test('incremental discovery skips inert terminals; explicit and periodic audits validate again', async t => {
  const f = fixture(t); let clock = 100
  for (let i = 0; i < 24; i++) f.seed('terminal-' + i)
  const p = f.make({ now: () => clock }); await p.start({ poll: false }); await p.drain()
  assert.equal(p.status().reads, 24, 'terminal records with no review need one verified read per audit')
  // Flush native watch notifications before recording the stable frontier.
  await new Promise(resolve => setTimeout(resolve, 30)); await p.drain(false)
  const reads = p.status().reads
  await p.drain(false)
  assert.equal(p.status().selected, 0); assert.equal(p.status().reads, reads); assert.equal(p.status().skipped, 24)
  clock += 30_001; await p.drain(false)
  assert.equal(p.status().full, true); assert.equal(p.status().selected, 24)
  await p.drain(); assert.equal(p.status().selected, 24)
})

test('a future review revalidates its completion when due even if it changed during acknowledgement', async t => {
  const f = fixture(t), calls = []
  const parent = f.seed('future-parent', true, { kind: 'activity.opportunity', producer: 'mind' })
  const original = { ...parent.marker, next_review_at: 200 }
  fs.writeFileSync(parent.done, JSON.stringify(original))
  let clock = 100, changed = false
  const p = f.make({ now: () => clock,
    acknowledge: event => {
      if (changed || event.event_id !== 'future-parent') return
      changed = true
      fs.writeFileSync(parent.done, JSON.stringify({ ...original, status: 'unknown' }))
    },
    dispatch: event => { calls.push(event.event_id); return { completed: true, status: 'decided', decision_id: 'fixture' } },
  })
  await p.start({ poll: false }); await p.drain()
  assert.deepEqual(calls, [])
  clock = 201; await p.drain(false)
  assert.deepEqual(calls, [], 'a changed completion cannot create a review from the prior snapshot')
  fs.writeFileSync(parent.done, JSON.stringify(original))
  await p.drain()
  assert.equal(calls.length, 1)
  assert.match(calls[0], /^main-review:/)
})

test('a long full audit leaves time for incremental discovery before another full audit', async t => {
  const f = fixture(t)
  f.seed('terminal-before-audit')
  let clock = 100
  const p = f.make({ now: () => clock, acknowledge: () => { clock += 45_000 } })
  await p.start({ poll: false })
  await p.drain()
  const reads = p.status().reads
  await p.drain(false)
  assert.equal(p.status().full, false)
  assert.equal(p.status().reads, reads)
  f.seed('live-after-audit', false)
  assert.equal((await p.drain(false)).processed, 1)
  assert.equal(p.status().selected, 1)
  clock += 30_001
  await p.drain(false)
  assert.equal(p.status().full, false)
  clock += 150_000
  await p.drain(false)
  assert.equal(p.status().full, true)
})

test('an invalid derived review is revisited by full audit without recurring incremental reads', async t => {
  const f = fixture(t)
  const record = f.seed('main-review:' + 'a'.repeat(64) + ':1', true,
    { producer: 'main-review', kind: 'activity.opportunity' })
  fs.writeFileSync(record.done, JSON.stringify({ ...record.marker, next_review_at: 20 }) + '\n')
  const p = f.make({ now: () => 100, dispatch: () => assert.fail('terminal must never dispatch') })
  await p.start({ poll: false })
  await p.drain()
  assert.equal(p.status().cached, 1)
  await p.drain(false)
  assert.equal(p.status().selected, 0)
  await p.drain()
  assert.equal(p.status().selected, 1)
})

test('legacy completion without its original stays reported but waits for a change or audit', async t => {
  const f = fixture(t), errors = [], acknowledged = []
  const legacy = f.seed('missing-legacy-original')
  const original = path.join(f.root, 'pending', legacy.name)
  const { event_raw, ...marker } = legacy.marker
  fs.unlinkSync(original)
  fs.writeFileSync(legacy.done, JSON.stringify({ ...marker, version: 1 }) + '\n')
  const p = f.make({
    now: () => 100,
    log: (level, message) => { if (level === 'error') errors.push(message) },
    dispatch: () => assert.fail('legacy completion cannot dispatch'),
    acknowledge: event => { acknowledged.push(event.event_id) },
  })
  await p.start({ poll: false })
  await p.drain()
  assert.equal(errors.length, 1)
  assert.deepEqual(acknowledged, [])
  const reads = p.status().reads
  await p.drain(false)
  assert.equal(p.status().selected, 0)
  assert.equal(p.status().reads, reads)
  fs.writeFileSync(original, event_raw)
  await p.drain(false)
  assert.deepEqual(acknowledged, ['missing-legacy-original'])
})

test('cold recovery confirms terminal records in bounded batches without replay', async t => {
  const f = fixture(t)
  const sizes = [], handled = [], acknowledged = []
  for (let i = 0; i < 128; i++) f.seed('historical-' + i)
  f.seed('fresh', false)
  const p = f.make({
    acknowledge: event => { acknowledged.push(event.event_id) },
    acknowledgeBatch: records => {
      sizes.push(records.length)
      return records.map(({ event, marker }) => {
        assert.equal(event.event_id, marker.event_id)
        acknowledged.push(event.event_id)
        return { acknowledged: false, reason: 'unknown_observation' }
      })
    },
    dispatch: event => {
      handled.push(event.event_id)
      return { completed: true, status: 'decided', decision_id: 'fresh-decision' }
    },
  })
  await p.start({ poll: false })
  assert.equal((await p.drain()).processed, 1)
  assert.ok(sizes.length > 0 && sizes.every(size => size >= 2 && size <= 64))
  assert.deepEqual(acknowledged.slice().sort(), ['fresh', ...Array.from({ length: 128 }, (_, i) => 'historical-' + i)].sort())
  assert.deepEqual(handled, ['fresh'])
  await p.drain()
  assert.equal(acknowledged.length, 129)
  assert.deepEqual(handled, ['fresh'])
})

test('large terminal originals flush before the maximum batch count', async t => {
  const f = fixture(t), sizes = []
  for (let i = 0; i < 40; i++) f.seed('large-terminal-' + i, true, { payload: { text: 'x'.repeat(128 * 1024) } })
  const p = f.make({
    acknowledge: () => assert.fail('large terminal batches should remain valid'),
    acknowledgeBatch: records => {
      sizes.push(records.length)
      return records.map(() => ({ acknowledged: true }))
    },
    dispatch: () => assert.fail('terminal records cannot dispatch'),
  })
  await p.start({ poll: false })
  assert.equal((await p.drain()).processed, 0)
  assert.deepEqual(sizes, [32, 8])
})

test('cached terminal loss cannot replay; restored evidence is revalidated', async t => {
  const f = fixture(t), value = f.seed('terminal'), errors = []; let calls = 0
  const p = f.make({ dispatch: () => { calls++; throw new Error('no replay') }, log: (_level, text) => errors.push(text) })
  await p.start({ poll: false }); await p.drain()
  fs.unlinkSync(value.done); await p.drain(false)
  assert.equal(calls, 0); assert.ok(errors.some(text => text.startsWith('observation_invalid:')))
  fs.writeFileSync(value.done, JSON.stringify(value.marker) + '\n'); await p.drain()
  assert.equal(calls, 0)
})

for (const method of ['readBatch', 'complete']) test(`worker crash during ${method} recovers without restarting the parent or repeating a settled turn`, async t => {
  const f = fixture(t), factory = await f.crashFactory(method), value = f.seed('recover', false)
  let calls = 0
  const p = f.make({ dispatch: event => { calls++; return { completed: true, status: 'unknown', decision_id: 'decision-' + event.event_id } } }, factory)
  await p.start({ poll: false }); fs.writeFileSync(path.join(f.root, 'crash-worker'), 'fixture')
  await assert.rejects(p.drain(), /Scanner worker exited/)
  assert.equal(calls, method === 'complete' ? 1 : 0)
  fs.unlinkSync(path.join(f.root, 'crash-worker'))
  await p.drain()
  assert.equal(calls, 1); assert.equal(p.status().starts, 2)
  assert.equal(JSON.parse(fs.readFileSync(value.done)).status, 'unknown')
  await p.drain(); assert.equal(calls, 1)
})

test('stop during startup settles and a new consumer can subsequently acquire ownership', async t => {
  const f = fixture(t), p = f.make()
  const starting = p.start({ poll: false }), stopped = p.stop()
  assert.equal(await starting, false); await stopped
  const next = f.make(); assert.equal(await next.start({ poll: false }), true)
})

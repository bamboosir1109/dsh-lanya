import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import net from 'node:net'
import { once } from 'node:events'
import { createHash } from 'node:crypto'
import { createInlineObservationPump as createObservationPump, observationKey } from '../plugin/lib/observation-queue.js'

function fixture(t) {
  const prefix = path.join(os.tmpdir(), 'lanya-pump-fairness-')
  const directory = fs.mkdtempSync(prefix)
  const pumps = []
  fs.mkdirSync(path.join(directory, 'pending'))
  fs.mkdirSync(path.join(directory, 'done'))
  t.after(() => {
    for (const pump of pumps) pump.stop()
    assert.ok(path.resolve(directory).startsWith(path.resolve(prefix)))
    fs.rmSync(directory, { recursive: true, force: true })
  })
  return {
    directory,
    async start(options = {}) {
      const pump = createObservationPump({ directory, dispatch: () => assert.fail('terminal evidence must not dispatch'), ...options })
      pumps.push(pump)
      assert.equal(await pump.start({ poll: false }), true)
      return pump
    },
    seed(id, { terminal = true, status = 'decided' } = {}) {
      const event = { version: 1, event_id: id, scope: 'group:200', kind: 'activity_due', producer: 'fixture',
        created_at: 1, payload: {}, workspace_key: 'fixture-workspace', agent_key: 'fixture-agent',
        origin: 'observation', control_allowed: false }
      const name = observationKey(event.scope, event.event_id) + '.json'
      const raw = JSON.stringify(event) + '\n'
      fs.writeFileSync(path.join(directory, 'pending', name), raw)
      const done = path.join(directory, 'done', name)
      const marker = { version: 2, event_raw: raw, event_id: event.event_id, scope: event.scope,
        event_sha256: createHash('sha256').update(raw).digest('hex'), completed_at: 1000, status, decision_id: 'fixture-decision-' + id }
      if (terminal) fs.writeFileSync(done, JSON.stringify(marker) + '\n')
      return { event, name, done, marker }
    },
  }
}

test('only exact successfully acknowledged terminal bytes are cached; changes remain validated', async t => {
  const f = fixture(t)
  const value = f.seed('identity')
  let acknowledgements = 0, invalid = 0
  const pump = await f.start({ acknowledge: () => { acknowledgements++; return { acknowledged: true } },
    log: (_level, message) => { if (message.startsWith('observation_invalid:')) invalid++ } })
  await pump.drain(); await pump.drain()
  assert.equal(acknowledgements, 1)
  // Even semantically equal JSON has different physical bytes and must be rechecked.
  const changed = JSON.stringify(value.marker, null, 2) + '\n'
  fs.writeFileSync(value.done, changed)
  await pump.drain(); await pump.drain()
  assert.equal(acknowledgements, 2)
  fs.writeFileSync(value.done, JSON.stringify({ ...value.marker, decision_id: '' }))
  await pump.drain()
  assert.equal(invalid, 1)
  assert.equal(acknowledgements, 2)
  fs.writeFileSync(value.done, changed)
  await pump.drain()
  assert.equal(acknowledgements, 2)
})

test('rejections and decision mismatches retry; unknown_observation is an idempotent completed ack', async t => {
  const f = fixture(t)
  f.seed('retry')
  let calls = 0
  const pump = await f.start({ acknowledge: () => {
    calls++
    if (calls === 1) throw new Error('fixture temporary policy failure')
    if (calls === 2) return { acknowledged: false, reason: 'decision_mismatch' }
    return { acknowledged: false, reason: 'unknown_observation' }
  } })
  await pump.drain(); await pump.drain(); await pump.drain(); await pump.drain()
  assert.equal(calls, 3)
})

test('ack caching belongs to one pump; restart recovers unknown terminal evidence without dispatch', async t => {
  const f = fixture(t)
  const value = f.seed('unknown-terminal', { status: 'unknown' })
  const original = fs.readFileSync(value.done)
  let calls = 0
  const acknowledge = () => { calls++ }
  const first = await f.start({ acknowledge })
  await first.drain(); await first.drain(); first.stop()
  assert.equal(calls, 1)
  const restarted = await f.start({ acknowledge })
  await restarted.drain(); await restarted.drain()
  assert.equal(calls, 2)
  assert.deepEqual(fs.readFileSync(value.done), original)
})

test('a large terminal history lets a real timer and loopback input run before acknowledgement finishes', async t => {
  const f = fixture(t), count = 128
  for (let index = 0; index < count; index++) f.seed('history-' + index)
  let acknowledged = 0, timerAt, inputAt
  const pump = await f.start({ acknowledge: () => { acknowledged++; return { acknowledged: false, reason: 'unknown_observation' } } })
  let inputResolve
  const input = new Promise(resolve => { inputResolve = resolve })
  const sockets = new Set()
  const server = net.createServer(socket => {
    sockets.add(socket)
    socket.once('data', () => { inputAt = acknowledged; inputResolve() })
  })
  const client = net.createConnection
  let connection
  t.after(async () => {
    connection?.destroy()
    for (const socket of sockets) socket.destroy()
    await new Promise(resolve => server.close(resolve))
  })
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  connection = client({ port: server.address().port, host: '127.0.0.1' })
  await once(connection, 'connect')
  connection.write('fixture input')
  const timer = new Promise(resolve => setTimeout(() => { timerAt = acknowledged; resolve() }, 0))
  await pump.drain()
  await Promise.all([timer, input])
  assert.ok(timerAt < count, 'timer must run before a complete historical scan')
  assert.ok(inputAt < count, 'real input must run before a complete historical scan')
  assert.equal(acknowledged, count)
  await pump.drain()
  assert.equal(acknowledged, count, 'unchanged history must not repeat policy transactions')
})

test('immediately settled pending dispatches also yield to timers in bounded batches', async t => {
  const f = fixture(t), count = 64
  for (let index = 0; index < count; index++) f.seed('pending-' + index, { terminal: false })
  let calls = 0, timerAt, finishTimer
  const timer = new Promise(resolve => { finishTimer = resolve })
  const pump = await f.start({ dispatch: async () => {
    if (++calls === 1) setTimeout(() => { timerAt = calls; finishTimer() }, 0)
    return { completed: true, status: 'decided', decision_id: 'fixture-' + calls }
  } })
  assert.equal((await pump.drain()).processed, count)
  await timer
  assert.ok(timerAt < count, 'terminal scan yielding alone must not hide dispatch-loop starvation')
})

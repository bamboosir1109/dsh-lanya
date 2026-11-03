import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { DatabaseSync } from 'node:sqlite'
import { fileURLToPath } from 'node:url'
import { createHash } from 'node:crypto'
import { createObservationPump, observationFreshness, observationKey, validateObservation } from '../plugin/lib/observation-queue.js'

const repository = fileURLToPath(new URL('../', import.meta.url))
const resources = new Map()
function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-observations-'))
  resources.set(root, [])
  t.after(() => {
    for (const p of resources.get(root)) p.stop()
    assert.ok(path.resolve(root).startsWith(path.join(os.tmpdir(), 'lanya-observations-')))
    fs.rmSync(root, { recursive: true, force: true })
    resources.delete(root)
  })
  return root
}
function event(id, extra = {}) {
  return { version: 1, event_id: id, scope: 'private:101', kind: 'activity_due', producer: 'fixture',
    created_at: 1, payload: { text: '观察不是用户命令' }, workspace_key: 'workspace-a', agent_key: '',
    origin: 'observation', control_allowed: false, ...extra }
}
function write(root, value, filename) {
  fs.mkdirSync(path.join(root, 'pending'), { recursive: true })
  const name = filename || observationKey(value.scope, value.event_id) + '.json'
  fs.writeFileSync(path.join(root, 'pending', name), JSON.stringify(value) + '\n', 'utf8')
  return name
}
async function pump(t, root, dispatch, extras = {}) {
  const instance = createObservationPump({ directory: root, dispatch, ...extras })
  resources.get(root).push(instance)
  t.after(() => instance.stop())
  assert.equal(await instance.start({ poll: false }), true)
  return instance
}

test('one real decision leaves immutable evidence and a physical-hash completion marker', async (t) => {
  const root = fixture(t)
  const name = write(root, event('one'))
  const before = fs.readFileSync(path.join(root, 'pending', name))
  let calls = 0, acknowledgements = 0
  const p = await pump(t, root, async (value) => {
    calls += 1
    assert.equal(value.control_allowed, false)
    return { completed: true, status: 'decided', decision_id: 'idle-one' }
  }, { acknowledge: () => { acknowledgements += 1 } })
  assert.equal((await p.drain()).processed, 1)
  assert.deepEqual(fs.readFileSync(path.join(root, 'pending', name)), before)
  const done = JSON.parse(fs.readFileSync(path.join(root, 'done', name), 'utf8'))
  assert.equal(done.event_sha256, createHash('sha256').update(before).digest('hex'))
  await p.drain()
  assert.equal(calls, 1)
  assert.equal(acknowledgements, 1)
})

test('failed model dispatch is retained with durable backoff and can recover', async (t) => {
  const root = fixture(t)
  const name = write(root, event('retry'))
  let now = 100, calls = 0
  const dispatch = async () => {
    if (++calls === 1) throw new Error('offline model fixture')
    return { completed: true, status: 'suppressed', decision_id: 'idle-retry' }
  }
  const first = await pump(t, root, dispatch, { now: () => now })
  assert.equal((await first.drain()).processed, 0)
  assert.equal(fs.existsSync(path.join(root, 'done', name)), false)
  first.stop()
  const second = await pump(t, root, dispatch, { now: () => now })
  await second.drain()
  assert.equal(calls, 1)
  now += 2001
  assert.equal((await second.drain()).processed, 1)
  assert.equal(calls, 2)
})

test('unknown sends and restart completion never automatically replay a delivery', async (t) => {
  const root = fixture(t)
  write(root, event('uncertain'))
  let calls = 0
  const first = await pump(t, root, async () => { calls += 1; return { completed: true, status: 'unknown', decision_id: 'sent-maybe' } })
  await first.drain()
  first.stop()
  const second = await pump(t, root, async () => assert.fail('must not repeat unknown send'))
  await second.drain()
  assert.equal(calls, 1)
})

test('missing real verdict is pending and cannot be acknowledged as completed', async (t) => {
  const root = fixture(t)
  const name = write(root, event('unresolved'))
  const p = await pump(t, root, async () => ({ completed: false, status: 'sent' }))
  assert.equal((await p.drain()).processed, 0)
  assert.equal(fs.existsSync(path.join(root, 'done', name)), false)
})

test('two living consumers cannot both own or dispatch the inbox', async (t) => {
  const root = fixture(t)
  write(root, event('single-owner'))
  const first = await pump(t, root, async () => ({ completed: true, status: 'decided', decision_id: 'fixture-decision' }))
  const second = createObservationPump({ directory: root, dispatch: () => assert.fail('other consumer') })
  resources.get(root).push(second)
  t.after(() => second.stop())
  assert.equal(await second.start({ poll: false }), false)
  assert.equal((await first.drain()).processed, 1)
})

test('a confirmed dead owner is replaced atomically without deleting inbox evidence', async (t) => {
  const root = fixture(t)
  const first = await pump(t, root, async () => ({ completed: true, status: 'decided', decision_id: 'fixture-decision' }))
  first.stop()
  const db = new DatabaseSync(path.join(root, 'consumer.sqlite'))
  db.prepare('INSERT INTO consumer(id,pid,nonce) VALUES (1,?,?)').run(42, 'dead-fixture')
  db.close()
  const name = write(root, event('recovered'))
  const second = await pump(t, root, async () => ({ completed: true, status: 'decided', decision_id: 'fixture-decision' }), { isOwnerAlive: (pid) => pid === process.pid })
  assert.equal((await second.drain()).processed, 1)
  assert.equal(fs.existsSync(path.join(root, 'pending', name)), true)
})

test('stop retains the consumer lease until its current decision has settled', async (t) => {
  const root = fixture(t)
  write(root, event('inflight'))
  let finish
  const first = await pump(t, root, () => new Promise((resolve) => { finish = resolve }))
  const running = first.drain()
  first.stop()
  const second = createObservationPump({ directory: root, dispatch: () => assert.fail('must wait') })
  resources.get(root).push(second)
  t.after(() => second.stop())
  assert.equal(await second.start({ poll: false }), false)
  finish({ completed: true, status: 'decided', decision_id: 'fixture-decision' })
  await running
  assert.equal(await second.start({ poll: false }), true)
  assert.equal((await second.drain()).processed, 0)
})

test('completion before a failed core acknowledgement is redriven without a new model turn', async (t) => {
  const root = fixture(t)
  write(root, event('ack-recovery'))
  let ack = 0, model = 0
  const p = await pump(t, root, async () => { model += 1; return { completed: true, status: 'decided', decision_id: 'known' } },
    { acknowledge: () => { if (++ack === 1) throw new Error('state busy') } })
  await assert.rejects(() => p.drain(), /state busy/)
  await p.drain()
  assert.equal(model, 1)
  assert.equal(ack, 2)
  await p.drain()
  assert.equal(ack, 2)
})

test('invalid/control-bearing evidence and mismatched completion markers do not reach the model', async (t) => {
  const root = fixture(t)
  write(root, event('forged', { control_allowed: true }))
  write(root, event('path-mismatch'), '0'.repeat(64) + '.json')
  const name = write(root, event('bad-done'))
  fs.mkdirSync(path.join(root, 'done'), { recursive: true })
  fs.writeFileSync(path.join(root, 'done', name), JSON.stringify({ version: 1, event_sha256: 'wrong' }))
  const p = await pump(t, root, () => assert.fail('invalid evidence'))
  assert.equal((await p.drain()).processed, 0)
  for (const change of [{ origin: 'live' }, { control_allowed: true }, { agent_key: null }, { created_at: 0.5 }]) {
    assert.throws(() => validateObservation(event('invalid', change)))
  }
})

test('Python publishing and Node consumption agree on Chinese UTF8 evidence and completion', async (t) => {
  const root = fixture(t)
  const value = event('python-node')
  const script = path.join(repository, 'scripts', 'decision_events.py')
  const invoke = () => spawnSync('python', ['-B', script, '--directory', root], { input: JSON.stringify(value), encoding: 'utf8', windowsHide: true })
  const published = invoke()
  assert.equal(published.status, 0, published.stderr)
  const p = await pump(t, root, async (received) => {
    assert.deepEqual(received.payload, value.payload)
    return { completed: true, status: 'decided', decision_id: 'interop' }
  })
  assert.equal((await p.drain()).processed, 1)
  const duplicate = invoke()
  assert.equal(duplicate.status, 0, duplicate.stderr)
  assert.equal(JSON.parse(duplicate.stdout).state, 'done')
})

const readEvents = (root) => fs.readdirSync(path.join(root, 'pending')).filter((name) => /^[a-f0-9]{64}\.json$/.test(name))
  .map((name) => JSON.parse(fs.readFileSync(path.join(root, 'pending', name), 'utf8')))

test('an explicit due review inherits immutable scope/owner/kind and survives restart exactly once', async (t) => {
  const root = fixture(t)
  const original = event('meme-original', { kind: 'meme.review', agent_key: 'preset-a', payload: { candidate: 'fixture.png', review_context: { old: true } } })
  const name = write(root, original)
  const before = fs.readFileSync(path.join(root, 'pending', name))
  let now = 1000, calls = 0
  const dispatch = async (value) => {
    calls += 1
    return { completed: true, status: 'decided', decision_id: 'decision-' + calls,
      next_review_at: value.producer === 'main-review' ? null : 2000 }
  }
  const first = await pump(t, root, dispatch, { now: () => now })
  await first.drain()
  const doneBefore = fs.readFileSync(path.join(root, 'done', name))
  now = 1999
  await first.drain()
  assert.equal(calls, 1)
  assert.equal(readEvents(root).length, 1)
  first.stop()
  now = 2000
  const second = await pump(t, root, dispatch, { now: () => now })
  assert.equal((await second.drain()).processed, 1)
  const review = readEvents(root).find((value) => value.producer === 'main-review')
  assert.equal(review.event_id, 'main-review:' + observationKey(original.scope, original.event_id) + ':2000')
  assert.equal(review.created_at, 2000)
  for (const key of ['scope', 'workspace_key', 'agent_key', 'kind', 'origin', 'control_allowed']) assert.equal(review[key], original[key])
  assert.equal(review.payload.candidate, original.payload.candidate)
  assert.deepEqual(review.payload.review_context, { parent_observation_key: observationKey(original.scope, original.event_id),
    parent_event_id: original.event_id, previous_decision_id: 'decision-1', scheduled_at: 2000,
    freshness_anchor: { key: observationKey(original.scope, original.event_id),
      sha256: createHash('sha256').update(before).digest('hex'), created_at: original.created_at } })
  assert.deepEqual(fs.readFileSync(path.join(root, 'pending', name)), before)
  assert.deepEqual(fs.readFileSync(path.join(root, 'done', name)), doneBefore)
  await second.drain()
  second.stop()
  const third = await pump(t, root, async () => assert.fail('completed review replayed'), { now: () => now })
  await third.drain()
  assert.equal(calls, 2)
  assert.equal(readEvents(root).length, 2)
})

test('successive explicit review times replace a small context instead of nesting the prior payload', async (t) => {
  const root = fixture(t)
  write(root, event('social-original', { kind: 'social.notification', payload: { evidence: { text: 'fixture' } } }))
  let now = 1000, calls = 0
  const p = await pump(t, root, async () => ({ completed: true, status: 'decided', decision_id: 'decision-' + (++calls),
    next_review_at: calls === 1 ? 2000 : calls === 2 ? 3000 : null }), { now: () => now })
  await p.drain()
  now = 2000; await p.drain()
  now = 3000; await p.drain()
  const reviews = readEvents(root).filter((value) => value.producer === 'main-review').sort((a, b) => a.created_at - b.created_at)
  assert.equal(reviews.length, 2)
  assert.deepEqual(reviews[1].payload.evidence, { text: 'fixture' })
  assert.equal(reviews[1].payload.review_context.parent_event_id, reviews[0].event_id)
  assert.equal(reviews[1].payload.review_context.review_context, undefined)
  assert.deepEqual(Object.keys(reviews[1].payload).sort(), ['evidence', 'review_context'])
})

test('unknown outcomes and producer-owned kinds never get a generic review; idle without time never wakes', async (t) => {
  const root = fixture(t)
  for (const [id, kind] of [['unknown', 'meme.review'], ['mind', 'activity_due'], ['reminder', 'reminder.due'], ['idle', 'group.quiet']]) {
    write(root, event(id, { kind }))
  }
  let now = 1000, calls = 0
  const p = await pump(t, root, async (value) => {
    calls += 1
    return { completed: true, status: value.event_id === 'unknown' ? 'unknown' : 'decided',
      decision_id: 'decision-' + value.event_id, next_review_at: value.event_id === 'idle' ? null : 2000 }
  }, { now: () => now })
  await p.drain()
  now = 3000; await p.drain()
  assert.equal(calls, 4)
  assert.equal(readEvents(root).length, 4)
  assert.equal(readEvents(root).some((value) => value.producer === 'main-review'), false)
})

test('bad physical completion markers never acknowledge or call the model again', async (t) => {
  const invalid = [{ status: 'failed' }, { decision_id: '' }, { decision_id: '  ' }, { decision_id: null },
    { decision_id: 'bad\nidentity' }, { decision_id: 'x'.repeat(257) }, { next_review_at: null },
    { next_review_at: true }, { next_review_at: 'later' }, { next_review_at: -1 }, { next_review_at: 0.5 },
    { next_review_at: Number.MAX_SAFE_INTEGER + 1 }, { next_review_at: 1 },
    { completed_at: true }, { completed_at: 0.5 }, { completed_at: -1 }, { scope: 'group:202' }, { event_id: 'different' }]
  for (const change of invalid) {
    const root = fixture(t)
    const value = event('bad-marker', { kind: 'meme.review' })
    const name = write(root, value)
    fs.mkdirSync(path.join(root, 'done'), { recursive: true })
    const raw = fs.readFileSync(path.join(root, 'pending', name))
    const marker = { version: 1, event_sha256: createHash('sha256').update(raw).digest('hex'),
      completed_at: 1000, status: 'decided', decision_id: 'decision-fixture', ...change }
    fs.writeFileSync(path.join(root, 'done', name), JSON.stringify(marker))
    let now = 2000
    const p = await pump(t, root, () => assert.fail('invalid marker called model'), {
      now: () => now, acknowledge: () => assert.fail('invalid marker acknowledged'),
    })
    await p.drain()
    now += 60 * 60 * 1000
    await p.drain()
    assert.equal(readEvents(root).length, 1)
  }
})

test('a review dispatch failure obeys the same durable backoff even when the completed parent reschedules it', async (t) => {
  const root = fixture(t)
  write(root, event('review-retry', { kind: 'meme.review' }))
  let now = 1000, calls = 0, reviewCalls = 0
  const dispatch = async (value) => {
    calls += 1
    if (value.producer === 'main-review' && ++reviewCalls === 1) throw new Error('fixture model offline')
    return { completed: true, status: 'decided', decision_id: 'decision-' + calls,
      next_review_at: value.producer === 'main-review' ? null : 2000 }
  }
  const first = await pump(t, root, dispatch, { now: () => now })
  await first.drain()
  now = 2000; await first.drain()
  assert.equal(reviewCalls, 1)
  await first.drain()
  assert.equal(reviewCalls, 1)
  first.stop()
  const second = await pump(t, root, dispatch, { now: () => now })
  await second.drain()
  assert.equal(reviewCalls, 1)
  now += 2001; await second.drain()
  assert.equal(reviewCalls, 2)
  assert.equal(calls, 3)
})

test('an invalid claimed-terminal review is durably parked, not revived by its parent or a restart', async (t) => {
  const root = fixture(t)
  write(root, event('review-park', { kind: 'social.notification' }))
  let now = 1000, reviewCalls = 0, acknowledgements = 0
  const dispatch = async (value) => value.producer === 'main-review'
    ? (reviewCalls++, { completed: true, status: 'sent', decision_id: '' })
    : { completed: true, status: 'decided', decision_id: 'parent-decision', next_review_at: 2000 }
  const first = await pump(t, root, dispatch, { now: () => now, acknowledge: (value) => {
    if (value.producer === 'main-review') acknowledgements++
  } })
  await first.drain()
  now = 2000; await first.drain()
  assert.equal(reviewCalls, 1)
  await first.drain()
  first.stop()
  now += 60 * 60 * 1000
  const second = await pump(t, root, dispatch, { now: () => now })
  await second.drain()
  assert.equal(reviewCalls, 1)
  assert.equal(acknowledgements, 0)
  const review = readEvents(root).find((value) => value.producer === 'main-review')
  assert.equal(fs.existsSync(path.join(root, 'done', observationKey(review.scope, review.event_id) + '.json')), false)
})

// Pre-anchor histories remain real immutable evidence. Every existing child is
// terminal; only the leaf has a future review, so polling has no work to create.
function legacyHistory(t, count) {
  const root = fixture(t), rows = []
  fs.mkdirSync(path.join(root, 'done'), { recursive: true })
  for (let index = 0; index < count; index++) {
    const previous = rows.at(-1)
    const value = previous ? {
      ...previous.value,
      event_id: 'main-review:' + previous.key + ':' + (index + 1),
      producer: 'main-review', created_at: index + 1,
      payload: { subject: 'synthetic review history', review_context: {
        parent_observation_key: previous.key, parent_event_id: previous.value.event_id,
        previous_decision_id: previous.marker.decision_id, scheduled_at: index + 1,
      } },
    } : event('history-root', { kind: 'social.notification', payload: { subject: 'synthetic review history' } })
    const name = write(root, value), key = name.slice(0, -5)
    const raw = fs.readFileSync(path.join(root, 'pending', name), 'utf8')
    const marker = { version: 2, event_raw: raw, event_id: value.event_id, scope: value.scope,
      event_sha256: createHash('sha256').update(raw).digest('hex'), completed_at: 1000,
      status: 'decided', decision_id: 'history-decision-' + index,
      next_review_at: index + 1 < count ? index + 2 : 10000 }
    fs.writeFileSync(path.join(root, 'done', name), JSON.stringify(marker) + '\n')
    rows.push({ value, name, key, raw, marker })
  }
  return { root, rows }
}

async function countHistoryIo(root, run) {
  const read = fs.readFileSync, exists = fs.existsSync
  const counts = { read: 0, exists: 0 }
  const inside = (file) => typeof file === 'string' &&
    (path.resolve(file) === root || path.resolve(file).startsWith(root + path.sep))
  fs.readFileSync = function (file, ...args) {
    if (inside(file)) counts.read++
    return read.call(this, file, ...args)
  }
  fs.existsSync = function (file, ...args) {
    if (inside(file)) counts.exists++
    return exists.call(this, file, ...args)
  }
  try { await run(); return counts } finally { fs.readFileSync = read; fs.existsSync = exists }
}

test('polling terminal legacy review history uses linear file work across shared ancestry', async (t) => {
  const samples = []
  for (const count of [24, 48]) {
    const { root } = legacyHistory(t, count)
    const p = await pump(t, root, () => assert.fail('terminal history cannot dispatch'), { now: () => 1000 })
    const first = await countHistoryIo(root, async () => assert.equal((await p.drain()).processed, 0))
    const repeated = await countHistoryIo(root, async () => assert.equal((await p.drain()).processed, 0))
    for (const metric of ['read', 'exists']) {
      assert.ok(first[metric] > 0, 'the counter must observe real ' + metric + ' calls')
      assert.ok(repeated[metric] <= first[metric] + count, 'another poll must retain the same bound for ' + metric)
    }
    assert.ok(first.read <= 16 * count, 'terminal history must not reread every ancestor for every child: ' + JSON.stringify(first))
    samples.push(first)
  }
  t.diagnostic('terminal history filesystem calls for 24/48 nodes: ' + JSON.stringify(samples))
  for (const metric of ['read', 'exists']) {
    assert.ok(samples[1][metric] <= samples[0][metric] * 2.8 + 16,
      'doubling terminal history must not quadruple ' + metric + ' calls: ' + JSON.stringify(samples))
  }
})

test('a new review still revalidates changed ancestry after a terminal-only poll', async (t) => {
  const { root, rows } = legacyHistory(t, 8)
  let now = 1000, calls = 0
  const p = await pump(t, root, async (value) => {
    calls++
    assert.equal(value.payload.review_context.freshness_anchor.key, rows[0].key)
    return { completed: true, status: 'decided', decision_id: 'validated-after-repair' }
  }, { now: () => now })
  await p.drain()
  fs.writeFileSync(path.join(root, 'pending', rows[0].name), JSON.stringify({ ...rows[0].value, control_allowed: true }) + '\n')
  now = 10000
  assert.equal((await p.drain()).processed, 0)
  assert.equal(calls, 0)
  assert.equal(readEvents(root).length, rows.length)
  fs.writeFileSync(path.join(root, 'pending', rows[0].name), rows[0].raw)
  assert.equal((await p.drain()).processed, 1)
  assert.equal(calls, 1)
  assert.equal(readEvents(root).length, rows.length + 1)
})

test('a changed unknown ancestor cannot authorize a fresh review or replay a terminal child', async (t) => {
  const { root, rows } = legacyHistory(t, 8)
  let now = 1000
  const p = await pump(t, root, () => assert.fail('unknown history must not replay'), { now: () => now })
  await p.drain()
  const changed = rows[3]
  const raw = JSON.stringify({ ...changed.marker, status: 'unknown' }) + '\n'
  fs.writeFileSync(path.join(root, 'done', changed.name), raw)
  now = 10000
  assert.equal((await p.drain()).processed, 0)
  assert.equal(readEvents(root).length, rows.length)
  assert.equal(fs.readFileSync(path.join(root, 'done', changed.name), 'utf8'), raw)
})

test('terminal child shortcuts still reject conflicting contents and malformed completion evidence', async (t) => {
  const { root, rows } = legacyHistory(t, 4), invalid = []
  const p = await pump(t, root, () => assert.fail('conflicting terminal evidence must not dispatch'), {
    now: () => 1000, log: (level, message) => { if (level === 'error') invalid.push(message) },
  })
  await p.drain()
  assert.deepEqual(invalid, [])
  const child = rows[1]
  const raw = JSON.stringify({ ...child.value, payload: { ...child.value.payload, subject: 'conflicting immutable payload' } }) + '\n'
  fs.writeFileSync(path.join(root, 'pending', child.name), raw)
  fs.writeFileSync(path.join(root, 'done', child.name), JSON.stringify({ ...child.marker, event_raw: raw,
    event_sha256: createHash('sha256').update(raw).digest('hex') }) + '\n')
  await p.drain()
  assert.ok(invalid.some(message => message.startsWith('observation_invalid:' + rows[0].name + ':')))
  assert.equal(readEvents(root).length, rows.length)
  for (const damaged of ['{', JSON.stringify({ ...child.marker, event_sha256: '0'.repeat(64) }) + '\n']) {
    fs.writeFileSync(path.join(root, 'pending', child.name), child.raw)
    fs.writeFileSync(path.join(root, 'done', child.name), JSON.stringify(child.marker) + '\n')
    await p.drain()
    invalid.length = 0
    fs.writeFileSync(path.join(root, 'done', child.name), damaged)
    assert.equal((await p.drain()).processed, 0)
    assert.ok(invalid.some(message => message.startsWith('observation_invalid:' + rows[0].name + ':')))
    assert.equal(fs.readFileSync(path.join(root, 'done', child.name), 'utf8'), damaged)
    assert.equal(readEvents(root).length, rows.length)
  }
})

test('new legacy reviews retain the 64-node ancestry boundary and reject self-parent metadata', async (t) => {
  const bounded = legacyHistory(t, 64)
  assert.equal(observationFreshness(bounded.root, bounded.rows.at(-1).value), 1)
  const tooDeep = legacyHistory(t, 65)
  assert.throws(() => observationFreshness(tooDeep.root, tooDeep.rows.at(-1).value), /bounded and acyclic/)
  let now = 1000
  const deepPump = await pump(t, tooDeep.root, () => assert.fail('overlong ancestry must not dispatch'), { now: () => now })
  await deepPump.drain()
  now = 10000
  assert.equal((await deepPump.drain()).processed, 0)
  assert.equal(readEvents(tooDeep.root).length, 65)

  const cyclic = legacyHistory(t, 5), forged = cyclic.rows[2]
  const value = { ...forged.value, payload: { ...forged.value.payload, review_context: {
    ...forged.value.payload.review_context, parent_observation_key: forged.key, parent_event_id: forged.value.event_id,
  } } }
  const raw = JSON.stringify(value) + '\n'
  fs.writeFileSync(path.join(cyclic.root, 'pending', forged.name), raw)
  fs.writeFileSync(path.join(cyclic.root, 'done', forged.name), JSON.stringify({ ...forged.marker, event_raw: raw,
    event_sha256: createHash('sha256').update(raw).digest('hex') }) + '\n')
  assert.throws(() => observationFreshness(cyclic.root, cyclic.rows.at(-1).value), /Review freshness/)
  const cyclePump = await pump(t, cyclic.root, () => assert.fail('self-parent ancestry must not dispatch'), { now: () => 10000 })
  assert.equal((await cyclePump.drain()).processed, 0)
  assert.equal(readEvents(cyclic.root).length, 5)
})

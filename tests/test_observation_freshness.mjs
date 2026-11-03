/** Real immutable files and queue markers on disposable storage. No model or transport. */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createHash } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import { createObservationPump, observationFreshness, observationKey } from '../plugin/lib/observation-queue.js'

const sha = raw => createHash('sha256').update(raw).digest('hex')
const pumps = new Map()
const original = () => ({ version: 1, event_id: 'synthetic-original', scope: 'private:101', kind: 'activity.opportunity',
  producer: 'mind', created_at: 1, payload: { evidence: 'synthetic' }, workspace_key: 'synthetic-workspace',
  agent_key: 'synthetic-agent', origin: 'observation', control_allowed: false })
function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-freshness-'))
  pumps.set(root, [])
  for (const sub of ['pending', 'done']) fs.mkdirSync(path.join(root, sub))
  t.after(async () => {
    await Promise.all(pumps.get(root).map(pump => pump.stop()))
    pumps.delete(root)
    assert.ok(root.startsWith(path.join(os.tmpdir(), 'lanya-freshness-')))
    fs.rmSync(root, { recursive: true, force: true })
  })
  return root
}
function rawEvent(value) { return JSON.stringify(value) + '\n' }
function complete(root, value, next) {
  const raw = rawEvent(value), key = observationKey(value.scope, value.event_id)
  fs.writeFileSync(path.join(root, 'done', key + '.json'), JSON.stringify({ version: 2, event_raw: raw,
    event_id: value.event_id, scope: value.scope, event_sha256: sha(raw), completed_at: value.created_at,
    status: 'decided', decision_id: 'decision-' + value.created_at, next_review_at: next }) + '\n')
}
function review(parent, at, anchor) {
  const key = observationKey(parent.scope, parent.event_id)
  return { ...parent, event_id: 'main-review:' + key + ':' + at, producer: 'main-review', created_at: at,
    payload: { ...parent.payload, review_context: { parent_observation_key: key, parent_event_id: parent.event_id,
      previous_decision_id: 'decision-' + parent.created_at, scheduled_at: at,
      ...(anchor === undefined ? {} : { freshness_anchor: anchor }) } } }
}
function chain(root, count, anchored = true) {
  const first = original(), anchor = { key: observationKey(first.scope, first.event_id), sha256: sha(rawEvent(first)), created_at: 1 }
  let value = first
  for (let index = 1; index <= count; index += 1) {
    complete(root, value, index + 1)
    value = review(value, index + 1, anchored ? anchor : undefined)
  }
  fs.writeFileSync(path.join(root, 'pending', observationKey(value.scope, value.event_id) + '.json'), rawEvent(value))
  return { first, value, anchor }
}

test('long anchored reviews read only the exact direct parent and original, including Python validation', t => {
  const root = fixture(t), { first, value } = chain(root, 100)
  const read = fs.readFileSync
  let reads = 0
  fs.readFileSync = function (file, ...args) { if (String(file).startsWith(root)) reads += 1; return read.call(this, file, ...args) }
  try { assert.equal(observationFreshness(root, value), first.created_at) } finally { fs.readFileSync = read }
  assert.equal(reads, 2, 'done-only originals require two physical bounded reads independent of depth')
  const result = spawnSync('python', ['-B', '-c',
    'import sys; from decision_events import observation_review_chain; r=observation_review_chain("private:101","synthetic-original",sys.argv[1]); print(r["event"]["created_at"])', root],
  { encoding: 'utf8', env: { ...process.env, PYTHONPATH: path.resolve('scripts') } })
  assert.equal(result.status, 0, result.stderr)
  assert.equal(result.stdout.trim(), '101')
})

test('forged or changed anchor and owner proof fail closed before any dispatch', t => {
  const root = fixture(t), { value } = chain(root, 3)
  for (const mutate of [event => { event.payload.review_context.freshness_anchor.sha256 = '0'.repeat(64) },
    event => { event.workspace_key = 'foreign-workspace' },
    event => { event.payload.review_context.freshness_anchor.created_at = 2 }]) {
    const changed = structuredClone(value); mutate(changed)
    assert.throws(() => observationFreshness(root, changed), /freshness/)
  }
})

test('legacy ancestry is retained and bounded rather than silently renewed', t => {
  const short = fixture(t), { value } = chain(short, 3, false)
  assert.equal(observationFreshness(short, value), 1)
  const long = fixture(t), deep = chain(long, 65, false).value
  assert.throws(() => observationFreshness(long, deep), /bounded/)
})

test('a pre-upgrade persisted review is accepted byte-for-byte and gains an anchor only in its successor', async t => {
  const root = fixture(t), { value } = chain(root, 1, false)
  const filename = path.join(root, 'pending', observationKey(value.scope, value.event_id) + '.json')
  const before = fs.readFileSync(filename)
  let now = 2, calls = 0
  const pump = createObservationPump({ directory: root, now: () => now, dispatch: async event => {
    calls += 1
    assert.deepEqual(event, value)
    return { completed: true, status: 'decided', decision_id: 'decision-2', next_review_at: 3 }
  } })
  pumps.get(root).push(pump)
  assert.equal(await pump.start({ poll: false }), true)
  assert.equal((await pump.drain()).processed, 1)
  assert.deepEqual(fs.readFileSync(filename), before)
  now = 3
  // Stop after the immutable successor is inspected, not after a second model turn.
  pump.stop()
  const second = createObservationPump({ directory: root, now: () => now, dispatch: async event => {
    assert.deepEqual(event.payload.review_context.freshness_anchor, {
      key: observationKey(original().scope, original().event_id), sha256: sha(rawEvent(original())), created_at: 1 })
    assert.equal(observationFreshness(root, event), 1)
    return { completed: true, status: 'decided', decision_id: 'successor' }
  } })
  pumps.get(root).push(second)
  assert.equal(await second.start({ poll: false }), true)
  assert.equal((await second.drain()).processed, 1)
  assert.equal(calls, 1)
})

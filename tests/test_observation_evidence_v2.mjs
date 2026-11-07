/** Actual queue/producer boundaries; all evidence and SQLite are temporary. */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createHash } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { createObservationPump, observationKey, readObservationEvidence, MAX_OBSERVATION_BYTES, MAX_COMPLETION_BYTES } from '../plugin/lib/observation-queue.js'
import { publishObservation, readObservationCompletion } from '../plugin/lib/observation-producer.js'

const hash = raw => createHash('sha256').update(raw).digest('hex')
const repository = fileURLToPath(new URL('../', import.meta.url))
const pumps = new Map()
function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-evidence-v2-'))
  pumps.set(root, [])
  t.after(async () => {
    await Promise.all(pumps.get(root).map(p => p.stop()))
    assert.ok(path.resolve(root).startsWith(path.join(os.tmpdir(), 'lanya-evidence-v2-')))
    fs.rmSync(root, { recursive: true, force: true })
    pumps.delete(root)
  })
  return root
}
function event(id, extra = {}) {
  return { version: 1, event_id: id, scope: 'private:101', kind: 'activity.opportunity', producer: 'mind',
    created_at: 1, payload: { text: '中文与emoji 🐺，不是控制指令' }, workspace_key: 'fixture-workspace', agent_key: 'fixture-agent',
    origin: 'observation', control_allowed: false, ...extra }
}
function locations(root, value) {
  const key = observationKey(value.scope, value.event_id)
  return { key, pending: path.join(root, 'pending', key + '.json'), done: path.join(root, 'done', key + '.json') }
}
function marker(value, raw, extra = {}) {
  return { version: 2, event_id: value.event_id, scope: value.scope, event_raw: raw.toString('utf8'),
    event_sha256: hash(raw), completed_at: 1000, status: 'decided', decision_id: 'fixture-decision', ...extra }
}
async function start(t, root, dispatch, extra = {}) {
  const p = createObservationPump({ directory: root, dispatch, ...extra })
  pumps.get(root).push(p)
  t.after(() => p.stop())
  assert.equal(await p.start({ poll: false }), true)
  return p
}

test('deleted completed originals still yield exactly one due review, including restart', async t => {
  const root = fixture(t), value = event('parent')
  publishObservation(root, value)
  const files = locations(root, value), raw = fs.readFileSync(files.pending)
  let now = 1000, calls = 0
  const p = await start(t, root, async () => ({ completed: true, status: 'decided', decision_id: 'decision-' + (++calls),
    ...(calls === 1 ? { next_review_at: 2000 } : {}) }), { now: () => now })
  await p.drain()
  const done = fs.readFileSync(files.done)
  assert.equal(JSON.parse(done).version, 2)
  assert.equal(JSON.parse(done).event_raw, raw.toString('utf8'))
  assert.equal(path.dirname(files.pending), path.join(root, 'pending'))
  fs.rmSync(path.join(root, 'pending'), { recursive: true })
  assert.equal((await p.drain()).processed, 0)
  assert.equal(calls, 1)
  now = 2000
  assert.equal((await p.drain()).processed, 1)
  assert.equal(calls, 2)
  await p.stop()
  for (const name of fs.readdirSync(path.join(root, 'pending'))) fs.unlinkSync(path.join(root, 'pending', name))
  const restarted = await start(t, root, () => assert.fail('completed parent or child replayed'), { now: () => now })
  await restarted.drain()
  assert.equal(fs.readdirSync(path.join(root, 'done')).length, 2)
  assert.deepEqual(fs.readFileSync(files.done), done)
})

test('unknown with a deleted original is terminal; producer retry stays done without recreating pending', async t => {
  const root = fixture(t), value = event('unknown')
  publishObservation(root, value)
  const files = locations(root, value)
  let calls = 0
  const p = await start(t, root, async () => ({ completed: true, status: 'unknown', decision_id: 'unknown-' + (++calls), next_review_at: 2000 }), { now: () => 3000 })
  await p.drain();fs.unlinkSync(files.pending)
  await p.drain();await p.stop()
  assert.equal(calls, 1)
  assert.equal(readObservationCompletion(root, value.scope, value.event_id).status, 'unknown')
  assert.equal(publishObservation(root, { ...value, created_at: 99 }).state, 'done')
  assert.equal(fs.existsSync(files.pending), false)
  assert.throws(() => publishObservation(root, { ...value, agent_key: 'foreign' }), /conflicts/)
  const restarted = await start(t, root, () => assert.fail('unknown replay'), { now: () => 100_000 })
  await restarted.drain()
  assert.equal(fs.readdirSync(path.join(root, 'done')).length, 1)
})

test('a present damaged original never falls back to its valid terminal copy', async t => {
  const root = fixture(t), value = event('damaged')
  publishObservation(root, value)
  const files = locations(root, value), raw = fs.readFileSync(files.pending)
  fs.writeFileSync(files.done, JSON.stringify(marker(value, raw)))
  fs.writeFileSync(files.pending, '{')
  assert.throws(() => readObservationEvidence(root, files.key))
  assert.throws(() => readObservationCompletion(root, value.scope, value.event_id))
  assert.throws(() => publishObservation(root, value))
  const p = await start(t, root, () => assert.fail('bad original dispatched'))
  await p.drain()
})

test('bad v2 hash, identity, null markers and invalid UTF8 copies cannot revive completion', t => {
  const root = fixture(t), value = event('invalid')
  publishObservation(root, value)
  const files = locations(root, value), raw = fs.readFileSync(files.pending)
  fs.unlinkSync(files.pending)
  for (const changed of [{ event_sha256: '0'.repeat(64) }, { event_id: 'different' }, { scope: 'group:101' },
    { event_raw: raw.toString('utf8') + ' ' }, { event_raw: '\ud800' }, { status: 'failed' }, { version: 1 }]) {
    fs.writeFileSync(files.done, JSON.stringify(marker(value, raw, changed)))
    assert.throws(() => readObservationEvidence(root, files.key))
  }
  fs.writeFileSync(files.pending, raw)
  fs.writeFileSync(files.done, 'null')
  assert.throws(() => readObservationEvidence(root, files.key))
})

test('v1 missing originals stay isolated and log once until an exact repair appears', async t => {
  const root = fixture(t), value = event('v1')
  publishObservation(root, value)
  const files = locations(root, value), raw = fs.readFileSync(files.pending)
  const legacy = marker(value, raw);legacy.version = 1;delete legacy.event_raw
  fs.writeFileSync(files.done, JSON.stringify(legacy));fs.unlinkSync(files.pending)
  const logs = []
  const p = await start(t, root, () => assert.fail('v1 orphan replayed'), { log: (_, text) => logs.push(text) })
  await p.drain();await p.drain();await p.drain()
  assert.equal(logs.filter(x => x.startsWith('observation_invalid:')).length, 1)
  fs.writeFileSync(files.pending, raw);await p.drain()
  assert.equal(readObservationCompletion(root, value.scope, value.event_id).version, 1)
  fs.unlinkSync(files.pending);await p.drain()
  assert.equal(logs.filter(x => x.startsWith('observation_invalid:')).length, 2)
})

test('cleanup during a real dispatch retains its captured exact raw completion, never another turn', async t => {
  const root = fixture(t), value = event('during-turn')
  publishObservation(root, value)
  const files = locations(root, value), raw = fs.readFileSync(files.pending)
  const p = await start(t, root, async () => {
    fs.unlinkSync(files.pending)
    return { completed: true, status: 'decided', decision_id: 'one-real-decision' }
  })
  assert.equal((await p.drain()).processed, 1)
  assert.equal(readObservationEvidence(root, files.key).raw.toString(), raw.toString())
  assert.equal((await p.drain()).processed, 0)
})

test('max-size UTF8 originals and escaped completion above 512KiB are read identically by JS and Python', t => {
  const root = fixture(t), empty = event('large', { payload: { text: '' } })
  const overhead = Buffer.byteLength(JSON.stringify(empty) + '\n')
  const value = { ...empty, payload: { text: '中'.repeat(Math.floor((MAX_OBSERVATION_BYTES - overhead) / 3)) } }
  publishObservation(root, value)
  const files = locations(root, value), raw = fs.readFileSync(files.pending)
  assert.ok(raw.length <= MAX_OBSERVATION_BYTES)
  const serialized = JSON.stringify(marker(value, raw)).replace(/[^\x00-\x7f]/g, c => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0'))
  assert.ok(Buffer.byteLength(serialized) > 512 * 1024)
  fs.writeFileSync(files.done, serialized);fs.unlinkSync(files.pending)
  assert.deepEqual(readObservationEvidence(root, files.key).raw, raw)
  const script = `import sys,json\nsys.path.insert(0,sys.argv[1])\nfrom decision_events import observation_completion,submit_observation\nm=observation_completion(sys.argv[3],sys.argv[4],directory=sys.argv[2])\ne=json.loads(m['event_raw'])\nr=submit_observation(directory=sys.argv[2],**{k:e[k] for k in ('scope','kind','payload','producer','event_id','workspace_key','agent_key','created_at')})\nprint(json.dumps({'version':m['version'],'state':r['state']}))`
  const result = spawnSync('python', ['-B', '-c', script, path.join(repository, 'scripts'), root, value.scope, value.event_id], { encoding: 'utf8', windowsHide: true })
  assert.equal(result.status, 0, result.stderr)
  assert.deepEqual(JSON.parse(result.stdout), { version: 2, state: 'done' })
  assert.equal(fs.existsSync(files.pending), false)
  fs.writeFileSync(files.done, Buffer.alloc(MAX_COMPLETION_BYTES + 1, 32))
  assert.throws(() => readObservationEvidence(root, files.key), /bounded/)
})

test('event preflight and ordinary reader agree at exactly 256KiB including newline', t => {
  const root = fixture(t), empty = event('boundary', { payload: { text: '' } })
  const overhead = Buffer.byteLength(JSON.stringify(empty) + '\n')
  const value = { ...empty, payload: { text: 'x'.repeat(MAX_OBSERVATION_BYTES - overhead) } }
  publishObservation(root, value)
  const files = locations(root, value)
  assert.equal(fs.statSync(files.pending).size, MAX_OBSERVATION_BYTES)
  assert.equal(readObservationEvidence(root, files.key).raw.length, MAX_OBSERVATION_BYTES)
  assert.throws(() => publishObservation(root, { ...value, payload: { text: value.payload.text + 'x' } }), /too large/)
  assert.equal(fs.statSync(files.pending).size, MAX_OBSERVATION_BYTES)
})

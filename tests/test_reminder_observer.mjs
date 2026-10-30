import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { createHash } from 'node:crypto'
import { createReminderObserver } from '../plugin/lib/reminder-observer.js'
import { publishObservation, readObservationCompletion } from '../plugin/lib/observation-producer.js'
import { processReminders } from '../plugin/lib/runtime.js'

function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-reminder-observer-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const now = new Date(2026, 9, 27, 10, 0)
  const state = { clock: now.getTime() }
  const observer = createReminderObserver({ directory, workspaceKey: 'fixture', agentKey: '', resolveScope: () => 'private:101', now: () => state.clock })
  const list = [{ at: new Date(now.getTime() - 1000).toISOString(), text: 'fixture reminder' }]
  const pending = () => fs.readdirSync(path.join(directory, 'pending')).filter((name) => name.endsWith('.json'))
  const complete = (name, status, nextReview) => {
    const raw = fs.readFileSync(path.join(directory, 'pending', name))
    const marker = { version: 1, status, decision_id: 'main-fixture', event_sha256: createHash('sha256').update(raw).digest('hex') }
    if (nextReview) marker.next_review_at = nextReview
    fs.writeFileSync(path.join(directory, 'done', name), JSON.stringify(marker))
  }
  return { directory, now, state, observer, list, pending, complete }
}

test('pending reminders remain undelivered and repeated ticks do not publish more evidence', async (t) => {
  const f = fixture(t)
  const first = await processReminders(f.list, {}, f.now, f.observer)
  const second = await processReminders(first.keep, first.state, f.now, f.observer)
  assert.deepEqual(second.keep, f.list)
  assert.deepEqual(second.state.once, {})
  assert.equal(f.pending().length, 1)
})
test('a durable sent outcome removes the reminder only on the following acknowledgement tick', async (t) => {
  const f = fixture(t)
  const first = await processReminders(f.list, {}, f.now, f.observer)
  f.complete(f.pending()[0], 'sent')
  const second = await processReminders(first.keep, first.state, f.now, f.observer)
  assert.equal(second.keep.length, 0)
  assert.equal(Object.keys(second.state.once).length, 1)
  assert.equal(f.pending().length, 1)
})
for (const status of ['unknown', 'decided', 'suppressed']) {
  test(status + ' retains reminder and never starts an automatic retry', async (t) => {
    const f = fixture(t)
    await processReminders(f.list, {}, f.now, f.observer)
    f.complete(f.pending()[0], status)
    f.state.clock += 24 * 3600 * 1000
    const later = await processReminders(f.list, {}, new Date(f.state.clock), f.observer)
    assert.deepEqual(later.keep, f.list)
    assert.deepEqual(later.state.once, {})
    assert.equal(f.pending().length, 1)
  })
}
test('only the primary LLM future review can create one successor observation', async (t) => {
  const f = fixture(t)
  await processReminders(f.list, {}, f.now, f.observer)
  const next = f.state.clock + 120_000
  f.complete(f.pending()[0], 'decided', next)
  await processReminders(f.list, {}, f.now, f.observer)
  assert.equal(f.pending().length, 1)
  f.state.clock = next
  await processReminders(f.list, {}, new Date(next), f.observer)
  await processReminders(f.list, {}, new Date(next), f.observer)
  assert.equal(f.pending().length, 2)
})
test('producer refuses identity changes and non-JSON evidence, and fixes control metadata', (t) => {
  const f = fixture(t)
  const event = { scope: 'private:101', event_id: 'fixture', kind: 'test', producer: 'fixture', workspace_key: '', agent_key: '', created_at: 1, payload: { text: '中文' }, origin: 'live', control_allowed: true }
  publishObservation(f.directory, event)
  assert.equal(publishObservation(f.directory, { ...event, created_at: 2 }).submitted, false)
  assert.throws(() => publishObservation(f.directory, { ...event, payload: { text: 'changed' } }), /conflict/)
  assert.throws(() => publishObservation(f.directory, { ...event, event_id: 'nan', payload: { n: NaN } }), /JSON/)
  const written = JSON.parse(fs.readFileSync(path.join(f.directory, 'pending', f.pending()[0])))
  assert.equal(written.origin, 'observation')
  assert.equal(written.control_allowed, false)
})
test('Python accepts a Node published immutable event without changing its bytes', (t) => {
  const f = fixture(t)
  const event = { version: 1, scope: 'private:101', event_id: 'interop', kind: 'test', producer: 'fixture', workspace_key: '', agent_key: '', created_at: 1, payload: { text: '中文' }, origin: 'observation', control_allowed: false }
  publishObservation(f.directory, event)
  const file = path.join(f.directory, 'pending', f.pending()[0])
  const before = fs.readFileSync(file)
  const script = fileURLToPath(new URL('../scripts/decision_events.py', import.meta.url))
  const run = spawnSync('python', [script, '--directory', f.directory], { input: JSON.stringify({ ...event, created_at: 2 }), encoding: 'utf8', windowsHide: true })
  assert.equal(run.status, 0, run.stderr)
  assert.equal(JSON.parse(run.stdout).submitted, false)
  assert.deepEqual(fs.readFileSync(file), before)
  f.complete(f.pending()[0], 'sent')
  assert.equal(readObservationCompletion(f.directory, event.scope, event.event_id).status, 'sent')
})

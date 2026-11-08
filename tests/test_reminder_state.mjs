import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { readReminderDocument } from '../plugin/lib/index.js'

test('only a genuinely missing reminder state becomes an empty baseline', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-reminder-state-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const file = path.join(root, 'state.json')
  assert.equal(readReminderDocument(file, { state: true }), null)
  for (const raw of ['{broken', 'null', '[]', '1', '"text"', Buffer.from([0xff, 0xfe])]) {
    fs.writeFileSync(file, raw)
    const before = fs.readFileSync(file)
    assert.throws(() => readReminderDocument(file, { state: true }))
    assert.deepEqual(fs.readFileSync(file), before)
  }
  fs.writeFileSync(file, '\uFEFF' + JSON.stringify({ pending: { fixture: { day: '2026-10-27' } } }))
  assert.equal(readReminderDocument(file, { state: true }).pending.fixture.day, '2026-10-27')
})

test('a corrupt reminder list cannot silently erase old pending state', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-reminder-list-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const file = path.join(root, 'reminders.json')
  fs.writeFileSync(file, '{broken')
  assert.throws(() => readReminderDocument(file))
  fs.writeFileSync(file, JSON.stringify([{ text: 'fixture' }]))
  assert.deepEqual(readReminderDocument(file), [{ text: 'fixture' }])
})

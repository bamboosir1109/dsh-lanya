import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { resolveRuntime, processReminders, localDay, acquireReminderLock } from '../plugin/lib/runtime.js'
import { apply, launchScript } from '../plugin/lib/index.js'

function workspace(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'character runtime '))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  fs.mkdirSync(path.join(root, 'napcat', 'config'), { recursive: true })
  fs.writeFileSync(path.join(root, 'napcat', 'config', 'project.json'), '\uFEFF' + JSON.stringify({
    dataDirName: 'Custom character', panelPort: 23456
  }))
  return root
}

test('runtime follows configured directory and preserves existing persona', (t) => {
  const root = workspace(t)
  const persona = path.join(root, 'napcat', 'Custom character-人设正文.md')
  fs.writeFileSync(persona, 'Existing persona')
  const runtime = resolveRuntime({ workDir: root }, { env: {}, cwd: os.tmpdir() })
  assert.equal(runtime.personaFile, persona)
  assert.equal(runtime.dataDir, path.join(root, 'Custom character'))
  assert.equal(runtime.project.panelPort, 23456)
})

test('plugin sections can evaluate synchronously during activation', (t) => {
  const root = workspace(t)
  fs.mkdirSync(path.join(root, 'Custom character'))
  fs.writeFileSync(path.join(root, 'Custom character', 'lanya-config.json'), JSON.stringify({ adminQQ: ['123456789'], adminCall: 'owner' }))
  const sections = []
  apply({
    effect(fn, label) { if (!label.includes('scheduler')) fn() },
    systemPrompt: { section(spec) { sections.push(spec.text()); return () => {} } },
    inject() {},
    logger: { info() {} }
  }, { workDir: root, schedulerEnabled: false })
  assert.ok(sections.some((text) => text.includes('123456789')))
})

test('environment overrides match the script resolver and keep explicit missing personas', (t) => {
  const root = workspace(t)
  const override = path.join(root, 'override.json')
  const missingPersona = path.join(root, 'not-created.md')
  fs.writeFileSync(override, JSON.stringify({ dataDirName: 'Override' }))
  const runtime = resolveRuntime({ workDir: root, projectConfig: path.join(root, 'napcat', 'config', 'project.json') },
    { cwd: root, env: { LANYA_PROJECT_CONFIG: override, LANYA_PERSONA_FILE: missingPersona } })
  assert.equal(runtime.dataDir, path.join(root, 'Override'))
  assert.equal(runtime.personaFile, missingPersona)
})

test('component actions execute Python scripts with Python', async (t) => {
  const root = workspace(t)
  const result = path.join(root, 'python-ran.txt')
  fs.writeFileSync(path.join(root, 'probe.py'), 'from pathlib import Path\nPath(__file__).with_name("python-ran.txt").write_text("ok")\n')
  await launchScript('probe.py', root)
  for (let i = 0; i < 100 && !fs.existsSync(result); i++) await new Promise((resolve) => setTimeout(resolve, 20))
  assert.equal(fs.readFileSync(result, 'utf8'), 'ok')
})

test('sending one due reminder keeps all future reminders', async () => {
  const now = new Date(2026, 9, 27, 10, 0)
  const list = [{ at: new Date(2026, 9, 27, 9, 0).toISOString(), text: 'due' },
    { at: new Date(2026, 9, 28, 10, 0).toISOString(), text: 'future' }]
  const sent = []
  const result = await processReminders(list, {}, now, async (r) => sent.push(r.text))
  assert.deepEqual(sent, ['due'])
  assert.deepEqual(result.keep, [list[1]])
})

test('daily delivery survives index shift and uses local calendar day', async () => {
  const now = new Date(2026, 9, 27, 8, 0)
  assert.equal(localDay(now), '2026-10-27')
  const once = { at: new Date(2026, 9, 27, 7, 0).toISOString(), text: 'once' }
  const daily = { daily: '08:00', text: 'daily' }
  const sent = []
  const first = await processReminders([once, daily], {}, now, async (r) => sent.push(r.text))
  await processReminders(first.keep, first.state, now, async (r) => sent.push(r.text))
  assert.deepEqual(sent, ['once', 'daily'])
})

test('failed transport remains due for retry', async () => {
  const now = new Date(2026, 9, 27, 10, 0)
  const list = [{ at: new Date(2026, 9, 27, 9, 0).toISOString(), text: 'retry' }]
  const result = await processReminders(list, {}, now, async () => { throw new Error('retcode=failed') })
  assert.deepEqual(result.keep, list)
  assert.deepEqual(result.state.once, {})
})

test('daily failure retries during grace window and does not create a stale backlog', async () => {
  const daily = { daily: '09:00', text: 'retry' }
  const failed = await processReminders([daily], {}, new Date(2026, 9, 27, 9, 0), async () => { throw new Error('offline') })
  let sent = 0
  await processReminders(failed.keep, failed.state, new Date(2026, 9, 27, 9, 1), async () => { sent++ })
  await processReminders([daily], {}, new Date(2026, 9, 27, 9, 6), async () => { sent++ })
  assert.equal(sent, 1)
})

test('reminder lock excludes a live owner and releases for the next consumer', (t) => {
  const root = workspace(t)
  const release = acquireReminderLock(root)
  assert.equal(typeof release, 'function')
  assert.equal(acquireReminderLock(root), null)
  release()
  const next = acquireReminderLock(root)
  assert.equal(typeof next, 'function')
  next()
})

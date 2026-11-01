import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

const fixture = fileURLToPath(new URL('./fixtures/qq_expression_media/native-cases.mjs', import.meta.url))
const names = [...fs.readFileSync(fixture, 'utf8').matchAll(/^test\('([^']+)'/gm)].map(match => match[1])
assert.ok(names.length >= 6)
for (const name of names) test(name, () => {
  const pattern = '^' + name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '$'
  const env = { ...process.env }; delete env.NODE_TEST_CONTEXT
  const run = spawnSync(process.execPath, ['--experimental-vm-modules', '--test', '--test-name-pattern=' + pattern, fixture],
    { encoding: 'utf8', windowsHide: true, timeout: 30000, env })
  assert.equal(run.status, 0, run.stdout + run.stderr)
  assert.match(run.stdout, /(?:pass 1|# pass 1)/)
  assert.match(run.stdout, /(?:fail 0|# fail 0)/)
})

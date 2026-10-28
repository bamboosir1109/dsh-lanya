import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

// Keep VM flags in the child, so the ordinary `node --test tests/*.mjs`
// invocation runs every native case without an experimental global setting.
const fixture = fileURLToPath(new URL('./fixtures/onebot_egress/native-cases.mjs', import.meta.url))
const names = [...fs.readFileSync(fixture, 'utf8').matchAll(/^test\('([^']+)'/gm)].map((match) => match[1])
assert.ok(names.length >= 15)
for (const name of names) test(name, () => {
  const pattern = '^' + name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '$'
  const env = { ...process.env }
  delete env.NODE_TEST_CONTEXT
  const result = spawnSync(process.execPath, ['--experimental-vm-modules', '--test', '--test-name-pattern=' + pattern, fixture],
    { encoding: 'utf8', windowsHide: true, timeout: 20000, env })
  assert.equal(result.status, 0, result.stdout + result.stderr)
  assert.match(result.stdout, /(?:pass 1|# pass 1)/)
  assert.match(result.stdout, /(?:fail 0|# fail 0)/)
})

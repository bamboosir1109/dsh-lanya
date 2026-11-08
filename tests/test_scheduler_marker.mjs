import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'node:test'
import { writeSchedulerMarker } from '../plugin/lib/index.js'

function cleanup(dir) {
  assert.equal(path.dirname(fs.realpathSync(dir)), fs.realpathSync(os.tmpdir()))
  assert.ok(path.basename(dir).startsWith('lanya-heartbeat-'))
  fs.rmSync(dir, { recursive: true, force: true })
}

test('scheduler heartbeat retains a coherent busy-reader document and then replaces the closed target with a new inode', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-heartbeat-'))
  try {
    const file = path.join(dir, 'marker.json')
    writeSchedulerMarker(file, { version: 2, pid: 10, at: 1, jobs: ['mind'] })
    const before = fs.statSync(file, { bigint: true })
    const old = fs.openSync(file, 'r')
    try {
      if (process.platform === 'win32') {
        assert.throws(() => writeSchedulerMarker(file, { version: 2, pid: 10, at: 2, jobs: ['mind'] }), { code: 'EPERM' })
        assert.equal(JSON.parse(fs.readFileSync(file, 'utf8')).at, 1)
      } else {
        writeSchedulerMarker(file, { version: 2, pid: 10, at: 2, jobs: ['mind'] })
        assert.equal(JSON.parse(fs.readFileSync(file, 'utf8')).at, 2)
      }
      assert.equal(JSON.parse(fs.readFileSync(old, 'utf8')).at, 1)
    } finally { fs.closeSync(old) }
    writeSchedulerMarker(file, { version: 2, pid: 10, at: 3, jobs: ['mind'] })
    assert.equal(JSON.parse(fs.readFileSync(file, 'utf8')).at, 3)
    assert.notEqual(before.ino, fs.statSync(file, { bigint: true }).ino)
    assert.deepEqual(fs.readdirSync(dir), ['marker.json'])
  } finally { cleanup(dir) }
})

test('a failed heartbeat replacement preserves the previous target and removes only its own candidate', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-heartbeat-failure-'))
  try {
    const target = path.join(dir, 'occupied-directory')
    fs.mkdirSync(target)
    fs.writeFileSync(path.join(target, 'original'), 'unchanged')
    assert.throws(() => writeSchedulerMarker(target, { version: 2 }))
    assert.equal(fs.readFileSync(path.join(target, 'original'), 'utf8'), 'unchanged')
    assert.deepEqual(fs.readdirSync(dir), ['occupied-directory'])
  } finally { cleanup(dir) }
})

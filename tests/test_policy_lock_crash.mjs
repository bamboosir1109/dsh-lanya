import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawn, spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { createDecisionPolicy, createJsonPolicyStorage, repairPolicyLocks } from '../plugin/lib/decision-policy.js'

const modulePath = fileURLToPath(new URL('../plugin/lib/decision-policy.js', import.meta.url))
const event = { scope: 'group:200', sender: '300', controlAllowed: false, origin: 'live', messageId: 'after-crash' }

function temporary(t) {
  const prefix = path.join(os.tmpdir(), 'lanya-lock-crash-')
  const dir = fs.mkdtempSync(prefix)
  t.after(() => {
    assert.ok(path.resolve(dir).startsWith(path.resolve(prefix)))
    fs.rmSync(dir, { recursive: true, force: true })
  })
  return { dir, stateFile: path.join(dir, 'policy.json') }
}

// Real filesystem operations are intercepted only at the chosen crash point in
// this disposable child. The parent kills that exact child while it is paused;
// recovery uses the ordinary process-liveness check, not an injected dead owner.
const crashChild = `
  import fs from 'node:fs';
  import { pathToFileURL } from 'node:url';
  const [modulePath,stateFile,point]=process.argv.slice(1);
  const lock=stateFile+'.lock';
  const pause=(candidate,target)=>{
    process.send({point,candidate,target,pid:process.pid});
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0);
  };
  const open=fs.openSync,link=fs.linkSync;
  fs.openSync=(file,flags,...args)=>{
    const fd=open(file,flags,...args);
    if(point==='candidate-created' && String(file).startsWith(lock+'.owner-') && flags==='wx') pause(file,null);
    return fd;
  };
  fs.linkSync=(candidate,target)=>{
    if(point==='before-publish' && target===lock) pause(candidate,target);
    const result=link(candidate,target);
    if(point==='after-publish' && target===lock) pause(candidate,target);
    if(point==='claim-published' && String(target).startsWith(lock+'.reclaim-')) pause(candidate,target);
    return result;
  };
  const {createDecisionPolicy}=await import(pathToFileURL(modulePath).href);
  await createDecisionPolicy({stateFile}).recordInbound(${JSON.stringify(event)});
  throw new Error('crash point was not reached');
`

async function pausedChild(t, stateFile, point) {
  const child = spawn(process.execPath, ['--input-type=module', '-e', crashChild, modulePath, stateFile, point], {
    windowsHide: true, stdio: ['ignore', 'ignore', 'pipe', 'ipc'],
  })
  let stderr = ''
  child.stderr.on('data', chunk => { stderr += chunk })
  const exited = new Promise(resolve => child.once('exit', (code, signal) => resolve({ code, signal })))
  const stop = async () => {
    if (child.exitCode === null && child.signalCode === null) assert.equal(child.kill('SIGKILL'), true)
    await exited
  }
  t.after(stop)
  const ready = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('crash fixture did not reach ' + point)), 10_000)
    child.once('message', message => { clearTimeout(timer); resolve(message) })
    child.once('error', error => { clearTimeout(timer); reject(error) })
    child.once('exit', () => { clearTimeout(timer); reject(new Error(stderr || 'crash fixture exited before pause')) })
  })
  assert.equal(ready.point, point)
  assert.equal(ready.pid, child.pid)
  return { ...ready, stop }
}

test('process death before publishing an owner cannot leave an active empty lock', async t => {
  for (const point of ['candidate-created', 'before-publish']) await t.test(point, async t => {
    const { dir, stateFile } = temporary(t)
    const paused = await pausedChild(t, stateFile, point)
    assert.equal(fs.existsSync(stateFile + '.lock'), false)
    const abandoned = fs.readFileSync(paused.candidate)
    if (point === 'candidate-created') assert.equal(abandoned.length, 0)
    else assert.equal(JSON.parse(abandoned).pid, paused.pid)
    await paused.stop()
    const policy = createDecisionPolicy({ stateFile })
    assert.equal((await policy.recordInbound(event)).accepted, true)
    assert.equal((await policy.snapshot(event.scope)).revision, 1)
    assert.equal(fs.existsSync(stateFile + '.lock'), false)
    // A crashed unique candidate is inert and retained, never glob-deleted.
    assert.deepEqual(fs.readFileSync(paused.candidate), abandoned)
    assert.deepEqual(fs.readdirSync(dir).sort(), [path.basename(paused.candidate), 'policy.json'].sort())
  })
})

test('process death after publication leaves a complete dead owner that recovers normally', async t => {
  const { dir, stateFile } = temporary(t)
  const paused = await pausedChild(t, stateFile, 'after-publish')
  const lock = stateFile + '.lock'
  const owner = fs.readFileSync(lock)
  assert.equal(JSON.parse(owner).pid, paused.pid)
  assert.ok(JSON.parse(owner).nonce)
  assert.equal(fs.statSync(lock, { bigint: true }).ino, fs.statSync(paused.candidate, { bigint: true }).ino)
  await paused.stop()
  const policy = createDecisionPolicy({ stateFile })
  assert.equal((await policy.recordInbound(event)).accepted, true)
  assert.equal(fs.existsSync(lock), false)
  assert.deepEqual(fs.readFileSync(paused.candidate), owner)
  assert.equal(fs.readdirSync(dir).some(name => name.includes('.reclaim-')), false)
})

test('a published reclaimer owner is complete and remains fail-closed after a real crash', async t => {
  const { stateFile } = temporary(t)
  const departed = spawnSync(process.execPath, ['-e', ''], { windowsHide: true })
  assert.equal(departed.status, 0)
  const lock = stateFile + '.lock'
  const original = JSON.stringify({ pid: departed.pid, nonce: 'departed-fixture' })
  fs.writeFileSync(lock, original)
  const claim = lock + '.reclaim-' + createHash('sha256').update(original).digest('hex')
  const paused = await pausedChild(t, stateFile, 'claim-published')
  assert.equal(paused.target, claim)
  const claimOwner = fs.readFileSync(claim)
  assert.equal(JSON.parse(claimOwner).pid, paused.pid)
  assert.ok(JSON.parse(claimOwner).nonce)
  await paused.stop()
  const store = createJsonPolicyStorage({ stateFile, lockTimeoutMs: 30, retryMs: 1 })
  await assert.rejects(() => store.transact(() => assert.fail('abandoned claim must block')), { code: 'POLICY_LOCK_UNAVAILABLE' })
  assert.equal(fs.readFileSync(lock, 'utf8'), original)
  assert.deepEqual(fs.readFileSync(claim), claimOwner)
  const preview = repairPolicyLocks({ stateFile })
  assert.equal(preview.status, 'repair_preview')
  assert.deepEqual(preview.files.map(file => file.ownerState), ['dead', 'dead'])
})

test('candidate cleanup failure releases only its own published lock before any transaction', async t => {
  const { dir, stateFile } = temporary(t)
  const unlink = fs.unlinkSync
  const denied = Object.assign(new Error('fixture candidate cleanup denied'), { code: 'EACCES' })
  t.mock.method(fs, 'unlinkSync', file => {
    if (String(file).startsWith(stateFile + '.lock.owner-')) throw denied
    return unlink(file)
  })
  const store = createJsonPolicyStorage({ stateFile })
  await assert.rejects(() => store.transact(() => assert.fail('cleanup must precede transaction')), error => error === denied)
  assert.equal(fs.existsSync(stateFile + '.lock'), false)
  assert.equal(fs.existsSync(stateFile), false)
  assert.equal(fs.readdirSync(dir).length, 1)
})

test('a filesystem without hard-link support refuses entry without creating a partial public lock', async t => {
  const { dir, stateFile } = temporary(t)
  const refused = Object.assign(new Error('fixture hard links unavailable'), { code: 'ENOTSUP' })
  t.mock.method(fs, 'linkSync', () => { throw refused })
  const store = createJsonPolicyStorage({ stateFile })
  await assert.rejects(() => store.transact(() => assert.fail('unsupported filesystem must not enter')), error => error === refused)
  assert.deepEqual(fs.readdirSync(dir), [])
})

test('failed cleanup never unlinks a foreign live lock and preserves both errors', async t => {
  const { stateFile } = temporary(t)
  const lock = stateFile + '.lock'
  const foreign = JSON.stringify({ pid: process.pid, nonce: 'existing-owner' })
  fs.writeFileSync(lock, foreign)
  const unlink = fs.unlinkSync
  const denied = Object.assign(new Error('fixture candidate cleanup denied'), { code: 'EACCES' })
  t.mock.method(fs, 'unlinkSync', file => {
    if (String(file).startsWith(lock + '.owner-')) throw denied
    assert.notEqual(file, lock, 'this contender never acquired the public lock')
    return unlink(file)
  })
  const store = createJsonPolicyStorage({ stateFile, lockTimeoutMs: 20, retryMs: 1 })
  await assert.rejects(() => store.transact(() => assert.fail('living lock must block')), error => {
    assert.ok(error instanceof AggregateError)
    assert.equal(error.errors[0].code, 'POLICY_LOCK_UNAVAILABLE')
    assert.equal(error.errors[1], denied)
    return true
  })
  assert.equal(fs.readFileSync(lock, 'utf8'), foreign)
})

test('denied candidate cleanup and denied own-lock release are both reported without entering a transaction', async t => {
  const { stateFile } = temporary(t)
  const candidateError = Object.assign(new Error('fixture candidate cleanup denied'), { code: 'EACCES' })
  const lockError = Object.assign(new Error('fixture own-lock release denied'), { code: 'EPERM' })
  const unlink = fs.unlinkSync
  t.mock.method(fs, 'unlinkSync', file => {
    if (String(file).startsWith(stateFile + '.lock.owner-')) throw candidateError
    if (file === stateFile + '.lock') throw lockError
    return unlink(file)
  })
  const store = createJsonPolicyStorage({ stateFile })
  await assert.rejects(() => store.transact(() => assert.fail('cleanup must precede entry')), error => {
    assert.ok(error instanceof AggregateError)
    assert.deepEqual(error.errors, [candidateError, lockError])
    return true
  })
  assert.equal(JSON.parse(fs.readFileSync(stateFile + '.lock')).pid, process.pid)
  assert.equal(fs.existsSync(stateFile), false)
})

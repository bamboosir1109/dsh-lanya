import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createHash, randomBytes } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import vm from 'node:vm'
import { recoverPendingInbox, recoveryDirectoryRecord, recoveryMessageHash } from '../plugin/lib/qq-inbox-recovery.js'

const sha = raw => createHash('sha256').update(raw).digest('hex')
const clone = value => JSON.parse(JSON.stringify(value))
const live = id => ({ id, role: 'user', source: { kind: 'plugin:dsh-onebot' }, content: [{ type: 'text', text: 'synthetic input ' + id }] })

function fixture(messages = [live('first'), live('second')]) {
  const directory = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(), 'qq-inbox-recovery-fixture-')))
  if (process.platform === 'win32') {
    const script = "$ErrorActionPreference='Stop';$p=$env:LANYA_RECOVERY_FIXTURE;$a=New-Object System.Security.AccessControl.DirectorySecurity;$u=[Security.Principal.WindowsIdentity]::GetCurrent().User;$a.SetOwner($u);$a.SetAccessRuleProtection($true,$false);foreach($s in @($u.Value,'S-1-5-18','S-1-5-32-544')){$r=New-Object System.Security.AccessControl.FileSystemAccessRule((New-Object System.Security.Principal.SecurityIdentifier($s)),'FullControl','ContainerInherit,ObjectInherit','None','Allow');$a.AddAccessRule($r)};[IO.Directory]::SetAccessControl($p,$a)"
    const executable = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
    const result = spawnSync(executable, ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(script, 'utf16le').toString('base64')],
      { windowsHide: true, encoding: 'utf8', timeout: 5000, env: { ...process.env, LANYA_RECOVERY_FIXTURE: directory } })
    if (result.status !== 0) {
      const actual = fs.realpathSync.native(directory), temp = fs.realpathSync.native(os.tmpdir())
      assert.equal(path.dirname(actual).toLowerCase(), temp.toLowerCase())
      assert.ok(path.basename(actual).startsWith('qq-inbox-recovery-fixture-'))
      fs.rmdirSync(actual)
    }
    assert.equal(result.status, 0, 'fixture private DACL must be established before writing: ' + result.stderr)
  } else fs.chmodSync(directory, 0o700)
  const native = []
  const inbox = {
    nextTurn: clone(messages), nextStep: [], removeCalls: 0,
    remove(id) {
      this.removeCalls += 1
      const index = this.nextTurn.findIndex(message => message.id === id)
      if (index < 0) return false
      this.nextTurn.splice(index, 1)
      native.push({ seq: native.length, time: Date.now(), type: 'agent/inbox/spliced',
        data: { target: 'next-turn', start: index, removedCount: 1, inserted: [], outcome: 'canceled' } })
      this.afterRemove?.()
      return true
    }
  }
  const owner = { cwd: directory, preset: 'fixture', workspace_key: 'workspace-fixture', agent_key: 'agent-fixture' }
  const session = { id: 'onebot-fixture', snapshotEvents: () => native }
  const chat = { sessionId: session.id, agent: { session, inbox } }
  const binding = { origin: 'live', scope: 'group:7', owner, messageId: 'new-live-input', revision: 3 }
  const operation = randomBytes(16).toString('hex')
  const plan = { format: 'dsh-qq-inbox-recovery-plan/v1', operation_id: operation, scope: binding.scope,
    owner, session_id: session.id, archive_only: true, automatic_retry: false,
    audit_directory: directory, audit_identity: recoveryDirectoryRecord(directory),
    selected: messages.filter(message => message.source.kind === 'plugin:dsh-onebot')
      .map(message => ({ id: message.id, message: clone(message), sha256: recoveryMessageHash(message) })) }
  const planFile = path.join(directory, 'plan.json')
  const config = {}
  function savePlan() {
    const raw = Buffer.from(JSON.stringify(plan))
    fs.writeFileSync(planFile, raw, { mode: 0o600 })
    config.inboxRecovery = { planFile, planSha256: sha(raw) }
  }
  savePlan()
  return { directory, native, inbox, chat, binding, plan, config, savePlan,
    run: () => recoverPendingInbox({ config, chat, binding }),
    result: () => JSON.parse(fs.readFileSync(path.join(directory, 'qq-inbox-' + operation + '.result.json'), 'utf8')),
    cleanup() {
      const actual = fs.realpathSync.native(directory)
      const temp = fs.realpathSync.native(os.tmpdir())
      const equalPath = value => process.platform === 'win32' ? value.toLowerCase() : value
      assert.equal(equalPath(path.dirname(actual)), equalPath(temp))
      assert.ok(path.basename(actual).startsWith('qq-inbox-recovery-fixture-'))
      fs.rmSync(actual, { recursive: true })
    }
  }
}

test('unconfigured and non-live calls do not touch chat or files', () => {
  assert.deepEqual(recoverPendingInbox({ config: {}, get chat() { throw Error('must not inspect chat') } }), { configured: false, applied: false })
  assert.equal(recoverPendingInbox({ config: { inboxRecovery: {} }, binding: { origin: 'observation' } }).not_live, true)
})

test('exact pending originals are archived before native canceled removals; foreign and next-step remain', () => {
  const foreign = { ...live('observation'), source: { kind: 'plugin:dsh-lanya-observation' } }
  const f = fixture([live('first'), foreign, live('second')])
  try {
    f.inbox.nextStep = [live('inflight-step')]
    const result = f.run(), receipt = f.result()
    assert.equal(result.success, true)
    assert.equal(result.removed_count, 2)
    assert.deepEqual(f.inbox.nextTurn, [foreign])
    assert.deepEqual(f.inbox.nextStep, [live('inflight-step')])
    assert.equal(receipt.native_cancellations.length, 2)
    assert.equal(receipt.native_disk_flush_verified, false)
    const original = JSON.parse(fs.readFileSync(path.join(f.directory, receipt.archive.filename), 'utf8'))
    assert.deepEqual(original.inbox.next_turn, [live('first'), foreign, live('second')])
    assert.equal(original.reviewed_originals.length, 2)
    assert.equal(receipt.budget_mutations, 0)
  } finally { f.cleanup() }
})

test('same completed plan is an in-memory no-op with no filesystem or PowerShell', () => {
  const f = fixture()
  try {
    f.run()
    const read = fs.lstatSync, open = fs.openSync, systemRoot = process.env.SystemRoot
    fs.lstatSync = () => { throw Error('cached call must not stat') }
    fs.openSync = () => { throw Error('cached call must not open') }
    process.env.SystemRoot = path.join(f.directory, 'nonexistent-windows')
    try {
      const cached = f.run()
      assert.equal(cached.cached, true)
      assert.equal(cached.applied, false)
      assert.equal(f.inbox.removeCalls, 2)
    } finally {
      fs.lstatSync = read; fs.openSync = open
      if (systemRoot === undefined) delete process.env.SystemRoot
      else process.env.SystemRoot = systemRoot
    }
  } finally { f.cleanup() }
})

test('already claimed original stays unknown while exact pending originals are archived', () => {
  const f = fixture()
  try {
    f.inbox.nextTurn.shift()
    f.inbox.nextStep.push(live('first'))
    const result = f.run(), receipt = f.result()
    assert.equal(result.success, false)
    assert.equal(result.no_longer_pending_count, 1)
    assert.equal(result.removed_count, 1)
    assert.equal(receipt.completed, true)
    assert.equal(receipt.safe_to_dispatch, true)
    assert.equal(receipt.no_longer_pending[0].status, 'unknown')
    assert.deepEqual(f.inbox.nextStep, [live('first')])
    assert.equal(f.run().cached, true)
    assert.equal(f.inbox.removeCalls, 1)
  } finally { f.cleanup() }
})

test('same-ID changed content refuses all mutation and failure is cached', () => {
  const f = fixture()
  try {
    f.inbox.nextTurn[1].content[0].text = 'changed synthetic input'
    assert.throws(f.run, { code: 'reviewed_pending_message_changed' })
    assert.equal(f.inbox.removeCalls, 0)
    assert.equal(f.result().content_match, false)
    const read = fs.lstatSync
    fs.lstatSync = () => { throw Error('failed plan must not be read again') }
    try { assert.throws(f.run, { code: 'reviewed_pending_message_changed' }) } finally { fs.lstatSync = read }
    assert.equal(f.inbox.removeCalls, 0)
  } finally { f.cleanup() }
})

test('foreign source, duplicate selection and wrong owner/session fail closed', () => {
  for (const kind of ['source', 'duplicate', 'owner', 'session']) {
    const f = fixture()
    try {
      if (kind === 'source') f.inbox.nextTurn[0].source = { kind: 'plugin:foreign' }
      if (kind === 'duplicate') { f.plan.selected.push(clone(f.plan.selected[0])); f.savePlan() }
      if (kind === 'owner') f.binding.owner = { ...f.binding.owner, agent_key: 'wrong-owner' }
      if (kind === 'session') f.chat.sessionId = 'different-session'
      assert.throws(f.run)
      assert.equal(f.inbox.removeCalls, 0)
    } finally { f.cleanup() }
  }
})

test('a recovery plan for another scope never blocks or changes that conversation', () => {
  const f = fixture()
  try {
    // A bad audit identity would reject the selected scope; unrelated scopes
    // must return before checking the audit ACL or writing any archive.
    f.plan.audit_identity = { deliberately_invalid: true }
    f.savePlan()
    for (const scope of ['private:7', 'group:8']) {
      f.binding.scope = scope
      assert.equal(f.run().not_target_scope, true)
      assert.equal(f.inbox.removeCalls, 0)
      assert.equal(fs.readdirSync(f.directory).length, 1)
    }
  } finally { f.cleanup() }
})

test('remove throwing after mutation preserves an unknown intent/result and never retries', () => {
  const f = fixture()
  try {
    f.inbox.afterRemove = () => { throw Error('synthetic listener failed after native mutation') }
    assert.throws(f.run)
    const receipt = f.result()
    assert.equal(receipt.status, 'unknown')
    assert.equal(receipt.mutation_attempted, true)
    assert.equal(receipt.mutation_may_have_occurred, true)
    assert.equal(receipt.safe_to_dispatch, false)
    assert.equal(f.inbox.nextTurn.length, 1)
    assert.throws(f.run, { code: 'recovery_previous_runtime_failure_no_retry' })
    assert.equal(f.inbox.removeCalls, 1)
  } finally { f.cleanup() }
})

test('persistent success can be checked by a new agent; raw originals remain', () => {
  const f = fixture()
  try {
    f.run()
    f.chat.agent = { ...f.chat.agent }
    const result = f.run()
    assert.equal(result.already_completed, true)
    assert.equal(result.applied, false)
    assert.equal(f.inbox.removeCalls, 2)
    assert.equal(fs.readdirSync(f.directory).filter(name => name.endsWith('.original.json')).length, 1)
  } finally { f.cleanup() }
})

test('unchanged installed SDK Inbox.remove produces the exact canceled event witness', {
  skip: !process.env.LANYA_TEST_AGENT_LOOP_FILE && 'Explicit readonly SDK source fixture not supplied',
}, () => {
  const source = fs.readFileSync(process.env.LANYA_TEST_AGENT_LOOP_FILE, 'utf8')
  const start = source.indexOf('var ReactLoopInbox = class {'), end = source.indexOf('//#endregion', start)
  assert.ok(start >= 0 && end > start)
  const Inbox = vm.runInNewContext(source.slice(start, end) + '\nReactLoopInbox')
  const f = fixture()
  try {
    const state = { 'next-turn': clone(f.inbox.nextTurn), 'next-step': [] }, discarded = []
    const session = f.chat.agent.session
    session.append = (type, data) => {
      const event = { seq: f.native.length, time: Date.now(), type, data: clone(data) }
      f.native.push(event)
      state[data.target].splice(data.start, data.removedCount || 0, ...data.inserted)
      return event
    }
    f.chat.agent.inbox = new Inbox({ stateOf: () => state }, session, {
      emit: (name, data) => { if (name === 'agent/inbox/discarded') discarded.push(data.message.id) },
    })
    const result = f.run(), receipt = f.result()
    assert.equal(result.removed_count, 2)
    assert.deepEqual(state['next-turn'], [])
    assert.deepEqual(discarded, ['first', 'second'])
    assert.equal(receipt.native_cancellations.length, 2)
    assert.deepEqual(receipt.native_cancellations.map(event => event.seq), [0, 1])
    assert.equal(receipt.native_disk_flush_verified, false)
  } finally { f.cleanup() }
})

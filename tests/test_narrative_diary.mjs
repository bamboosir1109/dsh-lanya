import test from 'node:test'
import assert from 'node:assert/strict'
import path from 'node:path'
import { NarrativeDiary } from '../plugin/lib/narrative-diary.js'

function fixture() {
  const owner = { cwd: path.resolve('fixture-workspace'), preset: 'Fixture', workspace_key: 'fixture-workspace', agent_key: 'fixture' }
  const record = { binding: { scope: 'private:42', revision: 1, owner } }, calls = []
  const a = {
    config: { memoryContextScript: path.join(owner.cwd, 'napcat', 'memory_context.py') },
    captureExec: () => record, trustedOwner: async () => owner,
    runtimeCapabilities: async () => ({ schema: 'dsh-runtime-capabilities/v1', read_only: true, owner,
      conversation_key: 'private:42', tools: { entertainment: { primary_identity_valid: true, primary_internal_leisure: true } } }),
    validateOwnerContext(value, schema, scope, expected) {
      assert.equal(value.schema, schema); assert.equal(value.conversation_key, scope)
      assert.equal(value.owner.workspace_key, expected.workspace_key); assert.equal(value.owner.agent_key, expected.agent_key)
    },
    policy: { snapshot: async () => ({ current: true, revision: 1, mode: 'silent' }) },
    checkBudget: async () => ({ allowed: true }), bridge: { effectiveCwd: () => owner.cwd },
    readTrustedHelper: async (script, request) => {
      calls.push({ script, request })
      return { schema: 'narrative-diary/v1', owner, conversation_key: 'private:42',
        ...(request.action === 'read' ? { read_only: true, private: true, status: 'empty', selected: null } : { status: 'committed', revision: 1, written: true }) }
    }
  }
  return { a, record, owner, calls, diary: new NarrativeDiary(a), exec: {} }
}

test('read uses trusted owner and fixed helper, silent private reflection grants no QQ output', async () => {
  const f = fixture(), value = await f.diary.execute({ action: 'read' }, f.exec)
  assert.equal(value.read_only, true); assert.equal(f.calls.length, 1)
  assert.equal(f.calls[0].script, path.join(f.owner.cwd, 'napcat', 'narrative_diary.py'))
  assert.equal(f.calls[0].request.conversation_key, 'private:42')
  assert.equal(f.calls[0].request.agent_preset, 'Fixture')
})

test('group, nonprimary, foreign owner and model-supplied authority never reach diary helper', async () => {
  for (const kind of ['group', 'nonprimary', 'owner', 'authority']) {
    const f = fixture()
    if (kind === 'group') f.record.binding.scope = 'group:42'
    if (kind === 'nonprimary') f.a.runtimeCapabilities = async () => ({ schema: 'dsh-runtime-capabilities/v1', read_only: true, owner: f.owner, conversation_key: 'private:42', tools: { entertainment: { primary_identity_valid: true, primary_internal_leisure: false } } })
    if (kind === 'owner') f.record.binding.owner = { ...f.owner, agent_key: 'foreign' }
    await assert.rejects(f.diary.execute({ action: 'read', ...(kind === 'authority' ? { scope: 'private:43' } : {}) }, f.exec))
    assert.equal(f.calls.length, 0)
  }
})

test('write key binds the logical facts revision, no model operation id or path accepted', async () => {
  const f = fixture(), args = { action: 'write', day: '2026-11-03', expected_revision: 0, fact_revision: 'a'.repeat(64), title: '一小段新发现', experiences: [{ text: '我读了字幕。', evidence_refs: ['b'.repeat(64)] }], feelings: '我有点好奇。', unfinished: '下次再看看。' }
  await f.diary.execute(args, f.exec); await f.diary.execute(args, f.exec)
  assert.match(f.calls[0].request.operation_id, /^[a-f0-9]{64}$/)
  assert.equal(f.calls[0].request.operation_id, f.calls[1].request.operation_id)
  await assert.rejects(f.diary.execute({ ...args, operation_id: 'c'.repeat(64) }, f.exec))
  assert.equal(f.calls.length, 2)
})

test('unknown helper result is not retried or called committed', async () => {
  const f = fixture(); let count = 0
  f.a.readTrustedHelper = async () => { count++; throw new Error('Timeout') }
  await assert.rejects(f.diary.execute({ action: 'read' }, f.exec), /Timeout/)
  assert.equal(count, 1)
})

test('newer policy and aborted turn refuse helper; switched owner cannot receive private result', async () => {
  const stale = fixture(); stale.a.policy.snapshot = async () => ({ current: true, revision: 2 })
  await assert.rejects(stale.diary.execute({ action: 'read' }, stale.exec)); assert.equal(stale.calls.length, 0)
  const aborted = fixture(); aborted.exec.signal = { aborted: true }
  await assert.rejects(aborted.diary.execute({ action: 'read' }, aborted.exec)); assert.equal(aborted.calls.length, 0)
  const changed = fixture(); let count = 0
  changed.a.trustedOwner = async () => count++ ? { ...changed.owner, agent_key: 'other' } : changed.owner
  await assert.rejects(changed.diary.execute({ action: 'read' }, changed.exec), /owner changed/)
})

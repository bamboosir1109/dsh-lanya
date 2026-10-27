/** Actual patched registration + synthetic MCP packets. No model/platform. */
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { spawnSync } from 'node:child_process'
import { createBiliMainAgent } from '../plugin/lib/bili-main-agent.js'

const repo = path.dirname(path.dirname(fileURLToPath(import.meta.url)))
const base = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-bili-main-fixtures-'))
after(() => fs.rmSync(base, { recursive: true, force: true }))
const oldOverride = process.env.LANYA_PROJECT_CONFIG
delete process.env.LANYA_PROJECT_CONFIG
after(() => { if (oldOverride === undefined) delete process.env.LANYA_PROJECT_CONFIG; else process.env.LANYA_PROJECT_CONFIG = oldOverride })
let sequence = 0
const execution = (callId = 'call-1') => ({ callId, agent: { session: { id: 'same-main-agent-session' } }, signal: new AbortController().signal })
const packet = value => ({ content: [{ type: 'text', text: JSON.stringify(value) }] })

function fixture(t) {
  const root = path.join(base, 'case-' + ++sequence)
  fs.mkdirSync(root)
  const projectConfig = path.join(root, 'project.json'), ledgerFile = path.join(root, 'private', 'bili-actions.sqlite')
  fs.writeFileSync(projectConfig, '{}')
  const calls = []
  let answer = request => request.params.name === 'bili_send_message'
    ? packet({ success: true, data: { data: { msg_key: 'fixture-receipt' } } })
    : packet({ videos: [{ bvid: 'BV1234567890', title: 'fixture actual fact' }] })
  const configure = mode => fs.writeFileSync(projectConfig, JSON.stringify({ decisions: { mode } }))
  const direct = createBiliMainAgent({ projectConfig, ledgerFile, call: async (name, args, options) => {
    const request = { method: 'tools/call', params: { name, arguments: args } }
    calls.push(request)
    const raw = await answer(request, options)
    if (raw?.isError) throw new Error('Fixture MCP error')
    return JSON.parse(raw.content[0].text)
  } })
  const tool = name => direct.tools().find(value => value.name === name)
  return { root, projectConfig, ledgerFile, calls, direct, tool, configure,
    set answer(value) { answer = value } }
}

test('replayed installed registration executes recommendation, details and private send in the same agent', async t => {
  const f = fixture(t), plugin = path.join(f.root, 'plugin')
  fs.cpSync(path.join(repo, 'tests/fixtures/bili_main'), plugin, { recursive: true })
  const args = ['-B', path.join(repo, 'scripts/patch_bili_main_agent.py'), '--plugin-dir', plugin,
    '--project-config', f.projectConfig, '--ledger-file', f.ledgerFile]
  const preview = spawnSync('python', args, { encoding: 'utf8', windowsHide: true })
  assert.equal(preview.status, 0, preview.stderr)
  const apply = spawnSync('python', [...args, '--apply', '--host-stopped', '--backup-dir', path.join(f.root, 'backups'),
    '--expected-fingerprint', JSON.parse(preview.stdout).fingerprint], { encoding: 'utf8', windowsHide: true })
  assert.equal(apply.status, 0, apply.stderr)
  const { McpClient } = await import(pathToFileURL(path.join(plugin, 'lib/mcp.mjs')).href)
  const tools = new Map(), packets = [], models = [], errors = []
  let entries
  const ctx = { tools: { register: spec => { tools.set(spec.name, spec); return () => {} } },
    mcp: new McpClient(async request => {
      packets.push(request)
      return request.params.name === 'bili_send_message'
        ? packet({ success: true, data: { code: 0, msg_key: 'main-agent-delivery-receipt' } })
        : packet({ videos: [{ bvid: 'BV1234567890', title: 'verified fixture title' }] })
    }), startLLM: why => { models.push(why); return { started: true } }, errors,
    inspect(value) { entries = value } }
  const { apply: activate } = await import(pathToFileURL(path.join(plugin, 'index.js')).href)
  activate(ctx)
  assert.deepEqual([...tools.keys()].sort(), ['bili_recommend', 'bili_run', 'bili_send_message', 'bili_video_info'])
  const exec = execution()
  const initial = await tools.get('bili_run').execute({}, exec)
  assert.equal(initial.mode, 'unified')
  assert.equal(initial.data.videos[0].title, 'verified fixture title')
  await tools.get('bili_video_info').execute({ bvid: initial.data.videos[0].bvid }, exec)
  const result = await tools.get('bili_send_message').execute({ receiver_uid: 101, text: 'fixture reply to observed evidence' }, exec)
  assert.equal(result.status, 'sent')
  assert.equal((await tools.get('bili_send_message').execute({ receiver_uid: 101, text: 'fixture reply to observed evidence' }, exec)).status, 'duplicate')
  entries.tick(); await entries.dmTick(); entries.runDuty(); entries.runDmReply(101, 'fixture')
  assert.equal(models.length, 0)
  assert.equal(errors.length, 0)
  assert.deepEqual(packets.map(value => value.params.name), ['bili_recommend', 'bili_video_info', 'bili_send_message'])
  assert.ok(packets.every(value => value.method === 'tools/call'))
  f.configure('legacy')
  assert.equal((await tools.get('bili_run').execute({}, execution('legacy-call'))).started, true)
  assert.equal(models.length, 1)
})

test('success requires a real receipt and survives bridge restart without a second send', async t => {
  const f = fixture(t), args = { receiver_uid: 101, text: 'private fixture text' }, exec = execution()
  assert.deepEqual(await f.tool('bili_send_message').execute(args, exec), { ok: true, status: 'sent', receipt: 'fixture-receipt' })
  const reloaded = createBiliMainAgent({ projectConfig: f.projectConfig, ledgerFile: f.ledgerFile,
    call() { throw new Error('a duplicate must never call MCP') } })
  const duplicate = await reloaded.tools().find(value => value.name === 'bili_send_message').execute(args, exec)
  assert.deepEqual(duplicate, { ok: false, status: 'duplicate', previous_status: 'sent' })
  assert.equal(f.calls.length, 1)
  assert.equal(fs.readFileSync(f.ledgerFile).includes(Buffer.from('private fixture text')), false)
})

for (const [label, value, status] of [
  ['negative ACK', { success: false, error: 'fixture rejected' }, 'failed'],
  ['nonzero code', { success: true, data: { code: 1, msg_key: 'invalid' } }, 'failed'],
  ['missing receipt', { success: true, data: {} }, 'unknown'],
  ['unstructured response', 'unconfirmed', 'unknown'],
]) {
  test(label + ' cannot claim delivery or automatically retry', async t => {
    const f = fixture(t), args = { receiver_uid: 101, text: 'fixture' }, exec = execution()
    f.answer = () => packet(value)
    assert.equal((await f.tool('bili_send_message').execute(args, exec)).status, status)
    assert.equal((await f.tool('bili_send_message').execute(args, exec)).status, 'duplicate')
    assert.equal(f.calls.length, 1)
  })
}

test('a transport exception is durable uncertainty and has no automatic second attempt', async t => {
  const f = fixture(t), args = { receiver_uid: 101, text: 'fixture' }, exec = execution()
  f.answer = () => { throw new Error('fixture timeout, may have sent') }
  assert.equal((await f.tool('bili_send_message').execute(args, exec)).status, 'unknown')
  assert.equal((await f.tool('bili_send_message').execute(args, exec)).previous_status, 'unknown')
  assert.equal(f.calls.length, 1)
})

test('malformed receipts never become sent, even with success true', async t => {
  const f = fixture(t)
  let calls = 0
  const invalid = [NaN, Infinity, -Infinity, 0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1, '', ' ', ' leading', 'line\nbreak', {}, [], 'x'.repeat(129)]
  for (const [index, receipt] of invalid.entries()) {
    const bridge = createBiliMainAgent({ projectConfig: f.projectConfig, ledgerFile: f.ledgerFile,
      call: async () => { calls++; return { success: true, data: { msg_key: receipt } } } })
    const send = bridge.tools().find(value => value.name === 'bili_send_message')
    const exec = execution('bad-receipt-' + index), args = { receiver_uid: 101, text: 'fixture' }
    assert.equal((await send.execute(args, exec)).status, 'unknown')
    assert.equal((await send.execute(args, exec)).previous_status, 'unknown')
  }
  assert.equal(calls, invalid.length)
})

test('parallel identical host executions reserve once, and changed args cannot borrow the identity', async t => {
  const f = fixture(t), args = { receiver_uid: 101, text: 'fixture' }, exec = execution()
  let release
  const waiting = new Promise(resolve => { release = resolve })
  f.answer = async () => { await waiting; return packet({ success: true, data: { msg_key: 'concurrent-receipt' } }) }
  const first = f.tool('bili_send_message').execute(args, exec)
  const second = await f.tool('bili_send_message').execute(args, exec)
  assert.equal(second.status, 'duplicate')
  assert.equal(second.previous_status, 'unknown')
  assert.equal((await f.tool('bili_send_message').execute({ ...args, text: 'changed' }, exec)).status, 'blocked')
  release()
  assert.equal((await first).status, 'sent')
  assert.equal(f.calls.length, 1)
})

test('a child crash after reserving a send survives restart as unknown without transport replay', async t => {
  const f = fixture(t), marker = path.join(f.root, 'fixture-transport-count')
  const code = `import fs from 'node:fs';import {createBiliMainAgent} from ${JSON.stringify(pathToFileURL(path.join(repo, 'plugin/lib/bili-main-agent.js')).href)};
const [projectConfig,ledgerFile,marker]=process.argv.slice(1);
const bridge=createBiliMainAgent({projectConfig,ledgerFile,call:async()=>{fs.writeFileSync(marker,'called-once');process.exit(0)}});
await bridge.tools().find(t=>t.name==='bili_send_message').execute({receiver_uid:101,text:'fixture'},{callId:'crashed-call',agent:{session:{id:'same-main-agent-session'}}});`
  const child = spawnSync('node', ['--input-type=module', '-e', code, f.projectConfig, f.ledgerFile, marker], { encoding: 'utf8', windowsHide: true })
  assert.equal(child.status, 0, child.stderr)
  assert.equal(fs.readFileSync(marker, 'utf8'), 'called-once')
  const result = await f.tool('bili_send_message').execute({ receiver_uid: 101, text: 'fixture' }, execution('crashed-call'))
  assert.equal(result.status, 'duplicate')
  assert.equal(result.previous_status, 'unknown')
  assert.equal(f.calls.length, 0)
})

test('bad project config cannot select legacy or make a platform request', async t => {
  const f = fixture(t)
  f.configure('legacy')
  assert.equal(f.direct.mode(), 'legacy')
  for (const raw of ['{broken', '[]', '{"decisions":{"mode":"typo"}}']) {
    fs.writeFileSync(f.projectConfig, raw)
    let models = 0
    await assert.rejects(() => f.direct.run({}, execution(), () => { models++; return {} }))
    assert.equal(models, 0)
    assert.equal(f.calls.length, 0)
  }
})

test('an unrelated legacy environment cannot replace the reviewed project configuration', async t => {
  const f = fixture(t), foreign = path.join(f.root, 'foreign-project.json')
  fs.writeFileSync(foreign, JSON.stringify({ decisions: { mode: 'legacy' } }))
  const prior = process.env.LANYA_PROJECT_CONFIG
  process.env.LANYA_PROJECT_CONFIG = foreign
  let models = 0
  try {
    const result = await f.direct.run({}, execution(), () => { models++; return { started: true } })
    assert.equal(result.mode, 'unified')
    assert.equal(models, 0)
    fs.unlinkSync(f.projectConfig)
    await assert.rejects(() => f.direct.run({}, execution(), () => { models++; return {} }))
    assert.equal(models, 0)
    assert.equal(f.calls.length, 1)
  } finally { if (prior === undefined) delete process.env.LANYA_PROJECT_CONFIG; else process.env.LANYA_PROJECT_CONFIG = prior }
})

test('validation and unrecognized/corrupt ledgers fail before contacting MCP', async t => {
  const f = fixture(t), send = f.tool('bili_send_message')
  for (const [args, exec] of [[{ receiver_uid: 0, text: 'fixture' }, execution()],
    [{ receiver_uid: 101, text: ' ' }, execution()], [{ receiver_uid: 101, text: 'fixture' }, {}]]) {
    await assert.rejects(() => send.execute(args, exec))
  }
  assert.throws(() => f.tool('bili_video_info').execute({ bvid: 'bad' }, execution()))
  await assert.rejects(async () => f.tool('bili_recommend').execute({ ps: 41 }, execution()))
  fs.mkdirSync(path.dirname(f.ledgerFile))
  fs.writeFileSync(f.ledgerFile, 'fixture damaged ledger')
  await assert.rejects(() => send.execute({ receiver_uid: 101, text: 'fixture' }, execution()))
  assert.equal(f.calls.length, 0)
})

test('ledger capacity refuses a new delivery without evicting old execution protection', async t => {
  const f = fixture(t)
  let calls = 0
  const bridge = createBiliMainAgent({ projectConfig: f.projectConfig, ledgerFile: f.ledgerFile, maxDeliveries: 1,
    call: async () => { calls++; return { success: true, data: { msg_key: 123 } } } })
  const send = bridge.tools().find(value => value.name === 'bili_send_message'), args = { receiver_uid: 101, text: 'fixture' }
  assert.equal((await send.execute(args, execution())).status, 'sent')
  await assert.rejects(() => send.execute(args, execution('new-call')), /ledger is full/)
  assert.equal((await send.execute(args, execution())).status, 'duplicate')
  assert.equal(calls, 1)
})

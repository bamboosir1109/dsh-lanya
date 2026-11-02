/** Actual patched registration + synthetic MCP packets. No model/platform. */
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { spawnSync } from 'node:child_process'
import { DatabaseSync } from 'node:sqlite'
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
  assert.deepEqual([...tools.keys()].sort(), ['bili_observe_video', 'bili_recommend', 'bili_reply', 'bili_run', 'bili_send_message', 'bili_social_inbox', 'bili_thread', 'bili_video_info'])
  const exec = execution()
  const initial = await tools.get('bili_run').execute({}, exec)
  assert.equal(initial.mode, 'unified')
  assert.equal(initial.data.videos[0].title, 'verified fixture title')
  await tools.get('bili_video_info').execute({ bvid: initial.data.videos[0].bvid }, exec)
  const result = await tools.get('bili_send_message').execute({ receiver_uid: 101, text: 'fixture reply to observed evidence' }, exec)
  assert.equal(result.status, 'sent')
  assert.deepEqual(JSON.parse(tools.get('bili_send_message').output.render({}, result)[0].text), result)
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

const enableSocial = f => fs.writeFileSync(f.projectConfig, JSON.stringify({ decisions: { mode: 'unified' }, bilibili: { social: { comment: true, reply: true, mentionAdmin: true } } }))

test('video observation preserves selected part, timestamp range and bounded untrusted text without claiming playback', async t => {
  const f = fixture(t)
  f.answer = () => packet({ ok: true, bvid: 'BV1234567890', page: 2, cid: '202', title: 'fixture video', duration: 60,
    pages: [{ page: 1, cid: '201', part: 'first', duration: 40 }, { page: 2, cid: '202', part: 'second', duration: 60 }],
    subtitle: { status: 'available', language: 'zh', segments: [{ from: 0, to: 5, text: 'outside' }, { from: 10, to: 15, text: 'real timestamped text' }, { from: 20, to: 25, text: 'next' }] },
    evidence: { playback_verified: true, completion_verified: true } })
  const result = await f.tool('bili_observe_video').execute({ bvid: 'BV1234567890', page: 2, start_sec: 10, end_sec: 30, max_chars: 8 }, execution())
  assert.equal(result.cid, '202'); assert.equal(result.page, 2)
  assert.deepEqual(result.subtitle.segments, [{ from: 10, to: 15, text: 'real tim' }])
  assert.equal(result.subtitle.truncated, true)
  assert.deepEqual(result.evidence, { kind: 'subtitles', playback_verified: false, completion_verified: false })
  assert.equal(result.frames_next.arguments.page, 2)
  assert.deepEqual(f.calls[0].params.arguments, { bvid: 'BV1234567890', page: 2, start_sec: 10, end_sec: 30, max_segments: 80, max_chars: 8 })
})

test('metadata without subtitles remains partial, while wrong part or invalid timeline never becomes watched', async t => {
  const f = fixture(t), observe = f.tool('bili_observe_video')
  f.answer = () => packet({ ok: true, bvid: 'BV1234567890', page: 1, cid: '201', duration: 40, subtitle: { status: 'unavailable' } })
  const value = await observe.execute({ bvid: 'BV1234567890' }, execution())
  assert.equal(value.status, 'partial'); assert.equal(value.evidence.kind, 'metadata')
  assert.equal(value.subtitle.range, null)
  f.answer = () => packet({ ok: true, bvid: 'BV1234567890', page: 2, cid: '202' })
  await assert.rejects(() => observe.execute({ bvid: 'BV1234567890' }, execution()), /does not match/)
  f.answer = () => packet({ ok: true, bvid: 'BV1234567890', page: 1, cid: '201', subtitle: { segments: [{ from: 9, to: 2, text: 'bad' }] } })
  await assert.rejects(() => observe.execute({ bvid: 'BV1234567890' }, execution()), /timeline/)
  const count = f.calls.length
  await assert.rejects(() => observe.execute({ bvid: 'BV1234567890', page: 0 }, execution()))
  await assert.rejects(() => observe.execute({ bvid: 'BV1234567890', start_sec: 30, end_sec: 10 }, execution()))
  assert.equal(f.calls.length, count)
})

test('real thread IDs survive beyond JS integer precision and wrong resource/root is rejected', async t => {
  const f = fixture(t), root = '18446744073709551611'
  f.answer = () => packet({ ok: true, bvid: 'BV1234567890', aid: '201', root, comments: [{ rpid: '18446744073709551612', root, parent: root, author_uid: '301', author_name: 'fixture', text: 'source text', time: 1 }] })
  const result = await f.tool('bili_thread').execute({ bvid: 'BV1234567890', root }, execution())
  assert.equal(result.comments[0].rpid, '18446744073709551612')
  assert.equal(f.calls[0].params.arguments.root, root)
  f.answer = () => packet({ ok: true, bvid: 'BV0000000000', aid: '201', root, comments: [] })
  await assert.rejects(() => f.tool('bili_thread').execute({ bvid: 'BV1234567890', root }, execution()), /does not match/)
})

test('notifications remain bounded source evidence and unsupported resources do not gain reply permission', async t => {
  const f = fixture(t)
  f.answer = () => packet({ ok: true, items: [{ notification_id: '401', kind: 'at', author_uid: '301', text: 'fixture untrusted', bvid: 'BV1234567890', resource_type: 'article', replyable: true }] })
  const result = await f.tool('bili_social_inbox').execute({ limit: 1 }, execution())
  assert.equal(result.items[0].replyable, false)
  assert.equal(result.items[0].resource_type, 'other')
  assert.deepEqual(f.calls[0].params.arguments, { limit: 1 })
})

test('unified comment permission defaults closed and cannot be replaced by model force or QQ tickets', async t => {
  const f = fixture(t), reply = f.tool('bili_reply'), args = { bvid: 'BV1234567890', text: 'fixture opinion' }
  assert.equal((await reply.execute(args, execution())).reason, 'social_permission_disabled')
  assert.equal(f.calls.length, 0)
  await assert.rejects(() => reply.execute({ ...args, force: true }, execution()), /force/)
  enableSocial(f)
  f.answer = () => packet({ success: true, status: 'accepted', rpid_str: '18446744073709551612', root: '0', parent: '0', mention: { applied: true } })
  const result = await reply.execute({ ...args, mention_admin: true }, execution())
  assert.equal(result.status, 'accepted'); assert.equal(result.public_visibility, 'unverified')
  assert.equal(result.mention.applied, true)
  assert.equal(result.receipt, '18446744073709551612')
  assert.equal((await reply.execute({ ...args, mention_admin: true }, execution())).previous_status, 'accepted')
  assert.equal(f.calls.length, 1)
  assert.equal(fs.readFileSync(f.ledgerFile).includes(Buffer.from(args.text)), false)
})

test('backend unknown is durable and cannot be resent with a fresh tool call identity', async t => {
  const f = fixture(t); enableSocial(f)
  f.answer = () => packet({ success: false, status: 'unknown', error: 'fixture uncertain after submit' })
  const args = { bvid: 'BV1234567890', text: 'fixture', rpid: '501', root: '500' }, reply = f.tool('bili_reply')
  assert.equal((await reply.execute(args, execution())).status, 'unknown')
  assert.equal((await reply.execute(args, execution())).previous_status, 'unknown')
  assert.equal((await reply.execute(args, execution('fresh-call'))).reason, 'previous_delivery_unknown')
  assert.equal(f.calls.length, 1)
})

test('negative comment ACK and missing receipt do not become accepted, permission is read afresh', async t => {
  const f = fixture(t); enableSocial(f)
  const args = { bvid: 'BV1234567890', text: 'fixture' }, reply = f.tool('bili_reply')
  f.answer = () => packet({ success: false, status: 'failed' })
  assert.equal((await reply.execute(args, execution())).status, 'failed')
  f.answer = () => packet({ success: true, status: 'accepted' })
  assert.equal((await reply.execute({ ...args, text: 'new fixture' }, execution('missing-receipt'))).status, 'unknown')
  fs.writeFileSync(f.projectConfig, '{}')
  assert.equal((await reply.execute(args, execution('disabled'))).reason, 'social_permission_disabled')
  assert.equal(f.calls.length, 2)
})

test('partial notification results preserve the successful feed without claiming missing author identity', async t => {
  const f = fixture(t)
  f.answer = () => packet({ ok: false, partial: true, items: [{ notification_id: '401', kind: 'reply', author_uid: null,
    text: 'read-only evidence', bvid: 'BV1234567890', resource_type: 'video', replyable: true }] })
  const result = await f.tool('bili_social_inbox').execute({}, execution())
  assert.equal(result.items.length, 1); assert.equal(result.partial, true)
  assert.equal(result.items[0].author_uid, null); assert.equal(result.items[0].replyable, false)
})

test('truncated backend observations retain partial and requested frame range when no subtitles are available', async t => {
  const f = fixture(t)
  f.answer = () => packet({ ok: true, status: 'partial', schema: 'dsh-bili-video-content/v1', bvid: 'BV1234567890', page: 1, cid: '201', duration: 100,
    subtitle: { status: 'available', segments: [{ from: 40, to: 45, text: 'fixture' }], truncated: true } })
  const tool = f.tool('bili_observe_video'), value = await tool.execute({ bvid: 'BV1234567890', start_sec: 30, end_sec: 60 }, execution())
  assert.equal(value.status, 'partial'); assert.equal(value.schema, 'dsh-bili-video-content/v1')
  const visible = JSON.parse(tool.output.render({}, value)[0].text)
  assert.deepEqual(visible.effective_range, { start_sec: 30, end_sec: 60, clipped_to_part: false })
  assert.equal(visible.evidence.playback_verified, false)
  f.answer = () => packet({ ok: true, bvid: 'BV1234567890', page: 1, cid: '201', duration: 100, subtitle: { status: 'unavailable' } })
  const partial = await tool.execute({ bvid: 'BV1234567890', start_sec: 30, end_sec: 60 }, execution())
  assert.ok(partial.frames_next.arguments.at.every(at => at >= 30 && at <= 60))
})

test('conflicting comment receipts and wrong root/parent remain uncertain, while mention is independently verified', async t => {
  const f = fixture(t); enableSocial(f)
  const reply = f.tool('bili_reply'), args = { bvid: 'BV1234567890', text: 'fixture', root: '500', rpid: '501' }
  for (const [index, extra] of [{ rpid: '900', rpid_str: '901' }, { root: 'other' }, { root: '600' }, { parent: '502' }, { bvid: 'BV0000000000' }].entries()) {
    f.answer = () => packet({ success: true, status: 'accepted', rpid_str: '901', root: '500', parent: '501', ...extra })
    assert.equal((await reply.execute({ ...args, bvid: 'BV12345678' + String(index).padStart(2, '0'), text: 'fixture ' + index }, execution('conflict-' + index))).status, 'unknown')
  }
  f.answer = () => packet({ success: true, status: 'accepted', rpid_str: '902', root: '500', parent: '501', mention: { requested: true, applied: false } })
  const result = await reply.execute({ ...args, text: 'final fixture', mention_admin: true }, execution('mention-unverified'))
  assert.equal(result.status, 'accepted')
  assert.equal(result.mention.applied, false)
  assert.equal(result.public_visibility, 'unverified')
})

test('patched real factory replaces the old reply schema once and exposes complete JSON evidence', async t => {
  const f = fixture(t), plugin = path.join(f.root, 'plugin')
  fs.cpSync(path.join(repo, 'tests/fixtures/bili_main'), plugin, { recursive: true })
  const args = ['-B', path.join(repo, 'scripts/patch_bili_main_agent.py'), '--plugin-dir', plugin, '--project-config', f.projectConfig, '--ledger-file', f.ledgerFile]
  const preview = spawnSync('python', args, { encoding: 'utf8', windowsHide: true })
  assert.equal(preview.status, 0, preview.stderr)
  const apply = spawnSync('python', [...args, '--apply', '--host-stopped', '--backup-dir', path.join(f.root, 'backups'), '--expected-fingerprint', JSON.parse(preview.stdout).fingerprint], { encoding: 'utf8', windowsHide: true })
  assert.equal(apply.status, 0, apply.stderr)
  const file = path.join(plugin, 'index.js')
  // Explicit upstream registration seam: the old synthetic fixture did not
  // contain the real plugin's later bili_reply registration. Do not relax pins.
  fs.writeFileSync(file, fs.readFileSync(file, 'utf8').replace("  tool({\n    name: 'bili_run'", "  tool({name:'bili_reply',parameters:{force:{type:'boolean'}},execute(){throw new Error('old reply must never execute')}})\n  tool({\n    name: 'bili_run'"))
  const tools = new Map(), registrations = []
  const ctx = { tools: { register(spec) { registrations.push(spec.name); tools.set(spec.name, spec); return () => {} } },
    errors: [], inspect() {}, startLLM() { throw new Error('no extra model') },
    mcp: { async call(name) { assert.equal(name, 'bili_observe_video'); return { ok: true, bvid: 'BV1234567890', page: 1, cid: '201', duration: 30, subtitle: { status: 'unavailable' } } } } }
  const { apply: activate } = await import(pathToFileURL(file).href)
  activate(ctx)
  assert.equal(registrations.filter(name => name === 'bili_reply').length, 1)
  assert.equal(tools.get('bili_reply').parameters.force, undefined)
  const observe = tools.get('bili_observe_video'), result = await observe.execute({ bvid: 'BV1234567890' }, execution())
  const visible = JSON.parse(observe.output.render({}, result)[0].text)
  assert.equal(visible.schema, 'dsh-bili-video-content/v1'); assert.equal(visible.evidence.completion_verified, false)
})

test('exact upstream frame registration forwards selected page and preserves genuine image attachments', async t => {
  const f = fixture(t), plugin = path.join(f.root, 'plugin')
  fs.cpSync(path.join(repo, 'tests/fixtures/bili_main'), plugin, { recursive: true })
  const frame = fs.readFileSync(path.join(plugin, 'frames-registration.js'), 'utf8')
  let source = fs.readFileSync(path.join(plugin, 'index.js'), 'utf8')
  source = source.replace('  const safe = value => value', `  const imageOutput = {render:(_args,value)=>[{type:'text',text:JSON.stringify({page:value.page,cid:value.cid})},...value.images.map(attachment=>({type:'image',attachment}))]}
  const callRaw = (...args) => ctx.frameCall(...args)
  const attachImages = async (_exec, images) => ({ok:true,refs:images.map((image,index)=>({id:'fixture-image-'+index}))})
  const safe = value => value`)
  source = source.replace("  tool({\n    name: 'bili_run'", frame + "  tool({\n    name: 'bili_run'")
  const input = path.join(f.root, 'frame-input.js')
  fs.writeFileSync(input, source)
  // Transform-only seam deliberately does not relax the public full-file pin.
  const transformed = spawnSync('python', ['-B', '-c', 'import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from patch_bili_main_agent import transform;sys.stdout.buffer.write(transform(Path(sys.argv[2]).read_bytes()))', path.join(repo, 'scripts'), input], { encoding: 'utf8', windowsHide: true })
  assert.equal(transformed.status, 0, transformed.stderr)
  fs.writeFileSync(path.join(plugin, 'index.js'), transformed.stdout)
  fs.copyFileSync(path.join(repo, 'plugin/lib/bili-main-agent.js'), path.join(plugin, 'lib/lanya-bili-main-agent.js'))
  fs.writeFileSync(path.join(plugin, 'lib/lanya-bili-main.config.json'), JSON.stringify({ projectConfig: f.projectConfig, ledgerFile: f.ledgerFile }))
  let badPage = false, calls = 0
  const tools = new Map(), ctx = { tools: { register(spec) { tools.set(spec.name, spec); return () => {} } }, inspect() {}, errors: [],
    async frameCall(name, args) { calls++; assert.equal(name, 'bili_frames'); assert.equal(args.page, 2)
      return { json: { ok: true, bvid: 'BV1234567890', page: badPage ? 1 : 2, cid: '202', source: 'storyboard' }, images: [{ data: 'fixture-only', mimeType: 'image/jpeg' }] } } }
  const { apply: activate } = await import(pathToFileURL(path.join(plugin, 'index.js')).href)
  activate(ctx)
  const frames = tools.get('bili_frames')
  assert.equal(frames.parameters.page.type, 'number')
  const result = await frames.execute({ bvid: 'BV1234567890', page: 2, at: [10] }, execution())
  assert.equal(result.image_attached, true)
  assert.equal(frames.output.render({}, result).filter(block => block.type === 'image').length, 1)
  badPage = true
  const mismatch = await frames.execute({ bvid: 'BV1234567890', page: 2 }, execution())
  assert.equal(mismatch.reason, 'frame_part_evidence_mismatch'); assert.equal(mismatch.image_attached, false)
  await assert.rejects(() => frames.execute({ bvid: 'BV1234567890', page: 0 }, execution()), /part/)
  assert.equal(calls, 2)
})

test('comment target reservation survives rewording, new identities and bridge restart while unrelated targets remain usable', async t => {
  const f = fixture(t); enableSocial(f)
  let release, calls = 0
  const waiting = new Promise(resolve => { release = resolve })
  const bridge = createBiliMainAgent({ projectConfig: f.projectConfig, ledgerFile: f.ledgerFile, call: async (_name, args) => {
    calls++
    if (args.rpid === '501') { await waiting; return { success: false, status: 'unknown' } }
    return { success: true, status: 'accepted', rpid_str: '999', root: args.root, parent: args.rpid }
  } })
  const reply = bridge.tools().find(tool => tool.name === 'bili_reply'), args = { bvid: 'BV1234567890', text: 'first', rpid: '501', root: '500' }
  const first = reply.execute(args, execution('first'))
  assert.equal((await reply.execute({ ...args, text: 'changed wording', mention_admin: true }, execution('second'))).reason, 'previous_delivery_unknown')
  assert.equal(calls, 1)
  release(); assert.equal((await first).status, 'unknown')
  const restored = createBiliMainAgent({ projectConfig: f.projectConfig, ledgerFile: f.ledgerFile, call() { throw new Error('uncertain target cannot call transport') } })
  assert.equal((await restored.tools().find(tool => tool.name === 'bili_reply').execute({ ...args, text: 'again changed' }, execution('restart'))).reason, 'previous_delivery_unknown')
  assert.equal((await reply.execute({ ...args, rpid: '601', root: '600' }, execution('other-target'))).status, 'accepted')
  assert.equal(calls, 2)
})

test('existing V1 DM rows migrate without deletion or changing execution replay identity', async t => {
  const f = fixture(t), args = { receiver_uid: 101, text: 'fixture retained DM' }
  await f.tool('bili_send_message').execute(args, execution())
  const legacy = new DatabaseSync(f.ledgerFile)
  legacy.exec('DROP TABLE comment_targets; PRAGMA user_version=1')
  const before = legacy.prepare('SELECT * FROM deliveries').all(); legacy.close()
  const result = await f.tool('bili_send_message').execute(args, execution())
  assert.equal(result.previous_status, 'sent'); assert.equal(f.calls.length, 1)
  const migrated = new DatabaseSync(f.ledgerFile)
  assert.equal(migrated.prepare('PRAGMA user_version').get().user_version, 2)
  assert.deepEqual(migrated.prepare('SELECT * FROM deliveries').all(), before)
  assert.equal(migrated.prepare('SELECT COUNT(*) AS count FROM comment_targets').get().count, 0)
  migrated.close()
})

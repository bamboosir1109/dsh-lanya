import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { nativeHost } from './native-host.mjs'
import * as nativeMedia from './native-media.mjs'

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..')
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-native-egress-'))
after(() => { assert.ok(path.basename(root).startsWith('lanya-native-egress-')); fs.rmSync(root, { recursive: true, force: true }) })
let count = 0
async function setup(t, options = {}) {
  const directory = path.join(root, 'case-' + ++count), plugin = path.join(directory, 'plugin')
  fs.mkdirSync(directory)
  const prepare = String.raw`
import json,shutil,sys
from pathlib import Path
repo,out=map(Path,sys.argv[1:3]);sys.path.insert(0,str(repo/'scripts'))
import patch_onebot_policy as policy,patch_onebot_replay as replay,patch_onebot_egress as egress
shutil.copytree(repo/'tests/fixtures/onebot_egress',out)
for name in ['bridge.js','outbound.js','tools.js','prompt.js']:
 p=out/'lib'/name;raw=policy.transform(name,p.read_bytes())
 if name=='bridge.js':raw=replay.transform('lib/bridge.js',raw)
 if name in ['bridge.js','outbound.js']:raw=egress.transform('lib/'+name,raw)
 p.write_bytes(raw)
# Upstream has no private cross-chat/profile block. Apply only the same existing
# V1 observe seam; this is a native-content assembly, never a forged install.
p=out/'lib/inbound.js';s=p.read_text(encoding='utf-8');a='        const nickname = sanitizeNickname(inbound.nickname);';assert s.count(a)==1
s=s.replace(a,'        const observed = await this.ctx.observeInbound(inbound);\n        if (!observed.accepted) return;\n'+a);p.write_text(s,encoding='utf-8')
for name,raw in policy.module_outputs({'stateFile':str(out.parent/'synthetic-policy.json')}).items():
 if name=='lanya-policy-adapter.js':raw=egress.transform('lib/'+name,raw)
 (out/'lib'/name).write_bytes(raw)
(out/egress.GENERATED).write_bytes((repo/'plugin/lib/qq-egress.js').read_bytes())
`
  const result = spawnSync('python', ['-B', '-c', prepare, repo, plugin], { encoding: 'utf8', windowsHide: true })
  assert.equal(result.status, 0, result.stderr)
  const h = await nativeHost(plugin, directory, options)
  t.after(() => h.close())
  return h
}

function stagedVoiceFixture(h) {
  fs.mkdirSync(h.deps.config.mediaDir, { recursive: true })
  const bytes = Buffer.alloc(44 + 1600)
  bytes.write('RIFF'); bytes.writeUInt32LE(bytes.length - 8, 4); bytes.write('WAVEfmt ', 8)
  bytes.writeUInt32LE(16, 16); bytes.writeUInt16LE(1, 20); bytes.writeUInt16LE(1, 22)
  bytes.writeUInt32LE(8000, 24); bytes.writeUInt32LE(8000 * 2, 28); bytes.writeUInt16LE(2, 32); bytes.writeUInt16LE(16, 34)
  bytes.write('data', 36); bytes.writeUInt32LE(1600, 40)
  const file = path.join(h.directory, 'original-local-synthesis.wav')
  fs.writeFileSync(file, bytes)
  let calls = 0
  // Synthetic PCM replaces only the helper/provider boundary; the compiled
  // adapter, real media roots/fence, tool body and egress/policy execute.
  h.bridge.lanyaPolicy.readTrustedHelper = async (_script, request) => {
    calls++
    return { schema: 'dsh-voice-result/v1', owner: {
      workspace_key: process.platform === 'win32' ? h.directory.toLowerCase() : h.directory, agent_key: 'fixture',
    }, conversation_key: request.conversation_key, request_id: request.request_id,
    status: 'ready', delivered: false, file, bytes: bytes.length,
    sha256: createHash('sha256').update(bytes).digest('hex'), duration_seconds: 0.1 }
  }
  return { file, bytes, calls: () => calls }
}

test('native member voice uses verified staging through real media fence without workspace access', async t => {
  const h = await setup(t, { nativeMedia, peer: '202' }), fixture = stagedVoiceFixture(h)
  await h.receive('local voice fixture'); h.start(); await h.decide()
  const roots = await h.bridge.mediaSendRoots(h.agent.session.id)
  assert.equal(roots.isTurnAdmin, false)
  assert.deepEqual([...roots.roots], [await fs.promises.realpath(h.deps.config.mediaDir)])
  await assert.rejects(() => nativeMedia.fileToBase64(fixture.file, 20 * 1024 * 1024, roots.roots), /path not allowed/)
  const synthExec = h.exec(), synth = h.tools.get('qq_synthesize_voice')
  const [result, reused] = await Promise.all([synth.execute({ text: 'neutral fixture' }, synthExec),
    synth.execute({ text: 'neutral fixture' }, synthExec)])
  assert.equal(result.file, reused.file)
  assert.deepEqual(fs.readdirSync(h.deps.config.mediaDir), [path.basename(result.file)])
  assert.equal(result.delivered, false)
  assert.equal(path.dirname(result.file), fs.realpathSync(h.deps.config.mediaDir))
  const canonical = fs.realpathSync.native(result.file)
  let alias = canonical
  if (process.platform === 'win32') {
    const query = spawnSync('python', ['-B', '-c', 'import ctypes,sys;b=ctypes.create_unicode_buffer(32768);n=ctypes.windll.kernel32.GetShortPathNameW(sys.argv[1],b,len(b));print(b.value if n and n<len(b) else sys.argv[1])', canonical],
      { encoding: 'utf8', windowsHide: true })
    assert.equal(query.status, 0)
    alias = query.stdout.trim()
    assert.equal(fs.realpathSync.native(alias).toLowerCase(), canonical.toLowerCase())
  }
  assert.equal((await h.tools.get('qq_send_voice').execute({ path: alias }, h.exec())).sent, true)
  const sent = h.calls.filter(call => call.action === 'send_msg')
  assert.equal(sent.length, 1)
  assert.equal(sent[0].params.message[0].data.file, 'base64://' + fixture.bytes.toString('base64'))
  for (const file of [result.file, path.join(path.dirname(result.file), path.basename(result.file).toUpperCase())]) {
    await assert.rejects(() => h.tools.get('qq_send_voice').execute({ path: file }, h.exec()), /voice_artifact_unavailable/)
  }
  await assert.rejects(() => synth.execute({ text: 'neutral fixture' }, synthExec), /voice_artifact_unavailable/)
  await h.receive('new turn'); h.start(); await h.decide()
  await assert.rejects(() => h.tools.get('qq_send_voice').execute({ path: result.file }, h.exec()), /voice_artifact_unavailable/)
  assert.equal(h.calls.filter(call => call.action === 'send_msg').length, 1)
})

test('native staged voice validates final encoded bytes after asynchronous media read', async t => {
  const h = await setup(t, { nativeMedia, peer: '202' }); stagedVoiceFixture(h)
  await h.receive('local voice fixture'); h.start(); await h.decide()
  const result = await h.tools.get('qq_synthesize_voice').execute({ text: 'neutral fixture' }, h.exec())
  const original = h.bridge.mediaSendRoots.bind(h.bridge)
  h.bridge.mediaSendRoots = async (...args) => {
    const roots = await original(...args), bytes = fs.readFileSync(result.file)
    bytes[bytes.length - 1] ^= 1; fs.writeFileSync(result.file, bytes)
    return roots
  }
  await assert.rejects(() => h.tools.get('qq_send_voice').execute({ path: result.file }, h.exec()), /Voice synthesis packet changed/)
  assert.equal(h.calls.filter(call => call.action === 'send_msg').length, 0)
})

test('native staged voice unknown is not resent under a new tool call', async t => {
  const h = await setup(t, { nativeMedia, peer: '202' }), fixture = stagedVoiceFixture(h)
  await h.receive('local voice fixture'); h.start(); await h.decide()
  const result = await h.tools.get('qq_synthesize_voice').execute({ text: 'neutral fixture' }, h.exec())
  h.deps.onTransmit = async () => { throw new Error('synthetic uncertain transport') }
  await assert.rejects(() => h.tools.get('qq_send_voice').execute({ path: result.file }, h.exec()), /unknown/)
  assert.equal(h.calls.filter(call => call.action === 'send_msg').length, 1)
  await assert.rejects(() => h.tools.get('qq_send_voice').execute({ path: result.file }, h.exec()), /voice_artifact_unavailable/)
  await assert.rejects(() => h.raw('send_private_msg', { user_id: '202', message: [{ type: 'record',
    data: { file: 'base64://' + fixture.bytes.toString('base64') } }] }), /voice_artifact_unavailable/)
  await assert.rejects(() => h.tools.get('qq_synthesize_voice').execute({ text: 'same synthetic audio' }, h.exec()), /voice_artifact_unavailable/)
  assert.equal(h.calls.filter(call => call.action === 'send_msg').length, 1)
})

test('native staged voice cancellation during media roots cannot transmit', async t => {
  const h = await setup(t, { nativeMedia, peer: '202' }); stagedVoiceFixture(h)
  await h.receive('local voice fixture'); h.start(); await h.decide()
  const result = await h.tools.get('qq_synthesize_voice').execute({ text: 'neutral fixture' }, h.exec())
  const controller = new AbortController(), original = h.bridge.mediaSendRoots.bind(h.bridge)
  h.bridge.mediaSendRoots = async (...args) => { const roots = await original(...args); controller.abort(); return roots }
  await assert.rejects(() => h.tools.get('qq_send_voice').execute({ path: result.file }, { ...h.exec(), signal: controller.signal }), /unbound_or_aborted_turn/)
  assert.equal(h.calls.filter(call => call.action.startsWith('send_')).length, 0)
})

test('native staged voice concurrent raw payload aliases cannot send the same content twice', async t => {
  const h = await setup(t, { nativeMedia, peer: '202' }), fixture = stagedVoiceFixture(h)
  await h.receive('local voice fixture'); h.start(); await h.decide()
  await h.tools.get('qq_synthesize_voice').execute({ text: 'neutral fixture' }, h.exec())
  const policy = h.bridge.lanyaPolicy.policy, original = policy.runGuarded.bind(policy)
  let count = 0, release
  const gate = new Promise(resolve => { release = resolve })
  policy.runGuarded = async (...args) => { if (++count === 2) release(); await gate; return original(...args) }
  const params = { user_id: '202', message: [{ type: 'record', data: { file: 'base64://' + fixture.bytes.toString('base64') } }] }
  const results = await Promise.allSettled([h.raw('send_private_msg', params), h.raw('send_private_msg', structuredClone(params))])
  assert.equal(results.filter(result => result.status === 'fulfilled').length, 1)
  assert.equal(results.filter(result => result.status === 'rejected').length, 1)
  assert.equal(h.calls.filter(call => call.action.startsWith('send_')).length, 1)
})

test('native staged voice cannot cross a newer silence or read a linked replacement', async t => {
  const h = await setup(t, { nativeMedia }), fixture = stagedVoiceFixture(h)
  await h.receive('local voice fixture'); h.start(); await h.decide()
  const result = await h.tools.get('qq_synthesize_voice').execute({ text: 'neutral fixture' }, h.exec())
  fs.unlinkSync(result.file); fs.linkSync(fixture.file, result.file)
  await assert.rejects(() => h.tools.get('qq_send_voice').execute({ path: result.file }, h.exec()), /artifact changed/)
  fs.unlinkSync(result.file); fs.copyFileSync(fixture.file, result.file)
  await h.receive('先别回复'); h.start(); await h.decide('silence', { control_update: true })
  await assert.rejects(() => h.tools.get('qq_send_voice').execute({ path: result.file }, h.exec()), /decision_required/)
  assert.equal(h.calls.filter(call => call.action === 'send_msg').length, 0)
})

test('actual native observeInbound freezes direct live RP and never grants it from quote/negative/model args', async (t) => {
  const h = await setup(t)
  const binding = await h.receive('我们来角色扮演，开始吧')
  assert.equal(binding.origin, 'live')
  assert.equal(binding.egressStyle.roleplay, true)
  h.start(); await h.decide(); await h.send('行（歪头）')
  assert.equal(h.calls.filter((c) => c.action === 'send_msg')[0].params.message[0].data.text, '行（歪头）')
})

test('native ordinary final, caption and raw terminal all share content shaping', async (t) => {
  const h = await setup(t)
  await h.receive('聊聊天'); h.start(); await h.decide()
  await h.send('我来啦（歪头）[表情: 笑]给你看![图](x.png)')
  await h.tools.get('qq_send_image').execute({ sources: ['https://example.invalid/synthetic.png'], caption: '好呀（歪头）[表情: 笑]' }, h.exec())
  const sent = h.calls.filter((c) => c.action === 'send_msg')
  assert.equal(sent[0].params.message[0].data.text, '我来啦给你看')
  assert.equal(sent[1].params.message[0].data.text, '好呀')
  assert.equal(sent[1].params.message.filter((s) => s.type === 'image').length, 1)
})

test('actual native final caption forward and raw API strip compound narration while keeping real content', async (t) => {
  const h = await setup(t)
  await h.receive('聊聊天'); h.start(); await h.decide()
  await h.send('我听着（尾巴在身后轻轻地摇来摇去，耳朵微微抖了抖）继续说')
  await h.tools.get('qq_send_image').execute({ sources: ['https://example.invalid/synthetic.png'],
    caption: '给你看（牙已经咬上去了，就是没使劲）' }, h.exec())
  await h.raw('send_msg', { user_id: 101, message: '再改（歪头，timeout 要改成 30）' })
  await h.raw('send_private_forward_msg', { user_id: 101, messages: [{ type: 'node', data: { name: 'fixture', uin: 999,
    content: [{ type: 'text', data: { text: '前（歪头\n抬起头\n叹了一口气）后' } }],
  } }] })
  await h.tools.get('qq_napcat_api').execute({ action: 'send_group_ai_record',
    params: { user_id: 101, text: '行【旁白：她没有开口，只是在等下一句话】', character: 'synthetic' } }, h.exec())
  const sends = h.calls.filter(c => c.action === 'send_msg')
  assert.deepEqual(sends.map(c => c.params.message.filter(s => s.type === 'text').map(s => s.data.text).join('')),
    ['我听着继续说', '给你看', '再改（timeout 要改成 30）'])
  assert.equal(sends.flatMap(c => c.params.message).filter(s => s.type === 'image').length, 1)
  assert.equal(h.calls.find(c => /forward/.test(c.action)).params.messages.flatMap(n => n.data.content).map(s => s.data.text).join(''), '前后')
  assert.equal(h.calls.find(c => c.action === 'send_group_ai_record').params.text, '行')
  const snapshot = await h.bridge.lanyaPolicy.policy.snapshot('private:101')
  assert.equal(new Set(snapshot.deliveries.map(d => d.ticketId)).size, 1)
})

test('actual native sliced quotations and ordinary empty-call parentheses retain their literal content', async (t) => {
  const h = await setup(t)
  await h.receive('聊聊天'); h.start(); await h.decide()
  const quote = '“' + '这一段只是引用原话。'.repeat(6) + '（歪头）”'
  await h.send(quote)
  assert.equal(h.calls.filter(c => c.action === 'send_msg').flatMap(c => c.params.message).map(s => s.data.text).join(''), quote)
  await h.send('调用 ready() 和 call( )；价格（含税）')
  assert.equal(h.calls.at(-1).params.message[0].data.text, '调用 ready() 和 call( )；价格（含税）')
})

test('native balanced image links leave no path fragments in final caption or raw text', async (t) => {
  const h = await setup(t)
  await h.receive('聊聊天'); h.start(); await h.decide()
  const image = '![图](https://example.invalid/a_(b).png "标题 (图)")'
  await h.send('前文' + image + '后文')
  await h.tools.get('qq_send_image').execute({ sources: ['https://example.invalid/real.png'], caption: '说明' + image + '结束' }, h.exec())
  await h.raw('send_msg', { user_id: 101, message: '原始' + image + '完整' })
  const sent = h.calls.filter((c) => c.action === 'send_msg')
  assert.deepEqual(sent.map((c) => c.params.message.filter((s) => s.type === 'text').map((s) => s.data.text).join('')), ['前文后文', '说明结束', '原始完整'])
  assert.equal(sent.flatMap((c) => c.params.message).filter((s) => s.type === 'image').length, 1)
  const previousCalls = h.calls.length
  await h.send(image)
  assert.equal(h.calls.length, previousCalls)
  const snapshot = await h.bridge.lanyaPolicy.policy.snapshot('private:101')
  assert.equal(new Set(snapshot.deliveries.map((d) => d.ticketId)).size, 1)
})

test('native long ordinary reply preserves 105 chars as 40/40/25 with one ticket', async (t) => {
  const h = await setup(t)
  await h.receive('聊聊天'); h.start(); await h.decide(); await h.send('哈'.repeat(105))
  const text = h.calls.filter((c) => c.action === 'send_msg').map((c) => c.params.message[0].data.text)
  assert.deepEqual(text.map((s) => s.length), [40, 40, 25])
  assert.equal(text.join(''), '哈'.repeat(105))
  const snapshot = await h.bridge.lanyaPolicy.policy.snapshot('private:101')
  assert.equal(new Set(snapshot.deliveries.map((d) => d.ticketId)).size, 1)
  assert.equal(new Set(snapshot.deliveries.map((d) => d.key)).size, 3)
})

test('native unsolicited activity link is empty without sending and requested file remains intact', async (t) => {
  const h = await setup(t)
  await h.receive('聊聊天'); h.start(); await h.decide()
  const reference = String.raw`[活动笔记](E:\Fixture workspace\character\notes\activity-primary.md)`
  await h.send(reference)
  assert.equal(h.calls.filter((c) => c.action === 'send_msg').length, 0)
  const requested = await setup(t)
  const binding = await requested.receive('请告诉我活动笔记文件路径')
  assert.equal(binding.egressStyle.localFiles, true)
  requested.start(); await requested.decide(); await requested.send(reference)
  const sent = requested.calls.filter((c) => c.action === 'send_msg')
  assert.equal(sent.length, 1)
  assert.equal(sent[0].params.message[0].data.text, reference)
})

test('native long English identifier remains one guarded part without truncation', async (t) => {
  const h = await setup(t)
  await h.receive('聊聊天'); h.start(); await h.decide()
  const word = 'SupercalifragilisticexpialidociousLongIdentifier'
  await h.send(word)
  const sent = h.calls.filter((c) => c.action === 'send_msg')
  assert.equal(sent.length, 1)
  assert.equal(sent[0].params.message[0].data.text, word)
})

test('native code and facts survive old Markdown/guard path intact', async (t) => {
  const h = await setup(t)
  await h.receive('请给我完整代码和步骤'); h.start(); await h.decide()
  const code = '```python\nprint("[表情: 笑]（歪头）![图](x.png)")\n```'
  await h.send(code)
  assert.equal(h.calls.filter((c) => c.action === 'send_msg')[0].params.message[0].data.text, code)
})

test('native direct negative, quotation, CQ reply and injected events cannot enable RP', async (t) => {
  for (const text of ['不要角色扮演', '“我们来角色扮演”是什么意思', '这里提到了角色扮演']) {
    const h = await setup(t)
    const binding = await h.receive(text)
    assert.equal(binding.egressStyle.roleplay, false)
    h.start(); await h.decide(); await h.send('好（歪头）')
    assert.equal(h.calls.at(-1).params.message[0].data.text, '好')
  }
  const h = await setup(t)
  const quoted = await h.receive('我们来角色扮演，开始吧', { message: [
    { type: 'reply', data: { id: 'synthetic-quote' } }, { type: 'text', data: { text: '我们来角色扮演，开始吧' } },
  ] })
  assert.equal(quoted.egressStyle.roleplay, false)
  const queued = h.bridge.lanyaPolicy.state(h.chat).queue.length
  await h.receive('我们来角色扮演，开始吧', { lanyaInjected: true })
  assert.equal(h.bridge.lanyaPolicy.state(h.chat).queue.length, queued)
})

test('native code containing literal forward markers stays code without a forward call', async (t) => {
  const h = await setup(t)
  await h.receive('请给我完整代码和步骤'); h.start(); await h.decide()
  const code = '```text\n[[qq_forward]]\n显示名\n合成文字（歪头）\n[[/qq_forward]]\n```'
  await h.send(code)
  assert.equal(h.calls.filter((c) => /forward/.test(c.action)).length, 0)
  assert.equal(h.calls.find((c) => c.action === 'send_msg').params.message[0].data.text, code)
  const data = '{"note":"（歪头）","sample":"[表情: 笑]","image":"![图](x.png)"}'
  await h.send(data)
  assert.equal(h.calls.at(-1).params.message[0].data.text, data)
})

test('native forward preserves more than 500 characters and shapes each node with the same ticket', async (t) => {
  const h = await setup(t)
  await h.receive('聊聊天'); h.start(); await h.decide()
  const body = '哈'.repeat(600) + '尾部完整哨兵'
  await h.send('[[qq_forward]]\n合成名称\n' + body + '（歪头）[表情: 笑]\n[[/qq_forward]]')
  const forwards = h.calls.filter((c) => /forward/.test(c.action))
  assert.equal(forwards.length, 1)
  const text = forwards[0].params.messages.flatMap((n) => n.data.content).map((s) => s.data.text)
  assert.equal(text.join(''), body)
  assert.ok(text.every((s) => s.length <= 40))
  const snapshot = await h.bridge.lanyaPolicy.policy.snapshot('private:101')
  assert.equal(snapshot.deliveries.length, 1)
  assert.equal(snapshot.deliveries[0].ticketId, h.bridge.lanyaPolicy.state(h.chat).active.ticket.id)
})

test('native media captions and raw forward media preserve ordering once without hidden slices', async (t) => {
  const h = await setup(t)
  await h.receive('聊聊天'); h.start(); await h.decide()
  await h.tools.get('qq_send_image').execute({ sources: ['https://example.invalid/synthetic.png'], caption: '哈'.repeat(82) + '（歪头）' }, h.exec())
  const sends = h.calls.filter((c) => c.action === 'send_msg')
  assert.deepEqual(sends.map((c) => c.params.message.filter((s) => s.type === 'text').map((s) => s.data.text).join('').length), [40, 40, 2])
  assert.equal(sends.flatMap((c) => c.params.message).filter((s) => s.type === 'image').length, 1)
  await h.raw('send_private_forward_msg', { user_id: 101, messages: [{ type: 'node', data: { name: 'fixture', uin: 999,
    content: [{ type: 'text', data: { text: '文'.repeat(81) } }, { type: 'image', data: { file: 'base64://synthetic' } },
      { type: 'record', data: { file: 'base64://synthetic-audio' } }, { type: 'text', data: { text: '末尾（歪头）' } }],
  } }] })
  const content = h.calls.at(-1).params.messages.flatMap((n) => n.data.content)
  assert.deepEqual(content.filter((s) => s.type !== 'text').map((s) => s.type), ['image', 'record'])
  assert.equal(content.filter((s) => s.type === 'text').map((s) => s.data.text).join(''), '文'.repeat(81) + '末尾')
  const snapshot = await h.bridge.lanyaPolicy.policy.snapshot('private:101')
  assert.equal(new Set(snapshot.deliveries.map((d) => d.ticketId)).size, 1)
  assert.equal(new Set(snapshot.deliveries.map((d) => d.key)).size, 4)
})

test('native unknown second caption slice stops remaining output and is partial rather than sent', async (t) => {
  const h = await setup(t)
  await h.receive('聊聊天'); h.start(); await h.decide()
  h.deps.onTransmit = async (action) => { if (action === 'send_msg' && h.calls.filter((c) => c.action === 'send_msg').length === 2) throw new Error('synthetic transport uncertainty') }
  await assert.rejects(() => h.tools.get('qq_send_image').execute({ sources: ['https://example.invalid/synthetic.png'], caption: '哈'.repeat(121) }, h.exec()),
    (error) => error.partial === true && error.status === 'unknown' && error.sent_parts === 1)
  const sends = h.calls.filter((c) => c.action === 'send_msg')
  assert.equal(sends.length, 2)
  assert.equal(sends.flatMap((c) => c.params.message).filter((s) => s.type === 'image').length, 0)
  const record = h.bridge.lanyaPolicy.state(h.chat).active
  assert.ok(record.outcomes.includes('partial'))
  const snapshot = await h.bridge.lanyaPolicy.policy.snapshot('private:101')
  assert.deepEqual(Array.from(snapshot.deliveries, (d) => d.status), ['sent', 'unknown'])
  assert.equal(new Set(snapshot.deliveries.map((d) => d.ticketId)).size, 1)
})

test('native silence between acknowledged slices revokes the fixed ticket without retry or RP relaxation', async (t) => {
  const h = await setup(t)
  await h.receive('聊聊天'); h.start(); await h.decide()
  const active = h.bridge.lanyaPolicy.state(h.chat).active
  const adapter = h.bridge.lanyaPolicy, original = adapter.guardConnectionCall.bind(adapter)
  let gated = 0
  adapter.guardConnectionCall = async (...args) => {
    const value = await original(...args)
    if (args[0] === 'send_msg' && ++gated === 1) {
      const stop = await adapter.policy.recordInbound({ scope: 'private:101', messageId: 'new-stop', sender: '101', origin: 'live', quoted: false, controlAllowed: true })
      await adapter.policy.decide({ scope: 'private:101', messageId: 'new-stop', expectedRevision: stop.revision, verdict: 'silence', controlUpdate: true, evidenceSource: 'current' })
    }
    return value
  }
  await assert.rejects(() => h.send('哈'.repeat(121)), (error) => error.partial === true && error.status === 'suppressed')
  assert.equal(h.calls.filter((c) => c.action === 'send_msg').length, 1)
  assert.equal(active.binding.egressStyle.roleplay, false)
  await h.receive('我们来角色扮演，开始吧')
  assert.equal(active.binding.egressStyle.roleplay, false)
  await assert.rejects(() => h.send('旧票（歪头）'))
  assert.equal(h.calls.filter((c) => c.action === 'send_msg').length, 1)
})

async function waitFor(check) {
  const deadline = Date.now() + 5000
  while (!check()) {
    if (Date.now() > deadline) throw new Error('Synthetic turn did not reach the expected phase')
    await new Promise((resolve) => setTimeout(resolve, 5))
  }
}

test('native final consumer completes pseudo-image-only observation as decided with zero sends', async (t) => {
  const h = await setup(t)
  const directory = path.join(h.directory, 'decision-inbox')
  const event = { version: 1, event_id: 'obs:synthetic-empty', scope: 'private:101', kind: 'fixture', producer: 'fixture', created_at: Date.now(),
    payload: { synthetic: true }, workspace_key: process.platform === 'win32' ? path.resolve(h.directory).toLowerCase() : path.resolve(h.directory),
    agent_key: 'fixture', origin: 'observation', control_allowed: false }
  await h.bridge.lanyaPolicy.pump.start({ poll: false })
  const name = createHash('sha256').update(event.scope + '\0' + event.event_id).digest('hex') + '.json'
  fs.mkdirSync(path.join(directory, 'pending'), { recursive: true })
  fs.writeFileSync(path.join(directory, 'pending', name), JSON.stringify(event) + '\n')
  const draining = h.bridge.lanyaPolicy.pump.drain()
  await waitFor(() => h.bridge.lanyaPolicy.state(h.chat).queue.length > 0)
  const binding = h.bridge.lanyaPolicy.state(h.chat).queue.at(-1)
  assert.equal(binding.origin, 'observation')
  assert.equal(binding.egressStyle, undefined)
  h.start(); await h.decide()
  const record = h.bridge.lanyaPolicy.state(h.chat).active
  assert.equal((await h.send('[表情: 笑]![图](x.png)')).length, 0)
  assert.deepEqual([...record.outcomes], [])
  h.assistant('[表情: 笑]![图](x.png)'); h.end()
  await draining
  const completed = JSON.parse(fs.readFileSync(path.join(directory, 'done', name), 'utf8'))
  assert.equal(completed.status, 'decided')
  assert.notEqual(completed.status, 'sent')
  assert.equal(h.calls.filter((c) => /^(send_|upload_)/.test(c.action)).length, 0)
  assert.equal((await h.bridge.lanyaPolicy.policy.snapshot('private:101')).deliveries.length, 0)
  assert.equal(h.bridge.lanyaPolicy.state(h.chat).turns.size, 0)
})

test('native T2I unknown never falls back to a second text delivery', async (t) => {
  const h = await setup(t)
  await h.receive('请给我完整代码'); h.start(); await h.decide()
  h.deps.config.textImageThreshold = 1
  h.deps.onTransmit = async (action) => { if (action === 'send_msg') throw new Error('synthetic unknown image delivery') }
  await assert.rejects(() => h.send('```text\n完整代码不能在未知投递后换正文重发\n```'), (error) => error.status === 'unknown')
  const sends = h.calls.filter((c) => c.action === 'send_msg')
  assert.equal(sends.length, 1)
  assert.equal(sends[0].params.message[0].type, 'image')
  assert.deepEqual(Array.from((await h.bridge.lanyaPolicy.policy.snapshot('private:101')).deliveries, (d) => d.status), ['unknown'])
})

test('native T2I pre-transport render error still falls back once with complete text', async (t) => {
  const h = await setup(t)
  await h.receive('请给我完整代码'); h.start(); await h.decide()
  h.deps.config.textImageThreshold = 1
  h.deps.onRender = () => { throw new Error('synthetic rendering failure') }
  const code = '```text\n完整代码[表情: 笑]（歪头）\n```'
  await h.send(code)
  const sends = h.calls.filter((c) => c.action === 'send_msg')
  assert.equal(sends.length, 1)
  assert.equal(sends[0].params.message[0].type, 'text')
  assert.equal(sends[0].params.message[0].data.text, code)
})

test('native whitelisted raw AI voice text shares the final content gate and ticket', async (t) => {
  const h = await setup(t)
  await h.receive('聊聊天'); h.start(); await h.decide()
  const value = await h.tools.get('qq_napcat_api').execute({ action: 'send_group_ai_record',
    params: { user_id: 101, text: '哈'.repeat(82) + '[表情: 笑]（歪头）', character: 'synthetic' } }, h.exec())
  const sends = h.calls.filter((c) => c.action === 'send_group_ai_record')
  assert.equal(sends.length, 3)
  assert.deepEqual(sends.map((c) => c.params.text.length), [40, 40, 2])
  assert.equal(value.lanya_egress.parts, 3)
  const before = sends.length
  await assert.rejects(() => h.tools.get('qq_napcat_api').execute({ action: 'send_group_ai_record',
    params: { user_id: 101, text: '[表情: 笑]![图](x.png)', character: 'synthetic' } }, h.exec()), (error) => error.status === 'suppressed')
  assert.equal(h.calls.filter((c) => c.action === 'send_group_ai_record').length, before)
  assert.equal(new Set((await h.bridge.lanyaPolicy.policy.snapshot('private:101')).deliveries.map((d) => d.ticketId)).size, 1)
})

test('native observation turn completion reports partial delivery as unknown rather than sent', async (t) => {
  const h = await setup(t)
  const event = { version: 1, event_id: 'obs:synthetic-partial', scope: 'private:101', kind: 'fixture', producer: 'fixture', created_at: Date.now(),
    payload: { synthetic: true }, workspace_key: process.platform === 'win32' ? path.resolve(h.directory).toLowerCase() : path.resolve(h.directory),
    agent_key: 'fixture', origin: 'observation', control_allowed: false }
  const completing = h.bridge.dispatchObservation(event)
  await waitFor(() => h.bridge.lanyaPolicy.state(h.chat).queue.length > 0)
  h.start(); await h.decide()
  const adapter = h.bridge.lanyaPolicy, original = adapter.guardConnectionCall.bind(adapter)
  let sent = false
  adapter.guardConnectionCall = async (...args) => {
    const result = await original(...args)
    if (args[0] === 'send_msg' && !sent) {
      sent = true
      const stop = await adapter.policy.recordInbound({ scope: 'private:101', messageId: 'synthetic-stop', sender: '101', origin: 'live', quoted: false, controlAllowed: true })
      await adapter.policy.decide({ scope: 'private:101', messageId: 'synthetic-stop', expectedRevision: stop.revision, verdict: 'silence', controlUpdate: true })
    }
    return result
  }
  await assert.rejects(() => h.send('哈'.repeat(82)), (error) => error.partial === true && error.status === 'suppressed')
  h.end()
  const completion = await completing
  assert.equal(completion.completed, true)
  assert.equal(completion.status, 'unknown')
  assert.notEqual(completion.status, 'sent')
  assert.equal(h.calls.filter((c) => c.action === 'send_msg').length, 1)
})

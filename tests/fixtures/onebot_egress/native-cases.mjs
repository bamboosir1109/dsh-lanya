import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { nativeHost } from './native-host.mjs'

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..')
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-native-egress-'))
after(() => { assert.ok(path.basename(root).startsWith('lanya-native-egress-')); fs.rmSync(root, { recursive: true, force: true }) })
let count = 0
async function setup(t) {
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
  const h = await nativeHost(plugin, directory)
  t.after(() => h.close())
  return h
}

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

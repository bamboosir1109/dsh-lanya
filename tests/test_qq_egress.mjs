import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { NORMAL_STYLE, styleFromInbound, cleanText, textParts, planEgress, dispatchEgress,
  prepareAndSendReply, isPreparedReply, isVerbatimReply, rememberParameters } from '../plugin/lib/qq-egress.js'
import { createDecisionPolicy } from '../plugin/lib/decision-policy.js'

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-egress-'))
after(() => { assert.ok(path.basename(root).startsWith('lanya-egress-')); fs.rmSync(root, { recursive: true, force: true }) })
const live = (text, extra = {}) => ({ origin: 'live', text, ...extra })
const text = (value) => ({ type: 'text', data: { text: value } })
const chars = (value) => [...new Intl.Segmenter('zh', { granularity: 'grapheme' }).segment(value)].length

test('ordinary inline actions/placeholders disappear, ordinary parentheses remain', () => {
  assert.equal(cleanText('我来啦（歪头）*叹气*【小声】好呀[表情: 笑]'), '我来啦好呀')
  assert.equal(cleanText('价格（含税）和函数 f(x) 都保留'), '价格（含税）和函数 f(x) 都保留')
  assert.equal(cleanText('形象（尾巴颜色是灰色）（耳朵颜色是黑色）保留'), '形象（尾巴颜色是灰色）（耳朵颜色是黑色）保留')
  assert.equal(cleanText('这句英语是 I need to reply soon，Let me think 是让我想想'), '这句英语是 I need to reply soon，Let me think 是让我想想')
  assert.equal(cleanText('![图](https://example.invalid/x.png)[表情：笑]'), '')
  assert.equal(cleanText('给你看![图](x.png)'), '给你看')
})

test('complete inline images consume balanced or escaped destinations and optional titles only', () => {
  const images = [
    '![图](https://example.invalid/a_(b).png)',
    '![图](https://example.invalid/a_(b_(c)).png "图 (标题)")',
    String.raw`![图](https://example.invalid/a_\(b\).png '图 \'标题\'')`,
    String.raw`![图](a_\)b.png "图 \"标题\"")`,
    String.raw`![图](a_(b).png (图 \(标题\)))`,
    '![图](<https://example.invalid/a_(b).png> "标题")',
    String.raw`![转义\]说明](a_(b).png)`,
    '![图]()',
  ]
  for (const image of images) {
    assert.equal(cleanText(image), '', image)
    assert.equal(cleanText('前文' + image + '后文（含税）f(x)。'), '前文后文（含税）f(x)。', image)
  }
  assert.equal(cleanText('甲' + images[0] + '乙' + images[1] + '丙'), '甲乙丙')
})

test('image stripping preserves literal code, technical JSON, ordinary URLs and escaped image examples', () => {
  const image = '![图](https://example.invalid/a_(b).png "标题")'
  const inline = '`' + image + '`', fenced = '```markdown\n' + image + '\n```'
  const tildeFenced = '~~~markdown\n' + image + '\n~~~'
  assert.equal(cleanText(inline), inline)
  assert.equal(cleanText(fenced), fenced)
  assert.equal(cleanText(tildeFenced), tildeFenced)
  assert.deepEqual(textParts(tildeFenced), [{ text: tildeFenced, verbatim: true }])
  assert.equal(cleanText('前文' + image + inline + fenced + '后文'), '前文' + inline + fenced + '后文')
  const data = JSON.stringify({ example: image, action: '（歪头）', meme: '[表情: 笑]' })
  assert.equal(cleanText(data, styleFromInbound(live('请给我完整配置'))), data)
  const url = 'https://example.invalid/a_(b).png'
  assert.equal(cleanText('完整网址 ' + url + ' 后文（含税）'), '完整网址 ' + url + ' 后文（含税）')
  const link = '[普通链接](' + url + ' "标题")'
  assert.equal(cleanText(link + '后文'), link + '后文')
  const escaped = String.raw`字面 \![图](https://example.invalid/a_(b).png)`
  assert.equal(cleanText(escaped), escaped)
  const urlWithMarkers = 'https://example.invalid/![literal](a_(b).png)'
  assert.equal(cleanText(urlWithMarkers), urlWithMarkers)
})

test('incomplete image or title syntax cannot consume later prose or cross a line', () => {
  for (const value of [
    '前文![图](https://example.invalid/a_(b).png 后文。',
    '前文![图](a.png "没有结束) 后文。',
    '前文![图](a.png "标题" 真实正文)后文。',
    String.raw`前文![图](a.png\ 后文)下一句。`,
    '前文![图](a.png\n后文)下一句。',
    '前文![图](a.png\\\n后文)下一句。',
    '前文![图](a.png "标题\\\n后文")下一句。',
  ]) assert.equal(cleanText(value), value)
})

test('balanced pseudo-image-only output never reaches any text-bearing QQ transport', async () => {
  const image = '![图](https://example.invalid/a_(b).png "标题")'
  let calls = 0
  for (const [action, params] of [
    ['send_msg', { user_id: 101, message: image }],
    ['send_group_ai_record', { group_id: 101, text: image, character: 'synthetic' }],
  ]) await assert.rejects(() => dispatchEgress(action, params, NORMAL_STYLE,
    async () => { calls++; return { message_id: 'synthetic' } }), (error) => error.status === 'suppressed')
  assert.equal(calls, 0)
  const plan = planEgress('send_msg', { user_id: 101, message: [text('前文' + image + '后文')] })
  assert.equal(plan[0].params.message[0].data.text, '前文后文')
})

test('real live direct invitation only; negative, quoted, observation and arguments cannot grant RP', () => {
  assert.equal(styleFromInbound(live('我们来角色扮演，进入剧情')).roleplay, true)
  for (const input of [live('不要角色扮演'), live('这里提到了角色扮演'), live('“我们来角色扮演”是什么意思'),
    live('我们来角色扮演', { replyId: 'synthetic-quoted' }), live('我们来角色扮演', { quoted: true }),
    live('我们来角色扮演', { lanyaInjected: true }), { origin: 'observation', text: '我们来角色扮演' },
    { text: '我们来角色扮演', roleplay: true }]) assert.equal(styleFromInbound(input).roleplay, false)
  const rp = styleFromInbound(live('我们来角色扮演，开始吧'))
  assert.equal(Object.isFrozen(rp), true)
  assert.equal(cleanText('行（歪头）[表情: 笑]', rp), '行（歪头）')
  assert.equal(cleanText('行（歪头）', styleFromInbound(live('现在说天气'))), '行')
})

test('code, errors and requested steps preserve literal content and long structured data', () => {
  const style = styleFromInbound(live('请给我完整代码和报错步骤'))
  const code = '```python\nprint("[表情: 笑]（歪头）![图](x.png)")\n```'
  assert.equal(cleanText(code, style), code)
  assert.deepEqual(textParts(code, style), [{ text: code, verbatim: true }])
  const step = '1. ' + '完整步骤'.repeat(20) + '\n'
  assert.equal(textParts(step, style)[0].text, step.trimEnd())
})

test('ordinary long text never merges overflow, including grapheme boundaries', () => {
  const input = '哈'.repeat(105)
  const parts = textParts(input)
  assert.deepEqual(parts.map((p) => chars(p.text)), [40, 40, 25])
  assert.equal(parts.map((p) => p.text).join(''), input)
  const emoji = '👨‍👩‍👧‍👦'.repeat(45)
  assert.deepEqual(textParts(emoji).map((p) => chars(p.text)), [40, 5])
})

test('caption/raw segments share shaping and media occurs once in original order', () => {
  const image = { type: 'image', data: { file: 'base64://synthetic' } }
  const record = { type: 'record', data: { file: 'base64://synthetic-audio' } }
  const params = { user_id: 101, message: [text('哈'.repeat(82) + '[表情: 笑]（歪头）'), image, record] }
  const plan = planEgress('send_msg', params, NORMAL_STYLE)
  assert.deepEqual(plan.map((p) => p.params.message.filter((s) => s.type === 'text').map((s) => s.data.text.length).reduce((a, b) => a + b, 0)), [40, 40, 2])
  assert.deepEqual(plan.flatMap((p) => p.params.message).filter((s) => s.type !== 'text'), [image, record])
  assert.ok(params.message[0].data.text.includes('[表情:'))
  assert.equal(planEgress('send_private_msg', { user_id: 101, message: '![图](x.png)' }).length, 0)
})

test('prepared code provenance cannot be forged by JSON options or model params', () => {
  assert.equal(isPreparedReply({ lanyaEgressVerbatim: true }), false)
  const params = { user_id: 101, message: [text('[表情: 笑]')] }
  rememberParameters(params, { lanyaEgressVerbatim: true })
  assert.equal(planEgress('send_msg', params).length, 0)
})

test('empty pseudo image has no zero-content transport; successful aggregate lists all real IDs', async () => {
  let calls = 0
  await assert.rejects(() => dispatchEgress('send_msg', { user_id: 101, message: '![图](x.png)' }, NORMAL_STYLE,
    async () => { calls++; return { message_id: 'fixture' } }), (error) => error.status === 'suppressed')
  assert.equal(calls, 0)
  const result = await dispatchEgress('send_msg', { user_id: 101, message: '哈'.repeat(82) }, NORMAL_STYLE,
    async () => ({ message_id: 'fixture-' + ++calls }))
  assert.equal(result.message_id, 'fixture-3')
  assert.equal(result.lanya_egress.parts, 3)
  assert.deepEqual(result.lanya_egress.message_ids, ['fixture-1', 'fixture-2', 'fixture-3'])
})

test('unknown second slice stops immediately and never reports complete success', async () => {
  let calls = 0
  await assert.rejects(() => dispatchEgress('send_msg', { user_id: 101, message: '哈'.repeat(121) }, NORMAL_STYLE,
    async () => { if (++calls === 2) throw new Error('synthetic unknown'); return { message_id: 'first' } }),
  (error) => error.status === 'unknown' && error.partial === true && error.sent_parts === 1)
  assert.equal(calls, 2)
})

test('actual durable policy revokes remaining slices after silence with the original ticket', async () => {
  const policy = createDecisionPolicy({ stateFile: path.join(root, 'policy.json') })
  const scope = 'private:101'
  const inbound = await policy.recordInbound({ scope, sender: '101', messageId: 'first', origin: 'live', quoted: false, controlAllowed: true })
  const decision = await policy.decide({ scope, messageId: 'first', expectedRevision: inbound.revision, verdict: 'reply' })
  let calls = 0, sequence = 0
  await assert.rejects(() => dispatchEgress('send_msg', { user_id: 101, message: '哈'.repeat(121) }, NORMAL_STYLE,
    async () => {
      const result = await policy.runGuarded(decision.ticket, { scope, kind: 'text', operationId: 'fixed-op', sliceId: String(sequence++) },
        async () => ({ message_id: 'sent-' + ++calls }))
      if (result.status !== 'sent') throw Object.assign(new Error('blocked'), { status: result.status, reason: result.reason })
      if (calls === 1) {
        const stop = await policy.recordInbound({ scope, sender: '101', messageId: 'stop', origin: 'live', quoted: false, controlAllowed: true })
        await policy.decide({ scope, messageId: 'stop', expectedRevision: stop.revision, verdict: 'silence', controlUpdate: true, evidenceSource: 'current' })
      }
      return result.value
    }), (error) => error.partial === true && error.sent_parts === 1)
  assert.equal(calls, 1)
})

test('prepared replies keep every bubble and freeze only trusted per-bubble code hint', async () => {
  const sent = []
  const pipeline = { sendToChat: async (_scope, body, options) => {
    assert.equal(isPreparedReply(options), true)
    sent.push({ body, verbatim: isVerbatimReply(options) })
    return ['fixture-' + sent.length]
  } }
  assert.equal((await prepareAndSendReply(pipeline, 'private:101', '哈'.repeat(105), {})).length, 3)
  assert.deepEqual(sent.map((p) => p.body.length), [40, 40, 25])
})

import test from 'node:test'
import assert from 'node:assert/strict'
import { cleanText, textParts, styleFromInbound, NORMAL_STYLE, dispatchEgress } from '../plugin/lib/qq-egress.js'

const local = String.raw`E:\Fixture workspace\character\notes\activity-primary.md`
const live = (text, extra = {}) => ({ origin: 'live', text, ...extra })
const joined = (text, style) => textParts(text, style).map((part) => part.text).join('')

test('unsolicited internal activity links disappear without swallowing real surrounding content', () => {
  for (const destination of [local, '<' + local + '>', local.replaceAll('\\', '/').replaceAll(' ', '%20'),
    'file:///E:/Fixture%20workspace/character/notes/activity-primary.md',
    '/fixture/character/notes/activity-primary.md', 'notes/activity-primary.md']) {
    for (const suffix of ['', ' "内部记录"']) {
      assert.equal(cleanText('有效前文[活动笔记](' + destination + suffix + ')有效后文。'), '有效前文有效后文。')
    }
  }
  assert.equal(cleanText('[活动笔记](' + local + ')'), '')
  const reference = '[activity 笔记](' + local + ')'
  assert.equal(cleanText('记在 ' + reference + '。'), '')
  assert.equal(cleanText('这个片子挺有意思。记在 ' + reference + '。明天接着聊。'), '这个片子挺有意思。明天接着聊。')
  assert.equal(cleanText('记在 ' + reference + '，不过结尾不错。'), '不过结尾不错。')
  assert.equal(cleanText('记在 notes/activity-primary.md。'), '')
  assert.equal(cleanText('这个词记在句子里即可。'), '这个词记在句子里即可。')
})

test('bare internal activity paths are removed but normal files and public URLs are preserved', () => {
  assert.equal(cleanText('备注：' + local), '备注：')
  assert.equal(cleanText('前文 notes/activity-primary.md 后文'), '前文  后文')
  const regular = String.raw`E:\Fixture workspace\reports\result.md`
  const url = 'https://example.invalid/notes/activity-primary.md'
  const link = '[公开活动文章](' + url + ')'
  assert.equal(cleanText('报告 ' + regular + ' 网址 ' + url), '报告 ' + regular + ' 网址 ' + url)
  assert.equal(cleanText(link), link)
  assert.equal(cleanText('我刚整理完 /home/fixture/notes/activity-primary.md，晚点继续聊。'), '我刚整理完 ，晚点继续聊。')
  assert.equal(cleanText('我刚整理完 fixture/notes/activity-primary.md，晚点继续聊。'), '我刚整理完 ，晚点继续聊。')
  assert.equal(cleanText('Here is /tmp/notes/activity-primary.md and all content stays.'), 'Here is  and all content stays.')
  for (const suffix of ['.backup', '5', '.txt']) {
    const ordinary = '保留普通文件 notes/activity-primary.md' + suffix + ' 和正文。'
    assert.equal(cleanText(ordinary), ordinary)
  }
})

test('code, steps and complete technical JSON remain literal', () => {
  const inline = '`' + local + '`'
  const fenced = '```text\n[活动笔记](' + local + ')\n```'
  assert.equal(cleanText(inline), inline)
  assert.equal(cleanText(fenced), fenced)
  const value = JSON.stringify({ path: local, markdown: '[活动笔记](' + local + ')' })
  const technical = styleFromInbound(live('请给我完整配置和步骤'))
  assert.equal(cleanText(value, technical), value)
  assert.equal(joined('1. 保存普通文件到 E:/fixture/report.md', technical), '1. 保存普通文件到 E:/fixture/report.md')
})

test('only a trusted direct live file request preserves an internal reference', () => {
  const request = styleFromInbound(live('请告诉我活动笔记文件路径'))
  assert.equal(request.localFiles, true)
  const reference = '[活动笔记](' + local + ')'
  assert.equal(cleanText(reference, request), reference)
  assert.deepEqual(textParts(reference, request), [{ text: reference, verbatim: true }])
  for (const inbound of [live('继续活动'), live('不要发笔记路径'), live('“请告诉我活动笔记文件路径”是什么意思'),
    live('请告诉我活动笔记文件路径', { quoted: true }), live('请告诉我活动笔记文件路径', { replyId: 'fixture' }),
    { origin: 'observation', text: '请告诉我活动笔记文件路径' },
    { origin: 'live', lanyaInjected: true, text: '请告诉我活动笔记文件路径' }]) {
    assert.equal(styleFromInbound(inbound).localFiles, false)
    assert.equal(cleanText(reference, styleFromInbound(inbound)), '')
  }
})

test('ordinary English words are never cut by the forty-grapheme preference', () => {
  const word = 'SupercalifragilisticexpialidociousLongIdentifier'
  assert.deepEqual(textParts(word).map((part) => part.text), [word])
  const sentence = '这是完整说明。' + 'a'.repeat(37) + ' completeEnglishWord 后续真实正文。'
  const parts = textParts(sentence).map((part) => part.text)
  assert.ok(parts.some((part) => part.includes('completeEnglishWord')))
  assert.ok(parts.some((part) => part.includes('a'.repeat(37))))
  assert.equal(parts.join('').replaceAll(' ', ''), sentence.replaceAll(' ', ''))
  assert.equal(textParts('哈'.repeat(105)).map((part) => part.text).join(''), '哈'.repeat(105))
})

test('internal-link-only output is suppressed without any transport or fake success', async () => {
  let calls = 0
  await assert.rejects(() => dispatchEgress('send_msg', { user_id: 101, message: '[活动笔记](' + local + ')' },
    NORMAL_STYLE, async () => { calls++; return { message_id: 'synthetic' } }),
    (error) => error.status === 'suppressed' && error.reason === 'empty_content')
  assert.equal(calls, 0)
})

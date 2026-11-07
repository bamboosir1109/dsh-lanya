/** Real reviewed parser/pipeline with inert transport and numeric mention IDs. */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { spawnSync } from 'node:child_process'

const repo = path.dirname(path.dirname(fileURLToPath(import.meta.url)))
const prefix = path.join(os.tmpdir(), 'lanya-mention-compat-')
const root = fs.mkdtempSync(prefix)
test.after(() => {
  assert.ok(path.resolve(root).startsWith(path.resolve(prefix)))
  fs.rmSync(root, { recursive: true, force: true })
})
const prepare = String.raw`
import shutil,sys
from pathlib import Path
repo,out=map(Path,sys.argv[1:]);sys.path.insert(0,str(repo/'scripts'))
import patch_onebot_inbound_context as context
import patch_onebot_mentions as mention
shutil.copytree(repo/'tests/fixtures/onebot_egress/lib',out/'lib')
(out/'package.json').write_text('{"type":"module"}',encoding='utf-8')
for name,body in {'media.js':"export const extForInboundName=()=>'.bin'",'stt.js':"export const transcriptLabel=x=>x",'trace.js':"export const currentTrace=()=>undefined;export const TRACE_REASONS={}",'errors.js':"export const describeError=e=>e?.name||'Error'"}.items():
 (out/'lib'/name).write_text(body,encoding='utf-8')
f=out/'lib/inbound.js';s=f.read_text(encoding='utf-8');a='        const nickname = sanitizeNickname(inbound.nickname);'
assert s.count(a)==1
s='// LANYA_POLICY_PATCH_V1\n'+s.replace(a,'        const observed = await this.ctx.observeInbound(inbound);\n        if (!observed.accepted) return;\n'+a)
f.write_bytes(context.transform('inbound.js',s.encode('utf-8')))
f=out/'lib/cq.js';f.write_bytes(mention.transform(f.read_bytes()))
`
const built = spawnSync('python', ['-B', '-c', prepare, repo, root], { encoding: 'utf8', windowsHide: true })
assert.equal(built.status, 0, built.stderr)
const { InboundPipeline, normalizeOneBot11 } = await import(pathToFileURL(path.join(root, 'lib/inbound.js')).href)
const { detectMention } = await import(pathToFileURL(path.join(root, 'lib/cq.js')).href)
const text = value => ({ type: 'text', data: { text: value } })
const at = qq => ({ type: 'at', data: { qq } })
const reply = qq => ({ type: 'reply', data: { id: '41', ...(qq === undefined ? {} : { qq }) } })
function fixture(bot = '999') {
  const observed = [], dispatched = [], calls = []
  const ctx = { config: { ignoreSelf: true, requireMention: true, botQQ: bot, rateLimitPerMinute: 0 },
    policy: { dmPolicy: 'open', allowAllUsers: true, groupPolicy: 'open', adminUsers: ['101'] },
    selfId: () => bot, getChat: () => undefined, getSettings: () => ({}),
    sweepIdleChats: async () => {}, log: () => {}, media: { cleanupExpired: async () => {} },
    observeInbound: async inbound => { observed.push(structuredClone(inbound)); return { accepted: true } },
    tryHandleCommand: async () => false, buildBody: async value => value,
    call: async (action, params) => { calls.push({ action, params }); return { message_id: 41,
      message_type: 'group', group_id: 303, sender: { user_id: 202 }, message: [text('synthetic quote')] } },
    dispatchFollowup: async (...args) => dispatched.push(args) }
  const pipeline = new InboundPipeline(ctx)
  ctx.expandQuote = (...args) => pipeline.expandQuote(...args)
  const receive = async (message, extra = {}) => {
    const event = { message_type: 'group', user_id: 101, group_id: 303, message_id: 42,
      sender: { nickname: 'Synthetic member' }, message, ...extra }
    await pipeline.processInbound(normalizeOneBot11(event))
    return dispatched.at(-1)?.[1]
  }
  return { ctx, observed, dispatched, calls, receive }
}
const address = value => JSON.parse(/<trusted_qq_addressing>(.*?)<\/trusted_qq_addressing>/.exec(value)[1])

test('numeric and string self mentions admit the same actual group pipeline input', async () => {
  for (const qq of ['999', 999]) {
    const h = fixture(), segments = [at(qq), text('synthetic direct question')], before = structuredClone(segments)
    const output = await h.receive(segments)
    assert.equal(h.observed.length, 1); assert.equal(h.dispatched.length, 1)
    assert.equal(address(output).bot_addressed, 'explicit_at')
    assert.deepEqual(address(output).explicit_at_ids, ['999'])
    assert.deepEqual(segments, before)
  }
})

test('numeric and string mentions of another member do not admit the bot', async () => {
  for (const qq of ['998', 998]) {
    const h = fixture()
    await h.receive([at(qq), text('synthetic question to somebody else')])
    assert.equal(h.observed.length, 0); assert.equal(h.dispatched.length, 0)
  }
})

test('@all remains admitted only when the bot identity is known', async () => {
  const h = fixture(), output = await h.receive([at('all'), text('synthetic group notice')])
  assert.equal(address(output).bot_addressed, 'at_all')
  const unknown = fixture(''); await unknown.receive([at('all'), text('synthetic notice')])
  assert.equal(unknown.observed.length, 0)
})

test('quoted text spelling an @ is never promoted to a current mention', async () => {
  const h = fixture()
  await h.receive([reply(202), text('[CQ:at,qq=999] quoted or typed text')], { raw_message: '[CQ:at,qq=999]' })
  assert.equal(h.observed.length, 0); assert.equal(h.dispatched.length, 0); assert.equal(h.calls.length, 0)
})

test('reply target numeric identity matches strings without treating other authors as the bot', () => {
  for (const qq of ['999', 999]) assert.equal(detectMention([reply(qq)], '', '999', ''), true)
  for (const qq of ['202', 202]) assert.equal(detectMention([reply(qq)], '', '999', ''), false)
})

test('the legacy missing-qq reply gate still needs independently verified bot authorship', async () => {
  const h = fixture(), output = await h.receive([reply(), text('synthetic follow up')])
  assert.equal(h.dispatched.length, 1)
  assert.equal(address(output).reply_to, 'other')
  assert.equal(address(output).bot_addressed, 'not_proven')
})

test('boolean, object, nonpositive and unsafe numeric IDs cannot impersonate a bot', () => {
  for (const qq of [true, false, { toString: () => '999' }, 0, -1, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    assert.equal(detectMention([at(qq)], '', '999', ''), false)
    assert.equal(detectMention([reply(qq)], '', '999', ''), false)
  }
  assert.equal(detectMention([at(Number.MAX_SAFE_INTEGER + 1)], '', '9007199254740992', ''), false)
  assert.equal(detectMention([at('9007199254740992')], '', '9007199254740992', ''), true)
})

test('CQ-string fallback and ordinary private input retain existing behavior', async () => {
  assert.equal(detectMention(undefined, '[CQ:at,qq=999]hello', '999', ''), true)
  assert.equal(detectMention(undefined, '[CQ:at,qq=998]hello', '999', ''), false)
  assert.equal(detectMention([at(999)], '', '', '999'), true)
  const h = fixture(); await h.receive([text('synthetic DM question')], { message_type: 'private' })
  assert.equal(h.observed.length, 1); assert.equal(h.dispatched.length, 1)
})

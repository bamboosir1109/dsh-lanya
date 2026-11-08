/** Real temporary SQLite journals and inert transport callbacks, never QQ. */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createHash } from 'node:crypto'
import { DatabaseSync } from 'node:sqlite'
import { AccountActivityStore, accountActivitySettings, accountPublicationText, accountPublicationAction } from '../plugin/lib/account-activity.js'

const hash = value => createHash('sha256').update(value).digest('hex')
function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'account-activity-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  let time = Date.parse('2026-11-08T01:00:00Z')
  const store = new AccountActivityStore(path.join(root, 'private', 'activity.sqlite'), hash('fixture account'), () => time)
  const settings = accountActivitySettings({ qqBackend: { accountActivity: { enabled: true } } }, ['account_post', 'account_signature'])
  const request = (kind = 'post', text = '今天发现了一首好听的歌。', key = 'first') => ({ key: hash(key), kind, text, audience: kind === 'post' ? 'friends' : 'profile' })
  return { root, store, settings, request, advance: ms => { time += ms } }
}

test('provider settings are opt-in, reject invalid scope and distinguish unsupported posts', () => {
  assert.equal(accountActivitySettings({}, ['account_post', 'account_signature']).enabled, false)
  assert.equal(accountActivitySettings({}, ['account_signature']).post_supported, false)
  for (const input of [null, { audience: 'public' }, { enabled: 'yes' }, { dailyPosts: 0 }, { postIntervalHours: 0 }, { secret: 'ignored?' }]) {
    assert.throws(() => accountActivitySettings({ qqBackend: { accountActivity: input } }, ['account_post', 'account_signature']))
  }
  assert.equal(accountPublicationAction('get_qzone_msg_list'), false)
  for (const action of ['send_qzone_msg', 'set_self_longnick', 'set_qq_profile', 'delete_qzone_msg']) assert.equal(accountPublicationAction(action), true)
})

test('short original updates permit ordinary paragraphs but refuse private routes and identifiers', () => {
  assert.equal(accountPublicationText('风好舒服。\n想出去走走。', 'post'), '风好舒服。\n想出去走走。')
  for (const text of [' 话 ', '号码123456', 'https://example.test', 'C:\\private\\note.md', 'token=secret', '<draft>', 'a'.repeat(401)]) assert.throws(() => accountPublicationText(text, 'post'))
  assert.throws(() => accountPublicationText('两行\n签名', 'signature'))
  assert.throws(() => accountPublicationText('my-secret-content', 'post', ['my-secret']))
})

test('status is read-only and durable reservation precedes the first real transport edge', async t => {
  const f = fixture(t)
  assert.deepEqual(f.store.rows(), []); assert.equal(fs.existsSync(f.store.file), false)
  let called = 0
  const value = await f.store.run(f.request(), f.settings, () => {}, () => {
    called++; assert.equal(f.store.rows()[0].status, 'reserved')
    return { status: 'ok', retcode: 0, data: { tid: 'fixture-post', time: 1 } }
  })
  assert.equal(called, 1); assert.equal(value.data.tid, 'fixture-post')
  assert.equal(f.store.rows()[0].status, 'accepted')
  assert.throws(() => f.store.run(f.request(), f.settings, () => {}, () => { called++ }))
  assert.equal(called, 1)
})

test('transport uncertainty remains held across a new store instance and is never replayed', async t => {
  const f = fixture(t); let called = 0
  await assert.rejects(f.store.run(f.request(), f.settings, () => {}, () => { called++; throw new Error('inert timeout') }))
  assert.equal(f.store.rows()[0].status, 'unknown')
  const reopened = new AccountActivityStore(f.store.file, hash('fixture account'), () => Date.parse('2026-11-09T02:00:00Z'))
  assert.equal(reopened.available('post', '另一个话题', f.settings).reason, 'previous_publication_unconfirmed')
  assert.throws(() => reopened.run(f.request('post', '新内容', 'second'), f.settings, () => {}, () => { called++ }))
  assert.equal(called, 1)
})

test('explicit negative receipt is retained as rejection, malformed success is unknown', async t => {
  const f = fixture(t)
  await assert.rejects(f.store.run(f.request(), f.settings, () => {}, () => ({ status: 'failed', retcode: 1401, data: null })))
  assert.equal(f.store.rows()[0].status, 'rejected')
  assert.equal(f.store.available('post', '换个内容', f.settings).reason, 'publication_interval')
  f.advance(16 * 60_000)
  await assert.rejects(f.store.run(f.request('post', '换个内容', 'second'), f.settings, () => {}, () => ({ status: 'ok', retcode: 0, data: {} })))
  assert.equal(f.store.rows()[0].status, 'unknown')
})

test('clock, per-account spacing, daily quota and identical content work independently', async t => {
  const f = fixture(t)
  await f.store.run(f.request('signature', '慢慢来。'), f.settings, () => {}, () => ({}))
  assert.equal(f.store.available('post', '阳光很好。', f.settings).allowed, true)
  assert.equal(f.store.available('signature', '晚点见。', f.settings).reason, 'publication_interval')
  f.advance(4 * 3600_000)
  assert.equal(f.store.available('signature', '慢慢来。', f.settings).reason, 'same_account_update')
  await f.store.run(f.request('signature', '去看看远方。', 'second'), f.settings, () => {}, () => ({}))
  f.advance(4 * 3600_000)
  assert.equal(f.store.available('signature', '第三个', f.settings).reason, 'publication_daily_limit')
  f.advance(7 * 3600_000)
  assert.equal(f.store.available('post', '夜里不发。', f.settings).reason, 'autonomy_rest')
})

test('a changed final binding stops transmission and retains uncertainty after reservation', async t => {
  const f = fixture(t); let checked = 0, called = 0
  await assert.rejects(f.store.run(f.request(), f.settings, () => { if (++checked === 2) throw new Error('changed selection') }, () => { called++ }))
  assert.equal(called, 0); assert.equal(f.store.rows()[0].status, 'unknown')
})

test('another account cannot open or overwrite this history', async t => {
  const f = fixture(t)
  await f.store.run(f.request('signature', '闲下来听歌。'), f.settings, () => {}, () => ({}))
  const other = new AccountActivityStore(f.store.file, hash('different account'))
  assert.throws(() => other.rows()); assert.equal(f.store.rows()[0].content, '闲下来听歌。')
})

test('corrupt history cannot be used to mint a new account reservation', async t => {
  const f = fixture(t)
  await f.store.run(f.request('signature', '听歌。'), f.settings, () => {}, () => ({}))
  const db = new DatabaseSync(f.store.file)
  try { db.prepare("UPDATE publications SET status='unrecognized'").run() } finally { db.close() }
  let called = 0
  assert.throws(() => f.store.rows(), /Invalid account activity record/)
  assert.throws(() => f.store.run(f.request('post', '新想法。', 'new'), f.settings, () => {}, () => { called++ }), /Invalid account activity record/)
  assert.equal(called, 0)
})

test('two concurrent calls reserve the same account before awaiting any transport', async t => {
  const f = fixture(t); let finish, called = 0
  const pending = f.store.run(f.request(), f.settings, () => {}, () => { called++; return new Promise(resolve => { finish = resolve }) })
  assert.throws(() => f.store.run(f.request('post', '另一个想法。', 'second'), f.settings, () => {}, () => { called++ }), /previous_publication_unconfirmed/)
  finish({ tid: 'one-reservation' }); await pending
  assert.equal(called, 1)
})

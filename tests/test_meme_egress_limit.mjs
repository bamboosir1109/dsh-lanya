import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createDecisionPolicy } from '../plugin/lib/decision-policy.js'
import { isStagedMemeImage, planEgress, dispatchEgress } from '../plugin/lib/qq-egress.js'
import { stageIndexedCandidate } from '../plugin/lib/meme-qq-picker.mjs'

const scope = 'private:101'
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jFqkAAAAASUVORK5CYII=', 'base64')
const text = value => ({ type: 'text', data: { text: value } })
const image = value => ({ type: 'image', data: { file: 'base64://' + value.toString('base64') } })
function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-meme-egress-'))
  t.after(() => { assert.ok(path.basename(root).startsWith('lanya-meme-egress-')); fs.rmSync(root, { recursive: true, force: true }) })
  const media = path.join(root, 'media'), gallery = path.join(root, 'gallery')
  fs.mkdirSync(media); fs.mkdirSync(gallery)
  const cap = { format: 'lanya-qq-meme-media/v1', mediaDir: media, maxBytes: 1024 * 1024, assertCurrent() {} }
  const stage = (bytes, name) => {
    const file = path.join(gallery, name); fs.writeFileSync(file, bytes)
    return stageIndexedCandidate({ resolveIndexed: stored => stored === name ? { root: gallery, relativePath: name, file } : null },
      { path: name, caption: 'synthetic image' }, cap)
  }
  return { root, media, stage, stateFile: path.join(root, 'policy.json') }
}
async function permit(policy, messageId) {
  const seen = await policy.recordInbound({ scope, messageId, sender: '101', controlAllowed: true, origin: 'live' })
  return (await policy.decide({ scope, messageId, expectedRevision: seen.revision, verdict: 'reply' })).ticket
}

test('only exact genuine staged picker copies are classified, not arbitrary screenshots or voice', t => {
  const h = fixture(t), staged = h.stage(png, 'one.png')
  assert.equal(isStagedMemeImage(image(png), h.media), true)
  assert.equal(isStagedMemeImage({ type: 'image', data: { file: staged.path } }, h.media), true)
  assert.equal(isStagedMemeImage(image(Buffer.concat([png, Buffer.from([1])])), h.media), false)
  assert.equal(isStagedMemeImage({ type: 'image', data: { url: 'https://example.invalid/media_meme_' + staged.sha256 + '.png' } }, h.media), false)
  assert.equal(isStagedMemeImage({ type: 'record', data: { file: staged.path } }, h.media), false)
  const unrelated = path.join(h.root, 'ordinary.png'); fs.writeFileSync(unrelated, png)
  assert.equal(isStagedMemeImage({ type: 'image', data: { file: unrelated } }, h.media), false)
  fs.writeFileSync(staged.path, Buffer.concat([png, Buffer.from([2])]))
  assert.throws(() => isStagedMemeImage(image(png), h.media), /changed_staged_meme/)
})

test('meme pictures have their own packets while reply and mention controls remain attached', t => {
  const h = fixture(t); h.stage(png, 'one.png')
  const at = { type: 'at', data: { qq: '101' } }, reply = { type: 'reply', data: { id: 'fixture-reference' } }
  const params = { user_id: 101, message: [reply, at, text('前面必要文字'), image(png), text('后面必要文字')] }
  const before = structuredClone(params)
  const plan = planEgress('send_msg', params, undefined, { isMemeSegment: value => isStagedMemeImage(value, h.media) })
  assert.deepEqual(plan.map(part => part.params.message.map(segment => segment.type)), [['reply', 'at', 'text'], ['image'], ['text']])
  assert.deepEqual(plan.map(part => part.meme === true), [false, true, false])
  assert.deepEqual(params, before)
  assert.equal(plan.some(part => 'meme' in part.params), false)
  const controlPlan = planEgress('send_msg', { user_id: 101, message: [reply, at, image(png)] }, undefined,
    { isMemeSegment: value => isStagedMemeImage(value, h.media) })
  assert.deepEqual(controlPlan[0].params.message.map(segment => segment.type), ['reply', 'at', 'image'])
  const ordinary = planEgress('send_msg', { user_id: 101, message: [text('附件说明'), image(Buffer.concat([png, Buffer.from([3])]))] }, undefined,
    { isMemeSegment: value => isStagedMemeImage(value, h.media) })
  assert.deepEqual(ordinary[0].params.message.map(segment => segment.type), ['text', 'image'])
})

test('one durable meme slot is shared by concurrent tool calls and survives a policy restart', async t => {
  const h = fixture(t), policy = createDecisionPolicy({ stateFile: h.stateFile }), ticket = await permit(policy, 'one-turn')
  let release, entered
  const waiting = new Promise(resolve => { entered = resolve }), gate = new Promise(resolve => { release = resolve })
  let calls = 0
  const first = policy.runGuarded(ticket, { scope, kind: 'media', meme: true, operationId: 'first-tool' }, async () => {
    calls++; entered(); await gate; return { message_id: 'fixture-first' }
  })
  await waiting
  const restarted = createDecisionPolicy({ stateFile: h.stateFile })
  const second = await restarted.runGuarded(ticket, { scope, kind: 'api', meme: true, operationId: 'different-tool' }, async () => { calls++ })
  assert.equal(second.reason, 'meme_turn_limit'); assert.equal(calls, 1)
  assert.equal((await restarted.snapshot(scope)).deliveries[0].status, 'reserved')
  release(); assert.equal((await first).status, 'sent')
  assert.equal((await restarted.runGuarded(ticket, { scope, kind: 'forward', meme: true, operationId: 'forward-tool' }, async () => { calls++ })).reason, 'meme_turn_limit')
  assert.equal((await restarted.runGuarded(ticket, { scope, kind: 'text', operationId: 'necessary-text' }, async () => ({ message_id: 'text' }))).status, 'sent')
  assert.equal((await restarted.runGuarded(ticket, { scope, kind: 'media', operationId: 'ordinary-attachment' }, async () => ({ message_id: 'attachment' }))).status, 'sent')
  const next = await permit(restarted, 'new-turn')
  assert.equal((await restarted.runGuarded(next, { scope, kind: 'media', meme: true, operationId: 'new-turn-image' }, async () => ({ message_id: 'next' }))).status, 'sent')
})

test('unknown meme delivery keeps its slot without changing the old uncertain entry or replaying', async t => {
  const h = fixture(t), policy = createDecisionPolicy({ stateFile: h.stateFile }), ticket = await permit(policy, 'unknown-turn')
  assert.equal((await policy.runGuarded(ticket, { scope, kind: 'media', meme: true, operationId: 'uncertain' }, async () => { throw new Error('uncertain fixture') })).status, 'unknown')
  const old = (await policy.snapshot(scope)).deliveries
  const restarted = createDecisionPolicy({ stateFile: h.stateFile })
  let calls = 0
  assert.equal((await restarted.runGuarded(ticket, { scope, kind: 'media', meme: true, operationId: 'another-image' }, async () => { calls++ })).reason, 'meme_turn_limit')
  assert.equal(calls, 0); assert.deepEqual((await restarted.snapshot(scope)).deliveries, old)
  assert.equal((await restarted.runGuarded(ticket, { scope, kind: 'media', meme: true, operationId: 'uncertain' }, async () => { calls++ })).status, 'duplicate')
})

test('small delivery capacity and text pressure cannot collect a current-ticket meme slot', async t => {
  const h = fixture(t), policy = createDecisionPolicy({ stateFile: h.stateFile, maxDeliveries: 2 }), ticket = await permit(policy, 'capacity-turn')
  let images = 0
  await policy.runGuarded(ticket, { scope, kind: 'media', meme: true, operationId: 'one-meme' }, async () => { images++; return {} })
  await policy.runGuarded(ticket, { scope, kind: 'text', operationId: 'one-text' }, async () => ({}))
  for (let index = 0; index < 8; index++) {
    assert.equal((await policy.runGuarded(ticket, { scope, kind: 'text', operationId: 'text-pressure-' + index }, async () => { throw new Error('full ledger cannot send') })).reason, 'delivery_capacity')
  }
  assert.equal((await policy.runGuarded(ticket, { scope, kind: 'media', meme: true, operationId: 'second-meme' }, async () => { images++ })).reason, 'meme_turn_limit')
  assert.equal(images, 1)
  assert.equal((await policy.snapshot(scope)).deliveries.filter(row => row.meme === true).length, 1)
  const next = await permit(policy, 'new-capacity-turn')
  assert.equal((await policy.runGuarded(next, { scope, kind: 'media', meme: true, operationId: 'new-meme' }, async () => { images++; return {} })).status, 'sent')
  assert.equal(images, 2)
})

test('a quota skip preserves following necessary text without reporting the whole image operation as sent', async t => {
  const h = fixture(t); h.stage(png, 'one.png'); const other = Buffer.concat([png, Buffer.from([8])]); h.stage(other, 'two.png')
  const policy = createDecisionPolicy({ stateFile: h.stateFile }), ticket = await permit(policy, 'mixed-output')
  const sent = []; let slice = 0
  await assert.rejects(dispatchEgress('send_msg', { user_id: 101,
    message: [text('必要前文'), image(png), image(other), text('必要后文')] }, undefined, async (action, params, part) => {
    const result = await policy.runGuarded(ticket, { scope, kind: part?.meme ? 'media' : 'text', meme: part?.meme,
      operationId: 'raw-operation', sliceId: String(slice++) }, async () => { sent.push({ action, params }); return { message_id: String(sent.length) } })
    if (result.status !== 'sent') throw Object.assign(new Error(result.reason), { name: 'PolicyDeliveryError', status: result.status, reason: result.reason })
    return result.value
  }, { isMemeSegment: value => isStagedMemeImage(value, h.media) }), error =>
    error.reason === 'meme_turn_limit' && error.partial && error.skipped_meme_images === 1 && error.delivered_meme_images === 1)
  assert.deepEqual(sent.flatMap(row => row.params.message).filter(row => row.type === 'text').map(row => row.data.text), ['必要前文', '必要后文'])
  assert.equal(sent.flatMap(row => row.params.message).filter(row => row.type === 'image').length, 1)
  assert.equal(new Set((await policy.snapshot(scope)).deliveries.map(row => row.ticketId)).size, 1)
})

test('forward packets also separate staged memes without dropping forward node identity or ordinary attachments', t => {
  const h = fixture(t); h.stage(png, 'one.png')
  const input = { user_id: 101, messages: [{ type: 'node', data: { name: 'fixture-author', uin: '101',
    content: [text('转发前文'), image(png), text('转发后文')] } }] }
  const plan = planEgress('send_private_forward_msg', input, undefined, { isMemeSegment: value => isStagedMemeImage(value, h.media) })
  assert.equal(plan.length, 3)
  assert.deepEqual(plan.map(row => row.params.messages[0].data.content.map(item => item.type)), [['text'], ['image'], ['text']])
  assert.equal(plan[1].meme, true)
  assert.ok(plan.every(row => row.params.messages[0].data.name === 'fixture-author'))
  assert.ok(plan.every(row => row.params.messages[0].data.uin === '101'))
})

import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import vm from 'node:vm'
import { createHash } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { fileToBase64, resolveContainedPath } from './fence.js'

const here = path.dirname(fileURLToPath(import.meta.url))
const repo = path.resolve(here, '../../..')
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-expression-media-'))
after(() => {
  assert.ok(path.basename(root).startsWith('lanya-expression-media-'))
  fs.rmSync(root, { recursive: true, force: true })
})
// Keep the native host's supported media seam and use the unmodified real fence.
import { nativeHost } from '../onebot_egress/native-host.mjs'
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jFqkAAAAASUVORK5CYII=', 'base64')
const digest = value => createHash('sha256').update(value).digest('hex')
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes }); return { promise, resolve } }
const pickerUrl = pathToFileURL(path.join(repo, 'plugin/lib/meme-qq-picker.mjs')).href
let sequence = 0

async function setup(t) {
  const directory = path.join(root, 'case-' + ++sequence), plugin = path.join(directory, 'plugin')
  fs.mkdirSync(directory)
  const prepare = String.raw`
import shutil,sys
from pathlib import Path
repo,out=map(Path,sys.argv[1:3]);sys.path.insert(0,str(repo/'scripts'))
import patch_onebot_policy as policy,patch_onebot_replay as replay,patch_onebot_egress as egress
shutil.copytree(repo/'tests/fixtures/onebot_egress',out)
for name in ['bridge.js','outbound.js','tools.js','prompt.js']:
 p=out/'lib'/name;raw=policy.transform(name,p.read_bytes())
 if name=='bridge.js':raw=replay.transform('lib/bridge.js',raw)
 if name in ['bridge.js','outbound.js']:raw=egress.transform('lib/'+name,raw)
 p.write_bytes(raw)
p=out/'lib/inbound.js';s=p.read_text(encoding='utf-8');a='        const nickname = sanitizeNickname(inbound.nickname);';assert s.count(a)==1
s=s.replace(a,'        const observed = await this.ctx.observeInbound(inbound);\n        if (!observed.accepted) return;\n'+a);p.write_text(s,encoding='utf-8')
for name,raw in policy.module_outputs({'stateFile':str(out.parent/'synthetic-policy.json')}).items():
 if name=='lanya-policy-adapter.js':raw=egress.transform('lib/'+name,raw)
 (out/'lib'/name).write_bytes(raw)
(out/egress.GENERATED).write_bytes((repo/'plugin/lib/qq-egress.js').read_bytes())
`
  const run = spawnSync('python', ['-B', '-c', prepare, repo, plugin], { encoding: 'utf8', windowsHide: true, timeout: 20000 })
  assert.equal(run.status, 0, run.stderr)
  let h
  h = await nativeHost(plugin, directory, { nativeMedia: { fileToBase64: async (...args) => {
    await h.deps.onMediaRead?.()
    return fileToBase64(...args)
  } } })
  fs.mkdirSync(h.deps.config.mediaDir, { recursive: true })
  t.after(() => h.close())
  const staged = path.join(h.deps.config.mediaDir, 'synthetic.png')
  fs.writeFileSync(staged, png)
  return { ...h, staged }
}

async function pickerFixture(h) {
  const picker = await import(pickerUrl)
  const gallery = path.join(root, 'indexed-gallery-' + sequence)
  const file = path.join(gallery, 'stolen', 'fixture.png')
  fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, png)
  const row = { path: 'stolen/fixture.png', caption: '', tag: 'happy' }
  const memes = {
    resolveIndexed: stored => stored === row.path ? { root: gallery, relativePath: row.path, file } : null,
    search: () => ({ memes: [row, row] }), sampleMood: () => ({ memes: [row] }),
  }
  // Synthetic SDK service boundary; its authority check is the actual adapter,
  // including agent object identity and the active, unfinished frozen record.
  const service = { forExecution(exec) {
    const record = h.bridge.lanyaPolicy.captureExec(exec)
    return Object.freeze({ format: 'lanya-qq-meme-media/v1', mediaDir: h.deps.config.mediaDir,
      maxBytes: h.deps.config.maxImageBytes, assertCurrent() {
        assert.equal(h.bridge.lanyaPolicy.captureExec(exec), record)
      } })
  } }
  h.agent.ctx = { get(key, required) { assert.equal(key, picker.CAPABILITY); assert.equal(required, false); return service } }
  const exec = h.exec()
  return { picker, gallery, file, row, memes, exec, cap: picker.getQqCapability(exec) }
}

test('real native media fence rejects gallery outside the workspace until an exact file is staged', async t => {
  const h = await setup(t)
  await h.receive('fixture ordinary question'); h.start(); await h.decide()
  const selected = await pickerFixture(h), original = selected.file
  await assert.rejects(() => h.tools.get('qq_send_image').execute({ sources: [original] }, h.exec()), /允许的媒体目录|path not allowed/)
  assert.equal(h.calls.filter(call => call.action === 'send_msg').length, 0)
  const projection = selected.picker.selectQqCandidates(selected.memes, { query: 'fixture', limit: 3 }, selected.exec, selected.cap)
  assert.equal(projection.mode, 'qq_selection'); assert.equal(projection.selected, true); assert.equal(projection.delivered, false)
  assert.equal(projection.candidates.length, 1)
  const candidate = projection.candidates[0]
  assert.equal(candidate.caption, ''); assert.equal(candidate.requires_visual_review, true)
  assert.equal(candidate.sha256, digest(png)); assert.equal(candidate.bytes, png.length)
  const rendered = JSON.parse(projection.message)
  assert.deepEqual(rendered.candidates, [candidate])
  assert.equal(rendered.mode, 'qq_selection')
  assert.equal(rendered.selected, true); assert.equal(rendered.delivered, false)
  assert.equal(path.dirname(candidate.path), h.deps.config.mediaDir)
  const result = await h.tools.get('qq_send_image').execute({ sources: [candidate.path] }, h.exec())
  assert.equal(result.sent, true)
  const images = h.calls.flatMap(call => call.params.message || []).filter(segment => segment.type === 'image')
  assert.equal(images.length, 1)
  assert.equal(images[0].data.file, 'base64://' + png.toString('base64'))
  assert.equal(digest(fs.readFileSync(original)), digest(png))
  assert.equal(digest(fs.readFileSync(selected.file)), digest(png))
})

test('five ordinary bubbles and a real local image retain all body bytes and one frozen ticket', async t => {
  const h = await setup(t)
  await h.receive('fixture ordinary question'); h.start(); await h.decide()
  const text = '甲'.repeat(161)
  await h.send(text)
  await h.tools.get('qq_send_image').execute({ sources: [h.staged] }, h.exec())
  const messages = h.calls.filter(call => call.action === 'send_msg')
  const texts = messages.flatMap(call => call.params.message).filter(segment => segment.type === 'text').map(segment => segment.data.text)
  assert.deepEqual(texts.map(value => [...value].length), [40, 40, 40, 40, 1])
  assert.equal(texts.join(''), text)
  assert.equal(messages.flatMap(call => call.params.message).filter(segment => segment.type === 'image').length, 1)
  const policy = await h.bridge.lanyaPolicy.policy.snapshot('private:101')
  assert.equal(policy.deliveries.length, 6)
  assert.equal(new Set(policy.deliveries.map(delivery => delivery.ticketId)).size, 1)
  assert.equal(new Set(policy.deliveries.map(delivery => delivery.key)).size, 6)
})

async function twoRealMemes(h) {
  const selected = await pickerFixture(h)
  const first = selected.picker.selectQqCandidates(selected.memes, { query: 'fixture', limit: 1 }, selected.exec, selected.cap).candidates[0]
  fs.writeFileSync(selected.file, Buffer.concat([png, Buffer.from([1])]))
  const second = selected.picker.selectQqCandidates(selected.memes, { query: 'fixture', limit: 1 }, selected.exec, selected.cap).candidates[0]
  assert.notEqual(first.sha256, second.sha256)
  return [first, second]
}

test('actual image tool separates caption and limits real picker memes within one call without false sent receipt', async t => {
  const h = await setup(t)
  await h.receive('fixture casual reply'); h.start(); await h.decide()
  const [first, second] = await twoRealMemes(h)
  await assert.rejects(h.tools.get('qq_send_image').execute({ sources: [first.path, second.path], caption: '必要说明' }, h.exec()),
    error => error.reason === 'meme_turn_limit' && error.partial && error.delivered_meme_images === 1 && error.skipped_meme_images === 1)
  const messages = h.calls.filter(call => call.action === 'send_msg')
  assert.deepEqual(messages.map(call => call.params.message.map(segment => segment.type)), [['text'], ['image']])
  assert.equal(messages[0].params.message[0].data.text, '必要说明')
  assert.equal(messages[1].params.message[0].data.file, 'base64://' + png.toString('base64'))
  const state = await h.bridge.lanyaPolicy.policy.snapshot('private:101')
  assert.equal(state.deliveries.filter(row => row.meme === true).length, 1)
  assert.equal(state.deliveries.filter(row => row.meme === true)[0].status, 'sent')
  assert.equal(h.bridge.lanyaPolicy.state(h.chat).active.outcomes.includes('partial'), false)
})

test('a second image tool call cannot bypass the ticket slot while later text and ordinary attachments remain possible', async t => {
  const h = await setup(t)
  await h.receive('fixture casual reply'); h.start(); await h.decide()
  const [first, second] = await twoRealMemes(h)
  assert.equal((await h.tools.get('qq_send_image').execute({ sources: [first.path] }, h.exec())).sent, true)
  await assert.rejects(h.tools.get('qq_send_image').execute({ sources: [second.path], caption: '继续必要文字', meme: false, attachment: true }, h.exec()),
    error => error.reason === 'meme_turn_limit' && error.partial && error.delivered_meme_images === 0)
  await h.send('还有完整说明')
  fs.writeFileSync(h.staged, Buffer.concat([png, Buffer.from([9])]))
  assert.equal((await h.tools.get('qq_send_image').execute({ sources: [h.staged, h.staged], caption: '普通附件' }, h.exec())).sent, true)
  const state = await h.bridge.lanyaPolicy.policy.snapshot('private:101')
  assert.equal(state.deliveries.filter(row => row.meme === true).length, 1)
  assert.equal(new Set(state.deliveries.map(row => row.ticketId)).size, 1)
  const messages = h.calls.filter(call => call.action === 'send_msg')
  assert.deepEqual(messages.flatMap(call => call.params.message).filter(row => row.type === 'text').map(row => row.data.text),
    ['继续必要文字', '还有完整说明', '普通附件'])
  assert.equal(messages.flatMap(call => call.params.message).filter(row => row.type === 'image').length, 3)
})

test('actual raw terminal and forward paths cannot merge meme captions or lose text after a quota refusal', async t => {
  const h = await setup(t)
  await h.receive('fixture raw expression'); h.start(); await h.decide()
  await twoRealMemes(h)
  const firstImage = { type: 'image', data: { file: 'base64://' + png.toString('base64') } }
  const secondImage = { type: 'image', data: { file: 'base64://' + Buffer.concat([png, Buffer.from([1])]).toString('base64') } }
  const text = value => ({ type: 'text', data: { text: value } })
  await assert.rejects(h.raw('send_msg', { user_id: 101,
    message: [{ type: 'reply', data: { id: 'fixture-reference' } }, { type: 'at', data: { qq: '101' } },
      text('原始前文'), firstImage, secondImage, text('原始后文')] }), error => error.reason === 'meme_turn_limit')
  const messages = h.calls.filter(call => call.action === 'send_msg')
  assert.deepEqual(messages.map(call => call.params.message.map(row => row.type)), [['reply', 'at', 'text'], ['image'], ['text']])
  assert.equal(messages[2].params.message[0].data.text, '原始后文')
  await assert.rejects(h.raw('send_private_forward_msg', { user_id: 101, messages: [{ type: 'node',
    data: { name: 'fixture-author', uin: '101', content: [text('转发必要前文'), secondImage, text('转发必要后文')] } }] }),
    error => error.reason === 'meme_turn_limit')
  const forward = h.calls.filter(call => /forward/.test(call.action))
  assert.equal(forward.length, 2)
  assert.deepEqual(forward.map(call => call.params.messages[0].data.content.map(row => row.type)), [['text'], ['text']])
  assert.equal(forward[1].params.messages[0].data.content[0].data.text, '转发必要后文')
})

test('unknown real meme acknowledgement retains its slot and never substitutes another image', async t => {
  const h = await setup(t)
  await h.receive('fixture uncertain expression'); h.start(); await h.decide()
  const [first, second] = await twoRealMemes(h)
  h.deps.onTransmit = async () => { throw new Error('synthetic uncertain transport') }
  await assert.rejects(h.tools.get('qq_send_image').execute({ sources: [first.path, second.path] }, h.exec()))
  h.deps.onTransmit = undefined
  const before = (await h.bridge.lanyaPolicy.policy.snapshot('private:101')).deliveries
  assert.equal(before.length, 1); assert.equal(before[0].meme, true); assert.equal(before[0].status, 'unknown')
  await assert.rejects(h.tools.get('qq_send_image').execute({ sources: [second.path] }, h.exec()),
    error => error.status === 'suppressed' && error.reason === 'turn_delivery_uncertain')
  assert.deepEqual((await h.bridge.lanyaPolicy.policy.snapshot('private:101')).deliveries, before)
  assert.equal(h.calls.filter(call => call.action === 'send_msg').length, 1)
})

test('new inbound while the actual media read waits revokes its captured image permit', async t => {
  const h = await setup(t), entered = deferred(), release = deferred()
  await h.receive('fixture ordinary question'); h.start(); await h.decide()
  h.deps.onMediaRead = async () => { entered.resolve(); await release.promise }
  const sending = h.tools.get('qq_send_image').execute({ sources: [h.staged] }, h.exec())
  await entered.promise
  await h.receive('fixture new incoming message')
  release.resolve()
  await assert.rejects(() => sending, error => error.status === 'suppressed')
  assert.equal(h.calls.filter(call => call.action === 'send_msg').length, 0)
})

test('persistent silence still prevents a real staged image from reaching the fake transport', async t => {
  const h = await setup(t)
  await h.receive('fixture authorized silence request'); h.start()
  await h.decide('silence', { control_update: true, evidenceSource: 'current' })
  const selected = await pickerFixture(h)
  const projection = selected.picker.selectQqCandidates(selected.memes, { query: 'fixture', limit: 1 }, selected.exec, selected.cap)
  assert.equal(projection.selected, true); assert.equal(projection.delivered, false)
  await assert.rejects(() => h.tools.get('qq_send_image').execute({ sources: [projection.candidates[0].path] }, h.exec()), error => error.status === 'suppressed')
  assert.equal(h.calls.filter(call => call.action === 'send_msg').length, 0)
  assert.equal((await h.bridge.lanyaPolicy.policy.snapshot('private:101')).mode, 'silent')
})

test('unknown real-image delivery is attempted once and cannot fall back to ordinary text', async t => {
  const h = await setup(t)
  await h.receive('fixture ordinary question'); h.start(); await h.decide()
  h.deps.onTransmit = async () => { throw new Error('synthetic unknown image transmission') }
  await assert.rejects(() => h.tools.get('qq_send_image').execute({ sources: [h.staged] }, h.exec()), error => error.status === 'unknown')
  const sends = h.calls.filter(call => call.action === 'send_msg')
  assert.equal(sends.length, 1)
  assert.equal(sends[0].params.message[0].type, 'image')
  assert.deepEqual(Array.from((await h.bridge.lanyaPolicy.policy.snapshot('private:101')).deliveries, delivery => delivery.status), ['unknown'])
})

test('actual containment exports enforce separator boundaries size caps and missing files', async () => {
  const directory = path.join(root, 'fence-only'); fs.mkdirSync(directory)
  const media = path.join(directory, 'media'), sibling = path.join(directory, 'media-foreign')
  fs.mkdirSync(media); fs.mkdirSync(sibling)
  const allowed = path.join(media, 'file.png'), outside = path.join(sibling, 'file.png')
  fs.writeFileSync(allowed, png); fs.writeFileSync(outside, png)
  assert.equal(await resolveContainedPath([media], outside), null)
  assert.equal(await resolveContainedPath([media], path.join(media, 'missing.png')), null)
  await assert.rejects(() => fileToBase64(outside, 1024, [media]), /path not allowed/)
  await assert.rejects(() => fileToBase64(allowed, 1, [media]), /file too large/)
  assert.equal(await fileToBase64(allowed, 1024, [media]), 'base64://' + png.toString('base64'))
})

test('actual selector reuses one SHA file and refuses a tampered existing file without overwriting it', async t => {
  const h = await setup(t); await h.receive('fixture ordinary question'); h.start()
  const selected = await pickerFixture(h)
  const choose = () => selected.picker.selectQqCandidates(selected.memes, { query: 'fixture', limit: 3 }, selected.exec, selected.cap)
  const first = choose(); assert.equal(first.candidates.length, 1)
  const candidate = first.candidates[0], before = fs.statSync(candidate.path)
  const files = fs.readdirSync(h.deps.config.mediaDir).sort()
  const again = choose(); assert.equal(again.candidates.length, 1)
  assert.equal(again.candidates[0].path, candidate.path)
  assert.equal(fs.statSync(candidate.path).ino, before.ino)
  assert.deepEqual(fs.readdirSync(h.deps.config.mediaDir).sort(), files)
  const foreign = Buffer.from('synthetic later bytes')
  fs.writeFileSync(candidate.path, foreign)
  const refused = choose(); assert.equal(refused.selected, false); assert.equal(refused.delivered, false)
  assert.deepEqual(fs.readFileSync(candidate.path), foreign)
  assert.deepEqual(fs.readdirSync(h.deps.config.mediaDir).sort(), files)
  assert.equal(digest(fs.readFileSync(selected.file)), digest(png))
  assert.equal(h.calls.filter(call => call.action === 'send_msg').length, 0)
})

test('actual scoped selection rejects cloned agents ended turns foreign index roots and hardlinked sources', async t => {
  const h = await setup(t); await h.receive('fixture ordinary question'); h.start()
  const selected = await pickerFixture(h), files = fs.readdirSync(h.deps.config.mediaDir).sort()
  const clonedExec = { ...selected.exec, agent: { ...h.agent, session: { ...h.agent.session } } }
  assert.throws(() => selected.picker.getQqCapability(clonedExec), /unbound_or_aborted_turn/)
  const foreign = { resolveIndexed: () => ({ root: selected.gallery, relativePath: selected.row.path, file: h.staged }) }
  assert.throws(() => selected.picker.stageIndexedCandidate(foreign, selected.row, selected.cap))
  const link = path.join(selected.gallery, 'stolen', 'duplicate.png')
  fs.linkSync(selected.file, link)
  assert.throws(() => selected.picker.stageIndexedCandidate(selected.memes, selected.row, selected.cap))
  fs.unlinkSync(link)
  h.end()
  assert.throws(() => selected.picker.stageIndexedCandidate(selected.memes, selected.row, selected.cap), /unbound_or_aborted_turn/)
  assert.deepEqual(fs.readdirSync(h.deps.config.mediaDir).sort(), files)
  assert.equal(h.calls.filter(call => call.action === 'send_msg').length, 0)
})

test('actual selector uses bounded descriptor reads and refuses a source growing during the read', async t => {
  const h = await setup(t); await h.receive('fixture ordinary question'); h.start()
  const selected = await pickerFixture(h), before = fs.readdirSync(h.deps.config.mediaDir).sort()
  let grown = false, descriptorFullReads = 0
  const requests = []
  const hookedFs = { ...fs,
    readFileSync(file, ...args) { if (typeof file === 'number') descriptorFullReads++; return fs.readFileSync(file, ...args) },
    readSync(descriptor, buffer, ...args) {
      requests.push(buffer.length)
      const result = fs.readSync(descriptor, buffer, ...args)
      if (!grown) { fs.appendFileSync(selected.file, Buffer.alloc(4096, 1)); grown = true }
      return result
    },
  }
  const context = vm.createContext({ Buffer, process: { platform: process.platform } })
  const source = fs.readFileSync(fileURLToPath(pickerUrl), 'utf8')
  const module = new vm.SourceTextModule(source, { context })
  await module.link(async name => {
    assert.ok(/^node:(fs|path|crypto)$/.test(name))
    const exports = name === 'node:fs' ? hookedFs : await import(name)
    return new vm.SyntheticModule(Object.keys(exports), function () {
      for (const [key, value] of Object.entries(exports)) this.setExport(key, value)
    }, { context })
  })
  await module.evaluate()
  assert.throws(() => module.namespace.stageIndexedCandidate(selected.memes, selected.row, selected.cap))
  assert.equal(grown, true); assert.equal(descriptorFullReads, 0)
  assert.ok(requests.length > 0 && requests.every(size => size <= png.length + 1))
  assert.deepEqual(fs.readdirSync(h.deps.config.mediaDir).sort(), before)
  assert.equal(h.calls.filter(call => call.action === 'send_msg').length, 0)
})

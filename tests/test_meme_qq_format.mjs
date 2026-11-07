import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import vm from 'node:vm'
import { createHash } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { fileToBase64, resolveContainedPath } from './fixtures/qq_expression_media/fence.js'

const here = path.join(path.dirname(fileURLToPath(import.meta.url)), 'fixtures/qq_expression_media')
const repo = path.resolve(here, '../../..')
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-expression-media-'))
after(() => {
  assert.ok(path.basename(root).startsWith('lanya-expression-media-'))
  fs.rmSync(root, { recursive: true, force: true })
})
// Keep the native host's supported media seam and use the unmodified real fence.
import { nativeHost } from './fixtures/onebot_egress/native-host.mjs'
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


const caseName = 'legacy JPG suffix stages genuine GIF bytes and passes the actual native QQ image fence'
async function formatCase(t) {
  const h = await setup(t)
  await h.receive('fixture ordinary question'); h.start(); await h.decide()
  const selected = await pickerFixture(h)
  const gif = Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64')
  const original = path.join(selected.gallery, 'stolen', 'legacy.jpg')
  fs.writeFileSync(original, gif)
  const before = fs.statSync(original)
  const row = { path: 'stolen/legacy.jpg', caption: '', tag: 'happy' }
  const memes = {
    resolveIndexed: stored => stored === row.path ? { root: selected.gallery, relativePath: row.path, file: original } : null,
    search: () => ({ memes: [row] }), sampleMood: () => ({ memes: [row] }),
  }
  const projection = selected.picker.selectQqCandidates(memes, { query: 'fixture', limit: 1 }, selected.exec, selected.cap)
  assert.equal(projection.selected, true); assert.equal(projection.delivered, false)
  assert.equal(projection.candidates.length, 1)
  const candidate = projection.candidates[0]
  assert.equal(path.extname(candidate.path), '.gif')
  assert.equal(candidate.sha256, digest(gif)); assert.deepEqual(fs.readFileSync(candidate.path), gif)
  assert.equal(candidate.caption, ''); assert.equal(candidate.requires_visual_review, true)
  assert.equal(h.calls.filter(call => call.action === 'send_msg').length, 0)
  const result = await h.tools.get('qq_send_image').execute({ sources: [candidate.path] }, h.exec())
  assert.equal(result.sent, true)
  const images = h.calls.flatMap(call => call.params.message || []).filter(segment => segment.type === 'image')
  assert.equal(images.length, 1); assert.equal(images[0].data.file, 'base64://' + gif.toString('base64'))
  assert.equal(fs.statSync(original).ino, before.ino)
  assert.equal(fs.statSync(original).mtimeMs, before.mtimeMs)
  assert.deepEqual(fs.readFileSync(original), gif)
  // An arbitrary non-image source suffix and a fake image signature stay denied.
  const txt = path.join(selected.gallery, 'stolen', 'not-image.txt'); fs.writeFileSync(txt, gif)
  const invalid = path.join(selected.gallery, 'stolen', 'invalid.jpg'); fs.writeFileSync(invalid, 'not image bytes')
  for (const file of [txt, invalid]) {
    const unsafe = { resolveIndexed: () => ({ root: selected.gallery, relativePath: 'stolen/' + path.basename(file), file }) }
    assert.throws(() => selected.picker.stageIndexedCandidate(unsafe, { path: 'indexed' }, selected.cap))
  }
  assert.equal(h.calls.filter(call => call.action === 'send_msg').length, 1)
}

if (typeof vm.SourceTextModule === 'function') {
  test(caseName, formatCase)
} else {
  // Standard node --test discovery must enable only this inert native VM host,
  // like the existing media fixtures, without changing the caller's environment.
  test(caseName, () => {
    const env = { ...process.env }; delete env.NODE_TEST_CONTEXT
    const run = spawnSync(process.execPath,
      ['--experimental-vm-modules', '--test', fileURLToPath(import.meta.url)],
      { encoding: 'utf8', windowsHide: true, timeout: 30000, env })
    assert.equal(run.status, 0, run.stdout + run.stderr)
    assert.match(run.stdout, /(?:pass 1|# pass 1)/)
    assert.match(run.stdout, /(?:fail 0|# fail 0)/)
  })
}

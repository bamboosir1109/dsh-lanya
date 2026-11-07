import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createHash } from 'node:crypto'
import { normalizeInboundImage } from '../plugin/lib/onebot-policy-adapter.js'

const digest = bytes => createHash('sha256').update(bytes).digest('hex')
function fixture(t) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'lanya-inbound-format-'))
  t.after(()=>fs.rmSync(root,{recursive:true,force:true}))
  const directory=path.join(root,'media');fs.mkdirSync(directory)
  const media=fs.realpathSync.native(directory)
  return {root,media,write(name,bytes){const file=path.join(media,name);fs.writeFileSync(file,bytes);return file}}
}
const samples={
  gif:Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7','base64'),
  png:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==','base64'),
  // Header fixtures exercise format selection only; the real image decoder
  // remains responsible for rejecting unsupported or invalid image contents.
  jpg:Buffer.from([255,216,255,224,0,2,255,217]),
  webp:Buffer.from('524946460400000057454250','hex'),
}

test('mislabeled supported inbound formats receive matching paths without changing original bytes',t=>{
  const f=fixture(t)
  for(const [format,bytes] of Object.entries(samples)){
    const original=f.write('original-'+format+'.wrong',bytes),before=fs.statSync(original)
    const fixed=normalizeInboundImage(f.media,original)
    assert.equal(path.dirname(fixed),f.media)
    assert.equal(path.extname(fixed),'.'+format)
    assert.deepEqual(fs.readFileSync(fixed),bytes)
    assert.deepEqual(fs.readFileSync(original),bytes)
    assert.equal(fs.statSync(original).ino,before.ino)
    assert.equal(fs.statSync(original).mtimeMs,before.mtimeMs)
    assert.equal(fs.statSync(original).nlink,1)
    assert.equal(fs.statSync(fixed).nlink,1)
    assert.equal(normalizeInboundImage(f.media,original),fixed,'repeated reads reuse one verified copy')
  }
  assert.equal(fs.readdirSync(f.media).filter(n=>n.startsWith('.image-format-')).length,0)
})

test('matching image suffixes, JPEG aliases and unsupported bytes are unchanged',t=>{
  const f=fixture(t)
  for(const [format,bytes] of Object.entries({...samples,unknown:Buffer.from('not an image')})){
    const file=f.write('same.'+format,bytes)
    assert.equal(normalizeInboundImage(f.media,file),file)
  }
  const jpeg=f.write('alias.JPEG',samples.jpg)
  assert.equal(normalizeInboundImage(f.media,jpeg),jpeg)
  assert.equal(fs.readdirSync(f.media).length,6)
})

test('normalization refuses root escape, hardlinked sources and oversized images',t=>{
  const f=fixture(t),outside=path.join(f.root,'outside.jpg');fs.writeFileSync(outside,samples.gif)
  assert.throws(()=>normalizeInboundImage(f.media,outside),/outside media root/)
  const original=f.write('linked.jpg',samples.gif);fs.linkSync(original,path.join(f.media,'alias.jpg'))
  assert.throws(()=>normalizeInboundImage(f.media,original),/Unverified/)
  const large=f.write('large.jpg',samples.gif);fs.truncateSync(large,8*1024*1024+1)
  assert.throws(()=>normalizeInboundImage(f.media,large),/Unverified/)
  assert.equal(fs.readdirSync(f.media).some(n=>n.startsWith('media_format_')),false)
})

test('an existing changed or hardlinked normalized target is never overwritten',t=>{
  const f=fixture(t),original=f.write('mislabeled.jpg',samples.gif)
  const target=path.join(f.media,'media_format_'+digest(samples.gif)+'.gif')
  fs.writeFileSync(target,'unrelated existing file')
  assert.throws(()=>normalizeInboundImage(f.media,original),/Normalized image changed/)
  assert.equal(fs.readFileSync(target,'utf8'),'unrelated existing file')
  fs.writeFileSync(target,samples.gif);fs.linkSync(target,path.join(f.media,'foreign-alias.gif'))
  assert.throws(()=>normalizeInboundImage(f.media,original),/Unverified/)
  assert.deepEqual(fs.readFileSync(original),samples.gif)
})

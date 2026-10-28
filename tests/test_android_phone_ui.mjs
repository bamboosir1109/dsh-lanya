import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'

const html = fs.readFileSync(new URL('../panel/phone.html', import.meta.url), 'utf8')
const script = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)][0][1]
const serial = 'emulator-fixture'
const frame = {ok:true, status:'observed', serial, seq:2, shot_id:'a'.repeat(32),
  width:2, height:3, captured_at:1000, age_seconds:0, max_age:30}

class Element {
  constructor() { this.hidden = false; this.disabled = true; this.value = ''; this.textContent = ''; this.handlers = new Map(); this.dataset = {} }
  addEventListener(name, fn) { this.handlers.set(name, fn) }
  removeAttribute(name) { if (name === 'src') this._src = null }
  set src(value) { this._src = value }
  get src() { return this._src }
  getBoundingClientRect() { return {left:10, top:20, width:200, height:300} }
  setPointerCapture() {}
}

async function fixture(initial={ok:false, status:'error', serial, code:'capture_required', error:'Capture a new screenshot'}) {
  const elements = new Map()
  for (const id of ['shot', 'capture', 'refresh', 'placeholder', 'frameInfo', 'status', 'claimWait', 'error', 'serial', 'gesture', 'text', 'package', 'textForm', 'startForm']) {
    elements.set(id, new Element())
  }
  const image = elements.get('shot'); image.hidden = true; image.naturalWidth = 2; image.naturalHeight = 3
  elements.get('gesture').value = 'tap'
  const keys = ['BACK','HOME','APP_SWITCH'].map(key => { const button = new Element(); button.dataset.key = key; return button })
  const buttons = [...keys, new Element(), new Element()]
  const requests = [], intervals = []
  let status = initial, post = {ok:false, status:'pending', serial, seq:2}, clock = 1000000
  class FixtureDate extends Date { static now() { return clock } }
  const sandbox = {
    Date:FixtureDate, URLSearchParams, encodeURIComponent, console, AbortController,
    setTimeout:() => 1, clearTimeout:() => {},
    window:{},
    document:{getElementById:id => elements.get(id), querySelectorAll:selector => selector === '[data-key]' ? keys : buttons},
    setInterval:(fn, ms) => { intervals.push({fn, ms}); return intervals.length },
    fetch:async (url, options) => {
      requests.push({url, options, body:options.body ? JSON.parse(options.body) : null})
      const value = url.includes('/job') ? post : status
      if (value instanceof Error) throw value
      return {ok:!value.httpError, status:value.httpError || 200, json:async () => value}
    }
  }
  vm.runInNewContext(script, sandbox)
  const flush = () => new Promise(resolve => setImmediate(resolve))
  await flush()
  return {elements, image, buttons, keys, requests, functions:sandbox.window.__androidPhone, intervals,
    status(value) { status = value }, post(value) { post = value }, advance(ms) { clock += ms },
    load() { assert.equal(typeof image.onload, 'function'); image.onload() }, flush,
    async capture() {
      status = {ok:false, status:'pending', serial, seq:2, code:'pending', error:'queued'}
      await sandbox.window.__androidPhone.submit('shot')
      status = {...frame}; await sandbox.window.__androidPhone.refreshStatus(); this.load()
    }
  }
}

test('phone contains one real image surface and no synthetic apps or device indicators', () => {
  new vm.Script(script)
  assert.match(html, /<img id="shot"/)
  assert.doesNotMatch(html, /localStorage|Math\.random|canvas|snBest|openChat|APPS|qq\/send|qq\/message|[●]{2,}|70%/)
  assert.match(html, /data-needs-view disabled/)
  assert.match(html, /\/api\/android\//)
})

test('opening page never displays cached successful results or automatically operates a device', async () => {
  const f = await fixture({...frame, seq:99})
  assert.equal(f.elements.get('serial').value, serial)
  assert.equal(f.image.hidden, true)
  assert.equal(f.image.src, undefined)
  assert.ok(f.buttons.every(button => button.disabled))
  assert.equal(f.requests.filter(request => request.options.method === 'POST').length, 0)
  assert.match(f.elements.get('status').textContent, /获取新的设备画面/)
})

test('offline response preserves its error and cannot create an image or enabled actions', async () => {
  const f = await fixture({ok:false, status:'offline', serial, code:'worker_offline', error:'fixture worker offline'})
  assert.equal(f.image.hidden, true)
  assert.equal(f.elements.get('error').textContent, 'fixture worker offline')
  assert.ok(f.buttons.every(button => button.disabled))
  await f.functions.submit('key', {key:'HOME'})
  assert.equal(f.requests.filter(request => request.options.method === 'POST').length, 0)
})

test('pending is not success; actions enable only after matching screenshot actually loads', async () => {
  const f = await fixture()
  f.status({ok:false, status:'pending', serial, seq:2, code:'pending'})
  await f.functions.submit('shot')
  const job = f.requests.find(request => request.body)
  assert.deepEqual(job.body, {action:'shot', serial})
  assert.equal(f.image.hidden, true)
  assert.ok(f.buttons.every(button => button.disabled))
  f.status({...frame}); await f.functions.refreshStatus()
  assert.match(f.image.src, /serial=emulator-fixture&shot_id=a{32}$/)
  assert.equal(f.image.hidden, true)
  assert.ok(f.buttons.every(button => button.disabled))
  f.load()
  assert.equal(f.image.hidden, false)
  assert.ok(f.buttons.every(button => !button.disabled))
  assert.match(f.elements.get('status').textContent, /真实截图/)
})

test('fresh and old claims show read-only unknown-result diagnostics without replay or enabling actions', async () => {
  for (const age of [2, 86400]) {
    const f = await fixture()
    f.status({ok:false, status:'pending', serial, seq:2, code:'pending', claim_wait:{
      present:true, count:1, age_seconds:age, detail:'waiting', result:'unknown'}})
    await f.functions.submit('shot')
    const before = f.requests.filter(request => request.body).length
    assert.equal(f.elements.get('claimWait').hidden, false)
    assert.match(f.elements.get('claimWait').textContent, new RegExp('等待 ' + age + ' 秒'))
    assert.match(f.elements.get('claimWait').textContent, /仍有操作结果未确认.*先核对设备状态/)
    assert.doesNotMatch(f.elements.get('claimWait').textContent, /claim|worker|处理中|已停止|自动重试/)
    assert.equal(f.elements.get('capture').disabled, true)
    assert.ok(f.buttons.every(button => button.disabled))
    assert.equal(f.image.hidden, true)
    f.intervals.find(timer => timer.ms === 1500).fn(); await f.flush()
    await f.functions.submit('key', {key:'HOME'})
    await f.functions.submit('shot')
    assert.equal(f.requests.filter(request => request.body).length, before)
  }
})

test('unverifiable claim age is explicit and its diagnostics clear after queue completion', async () => {
  const f = await fixture({ok:false, status:'offline', serial, code:'worker_offline', error:'offline',
    claim_wait:{present:true, count:1, age_seconds:null, detail:'clock_anomaly', result:'unknown'}})
  assert.match(f.elements.get('claimWait').textContent, /等待时长暂无法确认/)
  assert.match(f.elements.get('claimWait').textContent, /仍有操作结果未确认/)
  assert.equal(f.elements.get('error').textContent, 'offline')
  assert.ok(f.buttons.every(button => button.disabled))
  f.status({ok:false, status:'error', serial, code:'capture_required', claim_wait:null})
  await f.functions.refreshStatus()
  assert.equal(f.elements.get('claimWait').hidden, true)
  assert.equal(f.elements.get('claimWait').textContent, '')
  assert.ok(f.buttons.every(button => button.disabled))
})

test('an action carries the displayed serial and shot id and immediately removes the old image', async () => {
  const f = await fixture(); await f.capture()
  f.post({ok:false, status:'pending', serial, seq:3})
  f.status({ok:false, status:'pending', serial, seq:3, code:'pending'})
  await f.functions.submit('key', {key:'HOME'})
  const jobs = f.requests.filter(request => request.body)
  assert.deepEqual(jobs[1].body, {key:'HOME', action:'key', serial, expected_shot_id:frame.shot_id})
  assert.equal(f.image.hidden, true)
  assert.equal(f.image.src, null)
  assert.ok(f.buttons.every(button => button.disabled))
  assert.match(f.elements.get('status').textContent, /等待/)
})

test('a failed action stays visible and later cached status cannot resurrect its old image', async () => {
  const f = await fixture(); await f.capture()
  f.post({httpError:409, ok:false, status:'error', serial, code:'stale_view', error:'fixture action refused'})
  await f.functions.submit('key', {key:'BACK'})
  assert.equal(f.elements.get('error').textContent, 'fixture action refused')
  assert.equal(f.image.hidden, true)
  f.status({...frame}); await f.functions.refreshStatus()
  assert.equal(f.image.hidden, true)
  assert.ok(f.buttons.every(button => button.disabled))
  assert.equal(f.elements.get('error').textContent, 'fixture action refused')
  const count = f.requests.filter(request => request.body).length
  await f.functions.submit('tap', {x:0, y:0})
  assert.equal(f.requests.filter(request => request.body).length, count)
})

test('old image load callbacks cannot restore a frame after a newer failure', async () => {
  const f = await fixture()
  f.status({ok:false, status:'pending', serial, seq:2}); await f.functions.submit('shot')
  f.status({...frame}); await f.functions.refreshStatus()
  const lateLoad = f.image.onload
  f.status(new Error('fixture status unavailable')); await f.functions.refreshStatus()
  lateLoad()
  assert.equal(f.image.hidden, true)
  assert.ok(f.buttons.every(button => button.disabled))
  assert.equal(f.elements.get('error').textContent, 'fixture status unavailable')
})

test('expired frames and wrong serial responses fail closed without old-frame operations', async () => {
  const f = await fixture(); await f.capture()
  f.advance(30001)
  f.intervals.find(timer => timer.ms === 250).fn()
  assert.equal(f.image.hidden, true)
  assert.ok(f.buttons.every(button => button.disabled))
  assert.match(f.elements.get('error').textContent, /过期/)
  f.status({...frame, serial:'another-device'}); await f.functions.refreshStatus()
  assert.equal(f.image.hidden, true)
  assert.match(f.elements.get('error').textContent, /serial/)
})

test('screenshot coordinates map to real pixels and gestures stay within worker protocol', async () => {
  const f = await fixture(); await f.capture()
  f.post({ok:false, status:'pending', serial, seq:3}); f.status({ok:false, status:'pending', serial, seq:3})
  f.elements.get('gesture').value = 'swipe'
  f.image.handlers.get('pointerdown')({button:0, clientX:11, clientY:21, pointerId:1, preventDefault(){}})
  f.image.handlers.get('pointerup')({clientX:160, clientY:270, pointerId:1})
  await f.flush()
  const job = f.requests.filter(request => request.body).at(-1).body
  assert.deepEqual(job, {action:'swipe', serial, expected_shot_id:frame.shot_id, x1:0, y1:0, x2:1, y2:2, ms:300})
})

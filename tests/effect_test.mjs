/**
 * 每个按钮都点，并且**验证它真的生效了**。
 *
 * 之前的 click_all.mjs 只看有没有抛异常。结果「保存额度」这种
 * 接口返回 ok 但其实一个字都没写的按钮，一直是绿的 —— 用户点了说没反应，
 * 我才发现测试根本没验到东西。
 *
 * 这里的做法：点之前读一次状态，点之后重新读，比对是否变了。
 */
import { spawn } from 'node:child_process'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'
import { requireBrowserFixture, installFixtureBrowserNetwork, fetchFixtureApi } from './browser-fixture-guard.mjs'

const fixture = await requireBrowserFixture()

const PANEL = fixture.origin + '/lanya-panel.html'
const API = fixture.origin
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
const PORT = fixture.debugPort
const OUT = fixture.outDir
mkdirSync(OUT, { recursive: true })

const proc = spawn(EDGE, [
  '--enable-automation', '--headless=new', `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${join(OUT, '_profile')}`,
  '--no-first-run', '--no-default-browser-check', '--disable-gpu',
  '--window-size=1400,1000', 'about:blank',
], { stdio: 'ignore' })

async function target() {
  for (let i = 0; i < 40; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/list`, { redirect: 'error' })
      const l = await r.json()
      const p = l.find(t => t.type === 'page')
      if (p?.webSocketDebuggerUrl) return p.webSocketDebuggerUrl
    } catch { }
    await sleep(300)
  }
  throw new Error('CDP down')
}

const ws = new WebSocket(await target())
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej })
await installFixtureBrowserNetwork(ws, fixture)
let id = 0
const pend = new Map()
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data)
  if (m.id && pend.has(m.id)) {
    const { res, rej } = pend.get(m.id); pend.delete(m.id)
    m.error ? rej(new Error(m.error.message)) : res(m.result)
  }
}
function send(method, params = {}) {
  const i = ++id
  return new Promise((res, rej) => {
    pend.set(i, { res, rej })
    ws.send(JSON.stringify({ id: i, method, params }))
  })
}
async function ev(expr) {
  const r = await send('Runtime.evaluate', {
    expression: expr, awaitPromise: true, returnByValue: true,
  })
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || 'eval')
  return r.result.value
}
async function goto(url) {
  await send('Page.enable')
  await send('Page.navigate', { url })
  await sleep(2600)
}
async function getJson(p) {
  const r = await fetchFixtureApi(fixture, API + p, { cache: 'no-store' })
  return r.json()
}
async function go(page) {
  await ev(`document.querySelector('.navitem[data-page="${page}"]').click()`)
  await sleep(1400)
}

// ---------------------------------------------------------------------------
const results = []
function check(name, ok, detail) {
  results.push({ name, ok, detail })
  console.log((ok ? '  ✓ ' : '  ✗ ') + name + (detail ? '   ' + detail : ''))
}

await goto(PANEL)
await sleep(1500)

// ---------- 1. 零花钱：保存额度 ----------
console.log('\n=== 零花钱 ===')
await go('money')
const b0 = (await getJson('/api/budget')).budget
const want = +(Math.abs(b0.limit - 3.5) < 0.01 ? 4.5 : 3.5).toFixed(1)

await ev(`(function(){
  const i = document.getElementById('bLimit');
  i.value = '${want}';
  i.dispatchEvent(new Event('input', {bubbles:true}));
  i.dispatchEvent(new Event('change', {bubbles:true}));
})()`)
const typed = await ev(`document.getElementById('bLimit').value`)
await ev(`(function(){
  const btn = [...document.querySelectorAll('#page-money button')]
    .find(x => x.textContent.trim() === '保存额度');
  if (!btn) throw new Error('no save button');
  btn.click();
})()`)
await sleep(2000)
const b1 = (await getJson('/api/budget')).budget
check('保存额度：框里打进去了', String(typed) === String(want), '框=' + typed)
check('保存额度：服务端真变了',
  Math.abs(b1.limit - want) < 0.01,
  `${b0.limit} -> ${b1.limit}（期望 ${want}）`)

// ---------- 2. 零花钱：确认（记一笔） ----------
const c0 = (await getJson('/api/budget')).budget
await ev(`(function(){
  const i = document.getElementById('topupAmt');
  if (!i) throw new Error('no bAmt');
  i.value = '-0.1';
  i.dispatchEvent(new Event('input', {bubbles:true}));
  const btn = [...document.querySelectorAll('#page-money button')]
    .find(x => x.textContent.trim() === '确认');
  if (!btn) throw new Error('no ok button');
  btn.click();
})()`)
await sleep(1800)
const c1 = (await getJson('/api/budget')).budget
check('确认：负数是记花费', c1.spent > c0.spent, `${c0.spent} -> ${c1.spent}`)

// ---------- 3. 群发言：保存 ----------
console.log('\n=== 群发言 ===')
await go('talk')
const t0 = (await getJson('/api/tunables')).tunables || {}
const tWant = (t0.QuietMinutes || 120) === 90 ? 100 : 90
await ev(`(function(){
  const i = document.getElementById('tQuietMinutes');
  if (!i) throw new Error('no tIdle');
  i.value = '${tWant}';
  i.dispatchEvent(new Event('input', {bubbles:true}));
  const btn = [...document.querySelectorAll('#page-talk button')]
    .find(x => /保存/.test(x.textContent));
  if (!btn) throw new Error('no save button');
  btn.click();
})()`)
await sleep(1800)
const t1 = (await getJson('/api/tunables')).tunables || {}
check('群发言：空闲阈值存住了',
  t1.QuietMinutes === tWant,
  `${t0.QuietMinutes} -> ${t1.QuietMinutes}（期望 ${tWant}）`)

// ---------- 4. 记忆：加一条 ----------
console.log('\n=== 记忆 ===')
await go('memory')
const m0 = await getJson('/api/memory')
const n0 = (m0.memory?.entries || []).length
const probe = 'effect-' + Date.now()
await ev(`(function(){
  const i = document.getElementById('memNew');
  if (!i) throw new Error('no memNew');
  i.value = '${probe}';
  const btn = [...document.querySelectorAll('#page-memory button')]
    .find(x => x.textContent.trim() === '记住');
  if (!btn) throw new Error('no remember button');
  btn.click();
})()`)
await sleep(2200)
const m1 = await getJson('/api/memory')
const ents = m1.memory?.entries || []
check('记忆：条数 +1', ents.length === n0 + 1, `${n0} -> ${ents.length}`)
check('记忆：内容对', ents.some(e => (e.text || '').includes(probe)), probe)

// ---------- 5. 她在干嘛：开关 ----------
console.log('\n=== 她在干嘛 ===')
await go('activity')
const a0 = await getJson('/api/activities')
const list0 = a0.activities || []
if (list0.length) {
  const was = list0[0].enabled
  await ev(`(function(){
    const box = document.querySelector('#actList input[type=checkbox]');
    if (box) box.click();
  })()`)
  await sleep(700)
  await ev(`(function(){
    const btn = [...document.querySelectorAll('#page-activity button')]
      .find(x => /保存/.test(x.textContent));
    if (btn) btn.click();
  })()`)
  await sleep(1800)
  const a1 = await getJson('/api/activities')
  const now = (a1.activities || [])[0]?.enabled
  check('活动开关：改了并存住', now !== was, `${was} -> ${now}`)
  // 还原
  await ev(`(function(){
    const box = document.querySelector('#actList input[type=checkbox]');
    if (box) box.click();
    const btn = [...document.querySelectorAll('#page-activity button')]
      .find(x => /保存/.test(x.textContent));
    if (btn) btn.click();
  })()`)
  await sleep(1200)
}

// ---------- 6. 人设：保存 ----------
console.log('\n=== 人设 ===')
await go('persona')
const p0 = await getJson('/api/persona')
const orig = p0.persona?.text || p0.text || ''
const mark = '\n\n<!-- effect-probe -->\n'
await ev(`(function(){
  const ta = document.querySelector('#page-persona textarea');
  if (!ta) throw new Error('no textarea');
  ta.value = ta.value + ${JSON.stringify(mark)};
  const btn = [...document.querySelectorAll('#page-persona button')]
    .find(x => /保存/.test(x.textContent));
  if (!btn) throw new Error('no save button');
  btn.click();
})()`)
await sleep(1800)
const p1 = await getJson('/api/persona')
const txt1 = p1.persona?.text || p1.text || ''
check('人设：改动能存住', txt1.includes('effect-probe'), txt1.length + ' 字符')

// 还原人设
if (orig) {
  await fetchFixtureApi(fixture, API + '/api/persona', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ text: orig }),
  })
  await sleep(600)
}

// ---------- 汇总 ----------
const bad = results.filter(r => !r.ok)
console.log('\n=== 汇总 ===')
console.log('  检查项 ' + results.length + '，失败 ' + bad.length)
if (bad.length) {
  bad.forEach(r => console.log('    ✗ ' + r.name + '   ' + r.detail))
  process.exitCode = 1
}
try { proc.kill() } catch { }
try { ws.close() } catch { }

/**
 * 截关键页面的图 + 真点图片查看，用来肉眼确认。
 */
import { spawn } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'

const URL_BASE = 'http://127.0.0.1:19388/dafeiyu-panel.html'
const OUT = '<WORKDIR>\\大肥鱼\\made\\_uitest'
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
const PORT = 9336

mkdirSync(OUT, { recursive: true })
const proc = spawn(EDGE, [
  '--headless=new', `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${join(OUT, '_p4')}`,
  '--no-first-run', '--no-default-browser-check', '--disable-gpu',
  '--window-size=1500,1100', 'about:blank',
], { stdio: 'ignore' })

async function target() {
  for (let i = 0; i < 40; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/list`)
      const l = await r.json()
      const p = l.find(t => t.type === 'page')
      if (p?.webSocketDebuggerUrl) return p.webSocketDebuggerUrl
    } catch {}
    await sleep(300)
  }
  throw new Error('down')
}

const ws = new WebSocket(await target())
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej })
let id = 0
const pend = new Map()
let events = []
ws.onmessage = (e) => {
  const m = JSON.parse(e.data)
  if (m.id && pend.has(m.id)) {
    const { res, rej } = pend.get(m.id); pend.delete(m.id)
    m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result)
  } else if (m.method) events.push(m)
}
const send = (method, params = {}) => new Promise((res, rej) => {
  const i = ++id
  pend.set(i, { res, rej })
  ws.send(JSON.stringify({ id: i, method, params }))
  setTimeout(() => { if (pend.has(i)) { pend.delete(i); rej(new Error('timeout')) } }, 30000)
})
const ev = async (x) => (await send('Runtime.evaluate', { expression: x, returnByValue: true, awaitPromise: true })).result?.value
const shot = async (name) => {
  const r = await send('Page.captureScreenshot', { format: 'png' })
  writeFileSync(join(OUT, name), Buffer.from(r.data, 'base64'))
  console.log('   存图 ' + name)
}

try {
  await send('Runtime.enable'); await send('Log.enable'); await send('Page.enable')
  await send('Page.navigate', { url: URL_BASE })
  await sleep(2500)

  // ---- 表情包页 ----
  await ev(`document.querySelector('.navitem[data-page="memes"]').click()`)
  await sleep(2000)
  const tabs = await ev(`[...document.querySelectorAll('#memeTabs .tab')].map(t=>t.textContent.trim())`)
  console.log('表情标签: ' + tabs.join(' | '))
  const imgs = await ev(`(function(){
    const g = document.querySelectorAll('#memeGrid img');
    return { n: g.length, broken: [...g].filter(i => !i.complete || i.naturalWidth === 0).length };
  })()`)
  console.log('表情图: ' + imgs.n + ' 张，坏图 ' + imgs.broken)
  await shot('v-memes.png')

  // ---- 作品页 + 归档 ----
  await ev(`document.querySelector('.navitem[data-page="works"]').click()`)
  await sleep(1500)
  const madeCount = await ev(`document.querySelectorAll('#worksBody tr').length`)
  console.log('\n作品页（made）行数: ' + madeCount)

  await ev(`document.querySelector('#workTabs .tab[data-ext=".archive"]').click()`)
  await sleep(2500)
  const arcCount = await ev(`document.querySelectorAll('#worksBody tr').length`)
  console.log('归档标签行数: ' + arcCount)
  await shot('v-works-archive.png')

  // 真点一张图
  const clicked = await ev(`(function(){
    const rows = [...document.querySelectorAll('#worksBody tr')];
    for(const r of rows){
      const nm = r.querySelector('.nm')?.textContent || '';
      if(/\\.(png|jpg|jpeg|gif|webp)$/i.test(nm)){
        r.querySelector('button')?.click();
        return nm;
      }
    }
    return 'no image row';
  })()`)
  console.log('点开的图: ' + clicked)
  await sleep(2500)
  const viewer = await ev(`(function(){
    const v = document.getElementById('worksViewer');
    const img = v.querySelector('img');
    return { on: v.classList.contains('on'),
             hasImg: !!img,
             loaded: img ? (img.complete && img.naturalWidth > 0) : false,
             w: img ? img.naturalWidth : 0 };
  })()`)
  console.log('查看器: on=' + viewer.on + ' 有图=' + viewer.hasImg
    + ' 加载成功=' + viewer.loaded + ' 宽=' + viewer.w)
  await shot('v-work-image.png')

  // ---- 记忆页 ----
  await ev(`document.querySelector('.navitem[data-page="memory"]').click()`)
  await sleep(1800)
  const mem = await ev(`(function(){
    const list = document.getElementById('memPropList');
    return { rows: list.querySelectorAll('.frow').length,
             text: list.innerText.slice(0, 200) };
  })()`)
  console.log('\n记忆条目: ' + mem.rows + ' 条')
  console.log('  ' + mem.text.replace(/\\n/g, ' / ').slice(0, 160))
  await shot('v-memory.png')

  const errs = events.filter(e =>
    e.method === 'Runtime.exceptionThrown' ||
    (e.method === 'Log.entryAdded' && e.params.entry.level === 'error'))
  console.log('\n错误数: ' + errs.length)
} catch (e) {
  console.log('失败: ' + e.message)
} finally {
  try { proc.kill() } catch {}
  process.exit(0)
}

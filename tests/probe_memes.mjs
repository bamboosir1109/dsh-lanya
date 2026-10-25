/**
 * 专门探测表情包页的标签文字，顺便真点几个按钮。
 *
 * 通用 browser_test.mjs 只取前 260 字，看不到标签全貌；这里直接读 DOM。
 */
import { spawn } from 'node:child_process'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'

const URL_BASE = process.argv[2] || 'http://127.0.0.1:19388/dafeiyu-panel.html'
const OUT = '<WORKDIR>\\大肥鱼\\made\\_uitest'
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
const PORT = 9334

mkdirSync(OUT, { recursive: true })
const proc = spawn(EDGE, [
  '--headless=new', `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${join(OUT, '_profile2')}`,
  '--no-first-run', '--no-default-browser-check', '--disable-gpu',
  '--window-size=1400,1000', 'about:blank',
], { stdio: 'ignore' })

async function target() {
  for (let i = 0; i < 40; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/list`)
      const list = await r.json()
      const p = list.find(t => t.type === 'page')
      if (p?.webSocketDebuggerUrl) return p.webSocketDebuggerUrl
    } catch {}
    await sleep(300)
  }
  throw new Error('CDP down')
}

const ws = new WebSocket(await target())
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej })
let id = 0
const pend = new Map()
const events = []
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data)
  if (m.id && pend.has(m.id)) {
    const { res, rej } = pend.get(m.id); pend.delete(m.id)
    m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result)
  } else if (m.method) events.push(m)
}
const send = (method, params = {}) => new Promise((res, rej) => {
  const i = ++id
  pend.set(i, { res, rej })
  ws.send(JSON.stringify({ id: i, method, params }))
  setTimeout(() => { if (pend.has(i)) { pend.delete(i); rej(new Error('timeout ' + method)) } }, 30000)
})
const evalJS = async (expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })
  return r.result?.value
}

try {
  await send('Runtime.enable')
  await send('Page.enable')
  await send('Page.navigate', { url: URL_BASE })
  await sleep(2500)

  // 去表情包页
  await evalJS(`document.querySelector('.navitem[data-page="memes"]').click()`)
  await sleep(1600)

  const tabs = await evalJS(`[...document.querySelectorAll('#memeTabs .tab')].map(t => t.textContent.trim())`)
  console.log('=== 表情包标签（' + tabs.length + ' 个）===')
  tabs.forEach(t => console.log('  ' + t))

  const total = await evalJS(`document.getElementById('memeTotal').textContent`)
  console.log('\n总数:', total)

  // 真点一次标签，看能不能切换
  const switched = await evalJS(`(function(){
    const tabs = [...document.querySelectorAll('#memeTabs .tab')];
    if(tabs.length < 2) return 'only one tab';
    const before = document.querySelectorAll('#memeGrid .meme').length;
    tabs[1].click();
    return before + ' -> ' + document.querySelectorAll('#memeGrid .meme').length;
  })()`)
  console.log('点第二个标签，格子数:', switched)

  // 真点一次「重新读取」（开关卡片里的按钮）
  const reload = await evalJS(`(function(){
    const b = [...document.querySelectorAll('#page-memes button')].find(x => x.textContent.includes('重新读取'));
    if(!b) return 'no button';
    b.click();
    return 'clicked';
  })()`)
  console.log('点「重新读取」:', reload)
  await sleep(1200)
  const stealVal = await evalJS(`document.getElementById('mcfgSteal')?.checked`)
  console.log('偷图开关读回来的值:', stealVal)

  // 真点一次作品页的「网页」标签
  await evalJS(`document.querySelector('.navitem[data-page="works"]').click()`)
  await sleep(1500)
  const workSwitch = await evalJS(`(function(){
    const before = document.querySelectorAll('#worksBody tr').length;
    const t = document.querySelector('#workTabs .tab[data-ext=".html"]');
    if(!t) return 'no tab';
    t.click();
    return before + ' -> ' + document.querySelectorAll('#worksBody tr').length;
  })()`)
  console.log('\n作品页点「网页」标签，行数:', workSwitch)

  // 真点一次记忆页的「人物档案」标签
  await evalJS(`document.querySelector('.navitem[data-page="memory"]').click()`)
  await sleep(1500)
  const memSwitch = await evalJS(`(function(){
    const t = document.querySelector('#fileTabs .tab[data-dir="people"]');
    if(!t) return 'no tab';
    t.click();
    return 'clicked';
  })()`)
  await sleep(1400)
  const memRows = await evalJS(`document.querySelectorAll('#fileList .frow').length`)
  console.log('记忆页点「人物档案」:', memSwitch, ' 行数:', memRows)

  const errs = events.filter(e =>
    e.method === 'Runtime.exceptionThrown' ||
    (e.method === 'Log.entryAdded' && e.params.entry.level === 'error') ||
    (e.method === 'Runtime.consoleAPICalled' && e.params.type === 'error'))
  console.log('\n错误数:', errs.length)
  errs.forEach(e => console.log('  ' + JSON.stringify(e.params).slice(0, 200)))
} catch (e) {
  console.log('失败: ' + e.message)
} finally {
  try { proc.kill() } catch {}
  process.exit(0)
}

/**
 * 真点记忆页的增删：输入框打字 → 点「记住」→ 确认列表多一条 → 点 × 删掉 → 确认少一条。
 */
import { spawn } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'

const URL_BASE = 'http://127.0.0.1:19388/dafeiyu-panel.html'
const OUT = '<WORKDIR>\\dafeiyu\\made\\_uitest'
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
const PORT = 9337

mkdirSync(OUT, { recursive: true })
const proc = spawn(EDGE, [
  '--headless=new', `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${join(OUT, '_p5')}`,
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
const send = (m, p = {}) => new Promise((res, rej) => {
  const i = ++id
  pend.set(i, { res, rej })
  ws.send(JSON.stringify({ id: i, method: m, params: p }))
  setTimeout(() => { if (pend.has(i)) { pend.delete(i); rej(new Error('timeout')) } }, 30000)
})
const ev = async (x) => (await send('Runtime.evaluate', { expression: x, returnByValue: true, awaitPromise: true })).result?.value

try {
  await send('Runtime.enable'); await send('Log.enable'); await send('Page.enable')
  await send('Page.navigate', { url: URL_BASE })
  await sleep(2500)

  await ev(`document.querySelector('.navitem[data-page="memory"]').click()`)
  await sleep(2000)

  const before = await ev(`document.querySelectorAll('#memPropList .frow').length`)
  console.log('记忆条目（加之前）: ' + before)

  // 真打字 + 真点
  const typed = await ev(`(function(){
    const i = document.getElementById('memNew');
    if(!i) return 'no input';
    i.value = '这是测试条目，用来验证记忆能不能加';
    i.dispatchEvent(new Event('input', {bubbles:true}));
    const b = [...document.querySelectorAll('#page-memory button')].find(x => x.textContent.includes('记住'));
    if(!b) return 'no button';
    b.click();
    return 'clicked';
  })()`)
  console.log('输入并点「记住」: ' + typed)
  await sleep(5000)

  const after = await ev(`document.querySelectorAll('#memPropList .frow').length`)
  const hasTest = await ev(`document.getElementById('memPropList').innerText.includes('这是测试条目')`)
  console.log('记忆条目（加之后）: ' + after + '，包含测试条目: ' + hasTest)

  // 真点第一条的 ×
  const delClicked = await ev(`(function(){
    const first = document.querySelector('#memPropList .frow .fdel');
    if(!first) return 'no delete button';
    first.click();
    return 'clicked';
  })()`)
  console.log('点第一条的 ×: ' + delClicked)
  await sleep(5000)

  const final = await ev(`document.querySelectorAll('#memPropList .frow').length`)
  const stillTest = await ev(`document.getElementById('memPropList').innerText.includes('这是测试条目')`)
  console.log('记忆条目（删之后）: ' + final + '，还有测试条目: ' + stillTest)

  const r = await send('Page.captureScreenshot', { format: 'png' })
  writeFileSync(join(OUT, 'v-memory-crud.png'), Buffer.from(r.data, 'base64'))

  const errs = events.filter(e =>
    e.method === 'Runtime.exceptionThrown' ||
    (e.method === 'Log.entryAdded' && e.params.entry.level === 'error'))
  console.log('\n错误数: ' + errs.length)
  errs.slice(0, 3).forEach(e => console.log('  ' + JSON.stringify(e.params).slice(0, 150)))
} catch (e) {
  console.log('失败: ' + e.message)
} finally {
  try { proc.kill() } catch {}
  process.exit(0)
}

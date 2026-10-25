/**
 * 用真浏览器逐页点一遍面板，抓控制台错误 + 截图。
 *
 * 为什么必须这样：后台 curl 只能证明接口活着，证明不了页面能点。
 * 用户看到的报错是 "Cannot set properties of null"，这类只有真的跑一遍
 * JS 才会暴露。
 *
 * 用 Edge 的 CDP（Chrome DevTools Protocol）驱动，不依赖 puppeteer。
 *
 *   node browser_test.mjs [url] [outdir]
 */
import { spawn } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'

const URL_BASE = process.argv[2] || 'http://127.0.0.1:19388/lanya-panel.html'
const OUT = process.argv[3] || '<WORKDIR>\\岚娅\\made\\_uitest'
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
const PORT = 9333

const PAGES = [
  ['overview', '概览'],
  ['system', '部件与插件'],
  ['talk', '群发言'],
  ['money', '零花钱'],
  ['memes', '表情包'],
  ['schedule', '定时'],
  ['tools', '工具'],
  ['bili', '刷 B 站'],
  ['memory', '记忆'],
  ['works', '作品'],
  ['persona', '人设'],
  ['identity', '身份'],
]

mkdirSync(OUT, { recursive: true })

const userDataDir = join(OUT, '_profile')
const proc = spawn(EDGE, [
  '--headless=new',
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${userDataDir}`,
  '--no-first-run',
  '--no-default-browser-check',
  '--disable-gpu',
  '--window-size=1400,1000',
  'about:blank',
], { stdio: 'ignore', detached: false })

async function getTarget() {
  for (let i = 0; i < 40; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/list`)
      const list = await r.json()
      const page = list.find(t => t.type === 'page')
      if (page && page.webSocketDebuggerUrl) return page.webSocketDebuggerUrl
    } catch {}
    await sleep(300)
  }
  throw new Error('Edge CDP 没起来')
}

class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.events = [] }
  static async connect(url) {
    const ws = new WebSocket(url)
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej })
    const c = new CDP(ws)
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data)
      if (msg.id && c.pending.has(msg.id)) {
        const { res, rej } = c.pending.get(msg.id)
        c.pending.delete(msg.id)
        msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result)
      } else if (msg.method) {
        c.events.push(msg)
      }
    }
    return c
  }
  send(method, params = {}) {
    const id = ++this.id
    return new Promise((res, rej) => {
      this.pending.set(id, { res, rej })
      this.ws.send(JSON.stringify({ id, method, params }))
      setTimeout(() => {
        if (this.pending.has(id)) { this.pending.delete(id); rej(new Error('timeout ' + method)) }
      }, 30000)
    })
  }
  drain() { const e = this.events.slice(); this.events = []; return e }
}

const results = []

try {
  const wsUrl = await getTarget()
  const cdp = await CDP.connect(wsUrl)

  await cdp.send('Runtime.enable')
  await cdp.send('Log.enable')
  await cdp.send('Page.enable')
  await cdp.send('Console.enable')

  await cdp.send('Page.navigate', { url: URL_BASE })
  await sleep(2500)

  for (const [key, label] of PAGES) {
    // 清空事件缓冲，只收这一页的
    cdp.drain()

    // 点导航
    const clickRes = await cdp.send('Runtime.evaluate', {
      expression: `(function(){
        const el = document.querySelector('.navitem[data-page="${key}"]');
        if(!el) return 'NO_NAV';
        if(el.style.display === 'none') return 'HIDDEN';
        el.click();
        return 'OK';
      })()`,
      returnByValue: true,
    })
    const clicked = clickRes.result.value

    await sleep(1300)

    // 收集这一页里所有可见元素的空值/异常迹象
    const probe = await cdp.send('Runtime.evaluate', {
      expression: `(function(){
        const pg = document.getElementById('page-${key}');
        const out = { visible: !!(pg && pg.classList.contains('on')) };
        if(!pg) return out;
        // 每个 tbody 有多少行、每个容器有没有内容
        out.tables = [...pg.querySelectorAll('tbody')].map(tb => ({
          id: tb.id, rows: tb.children.length
        }));
        out.emptyBlocks = [...pg.querySelectorAll('.card')].map(card => {
          const h = card.querySelector('h3');
          const bodyLen = card.innerText.replace(/\\s+/g,'').length;
          return { title: h ? h.textContent.trim() : '?', chars: bodyLen };
        });
        out.inputs = [...pg.querySelectorAll('input,textarea')].map(i => ({
          id: i.id, value: String(i.value || '').slice(0, 24)
        }));
        out.text = pg.innerText.replace(/\\s+/g,' ').slice(0, 260);
        return out;
      })()`,
      returnByValue: true,
    })

    // 截图
    let shot = null
    try {
      const s = await cdp.send('Page.captureScreenshot', { format: 'png' })
      shot = join(OUT, `page-${key}.png`)
      writeFileSync(shot, Buffer.from(s.data, 'base64'))
    } catch {}

    // 这一页产生的错误
    const events = cdp.drain()
    const errors = []
    for (const e of events) {
      if (e.method === 'Runtime.exceptionThrown') {
        errors.push('EXCEPTION: ' + (e.params.exceptionDetails?.exception?.description
          || e.params.exceptionDetails?.text || '?').slice(0, 200))
      }
      if (e.method === 'Log.entryAdded' && e.params.entry.level === 'error') {
        errors.push('LOG: ' + (e.params.entry.text || '').slice(0, 200))
      }
      if (e.method === 'Runtime.consoleAPICalled' && e.params.type === 'error') {
        errors.push('CONSOLE: ' + (e.params.args || []).map(a => a.description || a.value || '').join(' ').slice(0, 200))
      }
    }

    results.push({ key, label, clicked, shot, errors, probe: probe.result.value })
    console.log(`[${clicked}] ${label}  错误 ${errors.length} 条`)
    for (const e of errors) console.log('    ' + e)
  }

  writeFileSync(join(OUT, 'result.json'), JSON.stringify(results, null, 2), 'utf8')
  console.log('\n结果写到 ' + join(OUT, 'result.json'))

  // 汇总
  const bad = results.filter(r => r.errors.length > 0 || r.clicked !== 'OK')
  console.log('\n=== 汇总 ===')
  console.log(`页数 ${results.length}，有问题 ${bad.length}`)
  for (const b of bad) {
    console.log(`  ✗ ${b.label}: click=${b.clicked} errors=${b.errors.length}`)
  }
} catch (e) {
  console.log('测试失败: ' + e.message)
} finally {
  try { proc.kill() } catch {}
  process.exit(0)
}

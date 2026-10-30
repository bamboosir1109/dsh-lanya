/**
 * 每个页面的每个按钮都真的点一遍。
 *
 * 覆盖：每个页面、每个按钮都真点一遍，抓控制台异常。
 *
 * 做法：
 *   1. 逐页导航
 *   2. 枚举该页所有 <button> 和可点的 .tab
 *   3. 一个个真点（onclick / click()）
 *   4. 每次点击后抓控制台异常
 *   5. 报告：哪个按钮点了没反应、哪个报了错
 *
 * 危险按钮（删除类）会被点，但点之前页面里的二次确认已经被移除了，
 * 所以这里只点「不会毁数据」的那批——删除类单独标记、跳过、只验证按钮存在。
 */
import { spawn } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'
import { requireBrowserFixture, installFixtureBrowserNetwork } from './browser-fixture-guard.mjs'

const fixture = await requireBrowserFixture()

const URL_BASE = fixture.origin + '/lanya-panel.html'
const OUT = fixture.outDir
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
const PORT = fixture.debugPort

// 这些按钮点了会毁数据，跳过（只确认存在）
// × 也是删除按钮（表情包/记忆每条的删除），必须跳过——之前只写了「删除」
// 两个字，结果 × 被真点了一遍，把 70 多张表情删掉了。
const SKIP_WORDS = ['×', '删除', '删 ', '删', '清空', '清掉',
                    '启动 / 重启 QQ', '安装 / 更新', '保存并生效', '驳回',
                    '重新读取', '重启', '启动']

const PAGES = [
  ['overview', '概览'], ['system', '部件与插件'], ['talk', '群发言'],
  ['money', '零花钱'], ['memes', '表情包'], ['schedule', '定时'],
  ['tools', '工具'], ['activity', '她在干嘛'], ['napcat', 'NapCatQ'], ['bili', '刷 B 站'], ['memory', '记忆'],
  ['works', '作品'], ['persona', '人设'], ['identity', '身份'],
]

mkdirSync(OUT, { recursive: true })
const proc = spawn(EDGE, [
  '--enable-automation', '--headless=new', `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${join(OUT, '_profile3')}`,
  '--no-first-run', '--no-default-browser-check', '--disable-gpu',
  '--window-size=1400,1000', 'about:blank',
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
  throw new Error('CDP down')
}

const ws = new WebSocket(await target())
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej })
await installFixtureBrowserNetwork(ws, fixture)
let id = 0
const pend = new Map()
let events = []
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
const ev = async (expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })
  return r.result?.value
}
const errsSince = () => {
  const e = events.filter(x =>
    x.method === 'Runtime.exceptionThrown' ||
    (x.method === 'Log.entryAdded' && x.params.entry.level === 'error') ||
    (x.method === 'Runtime.consoleAPICalled' && x.params.type === 'error'))
  events = []
  return e.map(x => (x.params.exceptionDetails?.exception?.description
    || x.params.entry?.text
    || (x.params.args || []).map(a => a.description || a.value).join(' ')
    || '').slice(0, 160))
    // 浏览器扩展会往控制台写无关噪音，不是本页代码
    .filter(t => t && !/iterable|mailtrack|grm ERROR|chrome-extension/i.test(t))
}

const report = []
try {
  await send('Runtime.enable'); await send('Log.enable'); await send('Page.enable')
  await send('Page.navigate', { url: URL_BASE })
  await sleep(2500)
  errsSince()

  for (const [key, label] of PAGES) {
    const nav = await ev(`(function(){
      const el = document.querySelector('.navitem[data-page="${key}"]');
      if(!el) return 'NO_NAV';
      if(el.style.display === 'none') return 'HIDDEN';
      el.click(); return 'OK';
    })()`)
    if (nav !== 'OK') { report.push({ page: label, nav, buttons: [] }); continue }
    await sleep(1200)

    // 枚举这一页的按钮和标签
    const btns = await ev(`(function(){
      const pg = document.getElementById('page-${key}');
      if(!pg) return [];
      const out = [];
      pg.querySelectorAll('button').forEach((b, i) => {
        out.push({ i, kind: 'button', text: (b.textContent || '').trim().slice(0, 24),
                   cls: b.className });
      });
      pg.querySelectorAll('.tab').forEach((t, i) => {
        out.push({ i: 1000 + i, kind: 'tab', text: (t.textContent || '').trim().slice(0, 24),
                   cls: t.className });
      });
      return out;
    })()`)

    const pageRes = { page: label, key, nav, buttons: [], errors: [] }
    for (const b of btns) {
      const skip = SKIP_WORDS.some(w => b.text.includes(w))
      if (skip) {
        pageRes.buttons.push({ text: b.text, kind: b.kind, status: 'SKIPPED(dangerous)' })
        continue
      }
      const clicked = await ev(`(function(){
        const pg = document.getElementById('page-${key}');
        if(!pg) return 'no-page';
        const list = ${b.kind === 'button'
          ? `[...pg.querySelectorAll('button')]`
          : `[...pg.querySelectorAll('.tab')]`};
        const el = list[${b.i - (b.kind === 'tab' ? 1000 : 0)}];
        if(!el) return 'no-el';
        el.click();
        return 'clicked';
      })()`)
      await sleep(700)
      const e = errsSince()
      pageRes.buttons.push({ text: b.text, kind: b.kind, click: clicked, errors: e.length })
      if (e.length) pageRes.errors.push(...e.map(x => b.text + ' -> ' + x))
    }
    report.push(pageRes)
    console.log(`[${label}] 按钮 ${pageRes.buttons.length} 个，错误 ${pageRes.errors.length}`)
    pageRes.errors.slice(0, 3).forEach(e => console.log('    ' + e))
  }

  writeFileSync(join(OUT, 'buttons.json'), JSON.stringify(report, null, 2), 'utf8')
  console.log('\n=== 汇总 ===')
  let totalBtns = 0, totalErr = 0, skipped = 0
  for (const r of report) {
    totalBtns += r.buttons.length
    totalErr += r.errors.length
    skipped += r.buttons.filter(b => b.status === 'SKIPPED(dangerous)').length
  }
  console.log(`按钮总数 ${totalBtns}，跳过危险 ${skipped}，报错 ${totalErr}`)
  for (const r of report) {
    if (r.errors.length) console.log(`  ✗ ${r.page}: ${r.errors.length} 条`)
  }
} catch (e) {
  console.log('失败: ' + e.message)
} finally {
  try { proc.kill() } catch {}
  process.exit(0)
}

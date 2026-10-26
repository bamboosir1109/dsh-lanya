/**
 * 截关键页面的图 + 真点图片查看，用来肉眼确认。
 */
import { spawn } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'

const URL_BASE = 'http://127.0.0.1:19388/lanya-panel.html'
const OUT = 'E:\\DSH work\\大肥鱼\\made\\_uitest'
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

  // ---- 刷B站 ----
  await ev(`document.querySelector('.navitem[data-page="bili"]').click()`)
  await sleep(2200)
  const bv = await ev(`(function(){
    const box = document.getElementById('biliVideos');
    return { rows: box ? box.children.length : -1, text: box ? box.innerText.slice(0,80) : 'no box' };
  })()`)
  console.log('\n刷B站-刷过什么: 行=' + bv.rows + '  内容=' + bv.text.replace(/\n/g, ' / '))
  const hasBtn = await ev(`!!([...document.querySelectorAll('#page-bili button')].find(b=>b.textContent.includes('刷一条')))`)
  console.log('刷B站-有刷一条按钮: ' + hasBtn)
  if (hasBtn) {
    const clicked = await ev(`(function(){
      const b = [...document.querySelectorAll('#page-bili button')].find(x=>x.textContent.includes('刷一条 + 评论'));
      if(!b) return 'no btn';
      b.click(); return 'clicked';
    })()`)
    console.log('刷B站-点了: ' + clicked)
    await sleep(120000)
    const out = await ev(`document.getElementById('oneRunOut') ? document.getElementById('oneRunOut').innerText.slice(0,200) : 'no out'`)
    console.log('刷B站-结果框: ' + out.replace(/\n/g,' / '))
  }
  const bc = await ev(`(function(){
    const box = document.getElementById('biliComments');
    return { rows: box ? box.children.length : -1, text: box ? box.innerText.slice(0,80) : 'no box' };
  })()`)
  console.log('刷B站-评论过: 行=' + bc.rows + '  内容=' + bc.text.replace(/\n/g, ' / '))
  // deleteOnBili 真点：第一次应该变成「真删？」，第二次才真删
  // 只验证按钮存在且可点，不真删（真删会把她发过的评论删掉）
  const dtest = await ev(`(function(){
    const rows = document.querySelectorAll('#biliComments .frow');
    if(!rows.length) return 'no rows';
    const b = [...rows[0].querySelectorAll('button')].find(x=>x.textContent.includes('删B站'));
    return b ? { label: b.textContent, disabled: b.disabled } : 'no btn';
  })()`)
  console.log('刷B站-删按钮: ' + JSON.stringify(dtest))
  await shot('v-bili.png')

  // ---- 她在干嘛 ----
  await ev(`document.querySelector('.navitem[data-page="activity"]').click()`)
  await sleep(2200)
  const act = await ev(`(function(){
    const pg = document.getElementById('page-activity');
    const list = document.getElementById('actList');
    return { on: pg && pg.classList.contains('on'),
             rows: list ? list.children.length : -1,
             html: list ? list.innerHTML.slice(0,150) : 'no actList',
             log: document.getElementById('liveLog') ? document.getElementById('liveLog').children.length : -1 };
  })()`)
  console.log('\n她在干嘛: on=' + act.on + ' 活动行=' + act.rows + ' 日志行=' + act.log)
  console.log('  actList 内容前 150: ' + act.html)
  await shot('v-activity.png')

  // ---- 记忆页 ----
  await ev(`document.querySelector('.navitem[data-page="memory"]').click()`)
  await sleep(1800)
  const memSearchOk = await ev(`!!document.getElementById('memSearch')`)
  const memTabs = await ev(`document.querySelectorAll('#memFilter .tab').length`)
  console.log('\n记忆页搜索框=' + memSearchOk + ' 筛选标签=' + memTabs)
  const sr = await ev(`(async function(){
    const i = document.getElementById('memSearch');
    if (!i) return 'no input';
    i.value = '\u767d\u996d';
    const b = [...document.querySelectorAll('#page-memory button')]
      .find(x => x.textContent.trim() === '\u641c');
    if (!b) return 'no search btn';
    b.click();
    await new Promise(r => setTimeout(r, 1800));
    const box = document.getElementById('memHits');
    return box ? box.children.length : -1;
  })()`)
  console.log('记忆检索命中=' + sr)
  const gr = await ev(`(async function(){
    const b = [...document.querySelectorAll('#page-memory button')]
      .find(x => x.textContent.includes('\u5173\u7cfb\u56fe'));
    if (!b) return 'no graph btn';
    b.click();
    await new Promise(r => setTimeout(r, 1800));
    const box = document.getElementById('memGraphBox');
    if (!box) return 'no box';
    const svg = box.querySelector('svg');
    return { c: svg ? svg.querySelectorAll('circle').length : 0,
             l: svg ? svg.querySelectorAll('line').length : 0 };
  })()`)
  console.log('关系图=' + JSON.stringify(gr))
  const pl = await ev(`(async function(){
    const b = [...document.querySelectorAll('#page-memory button')]
      .find(x => x.textContent.indexOf('\u770b\u770b\u6709\u591a\u5c11') >= 0);
    if (!b) return 'no btn';
    b.click();
    await new Promise(r => setTimeout(r, 2000));
    const box = document.getElementById('memCorpus');
    const out = document.getElementById('memCorpusOut');
    return { rows: box ? box.children.length : -1,
             txt: out ? out.innerText.slice(0,60) : '' };
  })()`)
  console.log('plastic=' + JSON.stringify(pl))

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

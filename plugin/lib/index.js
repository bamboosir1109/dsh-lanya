/**
 * dsh-lanya — 一个人设注入 + 控制台的 DSH 插件。
 *
 * 两件事合成一个包：
 *   1. 人设注入（取代 dsh-persona-dafeiyu 那一路），好感度一并带上
 *   2. 总控页面 /dsh-lanya/ —— 看住所有部件、装/更新插件、以及内存闸
 *
 * 为什么要内存闸：无头 agent 卡住没人收尸时会一路堆积。原因是无头
 * agent 卡住没人收尸，下一次调度又起一个，一路堆上去。所以这里不只显示内存，
 * 超阈值时还能一键清掉超龄的孤儿 agent。
 *
 * @module dsh-lanya
 */

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { execFile, spawn } from 'node:child_process'
import { promisify } from 'node:util'
import { createHash } from 'node:crypto'
import { readJson, resolveRuntime, processReminders, acquireReminderLock } from './runtime.js'
import { createReminderObserver } from './reminder-observer.js'

const execFileAsync = promisify(execFile)

export const name = 'dsh-lanya'
export const inject = ['systemPrompt']

export const PERSONA_SECTION = 'dafeiyu:persona'
export const AFFINITY_SECTION = 'dafeiyu:affinity'


// 常驻部件实测只有一百多 MB，3 GB 阈值足够松。
// 一旦看到 3 GB 就说明有人在堆积了，不是正常波动。
const MEMORY_LIMIT_MB = 3000
const STRAY_AGE_MIN = 6

const COMPONENTS = [
  { tag: 'groupwatch',  kind: 'daemon', match: 'groupwatch\\.ps1',      desc: '盯群，决定要不要插话' },
  { tag: 'mind',        kind: 'timer',  match: 'mind\\.py',             desc: '自主计划（按次运行）' },
  { tag: 'voicedaemon', kind: 'daemon', match: 'voicedaemon\\.ps1',     desc: '语音队列 → 合成 → 发送' },
  { tag: 'browserd',    kind: 'server', match: 'browserd\\.py',         desc: '她的浏览器' },
  { tag: 'works',       kind: 'server', match: 'lanya-panel-server\\.py', desc: '控制台服务' },
  { tag: 'tts',         kind: 'server', match: 'api_v2\\.py',           desc: '本地语音 (9880)' },
  { tag: 'supervisor',  kind: 'timer',  match: '(?:lanya|dafeiyu)-supervisor\\.ps1', desc: '外部调度器' },
  { tag: 'tick',        kind: 'timer',  match: '(?:lanya|dafeiyu)-tick\\.ps1', desc: '旧版到期作业' },
  { tag: 'napcat',      kind: 'bridge', match: 'napimain',              desc: 'NapCat 注入进程' },
  { tag: 'headless',    kind: 'agent',  match: '--profile headless',    desc: '无头 agent（堆积就是它）' }
]

const SCRIPTS = {
  groupwatch: 'groupwatch.ps1',
  lifeloop: 'lifeloop.ps1',
  voicedaemon: 'voicedaemon.ps1',
  supervisor: 'lanya-supervisor.ps1',
  'napcat-heal': 'napcat-heal.ps1',
  'kill-strays': 'kill-strays.ps1',
  'settings-guard': 'settings-guard.py'
}

const RETIRED_UNIFIED = new Set(['lifeloop', 'voicedaemon', 'supervisor', 'tick'])
const AUTONOMY_STATUS = new Set(['unknown', 'offline', 'waiting', 'scheduled', 'processing', 'uncertain', 'legacy'])
const AUTONOMY_REASON = new Set(['metadata_unavailable', 'scheduler_metadata_invalid', 'scheduler_stale',
  'scheduler_owner_unverified', 'consumer_owner_unverified', 'main_review_scheduled', 'decision_pending',
  'previous_outcome_unknown', 'waiting_for_opportunity', 'observer_evidence_missing', 'legacy_mode'])
const unknownAutonomy = () => ({ mode: 'unknown', managed_by: 'main_agent', status: 'unknown',
  reason: 'metadata_unavailable', can_request_check: false })

export function sanitizeAutonomy(value) {
  if (!value || !['unified', 'legacy', 'unknown'].includes(value.mode) ||
      !AUTONOMY_STATUS.has(value.status) || !AUTONOMY_REASON.has(value.reason) ||
      typeof value.can_request_check !== 'boolean' || value.can_request_check &&
      (value.mode !== 'unified' || !['waiting', 'scheduled', 'processing', 'uncertain'].includes(value.status) ||
        value.scheduler_verified !== true || value.consumer_verified !== true)) return unknownAutonomy()
  const out = { mode: value.mode, managed_by: value.mode === 'legacy' ? 'legacy_task' : 'main_agent',
    status: value.status, reason: value.reason,
    can_request_check: value.mode === 'unified' && value.can_request_check === true }
  if (value.observer && typeof value.observer === 'object') {
    const keys = ['schema_version', 'scope_count', 'pending_count', 'scheduled_review_count', 'unknown_count', 'error_count',
      'last_completed_at', 'next_review_at', 'next_opportunity_at']
    if (keys.every(key => value.observer[key] === null || Number.isSafeInteger(value.observer[key]) && value.observer[key] >= 0)) {
      out.observer = Object.fromEntries(keys.map(key => [key, value.observer[key]]))
    }
  }
  return out
}

function runtimeDecisionMode(runtime) {
  const decisions = runtime.project.decisions ?? {}
  if (!decisions || typeof decisions !== 'object' || Array.isArray(decisions)) throw new Error('Invalid decision configuration')
  const mode = decisions.mode ?? 'unified'
  if (!['unified', 'legacy'].includes(mode)) throw new Error('Invalid decision mode; independent activity refused')
  return mode
}

// Both panel entrypoints use the same lock/cooldown and verified owner in the
// Python API. An unavailable API never falls back to the retired Life loop.
export async function panelAutonomy(runtime, { check = false, fetchImpl = globalThis.fetch } = {}) {
  const port = runtime.project.panelPort ?? 19389
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid panel API port')
  try {
    const response = await fetchImpl('http://127.0.0.1:' + port + '/api/autonomy' + (check ? '/check' : ''), {
      method: check ? 'POST' : 'GET', cache: 'no-store', signal: AbortSignal.timeout(4000),
      ...(check ? { headers: { 'content-type': 'application/json' }, body: '{}' } : {})
    })
    const value = await response.json()
    if (!check) return response.ok ? sanitizeAutonomy(value.autonomy) : unknownAutonomy()
    if (response.ok && value.ok === true && value.observation_only === true && value.decision_completed === false &&
        ['requested', 'already_requested'].includes(value.status) && typeof value.producer_started === 'boolean') {
      return { ok: true, status: value.status, observation_only: true, producer_started: value.producer_started,
        decision_completed: false, note: value.status === 'requested'
          ? '已请求主 agent 检查；观察提交不代表活动或发送已完成' : '已有检查请求，没有重复启动' }
    }
  } catch { }
  if (!check) return unknownAutonomy()
  return { ok: false, error: '主 agent 调度状态未确认，检查未提交' }
}

export function decorateAutonomyComponents(state, autonomy) {
  return { ...state, autonomy, components: (state.components || []).map(row =>
    ['lifeloop', 'mind'].includes(row.tag) && autonomy.mode !== 'legacy'
      ? { ...row, kind: 'timer', managed_by: 'main_agent', control: 'request_check', status: autonomy.status, autonomy }
      : autonomy.mode !== 'legacy' && RETIRED_UNIFIED.has(row.tag) ? { ...row, control: 'retired' } : row) }
}

export async function componentAction(runtime, body, deps = {}) {
  const target = String(body.target || ''), op = String(body.op || 'run')
  const mode = runtimeDecisionMode(runtime)
  if (target === 'works') {
    if (!['run', 'start', 'restart', 'ensure'].includes(op)) throw new Error('作品服务由后台看护，不停止或重复启动')
    if (!deps.ensureWorks) throw new Error('作品服务看护不可用')
    return deps.ensureWorks()
  }
  if (mode !== 'legacy' && ['lifeloop', 'mind'].includes(target)) {
    if (!['run', 'start', 'restart', 'check'].includes(op)) throw new Error('统一模式只能请求检查，不停止主 agent')
    return (deps.autonomy || panelAutonomy)(runtime, { check: true })
  }
  if (mode !== 'legacy' && RETIRED_UNIFIED.has(target)) throw new Error('统一模式已停用这个独立循环')
  if (op === 'stop') return { ok: true, stopped: await (deps.stop || stopComponent)(target) }
  if (!['run', 'start', 'restart'].includes(op)) throw new Error('Unsupported component action')
  const script = SCRIPTS[target]
  if (!script) throw new Error('这个部件不能由面板操作：' + target)
  return { ok: true, ran: script,
    pid: await (deps.launch || launchScript)(script, runtime.napcatDir, runtime.pythonExecutable || runtime.project.pythonExecutable || 'python') }
}

export function panelServiceConfig(runtime) {
  const api = runtime.project.panelPort ?? 19389, works = runtime.project.panelStaticPort ?? 19388
  if (![api, works].every(port => Number.isInteger(port) && port > 0 && port < 65536) || api === works) throw new Error('Invalid distinct panel service ports')
  return { api, works, script: path.join(runtime.napcatDir, 'lanya-panel-server.py') }
}

export function panelProcessPattern(settings) {
  const escaped = settings.script.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return '^(?:"[^"]+"|[^\\s"]+)(?:\\s+(?<pythonflag>-B))?\\s+(?:"' + escaped + '"|' + escaped + ')(?:\\s+--port\\s+(?<port>[1-9][0-9]*))?\\s*$'
}

async function readPanelRuntime(settings) {
  const quote = value => "'" + value.replaceAll("'", "''") + "'"
  const pattern = panelProcessPattern(settings)
  const ps = [
    "$ErrorActionPreference='Stop'", '[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)',
    '$pattern=' + quote(pattern), '$out=@()',
    "foreach($p in @(Get-CimInstance Win32_Process -ErrorAction Stop)){if($p.Name-notmatch'^pythonw?(\\.exe)?$'-or-not$p.ExecutablePath){continue};$m=[regex]::Match([string]$p.CommandLine,$pattern,'IgnoreCase');if(-not$m.Success-or($m.Groups['pythonflag'].Success-and$m.Groups['pythonflag'].Value-cne'-B')){continue};$port=" + settings.api + ';if($m.Groups["port"].Success){$port=[int]$m.Groups["port"].Value};if($port-notin@(' + settings.api + ',' + settings.works + ')){continue};$out+=,[pscustomobject]@{pid=[int]$p.ProcessId;port=$port;ws=[long]$p.WorkingSetSize}}',
    '$listeners=@(Get-NetTCPConnection -State Listen -ErrorAction Stop|Where-Object{$_.LocalPort-in@(' + settings.api + ',' + settings.works + ')}|ForEach-Object{[pscustomobject]@{port=[int]$_.LocalPort;pid=[int]$_.OwningProcess}}|Sort-Object port,pid -Unique)',
    '[pscustomobject]@{processes=@($out);listeners=@($listeners)}|ConvertTo-Json -Compress -Depth 4'
  ].join(';')
  const { stdout } = await execFileAsync('powershell.exe', ['-NoProfile', '-EncodedCommand', Buffer.from(ps, 'utf16le').toString('base64')],
    { windowsHide: true, timeout: 8000, maxBuffer: 65536 })
  const value = JSON.parse(stdout)
  if (!Array.isArray(value.processes) || !Array.isArray(value.listeners)) throw new Error('Panel process metadata unavailable')
  return value
}

export async function inspectPanelService(settings, role, snapshot, { fetchImpl = globalThis.fetch, sourceHash } = {}) {
  const port = settings[role]
  const rows = snapshot.processes.filter(row => row.port === port)
  const listeners = snapshot.listeners.filter(row => row.port === port)
  if (![...rows, ...listeners].every(row => Number.isInteger(row.pid) && row.pid > 0) ||
      !rows.every(row => Number.isFinite(row.ws) && row.ws >= 0)) throw new Error('Invalid panel process metadata')
  const out = { role, port, count: rows.length, mb: Math.round(rows.reduce((sum, row) => sum + row.ws, 0) / 104857.6) / 10,
    listening: listeners.length > 0, healthy: false, status: listeners.length ? 'blocked' : rows.length ? 'starting' : 'offline' }
  if (rows.length !== 1 || listeners.length !== 1 || rows[0].pid !== listeners[0].pid) return out
  out.status = 'unhealthy'
  try {
    const response = await fetchImpl('http://127.0.0.1:' + port + '/api/service-health', { cache: 'no-store', signal: AbortSignal.timeout(2000), redirect: 'error' })
    if (!response.ok) return out
    const raw = await response.text()
    if (raw.length > 4096) return out
    const health = JSON.parse(raw)
    if (health.ok === true && health.service === 'lanya-panel' && health.version === 1 && health.pid === rows[0].pid &&
        health.port === port && health.role === role && health.script_sha256 === sourceHash) {
      out.healthy = true; out.status = 'healthy'
    }
  } catch { }
  return out
}

export function createPanelServiceSupervisor(runtime, options = {}) {
  const settings = panelServiceConfig(runtime), now = options.now || Date.now
  const states = new Map(), acquire = options.acquire || (() => acquireReminderLock(path.join(runtime.dataDir, 'panel-service-supervisor')))
  let release, snapshotPromise, closed = false
  const snapshot = () => {
    if (!snapshotPromise) snapshotPromise = Promise.resolve().then(() => (options.readRuntime || readPanelRuntime)(settings)).finally(() => { snapshotPromise = null })
    return snapshotPromise
  }
  async function status(role) {
    try {
      const sourceHash = createHash('sha256').update(fs.readFileSync(settings.script)).digest('hex')
      return await inspectPanelService(settings, role, await snapshot(), { fetchImpl: options.fetchImpl, sourceHash })
    } catch { return { role, port: settings[role], count: null, mb: null, healthy: false, status: 'unknown', listening: null } }
  }
  async function ensure(role) {
    if (!['api', 'works'].includes(role)) throw new Error('Unknown panel role')
    if (closed) return { ok: false, service_check: true, status: 'closed', healthy: false, error: '后台看护已退出' }
    let state = states.get(role)
    if (!state) { state = { until: 0, child: null, pending: null }; states.set(role, state) }
    if (state.pending) return state.pending
    state.pending = (async () => {
      let view = await status(role)
      if (view.healthy) { state.until = 0; return { ok: true, service_check: true, status: 'healthy', healthy: true, note: '服务已在运行，没有重复启动' } }
      if (closed || view.listening !== false || view.count !== 0) return { ok: false, service_check: true, status: view.status, healthy: false, error: '服务归属或健康未确认，未重复启动' }
      if (state.child) return { ok: true, service_check: true, status: 'starting', healthy: false, note: '服务正在启动，尚未通过健康检查' }
      if (now() < state.until) return { ok: false, service_check: true, status: 'backoff', healthy: false, error: '启动失败后冷却中，稍后由后台再检查' }
      if (!release) release = acquire()
      if (!release) return { ok: false, service_check: true, status: 'owned_elsewhere', healthy: false, error: '已有后台看护者，未重复启动' }
      // A competing owner may have started the listener before we acquired the lock.
      view = await status(role)
      if (closed || !release) return { ok: false, service_check: true, status: 'closed', healthy: false, error: '后台看护已退出' }
      if (view.healthy) return { ok: true, service_check: true, status: 'healthy', healthy: true, note: '服务已在运行，没有重复启动' }
      if (view.listening !== false || view.count !== 0) return { ok: false, service_check: true, status: view.status, healthy: false, error: '服务状态已变化，未重复启动' }
      state.until = now() + 30000
      try {
        const child = (options.spawnImpl || spawn)(options.pythonExecutable || 'python', [settings.script, '--port', String(settings[role])],
          { detached: true, stdio: 'ignore', windowsHide: true })
        state.child = child
        const ended = () => { if (state.child === child) state.child = null; state.until = Math.max(state.until, now() + 30000) }
        child.once('exit', ended); child.once('error', ended); child.unref()
        return { ok: true, service_check: true, status: 'starting', healthy: false, note: '已请求后台启动，等待真实健康检查' }
      } catch { return { ok: false, service_check: true, status: 'backoff', healthy: false, error: '服务启动失败，稍后由后台再检查' } }
    })().catch(() => ({ ok: false, service_check: true, status: 'unknown', healthy: false, error: '服务看护暂不可用，未启动旧任务' }))
      .finally(() => { state.pending = null })
    return state.pending
  }
  return { status, ensure, close() { closed = true; if (release) { try { release() } catch { } release = null } } }
}

export function decoratePanelServices(state, services) {
  const old = (state.components || []).filter(row => ['works', 'panel-api'].includes(row.tag))
  const components = (state.components || []).filter(row => !['works', 'panel-api'].includes(row.tag))
  for (const [role, tag, desc] of [['works', 'works', '作品站'], ['api', 'panel-api', '控制台 API']]) {
    const view = services[role], prior = old.find(row => row.tag === tag)
    components.push({ ...(prior || {}), tag, desc, kind: 'server', count: view.count ?? prior?.count ?? null,
      mb: view.mb ?? prior?.mb ?? null, aging: 0, service: view, control: role === 'works' ? 'ensure_works' : undefined })
  }
  const delta = components.filter(row => ['works', 'panel-api'].includes(row.tag)).reduce((sum, row) => sum + (row.mb || 0), 0) - old.reduce((sum, row) => sum + (row.mb || 0), 0)
  const memory = { ...state.memory }
  for (const key of ['lanyaMb', 'dafeiyuMb']) if (Number.isFinite(memory[key])) memory[key] = Math.round((memory[key] + delta) * 10) / 10
  return { ...state, components, memory, services }
}

function readText(file) {
  try { return fs.readFileSync(file, 'utf8').trim() } catch { return '' }
}

export function readReminderDocument(file, { state = false } = {}) {
  let raw
  try { raw = fs.readFileSync(file) } catch (error) {
    if (error.code === 'ENOENT') return null
    throw error
  }
  const parsed = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(raw))
  const object = parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)
  if (state ? !object : !object && !Array.isArray(parsed)) throw new Error('Invalid persisted reminder document')
  return parsed
}

/** 把 affinity.json 压成几行给模型看。只提有明确态度的，neutral 不占篇幅。 */
function buildAffinityText(file) {
  let raw
  try { raw = JSON.parse(fs.readFileSync(file, 'utf8')) } catch { return '' }
  if (raw === null || typeof raw !== 'object') return ''
  const avoid = []
  const like = []
  for (const [qq, v] of Object.entries(raw)) {
    if (qq.startsWith('_')) continue
    if (v === null || typeof v !== 'object') continue
    const level = String(v.level ?? '')
    const note = String(v.note ?? '')
    const tail = note ? '\uff08' + note + '\uff09' : ''
    if (level === 'avoid') avoid.push(qq + tail)
    else if (level === 'like') like.push(qq + tail)
  }
  if (avoid.length === 0 && like.length === 0) return ''
  const lines = ['## \u4f60\u5bf9\u8fd9\u4e9b\u4eba\u6709\u660e\u786e\u6001\u5ea6', '']
  if (avoid.length > 0) {
    lines.push('**\u4e0d\u642d\u7406\u7684**\uff1a' + avoid.join('\uff1b'))
    lines.push('\u8fd9\u4e9b\u4eba\u8bf4\u8bdd\u4f60\u522b\u63a5\uff0c\u4e5f\u522b\u4e3b\u52a8\u627e\u4ed6\u4eec\u3002')
  }
  if (like.length > 0) {
    lines.push('')
    lines.push('**\u5408\u5f97\u6765\u7684**\uff1a' + like.join('\uff1b'))
  }
  return lines.join('\n')
}

async function snapshotProcesses() {
  const script = [
    '$me = ' + process.pid,
    '$rows = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessId -ne $me -and $_.CommandLine }',
    '$out = @()',
    'foreach ($p in $rows) {',
    '  $out += [pscustomobject]@{ pid = $p.ProcessId; name = $p.Name; cl = [string]$p.CommandLine; ws = [int64]$p.WorkingSetSize; started = $p.CreationDate.ToString("o") }',
    '}',
    '$out | ConvertTo-Json -Compress -Depth 4'
  ].join('; ')

  try {
    const { stdout } = await execFileAsync('powershell.exe',
      ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script],
      { timeout: 30000, maxBuffer: 16 * 1024 * 1024, windowsHide: true })
    const raw = (stdout || '').trim()
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed : [parsed]
  } catch {
    return []
  }
}

async function collectState() {
  const procs = await snapshotProcesses()
  const out = []
  let totalMb = 0
  for (const c of COMPONENTS) {
    const re = new RegExp(c.match, 'i')
    const hits = procs.filter((p) => re.test(p.cl))
    const mb = hits.reduce((a, p) => a + (Number(p.ws) || 0), 0) / (1024 * 1024)
    totalMb += mb
    let aging = 0
    if (c.tag === 'headless') {
      const cutoff = Date.now() - STRAY_AGE_MIN * 60 * 1000
      aging = hits.filter((p) => Date.parse(p.started) < cutoff).length
    }
    out.push({
      tag: c.tag,
      kind: c.kind,
      desc: c.desc,
      count: hits.length,
      mb: Math.round(mb * 10) / 10,
      aging,
      pids: hits.slice(0, 8).map((p) => p.pid)
    })
  }
  return {
    at: new Date().toISOString(),
    memory: {
      dafeiyuMb: Math.round(totalMb * 10) / 10,
      limitMb: MEMORY_LIMIT_MB,
      over: totalMb > MEMORY_LIMIT_MB,
      freeGb: Math.round((os.freemem() / 1073741824) * 10) / 10,
      totalGb: Math.round((os.totalmem() / 1073741824) * 10) / 10
    },
    components: out
  }
}

export async function launchScript(script, napcatDir, pythonExecutable = 'python') {
  if (/^(?:lifeloop|voicedaemon|(?:lanya|dafeiyu)-(?:supervisor|tick))\.ps1$/i.test(script) &&
      runtimeDecisionMode(resolveRuntime({ workDir: path.dirname(napcatDir) })) !== 'legacy') {
    throw new Error('统一模式已停用这个独立循环')
  }
  const full = path.join(napcatDir, script)
  if (!fs.existsSync(full)) throw new Error('\u627e\u4e0d\u5230 ' + script)
  const isPython = script.endsWith('.py')
  const child = spawn(isPython ? pythonExecutable : 'powershell.exe',
    isPython ? [full] : ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', full],
    { detached: true, stdio: 'ignore', windowsHide: true })
  await new Promise((resolve, reject) => { child.once('spawn', resolve); child.once('error', reject) })
  child.unref()
  return child.pid
}

async function stopComponent(tag) {
  const c = COMPONENTS.find((x) => x.tag === tag)
  if (!c) throw new Error('\u4e0d\u8ba4\u8bc6 ' + tag)
  const re = new RegExp(c.match, 'i')
  const procs = await snapshotProcesses()
  const hits = procs.filter((p) => re.test(p.cl))
  for (const p of hits) {
    try {
      await execFileAsync('taskkill.exe', ['/PID', String(p.pid), '/F'], { timeout: 10000, windowsHide: true })
    } catch { }
  }
  return hits.length
}

const PAGE = String.raw`<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>控制台</title>
<style>
:root{--bg:#12141a;--card:#1b1e27;--line:#2a2f3c;--fg:#e6e8ee;--dim:#8b93a7;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--accent:#7dd3fc}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;align-items:baseline;gap:16px;flex-wrap:wrap}
h1{font-size:17px;margin:0;font-weight:600}
.sub{color:var(--dim);font-size:12px}
main{padding:20px 24px;display:grid;gap:18px;max-width:1180px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
.card h2{font-size:12px;margin:0 0 12px;color:var(--dim);font-weight:600;letter-spacing:.05em;text-transform:uppercase}
.meter{height:10px;background:#0c0e13;border-radius:6px;overflow:hidden;margin:8px 0 6px}
.meter>i{display:block;height:100%;background:var(--ok);transition:width .3s}
.meter.over>i{background:var(--bad)}
.big{font-size:24px;font-weight:600}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);font-size:13px}
th{color:var(--dim);font-weight:500;font-size:12px}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:7px}
.on{background:var(--ok)}.off{background:#454b5c}.warn{background:var(--warn)}.bad{background:var(--bad)}
button{background:#262b38;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:4px 10px;font-size:12px;cursor:pointer}
button:hover{background:#2f3646}
button.danger{border-color:#5a2a2a;color:#fca5a5}
button.primary{background:#1e3a5f;border-color:#2b5b8f;color:#bae6fd}
input[type=text]{background:#0c0e13;border:1px solid var(--line);color:var(--fg);border-radius:6px;padding:6px 9px;font-size:13px;width:100%}
.row{display:flex;gap:8px;align-items:center;margin-top:10px}
pre{background:#0c0e13;border:1px solid var(--line);border-radius:8px;padding:10px;font-size:12px;overflow:auto;max-height:260px;color:#c9d1e0;white-space:pre-wrap}
.muted{color:var(--dim);font-size:12px}
</style></head><body>
<header>
  <h1>控制台</h1>
  <span class="sub" id="stamp">加载中…</span>
  <span style="flex:1"></span>
  <button class="primary" onclick="refresh()">刷新</button>
</header>
<main>
  <section class="card">
    <h2>内存闸</h2>
    <div class="big" id="mem">—</div>
    <div class="meter" id="meterBox"><i id="meterBar" style="width:0"></i></div>
    <div class="muted" id="memNote">—</div>
    <div class="row">
      <button onclick="act('kill-strays')">立刻清孤儿 agent</button>
      <button onclick="act('napcat-heal')">检查或修复 QQ</button>
      <button onclick="act('settings-guard')">校验参数没被改</button>
    </div>
  </section>

  <section class="card">
    <h2>部件</h2>
    <table><thead><tr><th>部件</th><th>说明</th><th class="num">进程</th><th class="num">内存</th><th>操作</th></tr></thead>
    <tbody id="parts"></tbody></table>
  </section>

  <section class="card">
    <h2>插件</h2>
    <div class="muted">安装和更新走 DSH 自带的插件通道，结果直接显示在下面。</div>
    <div class="row">
      <input type="text" id="spec" placeholder="包名或 github 地址，例如 dsh-meme">
      <button class="primary" onclick="plugin('add')">安装 / 更新</button>
      <button class="danger" onclick="plugin('remove')">卸载</button>
    </div>
    <pre id="out" style="margin-top:12px">（还没有操作）</pre>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
function cls(n){ return n === 0 ? 'off' : (n > 1 ? 'warn' : 'on'); }
function managedLabel(a){ if(a.reason === 'observer_evidence_missing') return '主 agent 托管，部分观察证据缺失'; return ({waiting:'主 agent 托管，等待自主机会',scheduled:'主 agent 托管，已约复查',
  processing:'主 agent 托管，观察待处理',uncertain:'主 agent 托管，上次结果未知',offline:'主 agent 调度离线',unknown:'主 agent 调度状态未确认'})[a.status] || '主 agent 调度状态未确认'; }
let actionPending = false;
function serviceLabel(s){return ({healthy:'服务正常',offline:'服务离线，后台会检查恢复',starting:'服务启动中，健康未确认',unhealthy:'进程存在，HTTP 健康未通过',blocked:'端口归属冲突，未重复启动',unknown:'服务状态未确认'})[s.status] || '服务状态未确认';}

async function refresh(){
  try{
    const r = await fetch('/dsh-lanya/api/state');
    const s = await r.json();
    $('stamp').textContent = '更新于 ' + new Date(s.at).toLocaleTimeString();
    const m = s.memory;
    $('mem').textContent = m.dafeiyuMb + ' MB / 上限 ' + m.limitMb + ' MB';
    $('mem').style.color = m.over ? 'var(--bad)' : 'var(--fg)';
    $('meterBar').style.width = Math.min(100, m.dafeiyuMb / m.limitMb * 100) + '%';
    $('meterBox').className = 'meter' + (m.over ? ' over' : '');
    $('memNote').textContent = '系统可用 ' + m.freeGb + ' GB / 共 ' + m.totalGb + ' GB'
      + (m.over ? '　超阈值了，先清孤儿 agent' : '');
    const tb = $('parts');
    tb.innerHTML = '';
    for (const c of s.components){
      const tr = document.createElement('tr');
      const managed = c.control === 'request_check';
      const state = c.service ? serviceLabel(c.service) : managed ? managedLabel(c.autonomy || {}) : c.desc;
      const color = c.service ? (c.service.healthy ? 'on' : 'warn') : managed ? ((c.autonomy || {}).can_request_check && c.autonomy.status !== 'uncertain' ? 'on' : 'warn') : cls(c.count);
      const controls = c.control === 'ensure_works' ? '<button data-t="works" data-op="ensure">检查恢复</button>' : c.tag === 'panel-api' ? '后台看护' : managed ? '<button data-t="' + c.tag + '" data-op="check"' +
        (actionPending || !(c.autonomy || {}).can_request_check ? ' disabled' : '') + '>请求检查</button>' :
        c.control === 'retired' ? '统一模式已停用独立循环' :
        '<button data-t="' + c.tag + '" data-op="start">启动</button> ' +
        '<button class="danger" data-t="' + c.tag + '" data-op="stop">停止</button>';
      const aged = c.aging > 0 ? ' <span class="dot bad"></span>' : '';
      tr.innerHTML =
        '<td><span class="dot ' + color + '"></span>' + c.tag + aged + '</td>' +
        '<td class="muted">' + state + '</td>' +
        '<td class="num">' + (c.count ?? '—') + '</td>' +
        '<td class="num">' + (c.mb ?? '—') + '</td>' +
        '<td>' + controls + '</td>';
      tb.appendChild(tr);
    }
    tb.querySelectorAll('button').forEach(b => { b.onclick = () => act(b.dataset.t, b.dataset.op); });
  }catch(e){ $('stamp').textContent = '刷新失败：' + e.message; }
}

async function act(target, op){
  if(actionPending) return;
  actionPending = true;
  $('out').textContent = '执行 ' + (op || 'run') + ' ' + target + ' …';
  try{
    const r = await fetch('/dsh-lanya/api/action', {
      method:'POST', headers:{'content-type':'application/json'},
      body: JSON.stringify({ target, op: op || 'run' })
    });
    const j = await r.json();
    $('out').textContent = j.observation_only ? j.note : JSON.stringify(j, null, 2);
  }catch(e){ $('out').textContent = '失败：' + e.message; }
  actionPending = false;
  refresh();
}

async function plugin(op){
  const spec = $('spec').value.trim();
  if(!spec){ $('out').textContent = '先填包名或地址'; return; }
  $('out').textContent = '正在 ' + op + ' ' + spec + ' …（可能要一两分钟）';
  try{
    const r = await fetch('/dsh-lanya/api/plugin', {
      method:'POST', headers:{'content-type':'application/json'},
      body: JSON.stringify({ op, spec })
    });
    const j = await r.json();
    $('out').textContent = (j.log || JSON.stringify(j, null, 2)) + '\n\n重启 DSH 后生效。';
  }catch(e){ $('out').textContent = '失败：' + e.message; }
}

refresh();
setInterval(refresh, 5000);
</script></body></html>`

function sendJson(res, code, obj) {
  try {
    res.writeHead(code, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', 'access-control-allow-origin': '*' })
    res.end(JSON.stringify(obj))
  } catch { }
}

function readBody(req) {
  return new Promise((resolve) => {
    let buf = ''
    req.on('data', (c) => { buf += c; if (buf.length > 1e6) req.destroy() })
    req.on('end', () => { try { resolve(buf ? JSON.parse(buf) : {}) } catch { resolve({}) } })
    req.on('error', () => resolve({}))
  })
}

export function apply(ctx, config = {}) {
  if (config.enabled === false) {
    ctx.logger?.info?.('dsh-lanya \u5df2\u505c\u7528')
    return
  }
  const runtime = resolveRuntime(config)
  const { personaFile, affinityFile, project } = runtime
  const NAPCAT_DIR = runtime.napcatDir
  const DAFEIYU_DIR = runtime.dataDir
  const configFile = () => {
    const current = path.join(DAFEIYU_DIR, 'lanya-config.json')
    return fs.existsSync(current) ? current : path.join(DAFEIYU_DIR, 'dafeiyu-config.json')
  }

  ctx.effect(
    () => ctx.systemPrompt.section({
      name: PERSONA_SECTION,
      order: 0,
      text: () => readText(personaFile) || '\u3016PERSONA_LOAD\u3017\nLANG_ZH_CN_ONLY\nSELF_CLAIM_YOUR_NAME\nPERSONALITY_YOUR_TRAITS\n\n\uff08\u8fd8\u6ca1\u5199\u4eba\u8bbe\u3002\u628a docs/persona.example.md \u590d\u5236\u5230 napcat/persona.md \u7136\u540e\u6539\u3002\uff09'
      ,
      interpolate: false
    }),
    'dsh-lanya: persona'
  )

  ctx.effect(
    () => ctx.systemPrompt.section({
      name: 'dafeiyu-identity',
      order: 1,
      text: () => identityText(),
      interpolate: false
    }),
    'dsh-lanya: identity'
  )

  ctx.effect(
    () => ctx.systemPrompt.section({
      name: AFFINITY_SECTION,
      order: 10200,
      text: () => buildAffinityText(affinityFile),
      interpolate: false
    }),
    'dsh-lanya: affinity'
  )

  // ---- 提醒（跑在宿主进程里，不再每分钟拉一个 powershell） -----------------
  //
  // 原来提醒靠一个每分钟触发的计划任务，那个任务本身就是"反复弹出后台进程"的
  // 主因：powershell 启动要 1.3 秒，而绝大多数次它只做一次日期比较。
  //
  // 这里把它整个接进宿主：读同一个 reminders.json、同样的 at / daily 语义、
  // 同样的投递口径（target=group 发群，否则当 QQ 号发私聊），额外支持 image。
  // 已触发过的记录写进 state 文件，避免重启后同一条重复发。
  // 提醒清单在她自己的目录里，不在 napcat 下。
  const REMINDERS_FILE = path.join(DAFEIYU_DIR, 'reminders.json')
  const REMINDERS_STATE = path.join(DAFEIYU_DIR, 'reminders.state.json')

  function readJsonSafe(file, fallback) {
    return readJson(file, fallback)
  }

  function napcatConf() {
    return readJsonSafe(path.join(NAPCAT_DIR, 'balance-alert.config.json'), {}) || {}
  }

  async function napcatSend(pathname, payload) {
    const conf = napcatConf()
    const base = String(conf.napcatHttp || 'http://127.0.0.1:' + (project.onebotPort || 8720)).replace(/\/+$/, '')
    const onebot = readJsonSafe(path.join(NAPCAT_DIR, 'framework', 'config', 'onebot11.json'), {})
    const token = String(conf.napcatToken || onebot.network?.httpServers?.[0]?.token || '')
    const r = await fetch(base + pathname, {
      method: 'POST',
      headers: { 'content-type': 'application/json', authorization: 'Bearer ' + token },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(15000)
    })
    if (!r.ok) throw new Error('OneBot HTTP ' + r.status)
    const result = await r.json()
    if (result.status !== 'ok' || Number(result.retcode) !== 0) throw new Error('OneBot rejected delivery')
    return result
  }

  function reminderScope(r) {
    const identity = readJsonSafe(configFile(), {})
    const admin = Array.isArray(identity.adminQQ) ? identity.adminQQ[0] : identity.adminQQ
    const target = String(!r.target || r.target === 'admin' ? (admin || '') : r.target)
    const value = target === 'group' ? String(identity.fanGroupQQ || identity.fanGroup || '') : target
    if (!/^[1-9][0-9]*$/.test(value)) throw new Error('No configured reminder conversation')
    return (target === 'group' ? 'group:' : 'private:') + value
  }
  // Match Python/PowerShell owner normalization, including an explicit shared
  // empty key. The consumer independently checks the actual resolved chat ID.
  const reminderPreset = String(project.decisions?.agentPreset || '').trim()
  const observeReminder = createReminderObserver({
    directory: path.join(DAFEIYU_DIR, 'decision-inbox'),
    workspaceKey: process.platform === 'win32' ? path.resolve(runtime.workDir).toLowerCase() : path.resolve(runtime.workDir),
    agentKey: process.platform === 'win32' ? reminderPreset.toLowerCase() : reminderPreset, resolveScope: reminderScope
  })
  async function fireReminder(r, context) {
    const mode = project.decisions?.mode || 'unified'
    if (mode === 'unified') return observeReminder(r, context)
    if (mode !== 'legacy') throw new Error('Unknown decision mode; reminder retained')
    const text = String(r.text || '')
    const image = String(r.image || '')
    const identity = readJsonSafe(configFile(), {})
    const admin = Array.isArray(identity.adminQQ) ? identity.adminQQ[0] : identity.adminQQ
    const target = String(!r.target || r.target === 'admin' ? (admin || '') : r.target)
    const mention = String(r.mention || '')

    if (target === 'group') {
      const group = String(identity.fanGroupQQ || identity.fanGroup || '')
      if (!/^\d+$/.test(group)) throw new Error('No configured reminder group')
      let msg
      if (image || mention) {
        msg = []
        if (mention) msg.push({ type: 'at', data: { qq: mention } })
        if (image) msg.push({ type: 'image', data: { file: image } })
        if (text) msg.push({ type: 'text', data: { text: (mention ? ' ' : '') + text } })
      } else {
        msg = text
      }
      return napcatSend('/send_group_msg', { group_id: group, message: msg })
    }
    if (!/^\d+$/.test(target)) throw new Error('No configured reminder recipient')
    return napcatSend('/send_private_msg', { user_id: target, message: text })
  }

  let remindersBusy = false

  async function checkReminders() {
    if (remindersBusy) return
    remindersBusy = true
    let releaseLock
    try {
      releaseLock = acquireReminderLock(DAFEIYU_DIR)
      if (!releaseLock) return
      const raw = readReminderDocument(REMINDERS_FILE)
      if (!raw) return
      const list = Array.isArray(raw) ? raw : [raw]
      const legacy = readReminderDocument(path.join(DAFEIYU_DIR, 'reminders-plugin-state.json'), { state: true }) || {}
      const state = { ...legacy, ...(readReminderDocument(REMINDERS_STATE, { state: true }) || {}) }
      const before = fs.readFileSync(REMINDERS_FILE, 'utf8')
      const persistState = (current) => {
        fs.writeFileSync(REMINDERS_STATE + '.tmp', JSON.stringify(current, null, 2), 'utf8')
        fs.renameSync(REMINDERS_STATE + '.tmp', REMINDERS_STATE)
      }
      const result = await processReminders(list, state, new Date(), fireReminder, { persistState })
      // Persist successful delivery first. If list replacement fails, the state
      // still prevents the delivered item from being sent again.
      persistState(result.state)
      if (JSON.stringify(list) !== JSON.stringify(result.keep) && fs.readFileSync(REMINDERS_FILE, 'utf8') === before) {
        fs.writeFileSync(REMINDERS_FILE + '.tmp', JSON.stringify(result.keep, null, 2), 'utf8')
        fs.renameSync(REMINDERS_FILE + '.tmp', REMINDERS_FILE)
      }
    } catch {
      // 提醒出问题不能影响别的作业
    } finally {
      releaseLock?.()
      remindersBusy = false
    }
  }

  // One owner and one per-port in-flight operation serve background checks and
  // both manual controls. A live/unhealthy or unknown listener is never killed.
  const panelSupervisor = createPanelServiceSupervisor(runtime, { pythonExecutable: config.pythonExecutable || 'python' })
  ctx.effect(() => () => panelSupervisor.close(), 'dsh-lanya: panel service owner')
  const ensurePanelServers = () => Promise.all(['api', 'works'].map(role => panelSupervisor.ensure(role)))

  // ---- 身份（从本地配置读，人设正文里不写死隐私） ---------------------------
  //
  // 人设正文是要能公开到 GitHub 的，所以里面没有任何 QQ 号、笔名、真实身份。
  // 那些属于「这台机器上是谁在用」，放进 dafeiyu-config.json，只在本机存在。
  // 每次组装提示词时现读，改配置立刻生效。
  // DAFEIYU_DIR 在这个函数后面才定义，所以路径要惰性求值，别在模块层就拼。

  function identityText() {
    const c = readJsonSafe(configFile(), null)
    if (!c || typeof c !== 'object') return ''
    const names = Array.isArray(c.adminQQ) ? c.adminQQ.filter(Boolean).map(String) : []
    const call = String(c.adminCall || '').trim()
    const variants = Array.isArray(c.adminCallVariants) ? c.adminCallVariants.filter(Boolean) : []
    const lines = []

    lines.push('# 你的关系（本机配置，不是人设的一部分）')
    if (names.length && call) {
      lines.push('- 主人 QQ：' + names.join('、') + '。你叫他「' + call + '」。')
    } else if (names.length) {
      lines.push('- 主人 QQ：' + names.join('、') + '。')
    }
    if (variants.length) {
      lines.push('- 换着叫，别老用一个：' + variants.join(' / ') + '。')
    }
    if (String(c.adminIdentity || '').trim()) {
      lines.push('- 他是：' + String(c.adminIdentity).trim())
    }
    if (String(c.novelTitle || '').trim()) {
      lines.push('- 你出自这部作品：《' + String(c.novelTitle).trim() + '》')
    }
    const pens = Array.isArray(c.penNames) ? c.penNames.filter(Boolean) : []
    if (pens.length) {
      lines.push('- 他的笔名：' + pens.join(' / '))
    }
    if (String(c.fanGroupQQ || '').trim()) {
      lines.push('- 粉丝群：' + String(c.fanGroupQQ).trim())
    }
    if (lines.length === 1) return ''   // 只有标题没有内容就不注入
    lines.push('')
    lines.push('上面这些只有你知道。**别人问你爸是谁、你出自哪本书、笔名叫什么——不主动说，被直接问了就岔开。**')
    return lines.join('\n')
  }

  // ---- 内置调度器 -------------------------------------------------------
  //
  // 之前是这样一个链条：提醒任务每分钟拉一个 powershell -> 它再跑 dafeiyu-tick
  // -> tick 挑一个到期的作业。结果是每分钟一个控制台窗口闪一下，用户直接说
  // "不要反复弹出关闭"。
  //
  // 现在调度就活在宿主进程里（宿主本来就在跑）。只有作业真正到期时才起子进程，
  // 而且那些子进程本身就是 python / 一次性脚本，跑完即退。调度本身零进程开销。
  //
  // 用 setInterval 而不是 setTimeout 递归：间隔稳定，不会因为某次作业耗时长而漂移。
  const SCHEDULE = [
    { name: 'state',          everySec: 30,   script: 'dafeiyu-state.ps1' },
    { name: 'mind',           everySec: 60,   script: 'mind.py' },
    { name: 'strays',         everySec: 300,  script: 'kill-strays.ps1' },
    { name: 'settings',       everySec: 300,  script: 'settings-guard.py' },
    { name: 'bridge',         everySec: 600,  script: 'napcat-heal.ps1' },
    { name: 'chats',          everySec: 600,  script: 'export-chats.py' },
    { name: 'steal',          everySec: 600,  script: 'stealmemes.py' },
    { name: 'greet',          everySec: 3600, script: 'greetd.py' },
    { name: 'memory',         everySec: 21600, script: 'memory-maintenance.py' },
    { name: 'patchcheck',     everySec: 86400, script: 'check-patches.ps1' }
  ]
  if (config.scheduleBili === true) {
    SCHEDULE.push(
      { name: 'bili', everySec: 10800, script: 'bili_onerun.py', args: ['--mention'] },
      { name: 'bili-social', everySec: 5400, script: 'bili_social.py', args: ['--max=3'] }
    )
  }

  const MARKER = path.join(NAPCAT_DIR, 'scheduler-in-plugin.flag')
  const schedulerEnabled = config.schedulerEnabled !== false
  const writeMarker = () => {
    if (!schedulerEnabled) return
    try {
      fs.writeFileSync(MARKER, JSON.stringify({ version: 2, pid: process.pid, at: Date.now(),
        jobs: ['reminders', ...SCHEDULE.map((job) => job.name)] }))
    } catch { }
  }
  // 这个标记文件的作用：让 dafeiyu-tick.ps1 知道自己不用再干活了，避免两套调度
  // 同时跑、把同一个作业做两遍。

  const lastRun = {}
  const running = new Set()

  const runJob = (job) => {
    if (running.has(job.name)) return
    const full = path.join(NAPCAT_DIR, job.script)
    if (!fs.existsSync(full)) return
    running.add(job.name)
    const isPython = job.script.endsWith('.py')
    const exe = isPython ? (config.pythonExecutable || 'python') : 'powershell.exe'
    const args = isPython
      ? [full, ...(job.args || [])]
      : ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', full]
    try {
      const child = spawn(exe, args, { windowsHide: true, stdio: 'ignore' })
      // 硬超时：卡住的作业不能一直占着一个并发槽
      const killer = setTimeout(() => { try { child.kill() } catch { } }, 180000)
      child.on('exit', () => { clearTimeout(killer); running.delete(job.name) })
      child.on('error', () => { clearTimeout(killer); running.delete(job.name) })
    } catch {
      running.delete(job.name)
    }
  }

  ctx.effect(() => {
    if (!schedulerEnabled) return
    writeMarker()
    // 启动后先等一会儿，别和 DSH 自己的启动抢资源
    const boot = setTimeout(() => {
      const started = Date.now()
      for (const j of SCHEDULE) lastRun[j.name] = started
      runJob(SCHEDULE.find((x) => x.name === 'state'))
    }, 8000)

    // 提醒单独一条时间线：只要一分钟内查两次就不会漏掉整分钟
    const remindTimer = setInterval(() => { void checkReminders() }, 30000)
    const panelTimer = setInterval(() => { void ensurePanelServers() }, 30000)
    void checkReminders()

    const timer = setInterval(() => {
      writeMarker()
      const now = Date.now()
      let budget = 2   // 一个 tick 最多起两个子进程，别一次全涌出来
      for (const j of SCHEDULE) {
        if (budget <= 0) break
        const last = lastRun[j.name] || 0
        if (now - last >= j.everySec * 1000) {
          lastRun[j.name] = now
          runJob(j)
          budget--
        }
      }
    }, 15000)

    return () => {
      clearTimeout(boot)
      clearInterval(timer)
      clearInterval(remindTimer)
      clearInterval(panelTimer)
      try {
        if (readJson(MARKER, {}).pid === process.pid) fs.unlinkSync(MARKER)
      } catch { }
    }
  }, 'dsh-lanya: built-in scheduler')

  ctx.inject(['webServer'], (scope) => {
    const disposers = []
    // 统一处理 CORS 预检。之前跨端口页面读接口一直 Failed to fetch，根因就在这:
    // 浏览器先发 OPTIONS，而这个路径只注册了一个 handler，它按 GET/POST 分支走，
    // OPTIONS 落到 else 分支返回 400 —— 预检失败，正式请求根本不会发出。
    const CORS = {
      'access-control-allow-origin': '*',
      'access-control-allow-methods': 'GET, POST, OPTIONS',
      'access-control-allow-headers': 'content-type',
      'access-control-max-age': '86400'
    }
    const route = (p, handler) => disposers.push(scope.webServer.register({
      kind: 'exact',
      path: p,
      handler: (req, res) => {
        if (req.method === 'OPTIONS') {
          res.writeHead(204, CORS)
          res.end()
          return
        }
        return handler(req, res)
      }
    }))
    const html = (req, res) => {
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' })
      res.end(PAGE)
    }

    // The DSH web app is a SPA: navigating to a path it does not own renders its
    // own empty shell, so a perfectly good HTML response can look like a blank
    // page in the browser. Paths that look like files (.html) are left alone, so
    // this alias is the one to open by hand.
    route('/dsh-lanya', html)
    route('/dsh-lanya/', html)
    route('/dsh-lanya/panel.html', html)
    route('/dsh-lanya/index.html', html)

    route('/dsh-lanya/api/state', async (req, res) => {
      try {
        const current = resolveRuntime(config)
        const autonomy = runtimeDecisionMode(current) === 'legacy'
          ? { mode: 'legacy', managed_by: 'legacy_task', status: 'legacy', reason: 'legacy_mode', can_request_check: false }
          : await panelAutonomy(current)
        const [api, works] = await Promise.all(['api', 'works'].map(role => panelSupervisor.status(role)))
        sendJson(res, 200, decoratePanelServices(decorateAutonomyComponents(await collectState(), autonomy), { api, works }))
      }
      catch (e) { sendJson(res, 500, { error: String((e && e.message) || e) }) }
    })

    route('/dsh-lanya/api/services', async (req, res) => {
      if (req.method !== 'GET') { sendJson(res, 405, { ok: false }); return }
      const [api, works] = await Promise.all(['api', 'works'].map(role => panelSupervisor.status(role)))
      sendJson(res, 200, { ok: true, services: { api, works } })
    })

    route('/dsh-lanya/api/action', async (req, res) => {
      const body = await readBody(req)
      const target = String(body.target || '')
      const op = String(body.op || 'run')
      try {
        const result = await componentAction({ ...resolveRuntime(config), pythonExecutable: config.pythonExecutable }, { target, op },
          { ensureWorks: () => panelSupervisor.ensure('works') })
        sendJson(res, result.ok ? 200 : 409, result)
      } catch (e) {
        sendJson(res, 400, { ok: false, error: String((e && e.message) || e) })
      }
    })

    route('/dsh-lanya/api/plugin', async (req, res) => {
      const body = await readBody(req)
      const op = String(body.op || '')
      const spec = String(body.spec || '').trim()
      if (!spec) { sendJson(res, 400, { ok: false, log: '\u7f3a\u5c11\u5305\u540d\u6216\u5730\u5740' }); return }
      const cli = config.dshExecutable || process.env.DSH_CLI || 'dsh'
      try {
        const { stdout, stderr } = await execFileAsync(cli, ['plugin', '--profile', 'desktop', op, spec],
          { timeout: 15 * 60 * 1000, maxBuffer: 32 * 1024 * 1024, windowsHide: true })
        sendJson(res, 200, { ok: true, log: ((stdout || '') + (stderr || '')).trim() || '(no output)' })
      } catch (e) {
        sendJson(res, 200, {
          ok: false,
          log: 'CLI \u5931\u8d25\uff1a' + String((e && e.message) || e) + '\n' + ((e && e.stdout) || '') + ((e && e.stderr) || '')
        })
      }
    })

    // ---- 开关插件 -------------------------------------------------------
    // 只改 profile 的 bundles 列表，不改别的。要重启 DSH 才真的生效，页面上
    // 明确写着这一点，不做"看起来成功了"的假象。
    route('/dsh-lanya/api/plugin-toggle', async (req, res) => {
      const body = await readBody(req)
      const name2 = String(body.name || '').trim()
      const want = body.enabled !== false
      if (!name2) { sendJson(res, 400, { ok: false, error: 'missing name' }); return }
      const pkgFile = path.join(os.homedir(), '.dsh', 'profiles', 'desktop', 'package.json')
      try {
        const raw = fs.readFileSync(pkgFile, 'utf8')
        const doc = JSON.parse(raw)
        const list = Array.isArray(doc?.dsh?.profile?.bundles) ? doc.dsh.profile.bundles.slice() : []
        const has = list.includes(name2)
        if (want && !has) list.push(name2)
        if (!want && has) {
          const idx = list.indexOf(name2)
          list.splice(idx, 1)
        }
        doc.dsh.profile.bundles = list
        fs.writeFileSync(pkgFile + '.tmp', JSON.stringify(doc, null, 2), 'utf8')
        fs.renameSync(pkgFile + '.tmp', pkgFile)
        sendJson(res, 200, { ok: true, enabled: want, restart: true })
      } catch (e) {
        sendJson(res, 500, { ok: false, error: String((e && e.message) || e) })
      }
    })

    // ---- 人设读写 -------------------------------------------------------
    route('/dsh-lanya/api/persona', async (req, res) => {
      if (req.method === 'GET') {
        try { sendJson(res, 200, { ok: true, text: fs.readFileSync(personaFile, 'utf8') }) }
        catch (e) { sendJson(res, 200, { ok: false, text: '', error: String((e && e.message) || e) }) }
        return
      }
      const body = await readBody(req)
      const text2 = typeof body.text === 'string' ? body.text : ''
      if (!text2.trim()) { sendJson(res, 400, { ok: false, error: 'empty' }); return }
      try {
        fs.writeFileSync(personaFile, text2, 'utf8')
        // 侧栏里再更新一次导入，让旧入口也看到同一份内容；失败不影响主路径 ----
        // （人设是每次组装提示词时现读磁盘的，所以这里不写也能立刻生效。）
        sendJson(res, 200, { ok: true, chars: text2.length })
      } catch (e) {
        sendJson(res, 500, { ok: false, error: String((e && e.message) || e) })
      }
    })

    scope.effect(() => () => {
      for (const d of disposers.splice(0)) { try { d() } catch { } }
    }, 'dsh-lanya: web routes')
  })

  ctx.logger?.info?.('dsh-lanya \u5df2\u542f\u7528\uff1a\u4eba\u8bbe ' + readText(personaFile).length +
    ' \u5b57\uff1b\u63a7\u5236\u53f0 /dsh-lanya/')
}

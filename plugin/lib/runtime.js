import fs from 'node:fs'
import path from 'node:path'
import { createHash } from 'node:crypto'

export function readJson(file, fallback = {}) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, '')) } catch { return fallback }
}

export function resolveRuntime(config = {}, options = {}) {
  const env = options.env || process.env
  const cwd = options.cwd || process.cwd()
  const usable = (value) => typeof value === 'string' && value.trim() && !/[<>]/.test(value)
  const explicit = env.LANYA_WORKDIR || config.workDir || config.workspacePath
  let workDir = usable(explicit) ? path.resolve(explicit) : ''
  if (!workDir && usable(config.personaFile) && path.isAbsolute(config.personaFile)) {
    const parent = path.dirname(config.personaFile)
    if (path.basename(parent).toLowerCase() === 'napcat') workDir = path.dirname(parent)
  }
  if (!workDir) {
    let base = path.resolve(cwd)
    for (;;) {
      if (fs.existsSync(path.join(base, 'napcat'))) { workDir = base; break }
      const parent = path.dirname(base)
      if (parent === base) break
      base = parent
    }
  }
  workDir ||= path.resolve(cwd)
  const napcatDir = path.join(workDir, 'napcat')
  const explicitProject = env.LANYA_PROJECT_CONFIG || config.projectConfig
  if (explicitProject && !usable(explicitProject)) throw new Error('Invalid explicit project configuration path')
  const candidates = explicitProject ? [explicitProject] :
    [path.join(napcatDir, 'config', 'project.json'), path.join(workDir, 'config', 'project.json')]
  let project = {}
  for (const file of candidates) {
    let raw
    try { raw = fs.readFileSync(file, 'utf8') } catch (error) {
      if (error.code === 'ENOENT' && !explicitProject) continue
      throw new Error('Project configuration is unavailable; independent activity is disabled', { cause:error })
    }
    let parsed
    try { parsed = JSON.parse(raw.replace(/^\uFEFF/, '')) } catch (error) {
      throw new Error('Project configuration is invalid; independent activity is disabled', { cause:error })
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Project configuration must be an object')
    project = parsed; break
  }
  const dataName = config.dataDirName || project.dataDirName || '\u5927\u80a5\u9c7c'
  const dataDir = usable(config.dataDir) ? path.resolve(config.dataDir) : path.join(workDir, dataName)
  const legacyName = dataName + '-\u4eba\u8bbe\u6b63\u6587.md'
  const explicitPersona = env.LANYA_PERSONA_FILE || config.personaFile
  const personas = [path.join(napcatDir, 'persona.md'),
    path.join(napcatDir, legacyName), path.join(dataDir, legacyName)].filter(usable)
  const personaFile = usable(explicitPersona) ? explicitPersona : (personas.find((file) => fs.existsSync(file)) || personas[0])
  return { workDir, napcatDir, dataDir, personaFile, project,
    affinityFile: usable(config.affinityFile) ? config.affinityFile : path.join(dataDir, 'affinity.json') }
}

export function localDay(now) {
  return [now.getFullYear(), String(now.getMonth() + 1).padStart(2, '0'), String(now.getDate()).padStart(2, '0')].join('-')
}

export function reminderKey(reminder, day = '') {
  const identity = ['at', 'daily', 'target', 'text', 'mention', 'image'].map((key) => String(reminder[key] || ''))
  const hash = createHash('sha256').update(identity.join('\0')).digest('hex')
  return day ? hash + '|' + day : hash
}

export function acquireReminderLock(dataDir) {
  const file = path.join(dataDir, 'reminders-send.lock')
  fs.mkdirSync(dataDir, { recursive: true })
  try {
    const fd = fs.openSync(file, 'wx')
    fs.writeFileSync(fd, String(process.pid))
    return () => { fs.closeSync(fd); try { fs.unlinkSync(file) } catch { } }
  } catch (error) {
    if (error.code !== 'EEXIST') throw error
    // Reclaim only a dead owner's lock; an unknown owner is left alone.
    try {
      const owner = Number(fs.readFileSync(file, 'utf8'))
      if (Number.isInteger(owner) && owner > 0) {
        try { process.kill(owner, 0) } catch (cause) {
          if (cause.code === 'ESRCH') { fs.unlinkSync(file); return acquireReminderLock(dataDir) }
        }
      }
    } catch { }
    return null
  }
}

// Pure orchestration: callers persist state before replacing the reminder list.
// A rejected delivery remains due and must never be silently removed.
export async function processReminders(list, previous, now, send, { persistState = async () => {} } = {}) {
  const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value)
  if (!Array.isArray(list) || !plain(previous) || !(now instanceof Date) || !Number.isFinite(now.getTime()) ||
      typeof send !== 'function' || typeof persistState !== 'function') throw new Error('Invalid reminder orchestration input')
  for (const key of ['daily', 'once', 'pending', 'outcomes']) {
    if (previous[key] !== undefined && !plain(previous[key])) throw new Error('Invalid persisted reminder state')
  }
  const state = { ...previous, daily: { ...(previous.daily || {}) }, once: { ...(previous.once || {}) },
    pending: { ...(previous.pending || {}) }, outcomes: { ...(previous.outcomes || {}) } }
  // Validate every persisted occurrence before the first dispatch. A damaged
  // pending record must never silently reopen a fresh daily occurrence.
  for (const [key, entry] of [...Object.entries(state.pending), ...Object.entries(state.outcomes)]) {
    if (!plain(entry) || !plain(entry.reminder) || entry.key !== key ||
        typeof entry.day !== 'string' || (entry.reminder.daily ? !/^\d{4}-\d{2}-\d{2}$/.test(entry.day) : entry.day !== '') ||
        entry.base_key !== reminderKey(entry.reminder) || key !== reminderKey(entry.reminder, entry.day) ||
        typeof entry.due_at !== 'string' || !Number.isFinite(Date.parse(entry.due_at))) {
      throw new Error('Invalid pending reminder occurrence')
    }
  }
  for (const [key, entry] of Object.entries(state.outcomes)) {
    if (!['decided','suppressed','unknown'].includes(entry.status) ||
        typeof entry.decision_id !== 'string' || !entry.decision_id.trim() || entry.decision_id.length > 256 ||
        /[\x00-\x1f]/.test(entry.decision_id) || state.pending[key]) {
      throw new Error('Invalid settled reminder outcome')
    }
  }
  const keep = []
  const visited = new Set()
  for (let i = 0; i < list.length; i++) {
    const r = list[i]
    if (!r || typeof r !== 'object') { keep.push(r); continue }
    const base = reminderKey(r)
    let due = r.at ? new Date(r.at) : new Date(NaN)
    const dailyMatch = /^([01]\d|2[0-3]):([0-5]\d)$/.exec(String(r.daily || ''))
    if (r.daily && dailyMatch) {
      due = new Date(now.getTime())
      due.setHours(Number(dailyMatch[1]), Number(dailyMatch[2]), 0, 0)
      // At 00:01 a 23:59 occurrence still belongs to the previous local day.
      if (due > now) due.setDate(due.getDate() - 1)
    }
    const minutesLate = (now.getTime() - due.getTime()) / 60_000
    const requestedGrace = Number(r.dailyGraceMinutes ?? 5)
    const grace = Number.isFinite(requestedGrace) ? Math.max(0, Math.min(60, requestedGrace)) : 5
    const ready = r.daily ? Boolean(dailyMatch) && minutesLate >= 0 && minutesLate <= grace
      : Number.isFinite(due.getTime()) && now >= due
    const day = r.daily && Number.isFinite(due.getTime()) ? localDay(due) : ''
    const key = reminderKey(r, day)
    const delivered = entryKey => r.daily ? state.daily[entryKey] === 'yes' : Boolean(state.once[entryKey])
    // Settling yesterday's idle/unknown is not delivery, and does not cancel
    // tomorrow's independently scheduled occurrence. A deferred old occurrence
    // and today's new one may coexist with distinct durable keys.
    if (ready && !delivered(key) && !state.outcomes[key] && !state.pending[key]) {
      state.pending[key] = { key, base_key:base, day, due_at:due.toISOString(), reminder:JSON.parse(JSON.stringify(r)) }
    }
    const outstanding = Object.values(state.pending).filter(entry => entry.base_key === base)
      .sort((a,b) => Date.parse(a.due_at)-Date.parse(b.due_at) || a.key.localeCompare(b.key))
    for (const occurrence of outstanding) {
      const originalKey = occurrence.key
      if (visited.has(originalKey)) continue
      visited.add(originalKey)
      const legacyKey = r.daily ? 'daily:' + i + ':' + r.daily + ':' + occurrence.day : 'at:' + i + ':' + r.at
      if (delivered(originalKey) || state[legacyKey]) {
        if (r.daily) state.daily[originalKey] = 'yes'
        else state.once[originalKey] ||= now.toISOString()
        delete state.pending[originalKey]
      } else {
        // The installed caller persists this intent BEFORE the producer can
        // publish evidence. A crash then resumes the same original day/key.
        await persistState(state)
        try {
          const outcome = await send(occurrence.reminder, { key:occurrence.key, now,
            occurrence:{ key:occurrence.key, day:occurrence.day, due_at:occurrence.due_at } })
          if (outcome?.delivered !== false) {
            if (r.daily) state.daily[originalKey] = 'yes'
            else state.once[originalKey] = now.toISOString()
            delete state.pending[originalKey]
            await persistState(state)
          } else if (outcome.terminal === true) {
            if (!['decided','suppressed','unknown'].includes(outcome.status) ||
                typeof outcome.decision_id !== 'string' || !outcome.decision_id.trim()) {
              throw new Error('Invalid terminal reminder outcome')
            }
            state.outcomes[originalKey] = { ...occurrence, status:outcome.status, decision_id:outcome.decision_id }
            delete state.pending[originalKey]
            await persistState(state)
          }
        } catch { }
      }
    }
    if (r.at && !r.daily && delivered(key)) continue
    keep.push(r)
  }
  for (const key of Object.keys(state)) {
    if (typeof state[key] === 'number' && state[key] < now.getTime() - 7 * 86400000) delete state[key]
  }
  return { keep, state }
}

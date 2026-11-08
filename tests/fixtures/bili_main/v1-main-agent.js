/** Direct Bilibili primitives for the existing chat agent, never a new LLM.
 * One private ledger belongs to one Bilibili account. It retains only argument
 * hashes and delivery receipts, not message bodies. A new callId is a new
 * action; this is execution replay protection, not semantic content dedup.
 */
import fs from 'node:fs'
import path from 'node:path'
import { createHash } from 'node:crypto'
import { DatabaseSync } from 'node:sqlite'
import { fileURLToPath } from 'node:url'

const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const digest = value => createHash('sha256').update(JSON.stringify(value)).digest('hex')
const APP_ID = 1279414860

function ordinary(file, { parents = false } = {}) {
  if (typeof file !== 'string' || !path.isAbsolute(file)) throw new Error('Explicit absolute Bili maintenance paths are required')
  const absolute = path.resolve(file), root = path.parse(absolute).root
  let current = root
  const parts = absolute.slice(root.length).split(path.sep).filter(Boolean)
  for (let index = 0; index < parts.length; index++) {
    current = path.join(current, parts[index])
    let info = fs.lstatSync(current, { throwIfNoEntry: false })
    if (parents && index < parts.length - 1 && !info) {
      try { fs.mkdirSync(current) } catch (error) { if (error.code !== 'EEXIST') throw error }
      info = fs.lstatSync(current)
    }
    if (!info) continue
    if (info.isSymbolicLink() || (index < parts.length - 1 ? !info.isDirectory() : !info.isFile())) {
      throw new Error('Bili maintenance paths must be ordinary files and directories')
    }
  }
  return absolute
}

function readObject(file) {
  const raw = fs.readFileSync(ordinary(file))
  if (raw.length > 1024 * 1024) throw new Error('Invalid Bili decision configuration')
  try {
    const value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(raw).replace(/^\uFEFF/, ''))
    if (!plain(value)) throw new Error()
    return value
  } catch { throw new Error('Invalid Bili decision configuration') }
}

function projectMode(projectConfig) {
  // This path was explicitly selected in the reviewed private patch config.
  // An unrelated workspace's inherited environment cannot select legacy mode.
  const value = readObject(projectConfig)
  if (value.decisions !== undefined && !plain(value.decisions)) throw new Error('Invalid Bili decision configuration')
  const mode = value.decisions?.mode ?? 'unified'
  if (!['unified', 'legacy'].includes(mode)) throw new Error('Unknown Bili decision mode')
  return mode
}

function executionKey(exec) {
  const session = exec?.agent?.session?.id, call = exec?.callId
  if (typeof session !== 'string' || !session || typeof call !== 'string' || !call || exec.signal?.aborted) {
    throw new Error('A live host execution identity is required for Bili delivery')
  }
  return digest(['bili_send_message', session, call])
}

function database(file) {
  ordinary(file, { parents: true })
  const db = new DatabaseSync(file)
  try {
    db.exec('PRAGMA busy_timeout=2500; PRAGMA synchronous=FULL')
    const version = db.prepare('PRAGMA user_version').get().user_version
    const app = db.prepare('PRAGMA application_id').get().application_id
    const tables = db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").all()
    if (!((version === 1 && app === APP_ID && tables.length === 1 && tables[0].name === 'deliveries') ||
      (version === 0 && app === 0 && tables.length === 0))) throw new Error('Unknown Bili delivery ledger')
    db.exec('BEGIN IMMEDIATE')
    db.exec("CREATE TABLE IF NOT EXISTS deliveries (operation_key TEXT PRIMARY KEY, args_hash TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('reserved','sent','failed','unknown')), receipt TEXT, created_at INTEGER NOT NULL); PRAGMA user_version=1; PRAGMA application_id=" + APP_ID)
    db.exec('COMMIT')
    return db
  } catch (error) {
    try { db.exec('ROLLBACK') } catch {}
    db.close()
    throw new Error('Bili delivery ledger unavailable')
  }
}

function reserve(file, operation, argsHash, now, limit) {
  const db = database(file)
  try {
    db.exec('BEGIN IMMEDIATE')
    const previous = db.prepare('SELECT args_hash,status FROM deliveries WHERE operation_key=?').get(operation)
    if (previous) {
      db.exec('COMMIT')
      if (previous.args_hash !== argsHash) return { ok: false, status: 'blocked', reason: 'execution_arguments_changed' }
      return { ok: false, status: 'duplicate', previous_status: previous.status === 'reserved' ? 'unknown' : previous.status }
    }
    if (db.prepare('SELECT COUNT(*) AS count FROM deliveries').get().count >= limit) throw new Error('Bili delivery ledger is full')
    db.prepare("INSERT INTO deliveries VALUES (?,?,'reserved',NULL,?)").run(operation, argsHash, now())
    db.exec('COMMIT')
    return null
  } catch (error) { try { db.exec('ROLLBACK') } catch {}; throw error }
  finally { db.close() }
}

function settle(file, operation, status, receipt) {
  const db = database(file)
  try {
    const result = db.prepare("UPDATE deliveries SET status=?,receipt=? WHERE operation_key=? AND status='reserved'")
      .run(status, receipt === undefined ? null : String(receipt), operation)
    if (result.changes !== 1) throw new Error('Bili reservation changed')
  } finally { db.close() }
}

function response(value) {
  if (typeof value === 'string') { try { return JSON.parse(value) } catch { return value } }
  return value
}

function validReceipt(value) {
  return typeof value === 'number' ? Number.isSafeInteger(value) && value > 0
    : typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(value)
}

export function createBiliMainAgent({ call, configFile, projectConfig, ledgerFile, now = Date.now, maxDeliveries = 10000 } = {}) {
  if (typeof call !== 'function') throw new Error('A direct MCP client is required')
  if (configFile !== undefined) {
    const config = readObject(configFile instanceof URL ? fileURLToPath(configFile) : configFile)
    if (Object.keys(config).some(key => !['projectConfig', 'ledgerFile'].includes(key))) throw new Error('Unknown Bili bridge configuration')
    ;({ projectConfig, ledgerFile } = config)
  }
  projectConfig = ordinary(projectConfig)
  ledgerFile = ordinary(ledgerFile)
  if (projectConfig === ledgerFile || !ledgerFile.endsWith('.sqlite') || !Number.isSafeInteger(maxDeliveries) || maxDeliveries <= 0) {
    throw new Error('An independent private Bili SQLite ledger is required')
  }
  const mode = () => projectMode(projectConfig)
  const read = async (name, args, exec) => {
    mode() // Bad/missing explicit configuration never falls back to legacy.
    if (exec?.signal?.aborted) throw new Error('Bili execution was aborted')
    const value = response(await call(name, args, { signal: exec?.signal }))
    if (plain(value) && (value.ok === false || value.success === false)) return { ok: false, status: 'failed', reason: 'platform_rejected' }
    return value
  }
  const recommend = (args = {}, exec) => {
    const count = args.ps ?? 20
    if (!Number.isSafeInteger(count) || count < 1 || count > 40) throw new Error('ps must be an integer from 1 to 40')
    return read('bili_recommend', { ps: count }, exec)
  }
  const send = async (args, exec) => {
    mode()
    const uid = args?.receiver_uid, text = args?.text
    if (!Number.isSafeInteger(uid) || uid <= 0 || typeof text !== 'string' || !text.trim() || text.length > 8000) {
      throw new Error('A positive receiver_uid and nonempty text up to 8000 characters are required')
    }
    const operation = executionKey(exec), input = { receiver_uid: uid, text: text.trim() }
    const previous = reserve(ledgerFile, operation, digest(input), now, maxDeliveries)
    if (previous) return previous
    let status = 'unknown', receipt
    try {
      if (exec.signal?.aborted) throw new Error('Bili execution was aborted')
      const value = response(await call('bili_send_message', input, { signal: exec.signal }))
      const result = plain(value?.data) ? value.data : {}
      receipt = result.msg_key ?? result.data?.msg_key
      if (value?.success === false || value?.ok === false || (result.code !== undefined && result.code !== null && result.code !== 0)) status = 'failed'
      else if (value?.success === true && validReceipt(receipt)) status = 'sent'
    } catch { /* May have reached the platform: keep uncertainty, never retry. */ }
    try { settle(ledgerFile, operation, status, status === 'sent' ? receipt : undefined) }
    catch { return { ok: false, status: 'unknown', reason: 'receipt_persistence_failed' } }
    return status === 'sent' ? { ok: true, status, receipt: String(receipt) }
      : { ok: false, status, reason: status === 'failed' ? 'platform_rejected' : 'delivery_unconfirmed' }
  }
  return {
    mode,
    async run(args, exec, legacy) {
      if (mode() === 'legacy') return legacy()
      const data = await recommend(args, exec)
      const ok = !(plain(data) && data.ok === false)
      return { ok, mode: 'unified', status: ok ? 'observed' : 'failed', data,
        instruction: 'Continue in this same agent using direct Bili primitives if useful, or idle. No new model was started and no QQ report is required.' }
    },
    tools: () => [
      { name: 'bili_video_info', description: 'Read actual Bilibili video details in this agent; no separate LLM.',
        parameters: { bvid: { type: 'string', required: true } }, isConcurrencySafe: () => true,
        execute(args, exec) {
          if (typeof args?.bvid !== 'string' || !/^BV[0-9A-Za-z]{10}$/.test(args.bvid)) throw new Error('A valid BV video ID is required')
          return read('bili_video_info', { bvid: args.bvid }, exec)
        } },
      { name: 'bili_recommend', description: 'Read 1–40 actual recommendations in this same agent; ps defaults to 20.',
        parameters: { ps: { type: 'number' } }, isConcurrencySafe: () => true, execute: recommend },
      { name: 'bili_send_message', description: 'Send a Bilibili private message using this main agent. Needs a real receipt; unknown/duplicate is never a delivery claim or automatic retry.',
        parameters: { receiver_uid: { type: 'number', required: true }, text: { type: 'string', required: true } },
        isConcurrencySafe: () => false, execute: send }
    ]
  }
}

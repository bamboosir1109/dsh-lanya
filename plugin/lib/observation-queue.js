/** Immutable evidence inbox for the main chat LLM. No model or QQ transport is
 * owned here. dispatch(event) resolves ONLY after that decision turn settles.
 * A failed dispatch leaves evidence pending; a sent/unknown result is retained
 * durably instead of being blindly replayed. One pump owns a bot/role inbox.
 */
import fs from 'node:fs'
import path from 'node:path'
import { createHash, randomUUID } from 'node:crypto'
import { DatabaseSync } from 'node:sqlite'

export class ObservationQueueError extends Error {
  constructor(message) { super(message); this.name = 'ObservationQueueError' }
}
const plain = (x) => x !== null && typeof x === 'object' && !Array.isArray(x)
const hash = (x) => createHash('sha256').update(x).digest('hex')
const identity = (x) => typeof x === 'string' && x.length > 0 && x.length <= 256 && !/[\x00-\x1f]/.test(x)
const terminalStatus = (x) => ['decided', 'sent', 'unknown', 'suppressed'].includes(x)
const decisionIdentity = (x) => identity(x) && x.trim().length > 0
const producerOwnedReview = new Set(['activity_due', 'reminder.due'])
function stable(value) {
  if (Array.isArray(value)) return '[' + value.map(stable).join(',') + ']'
  if (plain(value)) return '{' + Object.keys(value).sort().map((key) => JSON.stringify(key) + ':' + stable(value[key])).join(',') + '}'
  return JSON.stringify(value)
}

function validateCompletion(marker, event, sha256) {
  if (!plain(marker) || marker.version !== 1 || marker.event_sha256 !== sha256 ||
      !terminalStatus(marker.status) || !decisionIdentity(marker.decision_id) ||
      ('scope' in marker && marker.scope !== event.scope) ||
      ('event_id' in marker && marker.event_id !== event.event_id) ||
      ('completed_at' in marker && (!Number.isSafeInteger(marker.completed_at) || marker.completed_at < 0)) ||
      ('next_review_at' in marker && (!Number.isSafeInteger(marker.next_review_at) ||
        marker.next_review_at <= 0 || marker.next_review_at <= event.created_at))) {
    throw new ObservationQueueError('Invalid terminal completion or review metadata')
  }
  return marker
}

export function observationKey(scope, eventId) { return hash(scope + '\0' + eventId) }
export function validateObservation(event) {
  if (!plain(event) || event.version !== 1 || typeof event.scope !== 'string' ||
      !/^(private|group):[1-9][0-9]*$/.test(event.scope) ||
      !['event_id', 'kind', 'producer'].every((k) => identity(event[k])) ||
      !/^[a-z][a-z0-9_.-]{0,63}$/.test(event.kind) ||
      event.origin !== 'observation' || event.control_allowed !== false || !plain(event.payload) ||
      typeof event.workspace_key !== 'string' || typeof event.agent_key !== 'string' ||
      !Number.isSafeInteger(event.created_at) || event.created_at < 0) {
    throw new ObservationQueueError('Invalid observation metadata; no user/control impersonation is permitted')
  }
  return event
}

function ordinaryDirectories(value, create = false) {
  const absolute = path.resolve(value)
  const parsed = path.parse(absolute)
  let current = parsed.root
  for (const part of absolute.slice(parsed.root.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, part)
    if (create && !fs.existsSync(current)) {
      try { fs.mkdirSync(current) } catch (error) { if (error.code !== 'EEXIST') throw error }
    }
    if (fs.existsSync(current)) {
      const info = fs.lstatSync(current)
      if (!info.isDirectory() || info.isSymbolicLink()) throw new ObservationQueueError('Inbox parents must be ordinary directories')
    }
  }
  return absolute
}

function readRegular(file) {
  const before = fs.lstatSync(file, { bigint: true })
  if (!before.isFile() || before.isSymbolicLink() || before.size > 512n * 1024n) throw new ObservationQueueError('Inbox entry is not a bounded ordinary file')
  const raw = fs.readFileSync(file)
  const after = fs.lstatSync(file, { bigint: true })
  if (['dev', 'ino', 'size', 'mtimeNs'].some((k) => before[k] !== after[k])) throw new ObservationQueueError('Inbox entry changed during read')
  return { value: JSON.parse(raw.toString('utf8')), sha256: hash(raw) }
}

function publish(file, value) {
  const temporary = path.join(path.dirname(file), '.complete-' + randomUUID() + '.tmp')
  const fd = fs.openSync(temporary, 'wx', 0o600)
  try {
    fs.writeFileSync(fd, JSON.stringify(value) + '\n', 'utf8')
    fs.fsyncSync(fd)
  } finally { fs.closeSync(fd) }
  try {
    // link() publishes a complete file atomically and refuses an existing ack.
    fs.linkSync(temporary, file)
  } finally { fs.unlinkSync(temporary) }
}

function ownerAlive(pid) {
  try { process.kill(pid, 0); return true } catch (error) { return error.code !== 'ESRCH' }
}

export function createObservationPump({ directory, dispatch, acknowledge = async () => {}, log = () => {}, now = Date.now,
  intervalMs = 2000, isOwnerAlive = ownerAlive } = {}) {
  if (typeof directory !== 'string' || !directory.trim() || typeof dispatch !== 'function') throw new ObservationQueueError('directory and dispatch are required')
  const base = path.resolve(directory)
  const pending = path.join(base, 'pending')
  const done = path.join(base, 'done')
  const leaseFile = path.join(base, 'consumer.sqlite')
  let timer, busy = false, stopped = true, release = null, leaseDb = null

  function takeClaim() {
    ordinaryDirectories(pending, true)
    ordinaryDirectories(done, true)
    if (fs.existsSync(leaseFile)) {
      const info = fs.lstatSync(leaseFile)
      if (!info.isFile() || info.isSymbolicLink()) throw new ObservationQueueError('Consumer lease must be an ordinary SQLite file')
    }
    const db = new DatabaseSync(leaseFile)
    const nonce = randomUUID()
    try {
      db.exec('PRAGMA busy_timeout=2500; PRAGMA synchronous=FULL')
      const version = db.prepare('PRAGMA user_version').get().user_version
      const tables = db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").all()
      if (version !== 1 && !(version === 0 && tables.length === 0)) throw new ObservationQueueError('Unknown consumer lease schema')
      db.exec('BEGIN IMMEDIATE')
      db.exec('CREATE TABLE IF NOT EXISTS consumer (id INTEGER PRIMARY KEY CHECK(id=1), pid INTEGER NOT NULL, nonce TEXT NOT NULL); CREATE TABLE IF NOT EXISTS retry (event_key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, next_after INTEGER NOT NULL); PRAGMA user_version=1')
      const owner = db.prepare('SELECT pid,nonce FROM consumer WHERE id=1').get()
      if (owner && (!Number.isSafeInteger(owner.pid) || owner.pid <= 0 || !identity(owner.nonce) || isOwnerAlive(owner.pid))) {
        db.exec('ROLLBACK'); db.close()
        log('warn', 'observation_consumer_already_owned')
        return false
      }
      // Atomic SQLite replacement avoids stale-file unlink/ABA races. A long
      // running living owner is never reclaimed merely because time elapsed.
      db.prepare('INSERT OR REPLACE INTO consumer(id,pid,nonce) VALUES (1,?,?)').run(process.pid, nonce)
      db.exec('COMMIT')
      leaseDb = db
      release = () => {
        try { db.prepare('DELETE FROM consumer WHERE id=1 AND pid=? AND nonce=?').run(process.pid, nonce) } finally { db.close(); leaseDb = null }
      }
      return true
    } catch (error) {
      try { db.exec('ROLLBACK') } catch { }
      db.close()
      throw error
    }
  }

  function failed(key) {
    const attempts = (leaseDb.prepare('SELECT attempts FROM retry WHERE event_key=?').get(key)?.attempts || 0) + 1
    const delay = Math.min(600_000, 2000 * (2 ** Math.min(attempts - 1, 8)))
    leaseDb.prepare('INSERT OR REPLACE INTO retry(event_key,attempts,next_after) VALUES (?,?,?)').run(key, attempts, now() + delay)
  }

  function parkInvalidTerminal(key) {
    // A claimed terminal result with invalid metadata may already have sent.
    // Keep evidence and durably require repair, never call the model again.
    leaseDb.prepare('INSERT OR REPLACE INTO retry(event_key,attempts,next_after) VALUES (?,?,?)').run(key, 1, Number.MAX_SAFE_INTEGER)
  }

  function scheduleReview(event, completed) {
    if (completed.status === 'unknown' || producerOwnedReview.has(event.kind) ||
        !('next_review_at' in completed) || completed.next_review_at > now()) return null
    const parentKey = observationKey(event.scope, event.event_id)
    const review = validateObservation({ ...event,
      event_id: 'main-review:' + parentKey + ':' + completed.next_review_at,
      producer: 'main-review', created_at: completed.next_review_at,
      origin: 'observation', control_allowed: false,
      payload: { ...event.payload, review_context: {
        parent_observation_key: parentKey, parent_event_id: event.event_id,
        previous_decision_id: completed.decision_id, scheduled_at: completed.next_review_at,
      } },
    })
    if (Buffer.byteLength(JSON.stringify(review), 'utf8') > 256 * 1024) throw new ObservationQueueError('Review observation exceeds size limit')
    const name = observationKey(review.scope, review.event_id) + '.json'
    const target = path.join(pending, name)
    if (!fs.existsSync(target)) {
      if (fs.existsSync(path.join(done, name))) throw new ObservationQueueError('Review completion has no original evidence')
      try { publish(target, review) } catch (error) { if (error.code !== 'EEXIST') throw error }
    }
    const read = readRegular(target)
    validateObservation(read.value)
    if (stable(read.value) !== stable(review)) throw new ObservationQueueError('Review identity conflicts with immutable evidence')
    const marker = path.join(done, name)
    if (fs.existsSync(marker)) {
      validateCompletion(readRegular(marker).value, review, read.sha256)
      return null
    }
    if ((leaseDb.prepare('SELECT next_after FROM retry WHERE event_key=?').get(name)?.next_after || 0) > now()) return null
    return { event: review, name, sha256: read.sha256 }
  }

  async function drain() {
    if (busy || !release) return { processed: 0, status: 'not_owner_or_busy' }
    busy = true
    let processed = 0
    try {
      ordinaryDirectories(pending)
      ordinaryDirectories(done)
      const names = fs.readdirSync(pending).filter((name) => /^[0-9a-f]{64}\.json$/.test(name))
      const entries = new Map()
      for (const name of names) {
        try {
          if ((leaseDb.prepare('SELECT next_after FROM retry WHERE event_key=?').get(name)?.next_after || 0) > now()) continue
          const read = readRegular(path.join(pending, name))
          const event = validateObservation(read.value)
          if (name !== observationKey(event.scope, event.event_id) + '.json') throw new ObservationQueueError('Observation filename/identity mismatch')
          const marker = path.join(done, name)
          if (fs.existsSync(marker)) {
            const completed = validateCompletion(readRegular(marker).value, event, read.sha256)
            // A crash after durable completion but before core acknowledgement
            // is recovered here, without running the model or sending again.
            await acknowledge(event, completed)
            const review = scheduleReview(event, completed)
            if (review) entries.set(review.name, review)
            continue
          }
          entries.set(name, { event, name, sha256: read.sha256 })
        } catch (error) { log('error', 'observation_invalid:' + name + ':' + error.name) }
      }
      const ordered = [...entries.values()].sort((a, b) => a.event.created_at - b.event.created_at || a.name.localeCompare(b.name))
      for (const { event, name, sha256 } of ordered) {
        if (stopped) break
        if ((leaseDb.prepare('SELECT next_after FROM retry WHERE event_key=?').get(name)?.next_after || 0) > now()) continue
        let outcome
        try { outcome = await dispatch(event) } catch (error) {
          failed(name)
          log('warn', 'observation_dispatch_failed:' + name + ':' + error.name)
          continue
        }
        // Errors before a real verdict are never consumed. Unknown transport
        // outcomes ARE terminal evidence and may not be retried automatically.
        if (!plain(outcome) || outcome.completed !== true) {
          failed(name)
          log('warn', 'observation_without_terminal_decision:' + name)
          continue
        }
        const current = readRegular(path.join(pending, name))
        if (current.sha256 !== sha256) throw new ObservationQueueError('Observation changed before completion')
        const marker = { version: 1, event_id: event.event_id, scope: event.scope,
          event_sha256: sha256, completed_at: now(), status: outcome.status,
          decision_id: outcome.decision_id }
        // null/undefined in a dispatch receipt means the LLM did not schedule a
        // review. A persisted marker with an explicit null timestamp is invalid.
        if (outcome.next_review_at !== undefined && outcome.next_review_at !== null) {
          marker.next_review_at = outcome.next_review_at
        }
        try { validateCompletion(marker, event, sha256) } catch (error) {
          parkInvalidTerminal(name)
          log('error', 'observation_invalid_terminal_parked:' + name + ':' + error.name)
          continue
        }
        publish(path.join(done, name), marker)
        leaseDb.prepare('DELETE FROM retry WHERE event_key=?').run(name)
        await acknowledge(event, marker)
        processed += 1
      }
      return { processed, status: 'drained' }
    } finally {
      busy = false
      if (stopped && release) { release(); release = null }
    }
  }

  return {
    async start({ poll = true } = {}) {
      if (!stopped || busy) return false
      stopped = false
      try {
        if (!takeClaim()) { stopped = true; return false }
      } catch (error) { stopped = true; throw error }
      if (poll) {
        const tick = () => { void drain().catch((error) => log('error', 'observation_pump_failed:' + error.name)) }
        timer = setInterval(tick, intervalMs)
        timer.unref?.()
        tick()
      }
      return true
    },
    drain,
    stop() {
      stopped = true
      if (timer) { clearInterval(timer); timer = null }
      // Retain the ownership lock until any in-flight decision settles.
      if (!busy && release) { release(); release = null }
    },
  }
}

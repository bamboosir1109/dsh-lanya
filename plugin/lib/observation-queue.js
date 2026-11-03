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
const MAX_CONCURRENT_SCOPES = 4
const producerOwnedReview = new Set(['activity_due', 'reminder.due'])
export const MAX_OBSERVATION_BYTES = 256 * 1024
// Sixfold string escaping plus repeated event identities and bounded metadata.
export const MAX_COMPLETION_BYTES = 8 * MAX_OBSERVATION_BYTES + 16 * 1024
function stable(value) {
  if (Array.isArray(value)) return '[' + value.map(stable).join(',') + ']'
  if (plain(value)) return '{' + Object.keys(value).sort().map((key) => JSON.stringify(key) + ':' + stable(value[key])).join(',') + '}'
  return JSON.stringify(value)
}

function validateCompletion(marker, event, sha256, raw) {
  if (plain(marker) && marker.version === 3) {
    const fields = ['version', 'event_raw', 'event_id', 'scope', 'event_sha256', 'completed_at', 'status',
      'reason', 'local_outcome_id', 'observation_revision', 'native_turn_completed']
    const restClosure = ['autonomy_rest', 'observation_expired'].includes(marker.reason) && marker.native_turn_completed === false
    if (Object.keys(marker).sort().join(',') !== fields.sort().join(',') || marker.status !== 'no_decision' ||
        !((marker.reason === 'model_completed_without_decision' && marker.native_turn_completed === true) ||
          (marker.reason === 'native_admission_refused' && marker.native_turn_completed === false) || restClosure) ||
        !(restClosure || (event.kind === 'activity.opportunity' && ['mind', 'main-review'].includes(event.producer)) ||
          (event.kind === 'social.opportunity' && event.producer === 'social-planner')) ||
        marker.scope !== event.scope || marker.event_id !== event.event_id || marker.event_sha256 !== sha256 ||
        !Number.isSafeInteger(marker.observation_revision) || marker.observation_revision < 1 ||
        !Number.isSafeInteger(marker.completed_at) || marker.completed_at < event.created_at ||
        marker.local_outcome_id !== hash(['no-decision', event.scope, event.event_id, marker.observation_revision].join('\0')) ||
        typeof marker.event_raw !== 'string' || !Buffer.from(marker.event_raw, 'utf8').equals(raw) ||
        Buffer.from(marker.event_raw, 'utf8').toString('utf8') !== marker.event_raw) {
      throw new ObservationQueueError('Invalid local no-decision completion')
    }
    return marker
  }
  if (!plain(marker) || ![1, 2].includes(marker.version) || marker.event_sha256 !== sha256 ||
      !terminalStatus(marker.status) || !decisionIdentity(marker.decision_id) ||
      ('scope' in marker && marker.scope !== event.scope) ||
      ('event_id' in marker && marker.event_id !== event.event_id) ||
      ('completed_at' in marker && (!Number.isSafeInteger(marker.completed_at) || marker.completed_at < 0)) ||
      ('next_review_at' in marker && (!Number.isSafeInteger(marker.next_review_at) ||
        marker.next_review_at <= 0 || marker.next_review_at <= event.created_at))) {
    throw new ObservationQueueError('Invalid terminal completion or review metadata')
  }
  if (marker.version === 2 && (typeof marker.event_raw !== 'string' ||
      marker.scope !== event.scope || marker.event_id !== event.event_id ||
      Buffer.from(marker.event_raw, 'utf8').toString('utf8') !== marker.event_raw ||
      !Buffer.from(marker.event_raw, 'utf8').equals(raw))) {
    throw new ObservationQueueError('Completion original does not match immutable evidence')
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

function readRegular(file, maxBytes = MAX_COMPLETION_BYTES) {
  const before = fs.lstatSync(file, { bigint: true })
  if (!before.isFile() || before.isSymbolicLink() || before.size > BigInt(maxBytes)) throw new ObservationQueueError('Inbox entry is not a bounded ordinary file')
  const raw = fs.readFileSync(file)
  const after = fs.lstatSync(file, { bigint: true })
  if (['dev', 'ino', 'size', 'mtimeNs'].some((k) => before[k] !== after[k])) throw new ObservationQueueError('Inbox entry changed during read')
  const text = new TextDecoder('utf-8', { fatal: true }).decode(raw)
  return { raw, value: JSON.parse(text), sha256: hash(raw) }
}

/** Missing pending is recoverable only from a valid v2 terminal original.
 * Present corrupt evidence always wins over the backup: never hide tampering.
 */
export function readObservationEvidence(directory, key) {
  if (!/^[0-9a-f]{64}$/.test(key)) throw new ObservationQueueError('Invalid evidence filename')
  const base = ordinaryDirectories(directory)
  const target = path.join(ordinaryDirectories(path.join(base, 'pending')), key + '.json')
  const markerPath = path.join(ordinaryDirectories(path.join(base, 'done')), key + '.json')
  let read, marker, completionSha256 = null, hasMarker = false
  try { read = readRegular(target, MAX_OBSERVATION_BYTES) } catch (error) {
    if (error.code !== 'ENOENT') throw error
    const markerRead = readRegular(markerPath)
    marker = markerRead.value
    completionSha256 = markerRead.sha256
    hasMarker = true
    if (!plain(marker) || ![2, 3].includes(marker.version) || typeof marker.event_raw !== 'string') throw new ObservationQueueError('Missing original requires an exact terminal original')
    const raw = Buffer.from(marker.event_raw, 'utf8')
    if (raw.length > MAX_OBSERVATION_BYTES || raw.toString('utf8') !== marker.event_raw) throw new ObservationQueueError('Invalid UTF8 completion original')
    read = { raw, value: JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(raw)), sha256: hash(raw) }
  }
  const event = validateObservation(read.value)
  if (observationKey(event.scope, event.event_id) !== key) throw new ObservationQueueError('Observation filename/identity mismatch')
  if (!hasMarker) {
    try {
      const markerRead = readRegular(markerPath)
      marker = markerRead.value
      completionSha256 = markerRead.sha256
      hasMarker = true
    } catch (error) { if (error.code !== 'ENOENT') throw error }
  }
  if (hasMarker) validateCompletion(marker, event, read.sha256, read.raw)
  return { ...read, completion: hasMarker ? marker : null, completionSha256 }
}

const queuedReview = event => event.producer === 'main-review' && event.event_id.startsWith('main-review:')
function sameReviewOwner(left, right) {
  return ['scope', 'workspace_key', 'agent_key', 'kind'].every(key => left[key] === right[key])
}
function reviewAnchor(directory, event, depth = 0, seen = new Set()) {
  const key = observationKey(event.scope, event.event_id)
  if (depth >= 64 || seen.has(key)) throw new ObservationQueueError('Review freshness ancestry is not bounded and acyclic')
  seen.add(key)
  if (!queuedReview(event)) {
    const original = readObservationEvidence(directory, key)
    if (stable(original.value) !== stable(event)) throw new ObservationQueueError('Review freshness original conflicts')
    return { key, sha256: original.sha256, created_at: event.created_at }
  }
  const context = event.payload.review_context
  if (!plain(context) || !/^[0-9a-f]{64}$/.test(context.parent_observation_key) ||
      !identity(context.parent_event_id) || !decisionIdentity(context.previous_decision_id) ||
      !Number.isSafeInteger(context.scheduled_at) || context.scheduled_at !== event.created_at ||
      event.event_id !== 'main-review:' + context.parent_observation_key + ':' + context.scheduled_at) {
    throw new ObservationQueueError('Review freshness requires exact parent metadata')
  }
  const parent = readObservationEvidence(directory, context.parent_observation_key), completed = parent.completion
  if (!sameReviewOwner(event, parent.value) || parent.value.event_id !== context.parent_event_id ||
      !completed || ['unknown', 'no_decision'].includes(completed.status) ||
      completed.decision_id !== context.previous_decision_id || completed.next_review_at !== context.scheduled_at) {
    throw new ObservationQueueError('Review freshness parent receipt conflicts')
  }
  const anchor = context.freshness_anchor
  if (anchor !== undefined) {
    if (!plain(anchor) || Object.keys(anchor).sort().join(',') !== 'created_at,key,sha256' ||
        !/^[0-9a-f]{64}$/.test(anchor.key) || !/^[0-9a-f]{64}$/.test(anchor.sha256) ||
        !Number.isSafeInteger(anchor.created_at) || anchor.created_at < 0 || anchor.created_at > parent.value.created_at) {
      throw new ObservationQueueError('Review freshness anchor is invalid')
    }
    const inherited = queuedReview(parent.value) ? parent.value.payload.review_context?.freshness_anchor : null
    if (inherited) {
      if (stable(inherited) !== stable(anchor)) throw new ObservationQueueError('Review freshness anchor differs from parent')
    } else {
      const expected = queuedReview(parent.value) ? reviewAnchor(directory, parent.value, depth + 1, seen)
        : { key: context.parent_observation_key, sha256: parent.sha256, created_at: parent.value.created_at }
      if (stable(expected) !== stable(anchor)) throw new ObservationQueueError('Review freshness anchor is not the original')
    }
    const original = anchor.key === context.parent_observation_key ? parent : readObservationEvidence(directory, anchor.key)
    if (original.sha256 !== anchor.sha256 || !sameReviewOwner(event, original.value) ||
        original.value.created_at !== anchor.created_at || queuedReview(original.value)) {
      throw new ObservationQueueError('Review freshness anchor original conflicts')
    }
    return { ...anchor }
  }
  // Only pre-upgrade unanchored reviews use this bounded migration walk.
  return reviewAnchor(directory, parent.value, depth + 1, seen)
}

/** Queue-generated reviews inherit real source age, not a fresh greeting at
 * each due timestamp. Model payload fields never override the physical root. */
export function observationFreshness(directory, event) {
  validateObservation(event)
  return queuedReview(event) ? reviewAnchor(directory, event).created_at : event.created_at
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
  let rescanRequested = false, wakeDrain = null
  const invalidEvidence = new Set()
  // A restart retries acknowledgement from durable evidence. Within this pump,
  // unchanged terminal bytes need not reacquire the policy lock every poll.
  // This cache never authorizes dispatch, hides changed evidence or marks sends.
  const acknowledgedTerminals = new Map()

  async function acknowledgeTerminal(event, marker, name, eventHash, markerHash) {
    const fingerprint = eventHash + ':' + markerHash
    if (acknowledgedTerminals.get(name) === fingerprint) return
    const result = await acknowledge(event, marker)
    // Legacy callbacks resolve void. A missing pin is the idempotent result of
    // an earlier successful ack (including a crash before this cache existed).
    // Other negative outcomes remain retryable and never enter this cache.
    if (result === undefined || (plain(result) && (result.acknowledged === true ||
        (result.acknowledged === false && result.reason === 'unknown_observation')))) {
      acknowledgedTerminals.set(name, fingerprint)
    }
  }

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
    if (['unknown', 'no_decision'].includes(completed.status) || producerOwnedReview.has(event.kind) ||
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
    const name = observationKey(review.scope, review.event_id) + '.json'
    const target = path.join(pending, name)
    if (fs.existsSync(path.join(done, name))) {
      const existing = readObservationEvidence(base, name.slice(0, -5))
      if (existing.completion && existing.value.payload.review_context?.freshness_anchor === undefined) {
        if (stable(existing.value) !== stable(review)) throw new ObservationQueueError('Review identity conflicts with immutable evidence')
        // An exact completed legacy child needs no new work or freshness
        // authority. Revalidate ancestry only at the unfinished frontier.
        return null
      }
    }
    review.payload.review_context.freshness_anchor = reviewAnchor(base, event)
    if (Buffer.byteLength(JSON.stringify(review) + '\n', 'utf8') > MAX_OBSERVATION_BYTES) throw new ObservationQueueError('Review observation exceeds size limit')
    if (!fs.existsSync(target) && !fs.existsSync(path.join(done, name))) {
      try { publish(target, review) } catch (error) { if (error.code !== 'EEXIST') throw error }
    }
    const read = readObservationEvidence(base, name.slice(0, -5))
    validateObservation(read.value)
    if (stable(read.value) !== stable(review)) {
      const legacy = structuredClone(review)
      delete legacy.payload.review_context.freshness_anchor
      if (read.value.payload.review_context?.freshness_anchor !== undefined || stable(read.value) !== stable(legacy)) {
        throw new ObservationQueueError('Review identity conflicts with immutable evidence')
      }
    }
    if (read.completion) return null
    if ((leaseDb.prepare('SELECT next_after FROM retry WHERE event_key=?').get(name)?.next_after || 0) > now()) return null
    return { event: read.value, name, sha256: read.sha256, raw: read.raw }
  }

  async function processEntry({ event, name, sha256, raw }) {
    let outcome
    try { outcome = await dispatch(event) } catch (error) {
      failed(name)
      log('warn', 'observation_dispatch_failed:' + name + ':' + error.name)
      return 0
    }
    // Errors before a real verdict are never consumed. Unknown transport
    // outcomes ARE terminal evidence and may not be retried automatically.
    if (!plain(outcome) || outcome.completed !== true) {
      failed(name)
      log('warn', 'observation_without_terminal_decision:' + name)
      return 0
    }
    let current
    try { current = readRegular(path.join(pending, name), MAX_OBSERVATION_BYTES) } catch (error) {
      if (error.code !== 'ENOENT') throw error
      // A cleanup during the turn cannot discard the already validated
      // bytes captured before dispatch or force the completed turn to replay.
      current = { sha256: hash(raw) }
    }
    if (current.sha256 !== sha256) throw new ObservationQueueError('Observation changed before completion')
    const local = outcome.status === 'no_decision'
    const marker = { version: local ? 3 : 2, event_raw: raw.toString('utf8'), event_id: event.event_id, scope: event.scope,
      event_sha256: sha256, completed_at: local ? outcome.completed_at : now(), status: outcome.status,
      ...(local ? { reason: outcome.reason, local_outcome_id: outcome.local_outcome_id,
        observation_revision: outcome.observation_revision, native_turn_completed: outcome.native_turn_completed }
        : { decision_id: outcome.decision_id }) }
    // null/undefined in a dispatch receipt means the LLM did not schedule a
    // review. A persisted marker with an explicit null timestamp is invalid.
    if (!local && outcome.next_review_at !== undefined && outcome.next_review_at !== null) {
      marker.next_review_at = outcome.next_review_at
    }
    try {
      if (local && ('decision_id' in outcome || 'next_review_at' in outcome || 'verdict' in outcome || 'ticket' in outcome)) {
        throw new ObservationQueueError('A local no-decision outcome cannot carry a model verdict or review')
      }
      validateCompletion(marker, event, sha256, raw)
    } catch (error) {
      parkInvalidTerminal(name)
      log('error', 'observation_invalid_terminal_parked:' + name + ':' + error.name)
      return 0
    }
    publish(path.join(done, name), marker)
    leaseDb.prepare('DELETE FROM retry WHERE event_key=?').run(name)
    await acknowledgeTerminal(event, marker, name, sha256, hash(JSON.stringify(marker) + '\n'))
    return 1
  }

  async function drain() {
    if (busy) {
      // A poll is also a rescan signal while other scopes await their models.
      rescanRequested = true
      wakeDrain?.()
      return { processed: 0, status: 'not_owner_or_busy' }
    }
    if (!release) return { processed: 0, status: 'not_owner_or_busy' }
    busy = true
    rescanRequested = true
    let processed = 0, launched = 0, failure = null
    const backlog = new Map(), inFlight = new Map(), activeScopes = new Set(), terminal = new Set()
    const wake = () => { wakeDrain?.(); wakeDrain = null }
    const launch = entry => {
      const scope = entry.event.scope
      activeScopes.add(scope)
      backlog.delete(entry.name)
      const task = processEntry(entry).then(count => {
        processed += count
        if (count) terminal.add(entry.name)
        else rescanRequested = true
      }, error => { failure ||= error }).finally(() => {
        inFlight.delete(entry.name)
        activeScopes.delete(scope)
        wake()
      })
      inFlight.set(entry.name, task)
    }
    try {
      while (!stopped) {
        if (rescanRequested) {
          rescanRequested = false
          backlog.clear()
          ordinaryDirectories(pending, true)
          ordinaryDirectories(done)
          const names = [...new Set([...fs.readdirSync(pending), ...fs.readdirSync(done)])].filter((name) => /^[0-9a-f]{64}\.json$/.test(name))
          for (const [index, name] of names.entries()) {
            // Awaiting already-resolved acknowledgements only yields microtasks.
            // Let timers, input and provider I/O run even with a large done history.
            if (index > 0 && index % 32 === 0) {
              await new Promise(resolve => setImmediate(resolve))
              if (stopped) break
            }
            try {
              if (inFlight.has(name)) continue
              const read = readObservationEvidence(base, name.slice(0, -5))
              const event = validateObservation(read.value)
              if (name !== observationKey(event.scope, event.event_id) + '.json') throw new ObservationQueueError('Observation filename/identity mismatch')
              if (read.completion) {
                const completed = read.completion
                // A crash after durable completion but before core acknowledgement
                // is recovered here, without running the model or sending again.
                await acknowledgeTerminal(event, completed, name, read.sha256, read.completionSha256)
                const review = scheduleReview(event, completed)
                if (review) backlog.set(review.name, review)
                invalidEvidence.delete(name)
                continue
              }
              // Permanently parked uncertain terminal claims remain evidence, not
              // an executable head that freezes every later event in this scope.
              if (leaseDb.prepare('SELECT next_after FROM retry WHERE event_key=?').get(name)?.next_after === Number.MAX_SAFE_INTEGER) continue
              backlog.set(name, { event, name, sha256: read.sha256, raw: read.raw })
              invalidEvidence.delete(name)
            } catch (error) {
              if (!invalidEvidence.has(name)) log('error', 'observation_invalid:' + name + ':' + error.name)
              invalidEvidence.add(name)
            }
          }
        }
        if (failure) throw failure
        const ordered = [...backlog.values()].sort((a, b) => a.event.created_at - b.event.created_at || a.name.localeCompare(b.name))
        const heads = new Set()
        let scheduled = false
        for (const [index, entry] of ordered.entries()) {
          if (index > 0 && index % 32 === 0) await new Promise(resolve => setImmediate(resolve))
          if (stopped || failure || inFlight.size >= MAX_CONCURRENT_SCOPES) break
          const scope = entry.event.scope
          // Finite backoff holds this scope's oldest executable entry; unrelated
          // scopes remain eligible. Only one head per scope can start in a pass.
          // A scope completing during a yield cannot let a later entry overtake.
          if (heads.has(scope)) continue
          heads.add(scope)
          if (activeScopes.has(scope) || terminal.has(entry.name)) continue
          if ((leaseDb.prepare('SELECT next_after FROM retry WHERE event_key=?').get(entry.name)?.next_after || 0) > now()) continue
          if (launched > 0 && launched % 32 === 0) {
            await new Promise(resolve => setImmediate(resolve))
            if (stopped || failure) break
          }
          launched += 1
          launch(entry)
          scheduled = true
        }
        if (failure) throw failure
        if (stopped) break
        if (!inFlight.size) {
          if (scheduled || rescanRequested) continue
          break
        }
        if (rescanRequested) continue
        // Completion frees one scope; polling requests a new immutable scan.
        // Neither wait holds up a different scope's live native inbox.
        await new Promise(resolve => { wakeDrain = resolve })
      }
    } finally {
      wakeDrain = null
      // Even a scan/ack failure must not release the unique lease while another
      // scope may still be producing a terminal receipt or uncertain delivery.
      await Promise.allSettled([...inFlight.values()])
      busy = false
      if (stopped && release) { release(); release = null }
    }
    if (failure) throw failure
    return { processed, status: 'drained' }
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
      wakeDrain?.()
      if (timer) { clearInterval(timer); timer = null }
      // Retain the ownership lock until any in-flight decision settles.
      if (!busy && release) { release(); release = null }
    },
  }
}

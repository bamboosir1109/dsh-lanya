/** Immutable evidence inbox for the main chat LLM. No model or QQ transport is
 * owned here. dispatch(event) resolves ONLY after that decision turn settles.
 * A failed dispatch leaves evidence pending; a sent/unknown result is retained
 * durably instead of being blindly replayed. One pump owns a bot/role inbox.
 */
import fs from 'node:fs'
import path from 'node:path'
import { createHash, randomUUID } from 'node:crypto'
import { performance } from 'node:perf_hooks'
import { Worker, isMainThread, parentPort, workerData } from 'node:worker_threads'
import { DatabaseSync } from 'node:sqlite'

export class ObservationQueueError extends Error {
  constructor(message) { super(message); this.name = 'ObservationQueueError' }
}
export class ObservationForegroundBusy extends ObservationQueueError {
  constructor(reason = 'foreground_busy') {
    super('Observation could not be safely dispatched: ' + reason)
    this.name = 'ObservationForegroundBusy'
  }
}
class MissingLegacyObservation extends ObservationQueueError {}
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
    const activity = event.kind === 'activity.opportunity' && ['mind', 'main-review'].includes(event.producer)
    const superseded = activity && marker.reason === 'superseded_live_input' && marker.native_turn_completed === false
    if (Object.keys(marker).sort().join(',') !== fields.sort().join(',') || marker.status !== 'no_decision' ||
        !((marker.reason === 'model_completed_without_decision' && marker.native_turn_completed === true) ||
          (marker.reason === 'native_admission_refused' && marker.native_turn_completed === false) || restClosure || superseded) ||
        !(restClosure || activity ||
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
    let info
    try { info = fs.lstatSync(current) } catch (error) {
      if (error.code !== 'ENOENT') throw error
      if (!create) continue
      try { fs.mkdirSync(current) } catch (error) { if (error.code !== 'EEXIST') throw error }
      info = fs.lstatSync(current)
    }
    if (!info.isDirectory() || info.isSymbolicLink()) throw new ObservationQueueError('Inbox parents must be ordinary directories')
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
    if (plain(marker) && marker.version === 1 && typeof marker.event_raw !== 'string') {
      throw new MissingLegacyObservation('Missing original requires an exact terminal original')
    }
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
  if (depth >= 128 || seen.has(key)) throw new ObservationQueueError('Review freshness ancestry is not bounded and acyclic')
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

function buildReviewCandidate(base, event, completed, at) {
  const pending = path.join(base, 'pending'), done = path.join(base, 'done')
    if (['unknown', 'no_decision'].includes(completed.status) || producerOwnedReview.has(event.kind) ||
        !('next_review_at' in completed) || completed.next_review_at > at) return null
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
    const completedChild = fs.existsSync(path.join(done, name)) ? readObservationEvidence(base, name.slice(0, -5)) : null
    if (completedChild?.completion && completedChild.value.payload.review_context?.freshness_anchor === undefined) {
      if (stable(completedChild.value) !== stable(review)) throw new ObservationQueueError('Review identity conflicts with immutable evidence')
      // An exact completed legacy child needs no new work or freshness
      // authority. Revalidate ancestry only at the unfinished frontier.
      return null
    }
    review.payload.review_context.freshness_anchor = reviewAnchor(base, event)
    if (Buffer.byteLength(JSON.stringify(review) + '\n', 'utf8') > MAX_OBSERVATION_BYTES) throw new ObservationQueueError('Review observation exceeds size limit')
    if (!completedChild?.completion && !fs.existsSync(target) && !fs.existsSync(path.join(done, name))) {
      try { publish(target, review) } catch (error) { if (error.code !== 'EEXIST') throw error }
    }
    // Reuse only a verified terminal child. An executable child is read
    // after publication and independently verified again before dispatch.
    const read = completedChild?.completion ? completedChild : readObservationEvidence(base, name.slice(0, -5))
    validateObservation(read.value)
    if (stable(read.value) !== stable(review)) {
      const legacy = structuredClone(review)
      delete legacy.payload.review_context.freshness_anchor
      if (read.value.payload.review_context?.freshness_anchor !== undefined || stable(read.value) !== stable(legacy)) {
        throw new ObservationQueueError('Review identity conflicts with immutable evidence')
      }
    }
    if (read.completion) return null
    return { event: read.value, name, sha256: read.sha256, raw: read.raw }
  }


const SCANNER_PROTOCOL = 'dsh-observation-file-scanner/v1'
const RECONCILE_MS = 30_000
const MAX_RECONCILE_MS = 10 * 60_000
const evidenceName = name => typeof name === 'string' && /^[a-f0-9]{64}\.json$/.test(name)

// This object runs in a worker in production. Its cache is a discovery index:
// a hit only omits inert history, never authorizes execution or acknowledges it.
// Every executable candidate and review is read and validated again.
function fileScanner(directory, incremental = true) {
  const base = path.resolve(directory), pending = path.join(base, 'pending'), done = path.join(base, 'done')
  const history = new Map(), missingOriginals = new Map(), versions = new Map(), reads = new Map(), watchers = []
  let generation = 0, fullAt = null, fullStartedAt = null, reconcileAfter = RECONCILE_MS, reconcile = true, directories = null
  const metrics = { reads: 0, skipped: 0, selected: 0, entries: 0, full: true }
  const dirty = filename => {
    if (filename === null || filename === undefined) { reconcile = true; return }
    const name = String(filename)
    if (evidenceName(name)) versions.set(name, ++generation)
  }
  const directoryIdentity = () => [base, pending, done].map(directory => {
    ordinaryDirectories(directory)
    const info = fs.lstatSync(directory, { bigint: true })
    return [String(info.dev), String(info.ino)]
  })
  return {
    start() {
      ordinaryDirectories(pending, true); ordinaryDirectories(done, true)
      directories = stable(directoryIdentity())
      if (incremental) for (const directory of [pending, done]) {
        try {
          const watcher = fs.watch(directory, (_type, name) => dirty(name))
          watcher.on('error', () => { reconcile = true })
          watcher.unref?.(); watchers.push(watcher)
        } catch { reconcile = true }
      }
      return { ready: true }
    },
    list({ full, at }) {
      if (!Number.isSafeInteger(at) || at < 0) throw new ObservationQueueError('Invalid scanner timestamp')
      ordinaryDirectories(pending, true); ordinaryDirectories(done, true)
      const identity = stable(directoryIdentity())
      if (identity !== directories) { history.clear(); missingOriginals.clear(); reads.clear(); reconcile = true; directories = identity }
      const pendingNames = new Set(fs.readdirSync(pending)), doneNames = new Set(fs.readdirSync(done))
      const all = [...new Set([...pendingNames, ...doneNames])].filter(evidenceName)
      const names = new Set(all)
      for (const name of history.keys()) if (!names.has(name)) history.delete(name)
      for (const name of missingOriginals.keys()) if (!doneNames.has(name) || pendingNames.has(name)) missingOriginals.delete(name)
      for (const name of versions.keys()) if (!names.has(name)) versions.delete(name)
      reads.clear()
      const audit = !incremental || full || reconcile || fullAt === null || at < fullAt || at - fullAt >= reconcileAfter
      if (audit) { reconcile = false; fullStartedAt = at }
      const selected = audit ? all : all.filter(name => {
        if (missingOriginals.get(name) === (versions.get(name) || 0)) return false
        const entry = history.get(name)
        return !entry || !doneNames.has(name) || entry.version !== (versions.get(name) || 0) || entry.nextAt <= at
      })
      Object.assign(metrics, { skipped: all.length - selected.length, selected: selected.length, entries: all.length, full: audit })
      return selected
    },
    finishScan({ at }) {
      if (!Number.isSafeInteger(at) || at < 0) throw new ObservationQueueError('Invalid scan completion timestamp')
      if (metrics.full) {
        fullAt = at
        reconcileAfter = Math.min(MAX_RECONCILE_MS, Math.max(RECONCILE_MS, (at - fullStartedAt) * 4))
      }
    },
    read({ key }) {
      const name = key + '.json', version = versions.get(name) || 0
      metrics.reads++
      let read
      try { read = readObservationEvidence(base, key) }
      catch (error) {
        // A legacy completion without its original cannot be acknowledged
        // or used to derive work. Keep reporting it and revisit on change or
        // full audit without reading it on every poll.
        if (error instanceof MissingLegacyObservation) missingOriginals.set(name, version)
        throw error
      }
      missingOriginals.delete(name)
      if (!read.completion && history.has(name)) {
        throw new ObservationQueueError('Previously terminal evidence lost its completion; automatic replay refused')
      }
      if (read.completion) reads.set(key, { read, version })
      // Only one terminal read is awaiting its parent-side acknowledgement.
      if (reads.size > 16) throw new ObservationQueueError('Scanner read window exceeded')
      return read
    },
    review({ key, sha256, completionSha256, at, acknowledged }) {
      const previous = reads.get(key)
      reads.delete(key)
      if (!previous || previous.read.sha256 !== sha256 || previous.read.completionSha256 !== completionSha256) {
        throw new ObservationQueueError('Review does not match this scanner read')
      }
      const current = readObservationEvidence(base, key); metrics.reads++
      if (current.sha256 !== sha256 || current.completionSha256 !== completionSha256 || !current.completion) {
        throw new ObservationQueueError('Evidence changed during acknowledgement')
      }
      const event = current.value, completion = current.completion
      let result
      try { result = buildReviewCandidate(base, event, completion, at) }
      catch (error) {
        // Recheck failed derivations at the next audit or on file change.
        // A verified terminal is never treated as executable work.
        if (acknowledged === true) history.set(key + '.json', { version: previous.version, nextAt: Infinity })
        throw error
      }
      if (acknowledged === true) {
        const future = !['unknown', 'no_decision'].includes(completion.status) && !producerOwnedReview.has(event.kind)
          && Number.isSafeInteger(completion.next_review_at) && completion.next_review_at > at
        history.set(key + '.json', { version: previous.version, nextAt: future ? completion.next_review_at : Infinity })
      }
      return result
    },
    verify({ key, sha256 }) {
      const current = readObservationEvidence(base, key); metrics.reads++
      if (current.completion || current.sha256 !== sha256) throw new ObservationQueueError('Executable evidence changed before dispatch')
      // An unfinished review must still have its real, current parent ancestry.
      if (queuedReview(current.value)) reviewAnchor(base, current.value)
      return { verified: true }
    },
    complete({ key, sha256, raw: text, marker }) {
      if (typeof text !== 'string' || Buffer.byteLength(text, 'utf8') > MAX_OBSERVATION_BYTES) {
        throw new ObservationQueueError('Completion original exceeds size limit')
      }
      const raw = Buffer.from(text, 'utf8'), event = validateObservation(JSON.parse(text))
      if (observationKey(event.scope, event.event_id) !== key || raw.length > MAX_OBSERVATION_BYTES || hash(raw) !== sha256) {
        throw new ObservationQueueError('Completion snapshot identity mismatch')
      }
      validateCompletion(marker, event, sha256, raw)
      ordinaryDirectories(pending); ordinaryDirectories(done)
      try {
        if (readRegular(path.join(pending, key + '.json'), MAX_OBSERVATION_BYTES).sha256 !== sha256) {
          throw new ObservationQueueError('Observation changed before completion')
        }
      } catch (error) { if (error.code !== 'ENOENT') throw error }
      publish(path.join(done, key + '.json'), marker)
      history.delete(key + '.json')
      return { sha256: hash(JSON.stringify(marker) + '\n') }
    },
    stats() { return { ...metrics, cached: history.size, watchers: watchers.length } },
    close() { for (const watcher of watchers) watcher.close(); history.clear(); missingOriginals.clear(); reads.clear(); versions.clear() },
  }
}

function inlineScanner(base) {
  const files = fileScanner(base, false)
  return { start: async () => files.start(), call: async (method, args) => files[method](args),
    close: () => { files.close(); return Promise.resolve() }, status: () => ({ mode: 'inline', ...files.stats() }) }
}

function backgroundScanner(base) {
  let worker = null, starting = null, terminating = Promise.resolve(), closed = false, serial = 0, starts = 0
  let lastError = null, metrics = {}, closePromise = null, rejectStart = null
  const pending = new Map()
  const fail = (error, instance) => {
    if (worker !== instance) return
    lastError = error.name || 'Error'; worker = null
    rejectStart?.(error); rejectStart = null
    for (const entry of pending.values()) { clearTimeout(entry.timer); entry.reject(error) }
    pending.clear()
    terminating = instance.terminate().catch(() => {})
  }
  const start = async () => {
    if (closed) throw new ObservationQueueError('Observation scanner stopped')
    if (worker && !starting) return
    if (starting) return starting
    starting = (async () => {
      await terminating
      if (closed) throw new ObservationQueueError('Observation scanner stopped')
      const instance = new Worker(new URL(import.meta.url), { workerData: { protocol: SCANNER_PROTOCOL, directory: base },
        execArgv: [], env: {}, stdout: true, stderr: true,
        resourceLimits: { maxOldGenerationSizeMb: 128, maxYoungGenerationSizeMb: 16 } })
      worker = instance; starts++
      // All business errors use the typed reply/error channel. Drain warning
      // pipes so a worker cannot be blocked by its own diagnostic output.
      instance.stdout.resume(); instance.stderr.resume()
      await new Promise((resolve, reject) => {
        rejectStart = reject
        const timer = setTimeout(() => { const error = new ObservationQueueError('Scanner startup timeout'); fail(error, instance); reject(error) }, 10000)
        instance.on('message', message => {
          if (worker !== instance || !message || typeof message !== 'object') return
          if (message.ready === SCANNER_PROTOCOL) { clearTimeout(timer); rejectStart = null; instance.unref(); resolve(); return }
          const entry = pending.get(message.id)
          if (!entry) return
          pending.delete(message.id); clearTimeout(entry.timer)
          if (!pending.size) instance.unref()
          if (message.error) {
            const error = new ObservationQueueError(message.error.message)
            if (message.error.code) error.code = message.error.code
            entry.reject(error)
          } else { metrics = message.stats || metrics; entry.resolve(message.value) }
        })
        instance.on('error', error => { clearTimeout(timer); fail(error, instance); reject(error) })
        instance.on('exit', () => {
          clearTimeout(timer)
          if (worker === instance) { const error = new ObservationQueueError('Scanner worker exited'); fail(error, instance); reject(error) }
        })
      })
    })()
    try { await starting } finally { starting = null }
  }
  return {
    start,
    async call(method, args) {
      await start()
      if (closed || !worker) throw new ObservationQueueError('Scanner unavailable')
      if (pending.size >= 16) throw new ObservationQueueError('Scanner request window exceeded')
      const instance = worker, id = ++serial
      instance.ref()
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => fail(new ObservationQueueError('Scanner request timeout'), instance), 15000)
        pending.set(id, { resolve, reject, timer })
        try { instance.postMessage({ id, method, args }) } catch (error) { fail(error, instance) }
      })
    },
    status() { return { mode: 'worker', thread_id: worker?.threadId || null, starts, pending: pending.size, last_error: lastError, ...metrics } },
    close() {
      if (closePromise) return closePromise
      closed = true
      if (worker) fail(new ObservationQueueError('Observation scanner stopped'), worker)
      closePromise = Promise.allSettled([starting, terminating]).then(() => {})
      return closePromise
    },
  }
}

// Explicit inline mode is for deterministic, isolated filesystem/clock tests.
// The installed adapter always uses the default worker; there is no silent
// fallback to the shared chat thread if worker initialization fails.
export function createInlineObservationPump(options) { return createObservationPump({ ...options, scanMode: 'inline' }) }

export function createObservationPump({ directory, dispatch, acknowledge = async () => {}, acknowledgeBatch, log = () => {}, now = Date.now,
  intervalMs = 2000, isOwnerAlive = ownerAlive, scanMode = 'worker' } = {}) {
  if (typeof directory !== 'string' || !directory.trim() || typeof dispatch !== 'function') throw new ObservationQueueError('directory and dispatch are required')
  if (!['worker', 'inline'].includes(scanMode)) throw new ObservationQueueError('Invalid observation scanner mode')
  if (acknowledgeBatch !== undefined && typeof acknowledgeBatch !== 'function') throw new ObservationQueueError('Invalid acknowledgement batch callback')
  const base = path.resolve(directory)
  const pending = path.join(base, 'pending')
  const done = path.join(base, 'done')
  const leaseFile = path.join(base, 'consumer.sqlite')
  let timer, busy = false, stopped = true, release = null, leaseDb = null
  let scanInProgress = false
  let rescanRequested = false, wakeDrain = null
  let fullRescanRequested = true, scanner = null, drainFinished = Promise.resolve(), finishDrain, stopping = null
  const invalidEvidence = new Set()
  // A restart retries acknowledgement from durable evidence. Within this pump,
  // unchanged terminal bytes need not reacquire the policy lock every poll.
  // This cache never authorizes dispatch, hides changed evidence or marks sends.
  const acknowledgedTerminals = new Map()
  // A scanner restart after a settled model turn retries only its terminal
  // write. Never invoke that turn again merely because file RPC was lost.
  const unsettledWrites = new Map()
  let lastReportAt = 0

  function reportScanner(phase, error = null) {
    if (!leaseDb) return
    const at = Date.now()
    const value = { version: 1, phase, at, owner_pid: process.pid, active_scopes: inFlightCount,
      invalid_evidence: invalidEvidence.size, error, ...scanner?.status() }
    try {
      leaseDb.prepare('INSERT OR REPLACE INTO scanner_health(id,pid,updated_at,status_json) VALUES (1,?,?,?)')
        .run(process.pid, at, JSON.stringify(value))
      lastReportAt = performance.now()
    } catch (failure) { log('warn', 'observation_scanner_health_failed:' + failure.name) }
  }
  let inFlightCount = 0

  async function acknowledgeTerminal(event, marker, name, eventHash, markerHash) {
    const fingerprint = eventHash + ':' + markerHash
    if (acknowledgedTerminals.get(name) === fingerprint) return true
    return rememberAcknowledgement(name, fingerprint, await acknowledge(event, marker))
  }

  function rememberAcknowledgement(name, fingerprint, result) {
    // Legacy callbacks resolve void. A missing pin is the idempotent result of
    // an earlier successful ack (including a crash before this cache existed).
    // Other negative outcomes remain retryable and never enter this cache.
    if (result === undefined || (plain(result) && (result.acknowledged === true ||
        (result.acknowledged === false && result.reason === 'unknown_observation')))) {
      acknowledgedTerminals.set(name, fingerprint)
      return true
    }
    return false
  }

  async function acknowledgeScanned(records) {
    const outcomes = new Map()
    const missing = records.filter(record => {
      if (acknowledgedTerminals.get(record.name) !== record.fingerprint) return true
      outcomes.set(record.name, true)
      return false
    })
    if (acknowledgeBatch && missing.length > 1) {
      try {
        const results = await acknowledgeBatch(missing.map(({ event, read }) => ({ event, marker: read.completion })))
        if (!Array.isArray(results) || results.length !== missing.length) throw new ObservationQueueError('Incomplete acknowledgement batch')
        for (const [index, { name, fingerprint }] of missing.entries()) {
          outcomes.set(name, rememberAcknowledgement(name, fingerprint, results[index]))
        }
      } catch { /* Recheck individual evidence if the bounded transaction failed. */ }
    }
    return outcomes
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
      db.exec('CREATE TABLE IF NOT EXISTS scanner_health (id INTEGER PRIMARY KEY CHECK(id=1), pid INTEGER NOT NULL, updated_at INTEGER NOT NULL, status_json TEXT NOT NULL)')
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

  function deferForeground(key) {
    // No native turn was queued. Keep any previous failure count and resume
    // checking at the regular cadence once this scope is available.
    leaseDb.prepare('INSERT INTO retry(event_key,attempts,next_after) VALUES (?,0,?) '
      + 'ON CONFLICT(event_key) DO UPDATE SET next_after=excluded.next_after').run(key, now() + intervalMs)
  }

  function parkInvalidTerminal(key) {
    // A claimed terminal result with invalid metadata may already have sent.
    // Keep evidence and durably require repair, never call the model again.
    leaseDb.prepare('INSERT OR REPLACE INTO retry(event_key,attempts,next_after) VALUES (?,?,?)').run(key, 1, Number.MAX_SAFE_INTEGER)
  }

  async function scheduleReview(event, completed, read, acknowledged) {
    const review = await scanner.call('review', { key: observationKey(event.scope, event.event_id),
      sha256: read.sha256, completionSha256: read.completionSha256, at: now(), acknowledged })
    if (!review) return null
    if ((leaseDb.prepare('SELECT next_after FROM retry WHERE event_key=?').get(review.name)?.next_after || 0) > now()) return null
    return { ...review, raw: Buffer.from(review.raw) }
  }

  async function processEntry({ event, name, sha256, raw }) {
    const retained = unsettledWrites.get(name)
    if (retained) {
      if (retained.sha256 !== sha256) throw new ObservationQueueError('Settled observation bytes changed')
      const receipt = await scanner.call('complete', retained.args)
      await acknowledgeTerminal(event, retained.args.marker, name, sha256, receipt.sha256)
      unsettledWrites.delete(name)
      leaseDb.prepare('DELETE FROM retry WHERE event_key=?').run(name)
      return 1
    }
    let outcome
    try {
      await scanner.call('verify', { key: name.slice(0, -5), sha256 })
      if (stopped) return 0
      outcome = await dispatch(event)
    } catch (error) {
      if (error instanceof ObservationForegroundBusy) {
        deferForeground(name)
        return 0
      }
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
    const args = { key: name.slice(0, -5), sha256, raw: raw.toString('utf8'), marker }
    unsettledWrites.set(name, { sha256, args })
    const receipt = await scanner.call('complete', args)
    leaseDb.prepare('DELETE FROM retry WHERE event_key=?').run(name)
    await acknowledgeTerminal(event, marker, name, sha256, receipt.sha256)
    unsettledWrites.delete(name)
    return 1
  }

  async function drain(full = true) {
    if (busy) {
      // A poll is also a rescan signal while other scopes await their models.
      rescanRequested = true
      fullRescanRequested ||= full
      wakeDrain?.()
      return { processed: 0, status: 'not_owner_or_busy' }
    }
    if (!release) return { processed: 0, status: 'not_owner_or_busy' }
    busy = true
    rescanRequested = true
    fullRescanRequested ||= full
    drainFinished = new Promise(resolve => { finishDrain = resolve })
    let processed = 0, launched = 0, failure = null
    let lastYieldAt = performance.now()
    const backlog = new Map(), inFlight = new Map(), activeScopes = new Set(), terminal = new Set()
    const wake = () => { wakeDrain?.(); wakeDrain = null }
    const launch = entry => {
      const scope = entry.event.scope
      activeScopes.add(scope)
      inFlightCount = activeScopes.size
      backlog.delete(entry.name)
      const task = processEntry(entry).then(count => {
        processed += count
        if (count) terminal.add(entry.name)
        else rescanRequested = true
      }, error => { failure ||= error }).finally(() => {
        inFlight.delete(entry.name)
        activeScopes.delete(scope)
        inFlightCount = activeScopes.size
        wake()
      })
      inFlight.set(entry.name, task)
    }
    try {
      while (!stopped) {
        if (rescanRequested) {
          rescanRequested = false
          backlog.clear()
          scanInProgress = true
          reportScanner('scanning')
          try {
            const terminals = []
            const flushTerminals = async () => {
              if (!terminals.length) return
              const records = terminals.splice(0)
              const outcomes = await acknowledgeScanned(records)
              for (const { event, name, read } of records) {
                if (stopped) break
                try {
                  const completed = read.completion
                  const acknowledged = outcomes.has(name) ? outcomes.get(name)
                    : await acknowledgeTerminal(event, completed, name, read.sha256, read.completionSha256)
                  if (acknowledged) unsettledWrites.delete(name)
                  const review = await scheduleReview(event, completed, read, acknowledged)
                  if (review) backlog.set(review.name, review)
                  invalidEvidence.delete(name)
                } catch (error) {
                  if (!invalidEvidence.has(name)) log('error', 'observation_invalid:' + name + ':' + error.name)
                  invalidEvidence.add(name)
                }
              }
            }
            const audit = fullRescanRequested
            fullRescanRequested = false
            const names = await scanner.call('list', { full: audit, at: now() })
            for (const [index, name] of names.entries()) {
              if (performance.now() - lastReportAt >= 1000) reportScanner('scanning')
              // Check real monotonic work time: one complete evidence read stays
              // synchronous, but cheap or costly histories both yield to I/O.
              if (index > 0 && performance.now() - lastYieldAt >= 10) {
                await new Promise(resolve => setImmediate(resolve))
                lastYieldAt = performance.now()
                if (stopped) break
              }
              try {
                if (inFlight.has(name)) continue
                const read = await scanner.call('read', { key: name.slice(0, -5) })
                if (stopped) break
                read.raw = Buffer.from(read.raw)
                const event = validateObservation(read.value)
                if (name !== observationKey(event.scope, event.event_id) + '.json') throw new ObservationQueueError('Observation filename/identity mismatch')
                if (read.completion) {
                  // A crash after durable completion but before core acknowledgement
                  // is recovered here, without running the model or sending again.
                  terminals.push({ event, name, read, fingerprint: read.sha256 + ':' + read.completionSha256 })
                  if (terminals.length >= (acknowledgeBatch ? 8 : 1)) await flushTerminals()
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
            if (!stopped) {
              await flushTerminals()
              await scanner.call('finishScan', { at: now() })
            }
          } finally { scanInProgress = false; reportScanner(inFlight.size ? 'waiting_turns' : 'ready') }
        }
        if (failure) throw failure
        const ordered = [...backlog.values()].sort((a, b) => a.event.created_at - b.event.created_at || a.name.localeCompare(b.name))
        const heads = new Set()
        let scheduled = false
        for (const [index, entry] of ordered.entries()) {
          if (performance.now() - lastYieldAt >= 10) {
            await new Promise(resolve => setImmediate(resolve))
            lastYieldAt = performance.now()
          }
          if (stopped || failure || inFlight.size >= MAX_CONCURRENT_SCOPES) break
          const scope = entry.event.scope
          // Finite backoff holds this scope's oldest executable entry; unrelated
          // scopes remain eligible. Only one head per scope can start in a pass.
          // A scope completing during a yield cannot let a later entry overtake.
          if (heads.has(scope)) continue
          heads.add(scope)
          if (activeScopes.has(scope) || terminal.has(entry.name)) continue
          if ((leaseDb.prepare('SELECT next_after FROM retry WHERE event_key=?').get(entry.name)?.next_after || 0) > now()) continue
          if (launched > 0 && performance.now() - lastYieldAt >= 10) {
            await new Promise(resolve => setImmediate(resolve))
            lastYieldAt = performance.now()
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
        reportScanner('waiting_turns')
        // Completion frees one scope; polling requests a new immutable scan.
        // Neither wait holds up a different scope's live native inbox.
        await new Promise(resolve => { wakeDrain = resolve })
      }
    } catch (error) {
      reportScanner('error', error.name)
      throw error
    } finally {
      wakeDrain = null
      // Even a scan/ack failure must not release the unique lease while another
      // scope may still be producing a terminal receipt or uncertain delivery.
      await Promise.allSettled([...inFlight.values()])
      busy = false
      if (stopped) {
        await scanner?.close()
        reportScanner('stopped')
        if (release) { release(); release = null }
      }
      finishDrain?.(); finishDrain = null
    }
    if (failure) throw failure
    return { processed, status: 'drained' }
  }

  return {
    async start({ poll = true } = {}) {
      if (stopping) await stopping
      if (!stopped || busy) return false
      stopped = false
      fullRescanRequested = true
      scanner = scanMode === 'worker' ? backgroundScanner(base) : inlineScanner(base)
      try {
        await scanner.start()
        if (stopped) { await scanner.close(); return false }
        if (!takeClaim()) { stopped = true; await scanner.close(); return false }
      } catch (error) {
        const canceled = stopped
        stopped = true; await scanner.close()
        if (canceled) return false
        throw error
      }
      reportScanner('ready')
      if (poll) {
        const tick = () => {
          // A slow full scan must not schedule another full scan of itself.
          // Explicit drain() still requests a rescan, and polls while awaiting
          // in-flight turns still discover new work in other scopes.
          if (scanInProgress) return
          void drain(false).catch((error) => log('error', 'observation_pump_failed:' + error.name))
        }
        timer = setInterval(tick, intervalMs)
        timer.unref?.()
        tick()
      }
      return true
    },
    drain,
    status: () => ({ running: !stopped, scanning: scanInProgress, ...scanner?.status() }),
    stop() {
      stopped = true
      reportScanner('stopping')
      wakeDrain?.()
      if (timer) { clearInterval(timer); timer = null }
      // Retain the ownership lock until any in-flight decision settles.
      if (stopping) return stopping
      if (!busy && scanMode === 'inline') { scanner?.close(); if (release) { release(); release = null } }
      const finished = busy ? drainFinished : Promise.resolve().then(async () => {
        await scanner?.close()
        reportScanner('stopped')
        if (release) { release(); release = null }
      })
      stopping = finished.finally(() => { stopping = null })
      return stopping
    },
  }
}

if (!isMainThread && workerData?.protocol === SCANNER_PROTOCOL) {
  const files = fileScanner(workerData.directory)
  const methods = new Set(['list', 'read', 'review', 'verify', 'complete', 'finishScan'])
  files.start()
  parentPort.on('message', message => {
    if (!message || !Number.isSafeInteger(message.id) || !methods.has(message.method)) return
    try {
      const value = files[message.method](message.args)
      parentPort.postMessage({ id: message.id, value, stats: files.stats() })
    } catch (error) {
      parentPort.postMessage({ id: message.id, error: { message: String(error.message).slice(0, 4096), code: error.code } })
    }
  })
  parentPort.on('close', () => files.close())
  parentPort.postMessage({ ready: SCANNER_PROTOCOL })
}

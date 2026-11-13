/**
 * Conversation decision/egress primitives. Nothing is installed or intercepted
 * by importing this module. Callers supply trusted protocol metadata, obtain a
 * model verdict, and carry ONE ticket through every queued text/media/API slice.
 * A model's ordinary SILENT response means idle/defer, NOT persistent silence.
 * One stateFile belongs to ONE bot/role identity. Different bots must never
 * share it merely because their private/group IDs happen to be the same.
 */
import fs from 'node:fs'
import path from 'node:path'
import { createHash, randomUUID } from 'node:crypto'

const VERSION = 1
const KINDS = new Set(['text', 'media', 'forward', 'api', 'publication', 'tool_action'])
const VERDICTS = new Set(['reply', 'silence', 'resume', 'defer', 'idle', 'tool_action'])
const clone = (value) => JSON.parse(JSON.stringify(value))
const plain = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)
const identifier = (value) => typeof value === 'string' && value.length > 0 && value.length <= 256 && !/[\x00-\x1f]/.test(value)
const CONTACT_INTERVAL_MS = 60 * 60_000
const REST_REASONS = new Set(['autonomy_rest', 'observation_expired'])
const REPLY_REVIEW_AGE = 60_000
const REPLY_REVIEW_LIFETIME = 24 * 60 * 60_000
export const FRIEND_RECONNECT_WINDOW_MS = 3 * 60_000
const replyOwnerValid = owner => plain(owner) && ['cwd', 'preset', 'workspace_key', 'agent_key']
  .every(key => typeof owner[key] === 'string' && owner[key].length > 0 && owner[key].length <= 4096)
const replyOwnerSame = (a, b) => replyOwnerValid(a) && replyOwnerValid(b) &&
  ['cwd', 'preset', 'workspace_key', 'agent_key'].every(key => a[key] === b[key])
const replyReviewable = (scope, at) => {
  const event = scope?.current, input = event?.replyReview
  return Boolean(scope?.mode === 'open' && event?.origin === 'live' && input && !input.attempted &&
    at >= input.at && at - input.at <= REPLY_REVIEW_LIFETIME &&
    (!event.decided || event.ticket?.verdict === 'reply') &&
    // Even a suppressed/partial send attempt is not proof that nothing left
    // this turn. Never turn a timeout, negative ack or old reservation into a
    // second external operation. Only turns with NO egress entries qualify.
    !scope.deliveries.some(item => item.revision === scope.revision))
}

/** Owner-selected daily rest, independent of the host/browser timezone. */
export function autonomousRestState(at = Date.now()) {
  if (!Number.isSafeInteger(at) || at < 0 || at > 8_640_000_000_000_000 - 8 * 3600_000) {
    throw new PolicyStateError('A valid trusted epoch millisecond time is required')
  }
  const dayStart = Math.floor((at + 8 * 3600_000) / 86400_000) * 86400_000 - 8 * 3600_000
  const wake = dayStart + 8 * 3600_000
  return { timezone: 'Asia/Shanghai', resting: at < wake,
    current_day_wake_at: wake, next_wake_at: at < wake ? wake : wake + 86400_000 }
}

function latestLiveRevision(current) {
  return Math.max(current?.lastLiveRevision || 0,
    ...(current?.seen || []).filter(item => (item.origin || 'live') === 'live').map(item => item.revision))
}

function privateContactState(current, at, ticketId, autonomousChoice = false) {
  const contact = current?.proactiveContact
  if (contact?.ticketId === ticketId && ticketId !== undefined) return { allowed: true, reason: 'same_contact_turn' }
  // Existing deliveries have no trusted origin. Use their real time only as a
  // conservative initial interval; never relabel them as autonomous contacts.
  const previous = contact?.at ?? Math.max(0, ...(current?.deliveries || [])
    .filter(item => item.kind !== 'publication' && ['sent', 'unknown', 'reserved'].includes(item.status))
    .map(item => Number.isSafeInteger(item.at) && item.at >= 0 ? item.at : 0))
  const next = previous ? previous + CONTACT_INTERVAL_MS : null
  const waiting = Boolean(contact && latestLiveRevision(current) <= contact.lastLiveRevision)
  const reason = next !== null && at < next ? 'proactive_contact_interval'
    : waiting && !autonomousChoice ? 'proactive_contact_unanswered' : 'proactive_contact_available'
  return { allowed: reason === 'proactive_contact_available', reason,
    minimum_interval_minutes: 60, wait_for_live_reply: !autonomousChoice,
    contact_choice: autonomousChoice ? 'main_llm_no_daily_quota' : 'wait_for_live_reply',
    last_contact_at: previous || null, next_allowed_at: next, waiting_for_live_reply: waiting,
    history_basis: contact ? 'trusted_proactive_reservation' : 'legacy_delivery_time_only' }
}
const noDecisionId = (scope, eventId, revision) => createHash('sha256').update(['no-decision', scope, eventId, revision].join('\0')).digest('hex')
const localOutcomeValid = (value, scope, eventId, revision) => plain(value) &&
  Object.keys(value).sort().join(',') === 'completed_at,local_outcome_id,native_turn_completed,observation_revision,reason,status' &&
  value.status === 'no_decision' && ((value.reason === 'model_completed_without_decision' && value.native_turn_completed === true) ||
    ((['native_admission_refused', 'superseded_live_input'].includes(value.reason) || REST_REASONS.has(value.reason)) &&
      value.native_turn_completed === false)) &&
  Number.isSafeInteger(revision) && revision >= 1 && value.observation_revision === revision &&
  Number.isSafeInteger(value.completed_at) && value.completed_at >= 0 && value.local_outcome_id === noDecisionId(scope, eventId, revision)

export class PolicyStateError extends Error {
  constructor(message) { super(message); this.name = 'PolicyStateError' }
}

export function canonicalScope(value) {
  // Keep IDs as strings: Number() can silently change large protocol IDs.
  if (typeof value !== 'string' || !/^(private|group):[1-9][0-9]*$/.test(value)) {
    throw new PolicyStateError('Expected canonical private:<peer> or group:<id> scope')
  }
  return value
}

function validateState(state) {
  if (!plain(state) || state.version !== VERSION || !plain(state.scopes)) {
    throw new PolicyStateError('Unsupported or corrupt decision state')
  }
  for (const [key, value] of Object.entries(state.scopes)) {
    canonicalScope(key)
    if (!plain(value) || !Number.isSafeInteger(value.revision) || value.revision < 0 ||
        !['open', 'silent'].includes(value.mode) || !Array.isArray(value.seen) ||
        !Array.isArray(value.deliveries) || !(value.current === null || plain(value.current))) {
      throw new PolicyStateError('Corrupt scope state')
    }
    for (const seen of value.seen) {
      if (!plain(seen) || !identifier(seen.messageId) || !Number.isSafeInteger(seen.revision)) {
        throw new PolicyStateError('Corrupt inbound dedup state')
      }
    }
    if (value.lastLiveRevision !== undefined && (!Number.isSafeInteger(value.lastLiveRevision) ||
        value.lastLiveRevision < 0 || value.lastLiveRevision > value.revision)) throw new PolicyStateError('Corrupt live receipt revision')
    if (value.proactiveContact !== undefined && (!plain(value.proactiveContact) || !key.startsWith('private:') ||
        Object.keys(value.proactiveContact).sort().join(',') !== 'at,lastLiveRevision,ticketId' ||
        !identifier(value.proactiveContact.ticketId) || !Number.isSafeInteger(value.proactiveContact.at) || value.proactiveContact.at < 0 ||
        !Number.isSafeInteger(value.proactiveContact.lastLiveRevision) || value.proactiveContact.lastLiveRevision < 0)) {
      throw new PolicyStateError('Corrupt proactive contact state')
    }
    for (const delivery of value.deliveries) {
      if (!plain(delivery) || !identifier(delivery.key) ||
          !['reserved', 'sent', 'unknown', 'suppressed'].includes(delivery.status) ||
          (delivery.kind !== undefined && delivery.kind !== 'publication') ||
          (delivery.meme !== undefined && (delivery.meme !== true || !identifier(delivery.ticketId)))) {
        throw new PolicyStateError('Corrupt delivery dedup state')
      }
    }
    if (value.observations !== undefined && (!Array.isArray(value.observations) || value.observations.some((entry) =>
      !plain(entry) || !identifier(entry.eventId) || !Number.isSafeInteger(entry.revision) ||
      (entry.decision !== undefined && (!plain(entry.decision) || !VERDICTS.has(entry.decision.verdict) ||
        !identifier(entry.decision.decisionId) || !['open', 'silent'].includes(entry.decision.mode))) ||
      (entry.localOutcome !== undefined && (entry.decision !== undefined || !localOutcomeValid(entry.localOutcome, key, entry.eventId, entry.revision)))))) {
      throw new PolicyStateError('Corrupt pinned observation state')
    }
    if (value.observations?.some(entry => entry.localOnly !== undefined &&
        (entry.localOnly !== true || entry.decision !== undefined || !REST_REASONS.has(entry.localOutcome?.reason)))) {
      throw new PolicyStateError('Corrupt local rest outcome')
    }
    if (value.current !== null) {
      const current = value.current
      const trustedOrigin = (current.origin === 'live' && typeof current.sender === 'string' && /^[1-9][0-9]*$/.test(current.sender)) ||
        (current.origin === 'observation' && current.sender === null && current.controlAllowed === false)
      if (!identifier(current.messageId) || !trustedOrigin ||
          typeof current.controlAllowed !== 'boolean' || typeof current.quoted !== 'boolean' ||
          typeof current.decided !== 'boolean') {
        throw new PolicyStateError('Corrupt current inbound state')
      }
      if (current.replyReview !== undefined && (!plain(current.replyReview) || current.origin !== 'live' ||
          !replyOwnerValid(current.replyReview.owner) || typeof current.replyReview.text !== 'string' ||
          !current.replyReview.text.trim() || current.replyReview.text.length > 32_000 ||
          !Number.isSafeInteger(current.replyReview.at) || current.replyReview.at < 0 ||
          typeof current.replyReview.attempted !== 'boolean')) throw new PolicyStateError('Corrupt reply review evidence')
      if (current.ticket !== null && (!plain(current.ticket) || !identifier(current.ticket.id) ||
          !['reply', 'resume', 'tool_action'].includes(current.ticket.verdict) || current.decided !== true ||
          !Number.isFinite(current.ticket.expiresAt))) {
        throw new PolicyStateError('Corrupt decision ticket')
      }
    }
  }
  return state
}

function ownerAlive(pid) {
  try { process.kill(pid, 0); return true } catch (error) {
    // EPERM/unknown errors do not establish that the owner died.
    return error.code !== 'ESRCH'
  }
}

function safeUnlinkOwned(file, contents) {
  try { if (fs.readFileSync(file, 'utf8') === contents) fs.unlinkSync(file) } catch (error) {
    if (error.code !== 'ENOENT') throw error
  }
}

function createOwnerCandidate(target, contents) {
  // Publish a complete owner with linkSync, never an empty wx-created lock.
  // An interruption before publication leaves only this unique inert file;
  // after publication the normal dead-owner recovery can identify the owner.
  const candidate = target + '.owner-' + process.pid + '-' + randomUUID()
  let fd, created = false
  try {
    fd = fs.openSync(candidate, 'wx', 0o600)
    created = true
    fs.writeFileSync(fd, contents)
    fs.fsyncSync(fd)
    fs.closeSync(fd); fd = undefined
    return candidate
  } catch (error) {
    const errors = [error]
    try { if (fd !== undefined) fs.closeSync(fd) } catch (closeError) { errors.push(closeError) }
    try { if (created) removeOwnerCandidate(candidate) } catch (cleanupError) { errors.push(cleanupError) }
    if (errors.length > 1) throw new AggregateError(errors, 'Owner candidate creation and cleanup failed')
    throw error
  }
}

function removeOwnerCandidate(candidate) {
  try { fs.unlinkSync(candidate) } catch (error) { if (error.code !== 'ENOENT') throw error }
}

/** Diagnose a lock and its exact recovery claim without changing policy JSON.
 *
 * const preview = repairPolicyLocks({ stateFile })
 * // STOP every writer of this bot/role, then inspect/repeat the preview.
 * repairPolicyLocks({ stateFile, apply: true, writersStopped: true,
 *   expectedFingerprint: preview.fingerprint })
 *
 * writersStopped is an operator precondition, NOT an automatic proof that no
 * process can start. There is no portable unlink/CAS primitive here: repair
 * must never run concurrently with writers. Automatic recovery deliberately
 * leaves an existing claim alone, even if its recorded owner is now dead.
 * Only preview-matched ordinary lock/claim files with confirmed dead owners
 * may be unlinked. No glob, recursive deletion, state reset or JSON rewrite.
 */
export function repairPolicyLocks({ stateFile, apply = false, writersStopped = false,
  expectedFingerprint, isOwnerAlive = ownerAlive } = {}) {
  if (typeof stateFile !== 'string' || !stateFile.trim()) throw new PolicyStateError('stateFile is required')
  const file = path.resolve(stateFile)
  const lock = file + '.lock'
  const hash = (value) => createHash('sha256').update(value).digest('hex')
  const identity = (info) => [info.dev, info.ino, info.mode, info.size, info.mtimeNs, info.ctimeNs].map(String)

  function inspect(fileName) {
    let info
    try { info = fs.lstatSync(fileName, { bigint: true }) } catch (error) {
      if (error.code === 'ENOENT') return null
      throw error
    }
    if (!info.isFile() || info.isSymbolicLink()) throw new PolicyStateError('Repair only accepts ordinary lock/claim files')
    const contents = fs.readFileSync(fileName)
    const after = fs.lstatSync(fileName, { bigint: true })
    if (JSON.stringify(identity(info)) !== JSON.stringify(identity(after))) throw new PolicyStateError('Lock changed during inspection')
    let owner
    try { owner = JSON.parse(contents.toString('utf8')) } catch { }
    const known = Number.isSafeInteger(owner?.pid) && owner.pid > 0 && identifier(owner.nonce)
    return { file: fileName, sha256: hash(contents), identity: identity(info),
      ownerPid: known ? owner.pid : null,
      ownerState: !known ? 'unknown' : isOwnerAlive(owner.pid) ? 'alive_or_unknown' : 'dead' }
  }

  function inventory() {
    const observedLock = inspect(lock)
    const files = []
    if (observedLock) {
      files.push(observedLock)
      const claim = inspect(lock + '.reclaim-' + observedLock.sha256)
      if (claim) files.push(claim)
    }
    return { files, fingerprint: hash(JSON.stringify(files)) }
  }

  const preview = inventory()
  const repairable = preview.files.length > 0 && preview.files.every((entry) => entry.ownerState === 'dead')
  const result = { status: !preview.files.length ? 'clear' : repairable ? 'repair_preview' : 'blocked',
    repairable, requiresStoppedWriters: true, ...preview }
  if (!apply) return result
  if (writersStopped !== true) throw new PolicyStateError('Repair requires every writer to be stopped by the operator')
  if (!repairable) throw new PolicyStateError('Repair refused: a lock owner is alive/unknown or no lock exists')
  if (expectedFingerprint !== preview.fingerprint) throw new PolicyStateError('Repair preview fingerprint mismatch')
  if (inventory().fingerprint !== preview.fingerprint) throw new PolicyStateError('Locks changed since repair preview')
  const removed = []
  // Remove the claim first. Interrupted stopped-writer repair leaves the dead
  // main lock recoverable by the normal algorithm; it never changes silence.
  for (const entry of [...preview.files].reverse()) {
    if (JSON.stringify(inspect(entry.file)) !== JSON.stringify(entry)) throw new PolicyStateError('Lock changed before repair')
    fs.unlinkSync(entry.file)
    removed.push(entry.file)
  }
  return { ...result, status: 'repaired', removed }
}

/** JSON storage contract: transact(fn) serializes a synchronous fn(state, commit).
 * commit() persists immediately while the lock remains held. This lets egress
 * reserve durably BEFORE synchronously initiating transport, without holding the
 * lock while waiting for a network acknowledgement. Injected stores must obey
 * that same contract; callbacks must never return promises.
 */
export function createJsonPolicyStorage({ stateFile, lockTimeoutMs = 2500, retryMs = 10,
  isOwnerAlive = ownerAlive } = {}) {
  if (typeof stateFile !== 'string' || !stateFile.trim()) throw new PolicyStateError('stateFile is required')
  const file = path.resolve(stateFile)
  const lock = file + '.lock'

  function ordinary(fileName) {
    try {
      const info = fs.lstatSync(fileName)
      if (!info.isFile() || info.isSymbolicLink()) throw new PolicyStateError('State/lock must be an ordinary file')
    } catch (error) { if (error.code !== 'ENOENT') throw error }
  }

  function reclaimDeadLock() {
    // A per-owner recovery claim prevents two reclaimers deleting a fresh lock
    // after they both observed the same dead owner. Unknown/corrupt owners stay.
    ordinary(lock)
    let contents, owner
    try { contents = fs.readFileSync(lock, 'utf8'); owner = JSON.parse(contents) } catch { return }
    if (!Number.isSafeInteger(owner?.pid) || owner.pid <= 0 || !identifier(owner.nonce) || isOwnerAlive(owner.pid)) return
    const claim = lock + '.reclaim-' + createHash('sha256').update(contents).digest('hex')
    const ours = JSON.stringify({ pid: process.pid, nonce: randomUUID() })
    const candidate = createOwnerCandidate(claim, ours)
    let claimed = false
    const errors = []
    try {
      try { fs.linkSync(candidate, claim); claimed = true } catch (error) {
        // A crashed reclaimer's claim is intentionally not stolen: another
        // reclaimer can otherwise delete a fresh lock (ABA). Stop all writers
        // and use repairPolicyLocks for this fail-closed recovery path.
        if (error.code === 'EEXIST') return
        throw error
      }
      if (fs.readFileSync(lock, 'utf8') === contents && !isOwnerAlive(owner.pid)) fs.unlinkSync(lock)
    } catch (error) { if (error.code !== 'ENOENT') errors.push(error) } finally {
      try { removeOwnerCandidate(candidate) } catch (error) { errors.push(error) }
      try { if (claimed) safeUnlinkOwned(claim, ours) } catch (error) { errors.push(error) }
      if (errors.length > 1) throw new AggregateError(errors, 'Lock reclamation and cleanup failed')
      if (errors.length === 1) throw errors[0]
    }
  }

  async function acquire() {
    fs.mkdirSync(path.dirname(file), { recursive: true })
    const start = Date.now()
    const contents = JSON.stringify({ pid: process.pid, nonce: randomUUID() })
    const candidate = createOwnerCandidate(lock, contents)
    let published = false, acquireError
    try {
      for (;;) {
        ordinary(lock)
        try { fs.linkSync(candidate, lock); published = true } catch (error) {
          const windowsPendingDelete = process.platform === 'win32' && ['EPERM', 'EACCES'].includes(error.code)
          if (error.code !== 'EEXIST' && !windowsPendingDelete) throw error
          // Windows can report access denied while another process is closing a
          // deleted file. Wait within the same bound; permission failures never
          // establish a dead owner and must not trigger lock reclamation.
          if (!windowsPendingDelete) reclaimDeadLock()
          if (Date.now() - start >= lockTimeoutMs) {
            const unavailable = new PolicyStateError('Decision state lock unavailable; inspect repairPolicyLocks before stopped-writer repair')
            unavailable.code = 'POLICY_LOCK_UNAVAILABLE'
            throw unavailable
          }
          await new Promise((resolve) => setTimeout(resolve, retryMs))
          continue
        }
        return () => safeUnlinkOwned(lock, contents)
      }
    } catch (error) {
      acquireError = error
      throw error
    } finally {
      try { removeOwnerCandidate(candidate) } catch (error) {
        // No transaction has begun yet. Do not strand a live-owner lock merely
        // because cleanup of its otherwise inert candidate was denied.
        const errors = acquireError ? [acquireError, error] : [error]
        if (published) {
          try { safeUnlinkOwned(lock, contents) } catch (releaseError) { errors.push(releaseError) }
        }
        if (errors.length > 1) throw new AggregateError(errors, 'Lock acquisition or release and candidate cleanup failed')
        throw error
      }
    }
  }

  function read() {
    ordinary(file)
    try { return validateState(JSON.parse(fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, ''))) } catch (error) {
      if (error.code === 'ENOENT') return { version: VERSION, scopes: {} }
      if (error instanceof PolicyStateError) throw error
      throw new PolicyStateError('Decision state unreadable/corrupt; refusing to assume open')
    }
  }

  function write(state) {
    validateState(state)
    ordinary(file)
    const temp = file + '.tmp-' + process.pid + '-' + randomUUID()
    let fd
    let created = false
    try {
      fd = fs.openSync(temp, 'wx', 0o600)
      created = true
      fs.writeFileSync(fd, JSON.stringify(state) + '\n', 'utf8')
      fs.fsyncSync(fd)
      fs.closeSync(fd); fd = undefined
      fs.renameSync(temp, file)
    } finally {
      if (fd !== undefined) fs.closeSync(fd)
      if (created) {
        try { fs.unlinkSync(temp) } catch (error) { if (error.code !== 'ENOENT') throw error }
      }
    }
  }

  return {
    async transact(fn) {
      const release = await acquire()
      try {
        const state = read()
        let committed = JSON.stringify(state)
        const commit = () => { write(state); committed = JSON.stringify(state) }
        const result = fn(state, commit)
        if (result && typeof result.then === 'function') throw new PolicyStateError('Storage callbacks must be synchronous')
        validateState(state)
        if (JSON.stringify(state) !== committed) commit()
        return result
      } finally { release() }
    },
  }
}

/** Create an uninstalled policy instance. Metadata/ACL/evidence are supplied by
 * the adapter, never copied from arbitrary model text or a quoted message.
 * tool_action means an internal/non-communication action. QQ media/forward/raw
 * sends MUST be classified media/forward/api, even when invoked by a tool.
 */
export function createDecisionPolicy({ stateFile, storage, now = Date.now, maxInbound = 512,
  maxDeliveries = 512, maxScopes = 256, maxObservations = 256, ticketTtlMs = 300_000,
  autonomousPrivateScope } = {}) {
  if (typeof stateFile !== 'string' || !stateFile.trim()) throw new PolicyStateError('stateFile is required')
  if (autonomousPrivateScope !== undefined &&
      (!canonicalScope(autonomousPrivateScope).startsWith('private:'))) throw new PolicyStateError('Autonomous contact needs one explicit private scope')
  for (const value of [maxInbound, maxDeliveries, maxScopes, maxObservations, ticketTtlMs]) {
    if (!Number.isSafeInteger(value) || value < 1) throw new PolicyStateError('Policy limits must be positive integers')
  }
  const store = storage || createJsonPolicyStorage({ stateFile })
  if (typeof store.transact !== 'function') throw new PolicyStateError('storage.transact is required')
  const transaction = (fn) => store.transact((state, commit) => { validateState(state); return fn(state, commit) })

  function scopeState(state, scope, create = false) {
    canonicalScope(scope)
    if (!state.scopes[scope] && create) {
      if (Object.keys(state.scopes).length >= maxScopes) throw new PolicyStateError('Scope capacity reached; no silent scope is evicted')
      state.scopes[scope] = { revision: 0, mode: 'open', current: null, seen: [], deliveries: [] }
    }
    return state.scopes[scope]
  }

  function validateObservationAck({ scope, decisionId, localOutcomeId, expectedRevision }) {
    canonicalScope(scope)
    if (localOutcomeId !== undefined && (decisionId !== undefined || typeof localOutcomeId !== 'string' ||
        !/^[0-9a-f]{64}$/.test(localOutcomeId) || !Number.isSafeInteger(expectedRevision) || expectedRevision < 1)) {
      throw new PolicyStateError('Exact local outcome acknowledgement required')
    }
  }

  function acknowledgeObservation(state, { scope, eventId, decisionId, localOutcomeId, expectedRevision }) {
    const current = scopeState(state, scope)
    const index = current?.observations?.findIndex(entry => entry.eventId === eventId) ?? -1
    if (index < 0) return { acknowledged: false, reason: 'unknown_observation' }
    const entry = current.observations[index]
    const matches = localOutcomeId !== undefined
      ? entry.localOutcome?.local_outcome_id === localOutcomeId && entry.revision === expectedRevision &&
        !entry.decision && (entry.localOnly || !current.deliveries.some(item => item.revision === expectedRevision))
      : Boolean(entry.decision && entry.decision.decisionId === decisionId)
    if (!matches) return { acknowledged: false, reason: 'decision_mismatch' }
    current.observations.splice(index, 1)
    return { acknowledged: true }
  }

  function inspectTicket(state, ticket, { scope, kind = 'text' } = {}) {
    if (!plain(ticket)) return { allowed: false, reason: 'missing_ticket' }
    canonicalScope(scope ?? ticket.scope)
    if ((scope ?? ticket.scope) !== ticket.scope) return { allowed: false, reason: 'scope_mismatch' }
    if (!KINDS.has(kind)) return { allowed: false, reason: 'unknown_kind' }
    const current = scopeState(state, ticket.scope)
    if (!current || ticket.revision !== current.revision) return { allowed: false, reason: 'stale_revision' }
    const permit = current.current?.ticket
    if (!permit || permit.id !== ticket.id || current.current.messageId !== ticket.messageId) {
      return { allowed: false, reason: 'invalid_ticket' }
    }
    if (now() >= permit.expiresAt) return { allowed: false, reason: 'ticket_expired' }
    if (current.current.origin === 'observation' && autonomousRestState(now()).resting) {
      return { allowed: false, reason: 'autonomy_rest' }
    }
    if (permit.verdict === 'tool_action' && kind !== 'tool_action') return { allowed: false, reason: 'not_a_speech_ticket' }
    if (current.mode === 'silent' && kind !== 'tool_action') return { allowed: false, reason: 'silent' }
    if (current.current.origin === 'observation' && ticket.scope.startsWith('private:') && !['tool_action', 'publication'].includes(kind)) {
      const contact = privateContactState(current, now(), ticket.id, ticket.scope === autonomousPrivateScope)
      if (!contact.allowed) return { allowed: false, reason: contact.reason }
    }
    return { allowed: true, reason: 'permitted', revision: current.revision }
  }

  return {
    async proactiveStatus(scope) {
      canonicalScope(scope)
      return transaction(state => ({ ...autonomousRestState(now()),
        ...(scope.startsWith('private:') ? privateContactState(scopeState(state, scope), now(), undefined, scope === autonomousPrivateScope)
          : { allowed: true, reason: 'group_not_private_contact_limited' }) }))
    },
    async recordInbound(event) {
      if (!plain(event)) throw new PolicyStateError('Trusted inbound metadata is required')
      const scope = canonicalScope(event.scope)
      if (!identifier(event.messageId) || typeof event.sender !== 'string' || !/^[1-9][0-9]*$/.test(event.sender) ||
          typeof event.controlAllowed !== 'boolean' || (event.quoted !== undefined && typeof event.quoted !== 'boolean')) {
        throw new PolicyStateError('Invalid inbound identity/ACL metadata')
      }
      if (scope.startsWith('private:') && scope.slice(8) !== event.sender) throw new PolicyStateError('Private sender/scope mismatch')
      if (event.origin !== 'live') return { accepted: false, reason: 'not_live', scope }
      return transaction((state) => {
        const current = scopeState(state, scope, true)
        const seen = current.seen.find((entry) => entry.messageId === event.messageId && (entry.origin || 'live') === 'live')
        if (seen) return { accepted: false, reason: 'duplicate', scope, revision: seen.revision }
        if (!Number.isSafeInteger(current.revision + 1)) throw new PolicyStateError('Revision exhausted')
        current.revision += 1
        current.lastLiveRevision = current.revision
        current.updatedAt = now()
        current.current = { messageId: event.messageId, sender: event.sender, controlAllowed: event.controlAllowed,
          origin: 'live', quoted: event.quoted === true, decided: false, ticket: null }
        current.seen.push({ messageId: event.messageId, origin: 'live', revision: current.revision, at: now() })
        current.seen = current.seen.slice(-maxInbound)
        return { accepted: true, scope, revision: current.revision, messageId: event.messageId, mode: current.mode }
      })
    },

    /** Persist only the verified current live input, not the diagnostic log or
     * caller-supplied history. Storage is on the existing private policy file;
     * staging never issues a ticket or changes the live revision. */
    async rememberReplyInput({ scope, expectedRevision, messageId, owner, text } = {}) {
      canonicalScope(scope)
      if (!replyOwnerValid(owner) || typeof text !== 'string' || !text.trim() || text.length > 32_000) {
        throw new PolicyStateError('Bounded owned reply input required')
      }
      const evidence = clone({ owner, text, at: now(), attempted: false })
      return transaction(state => {
        const current = scopeState(state, scope)
        if (!current || current.revision !== expectedRevision || current.current?.messageId !== messageId ||
            current.current.origin !== 'live' || current.current.replyReview) return false
        current.current.replyReview = evidence
        return true
      })
    },

    async stageReconnectedFriend({ scope, expectedRevision, receipts, owner, canStage } = {}) {
      canonicalScope(scope)
      if (!scope.startsWith('private:') || !replyOwnerValid(owner) || typeof canStage !== 'function' ||
          !Number.isSafeInteger(expectedRevision) || !Array.isArray(receipts) || receipts.length < 1 || receipts.length > 8 ||
          receipts.some(receipt => !plain(receipt) || typeof receipt.messageId !== 'string' ||
            !/^-?[0-9]{1,16}$/.test(receipt.messageId) || !Number.isSafeInteger(Number(receipt.messageId)) ||
            !Number.isSafeInteger(receipt.at) || typeof receipt.text !== 'string' || !receipt.text.trim() ||
            receipt.text.length > 2000 || /^\s*\//u.test(receipt.text))) {
        throw new PolicyStateError('Invalid verified friend history')
      }
      const batch = clone(receipts), latest = batch.at(-1)
      if (new Set(batch.map(receipt => receipt.messageId)).size !== batch.length ||
          batch.some((receipt, i) => i > 0 && receipt.at < batch[i - 1].at)) {
        throw new PolicyStateError('Unordered or repeated friend history')
      }
      return transaction(state => {
        const current = scopeState(state, scope), at = now()
        if (!current || current.revision !== expectedRevision || current.mode !== 'open' ||
            replyReviewable(current, at) || latest.at > at || batch[0].at < at - FRIEND_RECONNECT_WINDOW_MS ||
            current.seen.some(item => Number.isSafeInteger(item.at) && item.at >= batch[0].at ||
              batch.some(receipt => item.messageId === receipt.messageId)) ||
            current.deliveries.some(item => item.at >= batch[0].at || item.status === 'reserved') || canStage() !== true) return false
        if (!Number.isSafeInteger(current.revision + batch.length)) throw new PolicyStateError('Revision exhausted')
        for (const receipt of batch) {
          current.revision += 1
          current.seen.push({ messageId: receipt.messageId, origin: 'live', revision: current.revision, at: receipt.at })
        }
        current.seen = current.seen.slice(-maxInbound)
        current.lastLiveRevision = current.revision
        current.updatedAt = at
        current.current = { messageId: latest.messageId, sender: scope.slice(8), origin: 'live',
          controlAllowed: false, quoted: true, decided: false, ticket: null,
          replyReview: { owner: clone(owner), text: batch.map(receipt => receipt.text).join('\n\n'),
            at: latest.at, attempted: false } }
        return true
      })
    },

    async replyReviewCandidates() {
      return transaction(state => Object.entries(state.scopes).filter(([, value]) =>
        replyReviewable(value, now()) && now() - value.current.replyReview.at >= REPLY_REVIEW_AGE)
        .map(([scope, value]) => ({ scope, revision: value.revision, messageId: value.current.messageId,
          sender: value.current.sender, owner: clone(value.current.replyReview.owner), at: value.current.replyReview.at }))
        .sort((a, b) => a.at - b.at))
    },

    /** One fresh decision about a confirmed unsent receipt. The original live
     * message remains in native history; its control/administrator authority is
     * NOT replayed. The transaction invalidates any old reply ticket. */
    async claimReplyReview({ scope, expectedRevision, messageId, owner, canClaim } = {}) {
      canonicalScope(scope)
      if (!replyOwnerValid(owner) || typeof canClaim !== 'function') throw new PolicyStateError('Trusted recovery checks required')
      return transaction(state => {
        const current = scopeState(state, scope)
        if (!current || current.revision !== expectedRevision || current.current?.messageId !== messageId ||
            !replyReviewable(current, now()) || now() - current.current.replyReview.at < REPLY_REVIEW_AGE ||
            !replyOwnerSame(owner, current.current.replyReview.owner) || canClaim() !== true) return { accepted: false }
        if (!Number.isSafeInteger(current.revision + 1)) throw new PolicyStateError('Revision exhausted')
        const previous = current.current, evidence = { ...previous.replyReview, attempted: true }
        current.revision += 1
        current.updatedAt = now()
        current.current = { messageId, sender: previous.sender, origin: 'live', controlAllowed: false,
          quoted: true, decided: false, ticket: null, replyReview: evidence }
        return { accepted: true, scope, messageId, revision: current.revision, origin: 'live',
          recoveryRevision: current.revision, text: evidence.text, sender: previous.sender, mode: current.mode }
      })
    },

    /** The adapter calls this only when it proves the queue was never touched. */
    async releaseUnqueuedReplyReview({ scope, expectedRevision, messageId, owner } = {}) {
      canonicalScope(scope)
      if (!replyOwnerValid(owner) || !identifier(messageId) || !Number.isSafeInteger(expectedRevision)) {
        throw new PolicyStateError('Exact reply review claim required')
      }
      return transaction(state => {
        const current = scopeState(state, scope), event = current?.current
        if (current?.revision !== expectedRevision || event?.messageId !== messageId ||
            event.origin !== 'live' || event.controlAllowed || !event.quoted || event.decided || event.ticket ||
            event.replyReview?.attempted !== true || !replyOwnerSame(owner, event.replyReview.owner) ||
            current.deliveries.some(item => item.revision === expectedRevision)) return false
        event.replyReview.attempted = false
        return true
      })
    },

    /** Trusted evidence pump only; never impersonates a live QQ user. An
     * undecided observation may be redriven after a crashed/aborted turn. A
     * decided one returns its durable summary and must not generate/send again.
     */
    async recordObservation({ scope, eventId, kind, producer, createdAt, redrive = false, canRecord } = {}) {
      canonicalScope(scope)
      if (![eventId, kind, producer].every(identifier) || !Number.isSafeInteger(createdAt) || createdAt < 0) {
        throw new PolicyStateError('Invalid observation metadata')
      }
      return transaction((state) => {
        // The trusted bridge can defer background evidence while a real user
        // turn is being queued or sent. Check inside the storage transaction,
        // after lock waits, so an observer cannot revoke that live turn.
        if (canRecord !== undefined && (typeof canRecord !== 'function' || canRecord() !== true)) {
          return { accepted: false, reason: 'foreground_busy', scope }
        }
        const current = scopeState(state, scope, true)
        if (replyReviewable(current, now())) return { accepted: false, reason: 'unanswered_live_input', scope }
        current.observations ||= []
        const seen = current.observations.find((entry) => entry.eventId === eventId)
        if (seen?.decision) return { accepted: false, reason: 'already_decided', scope, decision: clone(seen.decision) }
        if (seen?.localOutcome) return { accepted: false, reason: 'already_completed_without_decision', scope, local_outcome: clone(seen.localOutcome) }
        if (seen && redrive !== true) return { accepted: false, reason: 'duplicate', scope, revision: seen.revision }
        if (seen && current.deliveries.some((entry) => entry.revision === seen.revision)) {
          return { accepted: false, reason: 'uncertain_delivery', scope }
        }
        if (!seen && current.observations.length >= maxObservations) throw new PolicyStateError('Pinned observation capacity reached')
        if (!Number.isSafeInteger(current.revision + 1)) throw new PolicyStateError('Revision exhausted')
        current.revision += 1
        current.updatedAt = now()
        current.current = { messageId: eventId, sender: null, controlAllowed: false, origin: 'observation',
          quoted: false, decided: false, ticket: null, kind, producer, createdAt }
        if (seen) { seen.revision = current.revision; seen.at = now() }
        else current.observations.push({ eventId, revision: current.revision, at: now() })
        return { accepted: true, scope, revision: current.revision, messageId: eventId,
          origin: 'observation', mode: current.mode }
      })
    },

    /** A trusted consumer may close a missed rest-window opportunity locally.
     * This never impersonates a model decision or changes the foreground ticket.
     * localOnly revisions identify this event, not a native/live turn.
     */
    async finishUnstartedObservation({ scope, eventId, kind, producer, createdAt, canFinish } = {}) {
      canonicalScope(scope)
      if (![eventId, kind, producer].every(identifier) || !Number.isSafeInteger(createdAt) || createdAt < 0 ||
          canFinish !== undefined && typeof canFinish !== 'function') throw new PolicyStateError('Trusted observation metadata required')
      return transaction(state => {
        const time = now(), rest = autonomousRestState(time)
        if (createdAt > time) throw new PolicyStateError('A future observation cannot be closed as a missed opportunity')
        const reason = rest.resting ? 'autonomy_rest' : createdAt < rest.current_day_wake_at ? 'observation_expired' : null
        if (!reason) return { finished: false, reason: 'within_autonomous_hours' }
        if (canFinish && canFinish() !== true) return { finished: false, reason: 'observation_in_flight' }
        const current = scopeState(state, scope, true)
        current.observations ||= []
        let entry = current.observations.find(item => item.eventId === eventId)
        if (entry?.decision) return { finished: false, reason: 'already_decided', decision: clone(entry.decision) }
        if (entry?.localOutcome) return { finished: true, local_outcome: clone(entry.localOutcome) }
        if (entry && (current.deliveries.some(item => item.revision === entry.revision) ||
            current.current?.messageId === eventId && (current.current.decided || current.current.ticket))) {
          return { finished: false, reason: 'unsafe_existing_delivery' }
        }
        if (!entry) {
          if (current.observations.length >= maxObservations) throw new PolicyStateError('Pinned observation capacity reached')
          entry = { eventId, revision: Math.max(1, current.revision), at: time, localOnly: true }
          current.observations.push(entry)
        }
        entry.localOutcome = { status: 'no_decision', reason, native_turn_completed: false,
          observation_revision: entry.revision, completed_at: time,
          local_outcome_id: noDecisionId(scope, eventId, entry.revision) }
        return { finished: true, local_outcome: clone(entry.localOutcome) }
      })
    },

    /** Persist the local lifecycle outcome before the queue publishes done.
     * This does not grant a verdict, ticket, or persistent silence change.
     */
    async completeObservationWithoutDecision({ scope, eventId, expectedRevision, nativeTurnCompleted,
      nativeAdmissionReason, nativeModelSteps, nativeRestInterrupted, nativeRestInterruptedAt, nativeSupersedingRevision } = {}) {
      canonicalScope(scope)
      // These attestations come only from the native adapter, never qq_decide
      // arguments. A refused admission is not a completed model turn or a
      // settled budget. Only the stable old-instance key refusal is terminal.
      const refused = nativeAdmissionReason === 'previous_turn_unresolved' &&
        nativeTurnCompleted === false && nativeModelSteps === 0 && nativeRestInterrupted === undefined
      const completed = nativeAdmissionReason === undefined && nativeTurnCompleted === true && nativeModelSteps === undefined && nativeRestInterrupted === undefined
      const interruptionAt = nativeRestInterruptedAt ?? now()
      const restInterrupted = nativeRestInterrupted === true && nativeTurnCompleted === false &&
        nativeAdmissionReason === undefined && Number.isSafeInteger(interruptionAt) && interruptionAt <= now() &&
        interruptionAt >= 0 && autonomousRestState(interruptionAt).resting
      const superseded = Number.isSafeInteger(nativeSupersedingRevision) && nativeSupersedingRevision > expectedRevision &&
        nativeTurnCompleted === false && nativeAdmissionReason === undefined && nativeModelSteps === undefined &&
        nativeRestInterrupted === undefined && nativeRestInterruptedAt === undefined
      if (nativeSupersedingRevision !== undefined && !superseded ||
          nativeRestInterrupted !== undefined && nativeRestInterrupted !== true ||
          nativeRestInterruptedAt !== undefined && nativeRestInterrupted !== true ||
          !identifier(eventId) || !Number.isSafeInteger(expectedRevision) || expectedRevision < 1 ||
          (!completed && !refused && !restInterrupted && !superseded)) {
        throw new PolicyStateError('Exact native completion or zero-step admission refusal required')
      }
      return transaction((state) => {
        const current = scopeState(state, scope)
        const entry = current?.observations?.find(item => item.eventId === eventId)
        // A pinned entry's decision is never cleared by redrive. decide writes
        // that decision and its ticket in the same transaction; recordObservation
        // refuses every later redrive once it exists. Thus no decision here
        // excludes a legal ticket/send across ALL earlier revisions of this
        // same pinned event, without attributing other events' deliveries to it.
        if (!entry || entry.revision !== expectedRevision || entry.decision ||
            superseded && latestLiveRevision(current) < nativeSupersedingRevision ||
            current.deliveries.some(item => item.revision === expectedRevision) ||
            (current.revision === expectedRevision && (current.current?.decided || current.current?.ticket))) {
          throw new PolicyStateError('Observation is not an exact undecided no-delivery turn')
        }
        if (!entry.localOutcome) entry.localOutcome = { status: 'no_decision',
          reason: superseded ? 'superseded_live_input'
            : restInterrupted ? 'autonomy_rest' : refused ? 'native_admission_refused' : 'model_completed_without_decision',
          native_turn_completed: completed, observation_revision: expectedRevision, completed_at: now(),
          local_outcome_id: noDecisionId(scope, eventId, expectedRevision) }
        return clone(entry.localOutcome)
      })
    },

    /** Only the trusted pump calls this AFTER a durable matching done marker.
     * Pending observations remain pinned independently of the QQ LRU.
     */
    async ackObservation({ scope, eventId, decisionId, localOutcomeId, expectedRevision } = {}) {
      const request = { scope, eventId, decisionId, localOutcomeId, expectedRevision }
      validateObservationAck(request)
      return transaction(state => acknowledgeObservation(state, request))
    },

    async ackObservations(requests) {
      if (!Array.isArray(requests) || requests.length < 1 || requests.length > 8 || requests.some(request => !plain(request))) {
        throw new PolicyStateError('Expected a bounded observation acknowledgement batch')
      }
      const batch = clone(requests)
      for (const request of batch) validateObservationAck(request)
      return transaction(state => batch.map(request => acknowledgeObservation(state, request)))
    },

    async decide({ scope, messageId, expectedRevision, verdict, controlUpdate = false, evidenceSource = 'current', nextReviewAt = null } = {}) {
      canonicalScope(scope)
      if (!VERDICTS.has(verdict)) throw new PolicyStateError('Unknown model verdict')
      if (nextReviewAt !== null && (!Number.isSafeInteger(nextReviewAt) || nextReviewAt < now() + 60_000 || nextReviewAt > now() + 7 * 86400_000)) {
        throw new PolicyStateError('next_review_at must be an integer between one minute and seven days ahead')
      }
      return transaction((state) => {
        const current = scopeState(state, scope)
        if (!current || current.revision !== expectedRevision || current.current?.messageId !== messageId) {
          return { accepted: false, reason: 'stale_revision' }
        }
        const event = current.current
        if (event.replyReview?.attempted && !['reply', 'idle', 'defer'].includes(verdict)) {
          return { accepted: false, reason: 'historical_reply_review_only' }
        }
        if (event.decided) return { accepted: false, reason: 'already_decided' }
        if (verdict === 'silence' || verdict === 'resume') {
          // Only an explicit LLM control_update about THIS live authorized input
          // can change persistent mode. A quoted/replayed instruction cannot.
          if (controlUpdate !== true || evidenceSource !== 'current' || event.quoted || !event.controlAllowed) {
            return { accepted: false, reason: 'unauthorized_control' }
          }
          current.mode = verdict === 'silence' ? 'silent' : 'open'
        }
        event.decided = true
        event.verdict = verdict
        event.decisionId = randomUUID()
        event.next_review_at = nextReviewAt
        current.decisionRevision = expectedRevision
        current.updatedAt = now()
        const seen = event.origin === 'observation'
          ? current.observations?.find((entry) => entry.eventId === messageId && entry.revision === expectedRevision)
          : current.seen.find((entry) => entry.messageId === messageId &&
            (entry.origin || 'live') === event.origin && entry.revision === expectedRevision)
        if (seen) seen.decision = { verdict, decisionId: event.decisionId, mode: current.mode, next_review_at: nextReviewAt }
        if (['idle', 'defer', 'silence'].includes(verdict)) return { accepted: true, mode: current.mode, ticket: null, decisionId: event.decisionId, next_review_at: nextReviewAt }
        if (current.mode === 'silent' && verdict !== 'tool_action') return { accepted: true, mode: 'silent', ticket: null, reason: 'silent', decisionId: event.decisionId, next_review_at: nextReviewAt }
        event.ticket = { id: event.decisionId, verdict, expiresAt: now() + ticketTtlMs }
        const ticket = Object.freeze({ id: event.ticket.id, scope, revision: current.revision, messageId })
        return { accepted: true, mode: current.mode, ticket, decisionId: event.decisionId, next_review_at: nextReviewAt }
      })
    },

    async check(ticket, options = {}) { return transaction((state) => inspectTicket(state, ticket, options)) },

    async snapshot(scope) {
      canonicalScope(scope)
      return transaction((state) => clone(scopeState(state, scope) || null))
    },

    /** Local plan/proposal publication only. Hold the existing revision lock
     * through a synchronous host callback; no verdict or send permit is issued. */
    async runCurrentRevision({ scope, expectedRevision, requireOpen = false, check } = {}, run) {
      canonicalScope(scope)
      if (!Number.isSafeInteger(expectedRevision) || expectedRevision < 1 || typeof requireOpen !== 'boolean' ||
          typeof check !== 'function' || typeof run !== 'function' || run.constructor?.name === 'AsyncFunction') throw new PolicyStateError('Synchronous revision-bound local publication required')
      return transaction(state => {
        const current = scopeState(state, scope)
        if (!current?.current || current.revision !== expectedRevision || requireOpen && current.mode === 'silent' || check() !== true) throw new PolicyStateError('Stale or silent source revision')
        const value = run()
        if (value && typeof value.then === 'function') throw new PolicyStateError('Local publication must not return a promise')
        return value
      })
    },

    /** One operation/slice identity is reusable only to suppress a duplicate.
     * On transport failure the outcome is UNKNOWN, not safe-to-retry: OneBot
     * does not provide an idempotency key and a timeout may have been delivered.
     * transmit must synchronously INITIATE the send and return its ack promise;
     * it must not hide a later queue/sleep, which needs its own final guard.
     * The adapter must normalize protocol ACKs: a resolved OneBot object with
     * status=failed or nonzero retcode must REJECT, not count as a delivery.
     */
    async runGuarded(ticket, { scope, kind = 'text', operationId, sliceId = '0', meme, socialOneOff = false, autonomous } = {}, transmit) {
      if (!identifier(operationId) || !identifier(String(sliceId)) || typeof transmit !== 'function') {
        throw new PolicyStateError('An operationId, sliceId and transport callback are required')
      }
      if (typeof socialOneOff !== 'boolean' || (autonomous !== undefined && typeof autonomous !== 'boolean') || (meme !== undefined && typeof meme !== 'boolean') ||
          (meme === true && !['media', 'forward', 'api'].includes(kind))) throw new PolicyStateError('Invalid trusted meme delivery metadata')
      let acknowledgement
      const reservation = await transaction((state, commit) => {
        const permission = inspectTicket(state, ticket, { scope, kind })
        if (!permission.allowed) return { sent: false, status: 'suppressed', reason: permission.reason }
        const current = scopeState(state, ticket.scope)
        const observed = current.current.origin === 'observation'
        if (autonomous !== undefined && autonomous !== observed) return { sent: false, status: 'suppressed', reason: 'origin_mismatch' }
        const key = createHash('sha256').update([ticket.id, operationId, String(sliceId)].join('\0')).digest('hex')
        const prior = current.deliveries.find((entry) => entry.key === key)
        if (prior) return { sent: false, status: 'duplicate', previousStatus: prior.status }
        if (socialOneOff && current.deliveries.some(entry => entry.status === 'unknown' ||
            entry.status === 'reserved' && entry.ticketId !== ticket.id)) return { sent: false, status: 'suppressed', reason: 'recipient_unresolved_delivery' }
        if (meme === true && current.deliveries.some(entry => entry.ticketId === ticket.id && entry.meme === true)) {
          return { sent: false, status: 'suppressed', reason: 'meme_turn_limit' }
        }
        // Never evict unresolved sends: losing their reservation could duplicate
        // a message after a restart. A full unresolved ledger fails closed.
        while (current.deliveries.length >= maxDeliveries) {
          const removable = current.deliveries.findIndex((entry) => ['sent', 'suppressed'].includes(entry.status) &&
            // A slot for this ticket is durable until another ticket/revision;
            // text pressure must never collect it and permit a second meme.
            !(entry.meme === true && entry.ticketId === ticket.id) &&
            (entry.revision !== current.revision || now() >= entry.expiresAt))
          if (removable < 0) return { sent: false, status: 'suppressed', reason: 'delivery_capacity' }
          current.deliveries.splice(removable, 1)
        }
        current.deliveries.push({ key, status: 'reserved', at: now(), revision: ticket.revision,
          ticketId: ticket.id, expiresAt: current.current.ticket.expiresAt, ...(meme === true ? { meme: true } : {}),
          ...(kind === 'publication' ? { kind: 'publication' } : {}) })
        if (observed && kind !== 'publication' && ticket.scope.startsWith('private:') && current.proactiveContact?.ticketId !== ticket.id) {
          // Commit the contact with the first actual transport reservation,
          // not with a model verdict. A partial/unknown attempt remains held.
          current.proactiveContact = { ticketId: ticket.id, at: now(), lastLiveRevision: latestLiveRevision(current) }
        }
        if (typeof commit !== 'function') throw new PolicyStateError('storage must support durable commit before egress')
        commit()
        // No await or event-loop yield between the final gate and initiating the
        // transport; the cross-process state lock remains held for this call.
        try {
          acknowledgement = Promise.resolve(transmit()).then(
            (value) => ({ ok: true, value }), (error) => ({ ok: false, error }))
        } catch (error) { acknowledgement = Promise.resolve({ ok: false, error }) }
        return { key }
      })
      if (!reservation.key) return reservation
      const outcome = await acknowledgement
      try {
        await transaction((state) => {
          const current = scopeState(state, ticket.scope)
          const entry = current?.deliveries.find((item) => item.key === reservation.key)
          if (!entry) throw new PolicyStateError('Delivery reservation missing')
          entry.status = outcome.ok ? 'sent' : 'unknown'
          entry.completedAt = now()
        })
      } catch (error) {
        // The durable reservation remains an uncertain delivery; do not turn a
        // bookkeeping error after send into an automatic replay.
        return { sent: outcome.ok, status: 'unknown', reason: 'ack_persistence_failed', error }
      }
      return outcome.ok ? { sent: true, status: 'sent', value: outcome.value }
        : { sent: false, status: 'unknown', reason: 'transport_uncertain', error: outcome.error }
    },
  }
}

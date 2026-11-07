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
const KINDS = new Set(['text', 'media', 'forward', 'api', 'tool_action'])
const VERDICTS = new Set(['reply', 'silence', 'resume', 'defer', 'idle', 'tool_action'])
const clone = (value) => JSON.parse(JSON.stringify(value))
const plain = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)
const identifier = (value) => typeof value === 'string' && value.length > 0 && value.length <= 256 && !/[\x00-\x1f]/.test(value)

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
    for (const delivery of value.deliveries) {
      if (!plain(delivery) || !identifier(delivery.key) ||
          !['reserved', 'sent', 'unknown', 'suppressed'].includes(delivery.status)) {
        throw new PolicyStateError('Corrupt delivery dedup state')
      }
    }
    if (value.observations !== undefined && (!Array.isArray(value.observations) || value.observations.some((entry) =>
      !plain(entry) || !identifier(entry.eventId) || !Number.isSafeInteger(entry.revision) ||
      (entry.decision !== undefined && (!plain(entry.decision) || !VERDICTS.has(entry.decision.verdict) ||
        !identifier(entry.decision.decisionId) || !['open', 'silent'].includes(entry.decision.mode)))))) {
      throw new PolicyStateError('Corrupt pinned observation state')
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
    let fd
    try { fd = fs.openSync(claim, 'wx', 0o600) } catch (error) {
      // A crashed reclaimer's claim is intentionally not stolen: another
      // reclaimer can otherwise delete a fresh lock (ABA). Stop all writers and
      // use repairPolicyLocks for this uncommon fail-closed recovery path.
      if (error.code === 'EEXIST') return
      throw error
    }
    try {
      fs.writeFileSync(fd, ours)
      fs.closeSync(fd); fd = undefined
      if (fs.readFileSync(lock, 'utf8') === contents && !isOwnerAlive(owner.pid)) fs.unlinkSync(lock)
    } catch (error) { if (error.code !== 'ENOENT') throw error } finally {
      if (fd !== undefined) fs.closeSync(fd)
      safeUnlinkOwned(claim, ours)
    }
  }

  async function acquire() {
    fs.mkdirSync(path.dirname(file), { recursive: true })
    const start = Date.now()
    const contents = JSON.stringify({ pid: process.pid, nonce: randomUUID() })
    for (;;) {
      ordinary(lock)
      let fd
      try { fd = fs.openSync(lock, 'wx', 0o600) } catch (error) {
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
      try { fs.writeFileSync(fd, contents) } catch (error) {
        fs.closeSync(fd)
        // A partially written owner is deliberately left locked for inspection.
        throw error
      }
      fs.closeSync(fd)
      return () => safeUnlinkOwned(lock, contents)
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
  maxDeliveries = 512, maxScopes = 256, maxObservations = 256, ticketTtlMs = 300_000 } = {}) {
  if (typeof stateFile !== 'string' || !stateFile.trim()) throw new PolicyStateError('stateFile is required')
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
    if (permit.verdict === 'tool_action' && kind !== 'tool_action') return { allowed: false, reason: 'not_a_speech_ticket' }
    if (current.mode === 'silent' && kind !== 'tool_action') return { allowed: false, reason: 'silent' }
    return { allowed: true, reason: 'permitted', revision: current.revision }
  }

  return {
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
        current.updatedAt = now()
        current.current = { messageId: event.messageId, sender: event.sender, controlAllowed: event.controlAllowed,
          origin: 'live', quoted: event.quoted === true, decided: false, ticket: null }
        current.seen.push({ messageId: event.messageId, origin: 'live', revision: current.revision, at: now() })
        current.seen = current.seen.slice(-maxInbound)
        return { accepted: true, scope, revision: current.revision, messageId: event.messageId, mode: current.mode }
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
        current.observations ||= []
        const seen = current.observations.find((entry) => entry.eventId === eventId)
        if (seen?.decision) return { accepted: false, reason: 'already_decided', scope, decision: clone(seen.decision) }
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

    /** Only the trusted pump calls this AFTER a durable matching done marker.
     * Pending/decided observations are pinned independently of the QQ LRU.
     */
    async ackObservation({ scope, eventId, decisionId } = {}) {
      canonicalScope(scope)
      return transaction((state) => {
        const current = scopeState(state, scope)
        const index = current?.observations?.findIndex((entry) => entry.eventId === eventId) ?? -1
        if (index < 0) return { acknowledged: false, reason: 'unknown_observation' }
        const entry = current.observations[index]
        if (!entry.decision || entry.decision.decisionId !== decisionId) return { acknowledged: false, reason: 'decision_mismatch' }
        current.observations.splice(index, 1)
        return { acknowledged: true }
      })
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

    /** One operation/slice identity is reusable only to suppress a duplicate.
     * On transport failure the outcome is UNKNOWN, not safe-to-retry: OneBot
     * does not provide an idempotency key and a timeout may have been delivered.
     * transmit must synchronously INITIATE the send and return its ack promise;
     * it must not hide a later queue/sleep, which needs its own final guard.
     * The adapter must normalize protocol ACKs: a resolved OneBot object with
     * status=failed or nonzero retcode must REJECT, not count as a delivery.
     */
    async runGuarded(ticket, { scope, kind = 'text', operationId, sliceId = '0' } = {}, transmit) {
      if (!identifier(operationId) || !identifier(String(sliceId)) || typeof transmit !== 'function') {
        throw new PolicyStateError('An operationId, sliceId and transport callback are required')
      }
      let acknowledgement
      const reservation = await transaction((state, commit) => {
        const permission = inspectTicket(state, ticket, { scope, kind })
        if (!permission.allowed) return { sent: false, status: 'suppressed', reason: permission.reason }
        const current = scopeState(state, ticket.scope)
        const key = createHash('sha256').update([ticket.id, operationId, String(sliceId)].join('\0')).digest('hex')
        const prior = current.deliveries.find((entry) => entry.key === key)
        if (prior) return { sent: false, status: 'duplicate', previousStatus: prior.status }
        // Never evict unresolved sends: losing their reservation could duplicate
        // a message after a restart. A full unresolved ledger fails closed.
        while (current.deliveries.length >= maxDeliveries) {
          const removable = current.deliveries.findIndex((entry) => ['sent', 'suppressed'].includes(entry.status) &&
            (entry.revision !== current.revision || now() >= entry.expiresAt))
          if (removable < 0) return { sent: false, status: 'suppressed', reason: 'delivery_capacity' }
          current.deliveries.splice(removable, 1)
        }
        current.deliveries.push({ key, status: 'reserved', at: now(), revision: ticket.revision,
          ticketId: ticket.id, expiresAt: current.current.ticket.expiresAt })
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

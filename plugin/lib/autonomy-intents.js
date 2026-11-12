/** Small, scoped intentions for the existing main agent. No model, scheduler,
 * transport or memory-fact writer is created here. Summaries are agent reports;
 * tool metadata proves a returned receipt, never the truth of arbitrary prose. */
import fs from 'node:fs'
import path from 'node:path'
import { createHash, randomUUID } from 'node:crypto'
import { DatabaseSync } from 'node:sqlite'

const hash = (...values) => createHash('sha256').update(values.map(String).join('\0')).digest('hex')
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const ACTIVE = new Set(['planned', 'active', 'paused', 'blocked_unknown'])
const EDITABLE = new Set(['planned', 'active', 'paused', 'completed', 'abandoned'])
const OUTCOMES = new Set(['returned', 'success', 'failed', 'unknown', 'sent', 'accepted', 'rejected'])
const id = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const scopeValid = value => typeof value === 'string' && /^(private|group):[1-9][0-9]*$/.test(value)
const integer = value => Number.isSafeInteger(value) && value >= 0
export const INTENT_SCHEMA = 'dsh-autonomy-intent/v1'
export const INTENT_ACTIVE_LIMIT = 16
export function intentOwnerScopeKey(owner, scope) {
  if (!plain(owner) || typeof owner.workspace_key !== 'string' || !owner.workspace_key ||
      typeof owner.agent_key !== 'string' || !scopeValid(scope)) throw new Error('Invalid intent owner')
  return hash(owner.workspace_key, owner.agent_key, scope)
}
function short(value, maximum, required = false) {
  if (typeof value !== 'string' || value.length > maximum || /[\x00-\x08\x0b-\x1f]/.test(value) || required && !value.trim()) throw new Error('Invalid short intent field')
  return value.trim()
}
function ordinary(file, directory = false) {
  for (const item of [...parents(file), file]) {
    let info
    try { info = fs.lstatSync(item) } catch (error) { if (error.code === 'ENOENT') continue; throw error }
    if (info.isSymbolicLink() || (item !== file || directory ? !info.isDirectory() : !info.isFile())) throw new Error('Intent path must be ordinary')
  }
}
function parents(file) {
  const result = []; let next = path.dirname(file)
  while (!result.includes(next)) { result.unshift(next); const parent = path.dirname(next); if (parent === next) break; next = parent }
  return result
}
function parseRecord(raw, owner, scope) {
  const value = JSON.parse(raw)
  if (!plain(value) || value.schema !== INTENT_SCHEMA || value.scope !== scope ||
      value.owner?.workspace_key !== owner.workspace_key || value.owner?.agent_key !== owner.agent_key ||
      !id(value.intent_id) || !Number.isSafeInteger(value.revision) || value.revision < 1 ||
      ![...EDITABLE, 'blocked_unknown'].includes(value.status) || !integer(value.created_at) || !integer(value.updated_at) ||
      value.summary_is_agent_report !== true || !Array.isArray(value.tool_receipts) || value.tool_receipts.length > 8) throw new Error('Corrupt scoped intent')
  short(value.goal, 180, true); short(value.reason, 240); short(value.next_step, 240); short(value.agent_summary, 240)
  if (!(value.next_review_at === null || integer(value.next_review_at))) throw new Error('Corrupt intent review')
  for (const receipt of value.tool_receipts) if (!plain(receipt) || !/^[a-zA-Z][\w.-]{0,79}$/.test(receipt.tool_name) ||
      !OUTCOMES.has(receipt.outcome) || !integer(receipt.time) || !id(receipt.receipt_key)) throw new Error('Corrupt tool receipt metadata')
  return value
}

export class IntentConflictError extends Error {
  constructor(message) { super(message); this.name = 'IntentConflictError' }
}

export function createIntentStore({ file, botBinding, now = Date.now }) {
  if (!path.isAbsolute(file) || typeof botBinding !== 'string' || !botBinding) throw new Error('Explicit intent store binding required')
  const binding = hash(botBinding)
  function open(write = false) {
    ordinary(file)
    const existed = fs.existsSync(file)
    if (!existed && !write) return null
    if (write) { fs.mkdirSync(path.dirname(file), { recursive: true }); ordinary(file) }
    const db = new DatabaseSync(file, { readOnly: !write })
    try {
      db.exec('PRAGMA busy_timeout=100')
      const version = db.prepare('PRAGMA user_version').get().user_version
      if (existed && (version !== 1 || db.prepare("SELECT value FROM intent_meta WHERE key='bot_binding'").get()?.value !== binding)) {
        throw new Error('Intent database binding mismatch')
      }
      if (write) {
        db.exec('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL')
        if (version !== 0 && version !== 1) throw new Error('Unsupported intent database')
        db.exec(`CREATE TABLE IF NOT EXISTS intent_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS intent_heads (owner_scope_key TEXT NOT NULL, intent_id TEXT NOT NULL,
            revision INTEGER NOT NULL, status TEXT NOT NULL, updated_at INTEGER NOT NULL, record_json TEXT NOT NULL,
            PRIMARY KEY(owner_scope_key,intent_id));
          CREATE TABLE IF NOT EXISTS intent_revisions (owner_scope_key TEXT NOT NULL, intent_id TEXT NOT NULL,
            revision INTEGER NOT NULL, record_json TEXT NOT NULL, PRIMARY KEY(owner_scope_key,intent_id,revision));
          CREATE TABLE IF NOT EXISTS intent_operations (operation_key TEXT PRIMARY KEY, args_sha256 TEXT NOT NULL, receipt_json TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS social_requests (request_id TEXT PRIMARY KEY, source_key TEXT NOT NULL,
            intent_id TEXT NOT NULL, target_key TEXT NOT NULL, event_id TEXT NOT NULL UNIQUE,
            source_turn_key TEXT NOT NULL, state TEXT NOT NULL, record_json TEXT NOT NULL, updated_at INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS social_sightings (owner_scope_key TEXT NOT NULL, member_id TEXT NOT NULL,
            nickname TEXT NOT NULL, last_seen_at INTEGER NOT NULL, seen_count INTEGER NOT NULL,
            last_message_key TEXT NOT NULL, PRIMARY KEY(owner_scope_key,member_id));
          CREATE TABLE IF NOT EXISTS android_login_help (owner_scope_key TEXT NOT NULL, app TEXT NOT NULL,
            purpose TEXT NOT NULL, state TEXT NOT NULL, request_id TEXT NOT NULL,
            PRIMARY KEY(owner_scope_key,app));
          CREATE TABLE IF NOT EXISTS android_checkout_guard (owner_scope_key TEXT PRIMARY KEY);
          PRAGMA user_version=1`)
        db.prepare("INSERT OR IGNORE INTO intent_meta(key,value) VALUES('bot_binding',?)").run(binding)
      }
      if (db.prepare('PRAGMA user_version').get().user_version !== 1 ||
          db.prepare("SELECT value FROM intent_meta WHERE key='bot_binding'").get()?.value !== binding) throw new Error('Intent database binding mismatch')
      return db
    } catch (error) { db.close(); throw error }
  }
  function transaction(run) {
    const db = open(true)
    try { db.exec('BEGIN IMMEDIATE'); const result = run(db); db.exec('COMMIT'); return result }
    catch (error) { try { db.exec('ROLLBACK') } catch { /* preserve original error */ } throw error }
    finally { db.close() }
  }
  function get(owner, scope, intentId) {
    const key = intentOwnerScopeKey(owner, scope)
    if (!id(intentId)) throw new Error('Invalid intent identifier')
    const db = open()
    if (!db) return null
    try { const row = db.prepare('SELECT record_json FROM intent_heads WHERE owner_scope_key=? AND intent_id=?').get(key, intentId)
      return row ? parseRecord(row.record_json, owner, scope) : null
    } finally { db.close() }
  }
  function loginHelp(owner, scope) {
    const key = intentOwnerScopeKey(owner, scope), db = open()
    if (!db) return []
    try {
      if (!db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='android_login_help'").get()) return []
      return db.prepare('SELECT app,purpose,state FROM android_login_help WHERE owner_scope_key=? ORDER BY app').all(key)
    } finally { db.close() }
  }
  function checkoutActive(owner, scope) {
    const key = intentOwnerScopeKey(owner, scope), db = open()
    if (!db) return false
    try {
      if (!db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='android_checkout_guard'").get()) return false
      return Boolean(db.prepare('SELECT 1 FROM android_checkout_guard WHERE owner_scope_key=?').get(key))
    } finally { db.close() }
  }
  function setCheckout(owner, scope, active) {
    const key = intentOwnerScopeKey(owner, scope)
    return transaction(db => {
      if (active) db.prepare('INSERT OR IGNORE INTO android_checkout_guard VALUES(?)').run(key)
      else db.prepare('DELETE FROM android_checkout_guard WHERE owner_scope_key=?').run(key)
      return active
    })
  }
  function updateLoginHelp({ owner, scope, app, action, purpose, requestId, outcome }) {
    const key = intentOwnerScopeKey(owner, scope)
    return transaction(db => {
      const row = db.prepare('SELECT purpose,state,request_id FROM android_login_help WHERE owner_scope_key=? AND app=?').get(key, app)
      if (action === 'record') {
        if (row) return { status: row.state }
        db.prepare('INSERT INTO android_login_help VALUES(?,?,?,?,?)').run(key, app, purpose, 'pending', requestId)
        return { status: 'pending' }
      }
      if (action === 'resolve') {
        db.prepare('DELETE FROM android_login_help WHERE owner_scope_key=? AND app=?').run(key, app)
        return { status: 'resolved' }
      }
      if (action === 'preview' && row?.state === 'pending') {
        return { status: 'pending', purpose: row.purpose, requestId: row.request_id }
      }
      if (action === 'claim' && row?.state === 'pending' && row.request_id === requestId) {
        db.prepare("UPDATE android_login_help SET state='attempting' WHERE owner_scope_key=? AND app=?").run(key, app)
        return { status: 'claimed', purpose: row.purpose, requestId: row.request_id }
      }
      if (action === 'settle' && row?.state === 'attempting' && row.request_id === requestId) {
        db.prepare('UPDATE android_login_help SET state=? WHERE owner_scope_key=? AND app=?').run(outcome, key, app)
        return { status: outcome }
      }
      return { status: row?.state || 'missing' }
    })
  }
  function list(owner, scope) {
    const key = intentOwnerScopeKey(owner, scope), db = open()
    if (!db) return []
    try { return db.prepare("SELECT record_json FROM intent_heads WHERE owner_scope_key=? AND status IN ('planned','active','paused','blocked_unknown') ORDER BY updated_at DESC,intent_id LIMIT 16")
      .all(key).map(row => parseRecord(row.record_json, owner, scope))
    } finally { db.close() }
  }
  function update({ owner, scope, operationKey, action, intentId, expectedRevision, fields = {}, receipts = [] }) {
    const key = intentOwnerScopeKey(owner, scope)
    if (!id(operationKey) || !['create', 'update'].includes(action) || !plain(fields) ||
        Object.keys(fields).some(field => !['goal', 'reason', 'next_step', 'agent_summary', 'status', 'next_review_at'].includes(field))) throw new Error('Invalid intent operation')
    if (action === 'create') { intentId = hash('intent', key, operationKey); expectedRevision = 0 }
    if (!id(intentId) || !integer(expectedRevision)) throw new Error('Intent revision required')
    const argsHash = hash(JSON.stringify([key, action, intentId, expectedRevision, fields]))
    return transaction(db => {
      const previousOp = db.prepare('SELECT args_sha256,receipt_json FROM intent_operations WHERE operation_key=?').get(operationKey)
      if (previousOp) {
        if (previousOp.args_sha256 !== argsHash) throw new IntentConflictError('Intent operation arguments changed')
        return JSON.parse(previousOp.receipt_json)
      }
      const row = db.prepare('SELECT record_json FROM intent_heads WHERE owner_scope_key=? AND intent_id=?').get(key, intentId)
      const prior = row ? parseRecord(row.record_json, owner, scope) : null
      if ((prior?.revision || 0) !== expectedRevision || action === 'update' && !prior || action === 'create' && prior) throw new IntentConflictError('Stale intent revision')
      if (prior?.status === 'blocked_unknown') throw new IntentConflictError('Uncertain external action must not be automatically retried or cleared')
      const time = now()
      if (!integer(time)) throw new Error('Invalid intent clock')
      const value = { schema: INTENT_SCHEMA, owner: { workspace_key: owner.workspace_key, agent_key: owner.agent_key }, scope,
        intent_id: intentId, revision: expectedRevision + 1, goal: '', reason: '', next_step: '', agent_summary: '', status: 'planned',
        next_review_at: null, created_at: time, ...prior, ...fields, tool_receipts: receipts.slice(-8),
        updated_at: time, summary_is_agent_report: true }
      value.revision = expectedRevision + 1
      if (!EDITABLE.has(value.status)) throw new Error('Only host receipts can mark an uncertain action')
      parseRecord(JSON.stringify(value), owner, scope)
      if (ACTIVE.has(value.status) && (!prior || !ACTIVE.has(prior.status))) {
        const count = db.prepare("SELECT count(*) AS total FROM intent_heads WHERE owner_scope_key=? AND status IN ('planned','active','paused','blocked_unknown')").get(key).total
        if (count >= INTENT_ACTIVE_LIMIT) throw new IntentConflictError('Complete or abandon existing intentions before adding more')
      }
      const raw = JSON.stringify(value), receipt = { ok: true, status: 'stored', intent: value, memory_fact_written: false, automatic_send: false }
      db.prepare('INSERT INTO intent_revisions VALUES(?,?,?,?)').run(key, intentId, value.revision, raw)
      db.prepare('INSERT INTO intent_heads VALUES(?,?,?,?,?,?) ON CONFLICT(owner_scope_key,intent_id) DO UPDATE SET revision=excluded.revision,status=excluded.status,updated_at=excluded.updated_at,record_json=excluded.record_json')
        .run(key, intentId, value.revision, value.status, time, raw)
      db.prepare('INSERT INTO intent_operations VALUES(?,?,?)').run(operationKey, argsHash, JSON.stringify(receipt))
      return receipt
    })
  }
  function prepareSocial(record) {
    if (!id(record.request_id) || !id(record.source_key) || !id(record.target_key) || !id(record.intent_id) ||
        !plain(record.event) || typeof record.event.event_id !== 'string') throw new Error('Invalid social request')
    return transaction(db => {
      const prior = db.prepare('SELECT record_json FROM social_requests WHERE request_id=?').get(record.request_id)
      if (prior) {
        const value = JSON.parse(prior.record_json)
        if (value.proposal_sha256 !== record.proposal_sha256 || value.target_key !== record.target_key || value.source_key !== record.source_key) throw new IntentConflictError('Social request arguments changed')
        return value
      }
      const intent = db.prepare('SELECT revision,status FROM intent_heads WHERE owner_scope_key=? AND intent_id=?').get(record.source_key, record.intent_id)
      if (!intent || intent.revision !== record.intent_revision || !['planned', 'active'].includes(intent.status)) throw new IntentConflictError('Social intent is stale or not active')
      const previous = db.prepare("SELECT state FROM social_requests WHERE source_key=? AND intent_id=? AND state IN ('prepared','queued','unknown') LIMIT 1").get(record.source_key, record.intent_id)
      if (previous) throw new IntentConflictError('An earlier social request is pending or uncertain; do not replay')
      if (db.prepare('SELECT request_id FROM social_requests WHERE source_turn_key=? LIMIT 1').get(record.source_turn_key)) throw new IntentConflictError('Only one contact request may be proposed in a source turn')
      const recipient = db.prepare("SELECT state,updated_at FROM social_requests WHERE target_key=? AND (state IN ('prepared','queued','unknown') OR updated_at>?) LIMIT 1")
        .get(record.target_key, now() - 5 * 60_000)
      if (recipient) throw new IntentConflictError('Recipient has an unresolved request or a recent contact review; do not repeat it')
      const value = { ...record, state: 'prepared', updated_at: now() }
      db.prepare('INSERT INTO social_requests VALUES(?,?,?,?,?,?,?,?,?)').run(value.request_id, value.source_key, value.intent_id,
        value.target_key, value.event.event_id, value.source_turn_key, value.state, JSON.stringify(value), value.updated_at)
      return value
    })
  }
  function socialForEvent(eventId) {
    const db = open(); if (!db) return null
    try { const row = db.prepare('SELECT record_json FROM social_requests WHERE event_id=?').get(eventId); return row ? JSON.parse(row.record_json) : null }
    finally { db.close() }
  }
  function finishSocial(requestId, state, receipt = null) {
    if (!['queued', 'decided', 'sent', 'suppressed', 'unknown', 'no_decision'].includes(state)) throw new Error('Invalid social outcome')
    return transaction(db => {
      const row = db.prepare('SELECT record_json FROM social_requests WHERE request_id=?').get(requestId)
      if (!row) throw new Error('Social request missing')
      const value = JSON.parse(row.record_json)
      if (value.state === 'unknown' && state !== 'unknown') throw new IntentConflictError('Uncertain social action cannot be cleared automatically')
      if (value.state === state) return value
      if (['decided', 'sent', 'suppressed', 'no_decision'].includes(value.state)) throw new IntentConflictError('Completed social request cannot be replayed')
      value.state = state; value.updated_at = now(); value.receipt = receipt
      db.prepare('UPDATE social_requests SET state=?,record_json=?,updated_at=? WHERE request_id=?').run(state, JSON.stringify(value), value.updated_at, requestId)
      if (state === 'unknown') {
        const head = db.prepare('SELECT record_json FROM intent_heads WHERE owner_scope_key=? AND intent_id=?').get(value.source_key, value.intent_id)
        if (head) {
          const current = JSON.parse(head.record_json)
          current.revision++; current.status = 'blocked_unknown'; current.updated_at = value.updated_at
          const raw = JSON.stringify(current)
          db.prepare('INSERT INTO intent_revisions VALUES(?,?,?,?)').run(value.source_key, value.intent_id, current.revision, raw)
          db.prepare('UPDATE intent_heads SET revision=?,status=?,updated_at=?,record_json=? WHERE owner_scope_key=? AND intent_id=?')
            .run(current.revision, current.status, current.updated_at, raw, value.source_key, value.intent_id)
        }
      }
      return value
    })
  }
  function rememberSighting(owner, scope, member, nickname, messageKey) {
    const key = intentOwnerScopeKey(owner, scope)
    if (!scope.startsWith('group:') || !/^[1-9]\d*$/.test(member) || !id(messageKey)) throw new Error('Invalid live group sighting')
    const name = short(nickname, 80), time = now()
    return transaction(db => db.prepare(`INSERT INTO social_sightings VALUES(?,?,?,?,1,?)
      ON CONFLICT(owner_scope_key,member_id) DO UPDATE SET nickname=excluded.nickname,last_seen_at=excluded.last_seen_at,
      seen_count=min(social_sightings.seen_count+1,1000000),last_message_key=excluded.last_message_key
      WHERE social_sightings.last_message_key<>excluded.last_message_key`).run(key, member, name, time, messageKey))
  }
  function sighting(owner, scope, member) {
    const key = intentOwnerScopeKey(owner, scope), db = open()
    if (!db) return null
    try { return db.prepare('SELECT nickname,last_seen_at,seen_count FROM social_sightings WHERE owner_scope_key=? AND member_id=? AND last_seen_at>=?')
      .get(key, member, now() - 7 * 86400_000) || null
    } finally { db.close() }
  }
  function sightings(owner, scope) {
    const key = intentOwnerScopeKey(owner, scope), db = open()
    if (!db) return []
    try { return db.prepare('SELECT member_id,nickname,last_seen_at,seen_count FROM social_sightings WHERE owner_scope_key=? AND last_seen_at>=? ORDER BY last_seen_at DESC LIMIT 2000')
      .all(key, now() - 7 * 86400_000)
    } finally { db.close() }
  }
  return { file, get, list, update, prepareSocial, socialForEvent, finishSocial, rememberSighting, sighting, sightings,
    loginHelp, updateLoginHelp, checkoutActive, setCheckout }
}

/** Publish exactly one full observation. Existing bytes must match; an older
 * done marker is never removed/replaced. Caller separately verifies completion. */
export function publishIntentObservation(directory, event, observationKey) {
  const target = path.join(directory, 'pending', observationKey + '.json')
  ordinary(target)
  fs.mkdirSync(path.dirname(target), { recursive: true }); ordinary(target)
  const raw = Buffer.from(JSON.stringify(event) + '\n'), temporary = target + '.' + randomUUID() + '.candidate'
  let fd
  try {
    fd = fs.openSync(temporary, 'wx', 0o600); fs.writeFileSync(fd, raw); fs.fsyncSync(fd); fs.closeSync(fd); fd = undefined
    try { fs.linkSync(temporary, target) }
    catch (error) { if (error.code !== 'EEXIST' || !fs.readFileSync(target).equals(raw)) throw error }
  } finally { if (fd !== undefined) fs.closeSync(fd); if (fs.existsSync(temporary)) fs.unlinkSync(temporary) }
  return { published: true }
}

const peerId = value => typeof value !== 'boolean' && /^[1-9]\d*$/.test(String(value ?? '')) ? String(value) : null
const safeName = value => String(value || '').replace(/[\x00-\x1f<>]/g, '').slice(0, 80)
const sameOwner = (a, b) => a?.workspace_key === b?.workspace_key && a?.agent_key === b?.agent_key
/** Read only tool metadata in the current own native turn, never content,
 * thinking or another session. A non-error tool result is just "returned". */
export function readIntentReceipts(session, turn) {
  if (!Number.isSafeInteger(session?.seq) || typeof session.snapshotEvents !== 'function') return []
  const end = session.seq, start = Math.max(0, end - 4096, Number.isSafeInteger(session.inheritedEventCount) ? session.inheritedEventCount : 0)
  const events = session.snapshotEvents(start, end), calls = new Map(), used = new Set(), receipts = []
  if (!Array.isArray(events) || events.length > 4096) return []
  for (const event of events) {
    const data = event?.data
    if (data?.turn !== turn) continue
    if (event.type === 'tool/call' && Number.isSafeInteger(event.seq) && typeof data.callId === 'string' && /^[a-zA-Z][\w.-]{0,79}$/.test(data.name || '')) calls.set(event.seq, { callId: data.callId, step: data.step, name: data.name })
    if (event.type !== 'tool/result' || event.surfaceOp !== 'append' || !Array.isArray(event.sourceEventSeqs) || event.sourceEventSeqs.length !== 1) continue
    const callSeq = event.sourceEventSeqs[0], call = calls.get(callSeq), message = data.message
    if (!call || used.has(callSeq) || !Number.isSafeInteger(event.seq) || callSeq >= event.seq || call.step !== data.step || message?.role !== 'tool' || message.source?.kind !== 'tool' || message.source?.callId !== call.callId || message.toolCallId !== call.callId || !integer(event.time)) continue
    used.add(callSeq)
    receipts.push({ tool_name: call.name, outcome: message.isError === true || data.error ? 'failed' : 'returned',
      time: event.time, receipt_key: hash('tool-receipt', callSeq, event.seq, call.callId) })
  }
  return receipts.slice(-8)
}

export class AutonomyPlanner {
  constructor(adapter) {
    this.adapter = adapter
    this.store = createIntentStore({ file: path.join(path.dirname(adapter.stateFile), 'autonomy-intents.sqlite3'), botBinding: path.resolve(adapter.stateFile), now: adapter.now })
    this.scopes = new WeakSet(); this.candidates = new Map(); this.groups = new Map(); this.deliveryChecks = new Map()
  }
  definitions() {
    const json = { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] }
    return [{ name: 'qq_intent', description: 'Read or CAS-update this conversation’s short intentions. Store goals, a brief decision reason and the next step, never raw thinking. Summaries are agent reports, not verified memory facts. Does not send QQ or start a scheduler.',
      parameters: { action: { type: 'string', required: true, enum: ['list', 'create', 'update'] }, intent_id: { type: 'string' }, expected_revision: { type: 'number' },
        goal: { type: 'string' }, reason: { type: 'string' }, next_step: { type: 'string' }, agent_summary: { type: 'string' },
        status: { type: 'string', enum: [...EDITABLE] }, next_review_at: { type: 'number' } }, output: json, isConcurrencySafe: () => false, execute: (args, exec) => this.intent(args, exec) },
    { name: 'qq_social_candidates', description: 'Read real friend/shared-group candidates for the configured primary main agent. Names/relationship/recent live appearance metadata only. A roster alone is unfamiliar; this tool sends nothing. Group search and joining are unsupported.',
      parameters: { action: { type: 'string', required: true, enum: ['friends', 'groups', 'members'] }, group_ref: { type: 'string' }, limit: { type: 'number' } },
      output: json, isConcurrencySafe: () => false, execute: (args, exec) => this.socialCandidates(args, exec) },
    { name: 'qq_social_propose', description: 'Propose one contact from a real candidate and active intention. Only queues a one-off observation to that recipient’s own main agent, which must independently decide with a NEW scope-bound ticket. Never sends with the source ticket. Nonfriend private reachability is unverified; no group fallback, adding friends, retries or silent-mode override.',
      parameters: { intent_id: { type: 'string', required: true }, expected_revision: { type: 'number', required: true }, candidate_id: { type: 'string', required: true },
        reason: { type: 'string', required: true }, topic: { type: 'string', required: true } }, output: json, isConcurrencySafe: () => false, execute: (args, exec) => this.propose(args, exec) }]
  }
  async bound(exec, primary = false) {
    const a = this.adapter, record = a.captureExec(exec), scope = record.binding.scope, owner = await a.trustedOwner(scope)
    if (a.captureExec(exec) !== record || record.binding.owner && !sameOwner(owner, record.binding.owner)) throw new Error('Stale intent owner')
    const current = await a.policy.snapshot(scope)
    if (!current?.current || current.revision !== record.binding.revision) throw new Error('Stale intention turn')
    const budget = await a.checkBudget(record)
    if (!budget.allowed) throw new Error('Intention budget unavailable')
    if (primary) {
      const guide = await a.runtimeCapabilities(scope, owner)
      a.validateOwnerContext(guide, 'dsh-runtime-capabilities/v1', scope, owner)
      if (guide.read_only !== true || guide.tools?.entertainment?.primary_identity_valid !== true || guide.tools.entertainment.primary_internal_leisure !== true || !scope.startsWith('private:')) throw new Error('Social planning belongs to the configured primary agent')
    }
    if (a.captureExec(exec) !== record || exec.signal?.aborted) throw new Error('Stale intention turn')
    return { record, scope, owner, key: intentOwnerScopeKey(owner, scope) }
  }
  async recheck(b, exec, requireOpen = false) {
    const owner = await this.adapter.trustedOwner(b.scope), snapshot = await this.adapter.policy.snapshot(b.scope)
    if (!sameOwner(owner, b.owner) || this.adapter.captureExec(exec) !== b.record || exec.signal?.aborted ||
        !snapshot?.current || snapshot.revision !== b.record.binding.revision || requireOpen && snapshot.mode === 'silent') throw new Error('Stale or silent source proposal')
  }
  commitCurrent(b, exec, run, requireOpen = false) {
    return this.adapter.policy.runCurrentRevision({ scope: b.scope, expectedRevision: b.record.binding.revision, requireOpen,
      check: () => this.adapter.captureExec(exec) === b.record && !exec.signal?.aborted &&
        (!b.record.binding.owner || sameOwner(b.record.binding.owner, b.owner)) && this.adapter.bridge.effectiveCwd(b.scope) === b.owner.cwd }, run)
  }
  context(scope, owner) { return { schema: 'dsh-autonomy-intentions/v1', intents: this.store.list(owner, scope), summaries_are_agent_reports: true,
    automatic_execution: false, instruction: 'Continue or revise a small intention using actual tools, then update qq_intent with confirmed progress and the next step. Keep only concise goals and decision summaries, never raw thinking. A plan is not action evidence or a memory fact. Personal initiative may include a real candidate proposal; it never borrows a ticket or another conversation’s private notes.' } }
  observeLive(scope, owner, inbound, binding) {
    if (!scope.startsWith('group:') || binding?.origin !== 'live' || inbound?.chatId !== scope || inbound.lanyaInjected === true || !peerId(inbound.userId) || typeof inbound.messageId !== 'string') return
    this.store.rememberSighting(owner, scope, inbound.userId, safeName(inbound.nickname), hash(scope, inbound.messageId))
  }
  async intent(args, exec) {
    if (!plain(args) || !['list', 'create', 'update'].includes(args.action) || Object.keys(args).some(key => !['action', 'intent_id', 'expected_revision', 'goal', 'reason', 'next_step', 'agent_summary', 'status', 'next_review_at'].includes(key))) throw new Error('Invalid qq_intent arguments')
    const b = await this.bound(exec)
    if (args.action === 'list') return { ok: true, ...this.context(b.scope, b.owner), memory_fact_written: false, automatic_send: false }
    if (typeof exec.callId !== 'string' || !exec.callId) throw new Error('Intent tool call identity required')
    const fields = Object.fromEntries(['goal', 'reason', 'next_step', 'agent_summary', 'status', 'next_review_at'].filter(key => args[key] !== undefined).map(key => [key, args[key]]))
    const receipts = readIntentReceipts(exec.agent.session, b.record.turn)
    for (const [index, outcome] of b.record.outcomes.entries()) if (['sent', 'unknown', 'suppressed'].includes(outcome)) receipts.push({ tool_name: 'qq_egress', outcome: outcome === 'suppressed' ? 'rejected' : outcome, time: this.adapter.now(), receipt_key: hash('egress', b.key, b.record.turn, index, outcome) })
    await this.recheck(b, exec)
    return this.commitCurrent(b, exec, () => this.store.update({ owner: b.owner, scope: b.scope, operationKey: hash(b.key, exec.agent.session.id, exec.callId), action: args.action,
      intentId: args.intent_id, expectedRevision: args.expected_revision, fields, receipts }))
  }
  async readAction(action, params) {
    let timer
    try { return await Promise.race([this.adapter.bridge.guardedConnectionCall(action, params), new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Relationship lookup timeout')), 5000) })]) }
    finally { clearTimeout(timer) }
  }
  rows(value) { if (!Array.isArray(value) || value.length > 20000) throw new Error('Invalid bounded relationship list'); return value }
  cache(map, key, value, max = 256) { while (map.size >= max) map.delete(map.keys().next().value); map.set(key, value); return key }
  async socialCandidates(args, exec) {
    if (!plain(args) || Object.keys(args).some(key => !['action', 'group_ref', 'limit'].includes(key)) || !['friends', 'groups', 'members'].includes(args.action)) throw new Error('Invalid candidate action')
    const b = await this.bound(exec, true), limit = args.limit ?? 8
    if (!Number.isInteger(limit) || limit < 1 || limit > 12) throw new Error('Candidate limit must be 1..12')
    const now = this.adapter.now(), self = String(this.adapter.deps.connection.selfId || this.adapter.deps.config.botQQ || ''), candidates = [], groups = []
    if (args.action === 'groups') {
      for (const row of this.rows(await this.readAction('get_group_list', {}))) {
        const group = peerId(row.group_id)
        if (!group || !this.groupAllowed(group)) continue
        const owner = await this.adapter.trustedOwner('group:' + group)
        if (!sameOwner(owner, b.owner)) continue
        const ref = hash('group', b.key, group, randomUUID())
        this.cache(this.groups, ref, { source_key: b.key, group, expires_at: now + 2 * 60_000 }, 32)
        groups.push({ group_ref: ref, display_name: safeName(row.group_name), relation: 'current_bot_group' })
        if (groups.length >= limit) break
      }
    } else {
      let relation = 'friend', group = null, rows
      if (args.action === 'members') {
        const ref = this.groups.get(args.group_ref)
        if (!ref || ref.source_key !== b.key || ref.expires_at < now) throw new Error('Unknown or expired group reference')
        group = ref.group; relation = 'shared_group'
        if (!this.groupAllowed(group)) throw new Error('Group policy disabled this candidate source')
        if (!this.rows(await this.readAction('get_group_list', {})).some(row => peerId(row.group_id) === group)) throw new Error('Bot no longer belongs to this group')
        rows = this.rows(await this.readAction('get_group_member_list', { group_id: group }))
        const recent = new Map(this.store.sightings(b.owner, 'group:' + group).map(row => [row.member_id, row]))
        rows = rows.map(row => ({ ...row, sighting: recent.get(peerId(row.user_id)) })).sort((a, c) => (c.sighting?.last_seen_at || 0) - (a.sighting?.last_seen_at || 0))
      } else rows = this.rows(await this.readAction('get_friend_list', {}))
      for (const row of rows) {
        const peer = peerId(row.user_id)
        if (!peer || peer === self || 'private:' + peer === b.scope) continue
        const familiar = relation === 'friend' || Boolean(row.sighting), candidateId = hash('candidate', b.key, peer, randomUUID())
        this.cache(this.candidates, candidateId, { source_key: b.key, target_scope: 'private:' + peer, peer, group, relation, familiar, expires_at: now + 2 * 60_000 })
        candidates.push({ candidate_id: candidateId, display_name: safeName(row.remark || row.card || row.nickname), relationship: familiar ? relation : 'shared_group_unfamiliar',
          last_seen_at: row.sighting?.last_seen_at || null, live_appearance_count: row.sighting?.seen_count || null,
          can_propose: familiar && this.recipientAllowed(peer), transport: 'private_user_only', reachability: 'unverified',
          friendship: relation === 'friend' ? 'confirmed_friend' : 'not_checked', known_nonfriend: relation === 'friend' ? false : null })
        if (candidates.length >= limit) break
      }
    }
    if (this.adapter.captureExec(exec) !== b.record || exec.signal?.aborted) throw new Error('Stale candidate turn')
    return { ok: true, mode: 'readonly_relationship_candidates', candidates, groups, expires_at: now + 2 * 60_000, automatic_send: false,
      group_search: 'unsupported', group_join_application: 'unsupported' }
  }
  groupAllowed(group) {
    const policy = this.adapter.deps.policy
    if (policy.groupPolicy === 'disabled') return false
    return policy.groupPolicy !== 'allowlist' || Array.isArray(policy.groupAllowFrom) && policy.groupAllowFrom.includes(group)
  }
  recipientAllowed(peer) {
    const policy = this.adapter.deps.policy
    if (policy.dmPolicy === 'disabled') return false
    if (policy.dmPolicy === 'allowlist') return Array.isArray(policy.allowFrom) && policy.allowFrom.includes(peer)
    return policy.allowAllUsers === true || Array.isArray(policy.adminUsers) && policy.adminUsers.includes(peer)
  }
  async verifyRelation(request, ticketId = null) {
    const peer = request.recipient.peer, group = request.recipient.group
    if (!this.recipientAllowed(peer)) throw new Error('Recipient is disabled by private-chat policy')
    const state = await this.adapter.policy.snapshot(request.event.scope)
    if (state?.mode === 'silent' || state?.deliveries?.some(entry => entry.status === 'unknown' || entry.status === 'reserved' && entry.ticketId !== ticketId)) throw new Error('Recipient is silent or has an unresolved delivery')
    if (request.recipient.relation === 'friend') {
      if (!this.rows(await this.readAction('get_friend_list', {})).some(row => peerId(row.user_id) === peer)) throw new Error('Friend relationship no longer verified')
    } else {
      if (!group || !this.groupAllowed(group) || !this.store.sighting(request.source_owner, 'group:' + group, peer)) throw new Error('Recent known group relationship unavailable')
      if (!this.rows(await this.readAction('get_group_list', {})).some(row => peerId(row.group_id) === group)) throw new Error('Shared group unavailable')
      const member = await this.readAction('get_group_member_info', { group_id: group, user_id: peer })
      if (peerId(member?.user_id) !== peer) throw new Error('Current shared-group membership unverified')
    }
  }
  async propose(args, exec) {
    if (!plain(args) || Object.keys(args).sort().join(',') !== 'candidate_id,expected_revision,intent_id,reason,topic') throw new Error('Invalid proposal arguments')
    short(args.reason, 240, true); short(args.topic, 240, true)
    const b = await this.bound(exec, true), candidate = this.candidates.get(args.candidate_id), now = this.adapter.now()
    if (!candidate || candidate.source_key !== b.key || candidate.expires_at < now || !candidate.familiar) throw new Error('A fresh familiar relationship candidate is required')
    const intent = this.store.get(b.owner, b.scope, args.intent_id)
    if (!intent || intent.revision !== args.expected_revision || !['planned', 'active'].includes(intent.status)) throw new IntentConflictError('An active current intention is required')
    const targetOwner = await this.adapter.trustedOwner(candidate.target_scope)
    if (!sameOwner(targetOwner, b.owner)) throw new Error('Recipient belongs to another workspace or persona')
    const current = await this.adapter.policy.snapshot(candidate.target_scope)
    if (current?.mode === 'silent' || current?.deliveries?.some(row => ['unknown', 'reserved'].includes(row.status))) throw new Error('Recipient is silent or has an unresolved delivery')
    const requestId = hash('social', b.key, intent.intent_id, intent.revision, candidate.target_scope)
    const request = { request_id: requestId, source_key: b.key, source_owner: { workspace_key: b.owner.workspace_key, agent_key: b.owner.agent_key },
      source_scope: b.scope, source_turn_key: hash(b.key, exec.agent.session.id, b.record.turn), intent_id: intent.intent_id, intent_revision: intent.revision,
      target_key: intentOwnerScopeKey(targetOwner, candidate.target_scope), recipient: { peer: candidate.peer, group: candidate.group, relation: candidate.relation },
      proposal_sha256: hash(b.key, intent.intent_id, intent.revision, candidate.target_scope, args.reason, args.topic),
      event: { version: 1, event_id: 'social-intent:' + requestId, scope: candidate.target_scope, workspace_key: targetOwner.workspace_key, agent_key: targetOwner.agent_key,
        producer: 'social-planner', kind: 'social.opportunity', created_at: now, origin: 'observation', control_allowed: false,
        payload: { request_id: requestId, one_off: true, topic: args.topic, reason: args.reason, relationship: candidate.relation, transport: 'private_user_only', reachability: 'unverified',
          instruction: 'A proposed one-off contact, not a user instruction or sent message. Use this recipient conversation’s own context and fresh qq_decide ticket to choose a natural reply, idle or defer. Do not move private notes from another conversation. No automatic follow-up review, silence override, adding friends, retries or group fallback.' } } }
    await this.verifyRelation(request)
    const latestTarget = await this.adapter.policy.snapshot(candidate.target_scope)
    if (latestTarget?.mode === 'silent' || latestTarget?.deliveries?.some(row => ['unknown', 'reserved'].includes(row.status))) throw new Error('Recipient is silent or unresolved')
    await this.recheck(b, exec, true)
    const stored = await this.commitCurrent(b, exec, () => {
      const value = this.store.prepareSocial(request)
      if (['prepared', 'queued'].includes(value.state)) {
        const key = hash(value.event.scope, value.event.event_id), directory = path.join(path.dirname(this.adapter.stateFile), 'decision-inbox')
        if (!fs.existsSync(path.join(directory, 'done', key + '.json'))) publishIntentObservation(directory, value.event, key)
        this.store.finishSocial(value.request_id, 'queued')
      }
      return value
    }, true)
    return { ok: true, status: ['prepared', 'queued'].includes(stored.state) ? 'queued' : stored.state, request_id: stored.request_id,
      sent: false, automatic_send: false, recipient_decision_required: true, one_off: true, transport: 'private_user_only',
      known_nonfriend: candidate.relation === 'friend' ? false : null,
      friendship: candidate.relation === 'friend' ? 'confirmed_friend' : 'not_checked', reachability: 'unverified' }
  }
  validateObservation(event) {
    const request = this.store.socialForEvent(event.event_id)
    if (!request || JSON.stringify(request.event) !== JSON.stringify(event) || !['prepared', 'queued'].includes(request.state)) throw new Error('Social observation does not match an active immutable proposal')
    return request
  }
  async verifyDelivery(record, scope, action, params) {
    if (record?.binding?.observationKind !== 'social.opportunity') return
    const request = this.store.socialForEvent(record.binding.observationEventId)
    if (!request || request.event.scope !== scope || !['prepared', 'queued'].includes(request.state) ||
        !this.recipientAllowed(request.recipient.peer) || request.recipient.group && !this.groupAllowed(request.recipient.group) ||
        !['send_msg', 'send_private_msg', 'send_private_forward_msg', 'upload_private_file'].includes(action) ||
        params.group_id !== undefined || peerId(params.user_id) !== request.recipient.peer ||
        params.message_type !== undefined && params.message_type !== 'private') throw new Error('Private-only recipient route refused')
    const checked = this.deliveryChecks.get(request.request_id)
    if (!checked || checked < this.adapter.now() - 5000) { await this.verifyRelation(request, record.ticket?.id); this.deliveryChecks.set(request.request_id, this.adapter.now()) }
  }
  finish(eventId, outcome) {
    const request = this.store.socialForEvent(eventId)
    if (!request) throw new Error('Social result missing proposal')
    return this.store.finishSocial(request.request_id, outcome.status, { status: outcome.status, completed_at: this.adapter.now() })
  }
}

/** The primary main agent may publish its own short status or signature.
 * No model, timer, second account or raw transport is created here. */
import fs from 'node:fs'
import path from 'node:path'
import { createHash } from 'node:crypto'
import { DatabaseSync } from 'node:sqlite'

const digest = (...items) => createHash('sha256').update(items.map(String).join('\0')).digest('hex')
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const ownerSame = (a, b) => ['cwd', 'preset', 'workspace_key', 'agent_key'].every(key => a?.[key] === b?.[key])
const HOUR = 3600_000, DAY = 24 * HOUR
const STATES = new Set(['reserved', 'accepted', 'rejected', 'unknown'])
const ACTIONS = Object.freeze({ post: 'send_qzone_msg', signature: 'set_self_longnick' })

function checkedRows(rows) {
  if (rows.some(row => !STATES.has(row.status) || !Object.hasOwn(ACTIONS, row.kind) ||
      !/^[a-f0-9]{64}$/.test(row.request_key) || typeof row.content !== 'string' ||
      row.content_hash !== digest(row.content) || !Number.isSafeInteger(row.started_at) || row.started_at < 0 ||
      (row.status === 'reserved' ? row.completed_at !== null : !Number.isSafeInteger(row.completed_at) || row.completed_at < row.started_at) ||
      !(row.kind === 'post' ? ['friends', 'self'].includes(row.audience) : row.audience === 'profile') ||
      (row.receipt_id !== null && (typeof row.receipt_id !== 'string' || row.receipt_id.length > 256)) ||
      (row.reason !== null && (typeof row.reason !== 'string' || row.reason.length > 128)))) throw new Error('Invalid account activity record')
  return rows
}

export function accountPublicationAction(action) {
  return typeof action === 'string' && (['set_self_longnick', 'set_qq_profile', 'set_qq_avatar'].includes(action) ||
    /^(?:send|set|delete|comment|reply|forward|like|unlike)_qzone(?:_|$)/.test(action))
}

export function accountActivitySettings(project, capabilities = []) {
  if (!Array.isArray(capabilities) || capabilities.some(value => typeof value !== 'string')) throw new Error('Trusted account capabilities required')
  if (!plain(project) || project.qqBackend !== undefined && !plain(project.qqBackend)) throw new Error('Invalid account activity settings')
  const input = project.qqBackend?.accountActivity === undefined ? {} : project.qqBackend.accountActivity
  if (!plain(input) || Object.keys(input).some(key => !['enabled', 'posts', 'signature', 'audience',
    'postIntervalHours', 'signatureIntervalHours', 'dailyPosts', 'dailySignatures'].includes(key))) throw new Error('Invalid account activity settings')
  const value = { enabled: false, posts: true, signature: true, audience: 'friends',
    postIntervalHours: 6, signatureIntervalHours: 4, dailyPosts: 2, dailySignatures: 2, ...input }
  if (['enabled', 'posts', 'signature'].some(key => typeof value[key] !== 'boolean') ||
      !['friends', 'self'].includes(value.audience) ||
      ['postIntervalHours', 'signatureIntervalHours'].some(key => !Number.isSafeInteger(value[key]) || value[key] < 1 || value[key] > 168) ||
      ['dailyPosts', 'dailySignatures'].some(key => !Number.isSafeInteger(value[key]) || value[key] < 1 || value[key] > 6)) throw new Error('Invalid account activity settings')
  return Object.freeze({ ...value, post_supported: capabilities.includes('account_post'), signature_supported: capabilities.includes('account_signature') })
}

function ordinary(file) {
  for (let current = file;; current = path.dirname(current)) {
    try {
      const info = fs.lstatSync(current)
      if (info.isSymbolicLink() || (current === file ? !info.isFile() || info.nlink !== 1 || info.size > 32 * 1024 * 1024 : !info.isDirectory())) throw new Error('Account activity storage is unavailable')
    } catch (error) { if (error.code !== 'ENOENT') throw error }
    if (path.dirname(current) === current) break
  }
}

export function accountPublicationText(text, kind, forbidden = []) {
  if (typeof text !== 'string' || !text.trim() || text !== text.trim() ||
      [...text].length > (kind === 'signature' ? 80 : 400) ||
      /[\x00-\x09\x0b-\x1f<>]|(?:https?|file|base64):|[a-z]:[\\/]|\\\\|\d{5,}|(?:token|password|cookie|authorization)\s*[:=]/i.test(text) ||
      kind === 'signature' && text.includes('\n') ||
      forbidden.some(value => typeof value === 'string' && value.length >= 5 && text.includes(value))) throw new Error('Use a short original account update without private identifiers, links or local data')
  return text
}

export class AccountActivityStore {
  constructor(file, binding, now = Date.now) {
    if (!path.isAbsolute(file) || !/^[a-f0-9]{64}$/.test(binding)) throw new Error('Trusted account binding required')
    this.file = file; this.binding = binding; this.now = now
  }
  open(write = false) {
    ordinary(this.file)
    const existed = fs.existsSync(this.file)
    if (!existed && !write) return null
    if (write) { fs.mkdirSync(path.dirname(this.file), { recursive: true }); ordinary(this.file) }
    const db = new DatabaseSync(this.file, { readOnly: !write })
    try {
      db.exec('PRAGMA busy_timeout=1000')
      const version = db.prepare('PRAGMA user_version').get().user_version
      if (existed && version !== 1) throw new Error('Account activity database version is unavailable')
      if (write && !existed) {
        db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; BEGIN IMMEDIATE;
          CREATE TABLE account_binding (binding TEXT PRIMARY KEY);
          CREATE TABLE publications (request_key TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL,
            content_hash TEXT NOT NULL, started_at INTEGER NOT NULL, completed_at INTEGER, status TEXT NOT NULL,
            audience TEXT NOT NULL, receipt_id TEXT, reason TEXT);
          CREATE INDEX publication_recent ON publications(kind,started_at DESC);
          PRAGMA user_version=1`)
        db.prepare('INSERT INTO account_binding VALUES(?)').run(this.binding)
        db.exec('COMMIT')
      }
      const bindings = db.prepare('SELECT binding FROM account_binding').all()
      if (bindings.length !== 1 || bindings[0].binding !== this.binding) throw new Error('Account activity database belongs to another account')
      if (write) db.exec('PRAGMA synchronous=FULL')
      return db
    } catch (error) { db.close(); throw error }
  }
  rows(limit = 20) {
    const db = this.open(); if (!db) return []
    try {
      const rows = db.prepare('SELECT * FROM publications ORDER BY started_at DESC,request_key DESC LIMIT ?').all(limit)
      return checkedRows(rows)
    } finally { db.close() }
  }
  available(kind, text, settings, rows = this.rows(16384)) {
    const now = this.now(), hour = Math.floor((now + 8 * HOUR) / HOUR) % 24
    if (!Number.isSafeInteger(now) || now < 0) throw new Error('Trusted publication clock required')
    if (hour < 8) return { allowed: false, reason: 'autonomy_rest' }
    if (!settings.enabled || !(kind === 'post' ? settings.posts && settings.post_supported : settings.signature && settings.signature_supported)) return { allowed: false, reason: 'disabled_or_unsupported' }
    const relevant = rows.filter(row => row.kind === kind), unresolved = relevant.some(row => ['reserved', 'unknown'].includes(row.status))
    if (unresolved) return { allowed: false, reason: 'previous_publication_unconfirmed' }
    if (rows.length >= 16384) return { allowed: false, reason: 'publication_history_capacity' }
    const held = relevant.filter(row => ['accepted', 'reserved', 'unknown'].includes(row.status))
    const last = held[0], interval = (kind === 'post' ? settings.postIntervalHours : settings.signatureIntervalHours) * HOUR
    const next = Math.max(last ? last.started_at + interval : 0,
      ...relevant.filter(row => row.status === 'rejected').map(row => row.started_at + 15 * 60_000))
    if (now < next) return { allowed: false, reason: 'publication_interval', next_allowed_at: next }
    const start = Math.floor((now + 8 * HOUR) / DAY) * DAY - 8 * HOUR
    if (held.filter(row => row.started_at >= start).length >= (kind === 'post' ? settings.dailyPosts : settings.dailySignatures)) return { allowed: false, reason: 'publication_daily_limit' }
    if (text !== undefined && held.some(row => row.content_hash === digest(text) &&
        (kind === 'signature' ? row === last : now - row.started_at < 7 * DAY))) return { allowed: false, reason: 'same_account_update' }
    return { allowed: true, reason: 'available' }
  }
  run(request, settings, assertCurrent, transmit) {
    if (!plain(request) || !/^[a-f0-9]{64}$/.test(request.key) || !Object.hasOwn(ACTIONS, request.kind) ||
        !(request.kind === 'post' ? ['friends', 'self'].includes(request.audience) : request.audience === 'profile')) throw new Error('Invalid account reservation')
    accountPublicationText(request.text, request.kind)
    let db = this.open(true), acknowledgement
    try {
      db.exec('BEGIN IMMEDIATE')
      const prior = db.prepare('SELECT * FROM publications WHERE request_key=?').get(request.key)
      if (prior) throw new Error('This account update was already attempted; never resubmit it')
      const rows = checkedRows(db.prepare('SELECT * FROM publications ORDER BY started_at DESC,request_key DESC LIMIT 16384').all())
      const ready = this.available(request.kind, request.text, settings, rows)
      if (!ready.allowed) throw new Error(ready.reason)
      assertCurrent()
      db.prepare('INSERT INTO publications(request_key,kind,content,content_hash,started_at,status,audience) VALUES(?,?,?,?,?,?,?)')
        .run(request.key, request.kind, request.text, digest(request.text), this.now(), 'reserved', request.audience)
      db.exec('COMMIT')
      // The durable account reservation exists before this synchronous edge.
      try { assertCurrent(); acknowledgement = Promise.resolve(transmit()) }
      catch (error) { acknowledgement = Promise.reject(error) }
    } catch (error) { try { db.exec('ROLLBACK') } catch { /* retain original failure */ } throw error }
    finally { db.close() }
    return acknowledgement.then(value => {
      const envelope = plain(value) && ('status' in value || 'retcode' in value)
      const rejected = envelope && (value.status !== 'ok' || value.retcode !== 0)
      const data = envelope ? value.data : value
      const valid = !rejected && plain(data) && (request.kind === 'post'
        ? typeof data.tid === 'string' && data.tid.length > 0 && data.tid.length <= 256
        : data.result === undefined || data.result === 0)
      this.finish(request.key, rejected ? 'rejected' : valid ? 'accepted' : 'unknown',
        valid && request.kind === 'post' ? data.tid : null, rejected ? 'backend_rejected' : valid ? null : 'unrecognized_receipt')
      if (!valid) throw new Error(rejected ? 'Publication rejected by backend' : 'Publication outcome unconfirmed; do not retry')
      return value
    }, error => { this.finish(request.key, 'unknown', null, 'transport_unconfirmed'); throw error })
  }
  finish(key, state, receipt, reason) {
    const db = this.open(true)
    try {
      const result = db.prepare("UPDATE publications SET status=?,completed_at=?,receipt_id=?,reason=? WHERE request_key=? AND status='reserved'")
        .run(state, this.now(), receipt, reason, key)
      if (result.changes !== 1) throw new Error('Account receipt could not be durably saved; do not retry')
    } finally { db.close() }
  }
}

export class AccountActivity {
  constructor(adapter) { this.adapter = adapter }
  definitions() {
    return [{ name: 'qq_account_activity',
      description: '由当前配置的主人私聊主 agent 自主决定自己的说说或个性签名。status只读本机状态；post发表一条原创短说说，signature更新短签名。text不含私聊/记忆原文、账号、路径、凭据或未获授权的他人信息；basis只能是original_reflection或public_activity，标签不是事实证明。使用同一主LLM，无额外模型。可不发；静默/休息/预算/冷却仍有效。一轮最多一次，默认不另发QQ回执文字；未知结果不重试。',
      parameters: { action: { type: 'string', required: true, enum: ['status', 'post', 'signature'] },
        text: { type: 'string' }, basis: { type: 'string', enum: ['original_reflection', 'public_activity'] } },
      output: { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
      isConcurrencySafe: () => false, execute: (args, exec) => this.execute(args, exec) }]
  }
  async context(exec) {
    const a = this.adapter, record = a.captureExec(exec), scope = record.binding.scope
    const owner = await a.trustedOwner(scope)
    if (record.binding.owner && !ownerSame(owner, record.binding.owner)) throw new Error('Account activity owner changed')
    const guide = await a.runtimeCapabilities(scope, owner)
    a.validateOwnerContext(guide, 'dsh-runtime-capabilities/v1', scope, owner)
    if (guide.read_only !== true || guide.tools?.entertainment?.primary_identity_valid !== true ||
        guide.tools.entertainment.primary_internal_leisure !== true) throw new Error('Account updates belong to the configured primary private agent')
    const selected = a.backendGuard.selection()
    if (!/^[1-9][0-9]{0,19}$/.test(selected.expectedAccount || '') || !path.isAbsolute(selected.workspace)) throw new Error('Verified account configuration required')
    const workspace = process.platform === 'win32' ? selected.workspace.toLowerCase() : selected.workspace
    const binding = digest(workspace, selected.provider, selected.expectedAccount)
    const store = new AccountActivityStore(path.join(selected.workspace, 'napcat', 'account-activity', binding + '.sqlite'), binding, () => a.now())
    return { a, record, scope, owner, selected, store, settings: accountActivitySettings(selected.project, selected.capabilities) }
  }
  async execute(args, exec) {
    if (!plain(args) || !['status', 'post', 'signature'].includes(args.action) ||
        Object.keys(args).some(key => !(args.action === 'status' ? ['action'] : ['action', 'text', 'basis']).includes(key))) throw new Error('Invalid account activity request')
    const ctx = await this.context(exec), { a, record, scope, owner, selected, store, settings } = ctx
    const rows = store.rows()
    if (args.action === 'status') return { schema: 'dsh-account-activity/v1', read_only: true, settings,
      post: store.available('post', undefined, settings), signature: store.available('signature', undefined, settings),
      recent: rows.slice(0, 8).map(row => ({ kind: row.kind, text: row.content, state: row.status, at: row.started_at })) }
    if (!['original_reflection', 'public_activity'].includes(args.basis)) throw new Error('Choose an original reflection or verified public activity')
    const secretValues = Object.entries(a.deps.config || {}).filter(([key]) => /token|password|secret|cookie|api.?key/i.test(key)).map(([, value]) => value)
    const text = accountPublicationText(args.text, args.action, [selected.expectedAccount, a.deps.config?.botQQ,
      ...(a.deps.policy?.adminUsers || []), ...secretValues])
    const ready = store.available(args.action, text, settings)
    if (!ready.allowed) return { ok: false, accepted: false, reason: ready.reason, next_allowed_at: ready.next_allowed_at ?? null }
    if (record.accountActivityAttempt || record.inlineTextAttempted || record.outcomes.length || record.inFlight.size) return { ok: false, accepted: false, reason: 'another_turn_action_already_attempted' }
    if (!record.decision) {
      const choice = await a.decide({ verdict: 'reply', expression: 'publication' }, exec)
      if (!choice.accepted || !choice.can_send) return { ok: false, accepted: false, reason: choice.reason || 'publication_refused' }
    }
    if (record.decision?.verdict !== 'reply' || ![undefined, 'publication'].includes(record.decision.expression)) return { ok: false, accepted: false, reason: 'existing_decision_does_not_allow_publication' }
    const freshOwner = await a.trustedOwner(scope)
    if (!ownerSame(owner, freshOwner) || a.captureExec(exec) !== record || exec.signal?.aborted) throw new Error('Account activity turn changed')
    record.decision = { ...record.decision, expression: 'publication' }
    record.accountActivityAttempt = true; record.shadow.loopPending = null
    const operationId = digest('tool', exec.agent.session.id, exec.callId)
    const payload = args.action === 'post' ? { content: text, ugc_right: settings.audience === 'friends' ? 4 : 64 } : { longNick: text }
    const key = digest(selected.stamp, owner.workspace_key, owner.agent_key, scope, operationId)
    const authority = Object.freeze({ operationId, action: ACTIONS[args.action], payloadHash: digest(JSON.stringify(payload)),
      invoke: transmit => store.run({ key, kind: args.action, text, audience: args.action === 'post' ? settings.audience : 'profile' }, settings, () => {
        const current = a.backendGuard.selection()
        if (Math.floor((a.now() + 8 * HOUR) / HOUR) % 24 < 8 || a.captureExec(exec) !== record || exec.signal?.aborted || current.stamp !== selected.stamp ||
            current.expectedAccount !== selected.expectedAccount ||
            JSON.stringify(accountActivitySettings(current.project, current.capabilities)) !== JSON.stringify(settings) ||
            !a.backendGuard.edge(a.deps.connection, record.binding.backendStamp)) throw new Error('Account publication binding changed')
      }, transmit) })
    record.accountActivityAuthority = authority
    try {
      await a.runTool('qq_send_account_activity', {}, exec, () => a.bridge.guardedConnectionCall(authority.action, payload, scope))
      return { ok: true, accepted: true, action: args.action, audience: args.action === 'post' ? settings.audience : 'profile',
        client_visible_verified: false, receipt_boundary: 'backend_accepted', text }
    } finally {
      record.shadow.loopPending = null
      if (typeof exec.concludeTurn === 'function') exec.concludeTurn()
    }
  }
}

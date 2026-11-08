/** Explicit, one-shot archival of reviewed, unclaimed QQ inbox messages.
 * No model call, delivery, inferred binding, or budget mutation lives here.
 */
import fs from 'node:fs'
import path from 'node:path'
import { createHash } from 'node:crypto'
import { spawnSync } from 'node:child_process'

const PLAN = 'dsh-qq-inbox-recovery-plan/v1'
const RECORD = 'dsh-qq-inbox-recovery-record/v1'
const MAX_BYTES = 64 * 1024 * 1024
const attemptedPlans = new WeakMap()
const hex = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const hash = value => createHash('sha256').update(value).digest('hex')
const requireThat = (value, code) => { if (!value) throw new InboxRecoveryError(code) }

export class InboxRecoveryError extends Error {
  constructor(code) { super(code); this.name = 'InboxRecoveryError'; this.code = code }
}

function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']'
  if (plain(value)) return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}'
  requireThat(value === null || ['string', 'boolean', 'number'].includes(typeof value), 'non_json_recovery_evidence')
  requireThat(typeof value !== 'number' || Number.isFinite(value), 'non_json_recovery_number')
  return JSON.stringify(value)
}

export function recoveryMessageHash(message) { return hash(canonical(message)) }

function ordinary(filename, directory = false) {
  requireThat(typeof filename === 'string' && path.isAbsolute(filename) && !filename.includes('\0'), 'absolute_recovery_path_required')
  const resolved = path.resolve(filename)
  const equalPath = value => process.platform === 'win32' ? value.toLowerCase() : value
  requireThat(equalPath(fs.realpathSync.native(resolved)) === equalPath(resolved), 'recovery_path_alias_refused')
  for (let current = resolved; ; current = path.dirname(current)) {
    const info = fs.lstatSync(current)
    requireThat(!info.isSymbolicLink() && (current === resolved && !directory ? info.isFile() : info.isDirectory()), 'ordinary_recovery_path_required')
    if (current === path.dirname(current)) break
  }
  return resolved
}

function signature(info) {
  return [info.dev, info.ino, info.mode, info.size, info.mtimeNs, info.nlink].map(String).join(':')
}

function readStable(filename, maximum = MAX_BYTES) {
  const target = ordinary(filename)
  const before = fs.lstatSync(target, { bigint: true })
  requireThat(before.nlink === 1n && before.size <= BigInt(maximum), 'single_link_bounded_recovery_file_required')
  const descriptor = fs.openSync(target, fs.constants.O_RDONLY)
  try {
    requireThat(signature(fs.fstatSync(descriptor, { bigint: true })) === signature(before), 'recovery_file_identity_changed')
    const raw = fs.readFileSync(descriptor)
    requireThat(raw.length <= maximum && signature(fs.fstatSync(descriptor, { bigint: true })) === signature(before) &&
      signature(fs.lstatSync(target, { bigint: true })) === signature(before), 'recovery_file_changed_during_read')
    return { raw, sha256: hash(raw), identity: signature(before) }
  } finally { fs.closeSync(descriptor) }
}

/** An administrator's private audit directory, without reading any credential. */
export function recoveryDirectoryRecord(directory) {
  const target = ordinary(directory, true)
  const info = fs.lstatSync(target, { bigint: true })
  let security
  if (process.platform === 'win32') {
    const script = "$ErrorActionPreference='Stop';$a=[IO.Directory]::GetAccessControl($env:LANYA_RECOVERY_DIRECTORY);$u=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;$s=@($u,'S-1-5-18','S-1-5-32-544');$r=@($a.GetAccessRules($true,$false,[Security.Principal.SecurityIdentifier]));if(-not $a.AreAccessRulesProtected -or $r.Count -ne 3){throw 'private ACL required'};foreach($x in $r){if($x.IdentityReference.Value -notin $s -or $x.AccessControlType -ne 'Allow' -or $x.FileSystemRights -ne 'FullControl'){throw 'private ACL required'}};[Convert]::ToBase64String($a.GetSecurityDescriptorBinaryForm())"
    const executable = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
    const result = spawnSync(executable, ['-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(script, 'utf16le').toString('base64')],
      { windowsHide: true, timeout: 5000, maxBuffer: 8192, encoding: 'utf8', env: { ...process.env, LANYA_RECOVERY_DIRECTORY: target } })
    requireThat(!result.error && result.status === 0 && /^[A-Za-z0-9+/=]+$/.test(result.stdout.trim()), 'private_recovery_directory_acl_required')
    security = hash(Buffer.from(result.stdout.trim(), 'base64'))
  } else {
    requireThat((Number(info.mode) & 0o077) === 0 && info.uid === BigInt(process.getuid()), 'private_recovery_directory_required')
    security = hash([info.uid, info.gid, info.mode].map(String).join(':'))
  }
  const after = fs.lstatSync(target, { bigint: true })
  requireThat(info.dev === after.dev && info.ino === after.ino && info.mode === after.mode, 'recovery_directory_identity_changed')
  return { device: String(info.dev), inode: String(info.ino), mode: Number(info.mode), security_sha256: security }
}

function equal(left, right) { return canonical(left) === canonical(right) }
function snapshot(inbox) {
  requireThat(Array.isArray(inbox?.nextTurn) && Array.isArray(inbox?.nextStep) && typeof inbox.remove === 'function', 'native_inbox_required')
  const raw = canonical({ next_turn: inbox.nextTurn, next_step: inbox.nextStep })
  requireThat(Buffer.byteLength(raw) <= MAX_BYTES, 'recovery_queue_snapshot_too_large')
  return JSON.parse(raw)
}

function writeExclusive(directory, expectedDirectory, filename, value) {
  requireThat(equal(recoveryDirectoryRecord(directory), expectedDirectory), 'recovery_directory_drift')
  requireThat(/^[a-z0-9.-]+$/.test(filename), 'recovery_output_basename_required')
  const target = path.join(directory, filename), raw = Buffer.from(canonical(value) + '\n')
  requireThat(raw.length <= MAX_BYTES, 'recovery_archive_too_large')
  const descriptor = fs.openSync(target, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL, 0o600)
  try { fs.writeFileSync(descriptor, raw); fs.fsyncSync(descriptor) } finally { fs.closeSync(descriptor) }
  const saved = readStable(target)
  requireThat(saved.sha256 === hash(raw), 'recovery_archive_readback_failed')
  return { filename, sha256: saved.sha256, identity: saved.identity }
}

function validateSelected(plan, current) {
  const messages = current.next_turn
  requireThat(new Set(messages.map(message => message.id)).size === messages.length, 'ambiguous_native_pending_ids')
  for (const item of plan.selected) {
    const message = messages.find(value => value.id === item.id)
    if (message === undefined) continue
    requireThat(message.role === 'user' && message.source?.kind === 'plugin:dsh-onebot', 'foreign_pending_message_refused')
    requireThat(recoveryMessageHash(message) === item.sha256, 'reviewed_pending_message_changed')
  }
}

function nativeSequence(session) {
  requireThat(typeof session?.snapshotEvents === 'function', 'native_recovery_event_history_required')
  const events = session.snapshotEvents()
  requireThat(Array.isArray(events) && events.every(event => Number.isSafeInteger(event.seq)), 'native_recovery_event_history_invalid')
  return events
}

/** The caller already authenticated this real live binding and resolved its chat.
 * All queue checks and removals are synchronous in this one JavaScript turn.
 */
function recoverOnce({ config, chat, binding, log = () => {} }) {
  const input = config?.inboxRecovery
  if (input === undefined || input === null) return { configured: false, applied: false }
  requireThat(plain(input) && equal(Object.keys(input).sort(), ['planFile', 'planSha256']) && hex(input.planSha256), 'explicit_recovery_plan_required')
  const document = readStable(input.planFile)
  requireThat(document.sha256 === input.planSha256, 'recovery_plan_sha_mismatch')
  let plan
  try { plan = JSON.parse(document.raw.toString('utf8')) } catch { throw new InboxRecoveryError('invalid_recovery_plan_json') }
  requireThat(plain(plan) && equal(Object.keys(plan).sort(), ['archive_only', 'audit_directory', 'audit_identity', 'automatic_retry', 'format', 'operation_id', 'owner', 'scope', 'selected', 'session_id']) &&
    plan.format === PLAN && plan.archive_only === true && plan.automatic_retry === false && /^[a-f0-9]{32}$/.test(plan.operation_id) &&
    typeof plan.scope === 'string' && /^group:[1-9][0-9]*$/.test(plan.scope) && typeof plan.session_id === 'string' && plan.session_id.length > 0 &&
    Array.isArray(plan.selected) && plan.selected.length > 0 && plan.selected.length <= 512, 'reviewed_recovery_plan_required')
  if (binding.scope !== plan.scope) return { configured: true, applied: false, not_target_scope: true }
  requireThat(binding.scope === plan.scope && equal(binding.owner, plan.owner) &&
    chat?.agent?.session?.id === plan.session_id && chat.sessionId === plan.session_id, 'recovery_live_owner_session_mismatch')
  requireThat(plain(plan.owner) && ['cwd', 'preset', 'workspace_key', 'agent_key'].every(key => typeof plan.owner[key] === 'string') &&
    path.isAbsolute(plan.owner.cwd), 'trusted_recovery_owner_required')
  requireThat(plan.selected.every(item => plain(item) && equal(Object.keys(item).sort(), ['id', 'message', 'sha256']) && typeof item.id === 'string' && item.id.length > 0 && item.id.length <= 512 && hex(item.sha256) &&
    item.message?.id === item.id && item.message?.role === 'user' && item.message?.source?.kind === 'plugin:dsh-onebot' && recoveryMessageHash(item.message) === item.sha256) &&
    new Set(plan.selected.map(item => item.id)).size === plan.selected.length, 'exact_unique_recovery_selection_required')
  const directory = ordinary(plan.audit_directory, true)
  requireThat(path.dirname(path.resolve(input.planFile)) === directory && equal(recoveryDirectoryRecord(directory), plan.audit_identity), 'same_private_recovery_directory_required')
  const prefix = 'qq-inbox-' + plan.operation_id
  const intentPath = path.join(directory, prefix + '.intent.json'), resultPath = path.join(directory, prefix + '.result.json')
  if (fs.existsSync(resultPath)) {
    const previous = JSON.parse(readStable(resultPath).raw.toString('utf8'))
    requireThat(previous.format === RECORD && previous.plan_sha256 === document.sha256 && previous.operation_id === plan.operation_id &&
      previous.completed === true && previous.safe_to_dispatch === true && ['archived', 'archived_with_unresolved'].includes(previous.status) &&
      equal(previous.removed.concat(previous.no_longer_pending).map(item => item.id).sort(), plan.selected.map(item => item.id).sort()), 'recovery_previous_failure_no_retry')
    for (const reference of [previous.archive, previous.intent]) {
      requireThat(plain(reference) && /^[a-z0-9.-]+$/.test(reference.filename), 'recovery_receipt_reference_invalid')
      const actual = readStable(path.join(directory, reference.filename))
      requireThat(actual.sha256 === reference.sha256 && actual.identity === reference.identity, 'recovery_receipt_reference_drift')
    }
    return { configured: true, applied: false, already_completed: true, success: previous.success,
      removed_count: previous.removed_count, no_longer_pending_count: previous.no_longer_pending.length }
  }
  requireThat(!fs.existsSync(intentPath), 'recovery_intent_without_success_no_retry')
  const current = snapshot(chat.agent.inbox)
  const nativeBefore = nativeSequence(chat.agent.session)
  const nativeBeforeSeq = nativeBefore.length ? nativeBefore.at(-1).seq : -1
  const archive = writeExclusive(directory, plan.audit_identity, prefix + '.original.json', { format: RECORD, operation_id: plan.operation_id,
    plan_sha256: document.sha256, session_id: plan.session_id, scope: plan.scope, owner: plan.owner, observed_at: Date.now(),
    native_before_seq: nativeBeforeSeq, reviewed_originals: plan.selected, inbox: current })
  const intent = writeExclusive(directory, plan.audit_identity, prefix + '.intent.json', { format: RECORD, operation_id: plan.operation_id,
    plan_sha256: document.sha256, archive, selected: plan.selected, automatic_retry: false, archive_only: true })
  const result = { format: RECORD, operation_id: plan.operation_id, plan_sha256: document.sha256, intent, archive,
    success: false, applied: false, completed: false, safe_to_dispatch: false, mutation_attempted: false, mutation_may_have_occurred: false,
    removed_count: 0, removed: [], no_longer_pending: [], native_cancellations: [], native_before_seq: nativeBeforeSeq,
    native_disk_flush_verified: false, content_match: true, automatic_retry: false, model_requests: 0, send_requests: 0, budget_mutations: 0 }
  try {
    validateSelected(plan, current)
    for (const item of plan.selected) {
      const before = snapshot(chat.agent.inbox)
      const index = before.next_turn.findIndex(message => message.id === item.id)
      if (index < 0) {
        result.no_longer_pending.push({ id: item.id, sha256: item.sha256, status: 'unknown', reason: 'no_longer_pending' })
        continue
      }
      validateSelected({ ...plan, selected: [item] }, before)
      const priorEvents = nativeSequence(chat.agent.session)
      const priorSeq = priorEvents.length ? priorEvents.at(-1).seq : -1
      result.mutation_attempted = true
      result.mutation_may_have_occurred = true
      const removed = chat.agent.inbox.remove(item.id)
      requireThat(removed === true, 'native_remove_not_confirmed')
      result.applied = true
      result.removed.push({ id: item.id, sha256: item.sha256 })
      result.removed_count += 1
      const after = snapshot(chat.agent.inbox)
      requireThat(!after.next_turn.some(message => message.id === item.id) && equal(after.next_step, before.next_step) &&
        equal(after.next_turn, before.next_turn.filter(message => message.id !== item.id)), 'native_remove_scope_drift')
      const cancellations = nativeSequence(chat.agent.session).filter(event => event.seq > priorSeq && event.type === 'agent/inbox/spliced' && event.data?.outcome === 'canceled')
      requireThat(cancellations.length === 1 && cancellations[0].data.target === 'next-turn' && cancellations[0].data.start === index &&
        cancellations[0].data.removedCount === 1 && (!cancellations[0].data.inserted || cancellations[0].data.inserted.length === 0), 'native_cancellation_evidence_missing')
      result.native_cancellations.push({ seq: cancellations[0].seq, time: cancellations[0].time, target: 'next-turn', start: index,
        removed_count: 1, outcome: 'canceled', id: item.id, sha256: item.sha256 })
      result.mutation_may_have_occurred = false
    }
    result.success = result.no_longer_pending.length === 0
    result.completed = true
    result.safe_to_dispatch = true
    result.status = result.success ? 'archived' : 'archived_with_unresolved'
    result.final_queue = snapshot(chat.agent.inbox)
    const saved = writeExclusive(directory, plan.audit_identity, prefix + '.result.json', result)
    try { log('info', 'qq_inbox_recovery_archived:' + result.removed_count) } catch { /* Logging cannot repeat a remove. */ }
    return { configured: true, applied: result.applied, success: result.success, removed_count: result.removed_count,
      no_longer_pending_count: result.no_longer_pending.length, receipt_sha256: saved.sha256 }
  } catch (error) {
    result.status = result.mutation_attempted ? 'unknown' : 'refused'
    result.error_code = error instanceof InboxRecoveryError ? error.code : 'recovery_runtime_failure'
    result.content_match = result.error_code !== 'reviewed_pending_message_changed'
    try { result.final_queue = snapshot(chat.agent.inbox) } catch { result.final_queue_unavailable = true }
    try { writeExclusive(directory, plan.audit_identity, prefix + '.result.json', result) } catch { /* Existing intent retains unknown state. */ }
    throw error
  }
}

/** Cache one attempt per exact agent/session, owner, scope and pinned plan.
 * This is deliberately not keyed by the incoming message/revision: subsequent
 * live messages must never pay for another filesystem/PowerShell recovery.
 */
export function recoverPendingInbox(args = {}) {
  const input = args.config?.inboxRecovery
  if (input === undefined || input === null) return { configured: false, applied: false }
  if (args.binding?.origin !== 'live') return { configured: true, applied: false, not_live: true }
  const agent = args.chat?.agent
  requireThat(agent !== null && typeof agent === 'object', 'recovery_agent_required')
  const key = canonical({ session: agent.session?.id ?? null, chat_session: args.chat?.sessionId ?? null,
    owner: args.binding?.owner ?? null, scope: args.binding?.scope ?? null, planFile: input?.planFile ?? null,
    planSha256: input?.planSha256 ?? null })
  let cache = attemptedPlans.get(agent)
  if (!cache) { cache = new Map(); attemptedPlans.set(agent, cache) }
  const previous = cache.get(key)
  if (previous) {
    if (previous.error) throw new InboxRecoveryError(previous.error)
    return { ...previous.value, applied: false, cached: true }
  }
  try {
    const value = recoverOnce(args)
    cache.set(key, { value })
    return value
  } catch (error) {
    cache.set(key, { error: error instanceof InboxRecoveryError ? error.code : 'recovery_previous_runtime_failure_no_retry' })
    throw error
  }
}

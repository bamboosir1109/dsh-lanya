/** Native memento facts are archived in their own SQLite write transaction.
 * Recall counters are not facts. The patched Provider wraps only fact writes;
 * complete rows still include counters so restore cannot overwrite later use.
 * Archives are append-only and hash sealed, not a cryptographic trust service.
 */
import { createHash, randomUUID } from 'node:crypto'
import { DatabaseSync } from 'node:sqlite'

export const HISTORY_FORMAT = 'dsh-memento-native-history/v1'
export const HISTORY_TABLES = {
  batches: 'native_history_batches', rows: 'native_history_rows', audit: 'native_history_audit'
}
export function canonical(value) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return JSON.stringify(value)
  if (typeof value === 'number' && Number.isSafeInteger(value)) return JSON.stringify(value)
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']'
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}'
  throw new Error('Native history requires lossless JSON facts')
}
export const hash = value => createHash('sha256').update(typeof value === 'string' ? value : canonical(value)).digest('hex')
export function entrySchema(db) {
  return { columns: db.prepare('PRAGMA table_info(entries)').all(),
    sql: db.prepare("SELECT sql FROM sqlite_master WHERE type='table' AND name='entries'").get()?.sql }
}

function installNativeSchema(db) {
  db.exec(`CREATE TABLE IF NOT EXISTS native_history_batches (
    seq INTEGER PRIMARY KEY, batch_id TEXT NOT NULL UNIQUE, format TEXT NOT NULL,
    operation TEXT NOT NULL, created_at INTEGER NOT NULL, session_id TEXT,
    schema_json TEXT NOT NULL, row_count INTEGER NOT NULL CHECK(row_count>0),
    restores_batch_id TEXT UNIQUE, batch_hash TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS native_history_rows (
    batch_id TEXT NOT NULL, ordinal INTEGER NOT NULL, entry_id TEXT NOT NULL,
    before_json TEXT NOT NULL, after_json TEXT NOT NULL, before_sha256 TEXT NOT NULL,
    after_sha256 TEXT NOT NULL, result_version INTEGER NOT NULL,
    PRIMARY KEY(batch_id,ordinal), UNIQUE(batch_id,entry_id));
    CREATE INDEX IF NOT EXISTS native_history_entry ON native_history_rows(entry_id);
    CREATE TABLE IF NOT EXISTS native_history_audit (
    audit_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL UNIQUE, operation TEXT NOT NULL,
    created_at INTEGER NOT NULL, batch_hash TEXT NOT NULL, audit_hash TEXT NOT NULL);`)
  for (const table of Object.values(HISTORY_TABLES)) {
    for (const action of ['UPDATE', 'DELETE']) {
      db.exec(`CREATE TRIGGER IF NOT EXISTS ${table}_${action.toLowerCase()}_refused BEFORE ${action} ON ${table}
        BEGIN SELECT RAISE(ABORT,'Native history is append-only'); END`)
    }
  }
  const conflicts = {
    native_history_batches: "seq=NEW.seq OR batch_id=NEW.batch_id OR (NEW.restores_batch_id IS NOT NULL AND restores_batch_id=NEW.restores_batch_id)",
    native_history_rows: "batch_id=NEW.batch_id AND (ordinal=NEW.ordinal OR entry_id=NEW.entry_id)",
    native_history_audit: "audit_id=NEW.audit_id OR batch_id=NEW.batch_id"
  }
  for (const [table, conflict] of Object.entries(conflicts)) {
    db.exec(`CREATE TRIGGER IF NOT EXISTS ${table}_insert_conflict_refused BEFORE INSERT ON ${table}
      WHEN EXISTS(SELECT 1 FROM ${table} WHERE ${conflict})
      BEGIN SELECT RAISE(ABORT,'Native history existing identity cannot be replaced'); END`)
  }
}

function nativeSchemaIdentity(db) {
  return db.prepare("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name GLOB 'native_history_*' OR tbl_name IN ('native_history_batches','native_history_rows','native_history_audit') ORDER BY type,name").all()
    .map(row => ({ ...row, sql: row.sql === null ? null : row.sql.trim().replace(/\s+/g, ' ') }))
}
let expectedSchema
export function ensureNativeHistory(db) {
  if (!expectedSchema) {
    const model = new DatabaseSync(':memory:')
    try { installNativeSchema(model); expectedSchema = canonical(nativeSchemaIdentity(model)) }
    finally { model.close() }
  }
  const existing = nativeSchemaIdentity(db)
  if (!existing.length) installNativeSchema(db)
  if (canonical(nativeSchemaIdentity(db)) !== expectedSchema) throw new Error('Native history schema or immutable guards changed')
}

function validRow(row) {
  if (row === null) return
  const fields = ['id','track','scope','workspace_key','agent_key','text','source','tags','version','created_at','updated_at','last_recalled','recall_count','session_id']
  if (canonical(Object.keys(row).sort()) !== canonical(fields.sort()) ||
      ['id','track','scope','workspace_key','agent_key','text','source','tags'].some(key => typeof row[key] !== 'string') ||
      !(row.session_id === null || typeof row.session_id === 'string') ||
      ['created_at','updated_at','recall_count'].some(key => !Number.isSafeInteger(row[key]) || row[key] < 0) ||
      !(row.last_recalled === null || Number.isSafeInteger(row.last_recalled) && row.last_recalled >= 0)) {
    throw new Error('Invalid complete native SQL row')
  }
  if (typeof row.id !== 'string' || !row.id || !Number.isSafeInteger(row.version) || row.version < 1 ||
      !['user', 'agent'].includes(row.track) || !['workspace', 'user-global'].includes(row.scope) ||
      typeof row.workspace_key !== 'string' || typeof row.agent_key !== 'string') throw new Error('Invalid native history row identity')
  const tags = JSON.parse(row.tags)
  if (!Array.isArray(tags) || tags.some(tag => typeof tag !== 'string')) throw new Error('Invalid native history tags')
  canonical(row)
}

export function recordNativeBatch(db, operation, changes, { sessionId = null, restoresBatchId = null, now = Date.now } = {}) {
  if (!['add','seed','replace','remove','consolidate','native.restore'].includes(operation) ||
      !(sessionId === null || typeof sessionId === 'string') ||
      !(restoresBatchId === null || typeof restoresBatchId === 'string' && restoresBatchId) ||
      (operation === 'native.restore') !== (restoresBatchId !== null)) throw new Error('Invalid native history batch metadata')
  if (!changes.length) return null
  const batchId = randomUUID()
  const seen = new Set()
  const ordered = [...changes].sort((a,b) => {
    const first = (a.after || a.before)?.id, second = (b.after || b.before)?.id
    if (typeof first !== 'string' || typeof second !== 'string') throw new Error('Invalid native history row ID')
    return first < second ? -1 : first > second ? 1 : 0
  })
  const rows = ordered.map(({ before, after }, ordinal) => {
    validRow(before); validRow(after)
    const id = (after || before)?.id
    if (!id || seen.has(id) || before && after && before.id !== after.id) throw new Error('Invalid native history change set')
    if (before && after && (after.version <= before.version || canonical(before) === canonical(after))) throw new Error('Native fact version did not advance')
    seen.add(id)
    const version = after ? after.version : before.version + 1
    if (!Number.isSafeInteger(version)) throw new Error('Native history version exhausted')
    const prior = db.prepare('SELECT COUNT(*) AS count,COALESCE(MAX(result_version),0) AS version FROM native_history_rows WHERE entry_id=?').get(id)
    if (!Number.isSafeInteger(prior.version) || version <= prior.version ||
        !before && prior.count > 0 && operation !== 'native.restore') throw new Error('Native history identity reused or version did not advance')
    const beforeJson = canonical(before), afterJson = canonical(after)
    return { batch_id: batchId, ordinal, entry_id: id, before_json: beforeJson, after_json: afterJson,
      before_sha256: hash(beforeJson), after_sha256: hash(afterJson), result_version: version }
  })
  const header = { seq: db.prepare('SELECT COALESCE(MAX(seq),0)+1 AS seq FROM native_history_batches').get().seq,
    batch_id: batchId, format: HISTORY_FORMAT, operation, created_at: now(), session_id: sessionId,
    schema_json: canonical(entrySchema(db)), row_count: rows.length, restores_batch_id: restoresBatchId }
  if (!Number.isSafeInteger(header.seq) || header.seq < 1 || !Number.isSafeInteger(header.created_at) || header.created_at < 0) throw new Error('Invalid native history sequence/time')
  const batchHash = hash({ header, rows })
  db.prepare('INSERT INTO native_history_batches VALUES (?,?,?,?,?,?,?,?,?,?)').run(...Object.values(header), batchHash)
  const insert = db.prepare('INSERT INTO native_history_rows VALUES (?,?,?,?,?,?,?,?)')
  for (const row of rows) insert.run(...Object.values(row))
  const audit = { audit_id: randomUUID(), batch_id: batchId, operation, created_at: header.created_at, batch_hash: batchHash }
  db.prepare('INSERT INTO native_history_audit VALUES (?,?,?,?,?,?)').run(...Object.values(audit), hash(audit))
  return batchId
}

export function withNativeHistory(db, operation, fn, sessionId = null) {
  db.exec('BEGIN IMMEDIATE')
  try {
    ensureNativeHistory(db)
    const previous = new Map(db.prepare('SELECT * FROM entries ORDER BY id').all().map(row => [row.id, row]))
    const result = fn()
    if (result && typeof result.then === 'function') throw new Error('Native write must remain synchronous')
    const current = new Map(db.prepare('SELECT * FROM entries ORDER BY id').all().map(row => [row.id, row]))
    const changes = [...new Set([...previous.keys(), ...current.keys()])].sort().map(id => ({ before: previous.get(id) || null, after: current.get(id) || null }))
      .filter(({ before, after }) => canonical(before) !== canonical(after))
    recordNativeBatch(db, operation, changes, { sessionId })
    db.exec('COMMIT')
    return result
  } catch (error) {
    try { db.exec('ROLLBACK') } catch {}
    throw error
  }
}

export function requireNativeTarget(target, expected) {
  // Direct Provider management calls may omit a session. The native model
  // protocol always supplies the complete target it actually authorized.
  if (expected !== undefined && canonical(target) !== canonical(expected)) {
    throw new Error('Native memory target changed after authorization')
  }
}

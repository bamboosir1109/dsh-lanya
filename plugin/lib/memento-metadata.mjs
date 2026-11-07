/** Additive annotations inside the existing fact/history transaction.
 * Entries v4 and the older native-history compiler remain byte unchanged.
 * Weights are author annotations, never calibrated evidence or write authority.
 */
import { canonical, hash, withNativeHistory } from './memento-history.mjs'

export const METADATA_FORMAT = 'dsh-memory-metadata/v1'
export const METADATA_PARAMETER = {
  type:'object', additionalProperties:false, description:'Optional annotations for add/replace/consolidate under the same approval. Choose a concrete topic title, not a repeated first-person preamble. Event dates must be supported by evidence; omit unknown dates. Weights 0..100 are your uncalibrated judgments, never verified facts. Trusted live configuration takes precedence over remembered rules. Correct or consolidate explicitly; never silently erase contrary facts. clear_fields explicitly resets unknown values.',
  properties: {
    title:{type:'string'}, kind:{type:'string',enum:['fact','preference','event','rule','reflection','uncertain']},
    occurred_at:{type:'string',description:'Known event date YYYY-MM-DD or ISO timestamp with timezone; never the maintenance date unless it is the event date.'},
    valid_from:{type:'string'},valid_until:{type:'string'},importance:{type:'integer'},confidence:{type:'integer'},
    status:{type:'string',enum:['active','uncertain','superseded']},
    links:{type:'array',items:{type:'object',additionalProperties:false,properties:{entry_id:{type:'string',required:true},version:{type:'integer',required:true},relation:{type:'string',required:true,enum:['corrects','consolidates','updates']}}}},
    clear_fields:{type:'array',items:{type:'string',enum:['title','occurred_at','valid_from','valid_until','importance','confidence']}}
  }
}
export const METADATA_OUTPUT = {type:'object',additionalProperties:false,properties:{
  format:{type:'string'},...Object.fromEntries(Object.entries(METADATA_PARAMETER.properties).filter(([key])=>key !== 'clear_fields'))
}}
export const publicMetadata = metadata => Object.fromEntries(Object.entries(metadata).filter(([,value])=>value !== null))
const OWNER = ['track', 'scope', 'workspace_key', 'agent_key']
const FACT = ['id', ...OWNER, 'text', 'source', 'tags', 'version', 'created_at', 'updated_at', 'session_id']
const TABLE = 'memory_metadata_revisions'
const DDL = `CREATE TABLE memory_metadata_revisions (
entry_id TEXT NOT NULL, version INTEGER NOT NULL, track TEXT NOT NULL,
scope TEXT NOT NULL, workspace_key TEXT NOT NULL, agent_key TEXT NOT NULL,
fact_sha256 TEXT NOT NULL, metadata_json TEXT NOT NULL, metadata_sha256 TEXT NOT NULL,
recorded_at INTEGER NOT NULL, PRIMARY KEY(entry_id,version))`
const TRIGGERS = {
  memory_metadata_update_refused: "CREATE TRIGGER memory_metadata_update_refused BEFORE UPDATE ON memory_metadata_revisions BEGIN SELECT RAISE(ABORT,'Memory metadata is append-only'); END",
  memory_metadata_delete_refused: "CREATE TRIGGER memory_metadata_delete_refused BEFORE DELETE ON memory_metadata_revisions BEGIN SELECT RAISE(ABORT,'Memory metadata is append-only'); END",
  memory_metadata_replace_refused: "CREATE TRIGGER memory_metadata_replace_refused BEFORE INSERT ON memory_metadata_revisions WHEN EXISTS(SELECT 1 FROM memory_metadata_revisions WHERE entry_id=NEW.entry_id AND version=NEW.version) BEGIN SELECT RAISE(ABORT,'Memory metadata identity cannot be replaced'); END"
}
function date(value) {
  if (value === null) return null
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2}))?$/.test(value)) throw new Error('Event dates require an explicit ISO date or zoned timestamp')
  const day = value.slice(0, 10), parsedDay = new Date(day + 'T00:00:00Z')
  const parsed = Date.parse(value.length === 10 ? value + 'T00:00:00Z' : value)
  if (day.startsWith('0000-') || !Number.isFinite(parsed) || !Number.isFinite(parsedDay.getTime()) || parsedDay.toISOString().slice(0, 10) !== day || value.slice(11, 13) === '24') throw new Error('Invalid event date')
  return parsed
}
export function normalizeMetadata(value, previous = null) {
  const result = { format: METADATA_FORMAT, title: null, kind: 'uncertain', occurred_at: null,
    valid_from: null, valid_until: null, importance: null, confidence: null, status: 'active', links: [], ...(previous || {}) }
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).some(key => key !== 'clear_fields' && !Object.hasOwn(result, key))) throw new Error('Unknown memory metadata fields')
  const clear = value.clear_fields ?? []
  if (!Array.isArray(clear) || new Set(clear).size !== clear.length || clear.some(key=>!['title','occurred_at','valid_from','valid_until','importance','confidence'].includes(key) || Object.hasOwn(value,key))) throw new Error('Invalid annotation fields to clear')
  Object.assign(result, Object.fromEntries(Object.entries(value).filter(([key])=>key !== 'clear_fields')))
  for (const key of clear) result[key] = null
  if (result.format !== METADATA_FORMAT) throw new Error('Unsupported memory metadata format')
  if (result.title !== null) {
    if (typeof result.title !== 'string' || !result.title.trim() || Array.from(result.title).length > 80 || /[\x00-\x1f]/.test(result.title)) throw new Error('Title requires 1 to 80 printable characters')
    result.title = result.title.trim()
  }
  if (!['fact','preference','event','rule','reflection','uncertain'].includes(result.kind) || !['active','uncertain','superseded'].includes(result.status)) throw new Error('Invalid memory metadata classification')
  for (const key of ['importance','confidence']) if (!(result[key] === null || Number.isSafeInteger(result[key]) && result[key] >= 0 && result[key] <= 100)) throw new Error('Annotation weights require integers from 0 to 100 or null')
  for (const key of ['occurred_at','valid_from','valid_until']) date(result[key])
  if (result.valid_from && result.valid_until && date(result.valid_from) > date(result.valid_until)) throw new Error('Validity interval is reversed')
  if (!Array.isArray(result.links) || result.links.length > 32) throw new Error('Memory links require at most 32 exact references')
  const seen = new Set()
  for (const link of result.links) {
    if (!link || canonical(Object.keys(link).sort()) !== canonical(['entry_id','relation','version']) ||
        typeof link.entry_id !== 'string' || !link.entry_id || Array.from(link.entry_id).length > 256 || /[\x00-\x1f]/.test(link.entry_id) ||
        !Number.isSafeInteger(link.version) || link.version < 1 || !['updates','corrects','consolidates'].includes(link.relation)) throw new Error('Invalid exact memory relationship')
    const key = canonical(link)
    if (seen.has(key)) throw new Error('Duplicate memory relationship')
    seen.add(key)
  }
  if (Buffer.byteLength(canonical(result)) > 8192) throw new Error('Memory annotation exceeds bounded size')
  return JSON.parse(canonical(result))
}
export const metadataFactHash = row => hash(Object.fromEntries(FACT.map(key => [key, row[key] ?? null])))
const schema = db => db.prepare("SELECT name,sql FROM sqlite_master WHERE name=? OR tbl_name=? AND type='trigger'").all(TABLE,TABLE)
  .reduce((result,row) => ({...result,[row.name]:row.sql.trim().replace(/\s+/g,' ')}), {})
export function ensureMetadata(db, create = false) {
  let found = schema(db)
  if (!Object.keys(found).length && !create) return false
  if (!Object.keys(found).length) { db.exec(DDL); for (const sql of Object.values(TRIGGERS)) db.exec(sql); found = schema(db) }
  const expected = Object.fromEntries(Object.entries({[TABLE]:DDL,...TRIGGERS}).map(([key,value])=>[key,value.trim().replace(/\s+/g,' ')]))
  if (canonical(found) !== canonical(expected)) throw new Error('Memory metadata schema or immutable guards changed')
  return true
}
export function readMetadata(db, row) {
  if (!ensureMetadata(db)) return null
  const record = db.prepare(`SELECT * FROM ${TABLE} WHERE entry_id=? AND version=?`).get(row.id,row.version)
  if (!record || OWNER.some(key => record[key] !== row[key]) || record.fact_sha256 !== metadataFactHash(row)) return null
  let value
  try { value = JSON.parse(record.metadata_json) } catch { throw new Error('Memory annotation integrity check failed') }
  if (hash(record.metadata_json) !== record.metadata_sha256 || canonical(normalizeMetadata(value)) !== canonical(value)) throw new Error('Memory annotation integrity check failed')
  return value
}
export function metadataMatches(db,row,query) {
  if (!query) return true
  const needle = query.toLowerCase()
  if (row.text.toLowerCase().includes(needle)) return true
  const metadata = readMetadata(db,row)
  const searchable = [metadata?.title,metadata?.occurred_at,metadata?.valid_from,metadata?.valid_until]
  // These are explicitly record times in the query result, never event times.
  for (const key of ['created_at','updated_at']) if (Number.isSafeInteger(row[key]) && row[key] >= 0 && row[key] <= 8640000000000000) searchable.push(new Date(row[key]).toISOString())
  return searchable.some(value=>typeof value === 'string' && value.toLowerCase().includes(needle))
}
function writeMetadata(db, row, value, previous, targets) {
  const data = normalizeMetadata(value, previous)
  for (const link of data.links) {
    // Inherited links were checked when their exact previous annotation was
    // committed. New references must still match a current approved target.
    if (previous?.links.some(old => canonical(old) === canonical(link))) continue
    const target = targets.get(link.entry_id) || db.prepare('SELECT * FROM entries WHERE id=?').get(link.entry_id)
    if (!target || target.version !== link.version || OWNER.some(key => target[key] !== row[key])) throw new Error('Memory relationship target changed or belongs to another owner')
  }
  ensureMetadata(db,true)
  const raw = canonical(data)
  db.prepare(`INSERT INTO ${TABLE} VALUES (?,?,?,?,?,?,?,?,?,?)`).run(row.id,row.version,...OWNER.map(key=>row[key]),metadataFactHash(row),raw,hash(raw),row.updated_at)
  return data
}
export function annotatedEntry(db, entry) {
  if (!entry) return entry
  const row = db.prepare('SELECT * FROM entries WHERE id=?').get(entry.id)
  if (!row || row.version !== entry.version || row.track !== entry.track || row.scope !== entry.scope || row.workspace_key !== entry.workspaceKey || row.agent_key !== entry.agentKey || row.text !== entry.text) return {...entry,metadata:null}
  return {...entry,lastRecalled:row.last_recalled,recallCount:row.recall_count,metadata:readMetadata(db,row)}
}
export function withMetadataHistory(db, operation, fn, sessionId = null, metadata = undefined) {
  // Normalize before any write. The original wrapper owns BEGIN/COMMIT and
  // rolls both annotations and native fact history back on every failure.
  if (metadata !== undefined) normalizeMetadata(metadata)
  return withNativeHistory(db,operation,()=>{
    const previousRows = new Map(db.prepare('SELECT * FROM entries ORDER BY id').all().map(row=>[row.id,row]))
    const result = fn()
    if (result && typeof result.then === 'function') throw new Error('Native write must remain synchronous')
    const currentRows = db.prepare('SELECT * FROM entries ORDER BY id').all()
    const consolidated = operation === 'consolidate' ? [...previousRows.values()].filter(oldRow=>!currentRows.some(current=>current.id===oldRow.id)) : []
    for (const row of currentRows) {
      const before = previousRows.get(row.id)
      if (before && canonical(before) === canonical(row)) continue
      const old = before ? readMetadata(db,before) : null
      if (metadata === undefined && !old && !consolidated.some(original=>readMetadata(db,original))) continue
      let value = normalizeMetadata(metadata === undefined ? {} : metadata,old)
      if (before) value.links = [...value.links.filter(link=>link.relation !== 'updates'),{entry_id:before.id,version:before.version,relation:'updates'}]
      if (operation === 'consolidate') {
        value.links = [...value.links,...consolidated.map(oldRow=>({entry_id:oldRow.id,version:oldRow.version,relation:'consolidates'}))]
      }
      writeMetadata(db,row,value,old,previousRows)
    }
    if (Array.isArray(result)) return result.map(entry=>annotatedEntry(db,entry))
    if (result?.entry) return {...result,entry:annotatedEntry(db,result.entry)}
    return result?.id ? annotatedEntry(db,result) : result
  },sessionId)
}

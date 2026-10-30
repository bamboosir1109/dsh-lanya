/** Trusted local evidence producer. It never calls a model or a transport. */
import fs from 'node:fs'
import path from 'node:path'
import { createHash, randomUUID } from 'node:crypto'
import { observationKey, validateObservation } from './observation-queue.js'

const hash = (raw) => createHash('sha256').update(raw).digest('hex')
function ordinary(directory) {
  const absolute = path.resolve(directory)
  const root = path.parse(absolute).root
  let current = root
  for (const part of absolute.slice(root.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, part)
    try { fs.mkdirSync(current) } catch (error) { if (error.code !== 'EEXIST') throw error }
    const info = fs.lstatSync(current)
    if (!info.isDirectory() || info.isSymbolicLink()) throw new Error('Observation directory must be ordinary')
  }
  return absolute
}
function read(file) {
  const before = fs.lstatSync(file, { bigint: true })
  if (!before.isFile() || before.isSymbolicLink() || before.size > 512n * 1024n) throw new Error('Invalid observation file')
  const raw = fs.readFileSync(file)
  const after = fs.lstatSync(file, { bigint: true })
  if (['dev', 'ino', 'size', 'mtimeNs'].some((key) => before[key] !== after[key])) throw new Error('Observation changed during read')
  return { raw, value: JSON.parse(raw.toString('utf8')) }
}
function stable(value) {
  if (Array.isArray(value)) return '[' + value.map(stable).join(',') + ']'
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map((key) => JSON.stringify(key) + ':' + stable(value[key])).join(',') + '}'
  return JSON.stringify(value)
}
function semantic(event) { const { created_at, ...rest } = event; return stable(rest) }
function assertJson(value) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return
  if (typeof value === 'number' && Number.isFinite(value)) return
  if (Array.isArray(value)) { value.forEach(assertJson); return }
  if (value && Object.getPrototypeOf(value) === Object.prototype) { Object.values(value).forEach(assertJson); return }
  throw new Error('Observation payload must be JSON data')
}

export function readObservationCompletion(directory, scope, eventId) {
  const base = ordinary(directory)
  const key = observationKey(scope, eventId)
  const done = path.join(ordinary(path.join(base, 'done')), key + '.json')
  const pending = path.join(ordinary(path.join(base, 'pending')), key + '.json')
  if (!fs.existsSync(done)) return null
  const event = read(pending)
  validateObservation(event.value)
  const marker = read(done).value
  if (event.value.scope !== scope || event.value.event_id !== eventId || marker.version !== 1 ||
      marker.event_sha256 !== hash(event.raw) || !['decided', 'sent', 'unknown', 'suppressed'].includes(marker.status) ||
      typeof marker.decision_id !== 'string' || !marker.decision_id.trim() || marker.decision_id.length > 256 ||
      /[\x00-\x1f]/.test(marker.decision_id) ||
      ('next_review_at' in marker && (!Number.isSafeInteger(marker.next_review_at) || marker.next_review_at <= event.value.created_at)) ||
      ('completed_at' in marker && (!Number.isSafeInteger(marker.completed_at) || marker.completed_at < 0)) ||
      ('scope' in marker && marker.scope !== scope) || ('event_id' in marker && marker.event_id !== eventId)) {
    throw new Error('Invalid observation completion')
  }
  return marker
}

export function publishObservation(directory, input) {
  const event = validateObservation({ ...input, version: 1, origin: 'observation', control_allowed: false })
  assertJson(event)
  const serialized = JSON.stringify(event)
  if (Buffer.byteLength(serialized, 'utf8') > 256 * 1024) throw new Error('Observation too large')
  // JSON.stringify must not silently turn NaN/Infinity into user evidence.
  if (stable(JSON.parse(serialized)) !== stable(event)) throw new Error('Observation payload must be JSON data')
  const base = ordinary(directory)
  const pending = ordinary(path.join(base, 'pending'))
  ordinary(path.join(base, 'done'))
  const key = observationKey(event.scope, event.event_id)
  const target = path.join(pending, key + '.json')
  const existing = () => {
    const previous = validateObservation(read(target).value)
    if (semantic(previous) !== semantic(event)) throw new Error('Observation identity conflicts with existing evidence')
    return { submitted: false, state: readObservationCompletion(base, event.scope, event.event_id) ? 'done' : 'pending', key, event_id: event.event_id }
  }
  if (fs.existsSync(target)) return existing()
  if (fs.existsSync(path.join(base, 'done', key + '.json'))) throw new Error('Completion has no original evidence')
  const temporary = path.join(pending, '.publish-' + randomUUID() + '.tmp')
  const fd = fs.openSync(temporary, 'wx', 0o600)
  try {
    fs.writeFileSync(fd, serialized + '\n', 'utf8')
    fs.fsyncSync(fd)
  } finally { fs.closeSync(fd) }
  try {
    try { fs.linkSync(temporary, target) } catch (error) { if (error.code === 'EEXIST') return existing(); throw error }
    return { submitted: true, state: 'pending', key, event_id: event.event_id }
  } finally { fs.unlinkSync(temporary) }
}

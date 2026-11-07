/** Trusted local evidence producer. It never calls a model or a transport. */
import fs from 'node:fs'
import path from 'node:path'
import { randomUUID } from 'node:crypto'
import { observationKey, validateObservation, readObservationEvidence, MAX_OBSERVATION_BYTES, ObservationQueueError } from './observation-queue.js'

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
  if (!present(done)) return null
  try { return readObservationEvidence(base, key).completion } catch (error) {
    if (error instanceof ObservationQueueError) throw new Error('Invalid observation completion', { cause: error })
    throw error
  }
}

function present(file) {
  try { fs.lstatSync(file); return true } catch (error) { if (error.code !== 'ENOENT') throw error; return false }
}

export function publishObservation(directory, input) {
  const event = validateObservation({ ...input, version: 1, origin: 'observation', control_allowed: false })
  assertJson(event)
  const serialized = JSON.stringify(event)
  if (Buffer.byteLength(serialized + '\n', 'utf8') > MAX_OBSERVATION_BYTES) throw new Error('Observation too large')
  // JSON.stringify must not silently turn NaN/Infinity into user evidence.
  if (stable(JSON.parse(serialized)) !== stable(event)) throw new Error('Observation payload must be JSON data')
  const base = ordinary(directory)
  const pending = ordinary(path.join(base, 'pending'))
  ordinary(path.join(base, 'done'))
  const key = observationKey(event.scope, event.event_id)
  const target = path.join(pending, key + '.json')
  const existing = () => {
    const previous = readObservationEvidence(base, key).value
    if (semantic(previous) !== semantic(event)) throw new Error('Observation identity conflicts with existing evidence')
    return { submitted: false, state: readObservationCompletion(base, event.scope, event.event_id) ? 'done' : 'pending', key, event_id: event.event_id }
  }
  if (present(target) || present(path.join(base, 'done', key + '.json'))) return existing()
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

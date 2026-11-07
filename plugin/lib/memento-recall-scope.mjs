/** Current logical-session raw history only. Long-term memento facts are unchanged. */
import path from 'node:path'

const unavailable = error => ({ available: false, sessions: [], error })
const empty = () => ({ available: true, sessions: [] })
const presetKey = value => value === undefined ? ''
  : typeof value === 'string' ? (process.platform === 'win32' ? value.trim().toLowerCase() : value.trim()) : null
const workspaceKey = value => process.platform === 'win32' ? path.resolve(value).toLowerCase() : path.resolve(value)

export function assertRecallNotAborted(signal) {
  if (signal?.aborted) {
    const error = new Error('HistoryCancelled')
    error.name = 'AbortError'
    throw error
  }
}

/** Call only with exec.agent.session, never model args or message metadata. */
export function snapshotRecallOwner(session) {
  try {
    const id = session?.id, header = session?.header
    const cwd = header?.cwd, preset = presetKey(header?.agentPreset)
    if (typeof id !== 'string' || !id.trim() || id !== id.trim() || !header || typeof header !== 'object' || Array.isArray(header)
      || (header.id !== undefined && header.id !== id) || typeof cwd !== 'string' || !cwd.trim()
      || !path.isAbsolute(cwd) || preset === null) return null
    return Object.freeze({ id, cwd, workspace: workspaceKey(cwd), preset })
  } catch { return null }
}

function sameHeader(header, owner) {
  if (!header || typeof header !== 'object' || Array.isArray(header)) return false
  const id = header.id, cwd = header.cwd, preset = presetKey(header.agentPreset)
  return id === owner.id && typeof cwd === 'string' && path.isAbsolute(cwd)
    && workspaceKey(cwd) === owner.workspace && preset !== null && preset === owner.preset
}

function localPattern(query) {
  if (typeof query !== 'string' || !query.trim()) return null
  return new RegExp(query.trim().split(/\s+/u)
    .map(part => part.replace(/[.*+?^${}()|[\]\\]/gu, '\\$&')).join('\\s+'), 'iu')
}

const joined = parts => parts.filter(part => typeof part === 'string').map(part => part.trim()).filter(Boolean).join('\n')
function contentSearchText(content) {
  if (!Array.isArray(content)) return ''
  return joined(content.flatMap(block => block?.type === 'text' ? [block.text]
    : block?.type === 'tool-call' ? [block.name, block.arguments] : []))
}

/** Match the host's first-party semantic surface, not request envelopes or reasoning. */
export function historyEventSearchText(event) {
  const data = event?.data
  if (!data || typeof data !== 'object') return ''
  switch (event.type) {
    case 'user/message': return contentSearchText(data.content)
    case 'assistant/message': return contentSearchText(data.message?.content)
    case 'tool/call': return joined([data.name, data.arguments])
    case 'tool/result': return joined([contentSearchText(data.message?.content), data.error?.name, data.error?.code])
    case 'todo/write': return Array.isArray(data.todos) ? joined(data.todos.flatMap(todo => [todo?.status, todo?.content])) : ''
    case 'turn/end': {
      const reason = data.reason
      if (reason?.kind === 'error') return joined(['error', reason.error?.message])
      return ['aborted', 'max-tokens', 'interrupted'].includes(reason?.kind) ? reason.kind : ''
    }
    default: return ''
  }
}

/** No global listing fallback, arbitrary session ID, filterEvents counts, or raw errors. */
export async function recallSessionHistory({ getSessionQuery, owner, query, limit, snippetCap,
  snippetChars, signal, windowDays, extractEventText }) {
  if (!owner || typeof owner.id !== 'string' || typeof owner.cwd !== 'string'
    || !path.isAbsolute(owner.cwd) || !owner.id.trim() || owner.workspace !== workspaceKey(owner.cwd)
    || typeof owner.preset !== 'string') return unavailable('HistoryScopeUnavailable')
  // Freeze a second primitive-only copy before obtaining/awaiting any service.
  const trusted = Object.freeze({ id: owner.id, cwd: owner.cwd, workspace: owner.workspace, preset: owner.preset })
  try {
    assertRecallNotAborted(signal)
    if (![limit, snippetCap, snippetChars, windowDays].every(value => Number.isSafeInteger(value) && value >= 0)
      || typeof extractEventText !== 'function') return unavailable('HistoryInputInvalid')
    const pattern = localPattern(query)
    if (!pattern || limit === 0) return empty()
    const service = getSessionQuery()
    if (!service || typeof service.filterSessions !== 'function' || typeof service.readSession !== 'function') {
      return unavailable('HistoryServiceUnavailable')
    }
    const filters = [{ kind: 'id', values: [trusted.id] }, { kind: 'cwd', values: [trusted.cwd] }]
    if (windowDays > 0) {
      const from = Date.now() - windowDays * 86400000
      if (!Number.isSafeInteger(from)) return unavailable('HistoryInputInvalid')
      filters.push({ kind: 'created-at', from })
    }
    const records = await service.filterSessions(filters, signal)
    assertRecallNotAborted(signal)
    if (!Array.isArray(records)) return unavailable('HistoryScopeMismatch')
    if (!records.length) return empty()
    // An exact id filter has at most one logical source; do not silently accept
    // aliases, duplicates or foreign records even if the provider is faulty.
    if (records.length !== 1 || !sameHeader(records[0]?.header, trusted)) return unavailable('HistoryScopeMismatch')
    const snapshot = await service.readSession(trusted.id)
    assertRecallNotAborted(signal)
    if (!sameHeader(snapshot?.session, trusted)) return unavailable('HistoryScopeMismatch')
    const inherited = snapshot.inheritedEventCount, events = snapshot.events
    if (!Number.isSafeInteger(inherited) || inherited < 0 || !Array.isArray(events)
      || inherited > events.length) return unavailable('HistoryScopeMismatch')
    let matches = 0
    const snippets = [], seen = new Set()
    // This is a count of inherited array entries, never a seq threshold.
    for (const event of events.slice(inherited)) {
      const seq = event?.seq
      if (!Number.isSafeInteger(seq) || seq < 0) return unavailable('HistoryScopeMismatch')
      if (seen.has(seq)) continue
      seen.add(seq)
      const searchable = historyEventSearchText(event)
      if (!searchable.length || !pattern.test(searchable)) continue
      matches++
      const text = extractEventText(event)
      if (typeof text === 'string' && text.length && snippets.length < snippetCap && snippetChars > 0) {
        snippets.push(text.length > snippetChars ? `${text.slice(0, snippetChars)}…` : text)
      }
    }
    assertRecallNotAborted(signal)
    return matches ? { available: true, sessions: [{ sessionId: trusted.id, matches, snippets }] } : empty()
  } catch { return unavailable(signal?.aborted ? 'HistoryCancelled' : 'HistoryUnavailable') }
}

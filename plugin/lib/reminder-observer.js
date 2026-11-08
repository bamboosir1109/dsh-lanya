import { publishObservation, readObservationCompletion } from './observation-producer.js'

const REMINDER_INSTRUCTION = [
  'A local reminder is due. Decide once whether to deliver its actual content in this conversation, defer, or stay silent. Queuing is not delivery.',
  'Reuse an accepted reply ticket; do not call qq_decide again to change it to idle after sending.',
  'If any QQ send returns unknown or partial, stop all further QQ output in this turn, including final text, alternative content or media. Do not retry or claim success or failure without evidence.',
  'Delivery states, tool errors and do-not-retry notes are internal control evidence, not reminder content. Do not send a status, diagnostic aside or confirmation of silence. Local read-only work may continue.'
].join(' ')

export function createReminderObserver({ directory, workspaceKey, agentKey = '', resolveScope, now = Date.now }) {
  if (typeof workspaceKey !== 'string' || typeof agentKey !== 'string' || typeof resolveScope !== 'function') throw new Error('Trusted reminder ownership is required')
  return async (reminder, { key, occurrence } = {}) => {
    if (typeof key !== 'string' || !key) throw new Error('Stable reminder identity is required')
    const time = now()
    let createdAt = time
    if (occurrence !== undefined) {
      if (!occurrence || typeof occurrence !== 'object' || Array.isArray(occurrence) || occurrence.key !== key ||
          typeof occurrence.due_at !== 'string' || !Number.isFinite(Date.parse(occurrence.due_at)) ||
          Date.parse(occurrence.due_at) < 0 || Date.parse(occurrence.due_at) > time) {
        throw new Error('Exact due reminder occurrence is required')
      }
      // The opportunity began when this occurrence was due, not when a late
      // scheduler tick or a review generation happens to spool its evidence.
      createdAt = Date.parse(occurrence.due_at)
    }
    const scope = resolveScope(reminder)
    let generation = 0, previous = null
    for (;;) {
      const eventId = 'reminder:' + key + ':' + generation
      const completed = readObservationCompletion(directory, scope, eventId)
      if (completed?.status === 'sent') return { delivered: true, status: 'sent', terminal:true,
        decision_id:completed.decision_id }
      if (completed?.status === 'no_decision') return { delivered: false, status: 'no_decision', terminal: true,
        reason: completed.reason, local_outcome_id: completed.local_outcome_id,
        observation_revision: completed.observation_revision, completed_at: completed.completed_at,
        native_turn_completed: completed.native_turn_completed }
      // Unknown delivery is never retried. An idle decision stays idle until
      // the main LLM explicitly chose a future review time.
      if (completed && (completed.status === 'unknown' || !completed.next_review_at || now() < completed.next_review_at)) {
        const terminal = completed.status === 'unknown' || !completed.next_review_at
        return { delivered: false, status: completed.status, terminal,
          decision_id:completed.decision_id, next_review_at:terminal ? null : completed.next_review_at }
      }
      if (completed) {
        // Modern completion originals are validated against the immutable
        // event by readObservationCompletion. Keep that original opportunity
        // time for legacy callers without an occurrence context as well.
        if (typeof completed.event_raw === 'string') createdAt = Math.min(createdAt, JSON.parse(completed.event_raw).created_at)
        previous = completed; generation++; continue
      }
      publishObservation(directory, {
        scope, event_id: eventId, kind: 'reminder.due', producer: 'reminder-scheduler', created_at: createdAt,
        workspace_key: workspaceKey, agent_key: agentKey,
        payload: { reminder, previous_decision_id: previous?.decision_id || null,
          instruction: REMINDER_INSTRUCTION }
      })
      return { delivered: false, status: 'pending' }
    }
  }
}

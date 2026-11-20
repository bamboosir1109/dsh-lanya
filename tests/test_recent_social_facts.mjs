import test from 'node:test'
import assert from 'node:assert/strict'
import vm from 'node:vm'
import { createHash } from 'node:crypto'
import { OneBotDecisionAdapter, reduceRecentSocialFacts } from '../plugin/lib/onebot-policy-adapter.js'
import nativeReceipts from './fixtures/native_idle_wake/social-receipts.mjs'

function log() {
  const events = []
  const append = (type, data, extra = {}) => {
    const event = { type, data, seq: events.length, time: 1000 + events.length, ...extra }
    events.push(event); return event
  }
  const start = (turn, origin = 'plugin:dsh-lanya-observation') => {
    append('turn/start', { turn })
    append('user/message', { source: { kind: origin }, get content() { throw new Error('private body accessed') } })
    append('user/message', { source: { kind: 'runtime-context' }, get content() { throw new Error('context body accessed') } })
  }
  const tool = (turn, name, args, value, { error = false, raw = false } = {}) => {
    const callId = 'synthetic-call-' + events.length
    const call = append('tool/call', { turn, step: 1, callId, name, arguments: JSON.stringify(args) })
    const result = append('tool/result', { turn, step: 1, message: { role: 'tool',
      source: { kind: 'tool', callId }, toolCallId: callId, isError: error,
      content: [{ type: 'text', text: raw ? value : JSON.stringify(value) }] } },
    { surfaceOp: 'append', sourceEventSeqs: [call.seq] })
    return { call, result }
  }
  const end = turn => append('turn/end', { turn, reason: { kind: 'completed' } })
  return { events, append, start, tool, end }
}

test('native shaped accepted decisions, picker selection and image receipts remain separate facts', () => {
  const h = log()
  h.start(1)
  h.tool(1, 'qq_decide', { verdict: 'tool_action' }, { accepted: true, can_send: false })
  h.end(1); h.start(2)
  h.tool(2, 'qq_decide', { verdict: 'tool_action' }, { accepted: true, can_send: false })
  h.end(2)
  let facts = reduceRecentSocialFacts(h.events)
  assert.equal(facts.trailing_tool_action_decisions.count, 2)
  assert.equal(facts.turns[0].origin, 'observation')
  h.start(3, 'plugin:dsh-onebot')
  h.append('user/message', { source: { kind: 'plugin:dsh-lanya-decision-reminder' },
    get content() { throw new Error('reminder content accessed') } })
  h.tool(3, 'qq_decide', { verdict: 'reply', expression: 'meme' },
    { accepted: true, can_send: true, expression_plan: { choice: 'meme' } })
  h.tool(3, 'send_meme', { query: 'PRIVATE_QUERY', path: 'PRIVATE_PATH' },
    { ok: true, mode: 'qq_selection', selected: true, delivered: false, candidates: [{ path: 'PRIVATE_PATH', caption: 'PRIVATE_CAPTION' }] })
  h.tool(3, 'qq_send_image', { sources: ['PRIVATE_PATH'] }, '已发送（message_id=PRIVATE_ID）', { raw: true })
  h.append('assistant/message', { get content() { throw new Error('thinking/body accessed') } })
  h.end(3)
  facts = reduceRecentSocialFacts(h.events)
  assert.deepEqual(facts.counts, { accepted_decisions: 3, picker_selected: 1, image_successes: 1 })
  assert.equal(facts.turns[2].origin, 'live')
  assert.equal(facts.turns[2].expression, 'meme')
  assert.equal(facts.trailing_tool_action_decisions.count, 0)
  assert.equal(facts.image_intent, 'undetermined')
  assert.equal(facts.text_delivery, 'not_measured')
  assert.doesNotMatch(JSON.stringify(facts), /PRIVATE_|synthetic-call|toolCallId|sessionId/)
})

test('unmodified native append and message factory functions produce readable social facts', () => {
  for (const [name, raw] of Object.entries(nativeReceipts.functions)) {
    assert.equal(createHash('sha256').update(raw).digest('hex'), nativeReceipts.snippet_sha256[name])
  }
  const methods = vm.runInNewContext(Object.values(nativeReceipts.functions).join('\n') +
    '\n({ appendToolCall, appendToolResult })', { structuredClone,
    deepFreeze: value => Object.freeze(value), brandString: value => value, randomUUID: () => 'synthetic-native-message' })
  const h = log(); h.start(1)
  const session = { append: h.append }
  for (const [name, args, result] of [
    ['qq_decide', { verdict: 'reply', expression: 'meme' }, { accepted: true, can_send: true, expression_plan: { choice: 'meme' } }],
    ['send_meme', { tag: 'happy' }, { ok: true, mode: 'qq_selection', selected: true, delivered: false, candidates: [{}] }],
    ['qq_send_image', { sources: ['PRIVATE_NATIVE_PATH'] }, '已发送（message_id=PRIVATE_NATIVE_ID）'],
  ]) {
    const block = { id: 'native-' + h.events.length, name, arguments: JSON.stringify(args) }
    const callSeq = methods.appendToolCall(session, 1, 1, block)
    methods.appendToolResult(session, 1, 1, block, { content: [{ type: 'text', text: typeof result === 'string' ? result : JSON.stringify(result) }], isError: false }, callSeq)
  }
  h.end(1)
  const facts = reduceRecentSocialFacts(h.events)
  assert.deepEqual(facts.counts, { accepted_decisions: 1, picker_selected: 1, image_successes: 1 })
  assert.equal(facts.turns[0].expression, 'meme')
  assert.equal(facts.turns[0].origin, 'observation')
  assert.doesNotMatch(JSON.stringify(facts), /PRIVATE_|synthetic-native-message/)
})

test('failed, missing, repeated and rewritten receipts never create a second success', () => {
  const h = log(); h.start(1)
  h.tool(1, 'qq_decide', { verdict: 'reply' }, { accepted: false })
  h.tool(1, 'qq_send_image', {}, '已发送', { error: true, raw: true })
  h.end(1)
  assert.deepEqual(reduceRecentSocialFacts(h.events).counts, { accepted_decisions: 0, picker_selected: 0, image_successes: 0 })
  const fresh = log(); fresh.start(1)
  const { result } = fresh.tool(1, 'qq_send_image', {}, '已发送', { raw: true })
  fresh.append('tool/result', result.data, { surfaceOp: { op: 'replace', startSeq: result.seq, endSeq: result.seq }, sourceEventSeqs: [result.seq] })
  fresh.end(1)
  assert.equal(reduceRecentSocialFacts(fresh.events).counts.image_successes, 1)
  const duplicate = structuredClone(result.data)
  fresh.events.splice(fresh.events.length - 1, 0, { type: 'tool/result', data: duplicate, seq: fresh.events.length - 1,
    time: 1100, surfaceOp: 'append', sourceEventSeqs: result.sourceEventSeqs })
  fresh.events.at(-1).seq++
  fresh.events.at(-1).time = 1200
  assert.equal(reduceRecentSocialFacts(fresh.events).counts.image_successes, null)
  const missing = log(); missing.start(1)
  missing.append('tool/call', { turn: 1, step: 1, callId: 'missing', name: 'send_meme', arguments: '{}' })
  missing.end(1)
  assert.equal(reduceRecentSocialFacts(missing.events).counts.picker_selected, null)
})

test('partial histories, unknown endings, mixed origins and preset boundaries retain uncertainty', () => {
  const h = log(); h.start(1); h.tool(1, 'qq_decide', { verdict: 'tool_action' }, { accepted: true }); h.end(1)
  assert.equal(reduceRecentSocialFacts(h.events.slice(1), { fromSeq: 1, truncated: true }).counts.accepted_decisions, null)
  h.start(2)
  assert.equal(reduceRecentSocialFacts(h.events).trailing_tool_action_decisions.count, null)
  h.append('user/message', { source: { kind: 'plugin:dsh-onebot' } }); h.end(2)
  assert.equal(reduceRecentSocialFacts(h.events).turns[1].origin, 'unknown')
  h.append('agent-preset/selected', { agentPreset: 'PRIVATE_PRESET' })
  assert.equal(reduceRecentSocialFacts(h.events).counts.accepted_decisions, null)
  h.start(3); h.append('turn/end', { turn: 3, reason: {} })
  assert.equal(reduceRecentSocialFacts(h.events).counts.accepted_decisions, null)
  assert.doesNotMatch(JSON.stringify(reduceRecentSocialFacts(h.events)), /PRIVATE_PRESET/)
})

async function context(session, scope = 'private:101') {
  let snapshots = 0
  const adapter = Object.create(OneBotDecisionAdapter.prototype)
  adapter.config = {}
  const agent = { session }
  adapter.bridge = { chats: new Map([[scope, { agent }], ['group:202', { get agent() { throw new Error('foreign scope read') } }]]),
    effectiveCwd: value => { assert.equal(value, scope); return process.cwd() },
    resolvePresetId: async value => { assert.equal(value, scope); return 'synthetic' },
    chatForSession: id => { assert.equal(id, session.id); return scope } }
  adapter.memoryContext = async () => null
  adapter.runtimeCapabilities = async () => null
  adapter.log = () => {}
  const old = session.snapshotEvents
  session.snapshotEvents = (...args) => { snapshots++; return old(...args) }
  const value = await adapter.enrichContext(scope, 'synthetic input')
  const facts = JSON.parse(value.match(/<trusted_recent_social_facts>\n([^]*?)\n<\/trusted_recent_social_facts>/)[1])
  return { facts, value, snapshots }
}

test('fresh context reads only the own bounded native range, excluding inherited history, and sends nothing', async () => {
  const total = 20000, inherited = 17000
  const result = await context({ id: 'synthetic-session', seq: total, inheritedEventCount: inherited,
    snapshotEvents(from, to) {
      assert.equal(from, inherited); assert.equal(to, total)
      return Array.from({ length: to - from }, (_, i) => ({ seq: from + i, time: i, type: 'system/message',
        data: { get content() { throw new Error('private body accessed') } } }))
    } })
  assert.equal(result.snapshots, 1)
  assert.equal(result.facts.window.events_examined, 3000)
  assert.equal(result.facts.status, 'unknown')
  assert.match(result.value, /no user request or major achievement is required/)
  assert.match(result.value, /no required image frequency/)
  assert.match(result.value, /At most one actual meme/)
  assert.doesNotMatch(JSON.stringify(result.facts), /synthetic-session|private:101|group:202/)
  const capped = await context({ id: 'bounded', seq: 20000, inheritedEventCount: 0, snapshotEvents(from, to) {
    assert.equal(from, 15904); assert.equal(to, 20000)
    return Array.from({ length: 4096 }, (_, i) => ({ seq: from + i, time: i, type: 'system/message', data: {} }))
  } })
  assert.equal(capped.facts.window.events_examined, 4096)
})

test('unavailable native snapshots degrade to unknown without blocking fresh context', async () => {
  const result = await context({ id: 'unavailable', seq: 10, inheritedEventCount: 0,
    snapshotEvents() { throw new Error('snapshot unavailable') } })
  assert.equal(result.facts.status, 'unknown')
  assert.equal(result.facts.counts.image_successes, null)
  assert.match(result.value, /synthetic input/)
})

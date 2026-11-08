/** Current native session surfaces only; no history service, model or QQ. */
import test from 'node:test'
import assert from 'node:assert/strict'
import path from 'node:path'
import os from 'node:os'
import { readScopedMemoryEvents } from '../plugin/lib/onebot-policy-adapter.js'

const cwd = path.resolve(os.tmpdir(), 'synthetic-memory-owner')
const owner = { workspace_key: process.platform === 'win32' ? cwd.toLowerCase() : cwd, agent_key: 'fixture' }
const scope = 'private:fixture'
const user = (seq, text = 'raincoat topic') => ({ seq, time: seq * 1000, type: 'user/message',
  data: { role: 'user', source: { kind: 'plugin:dsh-onebot' }, content: [{ type: 'text', text }] } })
const assistant = (seq, text = 'raincoat report') => ({ seq, time: seq * 1000, type: 'assistant/message',
  data: { message: { role: 'assistant', content: [{ type: 'text', text }] } } })
function session(events, extra = {}) {
  return { id: 'fixture-session', header: { id: 'fixture-session', cwd, agentPreset: 'fixture' }, seq: events.length,
    inheritedEventCount: 0, snapshotEvents: (from, to) => events.slice(from, to), ...extra }
}

test('current native message shapes preserve roles and source times without claiming delivery', () => {
  const facts = readScopedMemoryEvents(session([user(0), assistant(1)]), scope, owner)
  assert.deepEqual(facts.events.map(event => event.role), ['user', 'assistant'])
  assert.deepEqual(facts.events.map(event => event.created_at), [0, 1000])
  assert.equal(facts.events[0].conversation_key, scope)
  assert.equal(facts.window.assistant_text, 'report_not_verified_qq_delivery')
})

test('foreign cwd preset header and malformed native boundaries do not read messages', () => {
  for (const header of [{ cwd, agentPreset: 'other' }, { cwd: path.dirname(cwd), agentPreset: 'fixture' },
    { cwd, agentPreset: 'fixture', id: 'foreign-session' }, { cwd }]) {
    const facts = readScopedMemoryEvents(session([], { header, snapshotEvents: () => assert.fail('foreign snapshot read') }), scope, owner)
    assert.equal(facts.window.status, 'unavailable')
  }
})

test('native range reads exclude inherited events and older preset surfaces', () => {
  const events = [user(0, 'inherited private prefix'), user(1, 'old preset'), { seq: 2, type: 'agent-preset/selected' }, user(3, 'current owner topic')]
  const seen = []
  const facts = readScopedMemoryEvents(session(events, { inheritedEventCount: 1,
    snapshotEvents: (from, to) => { seen.push([from, to]); return events.slice(from, to) } }), scope, owner)
  assert.deepEqual(seen, [[1, 4]])
  assert.deepEqual(facts.events.map(event => event.text), ['current owner topic'])
  assert.equal(facts.window.older_context, 'not_covered')
})

test('large native sessions request only bounded tail and return at most 64 surfaces', () => {
  const calls = []
  const facts = readScopedMemoryEvents(session([], { seq: 10000, snapshotEvents: (from, to) => {
    calls.push([from, to]); assert.equal(to - from, 2048)
    return Array.from({ length: to - from }, (_, index) => user(from + index, 'bounded fixture ' + index))
  } }), scope, owner)
  assert.deepEqual(calls, [[10000 - 2048, 10000]])
  assert.equal(facts.events.length, 64)
  assert.equal(facts.window.status, 'partial')
})

test('tools hidden thinking and earlier injected memory are not read or recursively injected', () => {
  const thinking = { type: 'thinking', get text() { assert.fail('thinking read') } }
  const tool = { seq: 1, type: 'tool/result', get data() { assert.fail('tool result read') } }
  const report = assistant(2); report.data.message.content.push(thinking)
  const surface = user(0, 'actual topic\n<scoped_agent_intentions>old plans and private memory</scoped_agent_intentions>')
  const observation = user(3, 'internal observation'); observation.data.source.kind = 'plugin:dsh-lanya-observation'
  const facts = readScopedMemoryEvents(session([surface, tool, report, observation]), scope, owner)
  assert.deepEqual(facts.events.map(event => event.text), ['actual topic', 'raincoat report'])
})

test('missing times remain unknown and excerpts state their truncation', () => {
  const event = user(0, 'x'.repeat(2000)); delete event.time
  const facts = readScopedMemoryEvents(session([event]), scope, owner)
  assert.equal(facts.events[0].created_at, null)
  assert.equal(facts.events[0].text.length, 1200)
  assert.equal(facts.events[0].text_truncated, true)
  assert.equal(facts.window.status, 'partial')
})

const inlineCall = (seq=0,text='confirmed inline answer') => ({seq,time:1000,type:'tool/call',
  data:{name:'qq_decide',callId:'inline-fixture',turn:1,step:1,arguments:{verdict:'reply',text}}})
const inlineResult = (seq=1,sent=true) => ({seq,time:2000,type:'tool/result',surfaceOp:'append',sourceEventSeqs:[0],
  data:{turn:1,step:1,message:{role:'tool',source:{kind:'tool',callId:'inline-fixture'},toolCallId:'inline-fixture',
    content:[{type:'text',text:JSON.stringify({accepted:true,inline_text:true,sent})}]}}})

test('confirmed inline replies remain same-owner assistant memory surfaces without replaying arbitrary tools',()=>{
  const result=readScopedMemoryEvents(session([inlineCall(),inlineResult()]),scope,owner)
  assert.deepEqual(result.events.map(x=>[x.role,x.text]),[['assistant','confirmed inline answer']])
  assert.equal(result.events[0].created_at,2000)
  assert.equal(result.events[0].conversation_key,scope)
})

test('inline memory excludes refused uncertain mismatched rewritten and owner-boundary tool results',()=>{
  for(const mode of ['unsent','wrong-call','wrong-step','wrong-source','wrong-parent','error','rewrite','boundary','other-tool']){
    const call=inlineCall(),result=inlineResult()
    if(mode==='unsent')result.data.message.content[0].text=JSON.stringify({accepted:true,inline_text:true,sent:false,status:'unknown'})
    if(mode==='wrong-call')result.data.message.toolCallId='foreign'
    if(mode==='wrong-step')result.data.step=2
    if(mode==='wrong-source')result.data.message.source.kind='host:web'
    if(mode==='wrong-parent')result.sourceEventSeqs=[999]
    if(mode==='error')result.data.message.isError=true
    if(mode==='rewrite')result.surfaceOp={op:'replace',startSeq:0,endSeq:0}
    if(mode==='other-tool')call.data.name='some_other_tool'
    const rows=mode==='boundary'?[call,{seq:1,type:'agent-preset/selected'}, {...result,seq:2}]:[call,result]
    assert.equal(readScopedMemoryEvents(session(rows),scope,owner).events.length,0,mode)
  }
})

test('native compaction cannot count an inline reply twice and long replies retain excerpt limits',()=>{
  const first=inlineResult(),rewrite=structuredClone(first);rewrite.seq=2;rewrite.surfaceOp={op:'replace',startSeq:1,endSeq:1};rewrite.sourceEventSeqs=[1]
  const facts=readScopedMemoryEvents(session([inlineCall(0,'x'.repeat(4000)),first,rewrite]),scope,owner)
  assert.equal(facts.events.length,1);assert.equal(facts.events[0].text.length,1200)
  assert.equal(facts.events[0].text_truncated,true)
})

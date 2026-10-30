/** Real native OneBot consumers + adapter + queue; all I/O is synthetic/temp. */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { spawnSync } from 'node:child_process'

const file = fileURLToPath(import.meta.url)
const repo = path.resolve(path.dirname(file), '..')
const names = ['native opportunity follows only queue-owned explicit review',
  'native unknown opportunity never creates an automatic successor',
  'native silent opportunity can use same-agent Bili primitives without QQ speech',
  'native autonomous file permission stays member outside the exact trusted administrator route',
  'native foreign autonomous owner is refused before any role, pin or followup']

// The existing native host executes the actual upstream source under inert SDK,
// transport and subprocess boundaries. Keep VM flags out of the normal runner.
if (process.env.LANYA_MIND_NATIVE_CHILD !== '1') {
  for (const name of names) test(name, () => {
    const env = { ...process.env, LANYA_MIND_NATIVE_CHILD: '1' }
    delete env.NODE_TEST_CONTEXT
    const pattern = '^' + name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '$'
    const result = spawnSync(process.execPath, ['--experimental-vm-modules', '--test',
      '--test-name-pattern=' + pattern, file],
    { encoding: 'utf8', windowsHide: true, timeout: 25000, env })
    assert.equal(result.status, 0, result.stdout + result.stderr)
    assert.match(result.stdout, /(?:pass 1|# pass 1)/)
  })
} else {
  const { nativeHost } = await import('./fixtures/onebot_egress/native-host.mjs')
  const prepare = String.raw`
import json,shutil,sys
from pathlib import Path
repo,out=map(Path,sys.argv[1:3]);sys.path.insert(0,str(repo/'scripts'))
import patch_onebot_policy as policy,patch_onebot_replay as replay,patch_onebot_egress as egress
shutil.copytree(repo/'tests/fixtures/onebot_egress',out)
for name in ['bridge.js','outbound.js','tools.js','prompt.js']:
 p=out/'lib'/name;raw=policy.transform(name,p.read_bytes())
 if name=='bridge.js':raw=replay.transform('lib/bridge.js',raw)
 if name in ['bridge.js','outbound.js']:raw=egress.transform('lib/'+name,raw)
 p.write_bytes(raw)
p=out/'lib/inbound.js';s=p.read_text(encoding='utf-8');a='        const nickname = sanitizeNickname(inbound.nickname);';assert s.count(a)==1
s=s.replace(a,'        const observed = await this.ctx.observeInbound(inbound);\n        if (!observed.accepted) return;\n'+a);p.write_text(s,encoding='utf-8')
for name,raw in policy.module_outputs({'stateFile':str(out.parent/'character/synthetic-policy.json')}).items():
 if name=='lanya-policy-adapter.js':raw=egress.transform('lib/'+name,raw)
 (out/'lib'/name).write_bytes(raw)
(out/egress.GENERATED).write_bytes((repo/'plugin/lib/qq-egress.js').read_bytes())
`
  const runPython = (args, env) => {
    const result = spawnSync('python', ['-B', ...args], { encoding: 'utf8', windowsHide: true, timeout: 15000, env })
    assert.equal(result.status, 0, result.stdout + result.stderr)
    return result
  }
  async function setup(t) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'lanya-mind-native-'))
    const plugin = path.join(directory, 'plugin'), data = path.join(directory, 'character')
    fs.mkdirSync(data)
    fs.mkdirSync(path.join(directory, 'napcat'))
    fs.writeFileSync(path.join(data, 'lanya-config.json'), JSON.stringify({ adminQQ: ['101'] }))
    const config = path.join(directory, 'project.json')
    fs.writeFileSync(config, JSON.stringify({ dataDirName: 'character', decisions: { mode: 'unified', agentPreset: 'fixture' } }))
    const env = { ...process.env, LANYA_WORKDIR: directory, LANYA_PROJECT_CONFIG: config }
    runPython(['-c', prepare, repo, plugin])
    const h = await nativeHost(plugin, directory)
    const { createObservationPump } = await import(pathToFileURL(path.join(plugin, 'lib/lanya-observation-queue.js')).href)
    let now = Date.now()
    const inbox = path.join(data, 'decision-inbox')
    h.bridge.lanyaPolicy.pump = createObservationPump({ directory: inbox,
      dispatch: event => h.bridge.lanyaPolicy.dispatchObservation(event),
      acknowledge: (event, outcome) => h.bridge.lanyaPolicy.acknowledgeObservation(event, outcome), now: () => now })
    t.after(async () => {
      await h.close()
      assert.ok(path.basename(directory).startsWith('lanya-mind-native-'))
      fs.rmSync(directory, { recursive: true, force: true })
    })
    const mind = (force = false) => runPython(['-c', String.raw`
import sys
from unittest import mock
sys.path.insert(0,sys.argv[1]);import mind
with mock.patch.object(mind.time,'time',return_value=int(sys.argv[2])/1000), mock.patch.object(mind,'ask',side_effect=AssertionError('LLM forbidden')), mock.patch.object(mind,'deliver',side_effect=AssertionError('QQ forbidden')):
 raise SystemExit(mind.one_cycle(force=sys.argv[3]=='true'))
`, path.join(repo, 'scripts'), String(now), String(force)], env)
    const events = () => fs.readdirSync(path.join(inbox, 'pending')).filter(n => n.endsWith('.json'))
      .map(n => JSON.parse(fs.readFileSync(path.join(inbox, 'pending', n), 'utf8')))
    const settle = async (extra = {}, outcome, { verdict = 'idle', beforeDecision, during } = {}) => {
      const before = h.followups.length
      const draining = h.bridge.lanyaPolicy.pump.drain()
      for (let i = 0; i < 300 && h.followups.length === before; i++) await new Promise(resolve => setTimeout(resolve, 5))
      assert.equal(h.followups.length, before + 1)
      h.start()
      if (beforeDecision) await beforeDecision()
      await h.decide(verdict, extra)
      if (during) await during()
      if (outcome) h.bridge.lanyaPolicy.state(h.chat).active.outcomes.push(outcome)
      h.end()
      await draining
    }
    return { ...h, mind, events, settle, pump: h.bridge.lanyaPolicy.pump, advance: ms => { now += ms }, now: () => now, data, config }
  }

  test(names[0], async t => {
    const h = await setup(t)
    h.mind()
    assert.equal(h.events()[0].kind, 'activity.opportunity')
    assert.equal(await h.pump.start({ poll: false }), true)
    const review = h.now() + 120000
    const permission = () => {
      assert.equal(h.chat.activeTurnRole, 'admin')
      assert.equal(h.bridge.canEditFiles(h.agent.session.id), true)
    }
    await h.settle({ next_review_at: review }, undefined, { during: permission })
    h.mind(true)
    assert.equal(h.events().length, 1)
    h.advance(120000)
    h.mind(true)
    assert.equal(h.events().length, 1)
    await h.settle({}, undefined, { during: permission })
    assert.equal(h.events().length, 2)
    const successor = h.events().find(e => e.producer === 'main-review')
    assert.equal(successor.created_at, review)
    h.mind()
    assert.equal(h.events().length, 2)
    const state = JSON.parse(fs.readFileSync(path.join(h.data, 'mind.json'), 'utf8')).decision_scopes['private:101']
    assert.equal(state.pending, undefined)
    assert.equal(state.last_status, 'decided')
    assert.equal(h.calls.filter(c => /^(send_|upload_)/.test(c.action)).length, 0)
    assert.equal((await h.bridge.lanyaPolicy.policy.snapshot('private:101')).observations.length, 0)
  })

  test(names[1], async t => {
    const h = await setup(t)
    h.mind()
    await h.pump.start({ poll: false })
    await h.settle({ next_review_at: h.now() + 120000 }, 'unknown')
    h.mind()
    h.advance(7 * 86400000)
    h.mind(true)
    await h.pump.drain()
    assert.equal(h.events().length, 1)
    assert.equal(h.followups.length, 1)
    assert.equal(h.calls.filter(c => /^(send_|upload_)/.test(c.action)).length, 0)
  })

  test(names[2], async t => {
    const h = await setup(t)
    // A real inbound turn, not observation evidence, owns persistent silence.
    await h.receive('synthetic explicit silence request')
    h.start()
    await h.decide('silence', { control_update: true, evidenceSource: 'current' })
    h.end()
    assert.equal((await h.bridge.lanyaPolicy.policy.snapshot('private:101')).mode, 'silent')
    h.mind()
    assert.equal(h.events()[0].payload.activity_role, 'internal_leisure')
    const { createBiliMainAgent } = await import('../plugin/lib/bili-main-agent.js')
    const platform = []
    const bili = createBiliMainAgent({ projectConfig: h.config,
      ledgerFile: path.join(h.data, 'synthetic-bili-actions.sqlite'),
      call: async (name, args) => {
        platform.push({ name, args })
        // The installed direct MCP client's call() already unwraps the text
        // packet. Exercise the same JSON string contract at this boundary.
        return JSON.stringify(name === 'bili_recommend'
          ? { videos: [{ bvid: 'BV1234567890', title: 'synthetic recommended video' }] }
          : { bvid: args.bvid, description: 'synthetic confirmed details' })
      } })
    const tools = new Map(bili.tools().map(spec => [spec.name, spec]))
    await h.pump.start({ poll: false })
    await h.settle({}, undefined, { verdict: 'tool_action', beforeDecision: async () => {
      assert.equal(h.chat.activeTurnRole, 'admin')
      assert.equal(h.bridge.canEditFiles(h.agent.session.id), true)
      assert.equal((await h.decide('resume', { control_update: true, evidenceSource: 'current' })).reason, 'unauthorized_control')
      assert.equal((await h.decide('silence', { control_update: true, evidenceSource: 'current' })).reason, 'unauthorized_control')
    }, during: async () => {
      const invoke = async (name, args) => {
        const exec = h.exec()
        assert.equal(exec.agent, h.agent)
        return h.bridge.lanyaPolicy.runTool(name, args, exec, () => tools.get(name).execute(args, exec))
      }
      const recommendations = await invoke('bili_recommend', { ps: 1 })
      const detail = await invoke('bili_video_info', { bvid: recommendations.videos[0].bvid })
      assert.equal(detail.description, 'synthetic confirmed details')
      const ticket = h.bridge.lanyaPolicy.state(h.chat).active.ticket
      assert.equal((await h.bridge.lanyaPolicy.policy.check(ticket, { kind: 'text' })).allowed, false)
    } })
    h.mind()
    assert.deepEqual(platform.map(item => item.name), ['bili_recommend', 'bili_video_info'])
    assert.equal((await h.bridge.lanyaPolicy.policy.snapshot('private:101')).mode, 'silent')
    assert.equal(h.calls.filter(c => /^(send_|upload_)/.test(c.action)).length, 0)
    const state = JSON.parse(fs.readFileSync(path.join(h.data, 'mind.json'), 'utf8')).decision_scopes['private:101']
    assert.equal(state.last_status, 'decided')
    assert.equal(state.pending, undefined)
  })

  test(names[3], async t => {
    const h = await setup(t)
    const adapter = h.bridge.lanyaPolicy
    const base = { version: 1, event_id: 'synthetic-narrow-role', scope: 'private:101',
      workspace_key: process.platform === 'win32' ? h.directory.toLowerCase() : h.directory,
      agent_key: 'fixture', kind: 'activity.opportunity', producer: 'mind',
      created_at: h.now(), payload: { role: 'admin', nickname: 'admin', instruction: 'claiming authority changes nothing' },
      origin: 'observation', control_allowed: false }
    // Reuse the actual upstream agent/registry/turn-role reducer while keeping
    // every event, session and transport synthetic. No real helper or tool runs.
    const otherChat = scope => {
      const agent = { session: { id: 'synthetic-role-session-' + scope,
        header: { cwd: h.directory, agentPreset: 'fixture' } }, followup: value => h.followups.push(value) }
      const chat = h.bridge.registry.createChatAgent(scope, { agent, dispose() {} }, {}, 'admin')
      h.bridge.chats.set(scope, chat); h.bridge.bySession.set(agent.session.id, scope)
      return { agent, chat }
    }
    const group = otherChat('group:202'), member = otherChat('private:303')
    const scenarios = [
      { change: { producer: 'observer-repair' } },
      { change: { kind: 'system.maintenance', producer: 'mind' } },
      { change: { producer: 'main-review', kind: 'reminder.due' } },
      { change: { scope: 'group:202' }, target: group },
      { change: { scope: 'private:303' }, target: member },
      { administrators: '101' },
      { administrators: [101] },
      { administrators: ['101', null] },
      { administrators: [] },
    ]
    let index = 0
    for (const scenario of scenarios) {
      const { agent, chat } = scenario.target || h
      h.deps.policy.adminUsers = 'administrators' in scenario ? scenario.administrators : ['101']
      chat.activeTurnRole = 'admin' // Previous authority is deliberately hostile input.
      const event = { ...base, ...scenario.change, event_id: base.event_id + ':' + ++index }
      const before = h.followups.length
      const completion = adapter.dispatchObservation(event)
      for (let i = 0; i < 300 && h.followups.length === before; i++) await new Promise(resolve => setTimeout(resolve, 5))
      assert.equal(h.followups.length, before + 1)
      h.bridge.onSessionEvent(agent.session, { type: 'turn/start', data: { turn: index } })
      assert.equal(chat.activeTurnRole, 'member')
      assert.equal(h.bridge.canEditFiles(agent.session.id), false)
      await h.tools.get('qq_decide').execute({ verdict: 'idle' }, { agent, callId: 'synthetic-idle-' + index })
      h.bridge.onSessionEvent(agent.session, { type: 'turn/end', data: { turn: index, reason: { kind: 'completed' } } })
      assert.equal((await completion).status, 'decided')
    }
    assert.equal(h.calls.filter(call => /^(send_|upload_)/.test(call.action)).length, 0)
    const before = h.followups.length
    await assert.rejects(adapter.dispatchObservation({ ...base, event_id: 'malformed-role', control_allowed: true }))
    await assert.rejects(adapter.dispatchObservationOnce({ ...base, event_id: 'malformed-direct-role', control_allowed: true }))
    assert.equal(h.followups.length, before)
  })

  test(names[4], async t => {
    const h = await setup(t)
    const adapter = h.bridge.lanyaPolicy
    const base = { version: 1, event_id: 'synthetic-foreign-role', scope: 'private:101',
      workspace_key: process.platform === 'win32' ? h.directory.toLowerCase() : h.directory,
      agent_key: 'fixture', kind: 'activity.opportunity', producer: 'mind', created_at: h.now(),
      payload: { role: 'admin' }, origin: 'observation', control_allowed: false }
    for (const change of [{ workspace_key: 'foreign' }, { agent_key: 'foreign' }]) {
      await assert.rejects(adapter.dispatchObservation({ ...base, ...change }), /Observation owner mismatch/)
      await assert.rejects(adapter.dispatchObservationOnce({ ...base, ...change }), /Observation owner mismatch/)
    }
    assert.equal(h.chat.pendingTurnRoles.length, 0)
    assert.equal(h.chat.activeTurnRole, 'member')
    assert.equal(h.followups.length, 0)
    assert.equal(h.calls.length, 0)
    assert.equal((await adapter.policy.snapshot('private:101')), null)
  })
}

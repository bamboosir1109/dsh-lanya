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
  'native unknown opportunity never creates an automatic successor']

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
    const settle = async (extra = {}, outcome) => {
      const before = h.followups.length
      const draining = h.bridge.lanyaPolicy.pump.drain()
      for (let i = 0; i < 300 && h.followups.length === before; i++) await new Promise(resolve => setTimeout(resolve, 5))
      assert.equal(h.followups.length, before + 1)
      h.start()
      await h.decide('idle', extra)
      if (outcome) h.bridge.lanyaPolicy.state(h.chat).active.outcomes.push(outcome)
      h.end()
      await draining
    }
    return { ...h, mind, events, settle, pump: h.bridge.lanyaPolicy.pump, advance: ms => { now += ms }, now: () => now, data }
  }

  test(names[0], async t => {
    const h = await setup(t)
    h.mind()
    assert.equal(h.events()[0].kind, 'activity.opportunity')
    assert.equal(await h.pump.start({ poll: false }), true)
    const review = h.now() + 120000
    await h.settle({ next_review_at: review })
    h.mind(true)
    assert.equal(h.events().length, 1)
    h.advance(120000)
    h.mind(true)
    assert.equal(h.events().length, 1)
    await h.settle()
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
}

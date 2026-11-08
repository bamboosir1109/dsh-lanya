/** Real 0.5.23 native tool -> protocol -> transformed Provider -> fresh SQLite.
 * The host schema wrapper and approval transport are synthetic, never an LLM.
 */
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { spawnSync, spawn } from 'node:child_process'
import { pathToFileURL, fileURLToPath } from 'node:url'

const repo = path.dirname(path.dirname(fileURLToPath(import.meta.url)))
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'memento-native-history-'))
after(() => fs.rmSync(temporary, { recursive: true, force: true }))
const template = path.join(temporary, 'template')
fs.cpSync(path.join(repo, 'tests/fixtures/memento_native_0_5_23'), template, { recursive: true })
function python(args) {
  const result = spawnSync('python', ['-B', ...args], { encoding: 'utf8', windowsHide: true })
  assert.equal(result.status, 0, result.stderr)
  return JSON.parse(result.stdout)
}
python([path.join(repo, 'scripts/patch_memento_visibility.py'), '--package-root', template,
  '--apply', '--backup-root', path.join(temporary, 'visibility-backup')])
const patchArgs = [path.join(repo, 'scripts/patch_memento_history.py'), '--package-root', template]
const preview = python(patchArgs)
python([...patchArgs, '--apply', '--host-stopped', '--expected-fingerprint', preview.fingerprint,
  '--backup-dir', path.join(temporary, 'history-backup')])
let sequence = 0
async function fixture(t) {
  const directory = path.join(temporary, 'case-' + ++sequence), plugin = path.join(directory, 'plugin')
  fs.cpSync(template, plugin, { recursive: true })
  const { openMemoryStore } = await import(pathToFileURL(path.join(plugin, 'lib/store.mjs')).href)
  const { MemoryProtocolCore } = await import(pathToFileURL(path.join(plugin, 'lib/protocol.mjs')).href)
  const { makeMemoryTool } = await import(pathToFileURL(path.join(plugin, 'native-tool.mjs')).href)
  const { workspaceKeyOf, agentKeyOf } = await import(pathToFileURL(path.join(plugin, 'lib/workspace.mjs')).href)
  const store = openMemoryStore(path.join(directory, 'synthetic.sqlite'))
  t.after(() => store.close())
  let gate = async () => 'allowed-once'
  const core = new MemoryProtocolCore({ store, budgets: { user: { userGlobal: 9000, workspace: 9000 }, agent: { userGlobal: 9000, workspace: 9000 } },
    writePolicy: 'ask', gate: (...args) => gate(...args) })
  const tool = makeMemoryTool(core)
  const exec = { agent: { session: { id: 'fixture-main-session', header: { cwd: directory, agentPreset: 'fixture-preset' } } },
    callId: 'fixture-call', signal: new AbortController().signal }
  const owner = { workspaceKey: workspaceKeyOf(directory), agentKey: agentKeyOf('fixture-preset') }
  return { directory, plugin, store, core, exec, owner, native: args => tool.execute(args, exec),
    get gate() { return gate }, set gate(value) { gate = value },
    batches: () => store.db.prepare('SELECT * FROM native_history_batches ORDER BY seq').all(),
    rows: batchId => store.db.prepare('SELECT * FROM native_history_rows WHERE batch_id=? ORDER BY ordinal').all(batchId),
    facts: () => store.db.prepare('SELECT * FROM entries ORDER BY id').all().map(row => ({ ...row })) }
}

test('actual native tool add/replace/remove archives full raw rows while preserving public return shape', async t => {
  const f = await fixture(t)
  const first = await f.native({ action: 'add', text: '原始中文事实 🐺', tags: ['fixture'] })
  assert.equal(first.ok, true)
  const original = f.facts()[0]
  const changed = await f.native({ action: 'replace', match: '原始中文', text: '已纠正的中文事实 🐺' })
  assert.equal(changed.previous.text, first.entry.text)
  assert.equal(changed.entry.id, first.entry.id)
  const replaced = f.facts()[0]
  assert.equal(replaced.version, original.version + 1)
  assert.equal((await f.native({ action: 'remove', match: '已纠正' })).ok, true)
  assert.equal(f.facts().length, 0)
  assert.deepEqual(f.batches().map(batch => batch.operation), ['add', 'replace', 'remove'])
  const replace = f.rows(f.batches()[1].batch_id)[0]
  assert.deepEqual(JSON.parse(replace.before_json), original)
  assert.deepEqual(JSON.parse(replace.after_json), replaced)
  assert.equal(Object.keys(JSON.parse(replace.before_json)).length, 14)
  assert.equal(f.rows(f.batches()[2].batch_id)[0].result_version, replaced.version + 1)
  assert.equal(f.store.db.prepare('SELECT COUNT(*) AS count FROM native_history_audit').get().count, 3)
})

test('native consolidate archives all removed IDs and the new entry as one batch', async t => {
  const f = await fixture(t)
  await f.native({ action: 'add', text: 'fixture first original' })
  await f.native({ action: 'add', text: 'fixture second original' })
  const before = f.facts()
  const result = await f.native({ action: 'consolidate', matches: ['first original', 'second original'], text: 'fixture derived summary' })
  assert.equal(result.ok, true)
  assert.equal(result.removed.length, 2)
  const batch = f.batches().at(-1)
  assert.equal(batch.operation, 'consolidate')
  assert.equal(batch.row_count, 3)
  const rows = f.rows(batch.batch_id)
  assert.deepEqual(rows.filter(row => row.after_json === 'null').map(row => JSON.parse(row.before_json)).sort((a,b) => a.id.localeCompare(b.id)), before.sort((a,b) => a.id.localeCompare(b.id)))
  assert.equal(rows.filter(row => row.before_json === 'null').length, 1)
})

test('native seed is one archived batch and recall updates create no new fact batches', async t => {
  const f = await fixture(t)
  await f.core.seed([{ track: 'agent', scope: 'workspace', text: 'seed first' }, { track: 'agent', scope: 'workspace', text: 'seed second' }], { agent: f.exec.agent, signal: f.exec.signal })
  assert.equal(f.batches().length, 1)
  assert.equal(f.batches()[0].row_count, 2)
  await f.native({ action: 'query', track: 'agent', text: 'seed' })
  f.store.bumpRecall(f.facts().map(row => row.id))
  assert.equal(f.batches().length, 1)
  assert.ok(f.facts().every(row => row.recall_count === 2))
})

for (const table of ['native_history_rows', 'native_history_audit']) {
  test('archive failure in ' + table + ' rolls back facts and every batch record', async t => {
    const f = await fixture(t)
    await f.native({ action: 'add', text: 'original fixture fact' })
    const before = f.facts(), count = f.batches().length
    f.store.db.exec(`CREATE TRIGGER fixture_archive_failure BEFORE INSERT ON ${table} BEGIN SELECT RAISE(ABORT,'fixture failure'); END`)
    // Strict layout rejects foreign triggers before fn; a separate SQL fault
    // injection below exercises a mid-archive failure after fact SQL executed.
    await assert.rejects(() => f.native({ action: 'replace', match: 'original fixture', text: 'must roll back' }))
    assert.deepEqual(f.facts(), before)
    assert.equal(f.batches().length, count)
  })
}

test('a mid-archive insert failure rolls back actual replacement and partial archive rows', async t => {
  const f = await fixture(t)
  await f.native({ action: 'add', text: 'original fixture fact' })
  const before = f.facts(), counts = f.batches().length
  const prepare = f.store.db.prepare.bind(f.store.db)
  f.store.db.prepare = sql => {
    if (sql === 'INSERT INTO native_history_audit VALUES (?,?,?,?,?,?)') return { run() { throw new Error('fixture audit disk failure') } }
    return prepare(sql)
  }
  await assert.rejects(() => f.native({ action: 'replace', match: 'original fixture', text: 'must roll back' }))
  f.store.db.prepare = prepare
  assert.deepEqual(f.facts(), before)
  assert.equal(f.batches().length, counts)
})

for (const action of ['replace', 'remove', 'consolidate']) {
  for (const mutation of ['edit', 'reinsert']) {
    test(action + ' rejects a same-owner ' + mutation + ' during real approval await', async t => {
      const f = await fixture(t)
      await f.native({ action: 'add', text: 'approved fixture original' })
      await f.native({ action: 'add', text: 'another fixture original' })
      f.gate = async () => {
        if (mutation === 'edit') f.store.replaceEntry({ track: 'user', scope: 'workspace', match: 'approved fixture', text: 'approved fixture concurrent fact' })
        else {
          f.store.removeEntry({ track: 'user', scope: 'workspace', match: 'approved fixture' })
          f.store.insertEntry({ track: 'user', scope: 'workspace', ...f.owner, text: 'approved fixture different ID' })
        }
        return 'allowed-once'
      }
      const args = { action, match: 'approved fixture', matches: ['approved fixture', 'another fixture'], text: 'unauthorized overwrite' }
      await assert.rejects(() => f.native(args), /target changed/)
      assert.ok(f.facts().some(row => row.text.includes(mutation === 'edit' ? 'concurrent fact' : 'different ID')))
      assert.equal(f.facts().some(row => row.text === 'unauthorized overwrite'), false)
      assert.equal(f.facts().length, 2)
    })
  }
}

test('actual native tool ignores model-supplied owner keys; final matching preserves foreign same-text rows', async t => {
  const f = await fixture(t)
  const own = await f.native({ action: 'add', text: 'same fixture text' })
  const foreign = f.store.insertEntry({ track: 'user', scope: 'workspace', workspaceKey: 'foreign-workspace', agentKey: 'foreign-preset', text: 'same fixture text' })
  const result = await f.native({ action: 'replace', match: 'same fixture', text: 'own corrected fixture', workspaceKey: 'foreign-workspace', agentKey: 'foreign-preset' })
  assert.equal(result.ok, true)
  assert.equal(result.entry.id, own.entry.id)
  assert.equal(f.store.db.prepare('SELECT text FROM entries WHERE id=?').get(foreign.id).text, 'same fixture text')
})

test('strict immutable schema rejects a forged existing guard and SQL REPLACE cannot overwrite evidence', async t => {
  const f = await fixture(t)
  await f.native({ action: 'add', text: 'original fixture fact' })
  const row = f.rows(f.batches()[0].batch_id)[0]
  assert.throws(() => f.store.db.prepare('INSERT OR REPLACE INTO native_history_rows VALUES (?,?,?,?,?,?,?,?)').run(...Object.values({ ...row, before_json: 'tampered' })), /cannot be replaced/)
  assert.throws(() => f.store.db.prepare('UPDATE native_history_rows SET before_json=? WHERE batch_id=?').run('tampered', row.batch_id), /append-only/)
  f.store.db.exec('DROP TRIGGER native_history_rows_update_refused; CREATE TRIGGER native_history_rows_update_refused BEFORE UPDATE ON native_history_rows BEGIN SELECT 1; END')
  const before = f.facts()
  await assert.rejects(() => f.native({ action: 'add', text: 'must refuse schema drift' }), /schema or immutable guards/)
  assert.deepEqual(f.facts(), before)
})

test('an incomplete preexisting history schema is refused without modifying facts or repairing DDL', async t => {
  const f = await fixture(t)
  f.store.db.exec('CREATE TABLE native_history_batches(seq INTEGER,batch_id TEXT,format TEXT,operation TEXT,created_at INTEGER,session_id TEXT,schema_json TEXT,row_count INTEGER,restores_batch_id TEXT,batch_hash TEXT)')
  await assert.rejects(() => f.native({ action: 'add', text: 'cannot bypass a malformed archive' }), /schema or immutable guards/)
  assert.equal(f.facts().length, 0)
  assert.equal(f.store.db.prepare("SELECT COUNT(*) AS count FROM sqlite_master WHERE name='native_history_rows'").get().count, 0)
})

test('a real child crash after fact SQL and before batch audit rolls back both', async t => {
  const f = await fixture(t)
  await f.native({ action: 'add', text: 'surviving original fixture' })
  const before = f.facts(), count = f.batches().length
  const code = `import {openMemoryStore} from ${JSON.stringify(pathToFileURL(path.join(f.plugin,'lib/store.mjs')).href)};
import {MemoryProtocolCore} from ${JSON.stringify(pathToFileURL(path.join(f.plugin,'lib/protocol.mjs')).href)};
import {makeMemoryTool} from ${JSON.stringify(pathToFileURL(path.join(f.plugin,'native-tool.mjs')).href)};
const [database,cwd]=process.argv.slice(1),store=openMemoryStore(database),prepare=store.db.prepare.bind(store.db);
store.db.prepare=sql=>sql==='INSERT INTO native_history_audit VALUES (?,?,?,?,?,?)'?{run(){process.exit(47)}}:prepare(sql);
const core=new MemoryProtocolCore({store,budgets:{user:{workspace:9000,userGlobal:9000},agent:{workspace:9000,userGlobal:9000}},writePolicy:'ask',gate:async()=> 'allowed-once'});
await makeMemoryTool(core).execute({action:'add',text:'uncommitted crashed fixture'},{agent:{session:{id:'crash-session',header:{cwd,agentPreset:'fixture-preset'}}},callId:'crash-call',signal:new AbortController().signal});`
  const result = spawnSync('node', ['--input-type=module','-e',code,f.store.path,f.directory], { encoding: 'utf8', windowsHide: true })
  assert.equal(result.status, 47, result.stderr)
  assert.deepEqual(f.facts(), before)
  assert.equal(f.batches().length, count)
  assert.equal(f.store.db.prepare('SELECT COUNT(*) AS count FROM native_history_audit').get().count, count)
})

test('real concurrent native writers serialize complete history batches with no lost facts', async t => {
  const f = await fixture(t)
  await f.native({ action: 'add', text: 'concurrent fixture baseline' })
  const code = `import {openMemoryStore} from ${JSON.stringify(pathToFileURL(path.join(f.plugin,'lib/store.mjs')).href)};
import {MemoryProtocolCore} from ${JSON.stringify(pathToFileURL(path.join(f.plugin,'lib/protocol.mjs')).href)};
import {makeMemoryTool} from ${JSON.stringify(pathToFileURL(path.join(f.plugin,'native-tool.mjs')).href)};
const [database,cwd,prefix]=process.argv.slice(1),store=openMemoryStore(database);
const core=new MemoryProtocolCore({store,budgets:{user:{workspace:9000,userGlobal:9000},agent:{workspace:9000,userGlobal:9000}},writePolicy:'ask',gate:async()=> 'allowed-once'});
for(let n=0;n<4;n++)await makeMemoryTool(core).execute({action:'add',text:prefix+' fixture '+n},{agent:{session:{id:prefix,header:{cwd,agentPreset:'fixture-preset'}}},callId:'call-'+n,signal:new AbortController().signal});store.close();`
  const run = prefix => new Promise((resolve,reject) => {
    const child = spawn('node',['--input-type=module','-e',code,f.store.path,f.directory,prefix],{windowsHide:true,stdio:['ignore','pipe','pipe']})
    let stderr='';child.stderr.on('data',chunk=>{stderr+=chunk});child.on('error',reject)
    child.on('exit',status=>status===0?resolve():reject(new Error('fixture child '+status+': '+stderr)))
  })
  await Promise.all([run('writer-a'),run('writer-b')])
  assert.equal(f.facts().length, 9)
  assert.deepEqual(f.batches().map(batch=>batch.seq), [1,2,3,4,5,6,7,8,9])
  assert.equal(f.store.db.prepare('SELECT COUNT(*) AS count FROM native_history_audit').get().count, 9)
})

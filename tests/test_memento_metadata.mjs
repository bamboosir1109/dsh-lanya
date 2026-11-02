/** Licensed native 0.5.23 consumer/protocol/provider with synthetic host seam.
 * No live database, approval transport, model or installed plugin is used.
 */
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { spawnSync } from 'node:child_process'
import { pathToFileURL,fileURLToPath } from 'node:url'

const repo=path.dirname(path.dirname(fileURLToPath(import.meta.url)))
const temp=fs.mkdtempSync(path.join(os.tmpdir(),'memento-metadata-'))
after(()=>fs.rmSync(temp,{recursive:true,force:true}))
const template=path.join(temp,'template')
fs.cpSync(path.join(repo,'tests/fixtures/memento_native_0_5_23'),template,{recursive:true})
const preparation=`
import sys
from pathlib import Path
sys.path.insert(0,str(Path(sys.argv[1])/'scripts'))
import patch_memento_visibility as v
import patch_memento_history as h
import patch_memento_recall_scope as r
import patch_memento_metadata as m
p=Path(sys.argv[2])
sources={name:(r.transform((p/name).read_bytes()) if name=='index.mjs' else h.transform(name,v.transform(name,(p/name).read_bytes()))) for name in ('index.mjs','lib/store.mjs','lib/protocol.mjs')}
outputs=m.build_candidates(sources)
for name,raw in outputs.items(): (p/name).write_bytes(raw)
(p/'lib/lanya-memento-history.mjs').write_bytes((Path(sys.argv[1])/'plugin/lib/memento-history.mjs').read_bytes())
(p/'native-tool.mjs').write_text(m.consumer((p/'native-tool.mjs').read_text(encoding='utf-8')),encoding='utf-8',newline='')
`
// Python3.9's Path.write_text has no newline argument.
const prepared=spawnSync('python',['-B','-c',preparation.replace(",newline=''",''),repo,template],{encoding:'utf8',windowsHide:true})
assert.equal(prepared.status,0,prepared.stderr)
for(const name of ['index.mjs','lib/store.mjs','lib/protocol.mjs','lib/lanya-memento-metadata.mjs','native-tool.mjs']) {
  const checked=spawnSync(process.execPath,['--check',path.join(template,name)],{encoding:'utf8',windowsHide:true})
  assert.equal(checked.status,0,checked.stderr)
}
let sequence=0
async function fixture(t) {
  const root=path.join(temp,'case-'+ ++sequence)
  fs.cpSync(template,root,{recursive:true})
  const {openMemoryStore}=await import(pathToFileURL(path.join(root,'lib/store.mjs')).href)
  const {MemoryProtocolCore}=await import(pathToFileURL(path.join(root,'lib/protocol.mjs')).href)
  const {makeMemoryTool,renderMemoryResult}=await import(pathToFileURL(path.join(root,'native-tool.mjs')).href)
  const annotations=await import(pathToFileURL(path.join(root,'lib/lanya-memento-metadata.mjs')).href)
  const store=openMemoryStore(path.join(root,'synthetic.sqlite'))
  t.after(()=>store.close())
  let approve=async()=> 'allowed-once'
  const core=new MemoryProtocolCore({store,budgets:{user:{userGlobal:12000,workspace:12000},agent:{userGlobal:12000,workspace:12000}},writePolicy:'ask',gate:(...args)=>approve(...args)})
  const tool=makeMemoryTool(core)
  const exec={agent:{session:{id:'synthetic-session',header:{cwd:root,agentPreset:'synthetic-preset'}}},signal:new AbortController().signal,callId:'synthetic-call'}
  const facts=()=>store.db.prepare('SELECT * FROM entries ORDER BY id').all()
  return {store,core,tool,exec,facts,annotations,renderMemoryResult,native:args=>tool.execute(args,exec),set approve(value){approve=value}}
}

test('native optional annotations reach the same tool, approval, atomic write and actual model renderer',async t=>{
  const f=await fixture(t)
  let plan
  f.approve=async payload=>{plan=payload;return 'allowed-once'}
  const result=await f.native({action:'add',text:'Synthetic event fact',metadata:{title:'Concrete topic',kind:'event',occurred_at:'2025-06-01',importance:65,confidence:45}})
  assert.equal(result.ok,true)
  assert.match(plan.text,/Concrete topic/)
  assert.equal(result.entry.metadata.occurred_at,'2025-06-01')
  assert.equal(result.entry.version,1)
  assert.equal(result.entry.recorded_at,f.facts()[0].created_at)
  assert.notEqual(result.entry.recorded_at,result.entry.metadata.occurred_at)
  assert.equal(f.tool.parameters.metadata.type,'object')
  assert.equal(f.tool.output.schema.properties.entry.properties.metadata.type,'object')
  const rendered=JSON.parse(f.renderMemoryResult({},result)[0].text)
  assert.deepEqual(rendered,result)
  const query=await f.native({action:'query',text:'Synthetic event'})
  assert.equal(query.entries[0].metadata.title,'Concrete topic')
  assert.equal(query.entries[0].id,result.entry.id)
})

test('native replacement preserves original recording/event dates, inherits omitted annotations and seals both versions',async t=>{
  const f=await fixture(t)
  const first=await f.native({action:'add',text:'Synthetic preference original',metadata:{title:'Preference',kind:'preference',occurred_at:'2025-01-02',confidence:80}})
  const raw=f.facts()[0]
  const second=await f.native({action:'replace',match:'preference original',text:'Synthetic preference corrected',metadata:{status:'uncertain',confidence:40}})
  assert.equal(second.entry.recorded_at,first.entry.recorded_at)
  assert.equal(second.entry.metadata.occurred_at,'2025-01-02')
  assert.equal(second.entry.metadata.title,'Preference')
  assert.deepEqual(second.entry.metadata.links,[{entry_id:first.entry.id,version:1,relation:'updates'}])
  assert.equal(f.annotations.readMetadata(f.store.db,raw).confidence,80)
  assert.equal(f.store.db.prepare('SELECT COUNT(*) AS n FROM memory_metadata_revisions').get().n,2)
  assert.equal(f.store.db.prepare('SELECT COUNT(*) AS n FROM native_history_batches').get().n,2)
})

test('foreign or stale correction links cannot be written and roll the fact/history transaction back',async t=>{
  const f=await fixture(t)
  await f.native({action:'add',text:'Synthetic local original'})
  const [local]=f.facts()
  const foreign=f.store.insertEntry({track:local.track,scope:local.scope,workspaceKey:local.workspace_key,agentKey:'foreign',text:'Synthetic foreign fact'})
  const before=f.facts(),batches=f.store.db.prepare('SELECT COUNT(*) AS n FROM native_history_batches').get().n
  for(const link of [{entry_id:foreign.id,version:1,relation:'corrects'},{entry_id:local.id,version:99,relation:'corrects'}]) {
    await assert.rejects(f.native({action:'replace',match:'local original',text:'Synthetic changed',metadata:{links:[link]}}),/target changed|another owner/)
    assert.deepEqual(f.facts(),before)
    assert.equal(f.store.db.prepare('SELECT COUNT(*) AS n FROM native_history_batches').get().n,batches)
  }
})

test('approval denial and invalid metadata never create an annotation or alter a fact',async t=>{
  const f=await fixture(t)
  f.approve=async()=> 'rejected'
  const denied=await f.native({action:'add',text:'Synthetic denied',metadata:{title:'Denied'}})
  assert.equal(denied.ok,false)
  assert.deepEqual(f.facts(),[])
  assert.equal(f.store.db.prepare("SELECT COUNT(*) AS n FROM sqlite_master WHERE name='memory_metadata_revisions'").get().n,0)
  for(const metadata of [{occurred_at:'yesterday'},{occurred_at:'2025-02-29'},{recorded_at:1},{confidence:100.5},{importance:true},{occurred_at:'2025-01-01T01:02:03'}]) {
    await assert.rejects(f.native({action:'add',text:'Synthetic invalid',metadata}))
  }
  assert.deepEqual(f.facts(),[])
})

test('consolidation keeps originals and their annotations in history and never invents an event date for the summary',async t=>{
  const f=await fixture(t)
  const first=await f.native({action:'add',text:'Synthetic first event',metadata:{title:'First event',kind:'event',occurred_at:'2025-01-01'}})
  const second=await f.native({action:'add',text:'Synthetic second event',metadata:{title:'Second event',kind:'event',occurred_at:'2025-02-01'}})
  const originals=f.facts()
  const merged=await f.native({action:'consolidate',matches:['first event','second event'],text:'Synthetic combined lessons'})
  assert.equal(merged.ok,true)
  assert.equal(merged.entry.metadata.kind,'uncertain')
  assert.equal(merged.entry.metadata.occurred_at,undefined)
  assert.deepEqual(merged.entry.metadata.links.map(link=>link.entry_id).sort(),[first.entry.id,second.entry.id].sort())
  for(const original of originals) assert.equal(f.annotations.readMetadata(f.store.db,original).kind,'event')
  assert.equal(f.store.db.prepare('SELECT COUNT(*) AS n FROM memory_metadata_revisions').get().n,3)
  assert.equal(f.store.db.prepare('SELECT row_count FROM native_history_batches ORDER BY seq DESC LIMIT 1').get().row_count,3)
})

test('cross-language reader validates Node annotations including Unicode and real recall mutations',async t=>{
  const f=await fixture(t)
  await f.native({action:'add',text:'Synthetic unicode fact',metadata:{title:'合成主题 🐺',kind:'reflection',confidence:20}})
  await f.native({action:'query',text:'unicode'})
  const result=spawnSync('python',['-B','-c',`import sys,json,sqlite3\nsys.path.insert(0,sys.argv[1])\nfrom memory_metadata import read_metadata\nc=sqlite3.connect('file:'+sys.argv[2]+'?mode=ro',uri=True);c.row_factory=sqlite3.Row\nr=c.execute('SELECT * FROM entries').fetchone()\nprint(json.dumps(read_metadata(c,r),ensure_ascii=True))`,path.join(repo,'scripts'),f.store.path],{encoding:'utf8',windowsHide:true})
  assert.equal(result.status,0,result.stderr)
  assert.equal(JSON.parse(result.stdout).title,'合成主题 🐺')
  assert.equal(f.annotations.readMetadata(f.store.db,f.facts()[0]).confidence,20)
})

test('native query matches actual annotation titles and explicit dates after owner visibility and before limit',async t=>{
  const f=await fixture(t)
  const added=await f.native({action:'add',text:'Synthetic body without title terms',metadata:{title:'Unique topic',occurred_at:'2025-07-09'}})
  const local=f.facts()[0]
  f.store.insertEntry({track:local.track,scope:local.scope,workspaceKey:'foreign-workspace',agentKey:local.agent_key,text:'Unique topic foreign',metadata:{title:'Unique topic',occurred_at:'2025-07-09'}})
  f.store.insertEntry({track:local.track,scope:local.scope,workspaceKey:local.workspace_key,agentKey:'foreign-agent',text:'Unique topic foreign agent',metadata:{title:'Unique topic'}})
  for(const text of ['Unique topic','2025-07-09']) {
    const result=await f.native({action:'query',text,limit:1})
    assert.equal(result.total,1)
    assert.equal(result.entries[0].id,added.entry.id)
  }
})

test('annotation guards remain append-only and stale fact versions remain unknown',async t=>{
  const f=await fixture(t)
  await f.native({action:'add',text:'Synthetic protected',metadata:{title:'Protected'}})
  const raw=f.facts()[0]
  assert.equal(f.annotations.readMetadata(f.store.db,{...raw,version:2}),null)
  for(const sql of ['DELETE FROM memory_metadata_revisions',"UPDATE memory_metadata_revisions SET metadata_json='{}'",'INSERT OR REPLACE INTO memory_metadata_revisions SELECT * FROM memory_metadata_revisions']) assert.throws(()=>f.store.db.exec(sql),/append-only|replaced/)
})

test('native audit failure rolls back facts, annotation and original history together',async t=>{
  const f=await fixture(t)
  await f.native({action:'add',text:'Synthetic before',metadata:{title:'Before'}})
  const facts=f.facts(),count=f.store.db.prepare('SELECT COUNT(*) AS n FROM memory_metadata_revisions').get().n
  const prepare=f.store.db.prepare.bind(f.store.db)
  f.store.db.prepare=sql=>sql === 'INSERT INTO native_history_audit VALUES (?,?,?,?,?,?)' ? {run(){throw new Error('synthetic audit failure')}} : prepare(sql)
  await assert.rejects(f.native({action:'replace',match:'Synthetic before',text:'Synthetic after',metadata:{title:'After'}}),/synthetic audit failure/)
  f.store.db.prepare=prepare
  assert.deepEqual(f.facts(),facts)
  assert.equal(f.store.db.prepare('SELECT COUNT(*) AS n FROM memory_metadata_revisions').get().n,count)
  assert.equal(f.store.db.prepare('SELECT COUNT(*) AS n FROM native_history_batches').get().n,1)
})

test('metadata approved before an await cannot be changed by mutating the original request object',async t=>{
  const f=await fixture(t)
  const request={action:'add',text:'Synthetic immutable plan',metadata:{title:'Approved title',confidence:20}}
  f.approve=async()=>{request.metadata.title='Unapproved title';request.metadata.confidence=99;return 'allowed-once'}
  const result=await f.native(request)
  assert.equal(result.entry.metadata.title,'Approved title')
  assert.equal(result.entry.metadata.confidence,20)
})

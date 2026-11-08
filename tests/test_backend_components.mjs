// Embedded panel provider projection; only temporary config and inert HTTP seams.
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { panelBackend, decorateBackendComponent } from '../plugin/lib/index.js'

function fixture(t) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'backend-components-'))
  t.after(()=>{assert.ok(path.basename(root).startsWith('backend-components-'));fs.rmSync(root,{recursive:true,force:true})})
  fs.mkdirSync(path.join(root,'config'))
  const write=(provider='snowluma',revision=1)=>fs.writeFileSync(path.join(root,'config/project.json'),JSON.stringify({qqBackend:{provider,revision}}))
  write();return {write,runtime:{workDir:root,project:{panelPort:8731}}}
}

test('embedded component uses the selected provider and never counts a NapCat injector by process name',async t=>{
  const h=fixture(t),calls=[]
  const status=await panelBackend(h.runtime,{fetchImpl:async(url,options)=>{
    calls.push({url,options});return {ok:true,json:async()=>({ok:true,backend:{provider:'snowluma',verified:true,status:'online',name:'UNTRUSTED'}})}
  }})
  assert.equal(status.name,'SnowLuma');assert.equal(status.verified,true)
  assert.equal(calls[0].url,'http://127.0.0.1:8731/api/qq/backend/status')
  assert.equal(calls[0].options.redirect,'error')
  const projected=decorateBackendComponent({components:[{tag:'napcat',count:2},{tag:'works',count:1}]},status)
  assert.deepEqual(projected.components.map(row=>row.tag),['works','qq-backend'])
  assert.equal(projected.components.at(-1).control,'backend_settings')
})

test('an old backend result or string boolean cannot verify a new selection',async t=>{
  const h=fixture(t)
  for(const backend of [{provider:'napcat',verified:true,status:'online'},
    {provider:'snowluma',verified:'true',status:'online'}]){
    const status=await panelBackend(h.runtime,{fetchImpl:async()=>({ok:true,json:async()=>({ok:true,backend})})})
    assert.equal(status.verified,false);assert.equal(status.name,'SnowLuma')
  }
})

test('a configuration generation changed during the request cannot inherit earlier health',async t=>{
  const h=fixture(t)
  const status=await panelBackend(h.runtime,{fetchImpl:async()=>{
    h.write('snowluma',2)
    return {ok:true,json:async()=>({ok:true,backend:{provider:'snowluma',verified:true,status:'online'}})}
  }})
  assert.equal(status.verified,false)
})

test('unavailable panel status retains selected provider without fabricating process count',async t=>{
  const h=fixture(t)
  const status=await panelBackend(h.runtime,{fetchImpl:async()=>{throw Error('fixture unavailable')}})
  assert.equal(status.name,'SnowLuma');assert.equal(status.verified,false)
  assert.equal(decorateBackendComponent({components:[]},status).components[0].count,null)
})

import test,{after} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
const repo=path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const root=fs.mkdtempSync(path.join(os.tmpdir(),'lanya-replay-integration-'));
after(()=>{assert.ok(path.basename(root).startsWith('lanya-replay-integration-'));fs.rmSync(root,{recursive:true,force:true})});
const plugin=path.join(root,'plugin');
fs.cpSync(path.join(repo,'tests/fixtures/onebot_policy'),plugin,{recursive:true});
fs.cpSync(path.join(repo,'tests/fixtures/onebot_replay/lib'),path.join(plugin,'lib'),{recursive:true});
function command(args){const value=spawnSync('python',['-B',...args],{encoding:'utf8',windowsHide:true});assert.equal(value.status,0,value.stderr);return JSON.parse(value.stdout)}
const firstArgs=[path.join(repo,'scripts/patch_onebot_policy.py'),'--plugin-dir',plugin,'--state-file',path.join(root,'production-state.json')];
const first=command(firstArgs);
const parent=command([...firstArgs,'--apply','--host-stopped','--expected-fingerprint',first.fingerprint,'--backup-dir',path.join(root,'base-backups')]);
const nextArgs=[path.join(repo,'scripts/patch_onebot_replay.py'),'--plugin-dir',plugin,'--policy-manifest',path.join(parent.backup,'patch.json')];
const next=command(nextArgs);
command([...nextArgs,'--apply','--host-stopped','--expected-fingerprint',next.fingerprint,'--backup-dir',path.join(root,'replay-backups')]);
for(const kind of ['normal','output','throw'])test('actual replay isolation: '+kind,()=>{
    const data=path.join(root,'case-'+kind);fs.mkdirSync(data);
    const result=spawnSync(process.execPath,['--experimental-vm-modules',path.join(repo,'tests/fixtures/onebot_replay/run-case.mjs'),plugin,data,kind],{encoding:'utf8',windowsHide:true});
    assert.equal(result.status,0,result.stdout+'\n'+result.stderr);
    const output=JSON.parse(result.stdout.trim());assert.equal(output.success,true);assert.equal(output.syntheticSentinels,4);assert.equal(output.sourceUnchanged,true);assert.equal(output.cleaned,true);
    assert.equal(output.configReads,0);assert.equal(output.consumerAccesses,0);assert.equal(output.networkAttempts,0);assert.equal(output.processAttempts,0);
    if(kind!=='throw'){assert.equal(output.followups,1);assert.ok(output.tempPolicyCommits>0)}
});

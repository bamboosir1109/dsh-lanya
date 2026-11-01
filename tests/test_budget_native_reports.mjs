import test from 'node:test';
import assert from 'node:assert/strict';
import {buildNativeDaily} from '../scripts/budget-native-report.mjs';

const now = Date.parse('2026-11-02T00:05:00+08:00');
function fixture() {
  return {now,dates:['2026-11-01','2026-11-02'],config:{},sessions:[{role:'private',own_start:0,
    source_start:now-600000,origins:[{seq:1,kind:'plugin:dsh-lanya-observation'}],
    records:[{seq:0,time:now-301000,type:'turn/start',data:{turn:0}},
      {seq:2,time:now-1000,type:'turn/end',data:{turn:0}}],
    expected_days:{'2026-11-01':0,'2026-11-02':0.0123}}]};
}
const priced = () => ({found:true,calls:[{atMs:now-2000,priced:true,apiCost:0.0123}]});
test('call date governs cross-midnight spending',()=>{
  const report=buildNativeDaily(fixture(),priced);
  assert.equal(report.days[0].total,0); assert.equal(report.days[1].autonomy,0.0123);
  assert.equal(report.days[0].turn_count,1);
});
test('overlapping turn lifecycle is refused',()=>{
  const value=fixture();value.sessions[0].records.splice(1,0,{seq:1,time:now-300000,type:'turn/start',data:{turn:1}});
  assert.throws(()=>buildNativeDaily(value,priced));
});
test('negative or nonfinite cost is refused',()=>{
  for(const value of [-1,Infinity,NaN])assert.throws(()=>buildNativeDaily(fixture(),()=>({found:true,calls:[{atMs:now-2000,priced:true,apiCost:value}]})));
});
test('duplicate role or native sequence is refused',()=>{
  const value=fixture();value.sessions.push(structuredClone(value.sessions[0]));assert.throws(()=>buildNativeDaily(value,priced));
  const duplicate=fixture();duplicate.sessions[0].records[1].seq=0;assert.throws(()=>buildNativeDaily(duplicate,priced));
});
test('pending native turn retains pending metadata',()=>{
  const value=fixture();value.sessions[0].records.pop();const report=buildNativeDaily(value,priced);
  assert.equal(report.days[0].pending_count,1);assert.equal(report.days[1].total,null);
});
test('one trusted input plus an unknown input cannot borrow its category',()=>{
  const value=fixture();value.sessions[0].origins.push({seq:2,kind:'untrusted-plugin'});
  const report=buildNativeDaily(value,priced);assert.equal(report.days[1].total,null);
  assert.equal(report.days[1].unclassified_count,1);
});
test('explicit host context is not another delivery input',()=>{
  const value=fixture();value.sessions[0].origins.push({seq:2,kind:'repeat-tool-reminder'});
  assert.equal(buildNativeDaily(value,priced).days[1].autonomy,0.0123);
});
test('ledger mismatch never fabricates a pending or unknown turn',()=>{
  const value=fixture();value.sessions[0].expected_days['2026-11-02']=9;
  const row=buildNativeDaily(value,priced).days[1];assert.equal(row.total,null);
  assert.equal(row.unknown_count,0);assert.equal(row.pending_count,0);assert.equal(row.reconciled,false);
});
test('cross-midnight unpriced calls affect their actual charge day',()=>{
  const value=fixture();value.sessions[0].expected_days['2026-11-01']=0.01;
  value.sessions[0].expected_days['2026-11-02']=0;
  const cost=()=>({found:true,calls:[{atMs:now-300500,priced:true,apiCost:0.01},
    {atMs:now-2000,priced:false,apiCost:0}]});
  const report=buildNativeDaily(value,cost),[first,second]=report.days;
  assert.equal(first.autonomy,0.01);assert.equal(first.unknown_count,0);
  assert.equal(second.autonomy,null);assert.equal(second.total,null);assert.equal(second.unknown_count,1);
  assert.equal(second.buckets.autonomy.known_spent_usd,0);
});
test('known costs survive beside unknown costs without becoming a confirmed total',()=>{
  const value=fixture();value.sessions[0].expected_days['2026-11-02']=0.0123;
  const report=buildNativeDaily(value,()=>({found:true,calls:[{atMs:now-2000,priced:true,apiCost:0.0123},
    {atMs:now-1500,priced:false,apiCost:0}]}));
  assert.equal(report.days[1].autonomy,null);assert.equal(report.days[1].known_total_usd,0.0123);
  assert.equal(report.days[1].unknown_count,1);
});
test('no-cost cross-midnight turn remains unknown on both days',()=>{
  const value=fixture();value.sessions[0].expected_days['2026-11-02']=0;
  const report=buildNativeDaily(value,()=>({found:false,calls:[]}));
  for(const row of report.days){assert.equal(row.unknown_count,1);assert.equal(row.total,null);assert.equal(row.autonomy,null);}
});

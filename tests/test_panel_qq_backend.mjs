// Actual backend UI code with inert DOM/API seams. No live service mutation.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { test } from 'node:test'

const source = readFileSync(new URL('../panel/qq-backend.js', import.meta.url), 'utf8')
const panel = readFileSync(new URL('../panel/panel.html', import.meta.url), 'utf8').replaceAll('\r\n', '\n')
function defer() { let resolve, reject; const promise = new Promise((a,b) => { resolve=a; reject=b }); return {promise,resolve,reject} }
function state(selected='napcat', revision='revision-a') {
  return {selected, revision, runtime_guard:{ready:true}, providers:['napcat','snowluma'].map(provider => ({provider,
    name:provider==='napcat'?'NapCatQQ':'SnowLuma', installed:true, configured:true, can_select:true,
    connection:{available:true, provider, base_url:'http://127.0.0.1:8730', wsClient:[], token_configured:true,
      webui_url:'http://127.0.0.1:5099', config_fingerprint:provider+'-'+revision}}))}
}
function harness(api=async () => ({ok:true,backend:state()})) {
  const nodes=new Map(), readers=[]
  const node=id => { if(!nodes.has(id)) nodes.set(id,{value:'',innerHTML:'',textContent:'',disabled:false,dataset:{},style:{}}); return nodes.get(id) }
  const context=vm.createContext({URL,Promise,api,$:node,PART:{},currentPage:'system',dirtyPages:new Set(),
    setText:(id,v)=>node(id).textContent=v,show:(id,v)=>node(id).textContent=v,
    loadQQFriends:async()=>readers.push('friends'),loadQQGroups:async()=>readers.push('groups'),
    loadNapcatProfile:async()=>readers.push('profile'),plainMessage:(n,v)=>n.textContent=v,
    appendPlainRow:(box,title,description)=>{box.textContent+=title+description;return {style:{}}}})
  vm.runInContext(source,context)
  const run=code=>vm.runInContext(code,context)
  const set=(value, trusted=true)=>{context.fixture=value;run(`qqBackendState=fixture;qqBackendTrusted=${trusted}`)}
  const load=name=>{const start=panel.indexOf('async function '+name+'(');assert.ok(start>=0);const end=panel.indexOf('\n}',start);run(panel.slice(start,end+2))}
  return {node,context,readers,run,set,load}
}

function account(provider='snowluma', revision='revision-a') {
  return {provider,revision,runtime_loaded:true,recent:[],settings:{enabled:true,posts:true,signature:true,
    audience:'friends',post_supported:provider==='snowluma',signature_supported:true,
    postIntervalHours:6,signatureIntervalHours:4,dailyPosts:2,dailySignatures:2}}
}

test('account page distinguishes provider support and escapes real history',async()=>{
  const value=account('napcat'), h=harness(async()=>({ok:true,activity:value}));h.set(state())
  value.recent=[{kind:'post',state:'unknown',text:'<private fixture>',at:1,audience:'friends'}]
  await h.context.loadQQAccountActivity()
  assert.equal(h.node('qqAccountPosts').disabled,true)
  assert.equal(h.node('qqAccountSignature').disabled,false)
  assert.match(h.node('qqAccountActivityHistory').innerHTML,/&lt;private fixture&gt;/)
  assert.match(h.node('qqAccountActivityHistory').innerHTML,/结果待确认/)
})

test('account read discards a late result after a provider change',async()=>{
  const wait=defer(), h=harness(()=>wait.promise);h.set(state('snowluma'))
  const pending=h.context.loadQQAccountActivity();h.set(state('napcat','revision-b'))
  h.node('qqAccountActivityState').textContent='current fixture'
  wait.resolve({ok:true,activity:account()});await pending
  assert.equal(h.node('qqAccountActivityState').textContent,'current fixture')
  assert.equal(h.run('qqAccountActivity'),null)
})

test('account settings CAS submits once without exposing provider credentials',async()=>{
  const writes=[], next=account('snowluma','revision-b')
  const h=harness(async(route,args)=>{
    if(route==='/qq/account-activity') return {ok:true,activity:account()}
    if(route==='/qq/backend') return {ok:true,backend:state('snowluma','revision-b')}
    writes.push(JSON.parse(args.body));return {ok:true,activity:next}
  });h.set(state('snowluma'));await h.context.loadQQAccountActivity()
  h.node('qqAccountAudience').value='self'
  await Promise.all([h.context.saveQQAccountActivity(),h.context.saveQQAccountActivity()])
  assert.equal(writes.length,1);assert.equal(writes[0].revision,'revision-a')
  assert.equal(writes[0].settings.audience,'self')
  assert.deepEqual(Object.keys(writes[0]).sort(),['revision','settings'])
  assert.equal(h.node('qqAccountSave').disabled,false)
})

test('late account save cannot replace a newer rendering of the same provider',async()=>{
  const wait=defer(),h=harness(async(route)=>route==='/qq/account-activity'?{ok:true,activity:account()}:wait.promise)
  h.set(state('snowluma'));await h.context.loadQQAccountActivity()
  const pending=h.context.saveQQAccountActivity();h.run('++qqBackendPageEpoch;qqAccountActivity=null')
  h.node('qqAccountActivityOut').textContent='new page fixture'
  wait.resolve({ok:true,activity:account('snowluma','revision-b')});await pending
  assert.equal(h.node('qqAccountActivityOut').textContent,'new page fixture')
  assert.equal(h.node('qqAccountSave').disabled,true)
})

test('unconfirmed metadata gives terminal account refresh feedback without another API request',async()=>{
  const calls=[],h=harness(async route=>{calls.push(route);return {ok:false}});h.set(state('snowluma'),false)
  await h.context.loadQQAccountActivity()
  assert.deepEqual(calls,[]);assert.match(h.node('qqAccountActivityState').textContent,/配置未确认/)
  assert.equal(h.node('qqAccountSave').disabled,true)
})

test('metadata failure revokes stale account records and loaded-state labels',async()=>{
  const h=harness(async()=>{throw new Error('inert unavailable')});h.set(state('snowluma'))
  h.context.renderQQAccountActivity(account());h.node('qqAccountActivityHistory').textContent='previous fixture'
  await h.context.loadQQBackendSettings()
  assert.match(h.node('qqAccountActivityState').textContent,/配置未确认/)
  assert.equal(h.node('qqAccountActivityHistory').textContent,'');assert.equal(h.node('qqAccountSave').disabled,true)
})

test('selected SnowLuma replaces the provider page and starts independent readers together',async()=>{
  const waiting=defer(), calls=[]
  const h=harness(async path=>{calls.push(path);return waiting.promise});h.set(state('snowluma'))
  h.node('qqBackendCommonTemplate').innerHTML='<div>shared profile</div>'
  const pending=h.context.renderQQBackendPage()
  assert.match(h.node('qqBackendPage').innerHTML,/SnowLuma 控制台/)
  assert.doesNotMatch(h.node('qqBackendPage').innerHTML,/NapCatQQ 控制台|NapNeko/)
  assert.match(h.node('qqBackendPage').innerHTML,/shared profile/)
  assert.deepEqual(h.readers,['friends','groups','profile'])
  assert.deepEqual(calls,['/qq/backend/health','/qq/account-activity'])
  waiting.resolve({ok:false});await pending
  h.context.renderQQBackendNavigation();assert.match(h.node('qqBackendNav').innerHTML,/SnowLuma/)
})

test('profile refresh clears absent cached fields and binds the avatar URL to the API and generation',async()=>{
  let profile={user_id:303,nickname:'current fixture',signature:'',hasAvatar:true}
  const h=harness(async()=>({ok:true,profile}));h.set(state('snowluma'))
  h.context.API='http://127.0.0.1:19389';h.context.FileReader=class {}
  h.node('ncNick').value='stale nickname';h.node('ncSign').value='stale signature';h.load('loadNapcatProfile')
  await h.context.loadNapcatProfile()
  assert.equal(h.node('ncNick').value,'current fixture');assert.equal(h.node('ncSign').value,'')
  const url=new URL(h.node('ncAvPrev').src)
  assert.equal(url.origin,h.context.API);assert.equal(url.pathname,'/qq-avatar.png')
  assert.equal(url.searchParams.get('backend_fingerprint'),'snowluma-revision-a')
})

test('late profile failure cannot overwrite a newly selected backend page',async()=>{
  const wait=defer(),h=harness(()=>wait.promise);h.set(state('snowluma'))
  h.node('ncNick').value='fixture edit';h.node('ncAvFile').files=[];h.load('saveNapcatProfile')
  const task=h.context.saveNapcatProfile();h.set(state('napcat','revision-b'));h.node('ncOut').textContent='current page'
  wait.reject(new Error('earlier request failed'));await task
  assert.equal(h.node('ncOut').textContent,'current page')
})

test('unavailable backend configuration gives terminal button feedback without a request',async()=>{
  const calls=[],h=harness(async path=>{calls.push(path);return {ok:true}});h.set(state('snowluma'),false)
  for(const name of ['loadQQFriends','loadQQGroups','loadNapcatProfile','saveNapcatProfile'])h.load(name)
  await h.context.loadQQBackendHealth();await h.context.loadQQFriends();await h.context.loadQQGroups()
  await h.context.loadNapcatProfile();await h.context.saveNapcatProfile()
  for(const id of ['qqBackendHealth','qqFriendCount','qqGroupCount','ncOut'])assert.match(h.node(id).textContent,/配置未确认/)
  assert.deepEqual(calls,[])
})

test('metadata failure terminates in-flight contact loading and its late response cannot restore records',async()=>{
  const wait=defer(),h=harness(path=>path==='/qq/backend'?Promise.reject(Error('fixture unavailable')):wait.promise)
  h.set(state('snowluma'));h.load('loadQQFriends')
  const task=h.context.loadQQFriends();await h.context.loadQQBackendSettings()
  assert.match(h.node('qqFriendList').textContent,/配置未确认/)
  wait.resolve({ok:true,friends:[{user_id:303,nickname:'stale fixture'}],backend:{provider:'snowluma',config_fingerprint:'snowluma-revision-a'}})
  await task
  assert.match(h.node('qqFriendList').textContent,/配置未确认/)
  assert.match(h.node('qqFriendCount').textContent,/配置未确认/)
})

test('settings failure revokes apply authority and the previous green health',async()=>{
  const h=harness(async()=>{throw Error('fixture offline')});h.set(state())
  h.node('qqBackendHealth').textContent='所选后端和账号已核验在线。'
  assert.equal(await h.context.loadQQBackendSettings(),null)
  assert.equal(h.node('qqBackendApply').disabled,true)
  assert.match(h.node('qqBackendHealth').textContent,/未确认/)
  assert.equal(h.run('qqBackendTrusted'),false)
})

test('optional-asset fallback leaves an already loaded backend module and its state intact',()=>{
  const h=harness();h.run('window=globalThis');h.set(state('snowluma'))
  const before=h.context.loadQQBackendPage
  const fallback=panel.match(/<script id="qqBackendFallback">([\s\S]*?)<\/script>/)
  assert.ok(fallback);h.run(fallback[1])
  assert.equal(h.context.loadQQBackendPage,before)
  assert.equal(h.run('qqBackendState.selected'),'snowluma');assert.equal(h.run('qqBackendTrusted'),true)
})

test('concurrent boot metadata reads share one result and cannot leave the provider page loading',async()=>{
  const waiting=defer(),calls=[],h=harness(async path=>{
    calls.push(path);return path==='/qq/backend' ? waiting.promise : {ok:false}
  })
  h.node('qqBackendPage').textContent='正在读取 QQ 接入…'
  const page=h.context.loadQQBackendPage(),settings=h.context.loadQQBackendSettings()
  assert.deepEqual(calls,['/qq/backend'])
  waiting.resolve({ok:true,backend:state('snowluma')});await Promise.all([page,settings])
  assert.match(h.node('qqBackendPage').innerHTML,/SnowLuma 控制台/)
  assert.equal(h.run('qqBackendPendingRead'),null)
})

test('late health success from another revision cannot paint the current connection green',async()=>{
  const waiting=defer(),h=harness(()=>waiting.promise);h.set(state())
  const pending=h.context.loadQQBackendHealth();h.set(state('napcat','revision-b'))
  h.node('qqBackendHealth').textContent='new connection pending'
  waiting.resolve({ok:true,health:{provider:'napcat',config_fingerprint:'napcat-revision-a',online:true,good:true,
    logged_in:true,provider_matches:true,account_matches:true}})
  await pending;assert.equal(h.node('qqBackendHealth').textContent,'new connection pending')
})

test('late failed health cannot overwrite a newer render of the same provider',async()=>{
  const waiting=defer(),h=harness(()=>waiting.promise);h.set(state())
  const pending=h.context.loadQQBackendHealth();h.set(state('napcat','revision-b'))
  h.node('qqBackendHealth').textContent='current status'
  waiting.reject(Error('old error'));await pending
  assert.equal(h.node('qqBackendHealth').textContent,'current status')
})

test('one CAS selection is submitted without credentials and concurrent clicks do not duplicate it',async()=>{
  const waiting=defer(),calls=[],h=harness(async(path,options)=>{
    calls.push({path,options});return options ? waiting.promise : {ok:true,backend:state('snowluma','revision-b')}
  });h.set(state());h.node('qqBackendSelect').value='snowluma'
  const pending=h.context.saveQQBackend();await h.context.saveQQBackend()
  assert.equal(calls.length,1);assert.deepEqual(JSON.parse(calls[0].options.body),{provider:'snowluma',revision:'revision-a'})
  waiting.resolve({ok:true,backend:state('snowluma','revision-b')});await pending
  assert.equal(h.run('qqBackendState.selected'),'snowluma')
  assert.match(h.node('qqBackendOut').textContent,/等待新连接核验/)
})

test('missing configuration or an unloaded runtime guard cannot enable selection',()=>{
  const h=harness(),value=state();value.providers[1].configured=false;value.providers[1].can_select=false
  h.set(value);h.node('qqBackendSelect').value='snowluma';h.context.renderQQBackendChoice()
  assert.equal(h.node('qqBackendApply').disabled,true)
  value.providers[1].can_select=true;h.set(value);h.context.renderQQBackendChoice()
  assert.equal(h.node('qqBackendApply').disabled,false)
  value.runtime_guard.ready=false;h.set(value);h.context.renderQQBackendChoice()
  assert.equal(h.node('qqBackendApply').disabled,true)
})

test('provider metadata stays inert and unsafe management URLs are refused',async()=>{
  const h=harness(async()=>({ok:false})),value=state('snowluma')
  value.providers[1].name='<img src=x onerror=bad()>'
  value.providers[1].connection.base_url='<script>bad()</script>'
  value.providers[1].connection.webui_url='http://external.invalid/'
  h.set(value);await h.context.renderQQBackendPage();h.context.renderQQBackendNavigation()
  assert.doesNotMatch(h.node('qqBackendPage').innerHTML,/<script>bad|href="http:\/\/external/)
  assert.match(h.node('qqBackendNav').innerHTML,/&lt;img/)
  assert.equal(h.context.qqBackendLocalUrl('http://127.0.0.1:5099/?token=secret'),'')
  assert.equal(h.context.qqBackendLocalUrl('http://name:pass@127.0.0.1:5099/'),'')
})

test('contact and profile readers discard records received for a different selected connection',async()=>{
  const h=harness(async()=>({ok:true,backend:{provider:'napcat',config_fingerprint:'old'},
    friends:[{nickname:'wrong contact'}],groups:[{group_name:'wrong group'}],profile:{nickname:'wrong profile'}}))
  h.set(state('snowluma'));h.load('loadQQFriends');h.load('loadQQGroups');h.load('loadNapcatProfile')
  await Promise.all([h.context.loadQQFriends(),h.context.loadQQGroups(),h.context.loadNapcatProfile()])
  assert.equal(h.node('qqFriendList').textContent,'');assert.equal(h.node('qqGroupList').textContent,'')
  assert.equal(h.node('ncNick').value,'')
})

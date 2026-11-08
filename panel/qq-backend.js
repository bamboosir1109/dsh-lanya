// QQ backend UI. Configuration selection and actual online health stay separate.
let qqBackendState = null;
let qqBackendReadEpoch = 0;
let qqBackendSaving = false;
let qqBackendTrusted = false;
let qqBackendPageEpoch = 0;
let qqBackendPendingRead = null;
let qqAccountActivity = null;
let qqAccountActivityEpoch = 0;
let qqAccountActivitySaving = false;

function qqBackendReadStamp(){
  return {provider:qqBackendState?.selected, revision:qqBackendState?.revision,
    fingerprint:qqBackendSelected()?.connection?.config_fingerprint, page:qqBackendPageEpoch};
}
function qqBackendAcceptRead(stamp, result){
  const current = qqBackendReadStamp();
  return qqBackendTrusted && Object.keys(current).every(key => current[key] === stamp[key]) &&
    (!result?.backend || (result.backend.provider === current.provider && result.backend.config_fingerprint === current.fingerprint));
}

function qqBackendEscape(value){
  return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function qqBackendLocalUrl(value){
  try{
    const url = new URL(value);
    return url.protocol === 'http:' && ['localhost','127.0.0.1','[::1]'].includes(url.hostname) && !url.username && !url.password && !url.search && !url.hash ? url.href : '';
  }catch{ return ''; }
}
function qqBackendSelected(){
  return (qqBackendState?.providers || []).find(row => row.provider === qqBackendState.selected) || null;
}
function renderQQBackendChoice(){
  const field = $('qqBackendSelect'), detail = $('qqBackendChoice'), button = $('qqBackendApply');
  if(!field || !detail || !button) return;
  const row = (qqBackendState?.providers || []).find(item => item.provider === field.value);
  button.disabled = !qqBackendTrusted || qqBackendSaving || !(row?.can_select || row?.configured) || !qqBackendState?.runtime_guard?.ready || row.provider === qqBackendState.selected;
  if(!qqBackendTrusted){ detail.textContent = '接入配置未确认，请重新读取。'; return; }
  if(!row){ detail.textContent = '接入配置未确认，请重新读取。'; return; }
  let text = row.configured ? '连接配置已就绪。' : '请先安装并配置 ' + row.name + '，启用 HTTP 和发往 DSH 的反向 WebSocket。';
  if(!qqBackendState.runtime_guard?.ready) text += '当前 QQ 程序还未加载切换保护，暂不能应用。';
  if(row.provider !== qqBackendState.selected) text += '应用后原接入停止代发；请确认其已退出，再启动所选后端。';
  if(row.installed && !row.configured) text += '已检测到程序，首次启动后在接入页打开它的管理页完成配置。';
  detail.textContent = text;
  setText('qqBackendConfigured', '已选择：' + (qqBackendSelected()?.name || '未确认'));
}
function renderQQBackendNavigation(){
  const row = qqBackendSelected(), nav = $('qqBackendNav');
  if(nav) nav.innerHTML = '<span class="ni">' + (row?.provider === 'snowluma' ? '❄' : '🐱') + '</span>' + qqBackendEscape(row?.name || 'QQ 接入');
  if(typeof PART !== 'undefined' && row) PART.napcat = ['QQ 接入', row.name + ' · OneBot v11'];
  for(const id of ['qqBackendTitle','qqBackendCardTitle']) setText(id, row ? row.name : 'QQ 接入');
}
async function loadQQBackendSettings(){
  if(qqBackendSaving) return qqBackendState;
  if(qqBackendPendingRead) return qqBackendPendingRead;
  const epoch = ++qqBackendReadEpoch;
  const pending = (async function(){
  try{
    const result = await api('/qq/backend', null, true);
    if(epoch !== qqBackendReadEpoch) return qqBackendState;
    if(!result.ok || !result.backend || !Array.isArray(result.backend.providers)) throw new Error(result.error || '读取接入配置失败');
    const old = qqBackendState?.selected, revision = qqBackendState?.revision, field = $('qqBackendSelect');
    qqBackendState = result.backend;
    qqBackendTrusted = true;
    if(field && (!field.value || old !== qqBackendState.selected)) field.value = qqBackendState.selected;
    renderQQBackendNavigation(); renderQQBackendChoice();
    if(old !== qqBackendState.selected || revision !== qqBackendState.revision) setText('qqBackendHealth', '连接待核验');
    return qqBackendState;
  }catch(error){
    if(epoch === qqBackendReadEpoch){
      qqBackendTrusted = false;
      setText('qqBackendChoice', '接入配置未确认：' + error.message);
      setText('qqBackendHealth', '接入配置未确认，请重新读取。');
      for(const id of ['qqFriendCount','qqGroupCount','qqFriendList','qqGroupList','ncOut']) setText(id,'接入配置未确认，请重新读取。');
      if($('qqBackendApply')) $('qqBackendApply').disabled = true;
      qqAccountActivityUnavailable('接入配置未确认，请重新读取。');
    }
    return null;
  }
  })();
  qqBackendPendingRead = pending;
  try{ return await pending; }
  finally{ if(qqBackendPendingRead === pending) qqBackendPendingRead = null; }
}
async function saveQQBackend(){
  if(qqBackendSaving || !qqBackendTrusted || !qqBackendState || !$('qqBackendSelect')) return;
  const provider = $('qqBackendSelect').value, revision = qqBackendState.revision;
  qqBackendSaving = true; ++qqBackendReadEpoch; qqBackendPendingRead = null; renderQQBackendChoice();
  try{
    const result = await api('/qq/backend', {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({provider,revision})});
    if(!result.ok || !result.backend) throw new Error(result.error || '切换结果未确认，请重新读取');
    qqBackendState = result.backend; qqBackendTrusted = true; renderQQBackendNavigation();
    show('qqBackendOut', '已选择 ' + (qqBackendSelected()?.name || provider) + '。配置原件已保留，正在等待新连接核验。');
    if(typeof dirtyPages !== 'undefined') dirtyPages.delete('system');
    if(typeof currentPage !== 'undefined' && currentPage === 'qq-backend') await renderQQBackendPage();
  }catch(error){ show('qqBackendOut', '未确认切换：' + error.message); }
  finally{ qqBackendSaving = false; await loadQQBackendSettings(); }
}
function renderQQBackendConnection(connection){
  const esc = qqBackendEscape;
  if(!connection?.available) return '<p class="note">连接配置尚未就绪，请在所选后端中完成配置。</p>';
  const ws = (connection.wsClient || []).map(row => row.url || '未确认').join('、');
  return '<dl class="qq-backend-connection"><div><dt>HTTP</dt><dd>' + esc(connection.base_url) + '</dd></div>'
    + '<div><dt>反向 WebSocket</dt><dd>' + esc(ws || '尚未启用') + '</dd></div>'
    + '<div><dt>访问令牌</dt><dd>' + (connection.token_configured ? '已配置，页面不显示内容' : '未配置') + '</dd></div></dl>';
}
async function renderQQBackendPage(){
  const page = $('qqBackendPage'), row = qqBackendSelected();
  if(!page || !row){ if(page) page.textContent = '接入配置未确认，请在部件页重新读取。'; return; }
  ++qqBackendPageEpoch;
  const snow = row.provider === 'snowluma', esc = qqBackendEscape;
  const url = qqBackendLocalUrl(row.connection?.webui_url || row.webui_url || '');
  const vendor = snow ? 'https://github.com/SnowLuma/SnowLuma/releases' : 'https://github.com/NapNeko/NapCatQQ/releases';
  const profile = $('qqBackendCommonTemplate')?.innerHTML || '';
  page.innerHTML = '<div class="phead"><h1 id="qqBackendTitle">' + esc(row.name) + '</h1><p>'
    + (snow ? 'SnowLuma 接入。注入、账号登录和网络适配器在它自己的 WebUI 中管理。' : 'NapCatQQ 接入。管理 QQ 连接、资料和联系人。') + '</p></div>'
    + '<div class="card"><h3>' + (snow ? 'SnowLuma 控制台' : 'NapCatQQ 控制台') + '</h3>'
    + '<p class="note">先核对所选后端，再确认连接和登录。首次协议、改密或扫码在后端控制台完成。</p>'
    + '<div class="btnrow">' + (url ? '<a class="qq-backend-link" href="' + esc(url) + '" target="_blank" rel="noreferrer">打开管理页</a>' : '')
    + '<button onclick="doAction(\'qq-backend\',\'qqBackendStartOut\')">启动 / 检查接入</button>'
    + '<button onclick="loadQQBackendHealth()">检查连接</button>'
    + '<a class="qq-backend-link" href="' + vendor + '" target="_blank" rel="noreferrer">官方下载</a></div>'
    + '<p class="note" id="qqBackendHealth">连接待核验</p><div class="out" id="qqBackendStartOut"></div></div>'
    + '<div class="card"><h3>所选连接</h3>' + renderQQBackendConnection(row.connection) + '</div>'
    + renderQQAccountActivityCard() + profile;
  page.dataset.provider = row.provider;
  await Promise.all([loadQQBackendHealth(), loadQQAccountActivity(), ...(row.configured ? [loadQQFriends(), loadQQGroups(), loadNapcatProfile()] : [])]);
}
async function loadQQBackendPage(){
  if(await loadQQBackendSettings()) await renderQQBackendPage();
}
async function loadQQBackendHealth(){
  if(!qqBackendTrusted){ setText('qqBackendHealth','接入配置未确认，请重新读取。'); return; }
  const selected = qqBackendState?.selected, stamp = qqBackendReadStamp();
  setText('qqBackendHealth','正在核对连接…');
  try{
    const result = await api('/qq/backend/health', null, true), health = result.health;
    if(!qqBackendAcceptRead(stamp)) return;
    const online = result.ok && health?.provider === selected && health.config_fingerprint === qqBackendSelected()?.connection?.config_fingerprint && health.online && health.good && health.logged_in && health.provider_matches && health.account_matches;
    setText('qqBackendHealth', online ? '所选后端和账号已核验在线。接口接受与手机实收仍按每次发送确认。' : '连接未就绪：' + (result.error || '请核对后端、账号登录与反向 WebSocket。'));
  }catch(error){ if(qqBackendAcceptRead(stamp)) setText('qqBackendHealth','连接未确认：' + error.message); }
}

function qqAccountActivityUnavailable(message){
  qqAccountActivity = null; setText('qqAccountActivityState',message); setText('qqAccountActivityHistory','');
  for(const id of ['qqAccountSave','qqAccountEnabled','qqAccountPosts','qqAccountSignature','qqAccountAudience']) if($(id)) $(id).disabled = true;
}

function renderQQAccountActivityCard(){
  qqAccountActivity = null;
  return '<div class="card"><h3>她的说说与签名</h3>'
    + '<p class="note" id="qqAccountActivityState">正在核对主 agent 和当前账号…</p>'
    + '<p class="note">由同一个主 LLM 根据当前兴趣决定，不要求每天更新。夜间 00:00–08:00 休息，未知结果保留，不自动重发。</p>'
    + '<div class="policy-grid"><label class="field"><span>自主更新</span><input id="qqAccountEnabled" type="checkbox" disabled></label>'
    + '<label class="field"><span>允许发说说</span><input id="qqAccountPosts" type="checkbox" disabled></label>'
    + '<label class="field"><span>允许改签名</span><input id="qqAccountSignature" type="checkbox" disabled></label>'
    + '<label class="field"><span>说说可见范围</span><select id="qqAccountAudience" disabled><option value="friends">好友可见</option><option value="self">仅自己可见</option></select></label></div>'
    + '<p class="note" id="qqAccountActivityLimits"></p>'
    + '<div class="btnrow"><button id="qqAccountSave" onclick="saveQQAccountActivity()" disabled>保存自主设置</button>'
    + '<button onclick="loadQQAccountActivity()">重读动态记录</button></div>'
    + '<div class="out" id="qqAccountActivityOut"></div><div class="record-list" id="qqAccountActivityHistory"></div></div>';
}

function renderQQAccountActivity(value){
  const settings = value.settings;
  if(!settings || typeof settings.enabled !== 'boolean' || typeof value.runtime_loaded !== 'boolean' || !Array.isArray(value.recent)) throw new Error('动态记录接口不匹配');
  for(const [id,key] of [['qqAccountEnabled','enabled'],['qqAccountPosts','posts'],['qqAccountSignature','signature']]){
    $(id).checked = settings[key] === true; $(id).disabled = false;
  }
  $('qqAccountPosts').disabled = !settings.post_supported;
  $('qqAccountSignature').disabled = !settings.signature_supported;
  $('qqAccountAudience').value = settings.audience; $('qqAccountAudience').disabled = !settings.post_supported;
  $('qqAccountSave').disabled = !value.revision || qqAccountActivitySaving;
  setText('qqAccountActivityState', (settings.enabled ? '自主更新已开启' : '自主更新未开启')
    + (value.runtime_loaded ? ' · 主 agent 已加载' : ' · 主 agent 尚未加载这项能力')
    + (settings.post_supported ? '' : ' · 当前接入未提供说说发布'));
  setText('qqAccountActivityLimits', '说说至少间隔 ' + settings.postIntervalHours + ' 小时、每天最多 ' + settings.dailyPosts
    + ' 条；签名至少间隔 ' + settings.signatureIntervalHours + ' 小时、每天最多 ' + settings.dailySignatures + ' 次。没有合适内容可以保持不变。');
  const names = {post:'说说',signature:'签名'}, states = {accepted:'接口已接受',reserved:'结果待确认',unknown:'结果待确认',rejected:'接口未接受'};
  $('qqAccountActivityHistory').innerHTML = value.recent.length ? value.recent.map(row =>
    '<div class="record-item"><strong>' + qqBackendEscape(names[row.kind] || '动态') + ' · ' + qqBackendEscape(states[row.state] || '待核对')
    + '</strong><p>' + qqBackendEscape(row.text) + '</p><p class="note">' + qqBackendEscape(new Date(row.at).toLocaleString('zh-CN'))
    + (row.kind === 'post' ? ' · ' + (row.audience === 'friends' ? '好友可见' : '仅自己可见') : '') + '</p></div>').join('')
    : '<div class="empty">尚无本机发布记录。开启后由她自主决定，不会为了填记录发布测试内容。</div>';
}

async function loadQQAccountActivity(){
  if(!qqBackendTrusted){ qqAccountActivityUnavailable('接入配置未确认，请重新读取。'); return; }
  const stamp = qqBackendReadStamp(), epoch = ++qqAccountActivityEpoch;
  setText('qqAccountActivityState','正在读取当前账号的动态…');
  try{
    const result = await api('/qq/account-activity', null, true);
    if(epoch !== qqAccountActivityEpoch || !qqBackendAcceptRead(stamp)) return;
    const value = result.activity;
    if(!result.ok || value?.provider !== stamp.provider || value?.revision !== stamp.revision) throw new Error(result.error || '配置有变化，请重新读取接入页');
    renderQQAccountActivity(value); qqAccountActivity = value;
  }catch(error){
    if(epoch === qqAccountActivityEpoch && qqBackendAcceptRead(stamp)){
      qqAccountActivityUnavailable('动态记录未确认：' + error.message);
    }
  }
}

async function saveQQAccountActivity(){
  if(qqAccountActivitySaving || !qqAccountActivity?.revision || !qqBackendTrusted) return;
  const stamp = qqBackendReadStamp(), before = qqAccountActivity;
  const keys = ['enabled','posts','signature','audience','postIntervalHours','signatureIntervalHours','dailyPosts','dailySignatures'];
  const settings = Object.fromEntries(keys.map(key => [key,before.settings[key]]));
  settings.enabled = $('qqAccountEnabled').checked; settings.posts = $('qqAccountPosts').checked;
  settings.signature = $('qqAccountSignature').checked; settings.audience = $('qqAccountAudience').value;
  qqAccountActivitySaving = true; ++qqAccountActivityEpoch; $('qqAccountSave').disabled = true;
  show('qqAccountActivityOut','保存中…');
  try{
    const result = await api('/qq/account-activity/settings', {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({revision:before.revision,settings})});
    if(!qqBackendAcceptRead(stamp)) return;
    if(!result.ok || result.activity?.provider !== stamp.provider) throw new Error(result.error || '保存未确认，请重读设置');
    await loadQQBackendSettings();
    if(qqBackendPageEpoch !== stamp.page || !qqBackendTrusted || qqBackendState?.selected !== stamp.provider) return;
    if(result.activity.revision !== qqBackendState.revision) throw new Error('设置已再次变化，请重读接入页');
    qqAccountActivity = result.activity; renderQQAccountActivity(result.activity);
    show('qqAccountActivityOut', result.activity.runtime_loaded ? '已保存，新自主决策会读取设置。' : '已保存，主 agent 需加载后才能使用。');
  }catch(error){
    if(qqBackendPageEpoch === stamp.page && qqBackendState?.selected === stamp.provider){
      qqAccountActivity = null; show('qqAccountActivityOut','保存未确认：' + error.message);
    }
  }
  finally{
    qqAccountActivitySaving = false;
    if($('qqAccountSave')) $('qqAccountSave').disabled = !qqBackendTrusted || !qqAccountActivity?.revision
      || qqAccountActivity.provider !== qqBackendState?.selected || qqAccountActivity.revision !== qqBackendState?.revision;
  }
}

'use strict';
let state=null, selected=null, settingsDirty=false, busy=false, connectionError=false;
let pollTimer=null, pollingPaused=false, statusFailures=0, refreshing=false, refreshQueued=false;
let settingsActor='aion';
const actorDrafts={};
const $=id=>document.getElementById(id);
const settingFields=['interval_seconds','context_messages','image_wait_seconds','hourly_limit','total_hourly_limit'];
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date=value=>value?new Date(value*1000).toLocaleString('zh-CN',{hour12:false}):'尚无';
const folded=value=>{const text=String(value||'');return text.length>140?`<details class="long"><summary>${esc(text.slice(0,90))}…</summary><p>${esc(text)}</p><button type="button" data-collapse>收起</button></details>`:`<p>${esc(text)}</p>`;};
function notice(text,error=false){$('notice').textContent=text;$('notice').classList.toggle('error',error);}
async function api(path,method='GET',body){const res=await fetch('/api/qq-social/'+path,{method,headers:{'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})});const data=await res.json();if(!res.ok){const error=Error(data.detail||'请求失败');error.retryStopped=Boolean(data.retry_stopped);throw error;}return data;}
function stopPolling(){pollingPaused=true;if(pollTimer!==null)clearInterval(pollTimer);pollTimer=null;}
function startPolling(){pollingPaused=false;statusFailures=0;if(pollTimer===null)pollTimer=setInterval(()=>{if(!document.hidden)refresh();},8000);}
function credentialNotice(text,error=false){$('credentialNotice').textContent=text;$('credentialNotice').classList.toggle('error',error);}
async function loadCredentials(initial=false,updatedActor=null){
 const data=await api('credentials');
 const count=Object.values(data.actors).filter(a=>a.configured).length;
 $('credentialCount').textContent=count?`${count} 位已配置`:'尚未配置';
 if(initial&&!count)$('credentialPanel').open=true;
 for(const [actor,a] of Object.entries(data.actors)){
  let form=$('credentialForms').querySelector(`[data-actor="${actor}"]`);
  if(!form){
   $('credentialForms').insertAdjacentHTML('beforeend',`<form class="credential-form" data-actor="${actor}" autocomplete="off"><h3></h3><label>AppID<input name="app_id" type="text" maxlength="64" inputmode="numeric" autocomplete="off" required></label><label>AppSecret<input name="secret" type="password" maxlength="2048" autocomplete="new-password" spellcheck="false"></label><button type="submit">保存并连接</button></form>`);
   form=$('credentialForms').querySelector(`[data-actor="${actor}"]`);
   form.addEventListener('submit',saveCredential);
  }
  form.querySelector('h3').textContent=a.name+(a.configured?' · 已配置':' · 未配置');
  if(initial||actor===updatedActor)form.elements.app_id.value=a.app_id||'';
  form.elements.secret.placeholder=a.configured?'已保存，留空保持原密钥':'填写该机器人的 AppSecret';
 }
}
async function saveCredential(event){
 event.preventDefault();if(busy)return;
 const form=event.currentTarget, actor=form.dataset.actor;
 const payload={actor,app_id:form.elements.app_id.value.trim(),secret:form.elements.secret.value.trim()};
 busy=true;form.querySelector('button').disabled=true;
 credentialNotice('正在验证并保存…');
 try{
  const result=await api('credentials','PATCH',payload);
  if(result.connection==='connecting')startPolling();else stopPolling();
  form.elements.secret.value='';
  await loadCredentials(false,actor);
  if(result.connection==='restart_required')credentialNotice('凭据已保存。当前 QQ 服务还是旧版本，请重启小家与 QQ 一次；之后可直接在这里保存生效。',true);
  else if(result.connection==='retry_required')credentialNotice('凭据已保存，连接尚未完成。请检查连接状态，稍后再点保存并连接。',true);
  else credentialNotice('已保存，正在连接 QQ。在线后打开总开关，在目标群 @ 机器人。');
 }catch(error){credentialNotice(error.message,true);}
 finally{form.elements.secret.value='';busy=false;form.querySelector('button').disabled=false;await refresh();}
}
function renderActorSettings(){
 const actor=state.actors[settingsActor],cfg=actor?.settings;
 $('actorTabs').innerHTML=Object.entries(state.actors).map(([id,a])=>`<button type="button" data-settings-actor="${id}" aria-pressed="${id===settingsActor}">${esc(a.name)}</button>`).join('');
 $('settingsTitle').textContent=(actor?.name||'AI')+'的回复设置';
 for(const input of $('settings').querySelectorAll('input,button'))input.disabled=!cfg;
 for(const button of $('modeControls').querySelectorAll('button')){button.disabled=!cfg;button.setAttribute('aria-pressed',String(button.dataset.mode===cfg?.mode));}
 if(!cfg){$('modeLabel').textContent='需更新服务';$('modeHint').textContent='独立设置需要重启小家与 QQ 后生效。';return;}
 $('modeLabel').textContent=!state.settings.enabled?'总开关已关':cfg.enabled?'点选即生效':'这位已暂停';
 $('modeHint').textContent=!state.settings.enabled?'总开关已关闭；仍可分别设置下次开启后的回复方式。':!cfg.enabled?'这位已暂停，@ 回复、主动判断和 QQ 记忆整理都停止。':cfg.mode==='natural'?'这位被 @ 会回应，也会按自己的频率接话。':'这位只有被 @ 才回复，普通聊天和单独引用不触发。';
 $('rateLabel').textContent=cfg.interval_seconds+' 秒 · '+cfg.context_messages+' 条';
 if(!settingsDirty){for(const key of settingFields)$('settings').elements[key].value=cfg[key];}
 $('settings').querySelector('button[type="submit"]').textContent='保存'+(actor?.name||'这位 AI')+'的参数';
}
function actorModelSelect(actor,a){
 const models=state.models, selected=a.settings?.model||'';
 if(!Array.isArray(models))return `<p class="model" title="重启小家与 QQ 后可单独选择模型">${esc(a.model||'未选择模型')}</p>`;
 const inherited=a.home_model||'未选择模型';
 const missing=selected&&!models.includes(selected)?`<option value="${esc(selected)}" selected disabled>${esc(selected)}（不可用）</option>`:'';
 return `<select class="model-select" data-actor-model="${actor}" aria-label="${esc(a.name)}的 QQ 模型" title="${esc(a.model||inherited)}"><option value="" ${selected?'':'selected'}>跟随聊天室 · ${esc(inherited)}</option>${missing}${models.map(key=>`<option value="${esc(key)}" ${key===selected?'selected':''}>${esc(key)}</option>`).join('')}</select>`;
}
async function refresh(force=false){
 if(busy||(pollingPaused&&!force))return;
 if(refreshing){if(force)refreshQueued=true;return;}
 refreshing=true;
 try{
  state=await api('status');
  statusFailures=0;
  if(connectionError){notice('');connectionError=false;}
  $('power').disabled=false;$('power').textContent=state.settings.enabled?'已开启 · 关闭':'已关闭 · 开启';$('power').classList.toggle('off',!state.settings.enabled);
  const actorOpen=[...$('actors').querySelectorAll('details[open]')].map(d=>d.dataset.actor);
  if(force||!document.activeElement?.matches('[data-actor-model]'))$('actors').innerHTML=Object.entries(state.actors).map(([actor,a])=>`<article class="actor"><h2>${esc(a.name)}<span class="badge">${esc(a.state)}</span></h2>${actorModelSelect(actor,a)}<label class="actor-participation"><input type="checkbox" data-actor-switch="${actor}" ${a.settings?.enabled?'checked':''} ${a.settings?'':'disabled'}>参与群聊</label><details data-actor="${actor}" ${actorOpen.includes(actor)?'open':''}><summary>连接详情</summary><p class="muted">收到：${date(a.last_event?.at)}<br>发送：${date(a.last_send)}</p>${a.context?.home_messages!==undefined?`<p class="muted">上次参考 ${a.context.home_messages} 条上下文、${a.context.recalled_memories} 条自身记忆</p>`:''}</details>${a.error||a.reply_error?`<p class="error">${esc(a.error||a.reply_error)}</p>`:''}</article>`).join('');
  renderActorSettings();
  const configured=Object.values(state.actors).filter(a=>a.state!=='未配置');
  if(configured.length&&configured.every(a=>a.retry_stopped)){stopPolling();notice('QQ连接已停止，请点击手动连接。');}
  $('bringHome').checked=state.settings.bring_home??true;
  $('bringHome').disabled=state.settings.bring_home===undefined;
  await refreshActivity();
  if($('outsidePanel').open&&$('activitySummaryPanel').open)await refreshActivitySummaries();
  if($('outsidePanel').open)await refreshOutside();
  $('groups').innerHTML=state.groups.length?state.groups.map(g=>`<div class="group"><div class="row"><div><strong>${esc(g.name||'朋友群 · '+g.group_id.slice(-6))}</strong><br><small>${esc(state.actors[g.actor]?.name||g.actor)} · ${g.full_messages?'可见普通群消息':'等待普通群消息'}</small></div><button data-detail="${g.id}">聊天与记忆</button></div><div class="switches"><label><input type="checkbox" data-group="${g.id}" data-field="enabled" ${g.enabled?'checked':''}> 参与</label><label><input type="checkbox" data-group="${g.id}" data-field="memory_enabled" ${g.memory_enabled?'checked':''}> 积累记忆</label></div></div>`).join(''):'<p class="empty">在目标群 @ 已接入的 AI，即可完成首次配对。</p>';
  if(selected)await detail(selected);
 }catch(e){
  connectionError=true;statusFailures++;
  if(e.retryStopped||statusFailures>=3)stopPolling();
  notice(pollingPaused?e.message+'；已停止自动查询，点击手动连接后重新尝试。':e.message,true);
  $('power').disabled=true;$('power').textContent='服务未连接';$('modeLabel').textContent='服务未连接';
  for(const b of $('modeControls').querySelectorAll('button'))b.disabled=true;
 }finally{refreshing=false;if(refreshQueued){refreshQueued=false;await refresh(true);}}
}
async function manualConnect(){
 if(busy||refreshing)return;
 busy=true;$('connect').disabled=true;notice('正在启动连接，最多尝试 3 次…');
 try{
  const result=await api('connect','POST');
  if(result.connection==='restart_required'){stopPolling();notice('请先重启小家与 QQ，更新后的手动连接才会生效。',true);}
  else if(result.connection==='retry_required'){stopPolling();notice('连接未启动，请检查本地配置后再点击手动连接。',true);}
  else startPolling();
 }catch(e){stopPolling();notice(e.message,true);}
 finally{busy=false;$('connect').disabled=false;await refresh();}
}
$('connect').onclick=manualConnect;
async function detail(id){
 selected=Number(id);const d=await api('groups/'+selected);$('detail').hidden=false;
 const names=Object.fromEntries(d.members.map(m=>[m.member_id,m.alias||m.name||'成员-'+m.member_id.slice(-6)]));
 $('detailTitle').textContent=(d.group.name||'朋友群')+' · '+(state.actors[d.group.actor]?.name||'AI');
 $('memberCount').textContent=d.members.length;$('memoryCount').textContent=d.memories.length;$('messageCount').textContent=Math.min(d.messages.length,30);
 const summaryMarkup=folded(d.group.summary||'聊过一段时间后，空闲时整理。');
 if($('summary').dataset.version!==summaryMarkup){$('summary').innerHTML=summaryMarkup;$('summary').dataset.version=summaryMarkup;}
 $('members').innerHTML=d.members.map(m=>`<div class="entry row"><span>${esc(names[m.member_id])} ${m.is_owner?'· 你':''}</span><button data-member="${esc(m.member_id)}" data-alias="${esc(names[m.member_id])}">备注 / 身份</button></div>`).join('');
 const memoryMarkup=d.memories.length?d.memories.map(m=>`<div class="entry"><div class="row"><small>${esc(names[m.member_id]||'成员-'+m.member_id.slice(-6))} · ${date(m.created_at)}</small><button data-delete="${m.id}">删除</button></div>${folded(m.content)}</div>`).join(''):'<p class="empty">还没有交往记忆。</p>';
 const messageMarkup=d.messages.slice(-30).map(m=>`<div class="entry"><small>${esc(m.direction==='out'?state.actors[d.group.actor]?.name:names[m.member_id]||m.name)} · ${date(m.created_at)}</small>${folded(m.content)}</div>`).join('');
 if($('memories').dataset.version!==memoryMarkup){$('memories').innerHTML=memoryMarkup;$('memories').dataset.version=memoryMarkup;}
 if($('messages').dataset.version!==messageMarkup){$('messages').innerHTML=messageMarkup;$('messages').dataset.version=messageMarkup;}
}
async function action(fn){if(busy)return;busy=true;$('power').disabled=true;for(const b of $('modeControls').querySelectorAll('button'))b.disabled=true;for(const select of $('actors').querySelectorAll('select'))select.disabled=true;try{await fn();notice('已保存');}catch(e){notice(e.message,true);}finally{busy=false;await refresh(true);}}
$('power').onclick=()=>action(()=>api('settings','PATCH',{enabled:!state.settings.enabled}));
$('actors').onchange=e=>{const target=e.target,actor=target.dataset.actorModel||target.dataset.actorSwitch;if(actor)action(()=>api('actors/'+actor+'/settings','PATCH',target.dataset.actorModel?{model:target.value}:{enabled:target.checked}));};
$('actorTabs').onclick=e=>{
 const actor=e.target.closest('[data-settings-actor]')?.dataset.settingsActor;
 if(!actor||actor===settingsActor||busy)return;
 if(settingsDirty)actorDrafts[settingsActor]=Object.fromEntries(settingFields.map(k=>[k,$('settings').elements[k].value]));
 settingsActor=actor;settingsDirty=Boolean(actorDrafts[actor]);renderActorSettings();
 if(settingsDirty)for(const k of settingFields)$('settings').elements[k].value=actorDrafts[actor][k];
};
$('bringHome').onchange=e=>action(()=>api('settings','PATCH',{bring_home:e.target.checked}));
async function refreshActivity(){
 try{const cfg=await api('activity');$('activitySync').checked=cfg.enabled;$('activitySync').disabled=false;
 $('activityHint').textContent='各自保留两小时内的短摘要，超过一小时无更新就略过。小家只读本地缓存，不额外调用模型。关闭会清空缓存并停止更新与加载，永久记忆不受影响。';
 }catch(e){$('activitySync').disabled=true;$('activityHint').textContent='近期互动设置暂不可用；刚升级时请重启小家与 QQ。';}
}
$('activitySync').onchange=e=>action(()=>api('activity','PATCH',{enabled:e.target.checked}));
async function refreshActivitySummaries(){
 let markup;
 try{
  const data=await api('activity/summaries');
  const count=Object.values(data.actors).reduce((total,rows)=>total+rows.length,0);
  $('activitySummaryCount').textContent=data.enabled?`${count} 条`:'已关闭';
  markup=data.enabled?Object.entries(data.actors).map(([actor,rows])=>`<div class="entry"><strong>${esc(state?.actors[actor]?.name||'AI')}</strong>${rows.length?rows.map(row=>`<div class="entry"><small>${esc(row.platform==='qq'?'QQ':row.platform)} · ${esc(row.label||'外部聊天')}<br>更新：${date(row.updated_at)}<br>无新摘要时，略过于：${date(row.expires_at)}</small>${folded(row.summary)}</div>`).join(''):'<p class="muted">暂无有效摘要，下一次群聊回复后再来看看。</p>'}</div>`).join(''):'<p class="muted">近期互动同步已关闭，短期缓存已清空。</p>';
 }catch(e){
  $('activitySummaryCount').textContent='';
  markup='<p class="muted">摘要暂时无法读取；刚升级时请重启小家与 QQ 后刷新。</p>';
 }
 if($('activitySummaries').dataset.version!==markup){$('activitySummaries').innerHTML=markup;$('activitySummaries').dataset.version=markup;}
}
$('activitySummaryPanel').ontoggle=()=>{if($('activitySummaryPanel').open)refreshActivitySummaries();};
$('activitySummaries').onclick=e=>{const b=e.target.closest('[data-collapse]');if(b)b.closest('details').open=false;};
async function refreshOutside(){
 const res=await fetch('/api/external-memories');if(!res.ok)throw Error(res.status===404?'新功能已准备好，重启小家和 QQ 服务后启用。':'外出回忆暂时无法读取');const data=await res.json();
 const markup=Object.entries(data).map(([actor,rows])=>`<div class="entry"><div class="row"><strong>${esc(state?.actors[actor]?.name||'AI')}</strong><a href="/memory?store=${actor==='aion'?'main':'chatroom'}&kind=friend">管理朋友记忆</a></div>${state?.actors[actor]?.context?.memory_status==='failed'?'<p class="error">上次带回失败，未确认保存。</p>':''}${rows.length?rows.map(m=>`<div class="entry"><small>${date(m.created_at)}</small>${folded(m.content)}</div>`).join(''):'<p class="muted">还没有带回的回忆，不必每次聊天都记。</p>'}</div>`).join('');
 if($('outsideMemories').dataset.version!==markup){$('outsideMemories').innerHTML=markup;$('outsideMemories').dataset.version=markup;}
}
$('outsidePanel').ontoggle=()=>{if($('outsidePanel').open){refreshOutside().catch(e=>notice(e.message,true));if($('activitySummaryPanel').open)refreshActivitySummaries();}};
$('outsideMemories').onclick=e=>{const b=e.target.closest('[data-collapse]');if(b)b.closest('details').open=false;};
$('modeControls').onclick=e=>{const b=e.target.closest('[data-mode]');const actor=settingsActor;if(b&&state?.actors[actor]?.settings&&b.dataset.mode!==state.actors[actor].settings.mode)action(()=>api('actors/'+actor+'/settings','PATCH',{mode:b.dataset.mode}));};
$('settings').oninput=()=>{settingsDirty=true;};
$('presets').onclick=e=>{const b=e.target.closest('[data-interval]');if(!b)return;const f=$('settings');f.elements.interval_seconds.value=b.dataset.interval;f.elements.hourly_limit.value=b.dataset.hourly;settingsDirty=true;notice('已填入快捷值，点击保存参数后生效');};
$('settings').onsubmit=e=>{e.preventDefault();const f=e.currentTarget,actor=settingsActor;action(async()=>{await api('actors/'+actor+'/settings','PATCH',Object.fromEntries(settingFields.map(k=>[k,Number(f.elements[k].value)])));settingsDirty=false;delete actorDrafts[actor];});};
$('groups').onchange=e=>{const t=e.target;if(t.dataset.group)action(()=>api('groups/'+t.dataset.group,'PATCH',{[t.dataset.field]:t.checked}));};
$('groups').onclick=e=>{const b=e.target.closest('[data-detail]');if(b)detail(b.dataset.detail).catch(e=>notice(e.message,true));};
$('detail').onclick=e=>{const collapse=e.target.closest('[data-collapse]');if(collapse)collapse.closest('details').open=false;const d=e.target.closest('[data-delete]');if(d)action(()=>api('memories/'+d.dataset.delete,'DELETE'));const m=e.target.closest('[data-member]');if(m){const alias=prompt('给这位群成员的备注名',m.dataset.alias);if(alias===null)return;const owner=confirm('这位是你本人（小家的用户）吗？\n确定：标记为你；取消：普通朋友。');action(()=>api('groups/'+selected+'/members/'+encodeURIComponent(m.dataset.member),'PATCH',{alias,is_owner:owner}));}};
loadCredentials(true).catch(()=>{$('credentialCount').textContent='暂不可用';credentialNotice('接入设置未加载。若刚更新代码，请重启小家后刷新页面；仍失败请检查服务。',true);});
startPolling();refresh();

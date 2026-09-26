/* Repair room owns its state, requests and rendering; no companion chat globals. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const chatScroll=window.createRepairScroll($('conversation'),$('jumpLatest'));
  const phaseNames = {discussion:'讨论中',executing:'执行中',review:'待你验收',paused:'已暂停',completed:'已完成'};
  const md = window.markdownit({html:false,breaks:true,linkify:false});
  const state = {task:null,active:null,names:{},after:0,messageAfter:0,files:[],poll:null,loading:false,live:new Map(),log:new Map(),epoch:0,planDirty:false,planEditRevision:0,tab:'chat',submitting:false,pendingSince:0,connectionLost:false,workerOnline:false,lastActivity:null,backlog:false};
  let noticeTimer;
  let timeline=[];
  let acceptingTask=null;
  let uploadBatch=null;
  function showModel(model){const el=$('workModel');el.hidden=!model?.name;el.textContent=model?.name||'';el.title=(model?.running?'本轮执行模型':'下轮工作模型')+' · 点击更换';el.setAttribute('aria-label',el.title+'：'+el.textContent);}
  let renameTarget=null;
  function renameDialog(task){renameTarget={id:task.id,title:task.title};$('taskDrawer').close();$('renameTitle').value=task.title;$('renameDialog').showModal();$('renameTitle').focus();$('renameTitle').select();}
  function notice(text) { $('notice').textContent=text; $('notice').hidden=false; clearTimeout(noticeTimer); noticeTimer=setTimeout(()=>{$('notice').hidden=true;},6500); }
  async function api(path, options={}) {
    const headers = {'X-Repair-Request':'1',...(options.headers||{})};
    if (options.body && !(options.body instanceof FormData)) {headers['Content-Type']='application/json';options.body=JSON.stringify(options.body);}
    const controller=new AbortController();
    const timeout=setTimeout(()=>controller.abort(),options.method&&options.method!=='GET'?120000:20000);
    let response,data;
    try {response=await fetch('/api/repair'+path,{...options,headers,cache:'no-store',signal:controller.signal});data=await response.json();}
    catch {const err=new Error('连接中断或等待超时，正在核对任务状态；请先不要重复发送。');err.connectionLost=true;throw err;}
    finally {clearTimeout(timeout);}
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求未完成，请检查输入后重试。');
    return data;
  }
  function action(fn) { return async event => {event?.preventDefault();try {await fn(event);} catch(err){notice(err.message);}}; }
  function tab(name) {state.tab=name;toolsOpen(false);for(const part of ['chat','plan']) $(part+'Panel').hidden=part!==name;document.querySelectorAll('[data-tab]').forEach(b=>{const selected=b.dataset.tab===name;b.classList.toggle('selected',selected);b.setAttribute('aria-pressed',String(selected));});}
  function toolsOpen(open) {$('composerTools').hidden=!open;$('toolsToggle').setAttribute('aria-expanded',String(open));}
  function resizeDraft() {const el=$('draft');el.style.height='44px';el.style.height=Math.min(112,el.scrollHeight+2)+'px';chatScroll.layout();}
  function recipientHint() {const recipient=$('recipient').value,name=$('recipient').selectedOptions[0]?.textContent||state.names.connor||'执行者';$('draft').placeholder='和'+name+'聊聊…';$('send').title='发送给'+name;$('participants').textContent=[state.names.user,...(recipient==='both'?[state.names.aion,state.names.connor]:[state.names[recipient]])].filter(Boolean).join(' · ');}
  function linkAttachment(parent,file) {
    const a=document.createElement('a');a.className='attachment';a.href='/api/repair/files/'+encodeURIComponent(file.id);a.download=file.name;a.textContent='↓ '+file.name;
    a.onclick=action(async event=>{
      const native=window.AionRepairFiles;
      if(native?.save){native.save(a.href,file.name);return;}
      if(window.AionImageSaver?.save && /\.(png|jpe?g|webp|gif)$/i.test(file.name)) {
        const response=await fetch(a.href);if(!response.ok)throw new Error('附件领取失败，请重新连接维修室。');
        const blob=await response.blob();const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=reject;reader.readAsDataURL(blob);});
        window.AionImageSaver.save(String(data).split(',')[1],file.name);return;
      }
      if(window.AionImageSaver){notice('当前 App 版本尚不支持普通附件保存，请更新 App 或用手机浏览器进入维修室领取。');return;}
      const download=document.createElement('a');download.href=a.href;download.download=file.name;document.body.append(download);download.click();download.remove();
    });
    if (/\.(png|jpe?g|webp|gif)$/i.test(file.name)) {const img=document.createElement('img');img.src=a.href+'?preview=true';img.alt=file.name;img.loading='lazy';a.prepend(img);}
    parent.append(a);
  }
  // Persisted messages and tool events share one timeline, including paged history.
  function insertTimeline(el, created) {
    el.dataset.created=String(created);
    let index=timeline.length;
    while(index>0&&Number(timeline[index-1].dataset.created)>created)index--;
    timeline.splice(index,0,el);
  }
  function groupWorkRecords() {
    const rows=[],used=new Set();
    for(let i=0;i<timeline.length;){
      const node=timeline[i];
      if(!node.matches('.event,.message.system')){rows.push(node);i++;continue;}
      const items=[];
      while(i<timeline.length&&timeline[i].matches('.event,.message.system'))items.push(timeline[i++]);
      const previous=items.map(el=>el.closest('.work-records')).filter(Boolean);
      let group=previous.find(el=>!used.has(el));
      if(!group){group=document.createElement('details');group.className='work-records';const summary=document.createElement('summary');const body=document.createElement('div');body.className='work-record-body';group.append(summary,body);}
      if(previous.some(el=>el!==group&&el.open))group.open=true;
      used.add(group);
      const running=items.some(el=>el.classList.contains('running')),failed=items.some(el=>el.classList.contains('error'));
      group.firstElementChild.textContent='工作记录 · '+items.length+' 条'+(running?' · 进行中':failed?' · 有异常':'');
      group.classList.toggle('has-error',failed);
      const body=group.lastElementChild;
      items.forEach((el,index)=>{if(body.children[index]!==el)body.insertBefore(el,body.children[index]||null);});
      rows.push(group);
    }
    const root=$('messages');
    rows.forEach((el,index)=>{if(root.children[index]!==el)root.insertBefore(el,root.children[index]||null);});
    const keep=new Set(rows);for(const el of [...root.children])if(!keep.has(el))el.remove();
  }
  function addMessage(message) {
    if (message.id<=state.messageAfter) return;
    const article=document.createElement('article');article.className='message '+message.who;
    article.dataset.messageId=message.id;
    const by=document.createElement('small');by.textContent=(state.names[message.who]||message.who)+' · '+new Date(message.created*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});article.append(by);
    const body=document.createElement('div');body.innerHTML=md.render(message.text||'');article.append(body);
    for(const file of message.attachments||[])linkAttachment(article,file);
    insertTimeline(article,message.created);state.messageAfter=message.id;
  }
  function clearLive(key) {
    const entries=key!==undefined?[state.live.get(key)].filter(Boolean):[...state.live.values()];
    for(const entry of entries){entry.el.remove();timeline=timeline.filter(el=>el!==entry.el);}
    if(key!==undefined)state.live.delete(key);else state.live.clear();
  }
  function recordActivity(event,title) {
    if(!state.lastActivity||event.created>=state.lastActivity.created)state.lastActivity={created:event.created,title};
  }
  function logEvent(event) {
    if(event.id<=state.after)return;
    state.after=event.id;const data=event.data;
    if(event.kind==='text') {
      recordActivity(event,(state.names[data.who||'connor']||'执行者')+' 正在回复');
      if(!state.active||event.created<state.active.created)return;
      const key=data.item||'live';let entry=state.live.get(key);
      if(!entry){const el=document.createElement('article');el.className='message '+(data.who||'connor');const by=document.createElement('small');by.textContent=state.names[data.who||'connor']||'执行者';const body=document.createElement('div');body.className='live-text';el.append(by,body);insertTimeline(el,event.created);entry={el,body,text:''};state.live.set(key,entry);}
      entry.text+=data.delta||'';entry.body.textContent=entry.text;return;
    }
    if(event.kind==='message_done'){clearLive(data.item);return;}
    if(event.kind==='item'&&data.item?.type==='agentMessage'){if(data.stage==='item/completed')clearLive(data.item.id);return;}
    let title='',text='',key=String(event.id),running=false,failed=event.kind==='error';
    if(event.kind==='status'||event.kind==='error'){title=data.text||'';if(data.who)title=(state.names[data.who]||data.who)+'：'+title;recordActivity(event,title);}
    else if(event.kind==='reasoning'){title='思路摘要';key='reasoning:'+(data.item||event.id);text=data.delta||'';recordActivity(event,'正在分析，整理下一步');}
    else if(event.kind==='output'){title='执行命令 · 进行中';key='item:'+data.item;text=data.delta||'';running=true;recordActivity(event,'命令正在运行，有新的输出');}
    else if(event.kind==='item') {
      const item=data.item||{};key='item:'+item.id;
      running=data.stage!=='item/completed';failed=item.status==='failed'||(item.exitCode!=null&&item.exitCode!==0);
      title=({commandExecution:'执行命令',fileChange:'修改文件',dynamicToolCall:'调用维修工具',mcpToolCall:'调用工具',webSearch:'搜索',imageView:'查看图片',contextCompaction:'整理工作上下文'})[item.type]||item.type||'操作';
      title+=' · '+(running?'进行中':failed?'未成功':'已结束');
      text=item.type==='commandExecution'?[item.command,item.cwd?'目录：'+item.cwd:'',item.exitCode!=null?'退出码：'+item.exitCode:''].filter(Boolean).join('\n'):JSON.stringify(item,null,2);
      recordActivity(event,running?title:failed?'上一步未成功，等待执行者继续处理':'上一步已结束，等待下一步');
    }
    else if(event.kind==='diff'){title='本轮代码差异';text=data.diff||'';key='diff:'+data.turnId;}
    else if(event.kind==='steps'){title='执行步骤';text=JSON.stringify(data.plan||data,null,2);key='steps:'+data.turnId;}
    else return;
    let entry=state.log.get(key);
    if(!entry){
      const el=document.createElement('details');el.className='event';const summary=document.createElement('summary');const stamp=document.createElement('time');stamp.textContent=new Date(event.created*1000).toLocaleTimeString();const label=document.createElement('span');summary.append(stamp,label);const pre=document.createElement('pre');el.append(summary,pre);insertTimeline(el,event.created);
      entry={el,label,pre,text:'',output:'',title:'',running:false,failed:false,created:event.created};state.log.set(key,entry);
    }
    if(event.kind==='output')entry.output+=text;
    else if(event.kind==='reasoning')entry.text+=text;
    else entry.text=text;
    if(event.kind==='item'&&data.item?.aggregatedOutput!=null)entry.output=data.item.aggregatedOutput;
    if(event.kind!=='output'||!entry.title){entry.title=title;entry.running=running;entry.failed=failed;}
    entry.label.textContent=entry.title;
    entry.pre.textContent=[entry.text,entry.output].filter(Boolean).join('\n\n');
    entry.pre.hidden=!entry.pre.textContent;entry.el.classList.toggle('brief',entry.pre.hidden);
    entry.el.classList.toggle('error',entry.failed);
    entry.el.classList.toggle('running',entry.running&&!!state.active&&event.created>=state.active.created);
  }
  function renderActivity() {
    if(!state.task)return;
    let text='',busy=!!state.active||state.submitting||!!state.pendingSince||acceptingTask===state.task.id;
    if(acceptingTask===state.task.id)text='正在整理本次成果摘要，完成后同步到群聊…';
    else if(state.submitting)text='正在发送你的要求…';
    else if(state.connectionLost)text='连接暂时中断，正在重连；暂时无法确认进度，请勿重复发送。';
    else if(state.pendingSince&&!state.active)text='要求已送达，正在获取工作状态…';
    else if(state.active?.cancel)text='正在停止，等待执行进程退出…';
    else if(state.active){
      if(!state.workerOnline)text='执行进程暂未连接，任务已保存，正在等待连接…';
      else if(state.active.state==='queued')text='要求已送达，排队等待执行…';
      else if(state.backlog)text='正在同步工作记录…';
      else {const recent=state.lastActivity?.created>=state.active.created?state.lastActivity:null;text=recent?.title||'正在连接工作会话…';if(recent&&Date.now()/1000-recent.created>25)text+=' · 等待新的进展';}
    }else if(state.task.phase==='paused')text='本轮未完成或已停止，请查看聊天里的记录。';
    else if(state.task.phase==='completed')text='已验收，成果已回传。';
    else if(state.task.phase==='review')text='本轮已结束，等你检查结果；需要调整可以继续说。';
    else text=state.task.proposal_valid?'方案已整理好，等你确认。':state.messageAfter?'回复已结束，可以继续讨论。':'可以开始讨论。';
    $('activityLine').hidden=false;$('activityLine').classList.toggle('busy',busy&&!state.connectionLost);$('activityLine').classList.toggle('error',state.connectionLost||state.task.phase==='paused');
    $('activity').textContent=text;$('activity').title=text;
    const since=state.active?.created||(state.pendingSince/1000),seconds=since?Math.max(0,Math.floor(Date.now()/1000-since)):0;
    $('activityTime').textContent=busy&&since?Math.floor(seconds/60)+':'+String(seconds%60).padStart(2,'0'):'';
    $('activityTime').title='本轮已等待 / 执行的时间';
  }
  async function refreshTasks() {
    const data=await api('/tasks');$('taskList').replaceChildren();
    for(const task of data.tasks){const row=document.createElement('div');row.className='task-row';const b=document.createElement('button');b.className='task-button'+(task.id===state.task?.id?' active':'');const name=document.createElement('strong');name.textContent=task.title;const sub=document.createElement('small');sub.textContent=phaseNames[task.phase]+' · '+new Date(task.updated*1000).toLocaleDateString();b.append(name,sub);b.onclick=action(()=>openTask(task.id));const edit=document.createElement('button');edit.className='task-rename';edit.textContent='✎';edit.setAttribute('aria-label','修改任务名称：'+task.title);edit.onclick=()=>renameDialog(task);row.append(b,edit);$('taskList').append(row);}
    return data.tasks;
  }
  function controls() {
    const uploading=uploadBatch?.taskId===state.task?.id;
    const busy=uploading||!!state.active||state.submitting||!!state.pendingSince||acceptingTask===state.task?.id,done=state.task?.phase==='completed',running=state.active?.state==='running'&&state.active.can_steer&&!state.active.cancel;
    $('stop').hidden=!state.active;$('stop').disabled=!!state.active?.cancel;
    renderActivity();
    $('send').disabled=done||state.submitting||!!state.pendingSince||(busy&&!running);$('send').textContent=state.submitting?'发送中':running?'补充':'发送';$('recipient').disabled=busy||done;
    $('inspect').disabled=busy||done;$('upload').disabled=busy||done;$('attachFile').disabled=busy||done;
    for(const id of ['upload','attachFile','imageUpload','attachImage'])$(id).disabled=busy||done||uploading;
    if(uploading){$('send').disabled=true;$('send').textContent='上传中';$('inspect').disabled=true;}
    $('attachImage').title=uploading?'正在上传附件':done?'新建任务后可以发送图片或截图':busy?'本轮结束后可以发送图片或截图':'发送图片或截图，也可以粘贴截图';
    for(const button of $('pendingFiles').querySelectorAll('button'))button.disabled=state.submitting;
    for(const id of ['generatePlan','savePlan','planText'])$(id).disabled=busy||done;
    $('execute').disabled=busy||done||state.planDirty||!state.task?.proposal_valid||!state.task?.plan?.trim();
    $('chatApproval').hidden=busy||done||!state.task?.proposal_valid||state.task?.phase==='review';
    $('chatStart').disabled=$('execute').disabled;
    $('chatReview').hidden=busy||state.task?.phase!=='review';
    $('chatAccept').disabled=busy;
    $('acceptBox').hidden=state.task?.phase!=='review';$('accept').disabled=busy;
    $('composerHint').textContent=running?'可以补充要求或纠正方向；项目改动和重启仍先商量。':'新建普通文件可直接做；项目改动或重启先商量，再回复「就这样改」。';
    if(running){$('draft').placeholder='补充给执行者…';$('send').title='补充给执行者';}else recipientHint();
  }
  let subPageVisible = window.frameElement?.dataset.aionSubPageVisible !== '0';
  function pageVisible() { return subPageVisible && !document.hidden; }
  function backgroundPoll() {
    if (pageVisible() && (state.active || state.pendingSince || state.submitting || state.backlog)) {
      renderActivity();poll();
    }
  }
  async function poll() {
    if(!state.task||state.loading)return;
    const taskId=state.task.id,epoch=state.epoch;state.loading=true;
    try {
      const data=await api('/tasks/'+taskId+'?after='+state.after+'&message_after='+state.messageAfter);
      if(epoch!==state.epoch||state.submitting)return;
      state.task=data.task;state.active=data.active;state.connectionLost=false;state.workerOnline=data.worker_online;state.pendingSince=0;state.backlog=data.events.length===300;
      showModel(data.work_model);
      $('taskTitle').textContent=data.task.title;$('taskTitle').title='维修室 · '+data.task.title;$('phase').textContent=phaseNames[data.task.phase];$('contextHint').textContent='工作记录持续保留 · 每轮补充最近 '+data.task.context_minutes+' 分钟生活背景';
      $('renameTask').disabled=false;
      if(!state.planDirty){$('planText').value=data.task.plan;$('revision').textContent='v'+data.task.revision;}
      // Timestamp insertion also handles older event pages arriving after final messages.
      data.messages.forEach(addMessage);data.events.forEach(logEvent);
      if(!data.active)clearLive();
      for(const entry of state.log.values()){
        if(entry.running&&(!data.active||entry.created<data.active.created)){
          entry.el.classList.remove('running');entry.label.textContent=entry.title.replace('进行中','未收到结束记录');
        }
      }
      groupWorkRecords();controls();
      if(data.messages.length||data.events.length)chatScroll.changed();
    }catch(err){if(epoch===state.epoch){state.connectionLost=true;renderActivity();}}
    finally{if(epoch===state.epoch){state.loading=false;if(state.backlog&&!state.connectionLost&&pageVisible())setTimeout(()=>{if(pageVisible())poll();},30);}}
  }
  async function openTask(id) {

    timeline=[];
    state.epoch++;state.loading=false;state.task={id};state.active=null;state.after=0;state.messageAfter=0;state.planDirty=false;state.files=[];clearLive();state.log.clear();state.lastActivity=null;state.pendingSince=0;state.connectionLost=false;state.backlog=false;$('messages').replaceChildren();$('draft').value='';renderPending();$('welcome').hidden=true;$('taskView').hidden=false;$('taskDrawer').close();resizeDraft();history.replaceState(null,'','/repair?task='+encodeURIComponent(id));tab('chat');$('activityLine').hidden=false;$('activity').textContent='正在载入聊天和工作记录…';await poll();await refreshTasks();
  }

  function renderPending() {
    $('pendingFiles').replaceChildren();
    state.files.forEach((file,index)=>{
      const item=document.createElement('div');item.className='pending-file';
      if(/\.(png|jpe?g|webp)$/i.test(file.name)){const img=document.createElement('img');img.src='/api/repair/files/'+encodeURIComponent(file.id)+'?preview=true';img.alt=file.name;item.append(img);}
      else {const name=document.createElement('span');name.textContent=file.name;item.append(name);}
      const remove=document.createElement('button');remove.type='button';remove.textContent='×';remove.title='移除 '+file.name;remove.setAttribute('aria-label',remove.title);remove.disabled=state.submitting;remove.onclick=()=>{state.files.splice(index,1);renderPending();};item.append(remove);$('pendingFiles').append(item);
    });
    const current=uploadBatch?.taskId===state.task?.id?uploadBatch:null;
    $('uploadStatus').hidden=!current;$('uploadStatus').textContent=current?'正在上传 '+current.name+'…':'';
    chatScroll.layout();
  }
  async function uploadFiles(files) {
    if(!files.length||!state.task)return;
    if($('attachImage').disabled){notice('请等当前上传或本轮工作结束后，再添加图片附件。');return;}
    const batch={taskId:state.task.id,epoch:state.epoch,name:''};uploadBatch=batch;toolsOpen(false);controls();
    try {
      for(const file of files){
        if(batch.epoch!==state.epoch)break;
        if(state.files.length>=4){notice('每条消息最多 4 个附件。');break;}
        if(file.size>50*1024*1024){notice('单个附件不能超过 50 MB：'+file.name);continue;}
        if((file.type.startsWith('image/')||/\.(heic|heif|gif|bmp|svg|avif|tiff?)$/i.test(file.name))&&!/\.(png|jpe?g|webp)$/i.test(file.name)){
          notice('请选 PNG、JPG 或 WebP 图片；其他格式可以先截图再发。');continue;
        }
        batch.name=file.name;renderPending();
        const form=new FormData();form.append('file',file);
        const data=await api('/tasks/'+batch.taskId+'/upload',{method:'POST',body:form});
        if(batch.epoch!==state.epoch)break;
        state.files.push(data);renderPending();
      }
    } finally {
      if(uploadBatch===batch)uploadBatch=null;
      renderPending();controls();
    }
  }
  async function send(kind='discuss') {
    if(!state.task||state.submitting||state.pendingSince||uploadBatch?.taskId===state.task.id)return;
    const text=$('draft').value.trim(),id=state.task.id,epoch=state.epoch;
    if(!text&&!state.files.length&&!['plan','inspect'].includes(kind))return;
    state.submitting=true;state.pendingSince=Date.now();controls();toolsOpen(false);
    try {
      if(state.active?.can_steer&&state.active.state==='running') {await api('/tasks/'+id+'/steer',{method:'POST',body:{text}});notice('补充已排队，送达进展会显示在聊天里。');}
      else await api('/tasks/'+id+'/send',{method:'POST',body:{text,kind,recipient:$('recipient').value,revision:state.planDirty?null:state.task.revision,attachments:state.files.map(f=>f.id)}});
      if(epoch!==state.epoch)return;
      if($('draft').value.trim()===text)$('draft').value='';resizeDraft();state.files=[];renderPending();state.planDirty=false;
    }catch(err){if(epoch===state.epoch){state.pendingSince=0;state.connectionLost=!!err.connectionLost;}throw err;}
    finally{state.submitting=false;controls();}
    await poll();await refreshTasks();
  }
  function newDialog() {$('taskDrawer').close();$('newDialog').showModal();$('newTitle').focus();}
  function modelSelection(){
    const custom=$('modelSelect').value==='';
    $('customModelLabel').hidden=!custom;$('modelName').disabled=!custom;$('modelName').required=custom;
  }
  $('modelSelect').onchange=modelSelection;
  $('workModel').onclick=action(async()=>{
    const data=await api('/model');
    const labels={'gpt-6-sol':'GPT 6 Sol','gpt-6-astra':'GPT 6 Astra','':'自定义模型…'};
    const names=[...new Set(['gpt-6-sol','gpt-6-astra',...data.options,data.name]),''];
    $('modelSelect').replaceChildren(...names.map(name=>{const option=document.createElement('option');option.value=name;option.textContent=labels[name]||name;return option;}));
    $('modelSelect').value=data.name;$('modelName').value='';modelSelection();
    $('modelDialog').showModal();$('modelSelect').focus();
  });
  $('cancelModel').onclick=()=>$('modelDialog').close();
  $('modelForm').onsubmit=action(async()=>{
    const name=$('modelSelect').value||$('modelName').value.trim();
    if(!name){notice('请填写模型名称。');return;}
    $('saveModel').disabled=true;
    try{
      const data=await api('/model',{method:'PUT',body:{name}});
      $('modelDialog').close();
      if(state.task)await poll();else showModel({name:data.name,running:false});
      notice('已保存：'+data.name+'，从下一轮开始使用。');
    }finally{$('saveModel').disabled=false;}
  });
  $('newTask').onclick=newDialog;$('welcomeNew').onclick=newDialog;$('cancelNew').onclick=()=>$('newDialog').close();
  $('renameTask').onclick=()=>{if(state.task?.title)renameDialog(state.task);};
  $('cancelRename').onclick=()=>$('renameDialog').close();
  $('renameForm').onsubmit=action(async()=>{
    const target=renameTarget,title=$('renameTitle').value.trim();
    if(!target||!title){notice('请填写任务名称。');return;}
    $('saveRename').disabled=true;
    try{const updated=await api('/tasks/'+target.id,{method:'PATCH',body:{title}});
      if(state.task?.id===target.id){state.task.title=updated.title;$('taskTitle').textContent=updated.title;$('taskTitle').title='维修室 · '+updated.title;}
      $('renameDialog').close();await refreshTasks();notice('任务名称已修改。');
    }finally{$('saveRename').disabled=false;}
  });
  $('newForm').onsubmit=action(async()=>{const data=await api('/tasks',{method:'POST',body:{title:$('newTitle').value,context_minutes:Number($('contextMinutes').value)}});$('newDialog').close();$('newTitle').value='';await openTask(data.id);});
  $('composer').onsubmit=action(()=>send());$('inspect').onclick=action(()=>send('inspect'));
  $('generatePlan').onclick=action(async()=>{await api('/tasks/'+state.task.id+'/send',{method:'POST',body:{kind:'plan',recipient:'connor'}});await poll();notice('正在根据工作讨论整理方案。');});
  $('savePlan').onclick=action(async()=>{state.task=await api('/tasks/'+state.task.id+'/plan',{method:'POST',body:{plan:$('planText').value,revision:state.planDirty?state.planEditRevision:state.task.revision}});state.planDirty=false;await poll();notice('方案已保存，请检查后确认开始。');});
  async function startWork(){await api('/tasks/'+state.task.id+'/execute',{method:'POST',body:{revision:state.task.revision}});tab('chat');await poll();}
  $('execute').onclick=action(startWork);$('chatStart').onclick=action(startWork);$('chatShowPlan').onclick=()=>tab('plan');
  $('planText').oninput=()=>{if(!state.planDirty)state.planEditRevision=state.task.revision;state.planDirty=true;controls();};
  $('stop').onclick=action(async()=>{await api('/tasks/'+state.task.id+'/stop',{method:'POST'});await poll();});
  const acceptWork=action(async()=>{
    if(acceptingTask||!state.task)return;
    const id=state.task.id;acceptingTask=id;controls();
    try{await api('/tasks/'+id+'/accept',{method:'POST'});await poll();await refreshTasks();notice('已验收，成果摘要已作为卡片同步到群聊。');}
    finally{acceptingTask=null;controls();}
  });
  $('accept').onclick=acceptWork;$('chatAccept').onclick=acceptWork;
  for(const id of ['upload','imageUpload'])$(id).onchange=action(async()=>{const files=[...$(id).files];$(id).value='';await uploadFiles(files);});
  $('attachImage').onclick=()=>$('imageUpload').click();
  $('draft').addEventListener('paste',event=>{
    const files=[...(event.clipboardData?.items||[])].filter(item=>item.kind==='file'&&item.type.startsWith('image/')).map(item=>item.getAsFile()).filter(Boolean);
    if(files.length){event.preventDefault();uploadFiles(files).catch(err=>notice(err.message));}
  });
  $('tasksToggle').onclick=()=>$('taskDrawer').showModal();$('closeTasks').onclick=()=>$('taskDrawer').close();
  $('taskDrawer').onclick=e=>{if(e.target===$('taskDrawer'))$('taskDrawer').close();};
  $('toolsToggle').onclick=()=>toolsOpen($('composerTools').hidden);
  $('attachFile').onclick=()=>$('upload').click();$('recipient').onchange=recipientHint;
  $('draft').oninput=resizeDraft;$('jumpLatest').onclick=chatScroll.latest;
  document.addEventListener('pointerdown',e=>{if(!$('composer').contains(e.target))toolsOpen(false);});
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('composerTools').hidden){toolsOpen(false);$('toolsToggle').focus();}});
  document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>tab(b.dataset.tab));
  $('draft').onkeydown=e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)&&!$('send').disabled){e.preventDefault();$('composer').requestSubmit();}};
  $('pairForm').onsubmit=action(async()=>{await api('/pair',{method:'POST',body:{code:$('pairCode').value}});await init();});
  async function init(){
    const data=await api('/bootstrap');showModel(data.work_model);
    $('pairing').hidden=data.authorized;$('workspace').hidden=!data.authorized;
    if(!data.authorized)return;
    state.names=data.names;$('participants').textContent=[data.names.user,data.names.aion,data.names.connor].join(' · ');
    for(const actor of ['aion','connor'])$('recipient').querySelector('[value='+actor+']').textContent=data.names[actor];
    $('recipient').value='connor';recipientHint();
    if(data.pairing_code){$('pairDetails').hidden=false;$('localCode').textContent=data.pairing_code;}
    const tasks=await refreshTasks();
    const id=new URLSearchParams(location.search).get('task')||tasks[0]?.id;
    if(id)await openTask(id);else $('welcome').hidden=false;
    clearInterval(state.poll);state.poll=setInterval(backgroundPoll,1200);
  }
  document.addEventListener('visibilitychange',()=>{if(pageVisible())poll();});
  const previousVisibility=window.onAionSubPageVisibilityChanged;
  window.onAionSubPageVisibilityChanged=visible=>{
    previousVisibility?.(visible);
    const returning=!subPageVisible&&visible;subPageVisible=!!visible;
    if(returning&&pageVisible())poll();
  };
  function fitViewport(){const viewport=window.visualViewport;if(!viewport||viewport.scale===1){document.documentElement.style.setProperty('--repair-height',Math.round(Math.min(window.innerHeight,viewport?.height||window.innerHeight))+'px');chatScroll.layout();}}
  window.visualViewport?.addEventListener('resize',fitViewport);window.addEventListener('resize',fitViewport);fitViewport();
  init().catch(err=>notice(err.message));
})();

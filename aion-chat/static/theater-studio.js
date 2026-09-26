/* Shared theater shell, chapter reader and manually activated segmented player. */
const Studio = (() => {
  const base = '/api/theater/studio';
  let book = null, chapters = [], selected = null, anchors = [], modes = new Map();
  let refreshBusy = false, selectionToken = 0, outlineBusy = false;
  let outlineFeedback = null;
  let outlineDraft = null;
  let lastBookPoll = -Infinity, lastAudioPoll = -Infinity;
  let subPageVisible = window.frameElement?.dataset.aionSubPageVisible !== '0';
  let discussion={messages:[],status:'idle'}, planningView=true, talkBusy=false;
  let readerComposerOpen=false;
  let discussionNames={user_name:'用户',ai_name:'AI'};
  let player = null, audio = null, playbackToken = 0, audioPollBusy = false;
  let readingFollow = null;
  const durations = new Map();
  const statusNames = {planned:'待写',writing:'正在写',draft:'待确认完成',ready:'已完成',interrupted:'可续写'};
  const oldSelect = selectConv, oldRender = renderMessages, oldSend = send;
  const oldSync = handleSync, oldList = renderConvList, oldStop = stopTTSPlayback;
  const oldToggle = toggleTopTTS, oldSeek = seekTopTTS;
  const oldDiscardMessageTTS = discardMessageTTS, oldDiscardConversationTTS = discardConversationTTS;

  async function request(path, method='GET', body) {
    const res = await fetch(base+path, {method, headers:{'Content-Type':'application/json'}, ...(body ? {body:JSON.stringify(body)} : {})});
    const data = await res.json();
    if (!res.ok) throw Error(typeof data.detail === 'string' ? data.detail : '操作未完成，请重试');
    return data;
  }
  async function action(fn) { try { return await fn(); } catch(e) { showToast(e.message || '操作失败'); } }
  function novel() { return book?.mode === 'novel'; }
  function chapter() { return chapters.find(c => c.id === selected); }
  function planning() { return book?.phase !== 'writing'; }
  function setPlanningView(value) {
    readerComposerOpen=false;
    planningView=value;
    if(currentConvId)localStorage.setItem('studio_view_'+currentConvId,value?'discussion':'reader');
  }
  function readerSize(value) {
    const size=Math.max(12,Math.min(24,Number(value)||16));
    document.body.style.setProperty('--reader-size',size+'px');
    localStorage.setItem('studio_reader_size',String(size));
    if($('readerSizeValue'))$('readerSizeValue').textContent=size+' px';
  }
  function readingSettings() {
    const size=Number(localStorage.getItem('studio_reader_size'))||16;
    modal('阅读字号',`<label for="readerSize">文字大小 · <span id="readerSizeValue">${size} px</span></label><input id="readerSize" type="range" min="12" max="24" value="${size}" oninput="Studio.readerSize(this.value)"><p class="field-help">调整立即生效，并自动记住。正文、对话和剧情讨论都会使用这个字号。</p>`,'完成',async()=>{});
  }
  function viewImage(url) {
    const d=$('studioImageDialog');
    $('studioFullImage').src=url;
    $('studioImageOriginal').href=url;
    $('studioSaveImage').onclick=()=>action(async()=>{
      const btn=$('studioSaveImage');btn.disabled=true;
      try {await saveIllustration(url);} finally {btn.disabled=false;}
    });
    d.showModal();
  }
  async function saveIllustration(url) {
    const response=await fetch(url);
    if(!response.ok)throw Error('图片下载失败，请重试或打开原图保存');
    const blob=await response.blob();
    const filename=new URL(url,location.href).pathname.split('/').pop()||'illustration.png';
    let saver;
    for(const frame of [window,window.parent,window.top]) {
      try {if(frame.AionImageSaver){saver=frame.AionImageSaver;break;}} catch(e) {}
    }
    if(saver) {
      const data=await new Promise((resolve,reject)=>{
        const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);
        reader.onerror=()=>reject(Error('读取图片失败，请重试'));reader.readAsDataURL(blob);
      });
      saver.save(data,filename);
    } else {
      const objectUrl=URL.createObjectURL(blob),link=document.createElement('a');
      link.href=objectUrl;link.download=filename;document.body.append(link);link.click();link.remove();
      setTimeout(()=>URL.revokeObjectURL(objectUrl),60000);
      showToast('已发起下载；也可长按大图，或打开原图保存');
    }
  }
  function button(label, handler, cls='') { return `<button type="button" class="studio-btn ${cls}" onclick="${handler}">${label}</button>`; }

  function modal(title, html, saveLabel, submit) {
    const d = $('studioDialog');
    d.innerHTML = `<form method="dialog"><header><h2>${escHtml(title)}</h2><button class="studio-btn quiet" value="cancel" aria-label="关闭">✕</button></header>${html}<footer><button class="studio-btn" value="cancel">取消</button><button type="button" class="studio-btn primary" id="studioSave">${saveLabel}</button></footer></form>`;
    $('studioSave').onclick = () => action(async () => {
      $('studioSave').disabled = true;
      try { await submit(); d.close(); } finally { if ($('studioSave')) $('studioSave').disabled = false; }
    });
    d.showModal();
  }

  function confirmAction(title, message, label='确认') {
    // Native confirm() can be suppressed by Safari, including after a speech-cache request.
    return new Promise(resolve=>{
      const d=document.createElement('dialog');
      d.className='studio-dialog';
      d.setAttribute('aria-label',title);
      d.innerHTML=`<form method="dialog"><header><h2>${escHtml(title)}</h2><button class="studio-btn quiet" value="cancel" aria-label="关闭">✕</button></header><p>${escHtml(message)}</p><footer><button class="studio-btn" value="cancel" autofocus>取消</button><button type="button" class="studio-btn primary" data-confirm>${escHtml(label)}</button></footer></form>`;
      d.querySelector('[data-confirm]').onclick=()=>d.close('confirm');
      d.addEventListener('close',()=>{d.remove();resolve(d.returnValue==='confirm');},{once:true});
      document.body.append(d);
      d.showModal();
    });
  }

  function updateReaderComposer() {
    document.body.classList.toggle('studio-reader-quiet',novel() && !planningView && !!chapter()?.content && !readerComposerOpen);
  }
  function bindReaderGestures() {
    const reader=$('messages');
    let gesture=null;
    const prose=target=>target.closest('#novelBody') && !target.closest('button,a,img,figure,input,textarea');
    const hide=()=>{readerComposerOpen=false;updateReaderComposer();};
    reader.addEventListener('pointerdown',e=>{
      gesture=novel() && !planningView && e.isPrimary && e.button===0 && prose(e.target)
        ? {id:e.pointerId,x:e.clientX,y:e.clientY,time:performance.now(),moved:false,ended:false} : null;
    },{passive:true});
    reader.addEventListener('pointermove',e=>{
      if(gesture?.id===e.pointerId && Math.hypot(e.clientX-gesture.x,e.clientY-gesture.y)>10){gesture.moved=true;hide();}
    },{passive:true});
    reader.addEventListener('pointercancel',()=>{if(gesture)hide();gesture=null;},{passive:true});
    reader.addEventListener('pointerup',e=>{
      if(gesture?.id===e.pointerId){
        gesture.ended=true;
        gesture.moved ||= Math.hypot(e.clientX-gesture.x,e.clientY-gesture.y)>10;
      }
    },{passive:true});
    reader.addEventListener('scroll',()=>{if(gesture && !gesture.ended)gesture.moved=true;},{passive:true});
    reader.addEventListener('wheel',()=>{if(novel() && !planningView)hide();},{passive:true});
    reader.addEventListener('click',e=>{
      const tap=gesture;gesture=null;
      if(!tap?.ended || tap.moved || performance.now()-tap.time>500 || !prose(e.target) || window.getSelection()?.toString())return;
      readerComposerOpen=!readerComposerOpen;
      updateReaderComposer();
    });
  }

  function mount() {
    document.body.classList.add('studio-shell');
    $('messages').insertAdjacentHTML('beforebegin','<section id="studioOutlineStatus" class="studio-outline-status" role="status" aria-live="polite" aria-atomic="true" hidden></section>');
    document.querySelector('.chat-header').insertAdjacentHTML('beforeend','<div class="studio-reading-header" id="studioReadingHeader" aria-label="当前章节" hidden><span id="novelNumber"></span><h1 id="novelTitle"></h1><span id="novelMeta"></span></div>');
    $('sidebar').querySelector('.sidebar-header').insertAdjacentHTML('beforeend','<div class="library-brand" style="text-align:right"><strong>小剧场</strong></div>');
    document.body.insertAdjacentHTML('beforeend','<dialog class="studio-dialog studio-chapters" id="studioChapterDialog"><header><h2>章节目录</h2><button class="studio-btn" onclick="document.getElementById(\'studioChapterDialog\').close()" aria-label="关闭目录">✕</button></header><p id="studioDirectoryTitle" class="field-help"></p><nav class="studio-directory" id="studioDirectory" aria-label="当前小剧场的章节"></nav></dialog>');
    $('studioTitleGroup').insertAdjacentHTML('afterend', `<div class="studio-header-actions">${button('返回正文','Studio.returnToReader()','studio-return')}<button type="button" class="studio-btn" id="studioChapterPlan" onclick="Studio.editChapter(false)" hidden>章节计划</button>${button('目录','Studio.directory()')}<details id="studioMenu" class="studio-menu"><summary class="studio-btn">菜单</summary><div class="studio-menu-panel"><div class="studio-menu-basics"><label>故事模式 <select class="studio-mode-select" id="studioMode" aria-label="故事模式" onchange="Studio.switchMode(this.value)"><option value="dialogue">对话</option><option value="novel">小说</option></select></label>${button('字号','Studio.readingSettings()')}${button('人物锚点','Studio.anchors()')}${button('模型与音色','Studio.modelSettings()')}${button('角色管理','Studio.persona()')}</div><div class="studio-toolbar" id="studioToolbar"></div></div></details></div>`);
    document.querySelector('.chat-header>.config-btn').style.display='none';
    $('studioMenu').addEventListener('click',e=>{if(e.target.closest('button'))$('studioMenu').open=false;});
    document.addEventListener('click',e=>{if(!$('studioMenu').contains(e.target))$('studioMenu').open=false;});
    document.addEventListener('keydown',e=>{if(e.key==='Escape')$('studioMenu').open=false;});
    readerSize(localStorage.getItem('studio_reader_size'));
    document.body.insertAdjacentHTML('beforeend','<dialog class="studio-dialog" id="studioDialog"></dialog>');
    document.body.insertAdjacentHTML('beforeend','<dialog class="studio-dialog studio-outline-dialog" id="studioOutlineDialog"></dialog>');
    bindReaderGestures();
    readingFollow = TheaterFollow.mount($('messages'), () => {
      const c = chapter();
      const root = novel() ? (!planningView && c ? $('novelBody') : null)
        : (player?.cid === currentConvId ? document.querySelector(`.msg-row[data-id="${player.source}"] .msg-body-bubble:not(.editing)`) : null);
      const content = novel() ? c?.content : currentMessages.find(m => m.id === player?.source)?.content;
      const matching = player?.cid === currentConvId && (!novel() || (player.source === selected && player.revision === c?.revision));
      const segment = player?.segments[player.seq];
      return {root, content: content || '', player: matching ? player : null, visible: subPageVisible && !document.hidden,
        currentTime: audio?.currentTime ?? player?.at ?? 0,
        duration: Number.isFinite(audio?.duration) && audio.duration > 0 ? audio.duration : durations.get(segment?.url)};
    });
    document.body.insertAdjacentHTML('beforeend',`<dialog class="studio-dialog studio-image-dialog" id="studioImageDialog"><header><h2>故事插画</h2><button class="studio-btn" onclick="document.getElementById('studioImageDialog').close()" aria-label="关闭插画">✕</button></header><img id="studioFullImage" alt="故事插画原图"><p class="field-help">可长按图片保存；App 内点下方按钮保存到相册。</p><footer><a class="studio-btn" id="studioImageOriginal" target="_blank" rel="noopener">打开原图</a><button class="studio-btn primary" id="studioSaveImage">保存图片</button></footer></dialog>`);
    $('messages').addEventListener('click',e=>{
      const img=e.target.closest('.novel-body figure img');if(img)viewImage(img.src);
    });
    $('messages').addEventListener('keydown',e=>{
      if((e.key==='Enter'||e.key===' ') && e.target.matches('.novel-body figure img')){e.preventDefault();viewImage(e.target.src);}
    });
    $('ttsTopPlayer').insertAdjacentHTML('afterbegin','<div class="studio-player-label" id="studioPlayerLabel" onclick="Studio.returnToPlaying()" title="回到正在听的内容"></div>');
    $('ttsTopPlayer').insertAdjacentHTML('beforeend','<button class="studio-stop-synthesis" id="studioCancelAudio" onclick="Studio.cancelAudio()" title="停止合成，保留已生成语音" style="display:none">停止合成</button><button class="studio-stop-synthesis" id="studioRetryAudio" onclick="Studio.retryAudio()" style="display:none">重试</button>');
    // Speech always starts from the visible manual button, including dialogue.
    ttsEnabled = false; localStorage.setItem('theater_tts_enabled','false');
    $('ttsToggle').checked=false; $('ttsToggle').closest('.tts-toggle-row').style.display='none';
    setInterval(backgroundPoll, 1000);
    const previousVisibility = window.onAionSubPageVisibilityChanged;
    window.onAionSubPageVisibilityChanged = visible => {
      previousVisibility?.(visible);
      const returning = !subPageVisible && visible;
      subPageVisible = !!visible;
      if (returning && !document.hidden) refresh();
      if (returning) readingFollow.update(true);
    };
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && subPageVisible) {
        refresh();
        if (player?.status === 'running') pollAudio();
        readingFollow.update(true);
      }
    });
    $('messages').addEventListener('scroll', () => {
      if (novel() && !planningView && selected) localStorage.setItem('studio_read_'+selected, String($('messages').scrollTop));
    }, {passive:true});
  }

  function modeUI() {
    updateReaderComposer();
    document.body.classList.toggle('novel-mode', novel());
    document.body.classList.toggle('studio-discussing',novel() && planningView && chapters.length>0);
    document.body.classList.toggle('studio-chat-input',!novel() || planningView);
    $('studioChapterPlan').hidden=!novel();
    $('studioChapterPlan').textContent='大纲';
    $('studioChapterPlan').onclick=()=>action(()=>outlineEditor());
    $('studioReadingHeader').hidden=!(novel() && !planningView && chapter());
    $('studioMode').value=book?.mode || 'dialogue';
    $('input').placeholder=novel() ? (planningView ? '聊聊剧情…' : '本章想法（可留空）') : '输入消息…';
    const sendLabel=novel() && !planningView ? (chapter()?.writing?'写作中…':chapter()?.status==='ready'?'下一章':['draft','interrupted'].includes(chapter()?.status)?'续写本章':'开始写') : (outlineBusy?'正在拟定大纲':talkBusy || discussion.status==='running'?'正在回应':'发送消息');
    $('sendBtn').textContent=novel() && !planningView ? sendLabel : '➤';
    $('sendBtn').setAttribute('aria-label',sendLabel);
    $('sendBtn').title=sendLabel;
    $('sendBtn').disabled=novel() ? (outlineBusy || talkBusy || discussion.status==='running' || !!chapter()?.writing) : isStreaming;
    renderDirectory(); toolbar(); renderOutlineStatus();
    readingFollow?.update();
  }
  function renderOutlineStatus() {
    const el=$('studioOutlineStatus');
    const visible=novel() && outlineFeedback?.cid===currentConvId && (outlineFeedback.state==='running' || planningView);
    el.hidden=!visible;
    if(!visible)return;
    const {state,error}=outlineFeedback;
    el.dataset.state=state;
    const title=state==='running'?'正在生成大纲':state==='done'?'大纲已拟好':'大纲生成未完成';
    const detail=state==='running'?'正在根据讨论整理故事走向和章节计划，请稍候…':state==='done'?'草稿已保存，可在「大纲」里查看，确认后创建新剧场。':(error || '请稍后重新点击生成大纲。');
    const html=`<span class="studio-outline-indicator" aria-hidden="true">${state==='running'?'':state==='done'?'✓':'!'}</span><div><strong>${title}</strong><p>${escHtml(detail)}</p></div>`;
    if(el.innerHTML!==html)el.innerHTML=html;
  }
  function renderDirectory() {
    $('studioDirectoryTitle').textContent=conversations.find(c=>c.id===currentConvId)?.title||'当前故事';
    $('studioDirectory').innerHTML=chapters.map(c => `<button class="chapter-link ${c.id===selected?'active':''}" onclick="Studio.selectChapter('${c.id}')"><span class="chapter-number">${String(c.number).padStart(2,'0')}</span><span>${escHtml(c.title)}<small>${statusNames[c.status] || c.status}${c.audio?' · 有声':''}</small></span><span class="chapter-word-count">${(c.word_count ?? [...(c.content||'').replace(/\s/g,'')].length).toLocaleString()} 字</span></button>`).join('')||'<p class="studio-notice">还没有章节，先一起讨论并生成大纲吧。</p>';
  }
  function toolbar() {
    const c=chapter();
    const copy=novel() && book?.outline && chapters.length ? button('复制大纲为新剧场','Studio.copyOutline()') : '';
    const writer=book?.writer_ready?`<span class="studio-writer">由 ${escHtml(book.writer_name)} 陪你创作</span>`:button('先选择讲故事的角色','Studio.persona()');
    if (novel() && planningView) {
      $('studioToolbar').innerHTML=writer+button('创作设置','Studio.settings()')+copy+
        button(outlineBusy?'正在拟定…':book?.outline?'重新生成大纲':'按讨论生成大纲','Studio.outline()','primary')+
        (outlineDraft?button('查看待确认草稿','Studio.outlineEditor(true)','primary'):'');
      return;
    }
    $('studioToolbar').innerHTML=writer+button('创作设置','Studio.settings()')+copy+button('商量剧情','Studio.discussionMode()')+(c ?
      button(c.audio?'收听本章':'开始朗读',`Studio.speak('${c.id}')`,'primary')+
      (c.writing?button('停止写作',`Studio.stopWriting()`):button('重写本章','Studio.write(true)'))+
      (!c.writing && c.content ? button('编辑正文','Studio.editChapter(true)'):'')+
      (!c.writing && c.content ? button(c.status==='ready'?'本章还没写完':'本章已写完',`Studio.completeChapter(${c.status!=='ready'})`):'')+
      (c.versions?.length && !c.writing ? button('恢复上一版','Studio.restore()'):'') : '');
  }
  function renderPlanning() {
    $('studioReadingHeader').hidden=true;
    const el=$('messages'),bottom=el.scrollHeight-el.scrollTop-el.clientHeight<80;
    let html='<section class="studio-planning"><div class="studio-kicker">一起聊脑洞 → 拟定大纲 → 确认后写正文</div>';
    if(!discussion.messages.length)html+='<div class="studio-empty"><div class="ornament">❧</div><h2>先一起想象，这个宇宙</h2><p>在下面说说想听什么。他会带着人设和你聊背景、人物与氛围。聊满意了，再点「按讨论生成大纲」。</p></div>';
    for(const m of discussion.messages)html+=discussionMessage(m);
    if(discussion.error)html+=`<p class="studio-notice">${escHtml(discussion.error)}</p>`;
    if(outlineDraft)html+=`<section class="studio-outline-review"><h2>新的大纲草稿 · 待确认</h2><p class="studio-notice">共 ${outlineDraft.plans.length} 章。${chapters.length?'确认后创建新剧场，从第一章开始，当前作品保留。':'确认后在当前剧场开始写作，保留名字和讨论记录。'}</p>${button('查看并编辑完整大纲','Studio.outlineEditor(true)','primary')}</section>`;
    html+='</section>';
    if(el.innerHTML!==html){el.innerHTML=html;if(bottom)el.scrollTop=el.scrollHeight;}
  }
  function discussionMessage(m) {
    const isUser=m.role==='user';
    const name=isUser ? discussionNames.user_name : (personas.find(p=>p.id===currentPersonaId)?.name || book?.writer_name || discussionNames.ai_name);
    const menu=m.id ? `<div class="msg-actions"><button class="msg-dots" aria-label="消息操作" onclick="toggleMenu(event,'talk_menu_${m.id}')">⋯</button><div class="msg-menu" id="talk_menu_${m.id}">${isUser?`<button onclick="Studio.talkAction('edit','${m.id}')">编辑</button>`:`<button onclick="Studio.talkAction('regenerate','${m.id}')">重新生成</button>`}<button onclick="Studio.talkAction('copy','${m.id}')">复制</button><button onclick="Studio.talkAction('delete','${m.id}')">删除</button></div></div>` : '';
    return `<div class="studio-talk ${isUser?'user':'assistant'}"><div class="studio-talk-name"><span>${escHtml(name)}</span>${menu}</div><div class="studio-talk-content">${escHtml(m.content || (!isUser && discussion.status==='running'?'正在想象这个故事…':''))}</div></div>`;
  }
  function renderNovel() {
    if(planningView || !chapter())return renderPlanning();
    const el=$('messages'), c=chapter();
    if (!c) {
      el.innerHTML=`<div class="studio-empty"><div class="ornament">❧</div><span class="studio-kicker">A STORY WAITING TO UNFOLD</span><h2>故事，从一个念头开始</h2><p>${escHtml(book?.premise || '在下方随便说说你的脑洞，点「生成大纲」。背景、人物关系和剧情安排交给 AI，不用自己写完整设定。')}</p>${button('写下脑洞','Studio.settings()','primary')}</div>`;
      return;
    }
    if (!el.querySelector(`[data-chapter="${c.id}"]`)) {
      el.innerHTML=`<article class="novel-reader" data-chapter="${c.id}"><div id="novelNotice" class="studio-notice"></div><div class="novel-body" id="novelBody"></div><footer id="novelCompletion" class="studio-chapter-completion"></footer></article>`;
    }
    $('studioReadingHeader').hidden=false;
    $('novelNumber').textContent=`第${c.number}章`;
    $('novelTitle').textContent=c.title;
    $('novelTitle').title=c.title;
    const count=c.content.replace(/\s/g,'').length;
    $('novelMeta').textContent=`${count.toLocaleString()} 字 · ${statusNames[c.status]||c.status}`;
    $('novelNotice').innerHTML=escHtml(c.error||'')+(c.image_error?` ${escHtml(c.image_error)} ${button('重试配图','Studio.illustrate()')}`:'');
    const completion=!c.writing && c.content && c.status!=='ready'?`<p>本轮生成已停止，你可以续写，或确认本章结束。</p><div>${button('续写本章','Studio.write(false)')}${button('本章已写完','Studio.completeChapter(true)','primary')}</div>`:'';
    if($('novelCompletion').innerHTML!==completion)$('novelCompletion').innerHTML=completion;
    const images=[...(c.images||[])].sort((a,b)=>a.after-b.after);
    let pos=0, html='';
    const paras=t=>TheaterFollow.paragraphs(t,pos,escHtml);
    for (const im of images) {
      html+=paras(c.content.slice(pos, im.after)); pos=im.after;
      if (im.status==='ready') html+=`<figure><img src="${escHtml(im.url)}" loading="lazy" alt="${escHtml(im.prompt)}" tabindex="0" role="button" aria-label="查看并保存故事插画"><figcaption>故事插画 · 点击查看与保存</figcaption></figure>`;
      else html+=`<div class="studio-notice">${im.status==='failed'?'插画暂未完成':'正在描绘这一幕…'} ${im.status==='failed'?button('重试配图','Studio.illustrate()'):''}</div>`;
    }
    html+=paras(c.content.slice(pos));
    if (!c.content) html='<p class="studio-notice">'+(c.writing?'正在构思这一章，正文会逐渐出现在这里。':'本章尚未开始。你可以先看章节计划，再点下方「开始写」。')+'</p>';
    if ($('novelBody').innerHTML!==html) $('novelBody').innerHTML=html;
    readingFollow?.update();
  }

  function backgroundPoll() {
    const now = performance.now();
    const active = outlineBusy || talkBusy || discussion.status === 'running' ||
      chapters.some(c => c.writing || c.processing || c.status === 'writing');
    if (active && subPageVisible && !document.hidden && now-lastBookPoll >= 1000) refresh();
    // Hidden pages still fetch new audio while listening, but finished synthesis needs no polling.
    if (player?.status === 'running' && ((subPageVisible && !document.hidden) || !player.paused) && now-lastAudioPoll >= 1000) pollAudio();
  }

  async function refresh() {
    if (refreshBusy || !currentConvId || !novel()) return;
    lastBookPoll=performance.now();
    refreshBusy=true;
    const cid=currentConvId;
    try {
      const data=await request('/books/'+cid+(selected?'?chapter='+encodeURIComponent(selected):''));
      if (cid!==currentConvId) return;
      book=data.book; outlineDraft=data.outline_draft; discussion=data.discussion||discussion; chapters=data.chapters.map(c=>({...chapters.find(old=>old.id===c.id),...c}));
      if (!chapters.some(c=>c.id===selected) && chapters.length) selected=chapters.find(c=>!c.content)?.id||chapters[0].id;
      modeUI(); renderNovel();
    } catch(e) { /* A later poll restores connectivity; user actions report errors. */ }
    finally {refreshBusy=false;}
  }
  selectConv = async function(id) {
    const token=++selectionToken;
    try {
      const data=await request('/books/'+id);
      if (token!==selectionToken) return;
      lastBookPoll=performance.now();
      book=data.book; chapters=data.chapters; outlineDraft=data.outline_draft; discussion=data.discussion||{messages:[],status:'idle'};currentConvId=id;
      readerComposerOpen=false;
      const savedView=localStorage.getItem('studio_view_'+id);
      planningView=!chapters.length || (savedView ? savedView==='discussion' : book.phase!=='writing' && !chapters.some(c=>c.content));
      selected=localStorage.getItem('studio_chapter_'+id);
      if (!chapters.some(c=>c.id===selected)) selected=chapters[0]?.id||null;
      modes.set(id,book.mode); modeUI();
      if (novel()) {
        renderConvList(); closeSidebar();
        const conv=conversations.find(c=>c.id===id);
        $('chatTitle').textContent=conv?.title||'小剧场';
        if (conv?.model) $('modelSelect').value=conv.model;
        currentPersonaId=conv?.persona_id||''; renderPersonaList(); renderNovel();
        $('messages').scrollTop=Number(localStorage.getItem('studio_read_'+selected)||0);
      } else await oldSelect(id);
    } catch(e) {showToast(e.message);}
  };
  renderMessages = function() { if (novel()) return renderNovel(); oldRender(); readingFollow?.update(); };
  renderConvList = function() {
    oldList();
    document.querySelectorAll('.conv-item').forEach((el,i)=>{
      const c=conversations[i]; if (!c) return;
      const title=el.querySelector('.title');
      title.insertAdjacentHTML('beforeend',`<span class="studio-mode-mark">${modes.get(c.id)==='novel'?'长篇小说':'自由对话'}</span>`);
    });
  };
  discardMessageTTS = function(id,deleted=false) {
    if(player?.source===id)stopPlayer();
    oldDiscardMessageTTS(id,deleted);
  };
  discardConversationTTS = function(id) {
    if(player?.cid===id)stopPlayer();
    oldDiscardConversationTTS(id);
  };
  handleSync = function(msg) {
    if(msg.type==='theater_msg_deleted' && player?.source===msg.data.id)stopPlayer();
    if (novel() && /^theater_msg_/.test(msg.type)) return;
    oldSync(msg);
  };
  send = function() { return novel() ? action(()=>write(false)) : oldSend(); };
  newConversation = function() {
    modal('新的一页', '<label>故事名字</label><input id="newStoryTitle" placeholder="为这个宇宙起个名字"><label>故事模式</label><select id="newStoryMode"><option value="novel">小说 · 先定大纲，逐章写作</option><option value="dialogue">对话 · 自由的一问一答</option></select>', '创建故事', async()=>{
      const mode=$('newStoryMode').value;
      const conv=await api('POST','/api/theater/conversations',{title:$('newStoryTitle').value.trim()||'未命名故事',persona_id:currentPersonaId,model:$('modelSelect').value});
      if (!conv.id) throw Error('创建失败');
      if (!conversations.some(c=>c.id===conv.id)) conversations.unshift(conv);
      await request('/books/'+conv.id,'PUT',{mode});
      await selectConv(conv.id);
    });
  };
  // The old send path needs an actual conversation, not the nonblocking creation dialog.
  const sendWithoutBook=oldSend;
  send=function() {
    if (!currentConvId) return newConversation();
    return novel()?action(()=>planningView?discuss():chapter()?.status==='ready'?next():write(false)):sendWithoutBook();
  };

  function copyOutline() {
    if(!book?.outline || !chapters.length){showToast('先生成大纲和章节计划，再复制');return;}
    const cid=book.id, original=conversations.find(c=>c.id===cid);
    modal('复制大纲为新剧场',`<label>新剧场名字</label><input id="copyStoryTitle" maxlength="200" value="${escHtml((original?.title||'故事')+'（大纲副本）')}"><label>写作模型</label><select id="copyStoryModel"></select><p class="field-help">保留故事设定、人物、全书大纲、各章计划和字数范围。从第一章重新写，正文、插画、语音和聊天记录不带入新剧场。</p>`,'创建副本',async()=>{
      const conv=await request('/books/'+cid+'/copy-outline','POST',{title:$('copyStoryTitle').value.trim(),model:$('copyStoryModel').value});
      if(!conversations.some(c=>c.id===conv.id))conversations.unshift(conv);
      modes.set(conv.id,'novel');
      await selectConv(conv.id);
      showToast('大纲已复制，可以从第一章开始写了');
    });
    $('copyStoryModel').innerHTML=$('modelSelect').innerHTML;
    $('copyStoryModel').value=original?.model || $('modelSelect').value;
  }
  function outlineEditor(preferDraft=planningView) {
    if(!book)return;
    const current=chapters.length && book.outline ? {consensus:book.consensus||'',outline:book.outline,plans:chapters,settings:book} : null;
    const sources=JSON.parse(JSON.stringify({current,draft:outlineDraft}));
    if(!sources.current && !sources.draft){showToast('先讨论故事，再生成大纲草稿');return;}
    const cid=book.id,d=$('studioOutlineDialog'),createsCopy=chapters.length>0;
    let active=(preferDraft && sources.draft) || !sources.current ? 'draft':'current',busy=false;
    function content(kind,data) {
      if(!data)return '';
      const lower=data.settings.min_chars||data.settings.target_chars||6500,upper=data.settings.max_chars||data.settings.target_chars||6500;
      return `<section data-outline-page="${kind}"><p class="field-help">${kind==='draft'?(createsCopy?'这是一份待确认草稿。确认后新建剧场，原作品保持不变。':'确认后在当前剧场开始写作，保留名字和讨论记录。'):'这是当前作品的大纲。保存只修改计划，不自动重写正文。'}</p>${kind==='draft' && createsCopy?`<label>新剧场名字<input data-field="title" maxlength="200" value="${escHtml((conversations.find(c=>c.id===cid)?.title||'故事')+'（重写版）')}"></label>`:''}<details class="outline-overview" ${kind==='draft' || planningView?'open':''}><summary>全书总览 · 共 ${data.plans.length} 章</summary><label>故事共识<textarea data-field="consensus" required maxlength="30000">${escHtml(data.consensus)}</textarea></label><label>全书走向与结局<textarea data-field="outline" required maxlength="30000">${escHtml(data.outline)}</textarea></label></details><div class="outline-list-heading"><strong>全部章节</strong><button type="button" class="studio-btn quiet" data-expand="${kind}">展开全部</button></div>${data.plans.map((p,i)=>`<details class="outline-plan" data-index="${i}" ${(kind==='current'?p.id===selected:i===0)?'open':''}><summary><span class="outline-chapter-heading"><span>第 ${i+1} 章 ·</span><input data-field="chapterTitle" aria-label="第 ${i+1} 章标题" title="编辑章节标题" maxlength="200" required value="${escHtml(p.title)}"></span><small>${escHtml((p.plan||'').split('\n')[0].slice(0,90))}</small></summary><label>章节详细计划<textarea data-field="plan" required maxlength="20000">${escHtml(p.plan)}</textarea></label><div class="studio-length-range"><label>字数下限<input data-field="min" type="number" min="1000" max="100000" required value="${p.min_chars||lower}"></label><label>字数上限<input data-field="max" type="number" min="1000" max="100000" required value="${p.max_chars||upper}"></label></div></details>`).join('')}</section>`;
    }
    d.innerHTML=`<header><h2>故事大纲</h2><button type="button" class="studio-btn quiet" data-close aria-label="关闭大纲">✕</button></header><nav class="outline-tabs" aria-label="选择大纲">${sources.current?'<button type="button" class="studio-btn" data-tab="current">当前作品</button>':''}${sources.draft?'<button type="button" class="studio-btn" data-tab="draft">待确认草稿</button>':''}</nav><div class="outline-editor-content">${content('current',sources.current)}${content('draft',sources.draft)}</div><footer><span class="field-help" data-feedback role="status"></span><button type="button" class="studio-btn" data-save>保存修改</button><button type="button" class="studio-btn primary" data-confirm>${createsCopy?'确认并创建新剧场':'确认并开始写作'}</button></footer>`;
    const page=()=>d.querySelector(`[data-outline-page="${active}"]`);
    function switchPage(kind) {
      if(busy)return;
      active=kind;
      d.querySelectorAll('[data-outline-page]').forEach(el=>el.hidden=el.dataset.outlinePage!==kind);
      d.querySelectorAll('[data-tab]').forEach(el=>{el.classList.toggle('primary',el.dataset.tab===kind);el.setAttribute('aria-pressed',String(el.dataset.tab===kind));});
      d.querySelector('[data-confirm]').hidden=kind!=='draft';
      d.querySelector('[data-feedback]').textContent='';
    }
    d.querySelector('[data-close]').onclick=()=>d.close();
    d.querySelectorAll('[data-tab]').forEach(el=>el.onclick=()=>switchPage(el.dataset.tab));
    d.querySelectorAll('[data-expand]').forEach(el=>el.onclick=()=>{
      const all=[...page().querySelectorAll('.outline-plan')],open=all.some(el=>!el.open);
      all.forEach(el=>el.open=open);el.textContent=open?'收起全部':'展开全部';
    });
    async function save(confirm=false) {
      if(busy)return;
      const panel=page(),data=sources[active],value=(el,name)=>el.querySelector(`[data-field="${name}"]`).value.trim();
      for(const el of panel.querySelectorAll('input,textarea')) {
        if(!el.checkValidity()){el.closest('details')?.setAttribute('open','');el.reportValidity();return;}
      }
      const payload={revision:data.revision||'',consensus:value(panel,'consensus'),outline:value(panel,'outline'),plans:[...panel.querySelectorAll('.outline-plan')].map((el,i)=>{
        const original=data.plans[i],limits=data.settings;
        const min=Number(value(el,'min')),max=Number(value(el,'max'));
        const lower=original.min_chars||limits.min_chars||limits.target_chars||6500,upper=original.max_chars||limits.max_chars||limits.target_chars||6500;
        return {id:original.id||null,title:value(el,'chapterTitle'),plan:value(el,'plan'),
          min_chars:min===lower?(original.min_chars??null):min,max_chars:max===upper?(original.max_chars??null):max};
      })};
      if(payload.plans.some(p=>(p.min_chars||data.settings.min_chars||data.settings.target_chars||6500)>(p.max_chars||data.settings.max_chars||data.settings.target_chars||6500))){showToast('章节字数下限不能大于上限');return;}
      busy=true;
      d.querySelectorAll('button,input,textarea').forEach(el=>el.disabled=true);
      const feedback=d.querySelector('[data-feedback]');feedback.textContent=confirm?(createsCopy?'正在保存并创建新剧场…':'正在保存并确认大纲…'):'正在保存…';
      try {
        const result=await request('/books/'+cid+(active==='draft'?'/outline-draft':'/outline-editor'),'PUT',payload);
        if(active==='draft') {
          sources.draft=result;
          if(currentConvId===cid)outlineDraft=result;
        } else {
          sources.current={consensus:result.book.consensus,outline:result.book.outline,plans:result.chapters,settings:result.book};
        }
        if(confirm) {
          const result=await request('/books/'+cid+'/confirm-outline','POST',{revision:sources.draft.revision,title:createsCopy?value(panel,'title'):''});
          const conv=result.conversation;
          if(!conv)throw Error('后端尚未加载新草稿流程，请重启后端');
          if(!conversations.some(c=>c.id===conv.id))conversations.unshift(conv);
          localStorage.setItem('studio_view_'+conv.id,'reader');
          modes.set(conv.id,'novel');d.close();await selectConv(conv.id);
          showToast(conv.id===cid?'大纲已确认，可以从第一章开始写了':'新剧场已创建，可以从第一章开始写了');
        } else {
          feedback.textContent='已保存';
          if(currentConvId===cid){await refresh();renderPlanningIfVisible();}
        }
      } catch(e) {feedback.textContent=e.message||'保存未完成，请重试';}
      finally {busy=false;d.querySelectorAll('button,input,textarea').forEach(el=>el.disabled=false);}
    }
    d.querySelector('[data-save]').onclick=()=>save();
    d.querySelector('[data-confirm]').onclick=()=>save(true);
    switchPage(active);d.showModal();
    if(active==='current' && !planningView)requestAnimationFrame(()=>page().querySelector('.outline-plan[open]')?.scrollIntoView({block:'start'}));
  }
  function renderPlanningIfVisible(){if(novel() && planningView)renderPlanning();}

  async function settings() {
    if (!book) return newConversation();
    if(outlineBusy){showToast('正在拟定大纲，请稍等');return;}
    const editingBook={...book};
    anchors=await request('/anchors');
    modal('创作设置',`<label>每章字数范围</label><div class="studio-length-range"><label>最少<input id="bookMinLength" type="number" min="1000" max="30000" required value="${book.min_chars||book.target_chars||6500}"></label><label>最多<input id="bookMaxLength" type="number" min="1000" max="30000" required value="${book.max_chars||book.target_chars||6500}"></label></div><label>本故事的人物锚点</label><div class="cast-picker">${anchors.map(a=>`<label><input type="checkbox" name="cast" value="${a.id}" ${(book.anchors||[]).includes(a.id)?'checked':''}> ${escHtml(a.name)}</label>`).join('')}</div><label>插画风格</label><input id="bookStyle" value="${escHtml(book.style||'')}"><label><input type="checkbox" id="bookIllustrations" ${book.illustrations?'checked':''}> 为小说配图</label><p class="field-help">故事内容通过讨论确定。总纲和各章计划请在顶部「大纲」中查看、编辑。</p>`,'保存',async()=>{
      if(!$('bookMinLength').reportValidity() || !$('bookMaxLength').reportValidity())return;
      const min_chars=Number($('bookMinLength').value),max_chars=Number($('bookMaxLength').value);
      if(min_chars>max_chars)throw Error('字数下限不能大于上限');
      const data=await request('/books/'+editingBook.id,'PUT',{...editingBook,min_chars,max_chars,style:$('bookStyle').value,illustrations:$('bookIllustrations').checked,anchors:[...document.querySelectorAll('[name=cast]:checked')].map(el=>el.value)});
      if(currentConvId===editingBook.id){book=data;modeUI();}
    });
  }
  async function discuss() {
    const text=$('input').value.trim();
    if(!text || outlineBusy || talkBusy || discussion.status==='running')return;
    if(outlineFeedback?.cid===book.id)outlineFeedback=null;
    talkBusy=true;modeUI();const cid=book.id;
    try {
      await request('/books/'+cid+'/discuss','POST',{content:text});
      if(currentConvId===cid){setPlanningView(true);if($('input').value.trim()===text){$('input').value='';autoResize($('input'));}await refresh();}
    } finally {talkBusy=false;modeUI();}
  }
  async function talkAction(kind,id) {
    document.querySelectorAll('.msg-menu.show').forEach(m=>m.classList.remove('show'));
    const message=discussion.messages.find(m=>m.id===id);
    if(!message || !book)return;
    if(kind==='copy'){await navigator.clipboard.writeText(message.content);showToast('已复制');return;}
    if(outlineBusy || talkBusy || discussion.status==='running' || chapters.some(c=>c.writing)) {
      showToast('先等当前生成结束，再修改讨论消息');return;
    }
    const cid=book.id, path='/books/'+cid+'/discussion/messages/'+id;
    async function save(method,body,suffix='') {
      if(outlineBusy || talkBusy || discussion.status==='running')throw Error('先等当前生成结束');
      talkBusy=true;modeUI();
      try {
        await request(path+suffix,method,body);
        if(currentConvId===cid)await refresh();
      } finally {talkBusy=false;modeUI();}
    }
    if(kind==='edit' && message.role==='user') {
      modal('编辑消息',`<textarea id="studioTalkEdit" class="long-text" maxlength="12000">${escHtml(message.content)}</textarea>`,'保存',async()=>{
        const content=$('studioTalkEdit').value.trim();
        if(!content)throw Error('消息不能为空');
        await save('PUT',{content});
      });
    } else if(kind==='delete')await save('DELETE');
    else if(kind==='regenerate' && message.role==='assistant')await save('POST',undefined,'/regenerate');
  }
  async function discussionMode() {
    if(outlineBusy)return;
    const cid=book.id;
    // Opening the discussion is navigation; only sending new ideas changes the story phase.
    if(player?.cid===cid && !player.paused)toggleTopTTS();
    setPlanningView(true);modeUI();renderPlanning();$('input').focus();
  }
  async function outline(useInput=true) {
    if(outlineBusy || !book)return;
    if(outlineDraft===undefined)throw Error('后端尚未加载大纲草稿功能，请重启后端并刷新页面');
    if(talkBusy || discussion.status==='running'){showToast('先等这一轮回应结束，再生成大纲');return;}
    if(useInput && $('input').value.trim()){await discuss();showToast('这段想法已送去讨论，听完回应后再生成大纲');return;}
    if(!discussion.messages.some(m=>m.content?.trim())){showToast('先在下面说说你的脑洞');$('input').focus();return;}
    const cid=book.id;
    outlineBusy=true;outlineFeedback={cid,state:'running'};modeUI();
    try {
      const data=await request('/books/'+cid+'/outline','POST');
      if(!data.outline_draft)throw Error('未收到大纲草稿，请重启后端后重试');
      outlineFeedback={cid,state:'done'};
      if(cid===currentConvId){outlineDraft=data.outline_draft;setPlanningView(true);renderPlanning();outlineEditor(true);}
      showToast('大纲草稿已生成，请查看并确认');
    } catch(e) {
      outlineFeedback={cid,state:'error',error:e.message || '请稍后重新点击生成大纲。'};
      throw e;
    } finally {outlineBusy=false;modeUI();}
  }
  async function confirmOutline() {
    if(outlineBusy || talkBusy || discussion.status==='running')return;
    if(outlineDraft)return outlineEditor(true);
    await request('/books/'+book.id+'/confirm-outline','POST');
    setPlanningView(false);await refresh();renderNovel();$('messages').scrollTop=0;
  }
  async function restoreOutline() {
    if(outlineBusy || talkBusy || discussion.status==='running')return;
    const data=await request('/books/'+book.id+'/restore-outline','POST');
    book=data.book;chapters=data.chapters;discussion=data.discussion||discussion;selected=chapters.find(c=>!c.content)?.id||chapters[0]?.id;setPlanningView(true);modeUI();renderPlanning();
  }
  async function write(rewrite=false) {
    if(planning()){
      if(!book?.outline || !chapters.length){showToast('还没有大纲，请先生成并确认');setPlanningView(true);modeUI();renderPlanning();return;}
      const cid=book.id;
      modal('沿用当前大纲？', '<p class="studio-notice">当前大纲尚未处于写作状态。若不想修改，可以直接沿用：保留现有大纲、章节计划和全部正文，讨论记录也不会删除。</p><p class="field-help">新讨论的想法不会自动加入正文计划。想采用新方向时，再到菜单中商量并更新大纲。</p>', '沿用当前大纲', async()=>{
        await request('/books/'+cid+'/use-current-outline','POST');
        if(currentConvId===cid){setPlanningView(false);await refresh();showToast('已恢复，点击开始写即可继续');}
      });
      return;
    }
    const c=chapter();
    if (!c) return outline();
    if (c.writing) return;
    if (!rewrite && c.content && !await confirmAction('续写本章','确定续写本章吗？模型会接着当前正文继续写。','继续写作')) return;
    if (rewrite && !await confirmAction('重写本章','重写本章？上一版会保留，当前语音合成将停止。','确认重写')) return;
    if (rewrite && player?.source===c.id) stopPlayer();
    await request('/chapters/'+c.id+'/write','POST',{instruction:$('input').value,rewrite});
    $('input').value=''; autoResize($('input')); await refresh();
  }
  async function completeChapter(completed) {
    const c=chapter();if(!c || c.writing)return;
    const cid=book.id;
    await request('/chapters/'+c.id+'/completion','POST',{completed});
    if(currentConvId===cid)await refresh();
  }
  async function editChapter(body=false) {
    if(!body)return outlineEditor(false);
    const c=chapter(); if (!c) return;
    const lower=c.min_chars || book.min_chars || book.target_chars || 6500, upper=c.max_chars || book.max_chars || book.target_chars || 6500;
    modal(body?'编辑正文':'本章计划', body?`<textarea id="chapterContent" class="long-text">${escHtml(c.content)}</textarea>`:`<label>章节标题</label><input id="chapterTitle" value="${escHtml(c.title)}"><label>本章场景与走向</label><textarea id="chapterPlan" class="long-text">${escHtml(c.plan)}</textarea><label>本章字数预算</label><div class="studio-length-range"><label for="chapterMinLength">最少<input id="chapterMinLength" type="number" min="1000" max="100000" step="1" required value="${lower}"></label><span>～</span><label for="chapterMaxLength">最多<input id="chapterMaxLength" type="number" min="1000" max="100000" step="1" required value="${upper}"></label></div><p class="field-help">默认沿用全书范围，修改后只影响本章。已写 ${[...(c.content||'').replace(/\s/g,'')].length.toLocaleString()} 字；续写会扣除已写字数。只调整预算不会修改正文或要求重新确认大纲。</p>`, '保存', async()=>{
      if (body && player?.source===c.id) stopPlayer();
      let fields;
      if(body)fields={content:$('chapterContent').value};
      else {
        if(!$('chapterMinLength').reportValidity() || !$('chapterMaxLength').reportValidity())throw Error('请填写有效的本章字数范围');
        const min=Number($('chapterMinLength').value),max=Number($('chapterMaxLength').value);
        if(min>max)throw Error('本章字数下限不能大于上限');
        fields={title:$('chapterTitle').value,plan:$('chapterPlan').value};
        if(min!==lower || max!==upper)Object.assign(fields,{min_chars:min,max_chars:max});
      }
      const saved=await request('/chapters/'+c.id,'PATCH',fields);
      if(fields.max_chars!==undefined && (saved.min_chars!==fields.min_chars || saved.max_chars!==fields.max_chars))throw Error('后端尚未加载本章预算功能，请重启后端后再保存');
      await refresh();
    });
  }
  async function manageAnchors() {
    anchors=await request('/anchors');
    const list=[...anchors];
    while(list.length<3) list.push({id:'anchor_'+(crypto.randomUUID?crypto.randomUUID():Date.now().toString(36)+Math.random().toString(36).slice(2)),name:'',aliases:'',appearance:'',image:''});
    modal('熟悉的人 · 人物锚点', `<p class="field-help">每张照片对应一个名字。外貌跟随照片，服饰与身份由每本故事决定。保存后在「故事与大纲」中选择出场人物。</p><div id="anchorCards">${list.map((a,i)=>`<section class="anchor-card" data-aid="${a.id}" data-image="${escHtml(a.image)}">${a.image?`<img src="${escHtml(a.image)}" alt="${escHtml(a.name)}">`:''}<label>名字</label><input data-field="name" value="${escHtml(a.name)}" placeholder="人物 ${i+1}"><label>别名（用逗号分隔）</label><input data-field="aliases" value="${escHtml(a.aliases)}"><label>固定外貌</label><input data-field="appearance" value="${escHtml(a.appearance)}" placeholder="发色、瞳色或其他特征"><label>锚点照片</label><input data-field="photo" type="file" accept="image/jpeg,image/png,image/webp"></section>`).join('')}</div>`, '保存人物', async()=>{
      for (const el of document.querySelectorAll('.anchor-card')) {
        const name=el.querySelector('[data-field=name]').value.trim(); if (!name) continue;
        let image=el.dataset.image;
        const file=el.querySelector('[data-field=photo]').files[0];
        if (file) {
          const fd=new FormData();fd.append('file',file);
          const res=await fetch('/api/upload',{method:'POST',body:fd}), data=await res.json();
          if (!res.ok || !data.url) throw Error(data.error||'照片上传失败'); image=data.url;
        }
        await request('/anchors/'+el.dataset.aid.replace(/^anchor_/,''),'PUT',{name,image,aliases:el.querySelector('[data-field=aliases]').value,appearance:el.querySelector('[data-field=appearance]').value});
      }
      showToast('人物锚点已保存');
    });
  }

  function selectChapter(id) {
    if(!chapters.some(c=>c.id===id))return;
    setPlanningView(false);selected=id;localStorage.setItem('studio_chapter_'+currentConvId,id);modeUI();renderNovel();
    $('messages').scrollTop=Number(localStorage.getItem('studio_read_'+id)||0);$('studioChapterDialog').close();
  }
  function next() {
    const i=chapters.findIndex(c=>c.id===selected);
    if (chapters[i+1]) selectChapter(chapters[i+1].id); else showToast('已到最后一章');
  }

  function time(value) { return TTSQueue.formatTime(value||0); }
  function saveListen() { if (player) localStorage.setItem('studio_listen_'+player.id,JSON.stringify({seq:player.seq,at:audio?.currentTime||player.at||0})); }
  function releaseAudio() { playbackToken++; if (audio) {audio.pause();audio.onended=null;audio.ontimeupdate=null;audio.onerror=null;audio.src='';audio=null;} }
  function stopPlayer() {saveListen();releaseAudio();player=null;readingFollow?.update();$('studioPlayerLabel').classList.remove('show');$('studioCancelAudio').style.display='none';$('studioRetryAudio').style.display='none';oldStop();}
  function readySegments() { return player?.segments.filter((s,i,arr)=>arr.slice(0,i+1).every(x=>durations.has(x.url)))||[]; }
  function updateProgress() {
    if (!player) return;
    const ready=readySegments(), total=ready.reduce((n,s)=>n+durations.get(s.url),0);
    const before=ready.slice(0,player.seq).reduce((n,s)=>n+durations.get(s.url),0);
    $('ttsTopSeek').max=total; $('ttsTopSeek').disabled=total===0;
    $('ttsTopSeek').value=before+(audio?.currentTime||player.at||0);
    $('ttsTopCurrent').textContent=time(Number($('ttsTopSeek').value));
    $('ttsTopDuration').textContent=(player.status==='ready'?'':'已就绪 ')+time(total);
    $('ttsTopToggle').textContent=player.paused?'▶':'❚❚';
    $('studioPlayerLabel').textContent=`正在听：${player.title}${player.status==='failed'?' · 某段合成失败':!audio && !player.paused && player.status!=='ready'?' · 等待后续内容':''} · 点击返回`;
    $('studioCancelAudio').style.display=player.status==='running'?'':'none';
    $('studioRetryAudio').style.display=['failed','stopped'].includes(player.status)?'':'none';
    readingFollow?.update();
  }
  async function duration(url) {
    if (durations.has(url)) return;
    await new Promise(resolve=>{
      const a=new Audio();a.preload='metadata'; let timer;
      const done=()=>{clearTimeout(timer);a.onloadedmetadata=null;a.onerror=null;a.src='';resolve();};
      a.onloadedmetadata=()=>{if(Number.isFinite(a.duration)&&a.duration>0)durations.set(url,a.duration);done();};
      a.onerror=done;timer=setTimeout(done,8000);a.src=url;
    });
  }
  function playSegment() {
    if (!player || player.paused || audio) return;
    const segment=player.segments[player.seq];
    if (!segment) { updateProgress(); return; }
    const token=++playbackToken, p=player;
    audio=!p.browserAudio && typeof window.createTtsAudio==='function'?window.createTtsAudio(segment.url):new Audio(segment.url);
    const current=audio;
    const nativeAudio=!(current instanceof Audio);
    current.onloadedmetadata=()=>{if(token!==playbackToken)return; if(p.at)current.currentTime=Math.min(p.at,Math.max(0,current.duration-.05));};
    current.ontimeupdate=()=>{if(token!==playbackToken)return;updateProgress();saveListen();};
    current.onended=()=>{
      if(token!==playbackToken)return; p.seq++;p.at=0;releaseAudio();saveListen();
      if(p.status==='ready'&&p.seq>=p.segments.length)p.paused=true;
      updateProgress();playSegment();
    };
    const failed=()=>{
      if(token!==playbackToken)return;
      if(Number.isFinite(current.currentTime) && current.currentTime>0)p.at=current.currentTime;
      if(nativeAudio) {
        // Keep the recording and position when Android's MediaPlayer cannot open it.
        p.browserAudio=true;releaseAudio();playSegment();return;
      }
      p.paused=true;releaseAudio();updateProgress();showToast('音频加载失败，点击播放重试');
    };
    current.onerror=failed;
    current.play().catch(()=>{
      if(token!==playbackToken)return;
      if(nativeAudio)failed();else {p.paused=true;updateProgress();}
    });
  }
  async function pollAudio() {
    if (!player || audioPollBusy) return;
    lastAudioPoll=performance.now();
    audioPollBusy=true;const id=player.id;
    try {
      const state=await request('/speech/status/'+id);
      if(player?.id!==id)return;
      Object.assign(player,{segments:state.segments,status:state.status});
      for(const s of state.segments) {await duration(s.url);if(player?.id!==id)return;}
      updateProgress();playSegment();
    } catch(e) { if(player?.id===id) $('studioPlayerLabel').textContent='连接中断，正在重连…'; }
    finally {audioPollBusy=false;}
  }
  async function speak(key) {
    let state=await request('/speech/'+key,'POST',{voice:ttsVoice||'',prefer_cached:true,allow_generation:false});
    if(state.needs_confirmation) {
      if(!await confirmAction('开始朗读',novel()?'本章节没有语音，是否开始合成？':'这条内容没有语音，是否开始合成？','开始合成'))return;
      if(!ttsVoice){showToast('请先在设置里选择音色');return;}
      state=await request('/speech/'+key,'POST',{voice:ttsVoice,prefer_cached:true,allow_generation:true});
    }
    if (player?.id===state.id) {
      player.status=state.status;player.segments=state.segments;
      if(player.paused)toggleTopTTS();
      await pollAudio();return;
    }
    stopPlayer();
    let saved={};try{saved=JSON.parse(localStorage.getItem('studio_listen_'+state.id)||'{}');}catch(e){}
    player={...state,seq:saved.seq||0,at:saved.at||0,paused:false,title:chapters.find(c=>c.id===key)?.title||conversations.find(c=>c.id===currentConvId)?.title||'对话',cid:currentConvId};
    if(player.status==='ready'&&player.seq>=player.segments.length){player.seq=0;player.at=0;}
    $('ttsTopPlayer').classList.add('show');document.querySelector('.chat-area').classList.add('tts-player-open');$('studioPlayerLabel').classList.add('show');
    await pollAudio();
  }
  replayTTS=function(key){return action(()=>speak(key));};
  stopTTSPlayback=function(){if(player)return stopPlayer();oldStop();};
  toggleTopTTS=function(){
    if(!player)return oldToggle();
    player.paused=!player.paused;
    if(player.paused){audio?.pause();saveListen();}
    else if(audio)audio.play().catch(()=>{player.paused=true;updateProgress();});
    else {if(player.status==='ready'&&player.seq>=player.segments.length){player.seq=0;player.at=0;}playSegment();}
    updateProgress();
  };
  seekTopTTS=function(value){
    if(!player)return oldSeek(value);
    let remaining=Number(value), seq=0, ready=readySegments();
    while(seq<ready.length-1&&remaining>=durations.get(ready[seq].url)){remaining-=durations.get(ready[seq].url);seq++;}
    releaseAudio();player.seq=seq;player.at=remaining;saveListen();playSegment();updateProgress();
    readingFollow?.resume();
  };

  async function boot() {
    mount();
    await Promise.all([
      action(async()=>{for(const b of await request('/books')) modes.set(b.id,b.mode);}),
      action(async()=>{const names=await api('GET','/api/worldbook');discussionNames={user_name:names.user_name||'用户',ai_name:names.ai_name||'AI'};})
    ]);
    await init();
    if(!currentConvId)$('messages').innerHTML='<div class="studio-empty"><div class="ornament">❧</div><h2>今晚，去哪个宇宙？</h2><p>自由对话，或把一个念头写成一本小说。<br>故事留在这里，随时回来接着听。</p>'+button('创建第一个故事','newConversation()','primary')+'</div>';
  }
  return {
    copyOutline:()=>action(copyOutline),
    outlineEditor:prefer=>action(()=>outlineEditor(prefer)),
    completeChapter:completed=>action(()=>completeChapter(completed)),
    talkAction:(kind,id)=>action(()=>talkAction(kind,id)),
    boot,selectChapter,next,readerSize,readingSettings,
    returnToReader(){if(chapters.length)selectChapter(chapter()?.id||chapters[0].id);},
    discussionMode:()=>action(discussionMode),confirmOutline:()=>action(confirmOutline),restoreOutline:()=>action(restoreOutline),
    modelSettings:openModelSettings,
    persona:()=>openPersonaModal(),
    directory(){if(!novel())return showToast('小说模式会显示章节目录');renderDirectory();$('studioChapterDialog').showModal();},
    switchMode(mode){action(async()=>{if(!book)return newConversation();book=await request('/books/'+book.id,'PUT',{...book,mode});await selectConv(book.id);});},
    settings:()=>action(settings),anchors:()=>action(manageAnchors),outline:()=>action(outline),write:r=>action(()=>write(r)),
    editChapter:b=>action(()=>editChapter(b)),speak:key=>action(()=>speak(key)),
    stopWriting:()=>action(async()=>{await request('/chapters/'+selected+'/stop','POST');await refresh();}),
    restore:()=>action(async()=>{if(player?.source===selected)stopPlayer();await request('/chapters/'+selected+'/restore','POST');await refresh();}),
    illustrate:()=>action(async()=>{await request('/chapters/'+selected+'/illustrate','POST');await refresh();}),
    cancelAudio:()=>action(async()=>{if(player){await request('/speech/'+player.source+'/stop','POST');await pollAudio();}}),
    retryAudio:()=>action(async()=>{const p=player;if(p && await confirmAction('继续合成语音','是否继续合成尚未完成的语音？已有片段会保留。','继续合成') && player===p){await request('/speech/'+p.source,'POST',{voice:p.voice,prefer_cached:false,allow_generation:true});await pollAudio();}}),
    returnToPlaying:()=>action(async()=>{if(!player)return;const p=player;await selectConv(p.cid);if(chapters.some(c=>c.id===p.source))selectChapter(p.source);else document.querySelector(`[data-id="${p.source}"]`)?.scrollIntoView();readingFollow?.resume();})
  };
})();
Studio.boot();

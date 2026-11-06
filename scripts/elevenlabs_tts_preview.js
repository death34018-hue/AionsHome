'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const PREFS = 'elevenlabs_listening_room_v1';
  const KEY = 'elevenlabs_listening_room_key';
  // Official TTS model IDs stay selectable even when /v1/models is unavailable
  // or its catalog has not yet included a newly released model.
  const standardModels = [
    {model_id:'eleven_v4',name:'Eleven v4 · 推荐'},
    {model_id:'eleven_v3',name:'Eleven v3 · 对照试听'},
  ];
  const groups = {
    '常用推荐': [['softly','轻柔'],['whispers','耳语'],['chuckles','轻笑'],['mischievously','调皮'],['warmly','温暖'],['sighs','叹气'],['curious','好奇'],['short pause','短停顿']],
    '情绪': [['happy','开心'],['sad','难过'],['excited','兴奋'],['angry','生气'],['thoughtful','若有所思'],['surprised','惊讶'],['sarcastic','讽刺'],['mischievously','调皮'],['curious','好奇'],['warmly','温暖']],
    '说话方式': [['softly','轻柔'],['whispers','耳语'],['quietly','小声'],['shouts','大声'],['slowly','慢慢说'],['rushed','匆忙'],['short pause','短停顿'],['long pause','长停顿']],
    '人声反应': [['chuckles','轻笑'],['laughs','笑出声'],['sighs','叹气'],['exhales','呼气'],['gasps','倒吸气'],['clears throat','清嗓子'],['crying','哭泣']],
    '色色 😏': [['moan','呻吟'],['groan','低哼'],['whimper','呜咽'],['breathy','气声'],['panting','喘气'],['heavy breathing','较重的呼吸'],['inhales','吸气'],['exhales','呼气'],['sighs','叹息'],['gasps','倒吸气'],['whispers','耳语'],['murmurs','低声呢喃'],['softly','轻柔'],['quietly','小声'],['slowly','慢慢说'],['chuckles','轻笑'],['teasing','逗弄的语气'],['flirty','俏皮暧昧']],
    '自定义': [],
  };
  // Keep unverified presets out of the everyday AI allowlist.
  const experimentalGroup = '色色 😏';
  const recommended = [...new Map(Object.entries(groups).filter(([name]) => name !== experimentalGroup).flatMap(([,items]) => items).map(item => [item[0], item])).values()];
  const recommendedNames = new Set(recommended.map(([name]) => name));
  const categoryNames = {cloned:'即时克隆', professional:'专业克隆', generated:'设计音色', premade:'预置音色', high_quality:'高品质音色'};
  const state = {voices:[], models:[], loadedKey:'', loading:false, generating:false, records:[], group:'常用推荐', custom:[], epoch:0, controller:null};
  let prefs = {};
  try { const data = JSON.parse(localStorage.getItem(PREFS) || '{}'); if (data && typeof data === 'object') prefs = data; } catch {}
  try { $('apiKey').value = localStorage.getItem(KEY) || ''; } catch { $('keyHint').textContent = '浏览器不允许本地保存；仍可填写 Key 临时使用。'; }
  if (typeof prefs.text === 'string') $('text').value = prefs.text.slice(0,5000);
  if (Array.isArray(prefs.custom)) state.custom = prefs.custom.filter(t => typeof t === 'string' && t.length <= 80 && !/[\[\]\r\n]/.test(t)).slice(0,40);

  function status(id, message, kind = '') { $(id).textContent = message; $(id).className = `status ${kind}`; }
  function savePrefs() {
    prefs = {...prefs, text:$('text').value, model:$('model').value, custom:state.custom};
    if ($('voiceSelect').value && state.loadedKey) prefs.voice = $('voiceSelect').value;
    try { localStorage.setItem(PREFS, JSON.stringify(prefs)); } catch {}
  }
  function setButtons() {
    $('loadVoices').disabled = state.loading || state.generating;
    $('loadVoices').textContent = state.loading ? '正在读取全部音色…' : '连接并加载全部音色 ↗';
    for (const id of ['apiKey','saveKey','clearKey']) $(id).disabled = state.generating;
    $('generate').disabled = state.loading || state.generating || !state.loadedKey || state.loadedKey !== $('apiKey').value.trim() || !$('voiceSelect').value || !$('text').value.trim();
    $('generate').textContent = state.generating ? '正在生成声音…' : '▶ 生成并试听';
    $('clearHistory').disabled = state.records.length === 0;
  }
  function renderModels(preferred) {
    const models = new Map(standardModels.map(model => [model.model_id,model]));
    for (const model of state.models) {
      const standard = standardModels.find(item => item.model_id === model.model_id);
      models.set(model.model_id,standard ? {...model,name:standard.name} : model);
    }
    $('model').replaceChildren();
    for (const model of models.values()) $('model').add(new Option(model.name || model.model_id,model.model_id));
    $('model').value = models.has(preferred) ? preferred : 'eleven_v4';
  }
  function resetConnection() {
    state.epoch++;
    state.controller?.abort();
    state.loadedKey = ''; state.voices = []; state.models = []; state.loading = false;
    $('connectionBadge').textContent = '未连接'; $('connectionBadge').className = 'pill';
    renderModels($('model').value);
    renderVoices(); updateModel(); setButtons();
  }
  async function post(path, payload, signal) {
    const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload), signal});
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.error || `请求失败（HTTP ${response.status}）`);
    }
    return response;
  }
  $('toggleKey').addEventListener('click', () => {
    const show = $('apiKey').type === 'password';
    $('apiKey').type = show ? 'text' : 'password'; $('toggleKey').textContent = show ? '隐藏' : '显示';
    $('toggleKey').setAttribute('aria-label', show ? '隐藏 API Key' : '显示 API Key');
  });
  $('saveKey').addEventListener('click', () => {
    const key = $('apiKey').value.trim();
    if (!key) return status('connectionStatus','先填写 API Key，再保存。','error');
    try { localStorage.setItem(KEY,key); status('connectionStatus','Key 已保存在此浏览器。点击连接即可加载声音。','success'); }
    catch { status('connectionStatus','浏览器阻止了保存，可以继续临时使用这个 Key。','error'); }
  });
  $('clearKey').addEventListener('click', () => {
    try { localStorage.removeItem(KEY); } catch { status('connectionStatus','浏览器阻止了清除，请在浏览器设置中清除此站点的数据。','error'); return; }
    $('apiKey').value = ''; resetConnection(); status('connectionStatus','已清除保存的 Key。');
  });
  $('apiKey').addEventListener('input', () => { resetConnection(); status('connectionStatus','Key 已修改，请重新连接；要记住新 Key，请再次点击保存。'); });

  $('loadVoices').addEventListener('click', async () => {
    const key = $('apiKey').value.trim();
    if (!key) return status('connectionStatus','请先填写 API Key。','error');
    const desiredVoice = prefs.voice, desiredModel = $('model').value;
    resetConnection();
    const epoch = state.epoch;
    state.loading = true; setButtons();
    const controller = new AbortController(); state.controller = controller;
    const timeout = setTimeout(() => controller.abort(), 180000);
    status('connectionStatus','正在读取音色的所有分页和可用模型…');
    try {
      const results = await Promise.allSettled([
        post('/api/voices',{api_key:key},controller.signal).then(r => r.json()),
        post('/api/models',{api_key:key},controller.signal).then(r => r.json()),
      ]);
      if (epoch !== state.epoch) return;
      if (results[0].status === 'rejected') throw results[0].reason;
      state.voices = results[0].value.voices || [];
      state.voices.sort((a,b) => (Number(!!b.is_owner) - Number(!!a.is_owner)) || String(a.name || '').localeCompare(String(b.name || ''),'zh-CN'));
      state.loadedKey = key;
      if (results[1].status === 'fulfilled') state.models = results[1].value.models || [];
      renderModels(desiredModel);
      $('voiceSearch').value = ''; $('voiceCategory').value = '';
      renderVoices(desiredVoice); updateModel();
      $('connectionBadge').textContent = '已连接'; $('connectionBadge').className = 'pill connected';
      const warning = results[1].status === 'rejected' ? ` 模型列表读取失败，仍可选择官方 v4 / v3：${results[1].reason.message}` : state.models.length ? '' : ' 未返回 TTS 模型，保留官方 v4 / v3 选项，账号可用性待验证。';
      status('connectionStatus',`已读取全部 ${state.voices.length} 个音色。${warning}`, warning ? '' : 'success');
      status('generationStatus', state.voices.length ? '选择音色，调整文本，就可以试听。' : '账号尚未返回可用音色，请在官网添加音色后重新加载。');
      savePrefs();
    } catch (error) {
      if (epoch === state.epoch) status('connectionStatus',error.name === 'AbortError' ? '加载超时，请检查网络后重新连接。' : error.message,'error');
    } finally {
      clearTimeout(timeout);
      if (epoch === state.epoch) { state.loading = false; setButtons(); }
    }
  });

  function renderVoices(preferred = $('voiceSelect').value) {
    const search = $('voiceSearch').value.trim().toLowerCase(), category = $('voiceCategory').value;
    const visible = state.voices.filter(v => (!category || v.category === category) && `${v.name} ${v.voice_id} ${v.description || ''} ${Object.values(v.labels || {}).join(' ')}`.toLowerCase().includes(search));
    $('voiceSelect').replaceChildren();
    for (const voice of visible) $('voiceSelect').add(new Option(`${voice.name || '未命名'} · ${categoryNames[voice.category] || voice.category || '音色'}`,voice.voice_id));
    $('voiceSelect').disabled = !visible.length;
    if (!visible.length) { const empty = new Option(state.loadedKey ? '没有匹配的声音' : '等待加载你的声音…',''); $('voiceSelect').add(empty); }
    else $('voiceSelect').value = visible.some(v => v.voice_id === preferred) ? preferred : visible[0].voice_id;
    $('voiceCount').textContent = search || category ? `${visible.length} / ${state.voices.length} 个` : `${state.voices.length} 个`;
    updateVoice(); setButtons();
  }
  function updateVoice() {
    const voice = state.voices.find(v => v.voice_id === $('voiceSelect').value);
    if (!voice) { $('voiceInfo').textContent = '这里会显示当前 Key 可访问的全部音色，包括你的克隆声音和已添加的音色。'; return; }
    const info = [`${voice.voice_id}`,voice.description || ''];
    const training = voice.fine_tuning?.state?.[$('model').value];
    if (training) info.push(`当前模型训练状态：${({fine_tuned:'训练完成',queued:'排队中',fine_tuning:'训练中',failed:'训练失败',not_started:'尚未开始',not_verified:'尚未验证',draft:'草稿',delayed:'延迟'})[training] || training}`);
    else if (voice.category === 'professional') info.push('专业克隆：此列表未提供当前模型的训练状态，实际生成时由官方确认可用性。');
    $('voiceInfo').textContent = info.filter(Boolean).join('\n');
  }
  function supportsTags() { return /^eleven_v[34](?:_|$)/.test($('model').value); }
  function updateModel() {
    const model = state.models.find(m => m.model_id === $('model').value);
    const max = Math.min(5000,model?.maximum_text_length_per_request || 5000);
    $('text').maxLength = max;
    $('modelHint').textContent = supportsTags() ? '支持 Audio Tags。相同标签在不同音色上的表现可能不同。' : '此模型未确认支持 [] 标签；可移除标签后比较普通朗读。';
    if (!model && standardModels.some(item => item.model_id === $('model').value)) $('modelHint').textContent += ' 官方模型选项；当前账号是否可调用，以生成结果为准。';
    if (/conversational|v4_turbo/.test($('model').value)) $('modelHint').textContent += ' 此为实时模型，若整段接口不接受，请选 v3 / v4。';
    updateVoice(); updateText();
  }
  function updateText() {
    const count = Array.from($('text').value).length;
    $('charCount').textContent = `${count} / ${$('text').maxLength}`;
    const tags = [...$('text').value.matchAll(/\[([^\]]+)\]/g)].map(m => m[1]);
    const custom = [...new Set(tags.filter(t => !recommendedNames.has(t.toLowerCase())))];
    const warning = tags.length && !supportsTags();
    $('tagStatus').textContent = warning ? '这款模型不适合 [] 语气标签，请切换到 v3 / v4 或移除标签。' : custom.length ? `含试验标签：${custom.map(t => `[${t}]`).join(' ')}。效果需要试听确认。` : `已添加 ${tags.length} 个标签。少量、明确的提示通常更容易比较效果。`;
    $('tagStatus').className = `hint${warning ? ' error' : ''}`;
    setButtons();
  }
  $('voiceSearch').addEventListener('input',() => renderVoices());
  $('voiceCategory').addEventListener('change',() => renderVoices());
  $('voiceSelect').addEventListener('change',() => { updateVoice(); savePrefs(); setButtons(); });
  $('model').addEventListener('change',() => { updateModel(); savePrefs(); });
  $('text').addEventListener('input',() => { updateText(); savePrefs(); });
  $('removeTags').addEventListener('click',() => { $('text').value = $('text').value.replace(/\[[^\]\r\n]*\][ \t]*/g,''); updateText(); savePrefs(); $('text').focus(); });

  function insertTag(tag) {
    const area = $('text'), start = area.selectionStart, end = area.selectionEnd;
    const insertion = `[${tag}] ` + area.value.slice(start,end);
    if (Array.from(area.value).length + tag.length + 3 > area.maxLength) return status('generationStatus','文本已达到当前模型的试听长度上限。','error');
    area.setRangeText(insertion,start,end,'end'); area.focus(); updateText(); savePrefs();
  }
  function renderTags() {
    $('tagTabs').replaceChildren(); $('tagList').replaceChildren();
    $('tagGroupHint').textContent = state.group === experimentalGroup
      ? '呼吸、气声与人声反应的试听集合。标有「试验」的项目是自然语言候选，未确认稳定支持；[groan] 也可能表现为疲惫或不适的低哼。先单独比较，再组合使用。'
      : '点击标签即可插入正文；实际效果取决于模型、音色与上下文。';
    for (const name of Object.keys(groups)) {
      const tab = document.createElement('button'); tab.type = 'button'; tab.textContent = name;
      tab.setAttribute('aria-pressed',String(name === state.group));
      tab.addEventListener('click',() => { state.group = name; renderTags(); }); $('tagTabs').append(tab);
    }
    const items = state.group === '自定义' ? state.custom.map(t => [t,'试验']) : groups[state.group];
    for (const [tag,label] of items) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'tag';
      const experimental = state.group === experimentalGroup && !recommendedNames.has(tag);
      const title = document.createElement('span'); title.textContent = label + (experimental ? ' · 试验' : '');
      const code = document.createElement('code'); code.textContent = `[${tag}]`; button.append(title,code);
      button.title = `插入 [${tag}]${experimental ? '；需试听确认，不自动加入 AI 推荐集合' : ''}`; button.addEventListener('click',() => insertTag(tag));
      if (state.group !== '自定义') $('tagList').append(button);
      else {
        const wrap = document.createElement('span'); wrap.className = 'custom-chip';
        const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'remove-custom'; remove.textContent = '×'; remove.setAttribute('aria-label',`删除自定义标签 ${tag}`);
        remove.addEventListener('click',() => { state.custom = state.custom.filter(t => t !== tag); savePrefs(); renderTags(); });
        wrap.append(button,remove); $('tagList').append(wrap);
      }
    }
    if (!items.length) { const hint = document.createElement('p'); hint.className = 'hint'; hint.textContent = '保存的自定义标签会出现在这里，之后也能一键插入。'; $('tagList').append(hint); }
  }
  $('addCustomTag').addEventListener('click',() => {
    let tag = $('customTag').value.trim();
    if (tag.startsWith('[') && tag.endsWith(']')) tag = tag.slice(1,-1).trim();
    if (!tag || /[\[\]\r\n]/.test(tag) || tag.length > 80) return status('customStatus','请输入一个 1～80 字符的短描述，不要嵌套方括号。','error');
    if (!state.custom.includes(tag)) {
      if (state.custom.length >= 40) return status('customStatus','最多保存 40 个自定义标签，请先删除不常用的项目。','error');
      state.custom.push(tag);
    }
    state.group = '自定义'; renderTags(); insertTag(tag); savePrefs(); $('customTag').value = '';
    status('customStatus',`已保存 [${tag}]；自定义标签不会自动进入 AI 的推荐集合。`);
  });
  $('customTag').addEventListener('keydown',event => { if (event.key === 'Enter') { event.preventDefault(); $('addCustomTag').click(); } });
  const rules = `为 ElevenLabs v3 / v4 的待朗读文本添加少量 Audio Tags。\n\n允许集合（项目推荐集合，并非官方穷尽列表）：\n${recommended.map(([tag,label]) => `[${tag}] ${label}`).join('；')}。\n\n规则：\n1. 只从上述集合选择，不自行创造标签。不确定时保持无标签。\n2. 保留原文的全部文字、顺序和含义，只插入标签，不写解释。\n3. 每句通常 0～1 个，必要时最多 2 个；不要连续堆叠矛盾情绪。\n4. 语气标签放在对应句子或短语前；叹气、笑声等放在合理停顿处。\n5. 使用英文半角方括号，沿用集合中的小写写法。\n6. 不添加视觉动作、环境音、背景音乐、角色控制指令或 SSML。\n7. 普通聊天显示正文；仅向支持 Audio Tags 的语音模型发送带标签文本。\n8. 自定义标签须先由使用者针对目标音色试听确认，再人工扩充允许集合。\n\n只输出添加标签后的原文。`;
  $('aiRules').value = rules;
  $('copyRules').addEventListener('click',async () => {
    try { await navigator.clipboard.writeText(rules); $('copyRules').textContent = '已复制 ✓'; setTimeout(() => { $('copyRules').textContent = '复制给 AI 的规则 ↗'; },1800); }
    catch { document.querySelector('.guide').open = true; $('aiRules').focus(); $('aiRules').select(); $('copyRules').textContent = '规则已选中，请手动复制'; }
  });

  function renderHistory() {
    $('history').replaceChildren();
    if (!state.records.length) {
      const empty = document.createElement('div'); empty.className = 'empty-state';
      const title = document.createElement('p'); title.textContent = '第一段声音，还在等你。';
      const hint = document.createElement('span'); hint.textContent = '生成后可在这里重播、下载，或复用那一句话。'; empty.append(title,hint); $('history').append(empty);
    }
    state.records.forEach((record,index) => {
      const row = document.createElement('article'); row.className = 'recording';
      const info = document.createElement('div'), title = document.createElement('strong'), meta = document.createElement('div'), transcript = document.createElement('p');
      title.textContent = record.voiceName; meta.className = 'meta';
      meta.textContent = `${record.modelName} · ${record.time} · ${record.seconds} 秒生成 · ${Array.from(record.text).length} 字符`;
      transcript.className = 'transcript'; transcript.textContent = record.text; info.append(title,meta,transcript);
      const player = document.createElement('div'), audio = document.createElement('audio'); audio.controls = true; audio.preload = 'metadata'; audio.src = record.url;
      audio.setAttribute('aria-label',`${record.voiceName} 试听音频`);
      audio.addEventListener('play',() => document.querySelectorAll('audio').forEach(other => { if (other !== audio) other.pause(); }));
      const actions = document.createElement('div'); actions.className = 'record-actions';
      const reuse = document.createElement('button'); reuse.type = 'button'; reuse.className = 'text-button'; reuse.textContent = '复用文本';
      reuse.addEventListener('click',() => { $('text').value = record.text; updateText(); savePrefs(); $('text').focus(); $('text').scrollIntoView({block:'center'}); });
      const download = document.createElement('a'); download.textContent = '下载 MP3 ↓'; download.href = record.url;
      download.download = `elevenlabs-${record.voiceName.replace(/[<>:"/\\|?*\x00-\x1f]/g,'_')}-${record.number}.mp3`;
      actions.append(reuse,download); player.append(audio,actions); row.append(info,player); $('history').append(row);
      if (index === 0) record.audio = audio;
    });
    setButtons();
  }
  function stopPlayers() { document.querySelectorAll('audio').forEach(audio => { audio.pause(); audio.removeAttribute('src'); audio.load(); }); }
  $('clearHistory').addEventListener('click',() => { stopPlayers(); state.records.forEach(r => URL.revokeObjectURL(r.url)); state.records = []; renderHistory(); });
  let recordingNumber = 0;
  $('generate').addEventListener('click',async () => {
    if (state.generating) return;
    const voice = state.voices.find(v => v.voice_id === $('voiceSelect').value), text = $('text').value.trim(), model = $('model').value;
    if (!voice || state.loadedKey !== $('apiKey').value.trim()) return status('generationStatus','请先连接账号并选择音色。','error');
    if (!text || Array.from(text).length > $('text').maxLength) return status('generationStatus','请填写文本，并控制在当前模型的长度上限内。','error');
    if (/\[[^\]]+\]/.test(text) && !supportsTags()) return status('generationStatus','请使用 v3 / v4 试听标签，或移除标签后继续。','error');
    if ((text.match(/\[/g) || []).length !== (text.match(/\]/g) || []).length) return status('generationStatus','方括号没有配对，请检查标签。','error');
    const snapshot = {text,modelName:$('model').selectedOptions[0].textContent,voiceName:voice.name || voice.voice_id};
    state.generating = true; setButtons(); savePrefs();
    document.querySelectorAll('audio').forEach(audio => audio.pause());
    const controller = new AbortController(), start = performance.now(), timer = setTimeout(() => controller.abort(),135000);
    status('generationStatus',`正在让「${snapshot.voiceName}」说这句话…`);
    try {
      const response = await post('/api/preview',{api_key:state.loadedKey,voice_id:voice.voice_id,model_id:model,text},controller.signal);
      const blob = await response.blob();
      if (!blob.size) throw new Error('没有收到音频，请稍后再试。');
      const seconds = response.headers.get('X-Generation-Seconds') || ((performance.now()-start)/1000).toFixed(2);
      const record = {...snapshot,seconds,url:URL.createObjectURL(blob),time:new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}),number:++recordingNumber};
      stopPlayers(); state.records.unshift(record);
      if (state.records.length > 8) URL.revokeObjectURL(state.records.pop().url);
      renderHistory();
      status('generationStatus',`「${snapshot.voiceName}」的音频已生成，可在下方重播和下载。`,'success');
      try { await record.audio.play(); } catch { status('generationStatus','音频已生成，请点击下方播放器开始试听。','success'); }
    } catch (error) {
      status('generationStatus',error.name === 'AbortError' ? '等待超时。请求可能已生成并扣费，请先查看官方历史，再决定是否重试。' : (error.message || '请求失败，请检查本机试听服务是否仍在运行。'),'error');
    } finally { clearTimeout(timer); state.generating = false; setButtons(); }
  });
  window.addEventListener('beforeunload',() => state.records.forEach(r => URL.revokeObjectURL(r.url)));
  renderTags(); updateModel();
})();

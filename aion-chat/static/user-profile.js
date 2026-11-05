let profileData = null;
let draftEntries = [];
let profileDirty = false;
let profileBusy = false;
let editingEntry = null;
let entryBeforeEdit = null;
let stateEditing = false;
const profileEl = id => document.getElementById(id);

function profileEscape(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function profileTime(ts) {
  if (!ts) return '';
  const date = new Date(ts * 1000);
  const pad = value => String(value).padStart(2, '0');
  return `${pad(date.getMonth()+1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
function localDateValue(ts) {
  const d = new Date(ts * 1000);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0,16);
}
function markProfileDirty() {
  const entriesChanged = draftEntries.length !== profileData.entries.length || draftEntries.some((entry, index) => {
    const saved = profileData.entries[index];
    return !saved || ['key','category','subcategory','content','next_step','kind','status'].some(field => entry[field] !== saved[field]);
  });
  const state = profileData.current_state;
  const text = profileEl('stateText').value.trim();
  const stateChanged = text !== (state?.text || '') || (text && (
    profileEl('statePhase').value !== state?.phase || profileEl('stateExpiry').value !== localDateValue(state?.expires_at || 0)));
  profileDirty = !!(entriesChanged || stateChanged);
  profileEl('save').disabled = !profileDirty || profileBusy;
  profileEl('saveHint').textContent = profileDirty ? '有未保存的修改' : '尚无修改';
  renderProfileBudget();
}
async function profileRequest(url, options = {}) {
  const response = await fetch(url, {cache:'no-store', ...options});
  const data = await response.json();
  if (!response.ok) {
    const message = typeof data.detail === 'string' ? data.detail : '请检查填写内容后再保存';
    throw new Error(message);
  }
  return data;
}
function renderProfileStatus(data) {
  profileEl('enabled').checked = data.enabled;
  profileEl('status').textContent = data.enabled
    ? `已开启${data.pending ? ` · ${data.pending} 条待整理` : ''}${data.failed ? ` · ${data.failed} 条未处理完整` : ''}${data.digest_pending ? ` · ${data.digest_pending} 批近期动态待整理` : ''}`
    : '已关闭';
  profileEl('model').textContent = `即时更新：${data.model || '未配置'} · 整体维护：随记忆总结${data.last_digest_at ? ` · 最近 ${profileTime(data.last_digest_at)}` : ''}`;
  profileEl('error').textContent = data.last_error || '';
  profileEl('error').hidden = !data.last_error;
  const init = data.initialization || {};
  profileEl('initializationStatus').textContent = init.status === 'running'
    ? `正在整理最近一周：${init.cursor || 0}/${init.total || 0} 条原文 · ${init.model || ''}`
    : init.status === 'error' ? `本周整理进度已保存：${init.error || '稍后可继续'}`
    : init.status === 'done' ? `最近一周已整理 · ${init.total || 0} 条原文 · ${init.calls || 0} 次调用，后续随聊天与记忆总结更新`
    : init.status === 'pending' ? `整理已暂停：${init.cursor || 0}/${init.total || 0} 条，可继续` : '';
  profileEl('initializeProfile').hidden = init.status === 'done' || init.status === 'running';
  profileEl('initializeProfile').textContent = init.cursor ? '继续整理本周 · 5.6 Sol' : '整理最近一周 · 5.6 Sol';
}
function renderProfileBudget() {
  if (!profileData) return;
  profileEl('budget').textContent = `当前清单约 ${profileData.prompt_tokens || 0} / ${profileData.prompt_limit || 2000} tokens`
    + (profileDirty ? ' · 保存后更新预览' : '');
  profileEl('promptPreview').textContent = profileData.enabled
    ? profileData.prompt_preview || '当前没有需要发送的资料'
    : '总开关已关闭，不向 AI 发送近期动态';
}
function renderProfileEntries() {
  const categories = profileData.categories;
  const openGroups = new Set(Array.from(profileEl('entries').querySelectorAll?.('details[open][data-section]') || [], node => node.dataset.section));
  const options = (items, value) => Object.entries(items).map(([key,label]) =>
    `<option value="${key}" ${value === key ? 'selected' : ''}>${profileEscape(label)}</option>`).join('');
  const row = (entry, index) => {
    const stamp = entry.source_ts || entry.updated_at;
    const time = `<time class="entry-time" ${stamp ? `datetime="${new Date(stamp*1000).toISOString()}" title="${profileEscape(new Date(stamp*1000).toLocaleString('zh-CN'))}"` : ''}>${profileTime(stamp)}</time>`;
    const status = entry.view_status || entry.status || 'active';
    const progress = entry.category === 'recent'
      ? `<span class="entry-progress">${profileEscape(profileData.statuses?.[status] || '')}${entry.kind === 'task' ? ' · 待办' : entry.kind === 'project' ? ' · 项目' : ''}</span>` : '';
    const evidence = [entry.event_at ? `发生于 ${new Date(entry.event_at*1000).toLocaleDateString('zh-CN')}` : '',
      entry.certainty === 'inferred' ? '有依据的推测' : entry.certainty === 'conflict' ? '存在冲突 · 待确认' : '',
      entry.included ? '发送给 AI' : entry.omission_reason || ''].filter(Boolean).join(' · ');
    return `<article class="profile-entry${editingEntry === entry.key ? ' is-editing' : ''}" data-index="${index}">
      ${editingEntry === entry.key ? `<div class="entry-editor">
        <select aria-label="资料分类" data-field="category">${options(categories, entry.category)}</select>
        <select aria-label="动态分类" data-field="subcategory">${options(profileData.subcategories?.[entry.category] || {}, entry.subcategory)}</select>
        <textarea rows="3" maxlength="160" aria-label="近期动态" data-field="content" placeholder="写下最近正在做的事、状态或偏好">${profileEscape(entry.content)}</textarea>
        <textarea rows="2" maxlength="80" aria-label="下一步" data-field="next_step" placeholder="下一步（仅填写明确的安排，可留空）">${profileEscape(entry.next_step || "")}</textarea>
        <select aria-label="进展" data-field="status">${options(profileData.statuses, entry.status || "active")}</select>
        <div class="editor-actions"><button class="entry-remove" data-action="remove" type="button">删除</button><button class="quiet-button" data-action="cancel" type="button">取消</button><button class="save-button" data-action="save" type="button">保存</button></div>
      </div>` : `<div class="entry-copy"><p>${profileEscape(entry.content)}</p>${entry.next_step ? `<p class="entry-next">下一步：${profileEscape(entry.next_step)}</p>` : ""}${time}${progress}${evidence ? `<span class="entry-progress">${profileEscape(evidence)}</span>` : ''}</div>
        <div class="entry-actions"><button class="quick-remove" data-action="quick-remove" aria-label="删除这条动态" title="删除这条动态" type="button">删除</button>
        <button class="edit-button" data-action="edit" aria-label="编辑动态" title="编辑动态" type="button"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m16 3 5 5M4 20l4-1L21 6a2 2 0 0 0-3-3L5 16l-1 4Z"/></svg></button></div>`}
    </article>`;
  };
  profileEl('entries').innerHTML = draftEntries.length ? Object.entries(categories).map(([cat,title]) => {
    const rows = draftEntries.map((entry,index) => ({entry,index})).filter(({entry}) => entry.category === cat)
      .sort((a,b) => (b.entry.source_ts || b.entry.updated_at || 0) - (a.entry.source_ts || a.entry.updated_at || 0));
    const ended = ({entry}) => ['done','cancelled','retired'].includes(entry.view_status || entry.status);
    const current = rows.filter(item => !ended(item));
    const archive = rows.filter(ended);
    const archiveEditing = archive.some(({entry}) => entry.key === editingEntry);
    const grouped = items => Object.entries(profileData.subcategories?.[cat] || {legacy:''}).map(([sub,label]) => {
      const matching = items.filter(({entry}) => (entry.subcategory || Object.keys(profileData.subcategories?.[cat] || {legacy:''})[0]) === sub);
      return matching.length ? `${label ? `<h5>${profileEscape(label)}</h5>` : ''}${matching.map(({entry,index}) => row(entry,index)).join('')}` : '';
    }).join('');
    if (!rows.length) return '';
    const body = `<section class="profile-current" aria-label="当前事项">
      <div class="profile-list-heading"><h4>${profileData.enabled === false ? '当前保留的事项' : '当前带入 AI'}</h4><span class="profile-count">${current.length} 项</span></div>
      <p class="profile-list-note">${profileData.enabled === false ? '总开关已关闭，以下内容暂不带入 AI。' : '以下是仍在关注的事项，会作为近期背景发送给 AI。'}</p>
      ${current.length ? grouped(current) : '<p class="profile-empty">暂无当前事项</p>'}
      </section>`
      + (archive.length ? `<details class="profile-archive" data-section="${cat}-ended"${archiveEditing || openGroups.has(`${cat}-ended`) ? ' open' : ''}>
        <summary><span class="profile-list-heading"><span class="profile-archive-title">已结束与归档</span><span class="profile-count">${archive.length} 项</span></span>
        <span class="profile-list-note">不再带入 AI · 已完成、已取消或已淡出</span><span class="profile-archive-toggle" aria-hidden="true"></span></summary>
        <div class="profile-archive-body">${grouped(archive)}</div></details>` : '');
    return `<div class="profile-group" aria-label="${profileEscape(title)}">${body}</div>`;
  }).join('') : '<p class="profile-empty">还没有近期动态。<br>可以先手动写几条，也会从新聊天中维护。</p>';
}
function renderState() {
  const state = profileData.current_state;
  profileEl('stateText').value = state?.text || '';
  profileEl('statePhase').value = state?.phase || 'unknown';
  profileEl('stateExpiry').value = localDateValue(state?.expires_at || Date.now()/1000 + 3600);
  profileEl('stateContent').textContent = state?.text || '暂无状态';
  const phase = state && (state.phase === 'finished' ? '已结束' : state.expires_at <= Date.now()/1000
    ? '最后已知状态 · 已过期' : profileData.phases?.[state.phase] || '');
  profileEl('stateTime').textContent = [profileTime(state?.updated_at || state?.event_at), phase].filter(Boolean).join(' · ');
  profileEl('stateView').hidden = stateEditing;
  profileEl('stateEditor').hidden = !stateEditing;
}
function renderProfile(data) {
  profileData = data;
  draftEntries = data.entries.map(e => ({...e}));
  editingEntry = null;
  entryBeforeEdit = null;
  stateEditing = false;
  renderProfileStatus(data);
  renderProfileEntries();
  renderState();
  profileDirty = false;
  profileEl('save').disabled = true;
  profileEl('saveHint').textContent = '尚无修改';
  renderProfileBudget();
}
async function loadProfile({background = false} = {}) {
  try {
    const query = profileEl('previewQuery')?.value.trim() || '';
    const data = await profileRequest('/api/user-profile' + (query ? `?query=${encodeURIComponent(query)}` : ''));
    if (data.version !== 5 || data.prompt_limit !== 2000) {
      profileEl('status').textContent = '后端版本尚未更新';
      throw new Error('请重启小家后端，再刷新此页使用近期生活与待办');
    }
    if (background && (profileDirty || editingEntry || stateEditing)) {
      renderProfileStatus(data);
      if (data.revision !== profileData?.revision) profileEl('saveHint').textContent = '后台资料已更新；你的修改仍保留，保存前请刷新核对';
      return;
    }
    renderProfile(data);
  } catch (error) {
    profileEl('saveHint').textContent = error.message || '加载失败，请稍后刷新';
  }
}
profileEl('entries').addEventListener('input', event => {
  const row = event.target.closest('[data-index]');
  if (!row || !event.target.dataset.field) return;
  const entry = draftEntries[Number(row.dataset.index)];
  const field = event.target.dataset.field;
  entry[field] = event.target.value;
  if (field === 'category') entry.subcategory = Object.keys(profileData.subcategories[entry.category])[0];
  if (['category','subcategory'].includes(field)) entry.kind = ({plans:'task',projects:'project',ideas:'idea'})[entry.subcategory] || 'context';
  markProfileDirty();
  if (field === 'category') renderProfileEntries();
});
profileEl('entries').addEventListener('click', event => {
  const action = event.target.closest('[data-action]')?.dataset.action;
  if (!action || profileBusy) return;
  const index = Number(event.target.closest('[data-index]').dataset.index);
  if (action === 'edit') {
    editingEntry = draftEntries[index].key;
    entryBeforeEdit = {...draftEntries[index]};
    renderProfileEntries();
    profileEl('entries').querySelector(`[data-index="${index}"] textarea`).focus();
    return;
  }
  if (action === 'save' && profileDirty) return profileEl('save').click();
  if (action === 'remove' || action === 'quick-remove' || (action === 'cancel' && !profileData.entries.some(e => e.key === editingEntry))) {
    draftEntries.splice(index, 1);
  } else if (action === 'cancel') {
    draftEntries[index] = entryBeforeEdit;
  }
  editingEntry = null;
  entryBeforeEdit = null;
  renderProfileEntries();
  markProfileDirty();
  if (action === 'quick-remove') profileEl('save').click();
});
profileEl('addEntry').addEventListener('click', () => {
  if (!profileData) return;
  if (draftEntries.filter(e => !['done','cancelled','retired'].includes(e.status)).length >= (profileData.max_entries || 24)) return showToast('请先合并或移除旧资料');
  const id = globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}.${Math.random().toString(36).slice(2)}`;
  const entry = {key:`manual.${id}`, category:'recent', subcategory:'plans', kind:'task', status:'active', content:'', next_step:'', locked:false};
  draftEntries.push(entry);
  editingEntry = entry.key;
  entryBeforeEdit = {...entry};
  renderProfileEntries();
  markProfileDirty();
  profileEl('entries').querySelector(`[data-index="${draftEntries.length-1}"] textarea`).focus();
});
for (const id of ['stateText','statePhase','stateExpiry']) {
  profileEl(id).addEventListener('input', markProfileDirty);
}
profileEl('editState').addEventListener('click', () => {
  stateEditing = true;
  profileEl('stateView').hidden = true;
  profileEl('stateEditor').hidden = false;
  profileEl('stateText').focus();
});
profileEl('cancelState').addEventListener('click', () => {
  stateEditing = false;
  renderState();
  markProfileDirty();
});
profileEl('saveState').addEventListener('click', () => {
  if (profileDirty) return profileEl('save').click();
  stateEditing = false;
  renderState();
});
profileEl('clearState').addEventListener('click', () => {
  profileEl('stateText').value = '';
  markProfileDirty();
});
profileEl('enabled').addEventListener('change', async event => {
  event.target.disabled = true;
  try {
    await profileRequest('/api/capabilities/post_sentinel', {
      method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({enabled:event.target.checked})
    });
    await loadProfile({background:true});
  } catch (error) {
    event.target.checked = !event.target.checked;
    showToast(error.message);
  } finally { event.target.disabled = false; }
});
profileEl('refresh').addEventListener('click', () => {
  if (!profileDirty || confirm('刷新会放弃尚未保存的修改，继续吗？')) loadProfile();
});
profileEl('save').addEventListener('click', async () => {
  if (!profileData || profileBusy) return;
  const text = profileEl('stateText').value.trim();
  const expiryValue = profileEl('stateExpiry').value;
  const originalExpiry = profileData.current_state?.expires_at;
  // Preserve seconds when another field is edited; datetime-local displays minutes.
  const expiresAt = originalExpiry && expiryValue === localDateValue(originalExpiry)
    ? originalExpiry : new Date(expiryValue).getTime()/1000;
  if (draftEntries.some(e => !e.content.trim())) return showToast('请填写每条动态的内容');
  if (draftEntries.some(e => ['available_at','due_at','expires_at'].some(f => e[f] != null && !Number.isFinite(e[f])))) return showToast('请检查资料中的日期');
  if (text && !Number.isFinite(expiresAt)) return showToast('请填写状态有效时间');
  const body = {
    revision:profileData.revision,
    entries:draftEntries.map(({key,category,subcategory,content,next_step,kind,status}) => ({key,category,subcategory,content:content.trim(),next_step:next_step || '',kind:kind || 'context',status:status || 'active',locked:false})),
    current_state:text ? {text,phase:profileEl('statePhase').value,expires_at:expiresAt,locked:false} : null
  };
  profileBusy = true;
  profileEl('save').disabled = true;
  try {
    const data = await profileRequest('/api/user-profile', {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    renderProfile(data);
    profileEl('saveHint').textContent = '已保存';
    showToast('近期动态已保存');
  } catch (error) {
    profileEl('saveHint').textContent = `${error.message}；未保存的内容仍保留`;
  } finally {
    profileBusy = false;
    profileEl('save').disabled = !profileDirty;
  }
});
connectCommonWS(message => {
  if (message.type === 'user_profile_changed' || message.type === 'capability_config_changed') loadProfile({background:true});
});
profileEl('previewButton')?.addEventListener('click', () => loadProfile({background:true}));
profileEl('previewEnd').addEventListener('click', () => {
  const preview = profileEl('promptPreview');
  preview.scrollTop = preview.scrollHeight;
});
profileEl('initializeProfile').addEventListener('click', async event => {
  event.target.disabled = true;
  try {
    await profileRequest('/api/user-profile/initialize', {method:'POST'});
    await loadProfile({background:true});
  } catch (error) { showToast(error.message); }
  finally { event.target.disabled = false; }
});
fetch('/api/worldbook').then(r => r.json()).then(data => {
  if (data.user_name) profileEl('pageTitle').textContent = `${data.user_name}的近期生活`;
}).catch(() => {});
loadProfile();
setInterval(() => { if (!document.hidden) loadProfile({background:true}); }, 60000);

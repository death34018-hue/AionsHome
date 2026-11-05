/* One manager for both stores. Shared styles/theme/navigation come from common.js. */
(() => {
  const el = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const params = new URLSearchParams(location.search);
  const names = {main:'主AI', chatroom:'第二AI'};
  const state = {store:params.get('store') === 'chatroom' || params.get('store') === 'second' ? 'chatroom' : 'main',
    view:params.get('view') === 'unresolved' ? 'unresolved' : 'memories', filter:['friend','unresolved'].includes(params.get('kind')) ? params.get('kind') : 'all', page:1, limit:20, total:0, items:[], selected:null, detail:null,
    pane:'source', sources:[], sourcePage:0, sourceTotal:0, sourceExact:true, sourceMore:false};
  let listRequest = 0, detailRequest = 0, sourceRequest = 0, dialogRequest = 0, searchTimer, dialogBusy = false, refreshPending = false;
  let initialDraft = null;
  const base = (store = state.store) => `/api/memory-library/${store}`;
  const endpoint = (id, store = state.store) => `${base(store)}/${encodeURIComponent(id)}`;
  const request = (method, url, data, timeoutMs = 45000) => api(method, url, data, {timeoutMs});
  const keywords = raw => { try { const a = JSON.parse(raw); if (Array.isArray(a)) return a.join(' · '); } catch {} return raw || ''; };
  const stamp = ts => ts ? new Date(ts * 1000).toLocaleString('zh-CN', {year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '未记录';
  const day = ts => { const d = new Date((ts || Date.now()/1000)*1000); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; };
  const kindLabel = m => ({daily:'日常', long_term:'长期重要', friend:'朋友记忆'}[m.memory_kind] || '长期重要');
  const periodLabel = m => ({day:'日摘要',week:'周摘要',month:'月摘要',year:'年摘要',fact:'提炼事实'}[m.period_kind] || (m.is_atom ? '' : '压缩摘要'));
  const sourceHref = m => `${m.id.startsWith('private:') ? '/chat?conv=' : '/chatroom?room='}${encodeURIComponent(m.window_id)}&msg=${encodeURIComponent(m.id.slice(m.id.indexOf(':')+1))}`;
  function status(text) { el('status').textContent = text; }
  function notify(text) { el('toast').textContent = text; el('toast').hidden = false; clearTimeout(notify.timer); notify.timer = setTimeout(() => el('toast').hidden = true, 4000); }

  function renderControls() {
    document.querySelectorAll('[data-store]').forEach(b => b.setAttribute('aria-pressed', b.dataset.store === state.store));
    document.querySelectorAll('[data-view]').forEach(b => b.setAttribute('aria-selected', b.dataset.view === state.view));
    el('viewHelp').textContent = state.view === 'atoms' ? '活跃与归档原文' : state.view === 'unresolved' || state.filter === 'unresolved' ? '含已归档的未完成记忆' : '当前参与召回';
    const filters = state.view === 'atoms' ? [['all','全部'],['active','活跃'],['cold','已归档']] : [['all','全部'],['daily','日常'],['long_term','长期重要'],['friend','朋友记忆'],...(state.view === 'memories' ? [['unresolved','当前未完成']] : [])];
    el('chips').innerHTML = filters.map(([id,label]) => `<button class="chip" data-filter="${id}" aria-pressed="${state.filter === id}">${label}</button>`).join('');
  }
  function renderList() {
    el('count').textContent = `${state.total} 条`;
    const pages = Math.max(1, Math.ceil(state.total / state.limit));
    el('pageInput').value = state.page;
    el('pageInput').max = pages;
    el('pageLabel').textContent = `/ ${pages} 页`;
    el('prev').disabled = state.page <= 1;
    el('next').disabled = state.page >= pages;
    el('list').innerHTML = state.items.length ? state.items.map(m => `<article class="entry ${m.id === state.selected ? 'selected' : ''}" data-id="${esc(m.id)}">
      <button class="entry-open"><div class="entry-head"><time>${esc(day(m.source_start_ts || m.created_at).replaceAll('-','.'))}</time><span class="pill ${m.memory_kind === 'daily' ? 'green' : ''}">${kindLabel(m)}</span><span class="end">${m.unresolved ? '未完成 · ' : ''}${state.view === 'atoms' ? (m.archive_state === 'cold' ? '<span class="pill gray">已归档</span>' : '<i class="dot"></i>活跃') : periodLabel(m)}</span></div><p class="bodytext">${esc(m.content)}</p></button>
      <div class="entry-foot"><span class="entry-keywords" title="${esc(keywords(m.keywords))}">${esc(keywords(m.keywords) || '无关键词')}</span><div class="entry-actions"><button data-action="source">原文</button><button data-action="edit">编辑</button><button class="danger" data-action="delete">删除</button></div></div></article>`).join('') : '<div class="empty">没有找到匹配的记忆<br><small>试试其他关键词，或新增一条记忆</small></div>';
  }
  function refreshBlocked() {
    return el('dialog').open || (state.detailReading && el('detail').classList.contains('open')) || state.page > 1 || el('list').scrollTop > 20;
  }
  function snapshotBridge() { try { return window.top?.AppSharedData || window.AppSharedData; } catch { return window.AppSharedData; } }
  function snapshotKey() {
    return state.page === 1 && state.filter === 'all' && !el('search').value.trim() && !el('from').value && !el('to').value ? `memory_library_v4:${state.store}:${state.view}` : null;
  }
  function saveSnapshot(result) {
    const key = snapshotKey(); if (!key) return;
    try { const data = JSON.stringify({items:result.items,total:result.total}); localStorage.setItem(key,data); snapshotBridge()?.put?.(key,data); } catch {}
  }
  function restoreSnapshot() {
    const key = snapshotKey(); if (!key) return false;
    try {
      const data = JSON.parse(snapshotBridge()?.get?.(key) || localStorage.getItem(key) || 'null');
      if (!Array.isArray(data?.items) || !Number.isFinite(data.total)) return false;
      state.items = data.items; state.total = data.total; renderList(); return true;
    } catch { return false; }
  }
  function jumpToPage() {
    const page = Number(el('pageInput').value);
    const pages = Math.max(1, Math.ceil(state.total / state.limit));
    if (!Number.isInteger(page) || page < 1 || page > pages) {
      notify(`请输入 1 到 ${pages} 之间的整数页码`);
      return;
    }
    el('pageInput').blur();
    if (page === state.page) return;
    state.page = page;
    el('list').scrollTop = 0;
    loadList();
  }
  async function loadList({keepDetail = false, quiet = false, keepPosition = false} = {}) {
    if (quiet && refreshBlocked()) { refreshPending = true; return; }
    refreshPending = false;
    clearTimeout(searchTimer);
    const serial = ++listRequest;
    const previousPage = state.page;
    renderControls();
    el('list').setAttribute('aria-busy', 'true');
    // A one-line loading placeholder collapses the scroller during an in-place refresh.
    const inPlace = quiet || keepPosition;
    const cached = !inPlace && restoreSnapshot();
    if (cached) status('正在更新本地缓存…');
    else if (!inPlace) el('list').innerHTML = '<div class="empty">正在读取记忆…</div>';
    el('prev').disabled = el('next').disabled = true;
    el('pageInput').disabled = el('jump').disabled = true;
    const q = new URLSearchParams({view:state.view,page:state.page,limit:state.limit,q:el('search').value.trim()});
    q.set(state.view === 'atoms' ? 'state' : 'kind', state.filter);
    if (el('from').value) q.set('start', el('from').value);
    if (el('to').value) q.set('end', el('to').value);
    try {
      const result = await request('GET', `${base()}?${q}`);
      if (serial !== listRequest) return;
      if (quiet && refreshBlocked()) { refreshPending = true; return; }
      Object.assign(state, {items:result.items,total:result.total,page:result.page});
      saveSnapshot(result);
      if (cached) status('');
      renderList();
      if (inPlace && result.page < previousPage) el('list').scrollTop = el('list').scrollHeight;
      if (!keepDetail) {
        closeDetail();
        state.selected = null; state.detail = null;
        el('detailBody').innerHTML = '<p class="note">选择一条记忆，查看内容和来源。</p>';
        el('edit').disabled = el('delete').disabled = true;
        if (innerWidth >= 750 && state.items.length) openDetail(state.items[0].id, {passive:true});
      }
    } catch (e) {
      if (serial !== listRequest) return;
      if (cached || inPlace) {
        status(`暂未更新：${e.message}。当前显示上次读取的记忆。`);
        el('status').insertAdjacentHTML('beforeend',' <button class="secondary" id="refreshLibrary">刷新列表</button>');
        return;
      }
      el('count').textContent = '读取失败';
      el('list').innerHTML = `<div class="empty">${esc(e.message)}<br><button class="secondary" data-retry>重新加载</button></div>`;
    } finally {
      if (serial === listRequest) {
        el('list').setAttribute('aria-busy', 'false');
        el('pageInput').disabled = el('jump').disabled = false;
        if (inPlace || cached) { el('prev').disabled = state.page <= 1; el('next').disabled = state.page >= Math.ceil(state.total/state.limit); }
      }
    }
  }
  function closeDetail() {
    ++detailRequest; ++sourceRequest;
    state.detailReading = false;
    el('detail').classList.remove('open'); el('scrim').classList.remove('show');
    el('detail').removeAttribute('role'); el('detail').removeAttribute('aria-modal');
    if (refreshPending && !el('dialog').open) queueMicrotask(() => { if (refreshPending) loadList({quiet:true}); });
  }
  async function openDetail(id, {passive = false} = {}) {
    const serial = ++detailRequest;
    ++sourceRequest;
    state.selected = id; state.pane = 'source'; state.detail = null;
    state.detailReading = !passive;
    el('detail').classList.add('open');
    if (innerWidth < 750) { el('scrim').classList.add('show'); el('detail').setAttribute('role','dialog'); el('detail').setAttribute('aria-modal','true'); }
    el('detailBody').innerHTML = '<p class="note">正在读取详情…</p>';
    el('edit').disabled = el('delete').disabled = true;
    renderList();
    try {
      const result = await request('GET', endpoint(id));
      if (serial !== detailRequest) return;
      state.detail = result;
      el('detailTitle').textContent = result.item.is_atom ? '原子记忆详情' : '记忆详情';
      el('edit').disabled = el('delete').disabled = false;
      renderDetail();
      if (innerWidth < 750) el('closeDetail').focus();
      loadSources();
    } catch (e) { if (serial === detailRequest) el('detailBody').innerHTML = `<p class="dialog-error">${esc(e.message)}</p>`; }
  }
  function renderDetail() {
    const {item:m, related, originals} = state.detail;
    el('detailBody').innerHTML = `<div class="eyebrow">${esc(names[state.store])} · ${m.is_atom ? '原子记忆' : periodLabel(m)}</div><p class="detail-text">${esc(m.content)}</p>
      <div class="kv"><div><label>发生时间</label>${esc(m.memory_time_label || stamp(m.source_start_ts || m.created_at))}</div><div><label>状态</label>${m.archive_state === 'cold' ? '已归档 · 保留来源' : '活跃 · 参与召回'}</div><div><label>分类</label>${kindLabel(m)}${m.unresolved ? ' · 当前未完成' : ''}</div><div><label>关键词</label>${esc(keywords(m.keywords) || '无')}</div></div>
      <div class="detail-tabs"><button data-pane="source" class="${state.pane === 'source' ? 'active' : ''}">聊天原文</button><button data-pane="related" class="${state.pane === 'related' ? 'active' : ''}">关联记忆${related.length + originals.length ? ` · ${related.length + originals.length}` : ''}</button></div><div id="detailPane"></div>`;
    if (state.pane === 'related') {
      const links = (rows, title) => rows.length ? `<p class="note">${title}</p>` + rows.map(r => `<button class="related" data-related="${esc(r.id)}"><small>${r.lineage_exact ? '' : '同批次候选 · '}${kindLabel(r)} · ${esc(day(r.source_start_ts || r.created_at))}${r.archive_state === 'cold' ? ' · 已归档' : ''}</small>${esc(r.content)}</button>`).join('') : '';
      el('detailPane').innerHTML = links(related, '引用这条记忆的摘要／事实') + links(originals, '这条摘要的原始记忆') || '<p class="note">没有找到关联记忆。</p>';
    }
  }
  async function loadSources(more = false) {
    const serial = ++sourceRequest, id = state.selected;
    if (!more) { state.sources = []; state.sourcePage = 0; }
    const pane = el('detailPane');
    if (!more) pane.innerHTML = '<p class="note">正在读取原文…</p>';
    else el('moreSources').disabled = true;
    try {
      const result = await request('GET', `${endpoint(id)}/sources?page=${state.sourcePage + 1}`);
      if (serial !== sourceRequest || state.pane !== 'source' || state.selected !== id) return;
      state.sources.push(...result.messages.map(m => ({...m, selected:true})));
      Object.assign(state, {sourcePage:result.page, sourceTotal:result.total, sourceExact:result.exact, sourceMore:result.has_more});
      renderSources();
    } catch (e) { if (serial === sourceRequest) pane.innerHTML = `<p class="dialog-error">${esc(e.message)}</p><button class="secondary" data-source-retry>重新加载原文</button>`; }
  }
  function renderSources() {
    const external = state.detail?.item.id.startsWith('extmem_');
    const editable = state.detail?.item.is_atom && !external;
    const outside = external ? `<details class="quote"><summary>外部平台来源记录</summary><p>${esc(state.detail.item.evidence_summary || '未保留原文')}</p></details>` : '';
    el('detailPane').innerHTML = outside + `<p class="note">${state.sourceExact ? '已关联的聊天原文' : '按时间范围／旧批次找到的候选原文，尚未确认逐条对应'} · ${state.sourceTotal} 条</p>` +
      state.sources.map((m,i) => `<div class="quote ${m.selected ? '' : 'unselected'}"><label class="source-check">${editable ? `<input type="checkbox" data-source-index="${i}" aria-label="保留这条原文" ${m.selected ? 'checked' : ''}>` : ''}<div><div class="quote-head"><span>${esc(m.name)}</span><time>${esc(stamp(m.created_at))}</time></div><p>${esc(m.content || '（附件消息）')}</p></div></label>${m.window_id ? `<a class="source-jump" href="${esc(sourceHref(m))}" target="_top">打开对话 ↗</a>` : ''}</div>`).join('') +
      (!state.sources.length && !external ? '<p class="note">没有关联的聊天原文；手动新增的记忆也可以独立保存。</p>' : '') +
      `<div class="source-toolbar">${state.sourceMore ? '<button id="moreSources">加载更多原文</button>' : ''}${editable && state.sources.length ? `<button id="saveSources" ${state.sourceMore ? 'disabled' : ''}>保存原文筛选</button>` : ''}</div>${editable && state.sourceMore ? '<p class="note">加载全部原文后可保存筛选，避免遗漏尚未显示的记录。</p>' : ''}`;
    if (el('moreSources')) el('moreSources').onclick = () => loadSources(true);
    if (el('saveSources')) el('saveSources').onclick = async () => {
      const ids = state.sources.filter(m => m.selected).map(m => m.id), store = state.store, id = state.selected;
      if (!ids.length && !confirm('清除这条记忆的全部原文关联？聊天消息本身会保留。')) return;
      const button = el('saveSources'); button.disabled = true;
      try { await request('PUT', `${endpoint(id,store)}/sources`, {source_message_ids:ids}); notify('原文筛选已保存'); if (id === state.selected && store === state.store) loadSources(); }
      catch (e) { notify(e.message); button.disabled = false; }
    };
  }

  function syncDialogViewport() {
    const viewport = window.visualViewport;
    let top = viewport?.offsetTop || 0, bottom = top + (viewport?.height || innerHeight);
    try {
      if (window.parent !== window && window.frameElement) {
        const parentViewport = window.parent.visualViewport, frameTop = window.frameElement.getBoundingClientRect().top;
        const parentTop = parentViewport?.offsetTop || 0;
        top = Math.max(top, parentTop - frameTop);
        bottom = Math.min(bottom, parentTop + (parentViewport?.height || window.parent.innerHeight) - frameTop);
      }
    } catch {}
    el('dialog').style.setProperty('--dialog-top', `${top}px`);
    el('dialog').style.setProperty('--dialog-height', `${Math.max(0, bottom-top)}px`);
  }
  function modal(title, body, actions = '') {
    ++dialogRequest;
    initialDraft = null;
    el('dialogTitle').textContent = title; el('dialogBody').innerHTML = body + '<p id="dialogError" class="dialog-error" role="alert"></p>'; el('dialogActions').innerHTML = actions;
    syncDialogViewport();
    if (!el('dialog').open) el('dialog').showModal();
    el('dialogBody').scrollTop = 0;
    return dialogRequest;
  }
  function draftValues() { return JSON.stringify([...el('dialogBody').querySelectorAll('input,textarea,select')].map(input => input.type === 'checkbox' ? input.checked : input.value)); }
  function dismissDialog() {
    if (dialogBusy) return;
    if (initialDraft !== null && initialDraft !== draftValues() && !confirm('修改还没有保存，确定放弃修改吗？')) return;
    ++dialogRequest; el('dialog').close();
  }
  async function submitDialog(button, action) {
    if (dialogBusy) return;
    dialogBusy = true; el('closeDialog').disabled = true;
    const buttons = [...el('dialog').querySelectorAll('button')]; buttons.forEach(b => b.disabled = true);
    const label = button.textContent; button.textContent = '处理中…';
    el('dialogError').textContent = '';
    try { await action(); }
    catch (e) { el('dialogError').textContent = e.message || '操作失败，请重试'; }
    finally { dialogBusy = false; buttons.forEach(b => b.disabled = false); button.textContent = label; }
  }
  const cancelSave = '<button class="secondary" id="cancelEdit">取消</button><button class="primary" id="saveEdit">保存</button>';
  const relationNote = related => related.length ? `<details class="note"><summary>关联 ${related.length} 条记忆，修改不会自动重写它们</summary>${related.map(m => `<p>${m.lineage_exact ? '' : '旧批次候选：'}${esc(m.content)}</p>`).join('')}</details>` : '';
  async function editRecord(id = null) {
    const store = state.store;
    const ticket = modal(id ? '编辑记忆' : '新增记忆', '<p class="note">正在读取…</p>');
    try {
      const result = id ? await request('GET', endpoint(id,store)) : {item:null,related:[]};
      if (!el('dialog').open || ticket !== dialogRequest) return;
      const m = result.item;
      const originalDay = m ? day(m.source_start_ts || m.created_at) : day();
      modal(id ? '编辑记忆' : '新增记忆', `<label class="field">内容<textarea id="editText" maxlength="30000" placeholder="记录一件值得记住的事…">${esc(m?.content || '')}</textarea></label>
        <div class="form-grid"><label class="field">发生日期<input id="editDate" type="date" value="${originalDay}"></label><label class="field">分类<select id="editKind"><option value="daily">日常</option><option value="long_term" ${!m || m.memory_kind === 'long_term' ? 'selected' : ''}>长期重要</option><option value="friend" ${m?.memory_kind === 'friend' ? 'selected' : ''}>朋友记忆</option></select></label></div>
        <label class="field inline"><input id="editUnresolved" type="checkbox" ${(m ? m.unresolved : state.view === 'unresolved' || state.filter === 'unresolved') ? 'checked' : ''}>当前未完成（完成后取消勾选）</label>
        <label class="field">关键词<input id="editTags" maxlength="2000" value="${esc(keywords(m?.keywords).replaceAll(' · ', '，'))}" placeholder="用逗号分开"></label><details><summary class="note">更多设置</summary><div class="form-grid"><label class="field">重要程度<input id="editImportance" type="number" min="0" max="1" step="0.05" value="${m?.importance ?? .5}"></label></div><label class="field">补充线索<input id="editEvidence" value="${esc(m?.evidence_summary || '')}"></label></details>${relationNote(result.related)}`, cancelSave);
      el('cancelEdit').onclick = dismissDialog;
      initialDraft = draftValues();
      el('saveEdit').onclick = () => submitDialog(el('saveEdit'), async () => {
        const content = el('editText').value.trim();
        if (!content) throw new Error('先写一点记忆内容吧');
        const importance = Number(el('editImportance').value);
        if (!Number.isFinite(importance) || importance < 0 || importance > 1) throw new Error('重要程度请填写 0 到 1');
        const body = {content,memory_kind:el('editKind').value,keywords:el('editTags').value.trim(),importance,unresolved:el('editUnresolved').checked,evidence_summary:el('editEvidence').value};
        // Opening and saving an existing range must not collapse it to midnight.
        if (el('editDate').value && (!id || el('editDate').value !== originalDay)) body.date = el('editDate').value;
        const saved = await request(id ? 'PUT' : 'POST', id ? endpoint(id,store) : base(store), body);
        el('dialog').close();
        notify(saved.embedding_ready === false ? '记忆已保存；向量暂未生成，可稍后重建索引' : '记忆已保存');
        if (store === state.store) { await loadList({keepPosition:true}); if (id && state.items.some(m => m.id === id)) openDetail(id); }
      });
      el('editText').focus();
    } catch (e) { if (el('dialog').open && ticket === dialogRequest) el('dialogError').textContent = e.message; }
  }
  async function deleteRecord(id) {
    const store = state.store;
    const ticket = modal('删除这条记忆？', '<p class="note">正在检查关联…</p>');
    try {
      const {item,related} = await request('GET', endpoint(id,store));
      if (!el('dialog').open || ticket !== dialogRequest) return;
      modal('删除这条记忆？', `<p class="detail-text">${esc(item.content)}</p><p class="note">删除后无法恢复，聊天原文会保留。</p>${related.length ? `<p class="dialog-error">${related.length} 条关联记忆将失去这条来源；它们的正文不会自动修改。</p>${relationNote(related)}` : ''}`, '<button class="secondary" id="cancelDelete">取消</button><button class="primary" id="confirmDelete">确认删除</button>');
      el('cancelDelete').onclick = dismissDialog;
      el('confirmDelete').onclick = () => submitDialog(el('confirmDelete'), async () => {
        await request('DELETE', `${endpoint(id,store)}?confirm_related=${related.length > 0}`);
        el('dialog').close(); notify('记忆已删除，聊天原文已保留');
        if (store === state.store) { closeDetail(); await loadList({keepPosition:true}); }
      });
    } catch (e) { if (el('dialog').open && ticket === dialogRequest) el('dialogError').textContent = e.message; }
  }

  function navigate(url) {
    if (window.parent !== window && typeof window.parent.openSubPage === 'function') window.parent.openSubPage(url);
    else location.href = url;
  }
  function openTools() {
    const store = state.store;
    modal('记忆库工具', `<p class="note">当前：${esc(names[store])} 的记忆库</p>` +
      [['compression','整理记忆','查看压缩任务与历史'],['digest','总结新消息','使用当前记忆库的独立锚点'],['anchor','总结锚点','查看与调整总结起点'],['rebuild','重建向量索引','更换向量模型后使用']].map(([id,title,description]) => `<button class="tool-row" data-tool="${id}"><div>${title}<small>${description}</small></div><span>›</span></button>`).join(''));
    el('dialogBody').querySelectorAll('[data-tool]').forEach(button => button.onclick = async () => {
      const tool = button.dataset.tool;
      if (tool === 'compression') { navigate(`/memory-compression?target=${store}&return=${encodeURIComponent('/memory?' + new URLSearchParams({store,return:params.get('return') || '/'}))}`); return; }
      if (tool === 'anchor') {
        try {
          const result = await request('GET', `${base(store)}/maintenance/anchor`);
          if (!el('dialog').open) return;
          modal('总结锚点', `<p class="note">${esc(names[store])} · 当前锚点：${result.anchor_ts ? esc(stamp(result.anchor_ts)) : '尚未总结'}</p><label class="field">新的起始日期<input id="anchorDate" type="date"></label><p class="note">回退日期会让那之后的消息重新参与总结，可能产生重复记忆。只影响当前记忆库。</p>`, cancelSave);
          el('cancelEdit').onclick = dismissDialog;
          el('saveEdit').onclick = () => submitDialog(el('saveEdit'), async () => {
            if (!el('anchorDate').value) throw new Error('请选择日期');
            await request('PUT', `${base(store)}/maintenance/anchor`, {date:el('anchorDate').value});
            el('dialog').close(); notify('当前记忆库的锚点已更新');
          });
        } catch (e) { el('dialogError').textContent = e.message; }
        return;
      }
      const isDigest = tool === 'digest';
      modal(isDigest ? '总结新消息' : '重建向量索引', `<p class="note">${isDigest ? '将总结当前记忆库尚未总结的新消息，可能需要几分钟。' : '将用当前向量模型重建这个记忆库的索引，包含已归档记录。会消耗模型额度，可能需要较长时间。'}</p>`, '<button class="secondary" id="cancelTool">取消</button><button class="primary" id="runTool">开始</button>');
      el('cancelTool').onclick = dismissDialog;
      el('runTool').onclick = () => submitDialog(el('runTool'), async () => {
        const result = await request('POST', `${base(store)}/maintenance/${tool}`, undefined, 30*60*1000);
        if (result.ok === false) throw new Error(result.message || '操作未完成');
        const message = isDigest ? (result.message || '总结完成') : `索引重建完成：${result.success}/${result.total} 条成功`;
        el('dialog').close(); status(message); notify(message); loadList({keepPosition:true});
      });
    });
  }
  function resetView() { state.page = 1; closeDetail(); state.selected = null; state.detail = null; loadList(); }
  // The preview called the companion "second"; production uses the existing backend store key.
  document.querySelector('[data-store="second"]').dataset.store = 'chatroom';
  document.querySelectorAll('[data-store]').forEach(b => b.onclick = () => {
    if (state.store === b.dataset.store) return;
    state.store = b.dataset.store;
    params.set('store', state.store); history.replaceState(null, '', `${location.pathname}?${params}`);
    status(''); resetView();
  });
  document.querySelectorAll('[data-view]').forEach(b => b.onclick = () => { state.view = b.dataset.view; state.filter = 'all'; resetView(); });
  el('chips').onclick = e => { const b = e.target.closest('[data-filter]'); if (b) { state.filter = b.dataset.filter; resetView(); } };
  el('search').maxLength = 200;
  el('search').oninput = () => { clearTimeout(searchTimer); ++listRequest; searchTimer = setTimeout(resetView, 250); };
  el('filter').onclick = () => { el('filters').hidden = !el('filters').hidden; el('filter').setAttribute('aria-expanded', !el('filters').hidden); };
  ['from','to'].forEach(id => el(id).onchange = resetView);
  el('reset').onclick = () => { el('from').value = el('to').value = ''; resetView(); };
  el('prev').onclick = () => { state.page--; loadList(); };
  el('next').onclick = () => { state.page++; loadList(); };
  el('pageJump').onsubmit = e => { e.preventDefault(); if (!el('jump').disabled) jumpToPage(); };
  el('list').onclick = e => {
    if (e.target.closest('[data-retry]')) { loadList(); return; }
    const row = e.target.closest('[data-id]'); if (!row) return;
    const action = e.target.closest('[data-action]')?.dataset.action;
    if (action === 'edit') editRecord(row.dataset.id);
    else if (action === 'delete') deleteRecord(row.dataset.id);
    else openDetail(row.dataset.id);
  };
  el('detailBody').onclick = e => {
    state.detailReading = true;
    const related = e.target.closest('[data-related]');
    if (related) { openDetail(related.dataset.related); return; }
    if (e.target.closest('[data-source-retry]')) { loadSources(); return; }
    const tab = e.target.closest('[data-pane]');
    if (tab) { state.pane = tab.dataset.pane; ++sourceRequest; renderDetail(); if (state.pane === 'source') loadSources(); }
  };
  el('detailBody').onchange = e => {
    state.detailReading = true;
    if (e.target.matches('[data-source-index]')) {
      state.sources[Number(e.target.dataset.sourceIndex)].selected = e.target.checked;
      e.target.closest('.quote').classList.toggle('unselected', !e.target.checked);
    }
  };
  el('edit').onclick = () => state.selected && editRecord(state.selected);
  el('delete').onclick = () => state.selected && deleteRecord(state.selected);
  el('add').onclick = () => editRecord();
  el('tools').onclick = openTools;
  el('closeDetail').onclick = el('scrim').onclick = closeDetail;
  el('back').onclick = () => {
    if (el('detail').classList.contains('open') && innerWidth < 750) closeDetail();
    else navigateSubPageBack();
  };
  el('closeDialog').onclick = dismissDialog;
  el('dialog').addEventListener('cancel', e => { e.preventDefault(); dismissDialog(); });
  el('dialog').addEventListener('close', () => { if (refreshPending) loadList({keepPosition:true}); });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !el('dialog').open) closeDetail();
    if (e.key === 'Tab' && innerWidth < 750 && el('detail').classList.contains('open') && !el('dialog').open) {
      const focusable = [...el('detail').querySelectorAll('button:not(:disabled),input:not(:disabled)')].filter(b => b.offsetParent !== null);
      const first = focusable[0], last = focusable.at(-1);
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
    }
  });
  // Read display names from their existing settings rather than freezing personal names in UI code.
  Promise.allSettled([request('GET','/api/worldbook'),request('GET','/api/chatroom/config')]).then(([main,companion]) => {
    if (main.status === 'fulfilled') names.main = main.value.ai_name || names.main;
    if (companion.status === 'fulfilled') names.chatroom = companion.value.connor_name || names.chatroom;
    el('mainName').textContent = names.main; el('secondName').textContent = names.chatroom;
    document.querySelectorAll('.avatar').forEach((a,i) => a.textContent = (i ? names.chatroom : names.main).slice(0,1).toUpperCase());
  });
  const onRefresh = () => {
    if (refreshBlocked() && !el('dialog').open) el('status').innerHTML = '有记忆更新 <button class="secondary" id="refreshLibrary">刷新列表</button>';
    return loadList({quiet:true});
  };
  el('status').onclick = e => { if (e.target.id === 'refreshLibrary') { status(''); loadList({keepPosition:true}); } };
  connectRetainedPageWS(message => {
    if (['memory_added','memory_updated','memory_deleted','memory_collection_changed','sync_reset_required'].includes(message.type)) onRefresh();
  }, {reconcile:onRefresh});
  [window, window.visualViewport].filter(Boolean).forEach(target => {
    target.addEventListener('resize', syncDialogViewport); target.addEventListener('scroll', syncDialogViewport);
  });
  try { if (window.parent !== window) [window.parent, window.parent.visualViewport].filter(Boolean).forEach(target => {
    target.addEventListener('resize', syncDialogViewport); target.addEventListener('scroll', syncDialogViewport);
  }); } catch {}
  window.addEventListener('pageshow', e => { if (e.persisted) onRefresh(); });
  loadList();
})();

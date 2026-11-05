(() => {
  'use strict';
  const {request, esc} = window.KitchenCards;
  const $ = id => document.getElementById(id);
  const categories = {vegetable:['蔬菜菌菇','🥬'], protein:['肉蛋豆与海鲜','🥚'], staple:['主食杂粮','🌽'], dairy:['奶与奶制品','🥛'], fruit:['水果','🍎'], other:['其他食材','🫙']};
  const storages = {fridge:'冷藏', freezer:'冷冻', pantry:'常温'};
  const localDate = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
  const today = () => localDate(new Date());
  let data = null, selectedDate = today(), storage = '', currentTab = 'today', menuView = '';
  let editing = null, generating = false, preferencesDirty = false, loadVersion = 0, toastTimer;

  function toast(text) {
    $('khToast').textContent = text;
    $('khToast').classList.add('is-visible');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => $('khToast').classList.remove('is-visible'), 2300);
  }
  function setTab(tab) {
    if (!['today','stock','preferences','pending'].includes(tab)) tab = 'today';
    currentTab = tab;
    const ids = {today:'panelToday', stock:'panelStock', preferences:'panelPreferences', pending:'panelPending'};
    document.querySelectorAll('[data-tab]').forEach(b => b.setAttribute('aria-selected', String(b.dataset.tab === tab)));
    for (const [key, id] of Object.entries(ids)) $(id).hidden = key !== tab;
    history.replaceState(null, '', location.pathname + '#' + tab);
  }
  function itemAmount(item) {
    const amount = String(item.amount ?? '少量');
    if (amount === '待估') return '余量待估';
    return amount + (item.unit && !amount.endsWith(item.unit) ? item.unit : '');
  }
  function icon(item) {
    const special = [['黄瓜','🥒'],['西兰花','🥦'],['青椒','🫑'],['虾','🦐'],['鱼','🐟'],['鸡蛋','🥚'],['玉米','🌽'],['蘑菇','🍄'],['胡萝卜','🥕']];
    return special.find(([word]) => item.name.includes(word))?.[1] || (categories[item.category] || categories.other)[1];
  }
  function renderWeek() {
    const now = new Date();
    const days = Array.from({length:7}, (_, i) => { const d = new Date(now); d.setDate(now.getDate() - (6-i)); return d; });
    if (!days.some(d => localDate(d) === selectedDate)) selectedDate = today();
    $('weekStrip').innerHTML = days.map(d => {
      const key = localDate(d), active = key === selectedDate;
      const label = key === today() ? '今天' : ['周日','周一','周二','周三','周四','周五','周六'][d.getDay()];
      return `<button type="button" class="kh-day ${active ? 'is-selected' : ''} ${data.menus.some(m => m.date === key) ? 'has-menu' : ''}" data-date="${key}" aria-pressed="${active}" aria-label="${d.getMonth()+1}月${d.getDate()}日${label}"><span>${label}</span><strong>${d.getDate()}</strong></button>`;
    }).join('');
  }
  function renderMenu() {
    const isToday = selectedDate === today();
    $('selectedDateTitle').textContent = isToday ? '今天的餐桌' : `${Number(selectedDate.slice(5,7))}月${Number(selectedDate.slice(8))}日的餐桌`;
    const menu = data.menus.find(m => m.date === selectedDate);
    const latest = new Map();
    for (const candidate of data.proposals || []) {
      if (candidate.date === selectedDate && !latest.has(candidate.actor)) latest.set(candidate.actor,candidate);
    }
    const views = [
      ...(menu ? [{key:'selected', label:'已选', item:menu}] : []),
      ...[...latest.values()]
        .map(p => ({key:p.actor, label:data.names?.[p.actor] || 'AI', item:p})),
    ];
    const active = views.find(v => v.key === menuView) || views[0];
    menuView = active?.key || '';
    $('menuViews').innerHTML = views.map(v => `<button type="button" data-view="${esc(v.key)}" aria-pressed="${v === active}">${esc(v.label)}${v.key === 'selected' ? '' : '的提案'}</button>`).join('');
    const area = $('menuArea');
    const previous = area.querySelector('.kitchen-card');
    const open = new Set(previous && previous.dataset.kitchenMenu === active?.item.id
      ? [...previous.querySelectorAll('details[open]')].map(d => d.dataset.meal) : []);
    if (active) {
      area.innerHTML = `<div class="kitchen-card" data-kitchen-menu="${esc(active.item.id)}" data-kitchen-ready="true">${window.KitchenCards.menuHTML({...active.item,ai_enabled:data.enabled})}</div>`;
      area.querySelectorAll('details').forEach(d => { d.open = open.has(d.dataset.meal); });
    } else {
      area.innerHTML = `<p class="kh-table-empty">${isToday ? '今天还没有菜单' : '这天没有菜单'}</p>`;
    }
    $('planControls').hidden = !isToday;
    const hasStock = data.items.some(i => i.quantity === null || i.quantity > 0);
    $('planButton').disabled = !data.enabled || generating || !hasStock;
    $('planActor').disabled = generating || !data.enabled;
    $('planButton').textContent = generating ? '正在想菜单…' : '安排菜单';
    if (!generating) $('planStatus').textContent = !isToday ? '' : !data.enabled ? '打开厨房 AI 可安排菜单。' : !hasStock ? '先在冰箱清单记几样食材。' : '';
  }
  function renderStock() {
    const query = $('stockSearch').value.trim().toLowerCase();
    const list = data.items.filter(i => (!storage || i.storage === storage) && (!query || (i.name + i.note).toLowerCase().includes(query)));
    if (!list.length) {
      $('stockArea').innerHTML = `<p class="kh-table-empty">${data.items.length ? '没有匹配的食材' : '冰箱清单是空的'}</p>`;
      return;
    }
    $('stockArea').innerHTML = Object.entries(categories).map(([key, [label]]) => {
      const items = list.filter(i => i.category === key);
      if (!items.length) return '';
      return `<section class="kh-stock-group"><h3 class="kh-group-title">${label}<span>${items.length} 样</span></h3><div class="kh-item-grid">${items.map(i => `<button type="button" class="kh-item ${i.quantity === 0 ? 'is-empty' : ''}" data-edit-item="${esc(i.id)}" aria-label="修改${esc(i.name)}，剩余${esc(itemAmount(i))}"><span class="kh-item-emoji" aria-hidden="true">${icon(i)}</span><span class="kh-item-body"><span class="kh-item-name">${esc(i.name)}</span><span class="kh-item-meta">${storages[i.storage] || '冷藏'}${i.uncertain ? ' · 待确认' : i.soon ? ' · 想尽快吃' : ''}</span></span><span class="kh-item-amount">${i.quantity === 0 ? '用完啦' : esc(itemAmount(i))}</span></button>`).join('')}</div></section>`;
    }).join('');
  }
  function renderPending() {
    $('pendingDot').hidden = !data.pending.length;
    $('pendingCount').textContent = data.pending.length || '';
    if (!data.pending.length) {
      $('pendingArea').innerHTML = '<p class="kh-table-empty">没有待确认的记录</p>';
    } else $('pendingArea').innerHTML = data.pending.map(p => {
      const change = JSON.parse(p.payload || '{}');
      const action = ['add','set','empty'].includes(change.op) ? (change.op === 'add' ? 'add' : 'set') : 'consume';
      const candidates = data.items.filter(i => i.name === p.name);
      const matches = candidates.filter(i => (!change.storage || i.storage === change.storage) && (!change.unit || i.unit === change.unit));
      const item = matches.length === 1 ? matches[0] : candidates.length === 1 ? candidates[0] : null;
      const unit = change.unit || item?.unit || '';
      const options = data.items.map(i => `<option value="${esc(i.id)}" ${i.id === item?.id ? 'selected' : ''}>${esc(i.name)} · ${esc(storages[i.storage] || '')} · ${esc(itemAmount(i))}</option>`).join('');
      const description = p.description === '食材或用量不够明确，请确认当前余量' ? '请确认这次的数量' : p.description;
      return `<article class="kh-pending-card"><h3>${esc(p.name)}</h3><p>${esc(description)}</p>
        <form class="kh-pending-form" data-pending-id="${esc(p.id)}">
          <label>对应食材<select name="item_id"><option value="">手填食材</option>${options}</select></label>
          <label>这次变动<select name="action">${[['consume','用掉'],['add','新增'],['set','校正余量']].map(([k,l]) => `<option value="${k}" ${k === action ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
          <label class="kh-span-two" ${item ? 'hidden' : ''}>食材名称<input name="name" ${item ? 'disabled' : 'required'} value="${esc(p.name === '这次用餐' ? '' : p.name)}"></label>
          <label>数量<input required name="amount" placeholder="如 2、半" value="${esc(change.op === 'empty' ? 0 : change.amount ?? '')}"></label>
          <label>单位<input name="unit" placeholder="节、袋…" value="${esc(unit)}"></label>
          <div class="kh-pending-actions"><button class="kh-primary" type="submit">记下这笔</button><button type="button" class="kh-pending-ignore" data-ignore-pending="${esc(p.id)}">忽略</button></div>
        </form></article>`;
    }).join('');
    if (data.errors.length) $('pendingArea').insertAdjacentHTML('beforeend', '<p class="kh-section-note">最近有食材记录没能完成，请顺手核对一下冰箱清单。</p>');
  }
  function renderPreferences() {
    if (preferencesDirty) return;
    const form = $('preferencesForm');
    for (const [key,value] of Object.entries(data.preferences)) {
      const field = form.elements.namedItem(key);
      if (!field) continue;
      if (field.type === 'checkbox') field.checked = Boolean(value); else field.value = value;
    }
  }
  async function refresh() {
    const version = ++loadVersion;
    try {
      const result = await request('');
      if (version !== loadVersion) return;
      data = result;
      $('pageError').hidden = true;
      $('aiEnabled').checked = data.enabled;
      if (data.names) {
        for (const option of $('planActor').options) option.textContent = data.names[option.value] || 'AI';
        window.KitchenCards.names = data.names;
      }
      renderWeek(); renderMenu(); renderStock(); renderPending(); renderPreferences();
    } catch (error) {
      $('pageError').textContent = error.message + '，稍后可以刷新再试。'; $('pageError').hidden = false;
    } finally { $('loadingState').hidden = true; }
  }
  function openItem(item = null) {
    editing = item;
    const form = $('itemForm'); form.reset();
    for (const key of ['id','name','amount','unit','category','storage','note']) {
      const field = form.elements.namedItem(key);
      field.value = item?.[key] ?? ({category:'vegetable',storage:'fridge'}[key] || '');
    }
    form.elements.namedItem('soon').checked = Boolean(item?.soon);
    $('itemDialogTitle').textContent = item ? '更新这份食材' : '记一样食材';
    $('itemError').textContent = '';
    $('deleteItemButton').hidden = !item;
    form.querySelector('[type="submit"]').textContent = item ? '保存现在的余量' : '放进冰箱清单';
    $('itemDialog').showModal();
  }
  document.querySelectorAll('[data-tab]').forEach(b => b.addEventListener('click', () => setTab(b.dataset.tab)));
  document.querySelectorAll('[data-close-dialog]').forEach(b => b.addEventListener('click', () => b.closest('dialog').close()));
  document.querySelectorAll('dialog').forEach(d => d.addEventListener('click', event => { if (event.target === d) { const r=d.getBoundingClientRect(); if (event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom) d.close(); } }));
  $('backButton').addEventListener('click', () => { if (typeof goBack === 'function') goBack(); else location.href='/'; });
  $('addButton').addEventListener('click', () => openItem());
  $('batchButton').addEventListener('click', () => { $('batchForm').reset(); $('batchError').textContent=''; $('batchDialog').showModal(); });
  $('stockSearch').addEventListener('input', () => { if (data) renderStock(); });
  document.querySelectorAll('[data-storage]').forEach(button => button.addEventListener('click', () => {
    storage = button.dataset.storage; document.querySelectorAll('[data-storage]').forEach(b => b.classList.toggle('is-active', b === button)); if (data) renderStock();
  }));
  $('menuViews').addEventListener('click', event => {
    const button = event.target.closest('[data-view]');
    if (button) { menuView=button.dataset.view; renderMenu(); $('menuViews').querySelector('[aria-pressed="true"]')?.focus(); }
  });
  $('weekStrip').addEventListener('click', event => { const b=event.target.closest('[data-date]'); if (b) { selectedDate=b.dataset.date; menuView=''; renderWeek(); renderMenu(); } });
  document.addEventListener('click', async event => {
    const editButton = event.target.closest('[data-edit-item]');
    if (editButton) openItem(data.items.find(i => i.id === editButton.dataset.editItem));
    const ignore = event.target.closest('[data-ignore-pending]');
    if (ignore) {
      ignore.disabled = true;
      try { await request('/pending/' + encodeURIComponent(ignore.dataset.ignorePending), {method:'POST', body:JSON.stringify({action:'ignore'})}); await refresh(); toast('这条已忽略'); }
      catch (error) { toast(error.message); } finally { ignore.disabled=false; }
    }
  });
  $('aiEnabled').addEventListener('change', async event => {
    const field = event.target; field.disabled=true;
    const previous = Boolean(data?.enabled);
    if (data) { data.enabled=field.checked; renderMenu(); }
    try { await request('/enabled', {method:'PUT',body:JSON.stringify({enabled:field.checked})}); await refresh(); toast(field.checked ? '厨房 AI 已开启' : '厨房 AI 已休息'); }
    catch (error) { if (data) { data.enabled=previous; renderMenu(); } field.checked=previous; toast(error.message); } finally { field.disabled=false; }
  });
  $('planButton').addEventListener('click', async () => {
    if (!data?.enabled || generating) return;
    generating=true; renderMenu();
    const actor = $('planActor').value;
    $('planStatus').textContent = `${data.names?.[actor] || 'AI'}正在查看冰箱…`;
    let errorText = '';
    try { await request('/plan', {method:'POST',body:JSON.stringify({actor})}); menuView=actor; await refresh(); $('planStatus').textContent=''; toast('菜单提案好啦，喜欢再确认'); }
    catch (error) { errorText=error.message; }
    finally { generating=false; if (data) renderMenu(); if (errorText) $('planStatus').textContent=errorText; }
  });
  $('itemForm').addEventListener('submit', async event => {
    event.preventDefault(); const form=event.target, button=form.querySelector('[type="submit"]'); button.disabled=true;
    const payload=Object.fromEntries(new FormData(form)); payload.soon=form.elements.namedItem('soon').checked;
    try { await request('/items', {method:'POST',body:JSON.stringify(payload)}); $('itemDialog').close(); await refresh(); toast('冰箱清单记好啦'); }
    catch (error) { $('itemError').textContent=error.message; } finally { button.disabled=false; }
  });
  $('deleteItemButton').addEventListener('click', async () => {
    if (!editing) return;
    const button=$('deleteItemButton'); button.disabled=true;
    try { await request('/items/' + encodeURIComponent(editing.id), {method:'DELETE'}); $('itemDialog').close(); await refresh(); toast('已从清单移除'); }
    catch (error) { $('itemError').textContent=error.message; } finally { button.disabled=false; }
  });
  $('batchForm').addEventListener('submit', async event => {
    event.preventDefault(); const form=event.target, button=form.querySelector('[type="submit"]'); button.disabled=true;
    try { await request('/batch', {method:'POST',body:JSON.stringify({text:new FormData(form).get('text')})}); $('batchDialog').close(); await refresh(); toast('这批食材记好啦'); }
    catch (error) { $('batchError').textContent=error.message; } finally { button.disabled=false; }
  });
  $('preferencesForm').addEventListener('input', () => { preferencesDirty=true; });
  $('preferencesForm').addEventListener('submit', async event => {
    event.preventDefault(); const form=event.target, button=form.querySelector('[type="submit"]'); button.disabled=true;
    const payload=Object.fromEntries(new FormData(form)); payload.use_only_stock=form.elements.namedItem('use_only_stock').checked;
    try { await request('/preferences', {method:'PUT',body:JSON.stringify(payload)}); preferencesDirty=false; await refresh(); toast('你的口味，我们记住啦'); }
    catch (error) { toast(error.message); } finally { button.disabled=false; }
  });
  $('pendingArea').addEventListener('change', event => {
    const field = event.target;
    if (field.name !== 'item_id') return;
    const name = field.form.elements.namedItem('name');
    name.disabled = name.closest('label').hidden = Boolean(field.value);
    name.required = !field.value;
    const unit = field.form.elements.namedItem('unit');
    if (!unit.value) unit.value = data.items.find(i => i.id === field.value)?.unit || '';
  });
  $('pendingArea').addEventListener('submit', async event => {
    const form=event.target.closest('[data-pending-id]'); if (!form) return;
    event.preventDefault(); const button=form.querySelector('[type="submit"]'); button.disabled=true;
    try { await request('/pending/' + encodeURIComponent(form.dataset.pendingId), {method:'POST',body:JSON.stringify(Object.fromEntries(new FormData(form)))}); await refresh(); toast('这笔已记好'); }
    catch (error) { toast(error.message); } finally { button.disabled=false; }
  });
  let eventTimer;
  function refreshSoon() { clearTimeout(eventTimer); eventTimer=setTimeout(refresh,100); }
  if (typeof connectCommonWS === 'function') connectCommonWS(event => { if (['kitchen_changed','sync_reset_required','capability_config_changed'].includes(event.type)) { refreshSoon(); window.KitchenCards.refresh(); } });
  window.addEventListener('kitchen:changed', refreshSoon);
  window.addEventListener('pageshow', refreshSoon);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshSoon(); });
  window.addEventListener('storage', event => { if (event.key === 'aion_chat_theme') document.documentElement.dataset.theme=event.newValue||'light'; });
  setTab(location.hash.slice(1) || 'today');
  refresh();
})();

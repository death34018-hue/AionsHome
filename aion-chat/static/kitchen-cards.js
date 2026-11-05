/* Chat and kitchen read the same menu record. */
(() => {
  'use strict';
  const cache = new Map(), busy = new Set();
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const today = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; };
  async function request(path, options = {}) {
    const response = await fetch('/api/kitchen' + path, {cache:'no-store', ...options,
      headers: {'Content-Type':'application/json', ...options.headers}});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { const error = new Error(data.detail || '暂时没有连接上小厨房'); error.status = response.status; throw error; }
    return data;
  }
  function amount(item) {
    let value = item.amount === .5 ? '半' : String(item.amount ?? '少量');
    const unit = item.unit || '';
    return value + (unit && !value.endsWith(unit) ? unit : '');
  }
  function servingIcon(meal) {
    const shape = meal === 'lunch'
      ? '<circle cx="12" cy="12" r="4"/><path d="M12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2"/>'
      : '<path d="M19.9 14.3A8.3 8.3 0 0 1 9.7 4.1a8.4 8.4 0 1 0 10.2 10.2Z"/><path d="M17.3 3.5v4m-2-2h4"/>';
    return `<svg class="kh-serving-icon" aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">${shape}</svg>`;
  }
  function menuHTML(menu, {actions = true} = {}) {
    const proposed = menu.kind === 'proposal' || (!menu.kind && menu.status === 'proposed');
    const onTable = !proposed && document.body.classList.contains('kh-page');
    const canChoose = actions && menu.date === today();
    const slots = proposed ? menu.table_slots || {} : Object.fromEntries(menu.meals.map(m => [m.meal, {
      name:m.name, proposal_id:m.proposal_id, source_meal:m.source_meal || m.meal, confirmed_at:m.confirmed_at,
    }]));
    const names = menu.names || window.KitchenCards?.names || {};
    const author = menu.actor_name || names[menu.actor] || 'AI';
    const title = proposed ? `${author}的提案` : menu.date === today() ? '今日菜单 · 已确认' : menu.date.slice(5).replace('-',' / ');
    const mealLabel = key => key === 'lunch' ? '午餐' : '晚餐';
    const sources = proposed ? [menu] : menu.meals;
    const letters = sources.filter((m, index) => m.message && sources.findIndex(other => other.actor === m.actor && other.message === m.message) === index);
    function letterHTML(source) {
      const signature = `<span>${esc(names[source.actor] || (source.actor === menu.actor ? author : 'AI'))}</span> `;
      const msg = source.message;
      return msg.length <= 64 ? `<div class="kc-letter">${signature}${esc(msg)}</div>` :
        `<details class="kc-letter" data-meal="letter-${esc(source.meal || source.actor)}"><summary>${signature}${esc(msg.slice(0,32))}…</summary>${esc(msg.slice(32))}</details>`;
    }
    return `<div class="kc-head"><strong>🍽️ ${esc(title)}</strong><a class="kc-link" href="/kitchen">去小厨房 ↗</a></div>` +
      menu.meals.map(meal => {
        const key = meal.meal, menuId = meal.source_id || menu.id;
        const label = proposed ? (key === 'lunch' ? '备选一' : '备选二') : mealLabel(key);
        const optionId = proposed ? menuId : meal.proposal_id, optionKey = proposed ? key : meal.source_meal || key;
        const picked = Object.keys(slots).filter(k => slots[k].proposal_id === optionId && slots[k].source_meal === optionKey);
        const detail = proposed ? (picked.length ? ' · 已选'+picked.map(mealLabel).join('、') : '') : ' · '+esc(names[meal.actor] || author);
        const choice = canChoose ? `<button type="button" class="kc-confirm" data-kitchen-action="choose" data-menu-id="${esc(menuId)}" data-meal="${esc(key)}" data-revision="${esc(meal.source_revision ?? menu.updated_at)}" data-slots="${esc(JSON.stringify(slots))}" data-picked="${esc(picked.join(','))}" data-on-table="${!proposed}" aria-label="为${esc(meal.name)}选餐">选餐</button>` : '';
        return `<section class="kc-meal${onTable ? ' kh-serving' : ''}" data-meal="${esc(key)}" data-actions="${Number(canChoose)}">
          <details class="kc-recipe" data-meal="${esc(key)}"><summary>
            <span class="kc-label">${onTable ? servingIcon(key) : ''}<span>${label}${detail}</span> <small>${esc(meal.minutes)} 分钟 · 做法 <span class="kc-open">⌄</span></small></span>
            <strong class="kc-name">${esc(meal.name)}</strong>
          </summary><div class="kc-method"><p class="kc-ingredients">${meal.ingredients.map(i => esc(i.name) + ' ' + esc(amount(i)) + (i.missing ? '（需补充）' : '')).join(' · ')}</p>
          <ol>${meal.steps.map(s => `<li>${esc(s)}</li>`).join('')}</ol>${meal.note ? `<p class="kc-note">${esc(meal.note)}</p>` : ''}</div></details>
          <div class="kc-row-actions">${choice}</div>
          ${onTable && letters.includes(meal) ? letterHTML(meal) : ''}
        </section>`;
      }).join('') +
      (onTable ? '' : letters.map(letterHTML).join('')) +
      '<div class="kc-action-status" aria-live="polite"></div>';
  }
  function render(attachments) {
    if (!Array.isArray(attachments)) return '';
    let html = '';
    for (const a of attachments) {
      if (a?.type === 'kitchen_menu') html += `<div class="kitchen-card" data-kitchen-menu="${esc(a.menu_id)}"><span class="kc-expired">正在打开菜单…</span></div>`;
      if (a?.type === 'kitchen_updated') html += '<a class="kc-updated" href="/kitchen">小厨房已更新</a>';
    }
    return html;
  }
  function lookupOrder(text, lookup) {
    const before = String(lookup.before || '').trim();
    if (!before) return lookup;
    // Saved replies can collapse paragraph breaks after the lookup was recorded.
    const pattern = before.split(/\s+/).map(part => part.replace(/[.*+?^$(){}|[\]\\]/g, '\\$&')).join('\\s*');
    let nearest = null;
    for (const match of text.matchAll(new RegExp(pattern,'g'))) {
      const offset = match.index + match[0].length;
      if (nearest === null || Math.abs(offset-lookup.offset) < Math.abs(nearest-lookup.offset)) nearest = offset;
    }
    return nearest === null ? lookup : {offset:nearest};
  }
  function contentParts(content, notices, attachments) {
    const text = String(content || '');
    const positioned = [...(notices || [])];
    const lookup = (attachments || []).find(a => a?.type === 'kitchen_lookup');
    if (lookup) positioned.push({message:{kitchenLookup:lookup},order:lookupOrder(text,lookup)});
    const parts = window.AionPat?.interleaveContent
      ? window.AionPat.interleaveContent(text, positioned)
      : [{type:'text',text}];
    return parts.map(p => p.type === 'notice' && p.message?.kitchenLookup
      ? {type:'kitchen_lookup',lookup:p.message.kitchenLookup} : p);
  }
  function lookupHTML(lookup, actorName) {
    const verb = {pending:'正在查看冰箱……',done:'查看了冰箱库存',failed:'暂时没能查看冰箱库存',stopped:'已停止查看冰箱'};
    return `<div class="kc-lookup" role="status" aria-live="polite" data-status="${esc(lookup.status)}"><span aria-hidden="true">&gt;</span><span>「${esc(actorName || lookup.actor_name || 'AI')}」${verb[lookup.status] || verb.failed}</span></div>`;
  }
  function load(id) {
    if (!cache.has(id)) cache.set(id, request('/menus/' + encodeURIComponent(id)));
    return cache.get(id);
  }
  async function hydrate(root = document) {
    const cards = [...(root.matches?.('.kitchen-card[data-kitchen-menu]') ? [root] : []), ...root.querySelectorAll('.kitchen-card[data-kitchen-menu]')];
    await Promise.all(cards.map(async card => {
      if (card.dataset.kitchenReady) return;
      card.dataset.kitchenReady = 'loading';
      const openMeals = [...card.querySelectorAll('details[open]')].map(d => d.dataset.meal);
      try {
        const menu = await load(card.dataset.kitchenMenu);
        card.innerHTML = menuHTML(menu);
        card.dataset.kitchenReady = 'true';
        card.querySelectorAll('details').forEach(d => { d.open = openMeals.includes(d.dataset.meal); });
      } catch (error) {
        card.dataset.kitchenReady = 'error';
        card.innerHTML = `<div class="kc-expired">${error.status === 410 ? '这份菜单已过期或移除，只保留近7天的菜单。' : '菜单暂时没打开。'}</div><div class="kc-actions" style="margin-top:10px"><a class="kc-link" href="/kitchen">去小厨房</a>${error.status === 410 ? '' : '<button type="button" data-kitchen-action="retry">再试一次</button>'}</div>`;
      }
    }));
  }
  async function refresh() {
    cache.clear();
    document.querySelectorAll('.kitchen-card[data-kitchen-menu]').forEach(c => delete c.dataset.kitchenReady);
    await hydrate(); refreshBadge();
    try { if (window.parent !== window) window.parent.KitchenCards?.refreshBadge(); } catch (_) {}
  }
  async function refreshBadge() {
    const badge = document.getElementById('kitchenBadge');
    if (!badge) return;
    try {
      const data = await request('/status');
      badge.style.display = data.pending_count ? 'block' : 'none';
      badge.closest('button')?.setAttribute('aria-label', data.pending_count ? `小厨房，${data.pending_count}项待确认` : '小厨房');
    } catch (_) { /* The launcher remains usable while reconnecting. */ }
  }
  function chooseServing(button, slots) {
    const dialog = document.createElement('dialog');
    dialog.className = 'kitchen-card kc-chooser';
    dialog.setAttribute('aria-label','选择用餐时间');
    const name = button.closest('.kc-meal').querySelector('.kc-name').textContent;
    const picked = button.dataset.picked.split(',');
    dialog.innerHTML = `<form method="dialog"><header><strong>${esc(name)}</strong><button value="" autofocus aria-label="关闭">×</button></header>` +
      [['lunch','午餐'],['dinner','晚餐']].map(([key,label]) => {
        const current = slots[key];
        return `<button class="kc-destination" value="${key}" ${picked.includes(key) ? 'disabled' : ''}><b>作${label}</b><span>${picked.includes(key) ? '已选这道菜' : current ? '替换「'+esc(current.name)+'」' : '尚未选菜'}</span></button>`;
      }).join('') + (button.dataset.onTable === 'true' ? `<button class="kc-drop" value="remove">移除这份${button.dataset.meal === 'lunch' ? '午餐' : '晚餐'}</button>` : '') + '</form>';
    document.body.appendChild(dialog);
    return new Promise(resolve => {
      dialog.addEventListener('close', () => { const result=dialog.returnValue; dialog.remove(); resolve(result); }, {once:true});
      dialog.showModal();
    });
  }
  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-kitchen-action]');
    if (!button) return;
    event.stopPropagation();
    const card = button.closest('.kitchen-card');
    if (button.dataset.kitchenAction === 'retry') {
      cache.delete(card.dataset.kitchenMenu); delete card.dataset.kitchenReady; hydrate(card); return;
    }
    if (button.dataset.kitchenAction !== 'choose') return;
    const slots = JSON.parse(button.dataset.slots);
    const choice = await chooseServing(button, slots);
    if (!choice) return;
    const key = button.dataset.menuId;
    if (busy.has(key)) return;
    busy.add(key); card.querySelectorAll('button').forEach(b => { b.disabled = true; });
    const label = card.querySelector('.kc-action-status');
    label.textContent = '正在记下你的选择…';
    try {
      if (choice === 'remove') {
        await request('/menus/' + encodeURIComponent(key) + '?meal=' + encodeURIComponent(button.dataset.meal) + '&expected_updated_at=' + encodeURIComponent(button.dataset.revision), {method:'DELETE'});
      } else {
        await request('/menus/' + encodeURIComponent(key) + '/confirm', {method:'POST',body:JSON.stringify({
          meal:button.dataset.meal,serve_as:choice,reselect:true,expected_updated_at:Number(button.dataset.revision),
          expected_selection_at:slots[choice]?.confirmed_at || 0,
        })});
      }
      await refresh(); window.dispatchEvent(new CustomEvent('kitchen:changed'));
    } catch (error) {
      if (error.status===409) { await refresh(); window.dispatchEvent(new CustomEvent('kitchen:changed')); }
      const status = card.querySelector('.kc-action-status'); if (status) status.textContent=error.message;
    }
    finally { busy.delete(key); card.querySelectorAll('button').forEach(b => { b.disabled = false; }); }
  });
  let scheduled = false;
  function schedule() {
    if (scheduled) return;
    scheduled = true; requestAnimationFrame(() => { scheduled = false; hydrate(); });
  }
  function boot() {
    hydrate(); refreshBadge();
    new MutationObserver(schedule).observe(document.body, {childList:true,subtree:true});
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, {once:true}); else boot();
  window.addEventListener('kitchen:changed', refresh);
  window.addEventListener('pageshow', refresh);
  window.KitchenCards = {render, hydrate, menuHTML, refresh, refreshBadge, request, esc, contentParts, lookupHTML};
})();

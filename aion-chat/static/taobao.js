(() => {
  const $ = id => document.getElementById(id);
  let state = null, filter = '', candidate = null, pollTimer = null, localBusy = false;
  let itemLimit = 24, tripsHaveMore = false;
  let linkedTrip = new URLSearchParams(location.search).get('trip');
  const returnTo = window.TaobaoCards.returnUrl(new URLSearchParams(location.search).get('return'));
  $('backLink').href = returnTo;
  $('backLink').setAttribute('aria-label', returnTo === '/' ? '返回首页' : '返回聊天');
  $('backLink').onclick = event => window.TaobaoCards.navigate(event, returnTo);
  window.handleTaobaoBack = () => {
    const dialog = document.querySelector('dialog[open]');
    if (dialog) dialog.close();
    else window.TaobaoCards.navigate(null, returnTo);
    return true;
  };
  const phaseName = {thinking:'在想逛什么',searching:'正在搜商品',selecting:'正在挑选',finished:'已结束',failed:'这次没完成',interrupted:'这次中断了'};
  const modeName = mode => mode === 'http' ? '淘宝官方 HTTP MCP' : 'MCP 原生桥接';
  const date = t => new Date(t * 1000).toLocaleString('zh-CN', {hour12: false});
  const node = (tag, className, text) => {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  };
  async function api(path, method = 'GET', body) {
    const response = await fetch('/api/taobao' + path, {method, headers: {'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请检查输入或稍后重试');
    return data;
  }
  function notice(text, error = false, id = 'notice') {
    $(id).textContent = text;
    $(id).classList.toggle('error', error);
  }
  function busy() {
    for (const id of ['roamButton', 'searchButton']) $(id).disabled = localBusy || !state || state.busy;
    $('roamButton').textContent = state?.busy ? '正在逛街…' : '自己去逛';
  }
  function options(select, names) {
    const value = select.value;
    select.replaceChildren(...Object.entries(names).map(([id, name]) => { const opt = node('option', '', name); opt.value = id; return opt; }));
    if (value in names) select.value = value;
  }
  function productLink(p, text) {
    const link = node('a', '', text);
    // Server verifies provenance; also restrict navigation at the rendering boundary.
    const url = new URL(p.url);
    if (url.protocol === 'https:' && ['item.taobao.com', 'detail.tmall.com'].includes(url.hostname)) link.href = url.href;
    link.target = '_blank'; link.rel = 'noopener noreferrer';
    return link;
  }
  function picture(p) {
    const fallback = node('div', 'picture-fallback', '商品图片暂不可用');
    if (p.image) {
      const img = node('img', 'product-picture'); img.src = p.image; img.alt = p.title;
      img.loading = 'lazy'; img.referrerPolicy = 'no-referrer';
      img.addEventListener('error', () => img.replaceWith(fallback), {once: true});
      return img;
    }
    return fallback;
  }
  function avatar(actor) {
    const img = node('img','avatar'); img.src = state.avatars?.[actor] || '/public/funIcon_0034_逛淘宝.png'; img.alt = '';
    img.onerror = () => {img.onerror = null; img.src = '/public/funIcon_0034_逛淘宝.png';};
    return img;
  }
  async function deleteSavedItem(item, button, closeDetail = false) {
    if (!confirm('只删除这件本地收藏，不会修改淘宝购物车。逛街历史中的当时小记会保留。确定删除吗？')) return;
    button.disabled = true;
    try {
      await api('/items/' + encodeURIComponent(item.id),'DELETE');
      if (closeDetail) $('detailDialog').close();
      await refresh(); notice('已移出本地收藏，淘宝购物车和逛街小记都没有改动。');
    } catch(e) { notice(e.message,true,closeDetail ? 'detailNotice' : 'notice'); button.disabled = false; }
  }
  async function deleteTripRecord(trip, button) {
    if (!confirm('只删除这篇本地逛街小记，不会移除收藏篮里的商品。确定删除吗？')) return;
    button.disabled = true;
    try {
      await api('/trips/' + encodeURIComponent(trip.id),'DELETE');
      $('detailDialog').close(); await refresh(); notice('这篇逛街小记已删除，收藏篮没有改动。');
    } catch(e) { notice(e.message,true,'detailNotice'); button.disabled = false; }
  }
  function card(p, saved = true, backTrip = null, deletable = false) {
    const article = node('article','product-card');
    const open = node('button','product-open'); open.type = 'button'; open.setAttribute('aria-label','查看商品：' + p.title);
    open.append(picture(p),node('h3','',p.title)); open.onclick = () => showProduct(p, saved, backTrip);
    const meta = node('div','card-meta'); meta.append(node('span','price',p.price ? '¥' + p.price : '暂无报价'));
    const picker = node('span','picker');
    if (p.actor) picker.append(avatar(p.actor));
    picker.append(node('span','', p.actor ? (state.names[p.actor] || 'AI') + ' 挑的' : '待挑选'));
    meta.append(picker); article.append(open,meta);
    if (deletable) {
      const del = node('button','card-delete','删除'); del.type = 'button';
      del.setAttribute('aria-label','删除收藏：' + p.title);
      del.onclick = event => {event.stopPropagation(); return deleteSavedItem(p,del);};
      article.append(del);
    }
    return article;
  }
  function showDetail(title) {
    $('detailTitle').textContent = title; $('detailBody').replaceChildren(); notice('',false,'detailNotice');
    if (!$('detailDialog').open) $('detailDialog').showModal();
  }
  function comment(actor,text,short = false) {
    const row = node('div','comment-row'); row.dataset.actor = actor;
    const content = node('div','comment-text'); content.append(node('strong','',state.names[actor] || 'AI'), node('p','bubble' + (short ? ' clamp' : ''),text));
    row.append(avatar(actor),content); return row;
  }
  function showProduct(p, saved, backTrip) {
    showDetail('为什么留下这件');
    const body = $('detailBody');
    if (backTrip) {const back = node('button','','‹ 返回这次逛街'); back.onclick = () => showTrip(backTrip); body.append(back);}
    const row = node('div','detail-product'), info = node('div');
    info.append(node('h3','',p.title),node('span','price',p.price ? '¥' + p.price : '暂无报价'));
    info.append(node('p','',p.shop || '店铺未提供'),node('p','',date(p.found_at) + ' 发现时快照，最终价格和规格以淘宝为准。'));
    if (p.recipient) info.append(node('p','','想给：' + p.recipient));
    if (p.purpose) info.append(node('p','','想用来：' + p.purpose));
    row.append(picture(p),info); body.append(row);
    if (p.reflection) body.append(comment(p.actor,p.reflection));
    else if (saved) body.append(node('p','empty','这件商品没有留下选品感想。'));
    const actions = node('div','detail-actions'), link = productLink(p,'去淘宝看商品 ↗'); link.className = 'detail-link'; actions.append(link);
    const current = state.items.find(x => x.actor === p.actor && x.item_id === p.item_id);
    if (current) {
      const del = node('button','','删除本地收藏');
      del.onclick = () => deleteSavedItem(current,del,true); actions.append(del);
    } else if (!saved) {
      const save = node('button','primary','加入收藏篮');
      save.onclick = () => {
        candidate = p; $('saveTitle').textContent = p.title; $('saveActor').value = $('actor').value;
        for (const id of ['recipient','purpose','reflection']) $(id).value = '';
        notice('',false,'saveNotice'); $('detailDialog').close(); $('saveDialog').showModal();
      }; actions.append(save);
    } else actions.append(node('p','muted','这是当时的选品记录；商品已不在收藏篮。'));
    body.append(actions);
  }
  function badges(trip) {
    const line = node('div','trip-badges');
    [state.names[trip.actor] || 'AI', date(trip.started_at), '搜到 ' + trip.candidate_count + ' 件',
     '选中 ' + trip.selected.length + ' 件', '未选点评 ' + trip.notes.length + ' 件', phaseName[trip.status] || trip.status]
      .forEach(text => line.append(node('span','',text)));
    return line;
  }
  function showTrip(trip) {
    showDetail('这次逛街的小记'); const body = $('detailBody');
    body.append(node('p','trip-motive',trip.motive || trip.keyword || '这次没有决定搜索什么'),badges(trip));
    if (trip.keyword) body.append(node('p','muted','搜索词：' + trip.keyword));
    if (trip.ended_at) body.append(node('p','muted','结束于 ' + date(trip.ended_at)));
    if (trip.error) body.append(node('p','error',trip.error));
    const selected = node('section','detail-section'); selected.append(node('h3','','选中的喜欢'));
    if (trip.selected.length) {const grid = node('div','product-grid'); grid.append(...trip.selected.map(p=>card(p,true,trip))); selected.append(grid);}
    else selected.append(node('p','empty',trip.status === 'finished' ? '这次没有选中商品。' : '目前没有记录到选中的商品。'));
    body.append(selected);
    const rejected = node('section','detail-section'); rejected.append(node('h3','','看过，但没带回来'));
    for (const p of trip.notes) {
      const row = node('article','note-product'), text = node('div');
      text.append(node('span','verdict',({pass:'不太合适',maybe:'先想想',unknown:'信息不足'})[p.verdict] || '没选中'),node('h4','',p.title),node('p','',p.comment),productLink(p,'去看原商品 ↗'));
      row.append(picture(p),text); rejected.append(row);
    }
    if (!trip.notes.length) rejected.append(node('p','empty','这次没有留下未选商品的点评，不代表其他商品都不喜欢。'));
    body.append(rejected);
    if (trip.summary) {const end = node('section','detail-section'); end.append(node('h3','','回来的时候'),comment(trip.actor,trip.summary)); body.append(end);}
    const actions = node('div','detail-actions');
    const del = node('button','trip-delete','删除这篇小记'); del.type = 'button';
    del.onclick = () => deleteTripRecord(trip,del); actions.append(del); body.append(actions);
  }
  function renderTrips() {
    const latest = state.trips?.[0]; $('latestTrip').replaceChildren();
    if (latest) {
      $('latestTrip').append(node('p','trip-motive clamp',latest.motive || latest.keyword || '这次暂时没想好逛什么'),badges(latest));
      const snippets = [latest.selected.find(p=>p.reflection)?.reflection, latest.notes[0]?.comment].filter(Boolean);
      if (!snippets.length && latest.summary) snippets.push(latest.summary);
      snippets.forEach(text=>$('latestTrip').append(comment(latest.actor,text)));
      if (latest.error) $('latestTrip').append(node('p','error clamp',latest.error));
      $('tripDetailButton').onclick = () => showTrip(latest);
    } else $('latestTrip').append(node('p','muted','从下一次闲逛开始，记录为什么出门、选了什么，以及没选的小吐槽。旧收藏仍在，不补写过去的经历。'));
    $('tripDetailButton').hidden = !latest;
    $('historyList').replaceChildren(...(state.trips || []).slice(1).map(trip=>{
      const card = node('button','history-card'); card.type = 'button'; card.dataset.actor = trip.actor;
      const copy = node('div','history-copy'); copy.append(node('span','muted',date(trip.started_at)),node('h3','clamp',trip.motive || trip.keyword || '一次没有出发的闲逛'));
      copy.append(node('span','muted',`${state.names[trip.actor]} / 选中 ${trip.selected.length} 件 / ${phaseName[trip.status] || trip.status}`));
      const quote = trip.summary || trip.notes[0]?.comment || trip.selected.find(p=>p.reflection)?.reflection;
      if (quote) copy.append(node('p','clamp',quote));
      card.append(copy,avatar(trip.actor)); card.onclick=()=>showTrip(trip); return card;
    }));
    if ((state.trips || []).length < 2) $('historyList').append(node('p','empty','不赶路，等下一篇逛街小记。'));
    $('moreTrips').hidden = !tripsHaveMore;
  }
  function render() {
    options($('actor'), state.names); options($('saveActor'), state.names);
    $('connectionLabel').textContent = `${modeName(state.settings.transport)} / 仅搜索与本地收藏`;
    $('filters').replaceChildren(...Object.entries({'': '全部', ...state.names}).map(([id, name]) => {
      const button = node('button', '', name); button.type = 'button'; button.setAttribute('aria-pressed', String(filter === id));
      button.onclick = () => {filter = id; render();}; return button;
    }));
    const items = state.items.filter(p => !filter || p.actor === filter);
    $('count').textContent = state.items.length;
    $('basketCount').textContent = items.length + ' 件';
    $('wishlist').replaceChildren(...items.slice(0,itemLimit).map(p => card(p, true, null, true)));
    $('moreItems').hidden = items.length <= itemLimit;
    $('empty').hidden = items.length > 0;
    $('recentProducts').replaceChildren(...state.items.slice(0,8).map(p=>card(p,true)));
    $('recentEmpty').hidden = state.items.length > 0;
    $('companionCards').replaceChildren(...['connor','aion'].map(actor=>{
      const card = node('div','companion'); card.dataset.actor = actor;
      const img = node('img','portrait'); img.src = state.portraits?.[actor] || state.avatars?.[actor]; img.alt = state.names[actor];
      img.onerror = () => {img.onerror=null; img.src=state.avatars?.[actor] || '/public/funIcon_0034_逛淘宝.png';};
      const active = state.active_trips?.find(t=>t.actor===actor);
      const text = node('div'); text.append(node('h2','',state.names[actor]),node('p','companion-status',active ? phaseName[active.phase] || '正在逛街' : '这会儿没在逛'));
      card.append(img,text); return card;
    }));
    renderTrips();
    busy();
  }
  async function refresh() {
    state = await api('/state'); tripsHaveMore = state.trips?.length === 20; render();
    if (linkedTrip) {
      const tripId = linkedTrip; linkedTrip = null;
      try {showTrip(await api('/trips/' + encodeURIComponent(tripId)));}
      catch(e) {showDetail('逛街记录'); notice(e.message,false,'detailNotice');}
    }
    clearTimeout(pollTimer);
    if (state.busy) {
      notice('正在逛淘宝，真实搜索和挑选可能需要几分钟。你可以离开页面，完成后会保存在收藏篮。');
      pollTimer = setTimeout(() => refresh().catch(e => notice(e.message, true)), 4000);
    } else if (state.last_run?.ok === false) notice(`${state.names[state.last_run.actor]}：${state.last_run.message}`, true);
    else notice('');
  }
  $('searchForm').addEventListener('submit', async e => {
    e.preventDefault(); localBusy = true; busy(); $('results').replaceChildren(); notice('正在通过 MCP 搜索真实商品…');
    try {
      const result = await api('/search', 'POST', {keyword: $('keyword').value.trim()});
      $('results').replaceChildren(...result.products.map(p => card(p, false)));
      notice(result.products.length ? `找到 ${result.products.length} 件可验证链接的商品。${result.skipped ? '另有 ' + result.skipped + ' 条缺少可验证链接，未展示。' : ''}` : '这次没有找到带可验证链接的商品，可以换个搜索词。');
    } catch (err) { notice(err.message, true); }
    finally {localBusy = false; busy();}
  });
  $('roamButton').onclick = async () => {
    localBusy = true; busy();
    try { await api('/roam', 'POST', {actor: $('actor').value}); await refresh(); }
    catch (e) {notice(e.message, true);} finally {localBusy = false; busy();}
  };
  $('refreshButton').onclick = () => refresh().catch(e => notice(e.message, true));
  $('closeDetail').onclick = () => $('detailDialog').close();
  function switchView(basket) {
    $('feedView').hidden = basket; $('basketView').hidden = !basket;
    $('feedTab').setAttribute('aria-pressed',String(!basket)); $('basketTab').setAttribute('aria-pressed',String(basket));
  }
  $('feedTab').onclick = () => switchView(false); $('basketTab').onclick = () => switchView(true);
  $('moreItems').onclick = () => {itemLimit += 24; render();};
  $('moreTrips').onclick = async () => {
    $('moreTrips').disabled = true;
    try {const more = await api('/trips?offset=' + state.trips.length); state.trips.push(...more.trips.filter(t=>!state.trips.some(x=>x.id===t.id))); tripsHaveMore=more.trips.length===20; renderTrips();}
    catch(e) {notice(e.message,true);} finally {$('moreTrips').disabled = false;}
  };
  $('saveForm').onsubmit = async e => {
    e.preventDefault(); $('confirmSave').disabled = true;
    try {
      await api('/items', 'POST', {actor: $('saveActor').value, candidate_id: candidate.id, recipient: $('recipient').value, purpose: $('purpose').value, reflection: $('reflection').value});
      $('saveDialog').close(); await refresh(); notice('已保存在独立收藏篮。同一角色已经收藏的商品不会重复添加。');
    } catch (err) {notice(err.message, true, 'saveNotice');} finally {$('confirmSave').disabled = false;}
  };
  $('cancelSave').onclick = () => $('saveDialog').close();
  $('settingsButton').onclick = () => {
    if (!state) return notice('页面尚未连接，请点击刷新重试。', true);
    $('transport').value = state.settings.transport; $('mcpUrl').value = state.settings.url;
    $('autonomyEnabled').checked = state.settings.autonomy_enabled; $('urlField').hidden = $('transport').value !== 'http';
    notice('', false, 'settingsNotice'); $('settingsDialog').showModal();
  };
  $('transport').onchange = () => {$('urlField').hidden = $('transport').value !== 'http';};
  $('closeSettings').onclick = () => $('settingsDialog').close();
  $('settingsForm').onsubmit = async e => {
    e.preventDefault();
    try {
      await api('/settings', 'PUT', {transport: $('transport').value, url: $('mcpUrl').value, autonomy_enabled: $('autonomyEnabled').checked});
      await refresh(); notice('已保存。可以检查连接，再做一次真实搜索。', false, 'settingsNotice');
    } catch (err) {notice(err.message, true, 'settingsNotice');}
  };
  $('checkButton').onclick = async () => {
    $('checkButton').disabled = true; notice('正在检查已保存的 MCP 连接…', false, 'settingsNotice');
    try {const r = await api('/check', 'POST'); notice(r.message, false, 'settingsNotice');}
    catch (e) {notice(e.message, true, 'settingsNotice');} finally {$('checkButton').disabled = false;}
  };
  busy(); refresh().catch(e => notice(e.message, true));
})();

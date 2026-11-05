(() => {
  'use strict';
  if (navigator.userAgent.includes('AionChatApp')) document.documentElement.classList.add('aion-app');
  function syncRoomChrome() {
    document.querySelector('meta[name="theme-color"]').content = '#11110f';
    window.AionStatusBar?.setBarStyle('dark');
  }
  window.addEventListener('aion-theme-applied', syncRoomChrome);
  syncRoomChrome();
  const $ = id => document.getElementById(id);
  const kind = location.pathname.includes('texas-holdem') ? 'texas-holdem' : 'liars-dice';
  const diceGame = kind === 'liars-dice';
  const title = diceGame ? '吹牛骰子' : '德州扑克';
  const base = `/api/table-games/${kind}`;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  let state = null, busy = false, timer = null, failed = false;
  let commandController = null, commandVersion = 0, busyPath = '';
  let wallets = {aion:0, connor:0};
  const amount = n => state?.wallet ? `¥${(n / 100).toFixed(2)}` : String(n);
  const signed = n => `${n > 0 ? '+' : n < 0 ? '−' : ''}${amount(Math.abs(n))}`;
  const inputAmount = n => state?.wallet ? (n / 100).toFixed(2) : n;
  let names = {user: '你', aion: '第一位伴侣', connor: '第二位伴侣'};
  const name = player => names[player] || player || '';
  const streets = {preflop: '翻牌前', flop: '翻牌', turn: '转牌', river: '河牌'};
  const pips = {1:[5], 2:[1,9], 3:[1,5,9], 4:[1,3,7,9], 5:[1,3,5,7,9], 6:[1,3,4,6,7,9]};
  function die(value) {
    if (!value) return '<span class="die unknown" aria-label="隐藏骰子">?</span>';
    return `<span class="die ${value === 1 ? 'one' : ''}" role="img" aria-label="${value} 点">${pips[value].map(pos => `<i class="pip" style="grid-area:${Math.ceil(pos / 3)}/${(pos - 1) % 3 + 1}"></i>`).join('')}</span>`;
  }
  function card(value, empty = false) {
    if (!value) return `<span class="card ${empty ? 'empty' : 'back-card'}" aria-label="${empty ? '未发公共牌' : '隐藏底牌'}"></span>`;
    const suit = value.slice(-1), rank = value.slice(0, -1), symbol = {S:'♠', H:'♥', C:'♣', D:'♦'}[suit];
    return `<span class="card ${['H','D'].includes(suit) ? 'red' : ''}" aria-label="${symbol}${rank}">${rank}<span class="suit">${symbol}</span></span>`;
  }
  async function api(path, body, controller = new AbortController()) {
    const timeout = setTimeout(() => controller.abort(), path === '/chat' ? 330000 : 255000);
    try {
      const response = await fetch(base + path, {cache:'no-store', signal:controller.signal,
        ...(body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {})});
      const data = await response.json();
      if (!response.ok) { const error = new Error(typeof data.detail === 'string' ? data.detail : '操作没有成功，请重试'); error.status = response.status; throw error; }
      return data;
    } finally { clearTimeout(timeout); }
  }
  async function accept(data, animate = false, showFinal = true) {
    const previous = state;
    state = data.state; names = {...names, ...data.names};
    wallets = data.wallets || wallets;
    $('walletInfo').textContent = `${names.aion} 钱包 ¥${(wallets.aion / 100).toFixed(2)} · ${names.connor} 钱包 ¥${(wallets.connor / 100).toFixed(2)}${!diceGame ? `；你的新桌额度 ¥${(Math.max(wallets.aion, wallets.connor) / 100).toFixed(2)}` : ''}。0 元可免费练习。`;
    document.body.classList.toggle('table-in-game', !!state);
    $('opponent').options[1].textContent = `和 ${names.aion} 双人玩`;
    $('opponent').options[2].textContent = `和 ${names.connor} 双人玩`;
    if (state) {
      $('opponent').value = state.players.length === 3 ? 'both' : state.players.find(p => p !== 'user');
      if (diceGame) $('wild').value = state.wild ? 'yes' : 'no';
    }
    if (animate) document.body.classList.add('table-presenting');
    render();
    tableCompanion.update(state, names);
    try { if (animate) await window.TableAtmosphere.present(previous, state, {die,card}); }
    finally { document.body.classList.remove('table-presenting'); }
    if (showFinal && state && (state.closed || state.phase === 'game_over') && (previous?.id !== state.id || (!previous.closed && previous.phase !== 'game_over'))) showSummary();
  }
  function showError(error) {
    failed = true;
    $('errorText').textContent = error.name === 'AbortError' ? '连接等待超时，刷新牌桌可查看是否已完成。' : error.message || '连接失败，请稍后重试';
    $('errorBox').hidden = false;
    $('retry').textContent = state?.phase === 'playing' && state.turn !== 'user' ? '重试这一回合' : '刷新牌桌';
  }
  function clearError() { failed = false; $('errorBox').hidden = true; }
  let refreshNoticeTimer;
  async function refresh(resume = true, manual = false) {
    if (busy) return;
    busy = true;
    if (manual) { $('refresh').textContent = '刷新中'; $('refresh').disabled = true; }
    try {
      clearError(); await accept(await api('/state'));
      if (manual) {
        clearTimeout(refreshNoticeTimer);
        $('refreshFeedback').textContent = '牌桌已同步 · 不会重新发牌或摇骰';
        $('refreshFeedback').hidden = false;
        refreshNoticeTimer = setTimeout(() => { $('refreshFeedback').hidden = true; }, 2500);
      }
    }
    catch (error) { showError(error); }
    finally { busy = false; $('refresh').textContent = '刷新'; render(); if (resume) schedule(); }
  }
  async function command(path, extra = {}) {
    const closing = path === '/cashout' || path === '/new';
    if (busy && !(closing && busyPath === '/ai-step')) return;
    commandController?.abort();
    const version = ++commandVersion;
    const controller = new AbortController();
    commandController = controller; busyPath = path;
    if (closing) window.TableAtmosphere?.cancel();
    clearTimeout(timer); clearError(); busy = true; render();
    try {
      if (closing && state && !state.closed && state.phase !== 'game_over') {
        await accept(await api('/state', undefined, controller), false, false);
        if (path === '/new' && !state.closed && state.phase !== 'game_over') {
          await accept(await api('/cashout', {game_id:state.id, revision:state.revision}, controller), false, false);
        }
      }
      const body = path === '/new' ? extra : {game_id:state.id, revision:state.revision, ...extra};
      const data = await api(path, body, controller);
      if (version !== commandVersion) return;
      if (path === '/new' || path === '/cashout') $('setup').close();
      await accept(data, path !== '/cashout');
      if (path === '/action') $('speech').value = '';
    } catch (error) {
      if (version !== commandVersion) return;
      if (error.status === 409) {
        try { await accept(await api('/state')); } catch (refreshError) { showError(refreshError); }
      } else { showError(error); }
    } finally {
      if (version === commandVersion) {
        busy = false; busyPath = ''; commandController = null; render(); schedule();
      }
    }
  }
  function schedule() {
    clearTimeout(timer);
    if (busy || failed || document.hidden || !state || state.phase !== 'playing' || state.turn === 'user') return;
    timer = setTimeout(() => state.busy ? refresh() : command('/ai-step'), state.busy ? 1800 : 700);
  }
  function seatHTML(player) {
    const seat = state.seats[player];
    const active = state.phase === 'playing' && state.turn === player;
    const avatar = `<span class="seat-portrait"><img class="seat-avatar" src="${{user:'/public/UserIcon.png', aion:'/public/gropicon1.png', connor:'/public/codexicon.png'}[player]}" alt="" loading="eager">${active ? `<span class="turn-badge">${failed ? '已暂停' : player === 'user' ? (busy ? '确认中' : '到你了') : '思考中'}</span>` : ''}</span>`;
    let meta, pieces;
    if (diceGame) {
      meta = seat.count ? `剩余 ${seat.count} 颗骰子` : '本局已淘汰';
      pieces = `<div class="dice-row">${(seat.dice || Array(seat.count).fill(null)).map(die).join('')}</div>`;
    } else {
      const role = [state.dealer === player ? '庄' : '', state.small_blind === player ? '小盲' : '', state.big_blind === player ? '大盲' : ''].filter(Boolean).join(' / ');
      meta = `<span class="seat-balance">${amount(seat.stack)}${state.wallet ? '' : ' 筹码'}</span><span class="seat-role">${role}${seat.folded ? ' · 弃牌' : !seat.in_hand ? ' · 已离桌' : seat.stack === 0 && state.phase === 'playing' ? ' · 全下' : ''}${seat.bet && state.phase === 'playing' ? ` · 已下 ${amount(seat.bet)}` : ''}</span>`;
      pieces = seat.in_hand ? `<div class="cards-row">${(seat.hand || [null,null]).map(c => card(c)).join('')}</div>` : '';

    }
    return `<article data-player="${player}" aria-label="${esc(name(player))}的座位" class="seat ${active ? 'active' : ''}">${avatar}<div class="seat-details">${player === 'user' ? '' : `<div class="seat-name">${esc(name(player))}</div>`}<div class="seat-meta">${meta}</div></div>${pieces}</article>`;
  }
  function button(label, action, css = '') { return `<button data-action="${action}" class="${css}" ${busy ? 'disabled' : ''}>${label}</button>`; }
  function render() {
    $('status').classList.toggle('turn-status', state?.phase === 'playing' && !failed);
    $('newGame').disabled = busy && busyPath !== '/ai-step';
    $('cashout').hidden = !state || state.closed || state.phase === 'game_over';
    $('cashout').disabled = busy && busyPath !== '/ai-step';
    $('cashout').textContent = '结束本场 · 保留已完成的输赢';
    $('refresh').disabled = busy;
    $('retry').disabled = busy;
    $('newGame').textContent = state ? '重新开一局' : '入座开局';
    if (!state) {
      $('roundLabel').textContent = '一起玩一局';
      $('opponents').innerHTML = ''; $('selfSeat').innerHTML = ''; clearBubbles();
      $('actionArea').innerHTML = button('设置并入座', 'settings', 'primary');
      $('centerpiece').innerHTML = `<span class="empty-table-label">${diceGame ? '等你摇盅' : '等你入座'}</span>`;
      $('tableCaption').textContent = '把好运带上桌，把心思藏好。';
      $('tableHint').textContent = diceGame ? '只数每个人杯里的骰子 · 桌中央没有额外骰子' : '每次发牌算一手 · 最多 3 手就收桌';
      $('status').textContent = busy ? '正在准备牌桌……' : '选好同伴，就可以开局了。';
      return;
    }
    const over = state.phase !== 'playing';
    $('roundLabel').textContent = `第 ${state.round}${state.round_limit ? ' / ' + state.round_limit : ''} ${diceGame ? '轮' : '手'}${state.closed ? ' · 已收桌' : ''}`;
    $('opponents').innerHTML = state.players.filter(p => p !== 'user').map(seatHTML).join('');
    $('selfSeat').innerHTML = seatHTML('user');
    syncBubbles();
    if (diceGame) {
      $('tableCaption').textContent = state.bid ? `${name(state.bid.player)} 说，全桌至少有` : `全桌 ${state.total_dice} 颗骰子 · 等一句开场`;
      $('centerpiece').innerHTML = state.bid ? `<strong class="bid-number">${state.bid.count}</strong><span class="bid-unit">个 ${state.bid.face} 点</span>` : '<span class="empty-table-label">等你叫数</span>';
      $('tableHint').textContent = state.phase === 'cancelled' ? '本场提前结束 · 不同步到群聊' : over ? `已经开骰 · 实际 ${state.result.actual} 个 ${state.result.bid.face} 点` :
        state.wild_active ? '1 点是百搭 · 叫过 1 后本轮失效' : state.wild ? '本轮叫过 1 · 现在只数实际点数' : '只数实际点数 · 1 点不百搭';
    } else {
      $('tableCaption').innerHTML = state.phase === 'cancelled' ? '本场已结束' : `${over ? '本手底池' : streets[state.street]} · <span class="pot-amount">${amount(state.pot)}</span>${state.wallet ? '' : ' 筹码'}`;
      $('centerpiece').innerHTML = `<div class="cards-row">${Array.from({length:5}, (_, i) => card(state.board[i], true)).join('')}</div>`;
      $('tableHint').textContent = state.phase === 'cancelled' ? (state.rounds?.length ? '已完成的输赢保留 · 收桌总结将同步到最近聊天的群' : '尚未完成一手 · 不生成群总结') : over ? (state.wallet?.status === 'open' ? '本手金额已结算在桌上 · 收桌时退回钱包' : '本手已结算') : '';
    }
    if (over) {
      renderResult();
    } else if (state.turn !== 'user') {
      $('status').textContent = `${name(state.turn)}${failed ? ' 的回合暂停了' : ' 正在想这一手……'}`;
      $('actionArea').innerHTML = `<p class="action-note">${state.seats.user.count === 0 || state.seats.user.in_hand === false ? '你可以继续观战，或在开局设置里重新开始。' : '看看对方这回会怎么说。'}</p>`;
    } else {
      $('status').textContent = busy ? '正在确认你的动作……' : '轮到你了。';
      renderActions();
    }
    $('history').innerHTML = state.history.length ? [...state.history].reverse().map(event => `<li><strong>${esc(name(event.player))}</strong><span class="event">第 ${event.round} 轮 · ${esc(event.text)}</span>${event.speech ? `<span class="talk">${esc(event.speech)}</span>` : ''}</li>`).join('') : '<li class="muted">第一句，等你开场。</li>';
  }
  function renderResult() {
    $('status').textContent = state.closed ? (state.end_reason || '已收桌') + (state.wallet ? ' · 余额已退回' : ' · 练习结束') : state.phase === 'game_over' ? `${name(state.winner)} 赢下了这局。` : diceGame ? '这一轮，真相揭晓。' : '这一手，金额落定。';
    if (state.phase === 'cancelled') {
      const reportHint = diceGame ? '本场不会同步到群聊。' : state.rounds?.length ? '收桌总结将同步到最近聊天的群。' : '尚未完成一手，不生成群总结。';
      $('actionArea').innerHTML = `<div class="settlement"><p>已完成的输赢保留，未完成的这一轮作废。<br>${reportHint}</p><div class="action-row">${button('重新开局', 'settings', 'primary')}${button('本场明细', 'summary')}</div></div>`;
      return;
    }
    let text;
    if (diceGame) {
      text = `<strong>${esc(name(state.result.loser))}</strong> 输一颗骰子${state.seats[state.result.loser].count === 0 ? '，本局淘汰' : ''}。<br>叫了 ${state.result.bid.count} 个 ${state.result.bid.face} 点，实际有 ${state.result.actual} 个。`;
      if (state.wallet) text += state.wallet.status === 'settled' ? '<br>' + state.players.map(p => `${esc(name(p))} ${signed(state.wallet.net[p])}`).join(' · ') : `<br><span class="muted">${state.wallet.status === 'refunded' ? '约定已取消，金额全额退回。' : `约定每位输家支付 ${amount(state.wallet.stake)}，整局结束后结算。`}</span>`;
      if (state.dice_mode === 'three_rounds') text = `<strong>${esc(name(state.result.winner))}</strong> 赢，${esc(name(state.result.loser))} 输。<br>${state.wallet ? `${esc(name(state.result.loser))} 支付 ${amount(state.wallet.stake)} 给 ${esc(name(state.result.winner))}` : '赢家 +1 分，输家 −1 分'}${state.players.length === 3 ? ' · 第三人不输不赢' : ''}`;
    } else {
      text = '<span class="round-results">' + state.players.map(p => `<span class="result-player"><span>${esc(name(p))}${state.result.hand_names[p] ? ` · ${state.result.hand_names[p]}` : ''}</span><span><strong>${signed(state.result.net[p])}</strong>${state.wallet ? ` · 累计 ${signed(state.wallet.net[p])}` : ''}</span></span>`).join('') + '</span>';
    }
    $('actionArea').innerHTML = `<div class="settlement"><p>${text}</p><div class="action-row">${state.phase === 'round_over' && !state.closed ? button(diceGame ? '重新摇骰 · 下一轮' : '继续发牌 · 下一手', 'next', 'primary') : button('再玩一场', 'settings', 'primary')}${button('本场明细', 'summary')}${state.wallet?.status === 'open' && !diceGame ? button('提前收桌', 'cashout') : ''}</div></div>`;
  }
  function renderActions() {
    const allowed = state.legal;
    if (diceGame) {
      const previousCount = $('bidCount')?.value, previousFace = $('bidFace')?.value;
      const counts = [...new Set((allowed.bids || []).map(b => b.count))];
      $('actionArea').innerHTML = `<div class="action-row">${counts.length ? `<label>全桌至少<select id="bidCount">${counts.map(n => `<option value="${n}">${n} 个</option>`).join('')}</select></label><label>这个点数<select id="bidFace"></select></label>${button('叫这个数', 'bid', 'primary')}` : ''}${allowed.actions.includes('challenge') ? button('不信，开骰！', 'challenge') : ''}</div><p class="action-note">数量要更多；数量相同，点数就要更大。</p>`;
      if (counts.length) {
        if (counts.includes(Number(previousCount))) $('bidCount').value = previousCount;
        updateFaces(previousFace);
        $('bidCount').onchange = () => updateFaces();
      }
    } else {
      const previous = $('raiseAmount')?.value;
      $('actionArea').innerHTML = `<div class="action-row">${allowed.actions.includes('check') ? button('过牌 · 不用付钱', 'check', 'primary') : ''}${allowed.actions.includes('call') ? button(`跟注 · 再付 ${amount(allowed.to_call)}`, 'call', 'primary') : ''}${allowed.actions.includes('all_in') ? button(`全下 ${amount(state.seats.user.stack)}`, 'all_in') : ''}${button('弃牌', 'fold')}</div>${allowed.actions.includes('raise') ? `<div class="raise-row"><label>加注后合计${state.wallet ? '（元）' : '（筹码）'}<input id="raiseAmount" type="number" inputmode="decimal" min="${inputAmount(allowed.min_raise)}" max="${inputAmount(allowed.max_raise)}" step="${state.wallet ? '.01' : '1'}" value="${inputAmount(allowed.min_raise)}"></label>${button('确认加注', 'raise')}<small id="raiseExplanation"></small></div>` : ''}`;
      if ($('raiseAmount') && previous && Number(previous) >= Number(inputAmount(allowed.min_raise)) && Number(previous) <= Number(inputAmount(allowed.max_raise))) $('raiseAmount').value = previous;
      if ($('raiseAmount')) {
        $('raiseAmount').oninput = explainRaise;
        explainRaise();
      }
    }
  }
  function explainRaise() {
    const total = Math.round(Number($('raiseAmount').value) * (state.wallet ? 100 : 1));
    const paid = state.seats.user.bet;
    const valid = $('raiseAmount').validity.valid;
    $('raiseExplanation').textContent = `这次下注已付 ${amount(paid)}${valid ? `；再付 ${amount(total - paid)}，合计 ${amount(total)}` : `；可填 ${amount(state.legal.min_raise)}～${amount(state.legal.max_raise)}`}`;
    const button = document.querySelector('[data-action="raise"]');
    button.textContent = valid ? `加注 · 再付 ${amount(total - paid)}` : '确认加注';
    button.disabled = busy || !valid;
  }
  function showSummary() {
    if (!state) return;
    $('summaryTitle').textContent = `${title} · 本场${state.closed || state.phase === 'game_over' ? '结算' : '明细'}`;
    const completion = state.end_reason || ((state.rounds || []).length ? `已记录 ${state.rounds.length} ${diceGame ? '轮' : '手'}` : '尚未完成一轮');
    $('summaryNote').textContent = `${completion}。${state.wallet ? state.wallet.status === 'open' ? '金额仍在桌上，收桌后退回钱包。' : '结算金额已从钱包自动扣除' : '免费练习，不变动钱包。'}`;
    const initial = state.wallet?.initial || state.starting_stacks;
    $('summaryTotals').style.setProperty('--players', state.players.length);
    $('summaryTotals').innerHTML = state.players.map(p => {
      const net = state.wallet?.net[p] ?? (diceGame ? state.dice_net[p] : (state.rounds || []).reduce((n, r) => n + r.net[p], 0)) ?? 0;
      const balance = state.wallet?.returned[p] ?? (diceGame ? (state.rounds.at(-1)?.balances[p] ?? 0) : state.seats[p].stack);
      return `<div class="summary-total ${net > 0 ? 'net-win' : net < 0 ? 'net-loss' : ''}"><strong>${esc(name(p))}</strong><span class="summary-caption">${net > 0 ? '本场赢得' : net < 0 ? '本场输掉' : '本场持平'}</span><b>${signed(net)}${!state.wallet && diceGame ? ' 分' : ''}</b><dl>${initial?.[p] != null ? `<div><dt>带入</dt><dd>${amount(initial[p])}</dd></div>` : ''}<div><dt>${state.wallet?.status === 'settled' ? '退回' : '剩余'}</dt><dd>${amount(balance)}</dd></div></dl></div>`;
    }).join('');
    $('summaryRounds').innerHTML = (state.rounds || []).map(r => `<section class="summary-hand"><h3>第 ${r.round} ${diceGame ? '轮' : '手'}</h3>${state.players.map(p => `<div><span>${esc(name(p))}</span><strong>${r.net[p] > 0 ? '赢' : r.net[p] < 0 ? '输' : '持平'} ${signed(r.net[p])}</strong><small>剩余 ${amount(r.balances[p])}</small></div>`).join('')}</section>`).join('') || `<p class="legacy-note">${state.dice_mode === 'three_rounds' || Object.keys(state.starting_stacks || {}).length ? '还没有完成的回合。' : '旧版只保存了总账，本场逐轮明细不可追溯。'}</p>`;
    if (!$('summaryDialog').open) $('summaryDialog').showModal();
  }
  function updateFaces(preferred) {
    const faces = state.legal.bids.filter(b => b.count === Number($('bidCount').value)).map(b => b.face);
    $('bidFace').innerHTML = faces.map(n => `<option value="${n}">${n} 点</option>`).join('');
    if (faces.includes(Number(preferred))) $('bidFace').value = preferred;
    else if (state.wild_active && faces.includes(2)) $('bidFace').value = '2';
  }
  function startNew() {
    if (!$('stake').reportValidity()) return;
    if (state && !state.closed && state.phase !== 'game_over' && !confirm('结束当前场次并重开？已完成的输赢保留，未完成的这一轮作废，不发群总结。')) return;
    command('/new', {opponent:$('opponent').value, wild:$('wild').value === 'yes', stake:Number($('stake').value)});
  }
  let bubbleGame = null, bubbleRound = null;
  const bubbleSeen = new Set(), activeBubbles = new Map();
  function clearBubbles() {
    for (const bubble of activeBubbles.values()) {
      clearTimeout(bubble.hideTimer);
    }
    activeBubbles.clear(); $('tableBubbles').replaceChildren();
  }
  function syncBubbles() {
    if (!state) { clearBubbles(); return; }
    const messages = state.messages || [];
    if (bubbleGame !== state.id) {
      const restoring = bubbleGame === null;
      clearBubbles(); bubbleSeen.clear(); bubbleGame = state.id; bubbleRound = state.round;
      // Loading an existing table must not replay its old speech bubbles.
      if (restoring) { messages.forEach(m => bubbleSeen.add(m.id)); return; }
    }
    if (bubbleRound !== state.round) { clearBubbles(); bubbleRound = state.round; }
    const players = state.players.filter(p => p !== 'user');
    $('tableBubbles').style.setProperty('--speakers', players.length);
    for (const [index, p] of players.entries()) {
      const latest = [...messages].reverse().find(m => m.sender === p && m.round === state.round);
      if (!latest || bubbleSeen.has(latest.id) || !latest.text?.trim()) continue;
      const previous = activeBubbles.get(p);
      if (previous) {
        clearTimeout(previous.hideTimer); previous.node.remove();
      }
      const node = document.createElement('div');
      node.className = 'seat-speech'; node.dataset.player = p;
      node.style.gridColumn = String(index + 1);
      node.setAttribute('role', 'region'); node.setAttribute('aria-label', `${name(p)} 的新消息`);
      const text = document.createElement('div'); text.className = 'speech-text'; text.tabIndex = 0;
      text.textContent = name(p) + '：' + latest.text;
      node.append(text); $('tableBubbles').append(node);
      const bubble = {node, hideTimer:null};
      activeBubbles.set(p, bubble);
      const dismissLater = delay => {
        clearTimeout(bubble.hideTimer);
        bubble.hideTimer = setTimeout(() => {
          node.remove();
          if (activeBubbles.get(p) === bubble) activeBubbles.delete(p);
        }, delay);
      };
      // Keep a finite lifetime even when a touch leaves the text focused.
      // Reading further gets a short extension, without moving the bubble frame.
      text.addEventListener('scroll', () => dismissLater(5000), {passive:true});
      node.addEventListener('pointerup', () => dismissLater(5000));
      dismissLater(Math.min(12000, Math.max(8000, latest.text.length * 60)));
    }
    messages.forEach(m => bubbleSeen.add(m.id));
  }
  $('actionArea').addEventListener('click', event => {
    const action = event.target.closest('button[data-action]')?.dataset.action;
    if (!action || busy) return;
    if (action === 'next') return command('/next');
    if (action === 'settings') return $('setup').showModal();
    if (action === 'summary') return showSummary();
    if (action === 'cashout') return command('/cashout');
    if (action === 'new') return startNew();
    const decision = {action, speech:$('speech').value};
    if (action === 'bid') Object.assign(decision, {count:Number($('bidCount').value), face:Number($('bidFace').value)});
    if (action === 'raise') {
      if (!$('raiseAmount').reportValidity()) return;
      decision.amount = Math.round(Number($('raiseAmount').value) * (state.wallet ? 100 : 1));
    }
    command('/action', decision);
  });
  document.body.dataset.game = kind;
  $('roomSubtitle').textContent = diceGame ? 'PRIVATE ROOM · LIAR’S DICE' : 'PRIVATE ROOM · TEXAS HOLD’EM';
  $('title').textContent = title; document.title = `${title} · 小家的娱乐室`;
  $('wildSetting').hidden = !diceGame;
  $('stake').value = diceGame ? '50' : '200';
  $('stakeLabel').textContent = diceGame ? '每轮输家支付（元）' : '本桌带入上限（元）';
  $('setupNote').textContent = (diceGame ? '共 3 轮，每轮重新摇 5 颗。输家付约定金额给赢家，第三人不输不赢；开局暂存 3 轮金额。' : '最多 3 手后自动收桌。小盲 ¥5 / 大盲 ¥10；默认带入 ¥200，不超过各自额度。') + ' 完整结束才同步全员总账；随时可结束或重开，未完成的这一轮作废并退回下注。';
  $('rulesText').innerHTML = diceGame ? '<p><strong>1. 看自己的骰子。</strong>你只知道自己杯子里的点数。</p><p><strong>2. 猜全桌，轮流叫数。</strong>“四个 5”表示所有人的骰子加起来，至少有四个 5。下一家可以叫“五个 2”，也可以叫“四个 6”。</p><p><strong>3. 不信就开。</strong>数量够，开骰的人输；不够，刚才叫数的人输。输家支付约定金额给另一方，第三人不输不赢。每轮每人重新摇 5 颗，输家先叫；共玩 3 轮后收桌，不淘汰玩家。桌中央没有额外的骰子。</p><p><strong>百搭例子：</strong>1、1、3、5、6 可以算三个 5。有人叫过“几个 1”后，本轮的 1 就只算 1；下一轮恢复。数量与点数的加叫规则不变。不百搭模式下，始终只数实际点数。</p>' : '<p><strong>整场最多 3 手。</strong>每次发两张底牌算一手，翻牌、转牌、河牌都在同一手里。第 3 手结束自动收桌，并展示逐手明细。</p><p><strong>1. 每人两张暗牌。</strong>桌上逐步发出三张翻牌、一张转牌、一张河牌；每次发牌之间轮流下注。</p><p><strong>2. 轮到你时。</strong>过牌＝不加钱继续；跟注＝补齐当前下注；加注＝提高本轮总额；弃牌＝放弃这手；全下＝投入剩余筹码。本桌金额由开局约定，余额不足可以全下；免费练习每人 500 筹码。</p><p><strong>3. 留到最后比牌。</strong>从底牌和公共牌里任选最强的五张。其他人都弃牌，你直接赢。有人全下时，多出来的筹码进入边池，只在有资格的人之间比较。</p><p><strong>牌型由强到弱：</strong>同花顺、四条、葫芦、同花、顺子、三条、两对、一对、高牌。相同牌型继续比点数，完全相同就平分。</p>';
  $('newGame').onclick = startNew;
  $('settingsToggle').onclick = () => $('setup').showModal();
  $('settingsClose').onclick = () => $('setup').close();
  $('summaryClose').onclick = () => $('summaryDialog').close();
  $('cashout').onclick = () => {
    const reportHint = diceGame ? '不发群总结。' : state.rounds?.length ? '收桌总结将同步到最近聊天的群。' : '尚未完成一手，不生成群总结。';
    if (confirm('结束本场？已完成的输赢保留，未完成的这一轮作废并退回下注。' + reportHint)) command('/cashout');
  };
  $('rulesToggle').onclick = () => $('rulesDialog').showModal();
  $('rulesClose').onclick = () => $('rulesDialog').close();
  $('refresh').onclick = () => refresh(true, true);
  $('retry').onclick = async () => { await refresh(false); if (!failed) schedule(); };
  const tableCompanion = window.TableCompanion.bind({kind, request:api, onChatState:next => {
    if (state?.id !== next.id || (next.chat_updated_at || 0) < (state.chat_updated_at || 0)) return;
    for (const key of ['messages','chat_updated_at','chat_error','chat_busy']) state[key] = next[key];
    syncBubbles();
  }});
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) clearTimeout(timer);
    else if (!busy) refresh();
  });
  render();
  refresh();
})();

/* Home owns gift/alarm UI. Other pages only retain incoming alarms. */
(function () {
  if (window.AionHomePopups) return;
  const ALARMS_KEY = 'aion_home_alarms_v1';
  const isHome = document.body?.dataset?.homePopups === 'enabled';
  const $ = id => document.getElementById(id);
  let subPageVisible = window.parent === window || window.frameElement?.dataset?.aionSubPageVisible === '1';
  let currentAlarmKey = null;
  let names = {};
  let namesReady = false;
  let pendingRequest = null;

  function homeVisible() { return isHome && subPageVisible && !document.hidden; }
  function escHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  }
  function readAlarms() {
    try { return JSON.parse(localStorage.getItem(ALARMS_KEY)) || { pending: [], seen: [] }; }
    catch (_) { return { pending: [], seen: [] }; }
  }
  function alarmKey(data) { return JSON.stringify([data.id, data.trigger_at, data.origin]); }
  function handleEvent(event) {
    const data = event?.data;
    if (event?.type === 'gift_pending') {
      if (isHome) _showGiftPopup(data);
      return;
    }
    if (event?.type !== 'schedule_alarm' || !data) return;
    const state = readAlarms();
    const key = alarmKey(data);
    if (!state.seen.includes(key) && !state.pending.some(item => alarmKey(item) === key)) {
      state.pending.push(data);
      localStorage.setItem(ALARMS_KEY, JSON.stringify(state));
      const body = data.origin_name ? '【' + data.origin_name + '】设定的闹铃：' + (data.content || '日程提醒') : (data.content || '日程提醒');
      if ('Notification' in window && Notification.permission === 'granted') {
        try { new Notification('⏰ 闹铃', { body, icon: '/public/icon-192.png' }); } catch (_) {}
      }
    }
    showNextAlarm();
  }
  window.AionHomePopups = { handleEvent };

  function showNextAlarm() {
    if (!homeVisible()) return;
    const data = readAlarms().pending[0];
    if (!data) {
      $('alarmOverlay')?.classList.remove('show');
      currentAlarmKey = null;
      return;
    }
    if (!$('alarmOverlay')) {
      const overlay = document.createElement('div');
      overlay.id = 'alarmOverlay';
      overlay.className = 'alarm-overlay';
      overlay.innerHTML = '<div class="alarm-box"><div class="alarm-icon">⏰</div><h3>日程提醒</h3><div class="alarm-content" id="alarmContent"></div><div class="alarm-time" id="alarmTime"></div><button onclick="dismissAlarm()">确认</button></div>';
      document.body.appendChild(overlay);
    }
    currentAlarmKey = alarmKey(data);
    $('alarmContent').textContent = data.origin_name ? '【' + data.origin_name + '】设定的闹铃：' + (data.content || '日程提醒') : (data.content || '日程提醒');
    $('alarmTime').textContent = data.trigger_at || '';
    $('alarmOverlay').classList.add('show');
  }
  function dismissAlarm() {
    const state = readAlarms();
    if (currentAlarmKey) {
      state.pending = state.pending.filter(item => alarmKey(item) !== currentAlarmKey);
      state.seen = [...new Set([...state.seen, currentAlarmKey])].slice(-200);
      localStorage.setItem(ALARMS_KEY, JSON.stringify(state));
    }
    showNextAlarm();
  }
  if (!isHome) return;

  // ── 礼物弹窗系统 ──
  let _giftQueue = [];
  let _giftShowing = false;
  const _GIFT_KNOWN_KEY = 'aion_gift_known_ids';
  let _giftKnownIds = _readGiftKnownIds();

  function _readGiftKnownIds() {
    try {
      return new Set(JSON.parse(localStorage.getItem(_GIFT_KNOWN_KEY) || '[]'));
    } catch(e) {
      return new Set();
    }
  }

  function _isGiftKnown(giftId) {
    if (!giftId) return true;
    if (_giftKnownIds.has(giftId)) return true;
    _giftKnownIds = _readGiftKnownIds();
    return _giftKnownIds.has(giftId);
  }

  function _rememberGiftSeen(giftId) {
    if (!giftId) return;
    _giftKnownIds = _readGiftKnownIds();
    _giftKnownIds.add(giftId);
    localStorage.setItem(_GIFT_KNOWN_KEY, JSON.stringify([..._giftKnownIds].slice(-200)));
  }

  function _dropKnownGiftPopups() {
    _giftKnownIds = _readGiftKnownIds();
    const current = _giftQueue[0];
    _giftQueue = _giftQueue.filter(g => g && !_giftKnownIds.has(g.id));
    if (current && _giftKnownIds.has(current.id)) {
      const overlay = document.getElementById('giftOverlay');
      if (overlay) overlay.remove();
      _giftShowing = false;
      _presentNextGift();
    }
  }

  window.addEventListener('storage', (e) => {
    if (e.key === _GIFT_KNOWN_KEY) _dropKnownGiftPopups();
  });

  function _showGiftPopup(gift) {
    if (!gift || !gift.id || _isGiftKnown(gift.id) || _giftQueue.some(g => g.id === gift.id)) return;
    _giftQueue.push(gift);
    if (!_giftShowing) _presentNextGift();
  }
  function _presentNextGift() {
    if (!_giftQueue.length) { _giftShowing = false; return; }
    if (!homeVisible() || !namesReady) return;
    _giftShowing = true;
    const gift = _giftQueue[0];
    const old = document.getElementById('giftOverlay');
    if (old) old.remove();
    const overlay = document.createElement('div');
    overlay.id = 'giftOverlay';
    overlay.className = 'gift-overlay';
    overlay.innerHTML = `
      <div class="gift-scene" id="giftScene">
        <div class="gift-box-wrap" id="giftBoxWrap" onclick="_openGiftBox()">
          <svg class="gift-box-svg" viewBox="0 0 200 200" width="180" height="180">
            <rect class="gift-body" x="30" y="100" width="140" height="90" rx="8" fill="#ff8359" stroke="#e0693f" stroke-width="2"/>
            <rect x="90" y="100" width="20" height="90" rx="2" fill="#ffcba4"/>
            <g class="gift-lid" id="giftLid">
              <rect x="22" y="80" width="156" height="28" rx="6" fill="#ff6b3d" stroke="#e0693f" stroke-width="2"/>
              <rect x="90" y="80" width="20" height="28" rx="2" fill="#ffcba4"/>
              <ellipse cx="100" cy="76" rx="24" ry="14" fill="#ffcba4" stroke="#e0693f" stroke-width="1.5"/>
              <ellipse cx="100" cy="76" rx="6" ry="6" fill="#ff6b3d"/>
            </g>
            <text x="50" y="140" font-size="16" fill="#ffcba4" opacity="0.7">✦</text>
            <text x="135" y="155" font-size="12" fill="#ffcba4" opacity="0.7">✦</text>
            <text x="65" y="170" font-size="10" fill="#ffcba4" opacity="0.5">✦</text>
          </svg>
          <div class="gift-tap-hint">点击打开</div>
        </div>
        <div class="gift-reveal" id="giftReveal" style="display:none">
          <div class="confetti-container" id="confettiContainer"></div>
          <div class="gift-image-wrap" id="giftImageWrap" onclick="_showGiftMessage()">
            <img class="gift-image" src="/uploads/${gift.image_path}" alt="礼物" />
          </div>
          <div class="gift-message-wrap" id="giftMessageWrap" style="display:none">
            <p class="gift-message-from" style="text-align:center;opacity:0.7;font-size:0.85em;margin-bottom:4px">—— from ${escHtml(gift.sender === 'connor' ? (names.connor_name || '第二AI') : (names.ai_name || 'AI'))} ——</p>
            <p class="gift-message-text">${escHtml(gift.message)}</p>
          </div>
          <button class="gift-receive-btn" id="giftReceiveBtn" style="display:none" onclick="_receiveGift('${gift.id}')">💝 收下礼物</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    requestAnimationFrame(() => overlay.classList.add('show'));
  }
  function _openGiftBox() {
    const lid = document.getElementById('giftLid');
    const wrap = document.getElementById('giftBoxWrap');
    const reveal = document.getElementById('giftReveal');
    if (!lid || !wrap || !reveal) return;
    const gift = _giftQueue[0];
    if (gift?.id) {
      _rememberGiftSeen(gift.id);
      fetch(`/api/gift/${gift.id}/receive`, { method: 'POST' }).catch(() => {});
    }
    // 播放开礼物音效
    new Audio('/public/打开礼物.mp3').play().catch(() => {});
    lid.classList.add('lid-open');
    wrap.classList.add('box-opening');
    setTimeout(() => {
      wrap.style.display = 'none';
      reveal.style.display = 'flex';
      _spawnConfetti();
      const imgWrap = document.getElementById('giftImageWrap');
      setTimeout(() => imgWrap.classList.add('show'), 100);
    }, 600);
  }
  function _spawnConfetti() {
    const container = document.getElementById('confettiContainer');
    if (!container) return;
    const colors = ['#ff8359','#ffcba4','#ff6b9d','#ffd700','#7ecbff','#a8e6cf','#ff9a9e','#fad0c4','#fbc2eb','#a18cd1'];
    const shapes = ['confetti-rect','confetti-circle','confetti-ribbon'];
    for (let i = 0; i < 60; i++) {
      const el = document.createElement('div');
      el.className = `confetti-piece ${shapes[Math.floor(Math.random()*shapes.length)]}`;
      el.style.setProperty('--x', (Math.random()*200-100)+'px');
      el.style.setProperty('--y', -(Math.random()*300+200)+'px');
      el.style.setProperty('--r', (Math.random()*720-360)+'deg');
      el.style.setProperty('--delay', (Math.random()*0.3)+'s');
      el.style.setProperty('--duration', (Math.random()*1+1.2)+'s');
      el.style.backgroundColor = colors[Math.floor(Math.random()*colors.length)];
      el.style.left = '50%'; el.style.top = '40%';
      container.appendChild(el);
    }
    setTimeout(() => container.innerHTML = '', 3000);
  }
  function _showGiftMessage() {
    const msgWrap = document.getElementById('giftMessageWrap');
    const btn = document.getElementById('giftReceiveBtn');
    if (msgWrap && msgWrap.style.display === 'none') {
      msgWrap.style.display = 'block';
      setTimeout(() => msgWrap.classList.add('show'), 50);
      if (btn) { btn.style.display = 'inline-block'; setTimeout(() => btn.classList.add('show'), 200); }
    }
  }
  async function _receiveGift(giftId) {
    _rememberGiftSeen(giftId);
    try { await fetch(`/api/gift/${giftId}/receive`, {method:'POST'}); } catch(e) {}
    const scene = document.getElementById('giftScene');
    if (scene) scene.classList.add('fly-away');
    setTimeout(() => {
      const overlay = document.getElementById('giftOverlay');
      if (overlay) overlay.remove();
      _giftQueue.shift();
      _giftShowing = false;
      _presentNextGift();
    }, 800);
  }


  async function checkPendingGifts() {
    if (pendingRequest) return pendingRequest;
    pendingRequest = (async () => {
      try {
        const res = await fetch('/api/gift/pending');
        const data = await res.json();
        if (data.ok && Array.isArray(data.gifts)) data.gifts.forEach(_showGiftPopup);
      } catch (_) {}
    })().finally(() => { pendingRequest = null; });
    return pendingRequest;
  }
  function resume() {
    if (!homeVisible()) return;
    showNextAlarm();
    if (!_giftShowing) _presentNextGift();
    checkPendingGifts();
  }
  window.onAionSubPageVisibilityChanged = visible => { subPageVisible = !!visible; resume(); };
  document.addEventListener('visibilitychange', resume);
  window.addEventListener('pageshow', resume);
  window.addEventListener('storage', event => {
    if (event.key === ALARMS_KEY || event.key === null) showNextAlarm();
  });
  Object.assign(window, { dismissAlarm, _openGiftBox, _showGiftMessage, _receiveGift });

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(proto + '//' + location.host + '/ws');
    socket.onopen = checkPendingGifts;
    socket.onmessage = event => handleEvent(JSON.parse(event.data));
    socket.onclose = () => setTimeout(connect, 2000);
    socket.onerror = () => socket.close();
  }
  fetch('/api/chatroom/config').then(res => res.json()).then(data => { names = data || {}; }).catch(() => {}).finally(() => {
    namesReady = true;
    if (!_giftShowing) _presentNextGift();
  });
  connect();
  resume();
})();

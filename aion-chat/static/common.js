/* ── Aion Common JS — 共享工具函数 ── */

const $ = id => document.getElementById(id);

// 在 iframe 子页面浮层中时，返回按钮改为关闭浮层回到聊天页
if (window.parent !== window) {
  document.addEventListener('DOMContentLoaded', () => {
    const backBtn = document.querySelector('.top-bar .back-btn');
    if (backBtn) backBtn.onclick = () => window.parent.closeSubPage();
  });
}

async function api(method, url, body) {
  const opts = { method, headers: {"Content-Type": "application/json"} };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(url, opts);
  return res.json();
}

function escHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

/* ── Toast ── */
let _toastTimer = null;
function showToast(msg) {
  let t = document.getElementById('commonToast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'commonToast';
    t.className = 'toast-msg';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => t.classList.remove('show'), 2000);
}

/* ── WebSocket（闹铃弹窗等全局事件） ── */
let _commonWs = null;
let _wsHandlers = {};

function connectCommonWS(extraHandler) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  _commonWs = new WebSocket(`${proto}//${location.host}/ws`);
  _commonWs.onmessage = e => {
    const msg = JSON.parse(e.data);
    // 闹铃弹窗 — 全局
    if (msg.type === "schedule_alarm") {
      showAlarmPopup(msg.data);
      return;
    }
    // 监控提示音 — 全局
    if (msg.type === "monitor_alert") {
      const audio = new Audio('/public/AionMonitoralart.mp3');
      audio.play().catch(() => {});
      sendSystemNotification('📷 监控提醒', msg.data?.content || '哨兵监控即将分析');
      return;
    }
    // 礼物通知 — 全局
    if (msg.type === "gift_pending") {
      _showGiftPopup(msg.data);
      return;
    }
    // 页面自定义处理
    if (extraHandler) extraHandler(msg);
  };
  _commonWs.onclose = () => setTimeout(() => connectCommonWS(extraHandler), 2000);
}

/* ── 闹铃弹窗 ── */
let _alarmQueue = [];
function showAlarmPopup(data) {
  _alarmQueue.push(data);
  if (_alarmQueue.length === 1) _showNextAlarm();
  sendSystemNotification('⏰ 闹铃', data.content || '日程提醒');
}
function _showNextAlarm() {
  if (!_alarmQueue.length) return;
  // 确保 DOM 中有闹铃弹窗
  _ensureAlarmOverlay();
  const data = _alarmQueue[0];
  $("alarmContent").textContent = data.content || "日程提醒";
  $("alarmTime").textContent = data.trigger_at || "";
  $("alarmOverlay").classList.add("show");
}
function dismissAlarm() {
  $("alarmOverlay").classList.remove("show");
  _alarmQueue.shift();
  if (_alarmQueue.length) setTimeout(_showNextAlarm, 300);
}

function _ensureAlarmOverlay() {
  if ($("alarmOverlay")) return;
  const div = document.createElement('div');
  div.innerHTML = `
    <div class="alarm-overlay" id="alarmOverlay">
      <div class="alarm-box">
        <div class="alarm-icon">⏰</div>
        <h3>日程提醒</h3>
        <div class="alarm-content" id="alarmContent"></div>
        <div class="alarm-time" id="alarmTime"></div>
        <button onclick="dismissAlarm()">确认</button>
      </div>
    </div>`;
  document.body.appendChild(div.firstElementChild);
}

/* ── 系统通知 ── */
function sendSystemNotification(title, body) {
  if (!('Notification' in window)) return;
  if (Notification.permission !== 'granted') return;
  try { new Notification(title, { body, icon: '/public/icon-192.png' }); } catch(e) {}
}

// 请求通知权限
if ('Notification' in window && Notification.permission === 'default') {
  Notification.requestPermission();
}

/* ── 礼物弹窗系统 ── */
let _giftQueue = [];
let _giftShowing = false;

// 页面加载时检查未领取的礼物
document.addEventListener('DOMContentLoaded', async () => {
  try {
    const res = await fetch('/api/gift/pending');
    const data = await res.json();
    if (data.ok && data.gifts && data.gifts.length > 0) {
      data.gifts.forEach(g => _showGiftPopup(g));
    }
  } catch(e) {}
});

function _showGiftPopup(gift) {
  _giftQueue.push(gift);
  if (!_giftShowing) _presentNextGift();
}

function _presentNextGift() {
  if (!_giftQueue.length) { _giftShowing = false; return; }
  _giftShowing = true;
  const gift = _giftQueue[0];
  _buildGiftOverlay(gift);
}

function _buildGiftOverlay(gift) {
  // 移除旧的
  const old = document.getElementById('giftOverlay');
  if (old) old.remove();

  const overlay = document.createElement('div');
  overlay.id = 'giftOverlay';
  overlay.className = 'gift-overlay';
  overlay.innerHTML = `
    <div class="gift-scene" id="giftScene">
      <!-- 阶段1: 礼物盒 -->
      <div class="gift-box-wrap" id="giftBoxWrap" onclick="_openGiftBox()">
        <svg class="gift-box-svg" viewBox="0 0 200 200" width="180" height="180">
          <!-- 盒身 -->
          <rect class="gift-body" x="30" y="100" width="140" height="90" rx="8" fill="#ff8359" stroke="#e0693f" stroke-width="2"/>
          <rect x="90" y="100" width="20" height="90" rx="2" fill="#ffcba4"/>
          <!-- 盒盖 -->
          <g class="gift-lid" id="giftLid">
            <rect x="22" y="80" width="156" height="28" rx="6" fill="#ff6b3d" stroke="#e0693f" stroke-width="2"/>
            <rect x="90" y="80" width="20" height="28" rx="2" fill="#ffcba4"/>
            <!-- 蝴蝶结 -->
            <ellipse cx="100" cy="76" rx="24" ry="14" fill="#ffcba4" stroke="#e0693f" stroke-width="1.5"/>
            <ellipse cx="100" cy="76" rx="6" ry="6" fill="#ff6b3d"/>
          </g>
          <!-- 星星装饰 -->
          <text x="50" y="140" font-size="16" fill="#ffcba4" opacity="0.7">✦</text>
          <text x="135" y="155" font-size="12" fill="#ffcba4" opacity="0.7">✦</text>
          <text x="65" y="170" font-size="10" fill="#ffcba4" opacity="0.5">✦</text>
        </svg>
        <div class="gift-tap-hint">点击打开</div>
      </div>

      <!-- 阶段2: 礼花 + 图片 (隐藏) -->
      <div class="gift-reveal" id="giftReveal" style="display:none">
        <div class="confetti-container" id="confettiContainer"></div>
        <div class="gift-image-wrap" id="giftImageWrap" onclick="_showGiftMessage()">
          <img class="gift-image" src="/uploads/${gift.image_path}" alt="礼物" />
        </div>
        <div class="gift-message-wrap" id="giftMessageWrap" style="display:none">
          <p class="gift-message-text">${escHtml(gift.message)}</p>
        </div>
        <button class="gift-receive-btn" id="giftReceiveBtn" style="display:none" onclick="_receiveGift('${gift.id}')">
          💝 收下礼物
        </button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  // 触发入场动画
  requestAnimationFrame(() => overlay.classList.add('show'));
}

function _openGiftBox() {
  const lid = document.getElementById('giftLid');
  const wrap = document.getElementById('giftBoxWrap');
  const reveal = document.getElementById('giftReveal');
  if (!lid || !wrap || !reveal) return;

  // 播放开礼物音效
  new Audio('/public/打开礼物.mp3').play().catch(() => {});
  // 盒盖飞走动画
  lid.classList.add('lid-open');
  wrap.classList.add('box-opening');

  setTimeout(() => {
    wrap.style.display = 'none';
    reveal.style.display = 'flex';
    // 生成礼花
    _spawnConfetti();
    // 图片入场
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
    const shape = shapes[Math.floor(Math.random() * shapes.length)];
    el.className = `confetti-piece ${shape}`;
    el.style.setProperty('--x', (Math.random() * 200 - 100) + 'px');
    el.style.setProperty('--y', -(Math.random() * 300 + 200) + 'px');
    el.style.setProperty('--r', (Math.random() * 720 - 360) + 'deg');
    el.style.setProperty('--delay', (Math.random() * 0.3) + 's');
    el.style.setProperty('--duration', (Math.random() * 1 + 1.2) + 's');
    el.style.backgroundColor = colors[Math.floor(Math.random() * colors.length)];
    el.style.left = '50%';
    el.style.top = '40%';
    container.appendChild(el);
  }
  // 清理礼花
  setTimeout(() => container.innerHTML = '', 3000);
}

function _showGiftMessage() {
  const msgWrap = document.getElementById('giftMessageWrap');
  const btn = document.getElementById('giftReceiveBtn');
  if (msgWrap && msgWrap.style.display === 'none') {
    msgWrap.style.display = 'block';
    setTimeout(() => msgWrap.classList.add('show'), 50);
    if (btn) {
      btn.style.display = 'inline-block';
      setTimeout(() => btn.classList.add('show'), 200);
    }
  }
}

async function _receiveGift(giftId) {
  try {
    await fetch(`/api/gift/${giftId}/receive`, { method: 'POST' });
  } catch(e) {}
  // 飞走动画
  const scene = document.getElementById('giftScene');
  if (scene) scene.classList.add('fly-away');
  setTimeout(() => {
    const overlay = document.getElementById('giftOverlay');
    if (overlay) overlay.remove();
    _giftQueue.shift();
    _presentNextGift();
  }, 800);
}

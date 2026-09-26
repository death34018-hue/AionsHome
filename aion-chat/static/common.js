/* ── Aion Common JS — 共享工具函数 ── */

(function () {
  const KEY = 'aion_chat_theme';
  const STYLE_ID = 'aion-theme-css';
  const STYLE_HREF = '/static/theme.css?v=20260606';

  function normalizeTheme(theme) {
    return theme === 'light' ? 'light' : 'dark';
  }

  function ensureThemeStyles() {
    if (document.getElementById(STYLE_ID)) return;
    const link = document.createElement('link');
    link.id = STYLE_ID;
    link.rel = 'stylesheet';
    link.href = STYLE_HREF;
    document.head.appendChild(link);
  }

  function updateThemeChrome(theme) {
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', theme === 'dark' ? '#050923' : '#eef3ff');
    if (window.AionStatusBar) window.AionStatusBar.setBarStyle(theme);
  }

  function applyAionTheme(theme, options) {
    const next = normalizeTheme(theme);
    document.documentElement.dataset.theme = next;
    if (document.body) document.body.dataset.theme = next;
    if (!options || options.persist !== false) localStorage.setItem(KEY, next);
    updateThemeChrome(next);
    window.dispatchEvent(new CustomEvent('aion-theme-applied', { detail: { theme: next } }));
    return next;
  }

  function initialTheme() {
    return localStorage.getItem(KEY)
      || (document.body && document.body.dataset.theme)
      || document.documentElement.dataset.theme
      || 'light';
  }

  window.AionTheme = Object.assign(window.AionTheme || {}, {
    key: KEY,
    apply: applyAionTheme,
    ensureStyles: ensureThemeStyles,
    current: () => normalizeTheme((document.body && document.body.dataset.theme) || document.documentElement.dataset.theme || 'light')
  });
  window.applyAionTheme = applyAionTheme;

  function initTheme() {
    ensureThemeStyles();
    applyAionTheme(initialTheme(), { persist: Boolean(localStorage.getItem(KEY)) });
  }

  if (document.body) initTheme();
  else document.addEventListener('DOMContentLoaded', initTheme, { once: true });

  window.addEventListener('storage', event => {
    if (event.key === KEY) applyAionTheme(event.newValue || initialTheme(), { persist: false });
  });
})();

const $ = id => document.getElementById(id);

// Android APK 中 WebView 是 edge-to-edge，普通功能页需要自己避开系统状态栏。
// iframe 子页面由 chat.html 的浮层统一处理，避免重复留白。
if (navigator.userAgent.includes('AionChatApp')) {
  const root = document.documentElement;
  root.classList.add('aion-app');
  if (window.parent !== window) root.classList.add('aion-iframe');
  document.addEventListener('DOMContentLoaded', () => {
    if (window.parent === window) {
      const topBar = document.querySelector('.top-bar');
      const color = getSolidVisualColor(topBar ? getComputedStyle(topBar).backgroundColor : '', getPageBaseColor());
      root.style.setProperty('--aion-safe-bg', color);
      if (window.AionStatusBar) window.AionStatusBar.setBarStyle(isLightVisualColor(color) ? 'light' : 'dark');
    }
  });
}

function parseVisualColor(value) {
  if (!value || value === 'transparent') return null;
  const hexMatch = value.trim().match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
  if (hexMatch) {
    const hex = hexMatch[1].length === 3 ? hexMatch[1].split('').map(part => part + part).join('') : hexMatch[1];
    return {
      red: parseInt(hex.slice(0, 2), 16),
      green: parseInt(hex.slice(2, 4), 16),
      blue: parseInt(hex.slice(4, 6), 16),
      alpha: 1
    };
  }
  const rgbMatch = value.match(/rgba?\(([^)]+)\)/i);
  if (!rgbMatch) return null;
  const parts = rgbMatch[1].split(/[\s,\/]+/).filter(Boolean).map(Number);
  if (parts.length < 3) return null;
  return { red: parts[0], green: parts[1], blue: parts[2], alpha: parts.length > 3 ? parts[3] : 1 };
}

function isLightVisualColor(value) {
  const color = parseVisualColor(value);
  if (!color || color.alpha <= 0.05) return true;
  return ((color.red * 299) + (color.green * 587) + (color.blue * 114)) / 1000 > 150;
}

function colorToRgbString(color) {
  return `rgb(${Math.round(color.red)}, ${Math.round(color.green)}, ${Math.round(color.blue)})`;
}

function getPageBaseColor() {
  const rootStyle = getComputedStyle(document.documentElement);
  const bodyStyle = getComputedStyle(document.body);
  return rootStyle.getPropertyValue('--bg').trim() || bodyStyle.backgroundColor || '#fff9f5';
}

function getSolidVisualColor(foregroundValue, backgroundValue) {
  const foreground = parseVisualColor(foregroundValue);
  const background = parseVisualColor(backgroundValue) || parseVisualColor('#fff9f5');
  if (!foreground || foreground.alpha <= 0.05) return colorToRgbString(background);
  if (foreground.alpha >= 0.98) return colorToRgbString(foreground);
  const alpha = foreground.alpha;
  return colorToRgbString({
    red: foreground.red * alpha + background.red * (1 - alpha),
    green: foreground.green * alpha + background.green * (1 - alpha),
    blue: foreground.blue * alpha + background.blue * (1 - alpha)
  });
}

function getSubPageReturnUrl() {
  const returnTo = new URLSearchParams(window.location.search).get('return');
  if (!returnTo) return '/';
  try {
    const target = new URL(returnTo, window.location.origin);
    if (target.origin !== window.location.origin) return '/';
    return `${target.pathname}${target.search}${target.hash}`;
  } catch(e) {
    return '/';
  }
}

function navigateSubPageBack() {
  const returnTo = getSubPageReturnUrl();
  if (window.parent !== window && typeof window.parent.openSubPage === 'function') {
    window.parent.openSubPage(returnTo);
  } else {
    window.location.href = returnTo;
  }
}

// iframe 子页面默认返回 Home；带 return 参数时回到指定的父页面功能。
if (window.parent !== window) {
  document.addEventListener('DOMContentLoaded', () => {
    const backBtn = document.querySelector('.top-bar .back-btn');
    if (backBtn && !backBtn.hasAttribute('data-custom-back')) backBtn.onclick = navigateSubPageBack;
  });
}

async function api(method, url, body, options = {}) {
  const controller = new AbortController();
  const timeoutMs = Number(options.timeoutMs || 15000);
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const opts = { method, headers: {"Content-Type": "application/json"}, signal: controller.signal };
  if (body !== undefined && body !== null) opts.body = JSON.stringify(body);
  try {
    const res = await fetch(url, opts);
    const text = await res.text();
    let payload = null;
    if (text) {
      try { payload = JSON.parse(text); }
      catch(e) { payload = { detail: text }; }
    }
    if (!res.ok) {
      const message = payload?.detail || payload?.error || `请求失败 (${res.status})`;
      const error = new Error(typeof message === 'string' ? message : JSON.stringify(message));
      error.status = res.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  } catch(e) {
    if (e?.name === 'AbortError') throw new Error('网络响应超时，请稍后重试');
    throw e;
  } finally {
    clearTimeout(timer);
  }
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
let _commonReconnectTimer = null;
let _wsHandlers = {};
const _securityAlertUi = import('/static/security-alert.js')
  .then(() => globalThis.AionSecurityAlerts?.init())
  .then(() => globalThis.AionSecurityAlerts)
  .catch(() => null);

const COMMON_SYNC_SEQ_KEY = `aion_sync_seq_v1:${location.pathname}`;
let _commonSyncPromise = null;

function _commonRememberSyncSeq(msg) {
  const seq = Number(msg?.sync_seq || 0);
  if (!seq) return;
  const current = Number(localStorage.getItem(COMMON_SYNC_SEQ_KEY) || 0);
  if (seq > current) localStorage.setItem(COMMON_SYNC_SEQ_KEY, String(seq));
}

async function reconcileCommonSync(extraHandler) {
  if (_commonSyncPromise) return _commonSyncPromise;
  _commonSyncPromise = (async () => {
    let after = Number(localStorage.getItem(COMMON_SYNC_SEQ_KEY) || 0);
    for (let batch = 0; batch < 20; batch++) {
      const result = await api('GET', `/api/sync/changes?after=${after}&limit=200`, null, { timeoutMs: 10000 });
      if (result?.reset_required) {
        if (extraHandler) extraHandler({ type: 'sync_reset_required', sync_event: true });
        const latest = Number(result.latest_seq || 0);
        localStorage.setItem(COMMON_SYNC_SEQ_KEY, String(latest));
        return;
      }
      const events = Array.isArray(result?.events) ? result.events : [];
      for (const event of events) {
        if (extraHandler) extraHandler({ ...event, sync_event: true });
        _commonRememberSyncSeq(event);
        after = Math.max(after, Number(event.sync_seq || 0));
      }
      if (!result?.has_more || !events.length) break;
    }
  })().catch(e => {
    console.warn('[sync] reconcile failed:', e);
  }).finally(() => { _commonSyncPromise = null; });
  return _commonSyncPromise;
}

function connectCommonWS(extraHandler, options = {}) {
  if (options.isActive && !options.isActive()) return;
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  _commonWs = new WebSocket(`${proto}//${location.host}/ws`);
  const socket = _commonWs;
  const reconcile = () => {
    if (options.isActive && !options.isActive()) return;
    return options.reconcile ? options.reconcile() : reconcileCommonSync(extraHandler);
  };
  _commonWs.onopen = reconcile;
  _commonWs.onmessage = e => {
    if (options.isActive && !options.isActive()) return;
    const msg = JSON.parse(e.data);
    _commonRememberSyncSeq(msg);
    if (msg.type === 'security_alert') {
      _securityAlertUi.then(ui => ui?.handleMessage(msg));
      return;
    }
    // 收到闹铃后交给 Home 展示。
    if (msg.type === "schedule_alarm") {
      showAlarmPopup(msg.data);
      return;
    }
    // 监控提示音 — 全局
    if (msg.type === "monitor_alert") {
      const data = msg.data || {};
      if (!data.phone_camera_native_capture) {
        if (window.AionTtsAudio && typeof window.AionTtsAudio.play === 'function') {
          window.AionTtsAudio.play('tts-monitor-alert', '/public/AionMonitoralart.mp3');
        } else {
          const audio = new Audio('/public/AionMonitoralart.mp3');
          audio.play().catch(() => {});
        }
      }
      const body = msg.data?.origin_name
        ? `【${msg.data.origin_name}】设定的监督：${msg.data?.content || '哨兵监控即将分析'}`
        : (msg.data?.content || '哨兵监控即将分析');
      sendSystemNotification('📷 监控提醒', body);
      return;
    }
    // 礼物通知由 Home 的连接处理。
    if (msg.type === "gift_pending") {
      // Home has its own notification connection and pending-gift recovery.
      return;
    }
    // 页面自定义处理
    if (extraHandler) extraHandler(msg);
  };
  _commonWs.onclose = () => {
    _commonReconnectTimer = setTimeout(() => connectCommonWS(extraHandler, options), 2000);
  };
  _commonWs.onerror = () => socket.close();

  if (!options.isActive && !connectCommonWS._visibilityBound) {
    connectCommonWS._visibilityBound = true;
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') reconcile();
    });
    window.addEventListener('pageshow', event => {
      if (event.persisted) reconcile();
    });
  }
}

const _homePopups = import('/static/home-popups.js?v=20260923').then(() => window.AionHomePopups);
function showAlarmPopup(data) {
  _homePopups.then(ui => ui.handleEvent({ type: 'schedule_alarm', data }));
}

// Opt in only for passive pages retained by the chat shell. Their server work
// continues normally; hidden pages stop opening sockets and repainting lists.
function connectRetainedPageWS(extraHandler, options = {}) {
  let pageVisible = true;
  try { pageVisible = window.frameElement?.dataset.aionSubPageVisible !== '0'; } catch (_) {}
  const isActive = () => pageVisible && document.visibilityState !== 'hidden';
  let wasActive = false;
  function sync() {
    const active = isActive();
    if (active === wasActive) return;
    wasActive = active;
    clearTimeout(_commonReconnectTimer);
    if (active) {
      connectCommonWS(extraHandler, { ...options, isActive });
    } else if (_commonWs) {
      _commonWs.onopen = _commonWs.onmessage = _commonWs.onclose = _commonWs.onerror = null;
      _commonWs.close();
      _commonWs = null;
    }
  }
  const previous = window.onAionSubPageVisibilityChanged;
  window.onAionSubPageVisibilityChanged = visible => {
    previous?.(visible);
    pageVisible = !!visible;
    sync();
  };
  document.addEventListener('visibilitychange', sync);
  window.addEventListener('pageshow', event => {
    if (event.persisted && isActive()) options.reconcile?.();
  });
  sync();
}

// Reveal page content once its DOM is ready, without waiting for large images.
document.addEventListener('DOMContentLoaded', () => {
  try { if (window.frameElement) window.parent.AionSubPageNavigation?.ready(window.frameElement); } catch (_) {}
});

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

// Android activates a new, fully verified bundle first, then asks the page for
// one idle reload. Focused editors postpone it instead of losing user input.
window.addEventListener('aion-client-update-ready', () => {
  if (window.__aionClientUpdateScheduled) return;
  window.__aionClientUpdateScheduled = true;
  const reloadWhenIdle = () => {
    const active = document.activeElement;
    const editing = active && (active.matches?.('input,textarea,select,[contenteditable="true"]')
      || active.tagName === 'IFRAME');
    if (editing || document.querySelector('.editing-focus,.show.active,.settings-overlay.active')) {
      setTimeout(reloadWhenIdle, 1500);
      return;
    }
    location.reload();
  };
  setTimeout(reloadWhenIdle, 800);
});

let conversations = [];
let currentConvId = null;
let currentMessages = [];
let serverMessageIds = new Set();
let models = [];
const DEPRECATED_MODEL_PROVIDERS = new Set(["gemini_cli", "antigravity_cli"]);
let sending = false;
let streamingAiId = null;
let _abortController = null;  // 用于中止 AI 生成
let camCheckMsgId = null;
let poiSearchMsgId = null;
let poiSearchCategories = null;
let chatroomConfig = {};

// 客户端唯一 ID（持久化）— 不用 crypto.randomUUID() 因为 WebView 非安全上下文不支持
const _clientId = localStorage.getItem('aion_client_id') || (() => {
  const a = new Uint8Array(16); crypto.getRandomValues(a);
  a[6] = (a[6] & 0x0f) | 0x40; a[8] = (a[8] & 0x3f) | 0x80;
  const h = [...a].map(b => b.toString(16).padStart(2, '0')).join('');
  const id = `${h.slice(0,8)}-${h.slice(8,12)}-${h.slice(12,16)}-${h.slice(16,20)}-${h.slice(20)}`;
  localStorage.setItem('aion_client_id', id); return id;
})();
let ws = null;
let pendingAttachments = [];  // [{url, type, name}]
let worldBook = { ai_persona: "", user_persona: "", ai_name: "AI", user_name: "你" };
let msgDebugData = {};  // { msgId: { model, recalled_memories, prompt_messages, prompt_count, usage } }
let systemLogs = [];    // 系统日志（会话级，刷新清空）
let msgMusicCards = {}; // { msgId: [{ id, name, artist, album, cover, audio_url, candidates }] }
let hasMoreMessages = false;   // 是否还有更早的消息可加载
let loadingMore = false;       // 防止重复加载
let _suppressScrollBottom = false; // 星标跳转时抑制自动滚底
const MSG_PAGE_SIZE = 50;
const PRIVATE_SYNC_SEQ_KEY = 'aion_sync_seq_v1:chat';
let privateSyncReplayPromise = null;
const $ = id => document.getElementById(id);

window.addEventListener('aion-client-update-ready', () => {
  if (window.__aionClientUpdateScheduled) return;
  window.__aionClientUpdateScheduled = true;
  const reloadWhenIdle = () => {
    const active = document.activeElement;
    const editing = active && (active.matches?.('input,textarea,select,[contenteditable="true"]')
      || active.tagName === 'IFRAME');
    if (sending || streamingAiId || editing || voiceInCall
        || (typeof videoCall !== 'undefined' && videoCall.active)) {
      setTimeout(reloadWhenIdle, 1500);
      return;
    }
    location.reload();
  };
  setTimeout(reloadWhenIdle, 800);
});

// ── 收发消息音效 ──
const sndSend = new Audio('/public/发送消息.mp3');
const sndRecv = new Audio('/public/收到消息.mp3');
sndSend.preload = 'auto';
sndRecv.preload = 'auto';
// 在首次用户交互时解锁音频（部分浏览器/WebView 要求）
let _audioUnlocked = false;
function _unlockAudio() {
  if (_audioUnlocked) return;
  _audioUnlocked = true;
  sndSend.load();
  sndRecv.load();
  // 播放静音片段解锁
  sndSend.volume = 0; sndSend.play().then(() => { sndSend.pause(); sndSend.currentTime = 0; sndSend.volume = 1; }).catch(() => { sndSend.volume = 1; });
  sndRecv.volume = 0; sndRecv.play().then(() => { sndRecv.pause(); sndRecv.currentTime = 0; sndRecv.volume = 1; }).catch(() => { sndRecv.volume = 1; });
  document.removeEventListener('click', _unlockAudio);
  document.removeEventListener('touchstart', _unlockAudio);
}
document.addEventListener('click', _unlockAudio);
document.addEventListener('touchstart', _unlockAudio);
function playSend() { sndSend.currentTime = 0; sndSend.play().catch(() => {}); }
function playRecv() { sndRecv.currentTime = 0; sndRecv.play().catch(() => {}); }

function applyAionTheme(theme) {
  const next = theme === 'light' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  document.body.dataset.theme = next;
  localStorage.setItem('aion_chat_theme', next);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', next === 'dark' ? '#050923' : '#eef3ff');
  // 通知原生 App 切换状态栏图标颜色
  if (window.AionStatusBar) window.AionStatusBar.setBarStyle(next);
}

function toggleAionTheme() {
  applyAionTheme(document.body.dataset.theme === 'light' ? 'dark' : 'light');
}

applyAionTheme(localStorage.getItem('aion_chat_theme') || 'dark');
window.addEventListener('storage', e => {
  if (e.key === 'aion_chat_theme') applyAionTheme(e.newValue || 'dark');
});

// ── 初始化 ──
async function init() {
  models = await api("GET", "/api/models");
  renderModelSelect();
  worldBook = await api("GET", "/api/worldbook");
  try { chatroomConfig = await api("GET", "/api/chatroom/config"); } catch(e) { chatroomConfig = {}; }
  conversations = await api("GET", "/api/conversations");
  const initParams = new URLSearchParams(location.search);
  const targetConvId = initParams.get('conv');
  const targetMsgId = initParams.get('msg');
  const lastId = localStorage.getItem('aion_last_conv');
  if (targetConvId && conversations.find(c => c.id === targetConvId)) {
    await selectConv(targetConvId);
    if (targetMsgId) setTimeout(() => jumpToChatMessage(targetConvId, targetMsgId), 100);
  } else if (lastId && conversations.find(c => c.id === lastId)) {
    await selectConv(lastId);
  } else {
    renderConvList();
    renderMessages();
  }
  // 恢复上下文数量设置
  const savedCtx = localStorage.getItem('aion_context_limit');
  if (savedCtx) { $("contextSlider").value = savedCtx; $("contextValue").textContent = savedCtx + '条'; }
  // 恢复温度设置
  const savedTemp = localStorage.getItem('aion_temperature');
  if (savedTemp) { $("tempSlider").value = savedTemp; $("tempValue").textContent = savedTemp; }
  // 恢复最大回复长度设置
  const savedMaxTokens = localStorage.getItem('aion_max_tokens');
  if (savedMaxTokens) { $("maxTokensSlider").value = savedMaxTokens; const v = parseInt(savedMaxTokens); $("maxTokensValue").textContent = v === 0 ? '不限' : v; }
  connectWS();
  // 滚动到顶部自动加载更早消息
  $("messages").addEventListener("scroll", function() {
    if (this.scrollTop < 80) loadOlderMessages();
  });
  // 请求系统通知权限
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }
}

function escHtml(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
function renderInnerMonologues(html) {
  return String(html || '').replace(/\[心里嘀咕[：:]\s*([^\]]+?)\]/g, (_, content) =>
    `<span class="inner-monologue">${content.trim()}</span>`
  );
}
function innerMonologueText(s) {
  const match = String(s || '').match(/^\s*\[心里嘀咕[：:]\s*([^\]]+?)\]\s*$/);
  return match ? match[1].trim() : null;
}
function splitInnerMonologueParts(s) {
  const items = [];
  const text = String(s || '');
  const monologueRe = /\[心里嘀咕[：:]\s*([^\]]+?)\]/g;
  let last = 0;
  let match;
  while ((match = monologueRe.exec(text)) !== null) {
    const before = text.slice(last, match.index).trim();
    if (before) items.push({ type: 'bubble', text: before });
    const thought = (match[1] || '').trim();
    if (thought) items.push({ type: 'monologue', text: thought });
    last = monologueRe.lastIndex;
  }
  const tail = text.slice(last).trim();
  if (tail) items.push({ type: 'bubble', text: tail });
  return items;
}
function hasInnerMonologue(s) {
  return /\[心里嘀咕[：:]\s*[^\]]+?\]/.test(String(s || ''));
}
function renderMsgPart(p) {
  return splitInnerMonologueParts(p).map(item => {
    if (item.type === 'monologue') return `<div class="inner-monologue-line">${escHtml(item.text)}</div>`;
    return `<div class="msg-bubble">${formatMsg(item.text)}</div>`;
  }).join('');
}
function renderBandVibrationNote(atts) {
  const note = (Array.isArray(atts) ? atts : []).find(item =>
    item && typeof item === 'object' && item.type === 'band_vibration'
  );
  if (!note) return '';
  const fallback = note.note
    ? `${note.pattern === 'call' ? '手环呼唤：' : '手环轻震：'}${note.note}`
    : (note.pattern === 'call'
      ? '手环震动：紧急呼叫！'
      : '手环震动：轻轻想了你一下');
  return `<div class="band-vibration-line">${escHtml(note.label || fallback)}</div>`;
}
function formatMsg(s) {
  // 先转义 HTML，再将 [[image:path]] 标记渲染为 <img>
  const escaped = escHtml(s);
  // 渲染 [转账给XXX：N元] 或 [转账：N元] 为微信风格卡片
  const transferRe = /\[\u8f6c\u8d26(?:\u7ed9([^\uff1a:]+?))?[\uff1a:]\s*(-?\d+(?:\.\d+)?)\s*\u5143\]/g;
  const aiName = (worldBook && worldBook.ai_name) || 'AI';
  const userName = (worldBook && worldBook.user_name) || '你';
  let processed = escaped.replace(transferRe, (match, recipient, amount) => {
    const val = parseFloat(amount);
    const isNeg = val < 0;
    const absVal = Math.abs(val);
    const targetName = recipient ? recipient.trim() : '';
    if (isNeg) {
      // 负数 = 钱包扣除
      return `<div class="transfer-card deduct"><div class="transfer-card-icon-wrap"><svg viewBox="0 0 40 40" width="28" height="28"><circle cx="20" cy="20" r="18" fill="none" stroke="#fff" stroke-width="2.5"/><line x1="14" y1="14" x2="26" y2="26" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/><line x1="26" y1="14" x2="14" y2="26" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/></svg></div><div class="transfer-card-body"><div class="transfer-card-amount">¥${absVal}</div><div class="transfer-card-desc">钱包扣除${targetName ? '（' + targetName + '）' : ''}</div></div><div class="transfer-card-footer">扣除</div></div>`;
    } else {
      // 正数 = 转账
      const descText = targetName ? `转账给${targetName}` : '发起了一笔转账';
      return `<div class="transfer-card"><div class="transfer-card-icon-wrap"><svg viewBox="0 0 40 40" width="28" height="28"><circle cx="20" cy="20" r="18" fill="none" stroke="#fff" stroke-width="2.5"/><path d="M12 17h12M24 17l-3-3" stroke="#fff" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M28 23H16M16 23l3 3" stroke="#fff" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></div><div class="transfer-card-body"><div class="transfer-card-amount">¥${absVal}</div><div class="transfer-card-desc">${descText}</div></div><div class="transfer-card-footer">转账</div></div>`;
    }
  });
  // 渲染 [[image:path]]
  const imgRe = /\[\[image:(\S+?)\]\]/g;
  let result = '', lastIdx = 0, match;
  while ((match = imgRe.exec(processed)) !== null) {
    result += processed.slice(lastIdx, match.index).replace(/\n/g, '<br>');
    const safeUrl = match[1];
    result += `<img class="cr-inline-img" src="${safeUrl}" ${imageInteractionAttrs()} loading="lazy" style="max-width:100%;border-radius:8px;cursor:pointer;margin:4px 0">`;
    lastIdx = imgRe.lastIndex;
  }
  result += processed.slice(lastIdx).replace(/\n/g, '<br>');
  return renderInnerMonologues(result);
}

// ── 配置弹窗 ──
function toggleConfig(e) {
  e.stopPropagation();
  $("configPopup").classList.toggle("show");
}
document.addEventListener("click", e => {
  const p = $("configPopup");
  if (p.classList.contains("show") && !p.contains(e.target)) p.classList.remove("show");
});

// 同步温度到后端 settings
async function syncTemperature() {
  const t = parseFloat($("tempSlider").value);
  await api("PUT", "/api/settings/temperature", { temperature: t });
}

// ── 语音唤醒通话模式 ──
let voiceEnabled = false;
let voiceInCall = false;
let voiceMicSource = 'local'; // 'local' = PC后端 sounddevice, 'remote' = 手机 getUserMedia

function isRemoteVoice() { return voiceMicSource === 'remote'; }

function onMicSourceChange() {
  const newSrc = $('voiceMicSource').value;
  // 如果正在运行，先关闭
  if (voiceEnabled) {
    $('voiceToggle').checked = false;
    if (voiceMicSource === 'remote') remoteVoice.stop();
    else fetch('/api/voice/toggle', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({enabled:false}) });
    voiceEnabled = false;
  }
  voiceMicSource = newSrc;
  localStorage.setItem('aion_voice_mic_source', newSrc);
  $('voiceStatus').textContent = '未开启';
}

async function toggleVoice() {
  const enabled = $('voiceToggle').checked;
  const wakeWord = $('voiceWakeWord').value.trim() || '老公';
  localStorage.setItem('aion_voice_enabled', enabled);
  localStorage.setItem('aion_voice_wakeword', wakeWord);

  if (isRemoteVoice()) {
    // 手机麦克风模式 — 全部在前端处理
    if (enabled) {
      await remoteVoice.start(wakeWord);
    } else {
      remoteVoice.stop();
    }
  } else {
    // PC 后端模式 — 调后端 API
    try {
      await fetch('/api/voice/toggle', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ enabled, wake_word: wakeWord })
      });
      voiceEnabled = enabled;
      $('voiceStatus').textContent = enabled ? '正在校准...' : '未开启';
    } catch(e) { console.error('Voice toggle error:', e); }
  }
}

function updateVoiceWakeWord() {
  const ww = $('voiceWakeWord').value.trim();
  localStorage.setItem('aion_voice_wakeword', ww);
  if (voiceEnabled) toggleVoice();
}

function voiceHangup() {
  if (isRemoteVoice()) {
    remoteVoice.hangup();
  } else {
    fetch('/api/voice/toggle', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ enabled: true, wake_word: $('voiceWakeWord').value.trim() || '老公' })
    });
  }
}

// 通知语音模块 AI 说话状态（自动分发到 local 或 remote 或视频通话）
function notifyVoiceAiSpeaking(speaking) {
  // 视频通话模式优先
  if (typeof videoCall !== 'undefined' && videoCall.active) {
    videoCall.setAiSpeaking(speaking);
    return;
  }
  if (isRemoteVoice() && remoteVoice.enabled) {
    remoteVoice.setAiSpeaking(speaking);
  } else {
    fetch('/api/voice/ai-speaking', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ speaking })
    });
  }
}

function notifyVoiceCamCheckStart() {
  if (isRemoteVoice() && remoteVoice.enabled) {
    remoteVoice.aiSpeaking = true;
  } else {
    fetch('/api/voice/cam-check-start', { method: 'POST' });
  }
}

function updateVoiceUI(data) {
  const ind = $('voiceIndicator');
  const txt = $('voiceIndicatorText');
  const btn = $('voiceHangupBtn');
  const status = $('voiceStatus');

  if (!data.enabled) {
    voiceEnabled = false;
    voiceInCall = false;
    ind.className = 'voice-indicator';
    btn.style.display = 'none';
    if (status) status.textContent = '未开启';
    return;
  }

  voiceEnabled = true;
  ind.classList.add('active');

  switch (data.status) {
    case 'calibrating':
      ind.className = 'voice-indicator active waiting';
      txt.textContent = '🔧 校准环境噪音...';
      btn.style.display = 'none';
      if (status) status.textContent = '校准中...';
      break;
    case 'waiting':
      ind.className = 'voice-indicator active waiting';
      txt.textContent = '🎙 等待唤醒词「' + (data.wake_word || '老公') + '」...';
      btn.style.display = 'none';
      voiceInCall = false;
      if (status) status.textContent = '监听中';
      break;
    case 'wakeup':
      ind.className = 'voice-indicator active in-call';
      txt.textContent = '📞 唤醒成功！通话开始';
      btn.style.display = 'inline-block';
      voiceInCall = true;
      if (status) status.textContent = '通话中';
      // 播放唤醒回复音频
      playWakeupReply();
      break;
    case 'listening_cmd':
      ind.className = 'voice-indicator active in-call';
      txt.textContent = '🎧 聆听中... (停顿3秒结束一句话)';
      btn.style.display = 'inline-block';
      voiceInCall = true;
      break;
    case 'recognizing':
      ind.className = 'voice-indicator active in-call';
      txt.textContent = '💬 识别中...';
      break;
    case 'ai_thinking':
      ind.className = 'voice-indicator active ai-speaking';
      txt.textContent = '🤖 AI 思考中...';
      break;
    case 'hangup':
      ind.className = 'voice-indicator active waiting';
      txt.textContent = '📞 ' + (data.message || '通话结束');
      btn.style.display = 'none';
      voiceInCall = false;
      if (status) status.textContent = '监听中';
      setTimeout(() => {
        if (voiceEnabled && !voiceInCall) {
          txt.textContent = '🎙 等待唤醒词...';
        }
      }, 2000);
      break;
    default:
      txt.textContent = data.message || '语音监听中...';
  }
}

async function playWakeupReply() {
  // 播放唤醒应答音频
  try {
    const audio = new Audio('/public/AIonResponse.mp3');
    notifyVoiceAiSpeaking(true);
    audio.onended = () => { notifyVoiceAiSpeaking(false); };
    audio.onerror = () => { notifyVoiceAiSpeaking(false); };
    await audio.play().catch(() => { notifyVoiceAiSpeaking(false); });
  } catch(e) {
    notifyVoiceAiSpeaking(false);
  }
}

// ══════════════════════════════════════════════════
// ── RemoteVoice: 手机麦克风录音 + VAD + ASR ──
// ══════════════════════════════════════════════════
const remoteVoice = {
  enabled: false,
  inCall: false,
  aiSpeaking: false,
  wakeWord: '老公',
  _stream: null,
  _ctx: null,
  _processor: null,
  _sampleRate: 48000,
  _useNative: false,  // true = Android App 原生桥接
  // VAD state
  _frames: [],
  _speechN: 0,
  _silenceN: 0,
  _isRecording: false,
  _waitN: 0,
  _processing: false,
  // 噪音基线（前 20 帧自动校准）
  _noiseFloor: 0.005,
  _calibFrames: [],
  _calibrated: false,

  async start(wakeWord) {
    if (this.enabled) return;
    this.wakeWord = wakeWord;
    this.inCall = false;
    this.aiSpeaking = false;
    this._processing = false;
    this._calibrated = false;
    this._calibFrames = [];
    this._useNative = false;

    // 优先用 Android 原生桥接（不需要 HTTPS）
    if (window.AionAudio) {
      const ok = window.AionAudio.start();
      if (ok) {
        this._useNative = true;
        this._sampleRate = 16000;
        this._resetVAD();
        this.enabled = true;
        voiceEnabled = true;
        updateVoiceUI({ enabled: true, status: 'calibrating' });
        console.log('[RemoteVoice] Started with native AudioBridge, 16kHz');
        return;
      }
      console.warn('[RemoteVoice] Native bridge start failed, trying getUserMedia...');
    }

    // 回退到 getUserMedia（需要 HTTPS 安全上下文）
    try {
      this._stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
      });
    } catch(e) {
      console.error('[RemoteVoice] getUserMedia failed:', e);
      alert('无法访问麦克风。如果在 App 外使用，需要 HTTPS 页面才能调用麦克风。');
      $('voiceToggle').checked = false;
      return;
    }

    this._ctx = new (window.AudioContext || window.webkitAudioContext)();
    this._sampleRate = this._ctx.sampleRate;
    const source = this._ctx.createMediaStreamSource(this._stream);
    this._processor = this._ctx.createScriptProcessor(2048, 1, 1);
    this._resetVAD();
    this.enabled = true;
    voiceEnabled = true;

    this._processor.onaudioprocess = (e) => this._onAudioFrame(e.inputBuffer.getChannelData(0));
    source.connect(this._processor);
    this._processor.connect(this._ctx.destination);

    updateVoiceUI({ enabled: true, status: 'calibrating' });
    console.log(`[RemoteVoice] Started with getUserMedia, sampleRate=${this._sampleRate}`);
  },

  stop() {
    this.enabled = false;
    this.inCall = false;
    this.aiSpeaking = false;
    voiceEnabled = false;
    voiceInCall = false;
    if (this._useNative && window.AionAudio) {
      window.AionAudio.stop();
      this._useNative = false;
    }
    if (this._processor) { this._processor.disconnect(); this._processor = null; }
    if (this._ctx) { this._ctx.close().catch(()=>{}); this._ctx = null; }
    if (this._stream) { this._stream.getTracks().forEach(t => t.stop()); this._stream = null; }
    updateVoiceUI({ enabled: false, status: 'off' });
    console.log('[RemoteVoice] Stopped');
  },

  hangup() {
    this.inCall = false;
    this.aiSpeaking = false;
    this._resetVAD();
    updateVoiceUI({ enabled: true, status: 'hangup', message: '手动挂断' });
    setTimeout(() => {
      if (this.enabled) updateVoiceUI({ enabled: true, status: 'waiting', wake_word: this.wakeWord });
    }, 2000);
  },

  setAiSpeaking(speaking) {
    this.aiSpeaking = speaking;
    if (!speaking && this.inCall && !this._processing) {
      this._resetVAD();
      updateVoiceUI({ enabled: true, status: 'listening_cmd', message: '聆听中...' });
    }
  },

  _resetVAD() {
    this._frames = [];
    this._speechN = 0;
    this._silenceN = 0;
    this._isRecording = false;
    this._waitN = 0;
  },

  // Android 原生桥接推送的音频帧（由 Java evaluateJavascript 调用）
  _onNativeChunk(b64) {
    if (!this.enabled || this._processing) return;
    // 视频通话进行中时，音频由 videoCall 模块处理，这里跳过
    if (typeof videoCall !== 'undefined' && videoCall.active) return;
    // 解码 base64 → Int16 → Float32
    const binary = atob(b64);
    const len = binary.length / 2;
    const float32 = new Float32Array(len);
    for (let i = 0; i < len; i++) {
      const lo = binary.charCodeAt(i * 2);
      const hi = binary.charCodeAt(i * 2 + 1);
      const int16 = (hi << 8) | lo;
      float32[i] = int16 >= 32768 ? (int16 - 65536) / 32768 : int16 / 32768;
    }
    this._onAudioFrame(float32);
  },

  // 统一音频处理入口（getUserMedia 和原生桥接共用）
  _onAudioFrame(input) {
    if (!this.enabled || this._processing) return;
    // 视频通话进行中时跳过
    if (typeof videoCall !== 'undefined' && videoCall.active) return;
    const energy = input.reduce((s, v) => s + Math.abs(v), 0) / input.length;

    // 校准阶段：前 20 帧（约 0.85 秒）采集噪音基线
    if (!this._calibrated) {
      this._calibFrames.push(energy);
      if (this._calibFrames.length >= 20) {
        const avg = this._calibFrames.reduce((a, b) => a + b, 0) / this._calibFrames.length;
        this._noiseFloor = Math.max(avg * 2.5, 0.003);  // 噪音的 2.5 倍作为阈值，最低 0.003
        this._calibrated = true;
        console.log(`[RemoteVoice] Calibrated: noiseFloor=${this._noiseFloor.toFixed(5)}`);
        updateVoiceUI({ enabled: true, status: 'waiting', wake_word: this.wakeWord });
      }
      return;
    }

    // AI 在说话时跳过
    if (this.aiSpeaking) { this._resetVAD(); return; }

    const isSpeech = energy > this._noiseFloor;

    // 每帧约 2048/48000 = 42.7ms
    // silenceLimit: 唤醒 ~0.85s(20帧), 通话 ~1.5s(35帧)
    // waitLimit: 唤醒 ~15s(350帧), 通话 ~60s(1400帧)
    const silenceLimit = this.inCall ? 35 : 20;
    const waitLimit = this.inCall ? 1400 : 350;

    if (!this._isRecording) {
      if (isSpeech) {
        this._speechN++;
        this._frames.push(new Float32Array(input));
        if (this._speechN >= 8) {  // ~340ms 确认是语音
          this._isRecording = true;
          this._silenceN = 0;
        }
      } else {
        this._speechN = 0;
        this._frames = [];
        this._waitN++;
        if (this._waitN > waitLimit) {
          if (this.inCall) {
            // 通话超时
            this.inCall = false;
            updateVoiceUI({ enabled: true, status: 'hangup', message: '通话超时结束' });
            setTimeout(() => {
              if (this.enabled) updateVoiceUI({ enabled: true, status: 'waiting', wake_word: this.wakeWord });
            }, 2000);
          }
          this._resetVAD();
        }
      }
    } else {
      this._frames.push(new Float32Array(input));
      if (!isSpeech) {
        this._silenceN++;
        if (this._silenceN > silenceLimit) {
          this._processAudio();
        }
      } else {
        this._silenceN = 0;
      }
      // 最长 30 秒（帧大小: getUserMedia=2048, 原生=640）
      const frameSize = this._useNative ? 640 : 2048;
      if (this._frames.length > Math.ceil(30 * this._sampleRate / frameSize)) {
        this._processAudio();
      }
    }
  },

  async _processAudio() {
    if (this._processing) return;
    this._processing = true;

    const frames = this._frames;
    this._resetVAD();

    // 合并帧
    const total = frames.reduce((s, f) => s + f.length, 0);
    const audio = new Float32Array(total);
    let offset = 0;
    for (const f of frames) { audio.set(f, offset); offset += f.length; }

    const duration = total / this._sampleRate;
    // 计算最大振幅（调试用）
    let maxAmp = 0;
    for (let i = 0; i < audio.length; i++) maxAmp = Math.max(maxAmp, Math.abs(audio[i]));
    console.log(`[RemoteVoice] Recorded ${duration.toFixed(1)}s, ${total} samples, maxAmp=${maxAmp.toFixed(4)}, sr=${this._sampleRate}, native=${this._useNative}`);
    if (duration < 0.3) { this._processing = false; return; }

    // 转 WAV
    const wav = this._encodeWAV(audio);
    console.log(`[RemoteVoice] WAV size: ${wav.byteLength} bytes`);

    updateVoiceUI({ enabled: true, status: 'recognizing', message: '识别中...' });

    try {
      const form = new FormData();
      form.append('file', new Blob([wav], { type: 'audio/wav' }), 'audio.wav');
      const resp = await fetch('/api/voice/remote-asr', { method: 'POST', body: form });
      console.log(`[RemoteVoice] ASR response status: ${resp.status}`);
      const data = await resp.json();
      const text = (data.text || '').trim();
      console.log(`[RemoteVoice] ASR result: text="${text}", error=${data.error || 'none'}, inCall=${this.inCall}`);

      if (!text) {
        console.warn(`[RemoteVoice] ASR returned empty text. duration=${duration.toFixed(1)}s, maxAmp=${maxAmp.toFixed(4)}`);
        this._processing = false;
        this._resumeListening();
        return;
      }

      if (!this.inCall) {
        // 待命模式 — 检查唤醒词
        if (text.includes(this.wakeWord)) {
          console.log('[RemoteVoice] Wakeup!');
          this.inCall = true;
          this.aiSpeaking = true;
          updateVoiceUI({ enabled: true, status: 'wakeup', message: '唤醒成功！' });
        } else {
          updateVoiceUI({ enabled: true, status: 'waiting', wake_word: this.wakeWord });
        }
      } else {
        // 通话模式 — 检查挂断
        const hangupWords = ['再见', '拜拜', '挂断', '结束通话', '挂了'];
        if (hangupWords.some(kw => text.includes(kw))) {
          this.inCall = false;
          this.aiSpeaking = false;
          updateVoiceUI({ enabled: true, status: 'hangup', message: '通话结束' });
          await this._sendToChat(text);
          setTimeout(() => {
            if (this.enabled) updateVoiceUI({ enabled: true, status: 'waiting', wake_word: this.wakeWord });
          }, 2000);
        } else {
          // 发送到聊天
          console.log(`[RemoteVoice] Sending to chat: "${text}", currentConvId=${currentConvId}, sending=${sending}`);
          this.aiSpeaking = true;
          updateVoiceUI({ enabled: true, status: 'ai_thinking', message: 'AI 思考中...' });
          await this._sendToChat(text);
        }
      }
    } catch(e) {
      console.error('[RemoteVoice] ASR error:', e);
      // 出错时在状态栏显示错误信息（方便手机端看）
      updateVoiceUI({ enabled: true, status: 'listening_cmd', message: '⚠ ASR出错: ' + (e.message || e) });
    }

    this._processing = false;
  },

  _resumeListening() {
    if (this.inCall) {
      updateVoiceUI({ enabled: true, status: 'listening_cmd', message: '聆听中...' });
    } else {
      updateVoiceUI({ enabled: true, status: 'waiting', wake_word: this.wakeWord });
    }
  },

  async _sendToChat(text) {
    console.log(`[RemoteVoice] _sendToChat: text="${text}", currentConvId=${currentConvId}, sending=${sending}`);

    // 没有当前对话时自动创建一个
    if (!currentConvId) {
      try {
        const conv = await api("POST", "/api/conversations");
        conversations.unshift(conv);
        await selectConv(conv.id);
        console.log(`[RemoteVoice] Auto-created conversation: ${conv.id}`);
      } catch(e) {
        console.error('[RemoteVoice] Failed to create conversation:', e);
        this.aiSpeaking = false;
        this._resumeListening();
        return;
      }
    }

    // 如果上一条还在发送中，等最多 5 秒
    if (sending) {
      let waited = 0;
      while (sending && waited < 5000) {
        await new Promise(r => setTimeout(r, 200));
        waited += 200;
      }
      if (sending) {
        console.warn('[RemoteVoice] Still sending after 5s, skip');
        this.aiSpeaking = false;
        this._resumeListening();
        return;
      }
    }

    $('input').value = text;
    send();
  },

  _encodeWAV(samples) {
    const sr = this._sampleRate;
    const buf = new ArrayBuffer(44 + samples.length * 2);
    const v = new DataView(buf);
    const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    w(0, 'RIFF');
    v.setUint32(4, 36 + samples.length * 2, true);
    w(8, 'WAVE');
    w(12, 'fmt ');
    v.setUint32(16, 16, true);
    v.setUint16(20, 1, true);       // PCM
    v.setUint16(22, 1, true);       // mono
    v.setUint32(24, sr, true);
    v.setUint32(28, sr * 2, true);
    v.setUint16(32, 2, true);
    v.setUint16(34, 16, true);
    w(36, 'data');
    v.setUint32(40, samples.length * 2, true);
    for (let i = 0; i < samples.length; i++) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
    }
    return buf;
  }
};

// 初始化语音设置
(function initVoice() {
  const ww = localStorage.getItem('aion_voice_wakeword') || '老公';
  $('voiceWakeWord').value = ww;
  // 自动检测：App 内或移动端默认用手机麦克风
  const ua = navigator.userAgent;
  const isApp = ua.includes('AionChatApp');
  const isMobile = /Android|iPhone|iPad/i.test(ua);
  const savedSrc = localStorage.getItem('aion_voice_mic_source');
  if (savedSrc) {
    voiceMicSource = savedSrc;
  } else if (isApp || isMobile) {
    voiceMicSource = 'remote';
  }
  $('voiceMicSource').value = voiceMicSource;
})();

// ── 视频通话开关 ──
async function toggleVideoCallEnabled() {
  const enabled = $('videoCallToggle').checked;
  try {
    await fetch('/api/settings/video-call', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ enabled })
    });
  } catch(e) { console.warn('保存视频通话设置失败', e); }
}
(async function initVideoCallToggle() {
  try {
    const r = await fetch('/api/settings/video-call');
    const d = await r.json();
    $('videoCallToggle').checked = !!d.video_call_enabled;
  } catch(e) {}
})();

// ── AI 生图开关 ──
async function toggleImageGenEnabled() {
  const enabled = $('imageGenToggle').checked;
  try {
    await fetch('/api/settings/image-gen', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ enabled })
    });
  } catch(e) { console.warn('保存生图设置失败', e); }
}
(async function initImageGenToggle() {
  try {
    const r = await fetch('/api/settings/image-gen');
    const d = await r.json();
    $('imageGenToggle').checked = !!d.image_gen_enabled;
  } catch(e) {}
})();

// ── CLI 工具调用开关 ──
async function toggleSongGenEnabled() {
  const enabled = $('songGenToggle').checked;
  try {
    await fetch('/api/settings/song-gen', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ enabled })
    });
  } catch(e) { console.warn('保存歌曲生成设置失败', e); }
}
(async function initSongGenToggle() {
  try {
    const r = await fetch('/api/settings/song-gen');
    const d = await r.json();
    $('songGenToggle').checked = !!d.song_gen_enabled;
  } catch(e) {}
})();

async function toggleGeminiCliTools() {
  const enabled = $('geminiCliToolsToggle').checked;
  try {
    await fetch('/api/settings/gemini-cli-tools', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ enabled })
    });
  } catch(e) { console.warn('保存 CLI 工具设置失败', e); }
}
(async function initGeminiCliToolsToggle() {
  try {
    const r = await fetch('/api/settings/gemini-cli-tools');
    const d = await r.json();
    $('geminiCliToolsToggle').checked = !!d.gemini_cli_tools_enabled;
  } catch(e) {}
})();

// ── TTS 语音合成（服务端流式 TTS via WebSocket） ──
let ttsEnabled = localStorage.getItem('aion_tts_enabled') === 'true';
let ttsVoiceId = localStorage.getItem('aion_tts_voice') || '';
let ttsAudio = new Audio();
let ttsPlaying = false;
let ttsResumeTimer = null;
let ttsManualStop = false;
// 分段播放队列：{ msgId: { nextPlay: 0, chunks: {seq: url}, playing: bool } }
let ttsChunkQueues = {};
let ttsPlayOrder = []; // 按到达顺序记录 msgId，确保跨消息顺序播放
let ttsAcceptAfter = Date.now() / 1000;
let ttsPlaybackActiveAt = Date.now() / 1000;
const ttsSuppressedMsgIds = new Set();

function clearTTSResumeTimer() {
  if (ttsResumeTimer) {
    clearTimeout(ttsResumeTimer);
    ttsResumeTimer = null;
  }
}

function scheduleTTSResume() {
  if (ttsManualStop || !ttsPlaying || !ttsAudio.src || ttsAudio.ended || !ttsAudio.paused) return;
  if (ttsResumeTimer) return;
  ttsResumeTimer = setTimeout(() => {
    ttsResumeTimer = null;
    if (ttsManualStop || !ttsPlaying || !ttsAudio.src || ttsAudio.ended || !ttsAudio.paused) return;
    ttsAudio.play().catch(() => {
      scheduleTTSResume();
    });
  }, 1500);
}

function isTTSPlaybackAllowed() {
  return true;
}

function stopLiveTTSQueue() {
  ttsManualStop = true;
  clearTTSResumeTimer();
  ttsAudio.pause();
  ttsAudio.src = '';
  ttsChunkQueues = {};
  ttsPlayOrder = [];
  ttsPlaying = false;
  if (voiceInCall || (typeof videoCall !== 'undefined' && videoCall.active)) {
    notifyVoiceAiSpeaking(false);
  }
  if (window.VoiceCall && window.VoiceCall.handleTTSEnd) {
    window.VoiceCall.handleTTSEnd({ surface: "private" });
  }
}

function suppressTTSMsg(msgId) {
  if (msgId) ttsSuppressedMsgIds.add(msgId);
}

function shouldAcceptTTSMsg(msgId, createdAt, targetClientId) {
  if (!msgId || ttsSuppressedMsgIds.has(msgId)) return false;
  if (targetClientId && targetClientId !== _clientId) return false;
  const ts = Number(createdAt || 0);
  if (ts && ts < ttsAcceptAfter) {
    suppressTTSMsg(msgId);
    return false;
  }
  return true;
}

function _sendTTSState() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({
      type: 'tts_state',
      enabled: ttsEnabled,
      voice: ttsVoiceId,
      can_play: isTTSPlaybackAllowed(),
      active_at: ttsPlaybackActiveAt,
      client_id: _clientId
    }));
  }
}

function refreshTTSPlaybackState() {
  _sendTTSState();
}

let ttsPlaybackStateLastSent = 0;
function bumpTTSPlaybackState() {
  const now = Date.now();
  if (now - ttsPlaybackStateLastSent < 1000) return;
  ttsPlaybackStateLastSent = now;
  ttsPlaybackActiveAt = now / 1000;
  _sendTTSState();
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') refreshTTSPlaybackState();
  else {
    bumpTTSPlaybackState();
    reconcilePrivateSync();
  }
});
window.addEventListener('pagehide', refreshTTSPlaybackState);
window.addEventListener('pageshow', bumpTTSPlaybackState);
document.addEventListener('freeze', refreshTTSPlaybackState);
window.addEventListener('focus', bumpTTSPlaybackState);
document.addEventListener('pointerdown', bumpTTSPlaybackState, { passive: true });
document.addEventListener('keydown', bumpTTSPlaybackState);

function toggleTTS() {
  ttsEnabled = $('ttsToggle').checked;
  localStorage.setItem('aion_tts_enabled', ttsEnabled);
  ttsAcceptAfter = Date.now() / 1000;
  ttsPlaybackActiveAt = ttsAcceptAfter;
  _sendTTSState();
  if (!ttsEnabled) {
    stopLiveTTSQueue();
  }
}

function changeTTSVoice() {
  ttsVoiceId = $('ttsVoiceSelect').value;
  localStorage.setItem('aion_tts_voice', ttsVoiceId);
  ttsPlaybackActiveAt = Date.now() / 1000;
  _sendTTSState();
}

async function refreshTTSVoices() {
  try {
    const data = await api("GET", "/api/tts/voices");
    const sel = $('ttsVoiceSelect');
    if (data.voices && data.voices.length > 0) {
      sel.innerHTML = data.voices.map(v => {
        const name = v.customName || v.uri || 'Unknown';
        return `<option value="${v.uri}" ${v.uri === ttsVoiceId ? 'selected' : ''}>${name}</option>`;
      }).join('');
      // 如果没有选中的音色，默认选第一个
      if (!ttsVoiceId || !data.voices.find(v => v.uri === ttsVoiceId)) {
        ttsVoiceId = data.voices[0].uri;
        localStorage.setItem('aion_tts_voice', ttsVoiceId);
        sel.value = ttsVoiceId;
        _sendTTSState();
      }
    } else {
      sel.innerHTML = '<option value="">无可用音色</option>';
    }
  } catch(e) {
    console.error('刷新TTS音色失败:', e);
  }
}

function privateVoiceCallSpeakerName() {
  return (worldBook && worldBook.ai_name) || "AI";
}

window.PrivateVoiceCallAdapter = {
  getDefaultSpeakerName() {
    return privateVoiceCallSpeakerName();
  },
  getSpeakerName(sender) {
    return sender === "user" ? ((worldBook && worldBook.user_name) || "用户") : privateVoiceCallSpeakerName();
  },
  speakerForMessage() {
    return "assistant";
  },
  async sendText(text) {
    const content = String(text || "").trim();
    if (!content) return;
    if (!currentConvId) {
      const conv = await api("POST", "/api/conversations");
      conversations.unshift(conv);
      await selectConv(conv.id);
    }
    let waited = 0;
    while (sending && waited < 10000) {
      await new Promise(resolve => setTimeout(resolve, 200));
      waited += 200;
    }
    if (sending) throw new Error("上一条消息仍在发送");

    const input = $("input");
    const ttsToggleEl = $("ttsToggle");
    const previousText = input ? input.value : "";
    const previousTtsEnabled = ttsEnabled;
    ttsEnabled = true;
    if (ttsToggleEl) ttsToggleEl.checked = true;
    ttsPlaybackActiveAt = Date.now() / 1000;
    _sendTTSState();
    if (input) {
      input.value = content;
      autoResize(input);
      _updateSendBtnState();
    }
    try {
      await send();
    } finally {
      ttsEnabled = previousTtsEnabled;
      if (ttsToggleEl) ttsToggleEl.checked = previousTtsEnabled;
      ttsPlaybackActiveAt = Date.now() / 1000;
      _sendTTSState();
      if (input && !sending && input.value === content) {
        input.value = previousText;
        autoResize(input);
        _updateSendBtnState();
      }
    }
  }
};

function _notifyVoiceCallPrivateTTSStart(msgId, seq, item) {
  if (!window.VoiceCall || !window.VoiceCall.handleTTSChunkStart) return;
  window.VoiceCall.handleTTSChunkStart({
    surface: "private",
    msgId,
    seq,
    url: item?.url || item,
    text: item?.text || "",
    sender: "assistant",
    speakerName: privateVoiceCallSpeakerName()
  });
}

function _notifyVoiceCallPrivateTTSEnd() {
  if (window.VoiceCall && window.VoiceCall.handleTTSEnd) {
    window.VoiceCall.handleTTSEnd({ surface: "private" });
  }
}

function enqueueTTSChunk(msgId, seq, url, createdAt, targetClientId, text = "") {
  const isChatroomTTS = msgId.startsWith('cm_');
  const voiceCallActive = !!(window.VoiceCall && window.VoiceCall.isActive && window.VoiceCall.isActive());
  if (!isChatroomTTS && !ttsEnabled && !voiceCallActive && !(typeof videoCall !== 'undefined' && videoCall.active)) return;
  // 忽略小剧场的 TTS（tm_ 前缀），避免重复播放
  if (msgId.startsWith('tm_')) return;
  if (!shouldAcceptTTSMsg(msgId, createdAt, targetClientId)) return;
  if (!ttsChunkQueues[msgId]) {
    ttsChunkQueues[msgId] = { nextPlay: 0, chunks: {} };
    ttsPlayOrder.push(msgId);
  }
  ttsChunkQueues[msgId].chunks[seq] = { url, text: text || "" };
  // 通话中时通知语音模块 AI 开始说话
  if ((voiceInCall || (typeof videoCall !== 'undefined' && videoCall.active)) && !ttsPlaying) {
    notifyVoiceAiSpeaking(true);
  }
  if (!ttsPlaying) playNextTTSChunk();
}

async function playNextTTSChunk() {
  const hasChatroomTTS = ttsPlayOrder.some(id => id.startsWith('cm_'));
  const voiceCallActive = !!(window.VoiceCall && window.VoiceCall.isActive && window.VoiceCall.isActive());
  if (!hasChatroomTTS && !ttsEnabled && !voiceCallActive && !(typeof videoCall !== 'undefined' && videoCall.active)) { ttsPlaying = false; return; }

  // 找到当前应该播放的 msgId
  while (ttsPlayOrder.length > 0) {
    const msgId = ttsPlayOrder[0];
    const q = ttsChunkQueues[msgId];
    if (!q) { ttsPlayOrder.shift(); continue; }

    const nextSeq = q.nextPlay;
    let chunk = q.chunks[nextSeq];
    let url = chunk && typeof chunk === "object" ? chunk.url : chunk;
    if (url === undefined) {
      // 如果该消息已标记完成且所有分段已播完，清理并继续下一条
      if (q.finished) {
        const maxSeq = Object.keys(q.chunks).length > 0 ? Math.max(...Object.keys(q.chunks).map(Number)) : -1;
        if (nextSeq > maxSeq) {
          ttsPlayOrder.shift();
          delete ttsChunkQueues[msgId];
          continue; // 继续处理下一条消息
        }
        while (q.nextPlay <= maxSeq && q.chunks[q.nextPlay] === undefined) q.nextPlay++;
        if (q.nextPlay > maxSeq) {
          ttsPlayOrder.shift();
          delete ttsChunkQueues[msgId];
          continue;
        }
        chunk = q.chunks[q.nextPlay];
        url = chunk && typeof chunk === "object" ? chunk.url : chunk;
      }
      if (url === undefined) {
        // 下一个分段还没到，等待
        ttsPlaying = false;
        return;
      }
    }

    // 播放这个分段
    ttsPlaying = true;
    ttsManualStop = false;
    clearTTSResumeTimer();
	    try {
	      ttsAudio.src = url;
	      _notifyVoiceCallPrivateTTSStart(msgId, q.nextPlay, chunk);
	      ttsAudio.onended = () => {
	        clearTTSResumeTimer();
	        ttsPlaying = false;
	        q.nextPlay++;
	        if (window.VoiceCall && window.VoiceCall.handleTTSChunkEnd) {
	          window.VoiceCall.handleTTSChunkEnd({ surface: "private", msgId });
	        }
	        playNextTTSChunk();
	      };
	      ttsAudio.onerror = () => {
	        clearTTSResumeTimer();
	        ttsPlaying = false;
	        q.nextPlay++;
	        if (window.VoiceCall && window.VoiceCall.handleTTSChunkEnd) {
	          window.VoiceCall.handleTTSChunkEnd({ surface: "private", msgId });
	        }
	        playNextTTSChunk();
	      };
      ttsAudio.onplaying = clearTTSResumeTimer;
      ttsAudio.onpause = () => {
        if (ttsAudio.ended) return;
        scheduleTTSResume();
      };
      await ttsAudio.play().catch(() => {
        // 外部 App 抢占音频焦点时，play() 可能会短暂失败；保留当前分片，等待焦点恢复。
        scheduleTTSResume();
      });
      return;
    } catch(e) {
      console.error('TTS chunk播放失败:', e);
      ttsPlaying = false;
      q.nextPlay++;
    }
  }

  // 所有消息播完
  ttsPlaying = false;
	  if (voiceInCall || (typeof videoCall !== 'undefined' && videoCall.active)) {
	    notifyVoiceAiSpeaking(false);
	  }
	  _notifyVoiceCallPrivateTTSEnd();
	}

function finishTTSForMsg(msgId, createdAt, targetClientId) {
  if (targetClientId && targetClientId !== _clientId) return;
  const ts = Number(createdAt || 0);
  if (ttsSuppressedMsgIds.has(msgId) || (ts && ts < ttsAcceptAfter)) {
    ttsSuppressedMsgIds.delete(msgId);
    return;
  }
  // 标记某条消息的 TTS 分段全部到达，如果已经播完所有分段则清理
  const q = ttsChunkQueues[msgId];
  if (!q) return;
  q.finished = true;
  _cleanupFinishedTTS();
}

function _cleanupFinishedTTS() {
  let cleaned = false;
  while (ttsPlayOrder.length > 0) {
    const msgId = ttsPlayOrder[0];
    const q = ttsChunkQueues[msgId];
    if (!q || !q.finished) break;
    // 检查是否所有分段都已播完
    const maxSeq = Object.keys(q.chunks).length > 0 ? Math.max(...Object.keys(q.chunks).map(Number)) : -1;
    if (q.nextPlay > maxSeq) {
      ttsPlayOrder.shift();
      delete ttsChunkQueues[msgId];
      cleaned = true;
    } else {
      break;
    }
  }
  // 清理后如果播放器空闲，重新触发播放流程（可能会走到"所有播完"逻辑）
  if (cleaned && !ttsPlaying) {
    playNextTTSChunk();
  }
}

// 重听 TTS 音频（从服务器缓存播放，支持分段）
let replayAudio = new Audio();
let replayChunks = []; // 当前重听的分段URL列表
let replayIdx = 0;
let replayToken = 0;
let replayDiscoverPromise = null;
async function replayTTS(msgId) {
  try {
    const btn = document.querySelector(`#m_${msgId} .tts-replay-btn`);
    // 如果正在播放同一条，停止
    if (btn && btn.classList.contains('playing')) {
      replayAudio.pause();
      replayAudio.src = '';
      replayChunks = [];
      btn.classList.remove('playing');
      replayToken++;
      replayDiscoverPromise = null;
      return;
    }
    // 停止之前的播放
    replayAudio.pause();
    replayChunks = [];
    document.querySelectorAll('.tts-replay-btn.playing').forEach(b => b.classList.remove('playing'));

    const token = ++replayToken;
    replayChunks = [`/api/tts/audio/${msgId}_s0`];
    replayIdx = 0;
    if (btn) btn.classList.add('playing');
    _playReplayChunk(btn, token);

    replayDiscoverPromise = discoverReplayChunks(msgId).then(chunks => {
      if (token === replayToken && chunks.length) replayChunks = chunks;
      return chunks;
    });
  } catch(e) {
    console.error('重听TTS失败:', e);
  }
}

async function discoverReplayChunks(msgId) {
  const chunks = [];
  for (let i = 0; i < 50; i++) {
    const resp = await fetch(`/api/tts/audio/${msgId}_s${i}`, { method: 'HEAD' });
    if (!resp.ok) break;
    chunks.push(`/api/tts/audio/${msgId}_s${i}`);
  }
  return chunks;
}

function finishReplay(btn) {
  if (btn) btn.classList.remove('playing');
  replayDiscoverPromise = null;
}

function _playReplayChunk(btn, token = replayToken) {
  if (token !== replayToken) return;
  if (replayIdx >= replayChunks.length) {
    if (replayDiscoverPromise) {
      const pending = replayDiscoverPromise;
      replayDiscoverPromise = null;
      pending.then(() => {
        if (token !== replayToken) return;
        if (replayIdx < replayChunks.length) _playReplayChunk(btn, token);
        else finishReplay(btn);
      }).catch(() => finishReplay(btn));
      return;
    }
    finishReplay(btn);
    return;
  }
  replayAudio.src = replayChunks[replayIdx];
  replayAudio.onended = () => { replayIdx++; _playReplayChunk(btn, token); };
  replayAudio.onerror = () => { replayIdx++; _playReplayChunk(btn, token); };
  replayAudio.play().catch((err) => {
    console.warn('[chat TTS] replay chunk play failed', {
      name: err?.name || '',
      message: err?.message || '',
      mediaCode: replayAudio.error?.code || 0,
      mediaMessage: replayAudio.error?.message || '',
      networkState: replayAudio.networkState,
      readyState: replayAudio.readyState,
      src: replayAudio.currentSrc || replayAudio.src || '',
    });
    if (btn) btn.classList.remove('playing');
  });
}


// ── 日程管理 → 已拆分为独立页面 ──
let _alarmQueue = [];
function showAlarmPopup(data) {
  _alarmQueue.push(data);
  if (_alarmQueue.length === 1) _showNextAlarm();
  // 系统通知（即使标签页在后台也能弹出）
  const body = data.origin_name
    ? `【${data.origin_name}】设定的闹铃：${data.content || '日程提醒'}`
    : (data.content || '日程提醒');
  sendSystemNotification('⏰ 闹铃', body);
}
function _showNextAlarm() {
  if (!_alarmQueue.length) return;
  const data = _alarmQueue[0];
  $("alarmContent").textContent = data.origin_name
    ? `【${data.origin_name}】设定的闹铃：${data.content || "日程提醒"}`
    : (data.content || "日程提醒");
  $("alarmTime").textContent = data.trigger_at || "";
  $("alarmOverlay").classList.add("show");
}
function dismissAlarm() {
  $("alarmOverlay").classList.remove("show");
  _alarmQueue.shift();
  if (_alarmQueue.length) setTimeout(_showNextAlarm, 300);
}

async function api(method, url, body, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), Number(options.timeoutMs || 15000));
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
    if (!res.ok) throw new Error(payload?.detail || payload?.error || `请求失败 (${res.status})`);
    return payload;
  } catch(e) {
    if (e?.name === 'AbortError') throw new Error('网络响应超时，请稍后重试');
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

function _dedupeMessagesById(messages) {
  const seen = new Map();
  const out = [];
  for (const msg of messages || []) {
    if (!msg || !msg.id) {
      out.push(msg);
      continue;
    }
    if (seen.has(msg.id)) {
      out[seen.get(msg.id)] = msg;
    } else {
      seen.set(msg.id, out.length);
      out.push(msg);
    }
  }
  return out;
}

function _replaceTempUserIfNeeded(msg) {
  if (!msg || msg.role !== "user") return false;
  const ti = currentMessages.findIndex(m => m.id === "temp_user" && m.conv_id === msg.conv_id);
  if (ti < 0) return false;
  currentMessages[ti] = msg;
  currentMessages = _dedupeMessagesById(currentMessages);
  return true;
}

function upsertCurrentMessage(msg, { replaceTempUser = false } = {}) {
  if (!msg || !msg.id) return "ignored";
  const existingIdx = currentMessages.findIndex(m => m.id === msg.id);
  if (existingIdx >= 0) {
    const existing = currentMessages[existingIdx];
    if (msg.role === "assistant" && msg.content === "..." && existing?.content && existing.content !== "...") {
      return "updated";
    }
    currentMessages[existingIdx] = msg;
    currentMessages = _dedupeMessagesById(currentMessages);
    return "updated";
  }
  if (replaceTempUser && _replaceTempUserIfNeeded(msg)) return "updated";
  currentMessages.push(msg);
  currentMessages = _dedupeMessagesById(currentMessages);
  return "inserted";
}

function setCurrentMessages(messages) {
  currentMessages = _dedupeMessagesById(messages);
  currentMessages.forEach(m => { if (m?.id && !String(m.id).startsWith("temp_")) serverMessageIds.add(m.id); });
}

// ── WebSocket 同步 ──
function rememberPrivateSyncSeq(event) {
  const seq = Number(event?.sync_seq || 0);
  if (!seq) return;
  const current = Number(localStorage.getItem(PRIVATE_SYNC_SEQ_KEY) || 0);
  if (seq > current) localStorage.setItem(PRIVATE_SYNC_SEQ_KEY, String(seq));
}

async function reconcilePrivateSync() {
  if (privateSyncReplayPromise) return privateSyncReplayPromise;
  privateSyncReplayPromise = (async () => {
    let after = Number(localStorage.getItem(PRIVATE_SYNC_SEQ_KEY) || 0);
    for (let batch = 0; batch < 20; batch++) {
      const result = await api('GET', `/api/sync/changes?after=${after}&limit=200`, null, { timeoutMs: 10000 });
      if (result?.reset_required) {
        await refreshCurrentConversationFromServer();
        const latest = Number(result.latest_seq || 0);
        localStorage.setItem(PRIVATE_SYNC_SEQ_KEY, String(latest));
        break;
      }
      const events = Array.isArray(result?.events) ? result.events : [];
      events.forEach(event => {
        // Replayed sync_event messages share the live event shape and dedupe by ID.
        handleSync(event);
        rememberPrivateSyncSeq(event);
        after = Math.max(after, Number(event.sync_seq || 0));
      });
      if (!result?.has_more || !events.length) break;
    }
  })().catch(e => console.warn('[chat sync] reconcile failed:', e))
    .finally(() => { privateSyncReplayPromise = null; });
  return privateSyncReplayPromise;
}

function connectWS() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.onopen = () => {
    ws.send(JSON.stringify({type:'register_client',client_id:_clientId}));
    _sendTTSState();
    reconcilePrivateSync();
  };
  ws.onmessage = e => {
    const event = JSON.parse(e.data);
    rememberPrivateSyncSeq(event);
    handleSync(event);
  };
  ws.onclose = () => setTimeout(connectWS, 2000);
}

function handleSync(msg) {
  const { type, data } = msg;

  if (type === "conv_created") {
    if (!conversations.find(c => c.id === data.id)) {
      conversations.unshift(data);
      renderConvList();
    }
  } else if (type === "conv_updated") {
    const c = conversations.find(c => c.id === data.id);
    if (c) { Object.assign(c, data); renderConvList(); }
    if (data.id === currentConvId && data.title) $("chatTitle").textContent = data.title;
  } else if (type === "conv_deleted") {
    conversations = conversations.filter(c => c.id !== data.id);
    renderConvList();
    if (data.id === currentConvId) { currentConvId = null; currentMessages = []; localStorage.removeItem('aion_last_conv'); renderMessages(); }
  } else if (type === "msg_created") {
    const wasServerCounted = serverMessageIds.has(data.id);
    serverMessageIds.add(data.id);
    if (data.conv_id === currentConvId) {
      // 正在流式的 AI 消息 → 用完整内容替换
      if (data.id === streamingAiId) {
        upsertCurrentMessage(data);
        streamingAiId = null;
        renderMessages();
      }
      // 临时用户消息被真实消息替换
      else if (currentMessages.find(m => m.id === "temp_user") && data.role === "user") {
        upsertCurrentMessage(data, { replaceTempUser: true });
        renderMessages();
      }
      // 其他端发来的新消息（含 Core 主动发言 / 语音唤醒）
      else if (!currentMessages.find(m => m.id === data.id)) {
        upsertCurrentMessage(data);
        playRecv();
        // CAM_CHECK 响应到达：收到 assistant 消息时关闭「正在查看监控」提示
        if (data.role === 'assistant' && camCheckMsgId) dismissCamCheckIndicator();
        if (data.role === 'assistant' && poiSearchMsgId) dismissPoiSearchIndicator();
        if (data.role === 'assistant' && activityCheckMsgId) dismissActivityCheckIndicator();
        if (data.role === 'assistant' && imageGenMsgId) dismissImageGenIndicator();
        renderMessages();
        // TTS 现在由服务端流式推送 tts_chunk，不再需要前端主动合成
        // 语音通话中但 TTS 未启用时，通知语音模块 AI 说完了
        if (data.role === 'assistant' && (voiceInCall || (typeof videoCall !== 'undefined' && videoCall.active)) && !ttsEnabled) {
          notifyVoiceAiSpeaking(false);
        }
      }
      scrollBottom();
    }
    const ci = conversations.findIndex(c => c.id === data.conv_id);
    if (ci >= 0) {
      if (!wasServerCounted && conversations[ci].message_count != null) conversations[ci].message_count++;
      if (ci > 0) { const [c] = conversations.splice(ci, 1); conversations.unshift(c); }
      renderConvList();
    }
  } else if (type === "msg_updated") {
    if (data.conv_id === currentConvId) {
      const mi = currentMessages.findIndex(m => m.id === data.id);
      if (mi >= 0) { currentMessages[mi] = data; renderMessages(); }
    }
  } else if (type === "wish_updated") {
    document.querySelectorAll('.wish-fulfill-card').forEach(card => {
      if (card.dataset.wishId === data.id) applyWishCardStatus(card, data.status || 'active');
    });
  } else if (type === "msg_deleted") {
    if (data.conv_id === currentConvId) {
      currentMessages = currentMessages.filter(m => m.id !== data.id);
      renderMessages();
    }
    const dc = conversations.find(c => c.id === data.conv_id);
    if (dc && dc.message_count != null && dc.message_count > 0) { dc.message_count--; renderConvList(); }
  } else if (type === "file_synced") {
    if (data.conv_id === currentConvId) {
      api("GET", `/api/conversations/${currentConvId}/messages?limit=${MSG_PAGE_SIZE}`).then(msgs => {
        setCurrentMessages(msgs);
        hasMoreMessages = msgs.length >= MSG_PAGE_SIZE;
        renderMessages();
      });
    }
  } else if (type === "voice_state") {
    // 远程模式下忽略后端的语音状态广播（PC sounddevice 的状态不应覆盖手机麦克风的状态）
    if (!isRemoteVoice()) updateVoiceUI(data);
  } else if (type === "cam_check") {
    // 通过 WebSocket 收到 cam_check（语音发送时前端没有 SSE 流）
    if (data.conv_id === currentConvId && !streamingAiId) {
      handleCamCheck(data.conv_id, data.model_key, data.msg_id);
    }
  } else if (type === "poi_search") {
    // 通过 WebSocket 收到 poi_search
    if (data.conv_id === currentConvId && !streamingAiId) {
      handlePoiSearch(data.categories, data.msg_id);
    }
  } else if (type === "activity_check") {
    // 通过 WebSocket 收到 activity_check（语音发送时前端没有 SSE 流）
    if (data.conv_id === currentConvId && !streamingAiId) {
      handleActivityCheck(data.conv_id, data.n, data.msg_id);
    }
  } else if (type === "debug") {
    // 通过 WebSocket 收到 debug 信息（语音发送时前端没有 SSE 流）
    if (data.msg_id && !streamingAiId) {
      msgDebugData[data.msg_id] = data;
      renderDebugBar(data.msg_id);
    }
  } else if (type === "music") {
    // 通过 WebSocket 收到音乐卡片（语音发送 / 闹铃触发 / 定时监控）
    // 忽略来自聊天室的音乐广播（聊天室有自己的播放器）
    if (data.msg_id && !streamingAiId && data.source !== "chatroom") {
      msgMusicCards[data.msg_id] = data.cards;
      renderMusicCards(data.msg_id);
      scrollBottom();
      // autoplay：闹铃/定时监控触发的音乐自动播放第一首
      if (data.autoplay && data.cards && data.cards.length) {
        playMusicOnline(data.cards[0].id);
      }
    }
  } else if (type === "image_gen_start") {
    // 通过 WebSocket 收到生图开始（语音发送时前端没有 SSE 流）
    if (data.conv_id === currentConvId && !streamingAiId) {
      handleImageGenStart(data);
    }
  } else if (type === "image_gen_done") {
    // 生图完成 → 移除占位指示器
    if (data.conv_id === currentConvId) {
      dismissImageGenIndicator();
    }
  } else if (type === "image_gen_failed") {
    // 生图失败 → 移除占位指示器
    if (data.conv_id === currentConvId) {
      dismissImageGenIndicator();
    }
  } else if (type === "song_gen_start") {
    if (data.conv_id === currentConvId && !streamingAiId) {
      handleSongGenStart(data);
    }
  } else if (type === "song_gen_done" || type === "song_gen_failed") {
    if (data.conv_id === currentConvId) {
      dismissSongGenIndicator();
    }
  } else if (type === "schedule_alarm") {
    showAlarmPopup(data);
  } else if (type === "monitor_alert") {
    // 定时监控即将触发，播放提示音
    const audio = new Audio('/public/AionMonitoralart.mp3');
    audio.play().catch(() => {});
    const body = data.origin_name
      ? `【${data.origin_name}】设定的监督：${data.content || '哨兵监控即将分析'}`
      : (data.content || '哨兵监控即将分析');
    sendSystemNotification('📷 监控提醒', body);
  } else if (type === "schedule_changed") {
    // 日程管理已拆分为独立页面
  } else if (type === "moment_new") {
    // 朋友圈动态已移至朋友圈页面，不在聊天界面展示
  } else if (type === "memory_record") {
    // 通过 WebSocket 收到记忆录入
    if (data.msg_id && !streamingAiId) {
      showMemoryRecordHint(data.msg_id, data.content);
    }
  } else if (type === "tts_chunk") {
    // 服务端流式 TTS 分段音频到达
    enqueueTTSChunk(data.msg_id, data.seq, data.url, data.created_at, data.target_client_id, data.text);
  } else if (type === "tts_done") {
    // 服务端通知该消息的所有 TTS 分段已推送完毕
    finishTTSForMsg(data.msg_id, data.created_at, data.target_client_id);
  } else if (type === "video_call_ring") {
    // AI 发起视频通话 — 定向推送到本客户端
    if (typeof videoCall !== 'undefined') videoCall.aiInitiate(data);
  } else if (type === "gift_pending") {
    // 礼物通知
    _showGiftPopup(data);
  } else if (type === "wallet_update") {
    // 钱包余额变动 → 如果钱包面板打开则刷新
    if ($('walletPanelOverlay').classList.contains('show')) openWalletPanel();
  }
}

// ── 时间 ──
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const now = new Date();
  const diffMs = now - d;
  const time = String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0');
  if (diffMs > 12 * 60 * 60 * 1000) {
    return d.getFullYear() + '-' + (d.getMonth()+1) + '-' + d.getDate() + ' ' + time;
  }
  return time;
}

const LEGACY_COMMAND_SYSTEM_NOTICE_RE = /^【[^】]+】(?:设定了|取消了)/;

function systemNoticeAfterMsgId(m) {
  if (!m || m.role !== "system" || !Array.isArray(m.attachments)) return "";
  const marker = m.attachments.find(a => a && typeof a === "object" && a.type === "system_notice_order" && a.after_msg_id);
  return marker ? String(marker.after_msg_id) : "";
}

function isLegacyCommandSystemNotice(m) {
  if (!m || m.role !== "system" || systemNoticeAfterMsgId(m)) return false;
  return LEGACY_COMMAND_SYSTEM_NOTICE_RE.test((m.content || "").trim());
}

function previousDisplayRole(messages, idx) {
  for (let i = idx - 1; i >= 0; i--) {
    const role = messages[i]?.role;
    if (role && !["system", "cam_user", "cam_log", "cam_trigger"].includes(role)) return role;
  }
  return "";
}

function messagesForDisplay(messages) {
  const out = [];
  const pendingById = new Map();
  let pendingLegacyNotices = [];
  const list = _dedupeMessagesById(messages || []);
  const indexById = new Map(list.map((m, idx) => [m?.id, idx]));

  function appendPendingFor(id) {
    const pending = pendingById.get(id);
    if (pending?.length) {
      out.push(...pending);
      pendingById.delete(id);
    }
    if (pendingLegacyNotices.length) {
      out.push(...pendingLegacyNotices);
      pendingLegacyNotices = [];
    }
  }

  for (let idx = 0; idx < list.length; idx++) {
    const m = list[idx];
    const afterMsgId = systemNoticeAfterMsgId(m);
    if (afterMsgId && indexById.has(afterMsgId) && idx < indexById.get(afterMsgId)) {
      if (!pendingById.has(afterMsgId)) pendingById.set(afterMsgId, []);
      pendingById.get(afterMsgId).push(m);
      continue;
    }

    if (isLegacyCommandSystemNotice(m) && previousDisplayRole(list, idx) !== "assistant") {
      pendingLegacyNotices.push(m);
      continue;
    }

    if (pendingLegacyNotices.length && m?.role !== "assistant") {
      out.push(...pendingLegacyNotices);
      pendingLegacyNotices = [];
    }
    out.push(m);
    if (m?.role === "assistant") appendPendingFor(m.id);
  }
  if (pendingLegacyNotices.length) out.push(...pendingLegacyNotices);
  for (const pending of pendingById.values()) out.push(...pending);
  return out;
}

// ── 渲染 ──
function renderModelSelect() {
  const visibleModels = models.filter(m => !DEPRECATED_MODEL_PROVIDERS.has(m.provider));
  $("modelSelect").innerHTML = visibleModels.map(m =>
    `<option value="${m.key}">${m.key}</option>`
  ).join("");
}

function renderConvList() {
  $("convList").innerHTML = conversations.map(c => {
    const count = c.message_count != null ? c.message_count : '';
    return `
    <div class="conv-item ${c.id === currentConvId ? 'active' : ''}" onclick="selectConv('${c.id}')">
      <span class="title">${escHtml(c.title)}</span>
      ${count !== '' ? `<span class="conv-count">${count}</span>` : ''}
      <button class="rename-btn" onclick="event.stopPropagation(); renameConv('${c.id}')" title="重命名">✎</button>
      <button class="del-btn" onclick="event.stopPropagation(); delConv('${c.id}')" title="删除">✕</button>
    </div>`;
  }).join("");
}

function renderMessages() {
  const el = $("messages");

  if (!currentConvId) {
    el.innerHTML = '<div class="empty-state" style="display:flex"><div class="icon">💬</div><div class="hint">选择或创建一个对话开始聊天</div></div>';
    return;
  }

  if (currentMessages.length === 0) {
    el.innerHTML = '<div class="empty-state" style="display:flex"><div class="icon">✨</div><div class="hint">发送第一条消息开始对话</div></div>';
    return;
  }

  const loadMoreBtn = hasMoreMessages ? '<div class="load-more-bar" onclick="loadOlderMessages()">⬆ 加载更早的消息</div>' : '';
  el.innerHTML = loadMoreBtn + messagesForDisplay(currentMessages).map(m => {
    const isUser = m.role === "user";

    // 隐藏监控相关消息（日志已独立存储）
    if (m.role === "cam_user" || m.role === "cam_log" || m.role === "cam_trigger") {
      return '';
    }

    // 系统提示消息（居中显示）
    if (m.role === "system") {
      const afterMsgId = systemNoticeAfterMsgId(m);
      const afterAttr = afterMsgId ? ` data-after-msg-id="${escHtml(afterMsgId)}"` : "";
      return `
      <div class="msg-row system" id="m_${m.id}" data-msg-id="${m.id}"${afterAttr}>
        <div class="system-notice">
          <span class="system-notice-text">${escHtml(m.content)}</span>
          <button class="msg-dots system-dots" onclick="event.stopPropagation();toggleMsgMenu('${m.id}')">&#8943;</button>
          <div class="msg-menu" id="menu_${m.id}">
            <button onclick="delMsg('${m.id}');closeMsgMenus()">删除</button>
          </div>
        </div>
      </div>`;
    }

    const isAssistant = m.role === "assistant";
    const roleLabel = isUser ? (worldBook.user_name || '你') : (worldBook.ai_name || 'AI');
    const time = m.created_at ? fmtTime(m.created_at) : "";
    const starLabel = m.starred ? '取消星标' : '⭐ 星标';
    const actionsHtml = `${isUser ? `<button onclick="editMsg('${m.id}');closeMsgMenus()">编辑</button>` : `<button onclick="regenerateMsg('${m.id}');closeMsgMenus()">重新生成</button>`}<button onclick="delMsg('${m.id}');closeMsgMenus()">删除</button><button onclick="copyMsg('${m.id}');closeMsgMenus()">复制</button><button onclick="toggleStar('${m.id}');closeMsgMenus()">${starLabel}</button>`;
    const starBadge = m.starred ? '<span class="msg-star-badge">✨</span>' : '';
    const dotsLeft = isUser ? `<button class="msg-dots" onclick="event.stopPropagation();toggleMsgMenu('${m.id}')">&#8943;</button>` : '';
    const dotsRight = !isUser ? `<button class="msg-dots" onclick="event.stopPropagation();toggleMsgMenu('${m.id}')">&#8943;</button>` : '';
    const feedbackHtml = isAssistant ? `<span class="msg-feedback-actions">
      <button class="msg-feedback-btn ${m.ai_feedback_rating === 'like' ? 'active' : ''}" onclick="openMsgFeedback(event,'${m.id}','like')" title="喜欢这条回复">👍</button>
      <button class="msg-feedback-btn ${m.ai_feedback_rating === 'dislike' ? 'active' : ''}" onclick="openMsgFeedback(event,'${m.id}','dislike')" title="不喜欢这条回复">👎</button>
      ${m.reasoning_content ? `<button class="msg-feedback-btn msg-reasoning-btn" onclick="openMsgReasoning(event,'${m.id}')" title="查看思考过程">💭</button>` : ''}
    </span>` : '';
    const messageAttachments = withWishFallbackAttachments(m);
    const bandVibrationHtml = renderBandVibrationNote(messageAttachments);
    const rawDisplayContent = isUser ? (m.content || '') : (m.content || '').replace(/<meta>[\s\S]*?<\/meta>/g, '').trim();
    const displayContent = stripWishFulfillmentMarker(rawDisplayContent).trim();
    const hasVoiceAtt = messageAttachments.some(a => typeof a === 'object' && (a.type === 'voice' || a.type === 'video_clip'));
    const hasWishFulfillmentAtt = messageAttachments.some(a => typeof a === 'object' && a.type === 'wish_fulfillment');
    const hasDateSummaryAtt = messageAttachments.some(a => typeof a === 'object' && a.type === 'date_summary');
    const isEmptyMessage = !displayContent && messageAttachments.length === 0;
    // 转账标签前后强制换行，确保卡片独占一个气泡
    const splitContent = displayContent.replace(/(\[转账(?:给[^\uff1a:]+?)?[：:]\s*-?\d+(?:\.\d+)?\s*元\])/g, '\n$1\n');
    const parts = (isUser ? splitContent.split(/\n+/) : splitContent.split(/\n+/)).filter(p => p.trim());
    let bubblesHtml;
    if (hasDateSummaryAtt) {
      bubblesHtml = `<div class="msg-bubbles date-summary-bubbles">${renderAttachments(messageAttachments)}</div>`;
    } else if (isEmptyMessage) {
      bubblesHtml = '<div class="msg-bubble empty-msg-bubble"></div>';
    } else if (hasWishFulfillmentAtt) {
      const explanationHtml = !isUser
        ? parts.map(renderMsgPart).join('')
        : '';
      bubblesHtml = `<div class="msg-bubbles wish-card-bubbles">${renderAttachments(messageAttachments)}${explanationHtml}</div>`;
    } else if (hasVoiceAtt && !displayContent.trim()) {
      // 纯语音消息：不显示文本气泡，只显示语音气泡
      bubblesHtml = `<div class="msg-bubble" style="background:transparent;padding:0;box-shadow:none;border:none">${renderAttachments(messageAttachments)}</div>`;
    } else if (parts.length > 1) {
      bubblesHtml = '<div class="msg-bubbles">' + parts.map(renderMsgPart).join('') + renderAttachments(messageAttachments) + '</div>';
    } else {
      const monologue = innerMonologueText(displayContent);
      bubblesHtml = monologue !== null || hasInnerMonologue(displayContent)
        ? `<div class="msg-bubbles">${renderMsgPart(displayContent)}${renderAttachments(messageAttachments)}</div>`
        : `<div class="msg-bubble">${formatMsg(displayContent)}${renderAttachments(messageAttachments)}</div>`;
    }
    const avatarSrc = isUser ? '/public/UserIcon.png' : '/public/AIIcon.png';
    const ttsBtn = !isUser ? `<button class="tts-replay-btn" onclick="replayTTS('${m.id}')" title="重听语音">🔊</button>` : '';
    return `
    <div class="msg-row ${m.role}${isEmptyMessage ? ' empty-message' : ''}" id="m_${m.id}" data-msg-id="${m.id}">
      <div class="msg-avatar-col">
        <img class="msg-avatar" src="${avatarSrc}" alt="">
        ${ttsBtn}
      </div>
      <div class="msg-body">
        <div class="msg-role-row">
          ${dotsLeft}<span class="msg-role-name">${roleLabel}</span><span class="msg-time">${time}</span>${dotsRight}${feedbackHtml}${starBadge}
          <div class="msg-menu" id="menu_${m.id}">${actionsHtml}</div>
        </div>
        ${bubblesHtml}
        ${bandVibrationHtml}
      </div>
    </div>`;
  }).join("");
  // 恢复音乐卡片
  for (const mid of Object.keys(msgMusicCards)) {
    renderMusicCards(mid);
  }
  // 恢复 [CAM_CHECK] 加载指示器
  if (camCheckMsgId) {
    const row = document.getElementById('m_' + camCheckMsgId);
    if (row && !row.querySelector('.cam-check-indicator')) {
      const aiName = worldBook.ai_name || 'AI';
      const indicator = document.createElement('div');
      indicator.className = 'cam-check-indicator';
      indicator.id = 'cam_check_loading';
      indicator.innerHTML = `\uD83D\uDCF7 ${escHtml(aiName)} \u6B63\u5728\u67E5\u770B\u76D1\u63A7<span class="cam-dots"><span></span><span></span><span></span></span>`;
      const msgBody = row.querySelector('.msg-body');
      (msgBody || row).appendChild(indicator);
    }
  }
  // 恢复 [POI_SEARCH] 加载指示器
  if (poiSearchMsgId) {
    const row = document.getElementById('m_' + poiSearchMsgId);
    if (row && !row.querySelector('.poi-search-indicator')) {
      const aiName = worldBook.ai_name || 'AI';
      const catText = (poiSearchCategories || []).join('\u3001');
      const indicator = document.createElement('div');
      indicator.className = 'poi-search-indicator';
      indicator.id = 'poi_search_loading';
      indicator.innerHTML = `\uD83D\uDCCD ${escHtml(aiName)} \u6B63\u5728\u641C\u7D22\u9644\u8FD1${escHtml(catText)}<span class="poi-dots"><span></span><span></span><span></span></span>`;
      const msgBody = row.querySelector('.msg-body');
      (msgBody || row).appendChild(indicator);
    }
  }
  // 恢复 [查看动态] 加载指示器
  if (activityCheckMsgId) {
    const row = document.getElementById('m_' + activityCheckMsgId);
    if (row && !row.querySelector('.activity-check-indicator')) {
      const aiName = worldBook.ai_name || 'AI';
      const minutes = (activityCheckN || 6) * 10;
      const indicator = document.createElement('div');
      indicator.className = 'activity-check-indicator';
      indicator.id = 'activity_check_loading';
      indicator.innerHTML = `📊 ${escHtml(aiName)} 正在查看过去${minutes}分钟的动态<span class="activity-dots"><span></span><span></span><span></span></span>`;
      const msgBody = row.querySelector('.msg-body');
      (msgBody || row).appendChild(indicator);
    }
  }
  // 恢复 [SELFIE/DRAW] 生图加载指示器
  if (imageGenMsgId) {
    const row = document.getElementById('m_' + imageGenMsgId);
    if (row && !row.querySelector('.image-gen-indicator')) {
      const aiName = worldBook.ai_name || 'AI';
      const indicator = document.createElement('div');
      indicator.className = 'image-gen-indicator';
      indicator.id = 'image_gen_loading';
      indicator.innerHTML = `🎨 ${escHtml(aiName)} 正在发送图片<span class="ig-dots"><span></span><span></span><span></span></span>`;
      const msgBody = row.querySelector('.msg-body');
      (msgBody || row).appendChild(indicator);
    }
  }
  // Restore [SONG] generation indicator after message re-render.
  if (songGenMsgId) {
    const row = document.getElementById('m_' + songGenMsgId);
    if (row && !row.querySelector('.song-gen-indicator')) {
      const indicator = document.createElement('div');
      indicator.className = 'song-gen-indicator';
      indicator.id = 'song_gen_loading';
      indicator.innerHTML = `歌曲谱写中....<span class="sg-dots"><span></span><span></span><span></span></span>`;
      const msgBody = row.querySelector('.msg-body');
      (msgBody || row).appendChild(indicator);
    }
  }
  // 恢复 [HEART] 心语气泡
  for (const hwMsgId of _heartWhisperMsgIds) {
    _applyHeartHint(hwMsgId);
  }
  // 恢复 [MEMORY] 记忆录入提示
  for (const mrMsgId of _memoryRecordMsgIds) {
    _applyMemoryHint(mrMsgId);
  }
  scrollBottom();
}

function scrollBottom() {
  if (_suppressScrollBottom) return;
  const el = $("messages");
  requestAnimationFrame(() => el.scrollTop = el.scrollHeight);
}

function renderDebugBar(msgId) {
  // 不再在聊天气泡下方渲染，改为写入系统日志
  const d = msgDebugData[msgId];
  if (!d) return;
  addSystemLog(d);
}

function renderDebugMemories(mems) {
  if (!mems || mems.length === 0) return '<h4>🧠 召回记忆</h4><div style="color:var(--text3);font-size:12px;padding:4px 0;">本次未召回任何记忆</div>';
  const items = mems.map(m => `<div class="debug-mem-item"><span class="score">${m.score.toFixed(4)}</span><span class="type">${escHtml(m.type)}</span><span class="content">${escHtml(m.content)}</span></div>`).join('');
  return `<h4>🧠 召回记忆 (${mems.length} 条，按相似度排序)</h4>${items}`;
}

function renderDebugPrompt(msgs, count) {
  if (!msgs || msgs.length === 0) return '';
  const items = msgs.map(m => {
    const roleCls = m.role === 'user' ? 'user' : 'assistant';
    return `<div class="debug-prompt-item"><span class="debug-prompt-role ${roleCls}">[${escHtml(m.role)}]</span> <span class="debug-prompt-text">${escHtml(m.content)}</span></div>`;
  }).join('');
  return `<h4>📝 完整 Prompt (${count} 条消息)</h4><div class="debug-prompt-list">${items}</div>`;
}

function toggleDebugDetail(msgId) {
  const el = document.getElementById(`debugDetail_${msgId}`);
  if (!el) return;
  el.classList.toggle('show');
  const btn = el.previousElementSibling?.querySelector('.debug-toggle');
  if (btn) btn.textContent = el.classList.contains('show') ? '收起 ▴' : '详情 ▾';
}

// ── 系统日志 ──
let sysLogHasUnreadError = false;  // 是否有未读的错误日志

function addSystemLog(d) {
  // 按 msg_id 去重，避免 SSE + WebSocket 双通道导致重复
  if (d.msg_id && systemLogs.some(log => log.msg_id === d.msg_id)) return;
  const now = new Date();
  const ts = String(now.getHours()).padStart(2,'0') + ':' + String(now.getMinutes()).padStart(2,'0') + ':' + String(now.getSeconds()).padStart(2,'0');
  systemLogs.unshift({ ...d, _ts: ts, _id: 'slog_' + Date.now() + '_' + Math.random().toString(36).slice(2,6) });
  // 如果是错误日志，闪烁系统日志按钮
  if (d.has_error) {
    sysLogHasUnreadError = true;
    const btn = $("sysLogBtn");
    if (btn && !btn.classList.contains('syslog-btn-flash')) {
      btn.classList.add('syslog-btn-flash');
    }
  }
  renderSystemLogList();
}

// 添加前端网络错误到系统日志
function addErrorToSystemLog(errorMsg, model) {
  const d = {
    type: 'debug',
    model: model || '?',
    msg_id: null,
    has_error: true,
    error_text: errorMsg,
    usage: null,
    recalled_memories: null,
    prompt_messages: null,
  };
  addSystemLog(d);
}

function _buildTokenHtml(u) {
  if (!u) return '🔤 token 无数据';
  const raw = u.raw;
  let parts = [];
  // 基础 token 信息（使用服务器返回的原始数据）
  if (raw) {
    // Gemini 格式
    if ('promptTokenCount' in raw) {
      parts.push(`<span class="tok-label">输入:</span><span class="tok-value">${raw.promptTokenCount || 0}</span>`);
      if (raw.thoughtsTokenCount) parts.push(`<span class="tok-label">思考:</span><span class="tok-value tok-thinking">${raw.thoughtsTokenCount}</span>`);
      if (raw.cachedContentTokenCount) parts.push(`<span class="tok-label">缓存:</span><span class="tok-value tok-cached">${raw.cachedContentTokenCount}</span>`);
      parts.push(`<span class="tok-label">输出:</span><span class="tok-value">${raw.candidatesTokenCount || 0}</span>`);
      if (raw.toolUsePromptTokenCount) parts.push(`<span class="tok-label">工具:</span><span class="tok-value">${raw.toolUsePromptTokenCount}</span>`);
      parts.push(`<span class="tok-label">总计:</span><span class="tok-value">${raw.totalTokenCount || 0}</span>`);
    }
    // SiliconFlow / OpenAI 格式
    else if ('prompt_tokens' in raw) {
      parts.push(`<span class="tok-label">输入:</span><span class="tok-value">${raw.prompt_tokens || 0}</span>`);
      if (raw.prompt_tokens_details) {
        if (raw.prompt_tokens_details.cached_tokens) parts.push(`<span class="tok-label">缓存:</span><span class="tok-value tok-cached">${raw.prompt_tokens_details.cached_tokens}</span>`);
      }
      parts.push(`<span class="tok-label">输出:</span><span class="tok-value">${raw.completion_tokens || 0}</span>`);
      if (raw.completion_tokens_details) {
        if (raw.completion_tokens_details.reasoning_tokens) parts.push(`<span class="tok-label">推理:</span><span class="tok-value tok-thinking">${raw.completion_tokens_details.reasoning_tokens}</span>`);
      }
      parts.push(`<span class="tok-label">总计:</span><span class="tok-value">${raw.total_tokens || 0}</span>`);
    }
  }
  // 无 raw 数据时使用简化格式
  if (parts.length === 0) {
    parts.push(`<span class="tok-label">输入:</span><span class="tok-value">${u.prompt_tokens || 0}</span>`);
    parts.push(`<span class="tok-label">输出:</span><span class="tok-value">${u.completion_tokens || 0}</span>`);
    parts.push(`<span class="tok-label">总计:</span><span class="tok-value">${u.total_tokens || 0}</span>`);
  }
  return '🔤 ' + parts.join(' ');
}

function _buildTokenDetailHtml(u) {
  if (!u || !u.raw) return '';
  const raw = u.raw;
  let html = '<h4>🔤 Token 用量详情（服务器原始数据）</h4><div class="syslog-token-raw">';
  // 直接展示服务器返回的所有字段
  for (const [k, v] of Object.entries(raw)) {
    if (v === null || v === undefined) continue;
    if (typeof v === 'object') {
      html += `<div><span class="tok-label">${escHtml(k)}:</span> <span class="tok-value">${escHtml(JSON.stringify(v))}</span></div>`;
    } else {
      html += `<div><span class="tok-label">${escHtml(k)}:</span> <span class="tok-value">${v}</span></div>`;
    }
  }
  html += '</div>';
  return html;
}

function _formatSystemLogForCopy(d) {
  const lines = [];
  lines.push(`[${d._ts || ''}] ${d.model || '?'}`);
  if (d.has_error && d.error_text) lines.push(`错误: ${d.error_text}`);
  if (d.recall_topic) lines.push(`话题: ${d.recall_topic}`);
  if (d.recall_keywords) lines.push(`关键词: ${d.recall_keywords}`);
  if (d.recall_query) lines.push(`向量匹配查询:\n${d.recall_query}`);
  if (d.debug_top6 && d.debug_top6.length > 0) {
    lines.push('记忆库 Top6:');
    d.debug_top6.forEach((m, i) => {
      lines.push(`${i + 1}. score=${m.score} vec=${m.vec_sim} kw=${m.kw_score} imp=${m.importance}\n${m.content || ''}`);
    });
  }
  if (d.recalled_memories && d.recalled_memories.length > 0) {
    lines.push('实际召回记忆:');
    d.recalled_memories.forEach((m, i) => {
      lines.push(`${i + 1}. score=${m.score} type=${m.type || ''}\n${m.content || ''}`);
    });
  }
  if (d.prompt_messages && d.prompt_messages.length > 0) {
    lines.push('完整 Prompt:');
    d.prompt_messages.forEach((m, i) => {
      lines.push(`--- ${i + 1}. ${m.role} ---\n${m.content || ''}`);
    });
  }
  return lines.join('\n\n');
}

async function copySystemLogEntry(id) {
  const item = systemLogs.find(log => log._id === id);
  if (!item) return;
  try {
    await navigator.clipboard.writeText(_formatSystemLogForCopy(item));
    alert('已复制本条系统日志全文');
  } catch (e) {
    console.error('复制系统日志失败:', e);
  }
}

async function copyAllSystemLogs() {
  if (!systemLogs.length) return;
  try {
    await navigator.clipboard.writeText(systemLogs.map(_formatSystemLogForCopy).join('\n\n==============================\n\n'));
    alert('已复制全部系统日志');
  } catch (e) {
    console.error('复制全部系统日志失败:', e);
  }
}

function renderSystemLogList() {
  const el = $("sysLogList");
  const countEl = $("sysLogCount");
  if (!el) return;
  if (countEl) countEl.textContent = `共 ${systemLogs.length} 条（刷新后清空）`;
  if (systemLogs.length === 0) {
    el.innerHTML = '<div class="syslog-empty">暂无日志</div>';
    return;
  }
  el.innerHTML = systemLogs.map(d => {
    const u = d.usage;
    const tokenText = _buildTokenHtml(u);
    const isError = d.has_error;
    const memCount = d.recalled_memories ? d.recalled_memories.length : 0;
    const memText = memCount > 0 ? `🧠 召回 ${memCount} 条记忆` : '🧠 无相关记忆';
    const memCls = memCount > 0 ? 'syslog-mem' : 'syslog-mem none';
    const detailId = 'sd_' + d._id;
    // 详情内容
    let detailHtml = '';
    // 错误信息
    if (isError && d.error_text) {
      detailHtml += `<div style="color:#f44336;margin-bottom:8px;word-break:break-all;">⚠️ ${escHtml(d.error_text)}</div>`;
    }
    // Token 详情（服务器原始数据）
    detailHtml += _buildTokenDetailHtml(u);
    // 即时哨兵结果
    if (d.is_search_needed !== undefined) {
      const searchTag = d.is_search_needed ? '<span style="color:#4caf50">✅ 需要搜索</span>' : '<span style="color:#ff9800">⏭️ 无需搜索</span>';
      detailHtml += `<div class="debug-recall-keywords">即时哨兵判断: ${searchTag}</div>`;
    }
    if (d.recall_topic) {
      detailHtml += `<div class="debug-recall-keywords">📌 话题: <span style="color:#4fc3f7">${escHtml(d.recall_topic)}</span></div>`;
    }
    if (d.recall_keywords) {
      detailHtml += `<div class="debug-recall-keywords">🏷️ 关键词: ${escHtml(d.recall_keywords)}</div>`;
    }
    // 向量匹配查询句
    if (d.recall_query) {
      detailHtml += `<h4>🔍 向量匹配查询</h4><div class="debug-recall-query">${escHtml(d.recall_query)}</div>`;
    }
    // 得分最高的 Top6（含未达标）
    if (d.debug_top6 && d.debug_top6.length > 0) {
      const topItems = d.debug_top6.map((m, i) => {
        const passed = m.score >= 0.45;
        return `<div class="debug-mem-item ${passed ? '' : 'below-threshold'}"><span class="score">${m.score.toFixed(4)}</span><span class="score-detail">vec:${m.vec_sim.toFixed(3)} kw:${m.kw_score.toFixed(3)} imp:${m.importance.toFixed(2)}</span><span class="content">${escHtml(m.content)}</span>${!passed ? '<span class="threshold-tag">未达标</span>' : ''}</div>`;
      }).join('');
      detailHtml += `<h4>📊 记忆库 Top6 得分 (阈值 0.45)</h4>${topItems}`;
    }
    if (d.recalled_memories && d.recalled_memories.length > 0) {
      const memItems = d.recalled_memories.map(m => `<div class="debug-mem-item"><span class="score">${m.score.toFixed(4)}</span><span class="type">${escHtml(m.type)}</span><span class="content">${escHtml(m.content)}</span></div>`).join('');
      detailHtml += `<h4>🧠 实际召回记忆 (${d.recalled_memories.length} 条)</h4>${memItems}`;
    }
    if (d.prompt_messages && d.prompt_messages.length > 0) {
      const pmItems = d.prompt_messages.map(m => {
        const roleCls = m.role === 'user' ? 'user' : 'assistant';
        return `<div class="debug-prompt-item"><span class="debug-prompt-role ${roleCls}">[${escHtml(m.role)}]</span> <span class="debug-prompt-text">${escHtml(m.content)}</span></div>`;
      }).join('');
      detailHtml += `<h4>📝 完整 Prompt (${d.prompt_count || d.prompt_messages.length} 条)</h4><div class="debug-prompt-list">${pmItems}</div>`;
    }
    const hasDetail = detailHtml.length > 0;
    const errorTag = isError ? '<span class="syslog-error-tag">❌ 错误</span>' : '';
    const entryCls = isError ? 'syslog-entry error-entry' : 'syslog-entry';
    return `<div class="${entryCls}">
      <span class="syslog-time">${d._ts}</span>
      ${errorTag}
      <span class="syslog-model">📦 ${escHtml(d.model || '?')}</span>
      <span class="syslog-tokens">${tokenText}</span>
      <span class="${memCls}">${memText}</span>
      ${hasDetail ? `<button class="syslog-detail-toggle" onclick="toggleSysLogDetail('${detailId}')">详情 ▾</button>` : ''}
      ${hasDetail ? `<button class="syslog-detail-toggle" onclick="copySystemLogEntry('${d._id}')">复制全文</button>` : ''}
      ${hasDetail ? `<div class="syslog-detail" id="${detailId}">${detailHtml}</div>` : ''}
    </div>`;
  }).join('');
}

function toggleSysLogDetail(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.toggle('show');
  const btn = el.previousElementSibling;
  if (btn && btn.classList.contains('syslog-detail-toggle')) {
    btn.textContent = el.classList.contains('show') ? '收起 ▴' : '详情 ▾';
  }
}

function openSystemLog() {
  // 清除红色闪烁
  sysLogHasUnreadError = false;
  const btn = $("sysLogBtn");
  if (btn) btn.classList.remove('syslog-btn-flash');
  renderSystemLogList();
  $("sysLogModal").classList.add("show");
}
function closeSystemLog() {
  $("sysLogModal").classList.remove("show");
}
function clearSystemLog() {
  systemLogs = [];
  sysLogHasUnreadError = false;
  const btn = $("sysLogBtn");
  if (btn) btn.classList.remove('syslog-btn-flash');
  renderSystemLogList();
}

// ── 星标消息 ──
async function toggleStar(msgId) {
  try {
    const res = await api("PATCH", `/api/messages/${msgId}/star`);
    if (res.error) return;
    // WebSocket broadcast 会自动更新 currentMessages 并 renderMessages
  } catch(e) { console.error('星标切换失败:', e); }
}

let msgFeedbackPopover = null;
let msgReasoningPopover = null;

function closeMsgReasoningPopover() {
  if (msgReasoningPopover) {
    msgReasoningPopover.remove();
    msgReasoningPopover = null;
  }
}

function openMsgReasoning(ev, msgId) {
  ev?.stopPropagation?.();
  closeMsgMenus();
  closeMsgFeedbackPopover();
  closeMsgReasoningPopover();
  const reasoning = currentMessages.find(m => m.id === msgId)?.reasoning_content || '';
  if (!reasoning.trim()) return;
  const pop = document.createElement('div');
  pop.className = 'msg-reasoning-popover';
  const title = document.createElement('div');
  title.className = 'msg-reasoning-title';
  title.textContent = '思考过程';
  const content = document.createElement('div');
  content.className = 'msg-reasoning-content';
  content.textContent = reasoning;
  pop.append(title, content);
  document.body.appendChild(pop);
  msgReasoningPopover = pop;
  const trigger = ev?.currentTarget;
  const rect = trigger?.getBoundingClientRect?.();
  const messageRect = trigger?.closest?.('.msg-row')?.getBoundingClientRect?.() || rect;
  if (rect && messageRect) {
    const pad = 8;
    const width = pop.offsetWidth;
    const availableHeight = Math.max(0, messageRect.top - pad - 6);
    const heightCap = Math.min(window.innerHeight * 0.28, 260, availableHeight);
    pop.style.maxHeight = `${heightCap}px`;
    const height = pop.offsetHeight;
    pop.style.left = `${Math.min(Math.max(pad, rect.left), window.innerWidth - width - pad)}px`;
    pop.style.top = `${Math.max(pad, messageRect.top - height - 6)}px`;
  }
}

function closeMsgFeedbackPopover() {
  if (msgFeedbackPopover) {
    msgFeedbackPopover.remove();
    msgFeedbackPopover = null;
  }
}

function openMsgFeedback(ev, msgId, rating) {
  ev?.stopPropagation?.();
  closeMsgMenus();
  closeMsgFeedbackPopover();
  const msg = currentMessages.find(m => m.id === msgId);
  const label = rating === 'like' ? '喜欢的原因' : '不喜欢的原因';
  const existing = msg?.ai_feedback_rating === rating ? (msg.ai_feedback_reason || '') : '';
  const pop = document.createElement('div');
  pop.className = 'msg-feedback-popover';
  pop.innerHTML = `
    <div class="msg-feedback-title">${label}</div>
    <textarea id="msgFeedbackReason" rows="3" maxlength="600" placeholder="写一点具体原因，之后复盘会用到">${escHtml(existing)}</textarea>
    <div class="msg-feedback-footer">
      <button type="button" class="msg-feedback-cancel" onclick="closeMsgFeedbackPopover()">取消</button>
      <button type="button" class="msg-feedback-submit" onclick="submitMsgFeedback('${msgId}','${rating}')">确认</button>
    </div>`;
  document.body.appendChild(pop);
  msgFeedbackPopover = pop;

  const rect = ev?.currentTarget?.getBoundingClientRect?.();
  const pad = 8;
  if (rect) {
    const width = pop.offsetWidth || 260;
    const height = pop.offsetHeight || 160;
    let left = Math.min(Math.max(pad, rect.left), window.innerWidth - width - pad);
    let top = rect.bottom + 6;
    if (top + height > window.innerHeight - pad) top = Math.max(pad, rect.top - height - 6);
    pop.style.left = `${left}px`;
    pop.style.top = `${top}px`;
  }
  setTimeout(() => pop.querySelector('textarea')?.focus(), 0);
}

async function submitMsgFeedback(msgId, rating) {
  const reason = document.getElementById('msgFeedbackReason')?.value.trim() || '';
  if (!reason) {
    showToast('先写一点原因');
    return;
  }
  try {
    const res = await api("PATCH", `/api/messages/${encodeURIComponent(msgId)}/feedback`, { rating, reason });
    if (res.detail || res.error) {
      showToast(res.detail || res.error || '反馈保存失败');
      return;
    }
    closeMsgFeedbackPopover();
    showToast('反馈已记录');
  } catch (e) {
    console.error('反馈保存失败:', e);
    showToast('反馈保存失败');
  }
}

async function openStarredPanel() {
  closeSidebar();
  try {
    const items = await api("GET", "/api/starred-messages");
    renderStarredList(items);
  } catch(e) { console.error('加载星标失败:', e); }
  $("starredModal").classList.add("show");
}
function closeStarredPanel() { $("starredModal").classList.remove("show"); }

function renderStarredList(items) {
  const el = $("starredList");
  if (!items || items.length === 0) {
    el.innerHTML = '<div class="starred-empty">暂无星标消息</div>';
    return;
  }
  el.innerHTML = items.map(m => {
    const t = m.created_at ? new Date(m.created_at * 1000) : null;
    const timeStr = t ? `${t.getMonth()+1}/${t.getDate()} ${String(t.getHours()).padStart(2,'0')}:${String(t.getMinutes()).padStart(2,'0')}` : '';
    const convTitle = escHtml(m.conv_title || '未知对话');
    const roleLabel = m.role === 'user' ? (worldBook.user_name || '你') : (worldBook.ai_name || 'AI');
    const preview = escHtml((m.content || '').replace(/<meta>[\s\S]*?<\/meta>/g, '').trim().slice(0, 80));
    return `<div class="starred-item" onclick="jumpToStarredMsg('${m.conv_id}','${m.id}')">
      <div class="starred-item-header">
        <span class="starred-item-conv">${convTitle}</span>
        <span style="display:flex;align-items:center;gap:6px">
          <span class="starred-item-time">${timeStr}</span>
          <button class="starred-item-unstar" onclick="event.stopPropagation();unstarFromPanel('${m.id}')" title="取消星标">✕</button>
        </span>
      </div>
      <div class="starred-item-role">${roleLabel}</div>
      <div class="starred-item-preview">${preview}</div>
    </div>`;
  }).join('');
}

async function unstarFromPanel(msgId) {
  await toggleStar(msgId);
  // 刷新面板
  const items = await api("GET", "/api/starred-messages");
  renderStarredList(items);
}

async function jumpToStarredMsg(convId, msgId) {
  closeStarredPanel();
  await jumpToChatMessage(convId, msgId);
}

async function jumpToChatMessage(convId, msgId) {
  _suppressScrollBottom = true;
  try {
    // 如果不在当前对话，先切换
    if (currentConvId !== convId) {
      await selectConv(convId);
    }
    // 检查消息是否已在当前加载的列表中
    const existing = document.getElementById('m_' + msgId);
    if (existing) {
      existing.scrollIntoView({ behavior: 'smooth', block: 'center' });
      existing.classList.add('star-highlight');
      setTimeout(() => existing.classList.remove('star-highlight'), 2000);
      return;
    }
    // 消息不在已加载范围，用 around API 加载
    const msgs = await api("GET", `/api/conversations/${convId}/messages-around/${msgId}?limit=25`);
    if (msgs.length === 0) return;
    setCurrentMessages(msgs);
    hasMoreMessages = true;  // 可能上下都有更多消息
    renderMessages();
    requestAnimationFrame(() => {
      const el = document.getElementById('m_' + msgId);
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        el.classList.add('star-highlight');
        setTimeout(() => el.classList.remove('star-highlight'), 2000);
      }
    });
  } catch(e) { console.error('跳转失败:', e); } finally {
    requestAnimationFrame(() => { _suppressScrollBottom = false; });
  }
}

window.jumpToGlobalSearchTarget = async function(target) {
  if (!target) return;
  if (target.source === 'aion_private') {
    closeSubPage(true);
    await jumpToChatMessage(target.target_id, target.id);
    return;
  }
  if (target.url) {
    openSubPage(target.url);
  }
};

// ── 音乐卡片 ──
function renderMusicCards(msgId) {
  const cards = msgMusicCards[msgId];
  if (!cards || !cards.length) return;
  const row = document.getElementById('m_' + msgId);
  if (!row) return;
  // 有完整卡片时隐藏胶囊
  row.querySelectorAll('.music-capsule').forEach(e => e.style.display = 'none');
  // 移除旧的音乐卡片容器
  row.querySelectorAll('.music-cards-container').forEach(e => e.remove());
  const container = document.createElement('div');
  container.className = 'music-cards-container';
  cards.forEach(song => {
    container.innerHTML += buildMusicCardHtml(song);
  });
  const msgBody = row.querySelector('.msg-body');
  msgBody.appendChild(container);
}

function buildMusicCardHtml(song) {
  const cover = song.cover ? escHtml(song.cover) : '';
  const coverImg = cover ? `<img class="music-cover" src="${cover}" alt="">` : `<div class="music-cover" style="display:flex;align-items:center;justify-content:center;font-size:24px;color:var(--text3)">🎵</div>`;
  const name = escHtml(song.name || '未知歌曲');
  const artist = escHtml(song.artist || '未知歌手');
  const album = song.album ? `<div class="music-album">💿 ${escHtml(song.album)}</div>` : '';
  const songId = song.id;

  // 在线播放按钮（统一走服务端代理推流）
  const onlineBtn = `<button class="music-btn secondary" onclick="playMusicOnline(${songId})">▶ 在线播放</button>`;

  // 备选歌曲
  let candidatesHtml = '';
  if (song.candidates && song.candidates.length) {
    const items = song.candidates.map(c =>
      `<div class="cand-item" onclick="openInNetease(${c.id})">🎵 ${escHtml(c.name)} - ${escHtml(c.artist)}</div>`
    ).join('');
    candidatesHtml = `<details class="music-candidates"><summary>不是这首？看看其他结果</summary>${items}</details>`;
  }

  return `
    <div class="music-card">
      ${coverImg}
      <div class="music-info">
        <div class="music-name">${name}</div>
        <div class="music-artist">${artist}</div>
        ${album}
        <div class="music-btns">
          <button class="music-btn primary" onclick="openInNetease(${songId})">🎶 网易云播放</button>
          ${onlineBtn}
        </div>
        ${candidatesHtml}
      </div>
    </div>`;
}

function openInNetease(songId) {
  window.open('https://music.163.com/song?id=' + songId, '_blank');
}

function playMusicOnline(songId) {
  let wrap = document.getElementById('globalMusicWrap');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.id = 'globalMusicWrap';
    wrap.style.cssText = 'position:fixed;top:calc(max(34px, env(safe-area-inset-top, 0px)) + 48px);left:0;right:0;z-index:999;display:none;align-items:center;gap:8px;background:var(--surface,#1e1e1e);padding:0 12px;height:36px;box-shadow:0 2px 8px rgba(0,0,0,0.25);border-bottom:1px solid var(--border,#333);';

    const playBtn = document.createElement('button');
    playBtn.id = 'globalMusicPlayBtn';
    playBtn.textContent = '⏸';
    playBtn.style.cssText = 'background:none;border:none;font-size:16px;cursor:pointer;color:var(--text,#eee);padding:0 4px;line-height:1;flex-shrink:0;';

    const bar = document.createElement('input');
    bar.id = 'globalMusicBar';
    bar.type = 'range'; bar.min = 0; bar.max = 1000; bar.value = 0;
    bar.style.cssText = 'flex:1;height:4px;accent-color:#e53935;cursor:pointer;';

    const volWrap = document.createElement('span');
    volWrap.style.cssText = 'display:flex;align-items:center;gap:2px;flex-shrink:0;';
    const volIcon = document.createElement('span');
    volIcon.textContent = '🔉';
    volIcon.style.cssText = 'font-size:13px;cursor:pointer;user-select:none;';
    const volBar = document.createElement('input');
    volBar.id = 'globalMusicVol';
    volBar.type = 'range'; volBar.min = 0; volBar.max = 100;
    volBar.value = localStorage.getItem('musicVolume') ?? 50;
    volBar.style.cssText = 'width:52px;height:4px;accent-color:#ff9800;cursor:pointer;';

    const audio = document.createElement('audio');
    audio.id = 'globalMusicAudio';
    audio.volume = (localStorage.getItem('musicVolume') ?? 50) / 100;

    volBar.oninput = () => { audio.volume = volBar.value / 100; localStorage.setItem('musicVolume', volBar.value); volIcon.textContent = volBar.value == 0 ? '🔇' : volBar.value < 50 ? '🔉' : '🔊'; };
    volIcon.onclick = () => { if (audio.volume > 0) { volIcon.dataset.prev = volBar.value; volBar.value = 0; audio.volume = 0; volIcon.textContent = '🔇'; } else { volBar.value = volIcon.dataset.prev || 50; audio.volume = volBar.value / 100; volIcon.textContent = volBar.value < 50 ? '🔉' : '🔊'; } localStorage.setItem('musicVolume', volBar.value); };
    volWrap.appendChild(volIcon);
    volWrap.appendChild(volBar);

    playBtn.onclick = () => { if (audio.paused) { audio.play(); playBtn.textContent = '⏸'; } else { audio.pause(); playBtn.textContent = '▶'; } };
    audio.ontimeupdate = () => { if (audio.duration) bar.value = (audio.currentTime / audio.duration) * 1000; };
    bar.oninput = () => { if (audio.duration) audio.currentTime = (bar.value / 1000) * audio.duration; };
    audio.onended = () => { wrap.style.display = 'none'; playBtn.textContent = '▶'; };
    audio.onplay = () => { playBtn.textContent = '⏸'; };
    audio.onpause = () => { if (!audio.ended) playBtn.textContent = '▶'; };

    const closeBtn = document.createElement('button');
    closeBtn.textContent = '✕';
    closeBtn.style.cssText = 'background:none;border:none;font-size:14px;cursor:pointer;color:var(--text2,#888);padding:0 4px;line-height:1;flex-shrink:0;';
    closeBtn.onclick = () => { audio.pause(); audio.src = ''; wrap.style.display = 'none'; bar.value = 0; };

    wrap.appendChild(playBtn);
    wrap.appendChild(bar);
    wrap.appendChild(volWrap);
    wrap.appendChild(audio);
    wrap.appendChild(closeBtn);
    document.body.appendChild(wrap);
  }
  const audio = document.getElementById('globalMusicAudio');
  audio.src = '/api/music/stream/' + songId;
  wrap.style.display = 'flex';
  document.getElementById('globalMusicBar').value = 0;
  document.getElementById('globalMusicPlayBtn').textContent = '⏸';
  audio.play().catch(() => {});
}

// ── 对话 ──
async function newConversation() {
  const model = $("modelSelect").value;
  const today = new Date();
  const title = today.getFullYear() + '-' + String(today.getMonth()+1).padStart(2,'0') + '-' + String(today.getDate()).padStart(2,'0');
  const conv = await api("POST", "/api/conversations", { title, model });
  await selectConv(conv.id);
  closeSidebar();
}

async function selectConv(id) {
  currentConvId = id;
  localStorage.setItem('aion_last_conv', id);
  msgDebugData = {};
  _heartWhisperMsgIds.clear();
  Object.keys(_heartWhisperContent).forEach(k => delete _heartWhisperContent[k]);
  _memoryRecordMsgIds.clear();
  Object.keys(_memoryRecordContent).forEach(k => delete _memoryRecordContent[k]);
  const conv = conversations.find(c => c.id === id);
  if (conv) {
    $("chatTitle").textContent = conv.title;
    $("modelSelect").value = conv.model;
  }
  setCurrentMessages(await api("GET", `/api/conversations/${id}/messages?limit=${MSG_PAGE_SIZE}`));
  // 加载该对话的心语数据
  try {
    const hwList = await api("GET", `/api/heart-whispers/by-conv/${id}`);
    for (const hw of hwList) {
      _heartWhisperMsgIds.add(hw.msg_id);
      _heartWhisperContent[hw.msg_id] = hw.content;
    }
  } catch (e) { console.warn('加载心语失败:', e); }
  // 加载该对话的 AI 主动记忆数据
  try {
    const mrList = await api("GET", `/api/memories/by-conv/${id}`);
    for (const mr of mrList) {
      _memoryRecordMsgIds.add(mr.msg_id);
      if (_memoryRecordContent[mr.msg_id]) {
        _memoryRecordContent[mr.msg_id] += '\n' + mr.content;
      } else {
        _memoryRecordContent[mr.msg_id] = mr.content;
      }
    }
  } catch (e) { console.warn('加载记忆记录失败:', e); }
  hasMoreMessages = currentMessages.length >= MSG_PAGE_SIZE;
  renderConvList();
  renderMessages();
  $("sendBtn").disabled = false;
  closeSidebar();
}

async function refreshCurrentConversationFromServer(options = {}) {
  if (!currentConvId) return false;
  const convId = currentConvId;
  try {
    conversations = await api("GET", "/api/conversations");
    renderConvList();
    const conv = conversations.find(c => c.id === convId);
    if (conv && currentConvId === convId) {
      $("chatTitle").textContent = conv.title;
      if ($("modelSelect")) $("modelSelect").value = conv.model;
    }
  } catch (e) {
    console.warn('[chat] refresh conversations failed:', e);
  }

  try {
    const msgs = await api("GET", `/api/conversations/${convId}/messages?limit=${MSG_PAGE_SIZE}`);
    if (currentConvId !== convId) return false;
    setCurrentMessages(msgs);
    hasMoreMessages = msgs.length >= MSG_PAGE_SIZE;
    renderMessages();
    if (options && options.scroll) scrollBottom();
    return true;
  } catch (e) {
    console.warn('[chat] refresh current conversation failed:', e);
    return false;
  }
}
window.refreshCurrentConversationFromServer = refreshCurrentConversationFromServer;

async function loadOlderMessages() {
  if (!currentConvId || !hasMoreMessages || loadingMore) return;
  loadingMore = true;
  const oldest = currentMessages[0];
  if (!oldest) { loadingMore = false; return; }
  const el = $("messages");
  const prevHeight = el.scrollHeight;
  try {
    const older = await api("GET", `/api/conversations/${currentConvId}/messages?limit=${MSG_PAGE_SIZE}&before=${oldest.created_at}`);
    if (older.length === 0) { hasMoreMessages = false; return; }
    hasMoreMessages = older.length >= MSG_PAGE_SIZE;
    setCurrentMessages([...older, ...currentMessages]);
    renderMessages();
    // 保持滚动位置
    requestAnimationFrame(() => el.scrollTop = el.scrollHeight - prevHeight);
  } finally {
    loadingMore = false;
  }
}

async function delConv(id) {
  if (!confirm("确定删除此对话？")) return;
  await api("DELETE", `/api/conversations/${id}`);
}

async function changeModel() {
  if (!currentConvId) return;
  await api("PUT", `/api/conversations/${currentConvId}`, { model: $("modelSelect").value });
}

async function renameConv(id) {
  const conv = conversations.find(c => c.id === id);
  if (!conv) return;
  const newTitle = prompt("重命名对话:", conv.title);
  if (newTitle !== null && newTitle.trim() && newTitle !== conv.title) {
    await api("PUT", `/api/conversations/${id}`, { title: newTitle.trim() });
  }
}

function renameCurrent() {
  if (currentConvId) renameConv(currentConvId);
}

// ── 发送/停止按钮切换 ──
function handleSendBtn() {
  if (sending) { stopGeneration(); } else { send(); }
}

function _showStopBtn() {
  const btn = $("sendBtn");
  btn.disabled = false;
  btn.classList.add('stop-mode');
  btn.innerHTML = '■';
}

function _showSendBtn() {
  const btn = $("sendBtn");
  btn.classList.remove('stop-mode');
  btn.innerHTML = '➤';
  btn.disabled = false;
}

function _updateSendBtnState() {
  if (sending) return;
  const btn = $("sendBtn");
  btn.disabled = !$("input").value.trim() && !pendingAttachments.length;
}

async function stopGeneration() {
  // 1. 中断前端 fetch 连接
  if (_abortController) { _abortController.abort(); _abortController = null; }
  // 2. 通知后端停止生成
  if (currentConvId) {
    try { await fetch(`/api/conversations/${currentConvId}/abort`, { method: 'POST' }); } catch {}
  }
}

function _getMaxTokens() {
  const v = parseInt($("maxTokensSlider").value) || 0;
  return v > 0 ? v : null;
}

// ── 发送消息 ──
async function send() {
  const input = $("input");
  const text = input.value.trim();
  if ((!text && !pendingAttachments.length) || !currentConvId || sending) return;

  sending = true;
  _showStopBtn();
  input.value = "";
  autoResize(input);
  const attachments = pendingAttachments.map(a => a.url);
  pendingAttachments = [];
  renderPreview();

  // 立即显示用户消息（乐观更新）
  playSend();
  const tempUserMsg = { id: "temp_user", conv_id: currentConvId, role: "user", content: text, created_at: Date.now()/1000, attachments };
  upsertCurrentMessage(tempUserMsg);
  renderMessages();

  _abortController = new AbortController();
  try {
    const contextLimit = parseInt($("contextSlider").value) || 30;
    const temperature = parseFloat($("tempSlider").value);
    const maxTokens = _getMaxTokens();
    const res = await fetch(`/api/conversations/${currentConvId}/send`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ content: text, context_limit: contextLimit, attachments, whisper_mode: whisperMode, temperature, max_tokens: maxTokens, tts_enabled: ttsEnabled, tts_voice: ttsVoiceId, client_id: _clientId }),
      signal: _abortController.signal
    });

    await _processSSEStream(res);

  } catch (err) {
    if (err.name === 'AbortError') {
      console.log("用户中止生成");
    } else {
      console.error("发送失败:", err);
      _stopTypingAnim();
      addErrorToSystemLog(`发送失败: ${err.message || err}`, $("modelSelect")?.value);
      if (streamingAiId) {
        const mi = currentMessages.findIndex(m => m.id === streamingAiId);
        if (mi >= 0 && currentMessages[mi].content === '...') {
          currentMessages.splice(mi, 1);
          renderMessages();
        }
      }
    }
  } finally {
    sending = false;
    streamingAiId = null;
    _abortController = null;
    _showSendBtn();
  }
}

async function _processSSEStream(res) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let aiMsgId = null;
    let aiContent = "";
    let aiFinalAlreadyReceived = false;
    let buf = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        try {
          const data = JSON.parse(line.slice(6));
          if (data.type === "start") {
            aiMsgId = data.id;
            const existing = currentMessages.find(m => m.id === aiMsgId);
            aiFinalAlreadyReceived = !!(existing && existing.content && existing.content !== "...");
            if (aiFinalAlreadyReceived) {
              streamingAiId = null;
              continue;
            }
            streamingAiId = aiMsgId;
            upsertCurrentMessage({ id: aiMsgId, conv_id: currentConvId, role: "assistant", content: "...", created_at: Date.now()/1000 });
            renderMessages();
            _startTypingAnim(aiMsgId);
          } else if (data.type === "cli_status") {
            _updateTypingStatus(aiMsgId, data.text);
          } else if (data.type === "chunk" || data.type === "replace") {
            if (aiFinalAlreadyReceived) continue;
            _stopTypingAnim();
            aiContent = data.type === "replace" ? data.content : aiContent + data.content;
            const display = aiContent.replace(/\[CAM_CHECK\]/g, '').replace(/\[POI_SEARCH:[^\]]*\]/g, '').replace(/\[MUSIC:[^\]]*\]/g, '').replace(/\[ALARM:[^\]]*\]/g, '').replace(/\[REMINDER:[^\]]*\]/g, '').replace(/\[Monitor:[^\]]*\]/g, '').replace(/\[SCHEDULE_DEL:[^\]]*\]/g, '').replace(/\[SCHEDULE_LIST\]/g, '').replace(/\[TOY:[^\]]*\]/g, '').replace(/\[HEART:[^\]]*\]/g, '').replace(/\[MEMORY:[^\]]*\]/g, '').replace(/\[查看动态:\d+\]/g, '').replace(/\[视频电话\]/g, '').replace(/\[SELFIE:\s*[^\]]*\]/g, '').replace(/\[DRAW:\s*[^\]]*\]/g, '').replace(/\[SONG\][\s\S]*?\[\/SONG\]/gi, '').replace(/<meta>[\s\S]*?<\/meta>/g, '').trim();
            const mi = currentMessages.findIndex(m => m.id === aiMsgId);
            if (mi >= 0) currentMessages[mi].content = display;
            const container = document.getElementById(`m_${aiMsgId}`);
            if (container) {
              const parts = display.split(/\n{2,}/).filter(p => p.trim());
              const target = container.querySelector('.msg-bubbles') || container.querySelector('.msg-bubble') || container.querySelector('.inner-monologue-line');
              if (parts.length > 1) {
                const wrapper = document.createElement('div');
                wrapper.className = 'msg-bubbles';
                wrapper.innerHTML = parts.map(renderMsgPart).join('');
                target.replaceWith(wrapper);
              } else if (target) {
                const monologue = innerMonologueText(display);
                const shouldSplit = monologue !== null || hasInnerMonologue(display);
                if (target.classList.contains('msg-bubbles') || shouldSplit) {
                  const single = document.createElement('div');
                  single.className = shouldSplit ? 'msg-bubbles' : 'msg-bubble';
                  single.innerHTML = shouldSplit ? renderMsgPart(display) : formatMsg(display);
                  target.replaceWith(single);
                } else {
                  target.innerHTML = formatMsg(display);
                }
              }
            }
            scrollBottom();
          } else if (data.type === "debug" && aiMsgId) {
            msgDebugData[aiMsgId] = data;
            renderDebugBar(aiMsgId);
          } else if (data.type === "cam_check") {
            handleCamCheck(data.conv_id, data.model_key, aiMsgId);
          } else if (data.type === "cam_offline") {
            showCamOfflineNotice();
          } else if (data.type === "activity_check") {
            handleActivityCheck(data.conv_id, data.n, aiMsgId);
          } else if (data.type === "poi_search") {
            handlePoiSearch(data.categories, aiMsgId);
          } else if (data.type === "music") {
            msgMusicCards[data.msg_id] = data.cards;
            renderMusicCards(data.msg_id);
            scrollBottom();
            if (data.cards && data.cards.length) playMusicOnline(data.cards[0].id);
          } else if (data.type === "toy_command") {
            if (toyConnected) data.commands.forEach(c => toyExecCmd(c));
            showToyCapsule(data.msg_id, data.commands);
          } else if (data.type === "moment_new") {
            // 朋友圈动态不在聊天界面展示
          } else if (data.type === "memory_record") {
            showMemoryRecordHint(data.msg_id, data.content);
          } else if (data.type === "video_call_incoming") {
            if (typeof videoCall !== 'undefined') videoCall.handleIncomingIndicator(data);
          } else if (data.type === "image_gen_start") {
            handleImageGenStart(data);
          } else if (data.type === "song_gen_start") {
            handleSongGenStart(data);
          }
        } catch {}
      }
    }
    if (aiMsgId) finishTTSForMsg(aiMsgId);
    if (aiMsgId && !aiFinalAlreadyReceived) playRecv();
    if ((voiceInCall || (typeof videoCall !== 'undefined' && videoCall.active)) && !ttsEnabled) {
      notifyVoiceAiSpeaking(false);
    }
}

// ── 消息操作 ──
async function delMsg(id) { await api("DELETE", `/api/messages/${id}`); }

function editMsg(id) {
  closeMsgMenus();
  const msg = currentMessages.find(m => m.id === id);
  if (!msg) return;
  const row = document.getElementById(`m_${id}`);
  if (!row) return;
  row.classList.add('editing');
  // 编辑时合并多气泡为单气泡
  const bubbles = row.querySelector('.msg-bubbles');
  if (bubbles) { const single = document.createElement('div'); single.className = 'msg-bubble'; bubbles.replaceWith(single); }
  const bubble = row.querySelector('.msg-bubble');
  bubble.classList.add('editing');
  bubble.innerHTML = '<textarea class="edit-textarea" id="edit_' + id + '"></textarea>' +
    '<div class="edit-actions">' +
    '<button class="edit-cancel" onclick="cancelEdit(\'' + id + '\')">取消</button>' +
    '<button class="edit-save" onclick="saveEdit(\'' + id + '\')">确认</button>' +
    '</div>';
  const ta = document.getElementById('edit_' + id);
  ta.value = msg.content;
  ta.style.height = 'auto';
  ta.style.height = ta.scrollHeight + 'px';
  ta.oninput = function() { this.style.height = 'auto'; this.style.height = this.scrollHeight + 'px'; };
  ta.focus();
}

function cancelEdit(id) { renderMessages(); }

async function saveEdit(id) {
  const ta = document.getElementById('edit_' + id);
  if (!ta) return;
  const newText = ta.value.trim();
  if (!newText) return;
  const msg = currentMessages.find(m => m.id === id);
  if (!msg) return;

  // 编辑重新发送：更新消息内容 + 删除后续消息 + AI 重新回复
  sending = true;
  _showStopBtn();
  msg.content = newText;

  // 前端立即删除该消息之后的所有消息
  const idx = currentMessages.indexOf(msg);
  if (idx >= 0) currentMessages.splice(idx + 1);
  // 添加临时的 AI 思考中占位
  const tempAiId = 'temp_edit_thinking';
  upsertCurrentMessage({ id: tempAiId, conv_id: currentConvId, role: 'assistant', content: '...', created_at: Date.now()/1000 });
  renderMessages();
  _startTypingAnim(tempAiId);
  _abortController = new AbortController();
  try {
    const contextLimit = parseInt($('contextSlider').value) || 30;
    const temperature = parseFloat($('tempSlider').value);
    const maxTokens = _getMaxTokens();
    const res = await fetch(`/api/messages/${id}/edit-resend`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ content: newText, context_limit: contextLimit, whisper_mode: whisperMode, temperature, max_tokens: maxTokens, tts_enabled: ttsEnabled, tts_voice: ttsVoiceId, client_id: _clientId }),
      signal: _abortController.signal
    });

    if (!res.ok) {
      console.error('编辑重发接口错误:', res.status, res.statusText);
      _stopTypingAnim();
      // 回滚：从服务器重新加载消息
      const msgs = await api('GET', `/api/conversations/${currentConvId}/messages?limit=${MSG_PAGE_SIZE}`);
      setCurrentMessages(msgs);
      renderMessages();
      sending = false;
      _showSendBtn();
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let aiMsgId = null;
    let aiContent = '';
    let buf = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const data = JSON.parse(line.slice(6));
          if (data.type === 'start') {
            _stopTypingAnim();
            // 替换临时思考占位为真正的 AI 消息
            const tempIdx = currentMessages.findIndex(m => m.id === tempAiId);
            if (tempIdx >= 0) currentMessages.splice(tempIdx, 1);
            aiMsgId = data.id;
            streamingAiId = aiMsgId;
            upsertCurrentMessage({ id: aiMsgId, conv_id: currentConvId, role: 'assistant', content: '...', created_at: Date.now()/1000 });
            renderMessages();
            _startTypingAnim(aiMsgId);
          } else if (data.type === 'cli_status') {
            _updateTypingStatus(aiMsgId, data.text);
          } else if (data.type === 'chunk' || data.type === 'replace') {
            _stopTypingAnim();
            aiContent = data.type === 'replace' ? data.content : aiContent + data.content;
            const display = aiContent.replace(/\[CAM_CHECK\]/g, '').replace(/\[POI_SEARCH:[^\]]*\]/g, '').replace(/\[MUSIC:[^\]]*\]/g, '').replace(/\[ALARM:[^\]]*\]/g, '').replace(/\[REMINDER:[^\]]*\]/g, '').replace(/\[Monitor:[^\]]*\]/g, '').replace(/\[SCHEDULE_DEL:[^\]]*\]/g, '').replace(/\[SCHEDULE_LIST\]/g, '').replace(/\[TOY:[^\]]*\]/g, '').replace(/\[HEART:[^\]]*\]/g, '').replace(/\[MEMORY:[^\]]*\]/g, '').replace(/\[查看动态:\d+\]/g, '').replace(/\[视频电话\]/g, '').replace(/\[SELFIE:\s*[^\]]*\]/g, '').replace(/\[DRAW:\s*[^\]]*\]/g, '').replace(/\[SONG\][\s\S]*?\[\/SONG\]/gi, '').replace(/<meta>[\s\S]*?<\/meta>/g, '').trim();
            const mi = currentMessages.findIndex(m => m.id === aiMsgId);
            if (mi >= 0) currentMessages[mi].content = display;
            const container = document.getElementById(`m_${aiMsgId}`);
            if (container) {
              const parts = display.split(/\n{2,}/).filter(p => p.trim());
              const target = container.querySelector('.msg-bubbles') || container.querySelector('.msg-bubble') || container.querySelector('.inner-monologue-line');
              if (parts.length > 1) {
                const wrapper = document.createElement('div');
                wrapper.className = 'msg-bubbles';
                wrapper.innerHTML = parts.map(renderMsgPart).join('');
                target.replaceWith(wrapper);
              } else if (target) {
                const monologue = innerMonologueText(display);
                const shouldSplit = monologue !== null || hasInnerMonologue(display);
                if (target.classList.contains('msg-bubbles') || shouldSplit) {
                  const single = document.createElement('div');
                  single.className = shouldSplit ? 'msg-bubbles' : 'msg-bubble';
                  single.innerHTML = shouldSplit ? renderMsgPart(display) : formatMsg(display);
                  target.replaceWith(single);
                } else {
                  target.innerHTML = formatMsg(display);
                }
              }
            }
            scrollBottom();
          } else if (data.type === 'debug' && aiMsgId) {
            msgDebugData[aiMsgId] = data;
            renderDebugBar(aiMsgId);
          } else if (data.type === 'cam_check') {
            handleCamCheck(data.conv_id, data.model_key, aiMsgId);
          } else if (data.type === 'cam_offline') {
            showCamOfflineNotice();
          } else if (data.type === 'activity_check') {
            handleActivityCheck(data.conv_id, data.n, aiMsgId);
          } else if (data.type === 'poi_search') {
            handlePoiSearch(data.categories, aiMsgId);
          } else if (data.type === 'music') {
            msgMusicCards[data.msg_id] = data.cards;
            renderMusicCards(data.msg_id);
            scrollBottom();
            if (data.cards && data.cards.length) playMusicOnline(data.cards[0].id);
          } else if (data.type === 'toy_command') {
            if (toyConnected) data.commands.forEach(c => toyExecCmd(c));
            showToyCapsule(data.msg_id, data.commands);
          } else if (data.type === 'moment_new') {
            // 朋友圈动态不在聊天界面展示
          } else if (data.type === 'memory_record') {
            showMemoryRecordHint(data.msg_id, data.content);
          } else if (data.type === 'video_call_incoming') {
            if (typeof videoCall !== 'undefined') videoCall.handleIncomingIndicator(data);
          } else if (data.type === 'image_gen_start') {
            handleImageGenStart(data);
          } else if (data.type === 'song_gen_start') {
            handleSongGenStart(data);
          }
        } catch {}
      }
    }
    if (aiMsgId) finishTTSForMsg(aiMsgId);
    if ((voiceInCall || (typeof videoCall !== 'undefined' && videoCall.active)) && !ttsEnabled) {
      notifyVoiceAiSpeaking(false);
    }
  } catch (err) {
    if (err.name === 'AbortError') {
      console.log("用户中止编辑重发");
    } else {
      console.error('编辑重发失败:', err);
      _stopTypingAnim();
      addErrorToSystemLog(`编辑重发失败: ${err.message || err}`, $('modelSelect')?.value);
      // 清理临时占位和未完成的 AI 消息
      currentMessages = currentMessages.filter(m => m.id !== tempAiId);
      if (streamingAiId) {
        const mi = currentMessages.findIndex(m => m.id === streamingAiId);
        if (mi >= 0 && currentMessages[mi].content === '...') {
          currentMessages.splice(mi, 1);
        }
      }
      renderMessages();
    }
  } finally {
    sending = false;
    streamingAiId = null;
    _abortController = null;
    _showSendBtn();
  }
}

function copyMsg(id) {
  const msg = currentMessages.find(m => m.id === id);
  if (msg) navigator.clipboard.writeText(msg.content);
}

async function regenerateMsg(aiMsgId) {
  if (sending || !currentConvId) return;
  await api("DELETE", `/api/messages/${aiMsgId}`);
  currentMessages = currentMessages.filter(m => m.id !== aiMsgId);
  renderMessages();

  sending = true;
  _showStopBtn();

  _abortController = new AbortController();
  try {
    const cl = parseInt($("contextSlider").value) || 30;
    const temperature = parseFloat($("tempSlider").value);
    const maxTokens = _getMaxTokens();
    const mtParam = maxTokens ? `&max_tokens=${maxTokens}` : '';
    const res = await fetch(`/api/conversations/${currentConvId}/regenerate?context_limit=${cl}&whisper_mode=${whisperMode}&temperature=${temperature}${mtParam}&tts_enabled=${ttsEnabled}&tts_voice=${encodeURIComponent(ttsVoiceId)}`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      signal: _abortController.signal
    });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let newId = null, aiContent = "", buf = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        try {
          const d = JSON.parse(line.slice(6));
          if (d.type === "start") {
            newId = d.id;
            streamingAiId = newId;
            upsertCurrentMessage({ id: newId, conv_id: currentConvId, role: "assistant", content: "...", created_at: Date.now()/1000 });
            renderMessages();
            _startTypingAnim(newId);
          } else if (d.type === "chunk" || d.type === "replace") {
            _stopTypingAnim();
            aiContent = d.type === "replace" ? d.content : aiContent + d.content;
            const display = aiContent.replace(/\[CAM_CHECK\]/g, '').replace(/\[POI_SEARCH:[^\]]*\]/g, '').replace(/\[MUSIC:[^\]]*\]/g, '').replace(/\[ALARM:[^\]]*\]/g, '').replace(/\[REMINDER:[^\]]*\]/g, '').replace(/\[Monitor:[^\]]*\]/g, '').replace(/\[SCHEDULE_DEL:[^\]]*\]/g, '').replace(/\[SCHEDULE_LIST\]/g, '').replace(/\[TOY:[^\]]*\]/g, '').replace(/\[HEART:[^\]]*\]/g, '').replace(/\[MEMORY:[^\]]*\]/g, '').replace(/\[查看动态:\d+\]/g, '').replace(/\[视频电话\]/g, '').replace(/\[SELFIE:\s*[^\]]*\]/g, '').replace(/\[DRAW:\s*[^\]]*\]/g, '').replace(/\[SONG\][\s\S]*?\[\/SONG\]/gi, '').replace(/<meta>[\s\S]*?<\/meta>/g, '').trim();
            const mi = currentMessages.findIndex(m => m.id === newId);
            if (mi >= 0) currentMessages[mi].content = display;
            const b = document.querySelector(`#m_${newId} .msg-bubble`);
            if (b) b.textContent = display;
            scrollBottom();
          } else if (d.type === "debug" && newId) {
            msgDebugData[newId] = d;
            renderDebugBar(newId);
          } else if (d.type === "cam_check") {
            handleCamCheck(d.conv_id, d.model_key, newId);
          } else if (d.type === "cam_offline") {
            showCamOfflineNotice();
          } else if (d.type === "activity_check") {
            handleActivityCheck(d.conv_id, d.n, newId);
          } else if (d.type === "poi_search") {
            handlePoiSearch(d.categories, newId);
          } else if (d.type === "music") {
            msgMusicCards[d.msg_id] = d.cards;
            renderMusicCards(d.msg_id);
            scrollBottom();
            if (d.cards && d.cards.length) playMusicOnline(d.cards[0].id);
          } else if (d.type === "toy_command") {
            if (toyConnected) d.commands.forEach(c => toyExecCmd(c));
            showToyCapsule(d.msg_id, d.commands);
          } else if (d.type === "moment_new") {
            // 朋友圈动态不在聊天界面展示
          } else if (d.type === "memory_record") {
            showMemoryRecordHint(d.msg_id, d.content);
          } else if (d.type === "video_call_incoming") {
            if (typeof videoCall !== 'undefined') videoCall.handleIncomingIndicator(d);
          } else if (d.type === "image_gen_start") {
            handleImageGenStart(d);
          } else if (d.type === "song_gen_start") {
            handleSongGenStart(d);
          }
        } catch {}
      }
    }
    // TTS 由服务端流式推送，标记该消息 TTS 分段完成
    if (newId) finishTTSForMsg(newId);
    if ((voiceInCall || (typeof videoCall !== 'undefined' && videoCall.active)) && !ttsEnabled) {
      notifyVoiceAiSpeaking(false);
    }
  } catch (err) {
    if (err.name === 'AbortError') {
      console.log("用户中止重新生成");
    } else {
      console.error("重新生成失败:", err);
      _stopTypingAnim();
      addErrorToSystemLog(`重新生成失败: ${err.message || err}`, $("modelSelect")?.value);
    }
  } finally {
    sending = false;
    _abortController = null;
    _showSendBtn();
  }
}

// ── AI 输入等待动画 ──
let _typingTimer = null;
function _startTypingAnim(msgId) {
  _stopTypingAnim();
  const container = document.getElementById(`m_${msgId}`);
  if (!container) return;
  const bubble = container.querySelector('.msg-bubble');
  if (!bubble) return;
  bubble.classList.add('typing-bubble');
  bubble.innerHTML = '<span class="typing-text">思考中</span><span class="typing-dots"><span></span><span></span><span></span></span>';
  const texts = ['思考中', '正在输入'];
  let idx = 0;
  _typingTimer = setInterval(() => {
    const label = bubble.querySelector('.typing-text');
    if (!label) { _stopTypingAnim(); return; }
    idx = (idx + 1) % texts.length;
    label.style.opacity = '0';
    setTimeout(() => { if (label.parentNode) { label.textContent = texts[idx]; label.style.opacity = '1'; } }, 200);
  }, 2500);
}
function _stopTypingAnim() {
  if (_typingTimer) { clearInterval(_typingTimer); _typingTimer = null; }
}
function _updateTypingStatus(msgId, statusText) {
  // CLI 状态更新：替换 typing bubble 中的文本，保留弹跳动画
  const container = document.getElementById(`m_${msgId}`);
  if (!container) return;
  const bubble = container.querySelector('.msg-bubble');
  if (!bubble) return;
  if (!bubble.classList.contains('typing-bubble')) {
    // 如果 typing 动画已停止，重新启动
    bubble.classList.add('typing-bubble');
  }
  bubble.innerHTML = `<span class="typing-text">${statusText}</span><span class="typing-dots"><span></span><span></span><span></span></span>`;
  // 停掉旧的循环切换定时器（不需要在"思考中"和"正在输入"之间轮换了）
  if (_typingTimer) { clearInterval(_typingTimer); _typingTimer = null; }
}

// ── [CAM_CHECK] 监控查看处理 ──
let camCheckSafetyTimer = null;
let _camCheckInProgress = false;
function dismissCamCheckIndicator() {
  camCheckMsgId = null;
  _camCheckInProgress = false;
  if (camCheckSafetyTimer) { clearTimeout(camCheckSafetyTimer); camCheckSafetyTimer = null; }
  const el = document.getElementById('cam_check_loading');
  if (el) el.remove();
}
function handleCamCheck(convId, modelKey, msgId) {
  // 去重：防止 SSE + WebSocket 双通道重复触发 UI
  if (_camCheckInProgress) return;
  _camCheckInProgress = true;
  // 通知语音模块：AI 触发了 CAM_CHECK，保持 AI 说话状态
  if (voiceInCall) {
    notifyVoiceCamCheckStart();
  }
  // 设置全局跟踪，确保 renderMessages 后能恢复
  camCheckMsgId = msgId;
  // 在当前 AI 消息下方显示加载指示器
  const aiName = worldBook.ai_name || 'AI';
  const msgRow = msgId ? document.getElementById('m_' + msgId) : null;
  if (msgRow) {
    const indicator = document.createElement('div');
    indicator.className = 'cam-check-indicator';
    indicator.id = 'cam_check_loading';
    indicator.innerHTML = `📷 ${escHtml(aiName)} 正在查看监控<span class="cam-dots"><span></span><span></span><span></span></span>`;
    const msgBody = msgRow.querySelector('.msg-body');
    (msgBody || msgRow).appendChild(indicator);
    scrollBottom();
  }

  // 保底30秒安全超时：防止AI无响应时提示一直卡着
  camCheckSafetyTimer = setTimeout(() => dismissCamCheckIndicator(), 30000);

  const audio = new Audio('/public/AionMonitoralart.mp3');
  audio.play().catch(() => {});
  // 监控查看由服务端直接触发，前端只负责 UI 显示
}

// ── [查看动态:n] 活动动态查看处理 ──
let activityCheckMsgId = null;
let activityCheckSafetyTimer = null;
let _activityCheckInProgress = false;
let activityCheckN = 0;
function dismissActivityCheckIndicator() {
  activityCheckMsgId = null;
  activityCheckN = 0;
  _activityCheckInProgress = false;
  if (activityCheckSafetyTimer) { clearTimeout(activityCheckSafetyTimer); activityCheckSafetyTimer = null; }
  const el = document.getElementById('activity_check_loading');
  if (el) el.remove();
}
function handleActivityCheck(convId, n, msgId) {
  if (_activityCheckInProgress) return;
  _activityCheckInProgress = true;
  activityCheckMsgId = msgId;
  activityCheckN = n || 6;
  const aiName = worldBook.ai_name || 'AI';
  const minutes = activityCheckN * 10;
  const msgRow = msgId ? document.getElementById('m_' + msgId) : null;
  if (msgRow) {
    const indicator = document.createElement('div');
    indicator.className = 'activity-check-indicator';
    indicator.id = 'activity_check_loading';
    indicator.innerHTML = `📊 ${escHtml(aiName)} 正在查看过去${minutes}分钟的动态<span class="activity-dots"><span></span><span></span><span></span></span>`;
    const msgBody = msgRow.querySelector('.msg-body');
    (msgBody || msgRow).appendChild(indicator);
    scrollBottom();
  }
  activityCheckSafetyTimer = setTimeout(() => dismissActivityCheckIndicator(), 30000);
}

// ── [SELFIE/DRAW] 生图处理 ──
let imageGenMsgId = null;
let imageGenSafetyTimer = null;
function dismissImageGenIndicator() {
  imageGenMsgId = null;
  if (imageGenSafetyTimer) { clearTimeout(imageGenSafetyTimer); imageGenSafetyTimer = null; }
  const el = document.getElementById('image_gen_loading');
  if (el) el.remove();
}
function handleImageGenStart(data) {
  const msgId = data.msg_id;
  if (imageGenMsgId) return; // 防重复
  imageGenMsgId = msgId;
  const aiName = worldBook.ai_name || 'AI';
  const msgRow = msgId ? document.getElementById('m_' + msgId) : null;
  if (msgRow) {
    const indicator = document.createElement('div');
    indicator.className = 'image-gen-indicator';
    indicator.id = 'image_gen_loading';
    indicator.innerHTML = `🎨 ${escHtml(aiName)} 正在发送图片<span class="ig-dots"><span></span><span></span><span></span></span>`;
    const msgBody = msgRow.querySelector('.msg-body');
    (msgBody || msgRow).appendChild(indicator);
    scrollBottom();
  }
  // 保底120秒安全超时（生图较慢）
  imageGenSafetyTimer = setTimeout(() => dismissImageGenIndicator(), 120000);
}

let songGenMsgId = null;
let songGenSafetyTimer = null;
function dismissSongGenIndicator() {
  songGenMsgId = null;
  if (songGenSafetyTimer) { clearTimeout(songGenSafetyTimer); songGenSafetyTimer = null; }
  const el = document.getElementById('song_gen_loading');
  if (el) el.remove();
}
function handleSongGenStart(data) {
  const msgId = data.msg_id;
  if (songGenMsgId) return;
  songGenMsgId = msgId;
  const msgRow = msgId ? document.getElementById('m_' + msgId) : null;
  if (msgRow) {
    const indicator = document.createElement('div');
    indicator.className = 'song-gen-indicator';
    indicator.id = 'song_gen_loading';
    indicator.innerHTML = `歌曲谱写中....<span class="sg-dots"><span></span><span></span><span></span></span>`;
    const msgBody = msgRow.querySelector('.msg-body');
    (msgBody || msgRow).appendChild(indicator);
    scrollBottom();
  }
  songGenSafetyTimer = setTimeout(() => dismissSongGenIndicator(), 300000);
}

// ── 图片查看器（Lightbox） ──
let imageLongPressTimer = null;
let imageLongPressState = null;
let imageLongPressSuppressClickUntil = 0;

function imageInteractionAttrs() {
  return 'onclick="return openImageFromElement(event, this)" oncontextmenu="showImageSaveMenu(this.src); return false;" onpointerdown="startImageLongPress(event, this.src)" onpointermove="moveImageLongPress(event)" onpointerup="cancelImageLongPress()" onpointerleave="cancelImageLongPress()" onpointercancel="cancelImageLongPress()" draggable="false"';
}

function bindImageSaveOnly(img, clickHandler) {
  if (!img) return;
  img.oncontextmenu = (event) => {
    event.preventDefault();
    showImageSaveMenu(img.src);
    return false;
  };
  img.onpointerdown = (event) => startImageLongPress(event, img.src);
  img.onpointermove = moveImageLongPress;
  img.onpointerup = cancelImageLongPress;
  img.onpointerleave = cancelImageLongPress;
  img.onpointercancel = cancelImageLongPress;
  img.onclick = (event) => {
    event.stopPropagation();
    if (Date.now() < imageLongPressSuppressClickUntil) {
      event.preventDefault();
      return false;
    }
    if (typeof clickHandler === 'function') clickHandler();
    return false;
  };
  img.draggable = false;
}

function openImageFromElement(event, img) {
  if (Date.now() < imageLongPressSuppressClickUntil) {
    if (event) event.preventDefault();
    return false;
  }
  openImageViewer(img.src);
  return false;
}

function startImageLongPress(event, url) {
  if (!url) return;
  if (event.pointerType === 'mouse' && event.button !== 0) return;
  cancelImageLongPress();
  imageLongPressState = { x: event.clientX || 0, y: event.clientY || 0 };
  imageLongPressTimer = setTimeout(() => {
    imageLongPressTimer = null;
    imageLongPressSuppressClickUntil = Date.now() + 700;
    try { navigator.vibrate?.(12); } catch (e) {}
    showImageSaveMenu(url);
  }, 560);
}

function moveImageLongPress(event) {
  if (!imageLongPressState) return;
  const dx = Math.abs((event.clientX || 0) - imageLongPressState.x);
  const dy = Math.abs((event.clientY || 0) - imageLongPressState.y);
  if (dx > 12 || dy > 12) cancelImageLongPress();
}

function cancelImageLongPress() {
  if (imageLongPressTimer) clearTimeout(imageLongPressTimer);
  imageLongPressTimer = null;
  imageLongPressState = null;
}

function closeImageSaveMenu() {
  document.querySelector('.image-save-menu-overlay')?.remove();
}

function showImageSaveMenu(url) {
  if (!url) return;
  cancelImageLongPress();
  closeImageSaveMenu();
  const overlay = document.createElement('div');
  overlay.className = 'image-save-menu-overlay';
  const sheet = document.createElement('div');
  sheet.className = 'image-save-menu';

  const saveBtn = document.createElement('button');
  saveBtn.type = 'button';
  saveBtn.className = 'primary';
  saveBtn.textContent = '保存图片';
  saveBtn.addEventListener('click', () => {
    closeImageSaveMenu();
    saveImage(url);
  });

  const viewBtn = document.createElement('button');
  viewBtn.type = 'button';
  viewBtn.textContent = '查看大图';
  viewBtn.addEventListener('click', () => {
    closeImageSaveMenu();
    openImageViewer(url);
  });

  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.textContent = '取消';
  cancelBtn.addEventListener('click', closeImageSaveMenu);

  sheet.append(saveBtn, viewBtn, cancelBtn);
  overlay.appendChild(sheet);
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) closeImageSaveMenu();
  });
  document.body.appendChild(overlay);
  requestAnimationFrame(() => overlay.classList.add('active'));
}

function getImageSaverBridge() {
  try { if (window.AionImageSaver) return window.AionImageSaver; } catch (e) {}
  try { if (window.parent && window.parent.AionImageSaver) return window.parent.AionImageSaver; } catch (e) {}
  try { if (window.top && window.top.AionImageSaver) return window.top.AionImageSaver; } catch (e) {}
  return null;
}

function openImageViewer(url) {
  const overlay = document.createElement('div');
  overlay.className = 'image-viewer-overlay';
  overlay.innerHTML = `
    <button class="image-viewer-close" onclick="this.parentElement.remove()">&times;</button>
    <img src="${url}" alt="图片">
    <div class="image-viewer-actions">
      <button onclick="saveImage('${url}')">💾 保存图片</button>
      <button onclick="this.closest('.image-viewer-overlay').remove()">关闭</button>
    </div>
  `;
  bindImageSaveOnly(overlay.querySelector('img'), () => overlay.remove());
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
  document.body.appendChild(overlay);
  requestAnimationFrame(() => overlay.classList.add('active'));
}
function saveImage(url) {
  fetch(url)
    .then(r => r.blob())
    .then(blob => {
      // Android App: 通过原生桥接保存到相册
      const saver = getImageSaverBridge();
      if (saver) {
        const reader = new FileReader();
        reader.onloadend = () => {
          const base64 = reader.result.split(',')[1];
          const filename = url.split('/').pop() || 'image.png';
          saver.save(base64, filename);
        };
        reader.readAsDataURL(blob);
        return;
      }
      // 浏览器: blob URL 下载
      const blobUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = blobUrl;
      a.download = url.split('/').pop() || 'image.png';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
    })
    .catch(() => window.open(url, '_blank'));
}

// ── [HEART] 心语提示 ──
const _heartWhisperMsgIds = new Set();
const _heartWhisperContent = {};  // msgId -> content
function showHeartWhisperHint(msgId, content) {
  if (!msgId) return;
  _heartWhisperMsgIds.add(msgId);
  if (content) _heartWhisperContent[msgId] = content;
  _applyHeartHint(msgId);
}
function _applyHeartHint(msgId) {
  const msgRow = document.getElementById('m_' + msgId);
  if (!msgRow) return;
  const avatarCol = msgRow.querySelector('.msg-avatar-col');
  if (!avatarCol || avatarCol.querySelector('.heart-whisper-hint')) return;
  const hint = document.createElement('span');
  hint.className = 'heart-whisper-hint';
  hint.textContent = '💭';
  hint.onclick = (e) => { e.stopPropagation(); _showHeartWhisperCard(msgId); };
  avatarCol.appendChild(hint);
}
function _showHeartWhisperCard(msgId) {
  const content = _heartWhisperContent[msgId];
  if (!content) return;
  const overlay = document.createElement('div');
  overlay.className = 'hw-card-overlay';
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
  overlay.innerHTML = `<div class="hw-card-popup">
    <div class="hw-card-label">—— 心语 ——</div>
    <button class="hw-card-close" onclick="this.closest('.hw-card-overlay').remove()">✕</button>
    <div class="hw-card-text">${escHtml(content)}</div>
  </div>`;
  document.body.appendChild(overlay);
}

// ── [MEMORY] 记忆录入提示 ──
const _memoryRecordMsgIds = new Set();
const _memoryRecordContent = {};  // msgId -> content (多条用换行拼接)
function showMemoryRecordHint(msgId, content) {
  if (!msgId) return;
  _memoryRecordMsgIds.add(msgId);
  if (content) {
    if (_memoryRecordContent[msgId]) {
      _memoryRecordContent[msgId] += '\n' + content;
    } else {
      _memoryRecordContent[msgId] = content;
    }
  }
  _applyMemoryHint(msgId);
}
function _applyMemoryHint(msgId) {
  const msgRow = document.getElementById('m_' + msgId);
  if (!msgRow) return;
  const avatarCol = msgRow.querySelector('.msg-avatar-col');
  if (!avatarCol || avatarCol.querySelector('.memory-record-hint')) return;
  const hint = document.createElement('span');
  hint.className = 'memory-record-hint';
  hint.textContent = '💡';
  hint.title = '已记录到记忆库';
  hint.onclick = (e) => { e.stopPropagation(); _showMemoryRecordCard(msgId); };
  avatarCol.appendChild(hint);
}
function _showMemoryRecordCard(msgId) {
  const content = _memoryRecordContent[msgId];
  if (!content) return;
  const overlay = document.createElement('div');
  overlay.className = 'mr-card-overlay';
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
  overlay.innerHTML = `<div class="mr-card-popup">
    <div class="mr-card-label">—— 💡 已记录到记忆库 ——</div>
    <button class="mr-card-close" onclick="this.closest('.mr-card-overlay').remove()">✕</button>
    <div class="mr-card-text">${escHtml(content)}</div>
  </div>`;
  document.body.appendChild(overlay);
}

function showCamOfflineNotice() {
  const notice = { id: 'notice_cam_' + Date.now(), conv_id: currentConvId, role: 'assistant',
    content: '📷 摄像头未开启，Core无法查看监控信息。请先在设置中开启摄像头。', created_at: Date.now()/1000 };
  upsertCurrentMessage(notice);
  renderMessages();
  scrollBottom();
}

// ── [POI_SEARCH] 周边搜索处理 ──
let poiSearchSafetyTimer = null;
function dismissPoiSearchIndicator() {
  poiSearchMsgId = null;
  poiSearchCategories = null;
  if (poiSearchSafetyTimer) { clearTimeout(poiSearchSafetyTimer); poiSearchSafetyTimer = null; }
  const el = document.getElementById('poi_search_loading');
  if (el) el.remove();
}
function handlePoiSearch(categories, msgId) {
  const aiName = worldBook.ai_name || 'AI';
  const catText = categories.map(c => c.trim()).join('、');
  poiSearchMsgId = msgId;
  poiSearchCategories = categories;
  const msgRow = msgId ? document.getElementById('m_' + msgId) : null;
  if (msgRow) {
    const indicator = document.createElement('div');
    indicator.className = 'poi-search-indicator';
    indicator.id = 'poi_search_loading';
    indicator.innerHTML = `📍 ${escHtml(aiName)} 正在搜索附近${escHtml(catText)}<span class="poi-dots"><span></span><span></span><span></span></span>`;
    const msgBody = msgRow.querySelector('.msg-body');
    (msgBody || msgRow).appendChild(indicator);
    scrollBottom();
  }
  poiSearchSafetyTimer = setTimeout(() => dismissPoiSearchIndicator(), 45000);
}

// ── UI ──
function handleKey(e) { if (e.key === "Enter" && e.ctrlKey) { e.preventDefault(); send(); } }

function luckinOrderStatusHtml(data) {
  const code = data && data.take_meal_code ? String(data.take_meal_code) : "";
  const takeOrderId = data && data.take_order_id ? String(data.take_order_id) : "";
  const status = data && data.order_status_name
    ? String(data.order_status_name)
    : (data && data.order_status !== undefined && data.order_status !== null ? `状态 ${data.order_status}` : "");
  const time = data && (data.take_meal_time || data.about_time) ? String(data.take_meal_time || data.about_time) : "";
  if (code) {
    const orderLine = takeOrderId ? `<div style="font-size:11px;color:rgba(255,255,255,.58);margin-top:3px">取餐序号：${escHtml(takeOrderId)}</div>` : "";
    const statusLine = status ? `<div style="font-size:11px;color:rgba(255,255,255,.58);margin-top:3px">${escHtml(status)}</div>` : "";
    return `<div style="font-size:13px;color:#fff;margin-top:8px">取餐码：<b style="font-size:18px;letter-spacing:.5px">${escHtml(code)}</b></div>${orderLine}${statusLine}`;
  }
  const main = status ? `当前状态：${status}` : "暂时还没有取餐码";
  const hint = time ? `预计：${time}` : "支付后稍等再查一次";
  return `<div style="font-size:12px;color:rgba(255,255,255,.72);margin-top:8px;line-height:1.35">${escHtml(main)}</div><div style="font-size:11px;color:rgba(255,255,255,.52);margin-top:3px">${escHtml(hint)}</div>`;
}

async function queryLuckinOrderStatus(btn) {
  const orderId = btn?.dataset?.orderId || "";
  const card = btn?.closest('.luckin-pay-card');
  const statusEl = card?.querySelector('.luckin-order-status');
  if (!orderId || !statusEl) return;
  const oldText = btn.textContent || "查询取餐码";
  btn.disabled = true;
  btn.textContent = "查询中...";
  statusEl.innerHTML = '<div style="font-size:12px;color:rgba(255,255,255,.72);margin-top:8px">正在查询订单状态...</div>';
  try {
    const res = await fetch(`/api/luckin/order/${encodeURIComponent(orderId)}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.error) throw new Error(data.detail || data.error || "查询失败");
    statusEl.innerHTML = luckinOrderStatusHtml(data);
  } catch (err) {
    statusEl.innerHTML = `<div style="font-size:12px;color:#ffd1d1;margin-top:8px;line-height:1.35">${escHtml(err.message || "查询失败")}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = oldText;
  }
}

function buildLuckinPaymentCard(item) {
  const title = escHtml(item.title || "瑞幸咖啡订单");
  const shop = item.shop ? `<div style="font-size:12px;color:rgba(255,255,255,.72);margin-top:2px">${escHtml(item.shop)}</div>` : "";
  const address = item.address ? `<div style="font-size:12px;color:rgba(255,255,255,.6);margin-top:2px;line-height:1.35">${escHtml(item.address)}</div>` : "";
  const amount = item.amount ? `<div style="font-size:13px;color:#fff;margin-top:6px">待支付：${escHtml(item.amount)}</div>` : "";
  const orderId = item.order_id ? `<div style="font-size:11px;color:rgba(255,255,255,.55);margin-top:2px">订单号：${escHtml(item.order_id)}</div>` : "";
  const hasSpecWarning = /未匹配|切换失败/.test(item.note || "");
  const noteLabel = hasSpecWarning ? "规格提醒" : "备注";
  const noteColor = hasSpecWarning ? "#ffe1a8" : "rgba(255,255,255,.66)";
  const note = item.note ? `<div style="font-size:11px;color:${noteColor};margin-top:6px;line-height:1.35">${noteLabel}：${escHtml(item.note)}</div>` : "";
  const qrUrl = item.qr_url || item.url || "";
  const qr = qrUrl ? `<img src="${escHtml(qrUrl)}" onclick="openImageViewer(this.src)" style="width:168px;max-width:100%;border-radius:8px;background:#fff;padding:8px;display:block;margin:10px auto 6px;cursor:pointer">` : "";
  const payUrl = item.pay_url || "";
  const payButton = payUrl ? `<a href="${escHtml(payUrl)}" target="_blank" rel="noopener" style="display:inline-flex;align-items:center;justify-content:center;padding:7px 12px;border-radius:999px;background:rgba(255,255,255,.16);color:#fff;text-decoration:none;font-size:13px">打开支付页</a>` : "";
  const queryButton = item.order_id ? `<button type="button" data-order-id="${escHtml(item.order_id)}" onclick="queryLuckinOrderStatus(this)" style="border:none;display:inline-flex;align-items:center;justify-content:center;padding:7px 12px;border-radius:999px;background:rgba(75,210,176,.24);color:#fff;font-size:13px;cursor:pointer">查询取餐码</button>` : "";
  const buttons = payButton || queryButton ? `<div style="display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin-top:8px">${payButton}${queryButton}</div>` : "";
  return `<div class="luckin-pay-card" style="margin-top:8px;padding:12px;border:1px solid rgba(75,210,176,.45);background:rgba(16,86,76,.42);border-radius:10px;max-width:260px">
    <div style="font-weight:700;color:#fff">瑞幸订单 · 扫码确认支付</div>
    <div style="font-size:13px;color:#fff;margin-top:4px">${title}</div>
    ${shop}${address}${amount}${orderId}${note}${qr}<div class="luckin-order-status"></div>${buttons}
  </div>`;
}

function escJsSingle(s) {
  return String(s || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\r/g, '').replace(/\n/g, '\\n');
}

const WISH_FULFILLMENT_MARK_RE = /\u2063wish_fulfillment:([A-Za-z0-9_-]+)\u2063/;

function stripWishFulfillmentMarker(text) {
  return String(text || '').replace(WISH_FULFILLMENT_MARK_RE, '');
}

function wishFulfillmentFromContent(text) {
  const raw = String(text || '');
  const marker = raw.match(WISH_FULFILLMENT_MARK_RE);
  if (!marker) return null;
  const clean = stripWishFulfillmentMarker(raw).trim();
  const parsed = clean.match(/我捞起了【(.+?)】的愿望，愿望内容：([\s\S]*?)。现在将为他实现。?$/);
  return {
    type: 'wish_fulfillment',
    wish_id: marker[1],
    author_name: parsed ? parsed[1].trim() : '许愿者',
    content: parsed ? parsed[2].trim() : clean,
    status: 'active',
    message: clean,
  };
}

function withWishFallbackAttachments(message) {
  const atts = Array.isArray(message?.attachments) ? message.attachments : [];
  if (atts.some(item => item && typeof item === 'object' && item.type === 'wish_fulfillment')) return atts;
  const fallback = wishFulfillmentFromContent(message?.content || '');
  return fallback ? [...atts, fallback] : atts;
}

function wishCardStatusLabel(status) {
  return status === 'fulfilled' ? '已完成' : '池中';
}

function applyWishCardStatus(card, status) {
  const next = status === 'fulfilled' ? 'fulfilled' : 'active';
  card.dataset.status = next;
  const stateEl = card.querySelector('.wish-card-state');
  if (stateEl) stateEl.textContent = wishCardStatusLabel(next);
  const hint = card.querySelector('.wish-card-hint');
  if (hint) hint.textContent = next === 'fulfilled' ? '愿望已标记完成' : '愿望已放回池中';
  card.querySelectorAll('[data-wish-action]').forEach(btn => {
    const action = btn.dataset.wishAction;
    btn.disabled = (action === next);
  });
}

async function setWishCardStatus(btn, wishId, status) {
  const card = btn?.closest('.wish-fulfill-card');
  if (!wishId || !card) return;
  if (status === 'active') {
    const hint = card.querySelector('.wish-card-hint');
    if (hint) hint.textContent = card.dataset.status === 'fulfilled' ? '愿望已经完成，记录保持不变' : '愿望还在池中';
    return;
  }
  const buttons = card.querySelectorAll('[data-wish-action]');
  buttons.forEach(item => { item.disabled = true; });
  const hint = card.querySelector('.wish-card-hint');
  if (hint) hint.textContent = '更新中...';
  try {
    const res = await fetch(`/api/wishes/${encodeURIComponent(wishId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    });
    if (!res.ok) throw new Error('request failed');
    const updated = await res.json();
    document.querySelectorAll('.wish-fulfill-card').forEach(item => {
      if (item.dataset.wishId === wishId) applyWishCardStatus(item, updated.status || status);
    });
  } catch (e) {
    if (hint) hint.textContent = '更新失败，稍后再试';
    buttons.forEach(item => { item.disabled = false; });
  }
}

function buildWishFulfillmentCard(item) {
  const wishId = String(item.wish_id || '');
  const status = item.status === 'fulfilled' ? 'fulfilled' : 'active';
  const idArg = escJsSingle(wishId);
  const activeDisabled = '';
  const fulfilledDisabled = status === 'fulfilled' ? ' disabled' : '';
  const authorName = escHtml(item.author_name || '许愿者');
  const content = escHtml(item.content || stripWishFulfillmentMarker(item.message || ''));
  return `<div class="wish-fulfill-card" data-wish-id="${escHtml(wishId)}" data-status="${status}">
    <div class="wish-card-head">
      <span>许愿池愿望</span>
      <span class="wish-card-state">${wishCardStatusLabel(status)}</span>
    </div>
    <div class="wish-card-from">来自【${authorName}】</div>
    <div class="wish-card-content">${content}</div>
    <div class="wish-card-actions">
      <button type="button" data-wish-action="active" onclick="setWishCardStatus(this,'${idArg}','active')"${activeDisabled}>放回池中</button>
      <button type="button" data-wish-action="fulfilled" onclick="setWishCardStatus(this,'${idArg}','fulfilled')"${fulfilledDisabled}>已完成</button>
    </div>
    <div class="wish-card-hint"></div>
  </div>`;
}

const generatedSongPlayerStore = {};
let generatedSongPlayerSeq = 0;
let generatedSongAudio = null;
let generatedSongProgressFrame = null;

function registerGeneratedSongItem(item) {
  const key = `song_${Date.now()}_${generatedSongPlayerSeq++}`;
  generatedSongPlayerStore[key] = item || {};
  return key;
}

function extractGeneratedSongLyrics(item) {
  const direct = (item && item.lyrics) ? String(item.lyrics).trim() : '';
  if (direct) return direct;
  const prompt = (item && item.prompt) ? String(item.prompt) : '';
  const match = prompt.match(/^\s*Lyrics\s*:\s*([\s\S]+)$/im);
  if (match && match[1].trim()) return match[1].trim();
  const desc = (item && item.description) ? String(item.description).trim() : '';
  return desc || '';
}

function formatGeneratedSongTime(seconds) {
  const sec = Number.isFinite(seconds) && seconds > 0 ? seconds : 0;
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

function closeGeneratedSongPlayer() {
  if (generatedSongProgressFrame) {
    cancelAnimationFrame(generatedSongProgressFrame);
    generatedSongProgressFrame = null;
  }
  if (generatedSongAudio) {
    generatedSongAudio.pause();
    generatedSongAudio.src = '';
    generatedSongAudio = null;
  }
  const overlay = document.getElementById('generatedSongPlayerOverlay');
  if (overlay) overlay.remove();
}

function openGeneratedSongPlayer(key) {
  const item = generatedSongPlayerStore[key];
  if (!item || !item.url) return;
  closeGeneratedSongPlayer();

  const title = item.title || 'AI 生成歌曲';
  const model = item.model || 'lyria-3-pro-preview';
  const lyrics = extractGeneratedSongLyrics(item);
  const overlay = document.createElement('div');
  overlay.id = 'generatedSongPlayerOverlay';
  overlay.className = 'song-player-overlay';
  overlay.innerHTML = `
    <div class="song-player-sheet" role="dialog" aria-modal="true" aria-label="歌曲播放器">
      <button class="song-player-close" type="button" aria-label="关闭">×</button>
      <div class="song-player-head">
        <div class="song-player-cover" aria-hidden="true"><span></span></div>
        <div class="song-player-info">
          <div class="song-player-kicker">Generated Song</div>
          <div class="song-player-title">${escHtml(title)}</div>
          <div class="song-player-meta">${escHtml(model)}</div>
        </div>
      </div>
      <div class="song-player-controls">
        <button class="song-player-play" type="button">播放</button>
        <div class="song-player-progress-wrap">
          <input class="song-player-progress" type="range" min="0" max="1000" value="0" aria-label="播放进度">
          <div class="song-player-time"><span class="song-player-current">0:00</span><span class="song-player-duration">0:00</span></div>
        </div>
      </div>
      <div class="song-player-lyrics-title">歌词</div>
      <div class="song-player-lyrics">${lyrics ? escHtml(lyrics) : '<span class="song-player-empty">暂无歌词</span>'}</div>
    </div>
  `;
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) closeGeneratedSongPlayer();
  });
  document.body.appendChild(overlay);
  requestAnimationFrame(() => overlay.classList.add('active'));

  const audio = new Audio(item.url);
  generatedSongAudio = audio;
  const playBtn = overlay.querySelector('.song-player-play');
  const progress = overlay.querySelector('.song-player-progress');
  const currentEl = overlay.querySelector('.song-player-current');
  const durationEl = overlay.querySelector('.song-player-duration');
  const cover = overlay.querySelector('.song-player-cover');

  function setProgressValue(pct) {
    const safePct = Math.min(1000, Math.max(0, pct || 0));
    progress.value = String(safePct);
    progress.style.setProperty('--song-progress', `${safePct / 10}%`);
  }

  function updateProgress() {
    const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
    const current = Number.isFinite(audio.currentTime) ? audio.currentTime : 0;
    const pct = duration > 0 ? Math.round((current / duration) * 1000) : 0;
    setProgressValue(pct);
    durationEl.textContent = formatGeneratedSongTime(duration);
    currentEl.textContent = formatGeneratedSongTime(current);
  }

  function stopProgressLoop() {
    if (generatedSongProgressFrame) {
      cancelAnimationFrame(generatedSongProgressFrame);
      generatedSongProgressFrame = null;
    }
  }

  function startProgressLoop() {
    stopProgressLoop();
    const tick = () => {
      updateProgress();
      if (generatedSongAudio === audio && !audio.paused && !audio.ended) {
        generatedSongProgressFrame = requestAnimationFrame(tick);
      }
    };
    generatedSongProgressFrame = requestAnimationFrame(tick);
  }

  overlay.querySelector('.song-player-close')?.addEventListener('click', closeGeneratedSongPlayer);
  playBtn.addEventListener('click', async () => {
    if (audio.paused) {
      try { await audio.play(); } catch(e) {}
    } else {
      audio.pause();
    }
  });
  progress.addEventListener('input', () => {
    const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
    if (!duration) return;
    audio.currentTime = (Number(progress.value) / 1000) * duration;
    updateProgress();
  });
  audio.addEventListener('play', () => {
    playBtn.textContent = '暂停';
    cover.classList.add('playing');
    startProgressLoop();
  });
  audio.addEventListener('pause', () => {
    playBtn.textContent = '播放';
    cover.classList.remove('playing');
    stopProgressLoop();
    updateProgress();
  });
  audio.addEventListener('ended', () => {
    playBtn.textContent = '播放';
    cover.classList.remove('playing');
    stopProgressLoop();
    updateProgress();
  });
  audio.addEventListener('loadedmetadata', updateProgress);
  audio.addEventListener('durationchange', updateProgress);
  audio.addEventListener('timeupdate', updateProgress);
  audio.addEventListener('seeking', updateProgress);
  audio.addEventListener('seeked', updateProgress);
  updateProgress();
}

function buildGeneratedSongCard(item) {
  const key = registerGeneratedSongItem(item);
  const keyArg = escJsSingle(key);
  const url = escHtml(item.url || '');
  const title = escHtml(item.title || 'AI 生成歌曲');
  const model = escHtml(item.model || 'lyria-3-pro-preview');
  const mime = escHtml(item.mime_type || 'audio/mpeg');
  return `<div class="generated-song-card" data-song-key="${escHtml(key)}">
    <div class="generated-song-main">
      <div class="generated-song-icon" aria-hidden="true"><span></span></div>
      <div class="generated-song-copy">
        <div class="generated-song-title">${title}</div>
        <div class="generated-song-meta">${model}</div>
      </div>
    </div>
    <div class="generated-song-actions">
      <button type="button" class="generated-song-open" onclick="openGeneratedSongPlayer('${keyArg}')">打开播放器</button>
      <audio controls preload="metadata" src="${url}" type="${mime}"></audio>
    </div>
  </div>`;
}

function buildDateSummaryCard(item) {
  const title = escHtml(item.title || '约会');
  const summary = escHtml(item.summary || '');
  return `<div class="date-summary-card">
    <div class="date-summary-kicker">刚刚完成了约会</div>
    <div class="date-summary-title">${title}</div>
    ${summary ? `<div class="date-summary-text">${summary}</div>` : ''}
  </div>`;
}

function safeHttpUrl(value) {
  try {
    const url = new URL(value || '', window.location.href);
    return (url.protocol === 'http:' || url.protocol === 'https:') ? url.href : '';
  } catch (e) {
    return '';
  }
}

function getExternalLinkBridge() {
  try { if (window.AionExternal) return window.AionExternal; } catch (e) {}
  try { if (window.parent && window.parent.AionExternal) return window.parent.AionExternal; } catch (e) {}
  try { if (window.top && window.top.AionExternal) return window.top.AionExternal; } catch (e) {}
  return null;
}

function openExternalLink(event, href) {
  const url = safeHttpUrl(href);
  if (!url) return true;
  const bridge = getExternalLinkBridge();
  if (!bridge || typeof bridge.open !== 'function') return true;
  if (event && typeof event.preventDefault === 'function') event.preventDefault();
  bridge.open(url);
  return false;
}

function buildLinkPreviewCard(item) {
  const href = safeHttpUrl(item?.url || '');
  if (!href) return '';
  let host = '';
  try { host = new URL(href).hostname.replace(/^www\./, ''); } catch (e) {}
  const title = escHtml(item.title || item.site_name || host || href);
  const description = escHtml(item.description || '');
  const source = escHtml(item.site_name || host || '网页链接');
  const image = safeHttpUrl(item.image || '');
  const favicon = safeHttpUrl(item.favicon || '');
  const thumb = image
    ? `<img class="link-preview-thumb-img" src="${escHtml(image)}" alt="" loading="lazy">`
    : `<div class="link-preview-thumb-placeholder">URL</div>`;
  const icon = favicon ? `<img class="link-preview-favicon" src="${escHtml(favicon)}" alt="" loading="lazy">` : '';
  const hrefArg = escJsSingle(href);
  return `<a class="link-preview-card" href="${escHtml(href)}" target="_blank" rel="noopener noreferrer" onclick="return openExternalLink(event,'${hrefArg}')">
    <span class="link-preview-thumb">${thumb}</span>
    <span class="link-preview-body">
      <span class="link-preview-title">${title}</span>
      ${description ? `<span class="link-preview-desc">${description}</span>` : ''}
      <span class="link-preview-source">${icon}${source}</span>
    </span>
  </a>`;
}

function renderAttachments(atts) {
  if (!atts || !atts.length) return '';
  let mediaHtml = '';
  let capsuleHtml = '';
  let voiceHtml = '';
  let wishHtml = '';
  const aiName = worldBook.ai_name || 'AI';
  atts.forEach(item => {
    if (typeof item === 'object' && item.type === 'luckin_payment') {
      mediaHtml += buildLuckinPaymentCard(item);
    } else if (typeof item === 'object' && item.type === 'date_summary') {
      mediaHtml += buildDateSummaryCard(item);
    } else if (typeof item === 'object' && item.type === 'link_preview') {
      mediaHtml += buildLinkPreviewCard(item);
    } else if (typeof item === 'object' && item.type === 'wish_fulfillment') {
      wishHtml += buildWishFulfillmentCard(item);
    } else if (typeof item === 'object' && item.type === 'music') {
      capsuleHtml += `<div class="music-capsule" onclick="openInNetease(${item.id})">🎵 ${escHtml(aiName)}给你点播歌曲《${escHtml(item.name)}》</div>`;
    } else if (typeof item === 'object' && item.type === 'generated_song') {
      mediaHtml += buildGeneratedSongCard(item);
    } else if (typeof item === 'object' && item.type === 'voice') {
      const dur = item.duration || 0;
      const durText = dur >= 60 ? `${Math.floor(dur/60)}:${String(Math.floor(dur%60)).padStart(2,'0')}` : `${Math.floor(dur)}"`;
      const barCount = Math.max(3, Math.min(12, Math.floor(dur * 1.5)));
      const bars = Array.from({length: barCount}, () => {
        const h = 3 + Math.floor(Math.random() * 11);
        return `<span style="height:${h}px"></span>`;
      }).join('');
      const url = escHtml(item.url || '');
      const transcript = item.transcript || '';
      voiceHtml += `<div class="voice-wrapper">`;
      voiceHtml += `<div class="voice-bubble" onclick="playVoiceMsg(this,'${url}')" data-url="${url}">`;
      voiceHtml += `<span class="vb-play">▶</span><span class="vb-waves">${bars}</span><span class="vb-dur">${durText}</span></div>`;
      if (transcript) {
        voiceHtml += `<div class="voice-transcript">${escHtml(transcript)}</div>`;
      }
      voiceHtml += `</div>`;
    } else if (typeof item === 'object' && item.type === 'video_clip') {
      const dur = item.duration || 0;
      const durText = dur >= 60 ? `${Math.floor(dur/60)}:${String(Math.floor(dur%60)).padStart(2,'0')}` : `${Math.floor(dur)}"`;
      const url = escHtml(item.url || '');
      const transcript = item.transcript || '';
      voiceHtml += `<div class="voice-wrapper">`;
      voiceHtml += `<div class="voice-bubble video-clip-bubble" onclick="playVideoClip(this,'${url}')" data-url="${url}">`;
      voiceHtml += `<span class="vb-play">▶</span><span style="font-size:13px">📹 视频片段</span><span class="vb-dur">${durText}</span></div>`;
      if (transcript) {
        voiceHtml += `<div class="voice-transcript">${escHtml(transcript)}</div>`;
      }
      voiceHtml += `</div>`;
    } else {
      const url = typeof item === 'string' ? item : (item && item.url ? item.url : '');
      if (/\.(mp4|webm|mov)$/i.test(url)) mediaHtml += `<video src="${escHtml(url)}" controls preload="metadata"></video>`;
      else if (/\.(mp3|wav|m4a|aac|ogg)$/i.test(url)) mediaHtml += `<audio src="${escHtml(url)}" controls preload="metadata"></audio>`;
      else if (url) mediaHtml += `<img src="${escHtml(url)}" ${imageInteractionAttrs()}>`;
    }
  });
  let html = '';
  if (voiceHtml) html += voiceHtml;
  if (wishHtml) html += wishHtml;
  if (mediaHtml) html += '<div class="msg-media">' + mediaHtml + '</div>';
  if (capsuleHtml) html += capsuleHtml;
  return html;
}

async function handleFileSelect(input) {
  for (const file of input.files) {
    const fd = new FormData();
    fd.append('file', file);
    const res = await fetch('/api/upload', {method:'POST', body: fd});
    const data = await res.json();
    if (data.error) { alert(data.error); continue; }
    pendingAttachments.push(data);
  }
  input.value = '';
  renderPreview();
}

// 粘贴图片到输入框
document.addEventListener('DOMContentLoaded', () => {
  const input = $('input');
  if (input) input.addEventListener('paste', async (e) => {
    const items = e.clipboardData && e.clipboardData.items;
    if (!items) return;
    for (const item of items) {
      if (!item.type.startsWith('image/')) continue;
      e.preventDefault();
      const file = item.getAsFile();
      if (!file) continue;
      const fd = new FormData();
      fd.append('file', file);
      const res = await fetch('/api/upload', {method:'POST', body: fd});
      const data = await res.json();
      if (data.error) { alert(data.error); continue; }
      pendingAttachments.push(data);
      renderPreview();
    }
  });
});

function renderPreview() {
  const area = $('previewArea');
  if (!pendingAttachments.length) { area.className = 'preview-area'; area.innerHTML = ''; return; }
  area.className = 'preview-area has-files';
  area.innerHTML = pendingAttachments.map((a, i) => {
    const isVid = a.type && a.type.startsWith('video/');
    const media = isVid ? `<video src="${a.url}" muted></video>` : `<img src="${a.url}">`;
    return `<div class="preview-item">${media}<button class="preview-remove" onclick="removeAttachment(${i})">✕</button></div>`;
  }).join('');
}

function removeAttachment(i) {
  pendingAttachments.splice(i, 1);
  renderPreview();
}

// ── 附加功能菜单 ──
function positionPlusMenu() {
  const m = $('plusMenu');
  const btn = document.querySelector('.plus-menu-wrap .upload-btn');
  if (!m || !btn) return;
  if (m.parentElement !== document.body) document.body.appendChild(m);
  m.classList.add('show');
  const btnRect = btn.getBoundingClientRect();
  const menuRect = m.getBoundingClientRect();
  const gap = 10;
  const pad = 12;
  const left = Math.min(Math.max(pad, btnRect.left), window.innerWidth - menuRect.width - pad);
  const top = Math.max(pad, btnRect.top - menuRect.height - gap);
  m.style.left = `${left}px`;
  m.style.top = `${top}px`;
}
function togglePlusMenu() {
  const m = $('plusMenu');
  if (m.classList.contains('show')) closePlusMenu();
  else positionPlusMenu();
}
function closePlusMenu() {
  $('plusMenu').classList.remove('show');
}
document.addEventListener('click', e => {
  const wrap = document.querySelector('.plus-menu-wrap');
  const menu = $('plusMenu');
  if (menu && (wrap?.contains(e.target) || menu.contains(e.target))) return;
  closePlusMenu();
});
window.addEventListener('resize', () => {
  const m = $('plusMenu');
  if (m?.classList.contains('show')) positionPlusMenu();
});

// ── 拍照功能 ──
let _camOverlay = null;
let _camStream = null;
let _camUseNative = false;
let _camNativeTimer = null;
let _camFacing = 'environment';

function openCamera() {
  if (_camOverlay) _camOverlay.remove();
  _camFacing = 'environment';
  _camOverlay = document.createElement('div');
  _camOverlay.className = 'camera-overlay show';
  _camOverlay.innerHTML = `
    <div class="camera-preview">
      <video id="camVideo" autoplay playsinline muted></video>
      <img id="camImg" style="display:none">
    </div>
    <div class="camera-bar">
      <button class="cam-close-btn" onclick="closeCamera()">✕</button>
      <button class="cam-shutter-btn" onclick="capturePhoto()">📷</button>
      <button class="cam-flip-btn" onclick="flipCam()">🔄</button>
    </div>
  `;
  document.body.appendChild(_camOverlay);
  startCam();
}

async function startCam() {
  // 1) 先尝试 getUserMedia
  try {
    _camStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: _camFacing, width: { ideal: 640 }, height: { ideal: 480 } },
      audio: false
    });
    const vid = document.getElementById('camVideo');
    if (vid) {
      vid.srcObject = _camStream;
      vid.style.transform = _camFacing === 'user' ? 'scaleX(-1)' : 'none';
      vid.style.display = 'block';
      vid.play().catch(() => {});
    }
    const img = document.getElementById('camImg');
    if (img) img.style.display = 'none';
    _camUseNative = false;
    return;
  } catch (e) {
    console.warn('[Camera] getUserMedia failed:', e);
  }
  // 2) 回退到原生 CameraBridge
  if (window.AionCamera) {
    const ok = window.AionCamera.start(_camFacing === 'user' ? 'user' : 'environment');
    if (ok) {
      _camUseNative = true;
      const vid = document.getElementById('camVideo');
      const img = document.getElementById('camImg');
      if (vid) vid.style.display = 'none';
      if (img) {
        img.style.display = 'block';
        img.style.transform = _camFacing === 'user' ? 'scaleX(-1)' : 'none';
      }
      pollCamFrame();
      return;
    }
  }
  alert('无法打开摄像头');
  closeCamera();
}

function pollCamFrame() {
  if (!_camUseNative || !window.AionCamera) return;
  const frame = window.AionCamera.getFrame();
  if (frame) {
    const img = document.getElementById('camImg');
    if (img) img.src = 'data:image/jpeg;base64,' + frame;
  }
  _camNativeTimer = requestAnimationFrame(pollCamFrame);
}

function stopCam() {
  if (_camNativeTimer) { cancelAnimationFrame(_camNativeTimer); _camNativeTimer = null; }
  if (_camUseNative && window.AionCamera) { window.AionCamera.stop(); _camUseNative = false; }
  if (_camStream) { _camStream.getTracks().forEach(t => t.stop()); _camStream = null; }
}

function closeCamera() {
  stopCam();
  if (_camOverlay) { _camOverlay.remove(); _camOverlay = null; }
}

async function flipCam() {
  _camFacing = _camFacing === 'environment' ? 'user' : 'environment';
  if (_camUseNative && window.AionCamera) {
    window.AionCamera.flip();
    const img = document.getElementById('camImg');
    if (img) img.style.transform = _camFacing === 'user' ? 'scaleX(-1)' : 'none';
  } else {
    stopCam();
    await startCam();
  }
}

async function capturePhoto() {
  let dataUrl = null;
  if (_camUseNative && window.AionCamera) {
    const b64 = window.AionCamera.capture();
    if (b64) dataUrl = 'data:image/jpeg;base64,' + b64;
  } else if (_camStream) {
    const videoEl = document.getElementById('camVideo');
    if (videoEl) {
      const canvas = document.createElement('canvas');
      canvas.width = videoEl.videoWidth || 640;
      canvas.height = videoEl.videoHeight || 480;
      const ctx = canvas.getContext('2d');
      ctx.drawImage(videoEl, 0, 0, canvas.width, canvas.height);
      dataUrl = canvas.toDataURL('image/jpeg', 0.85);
    }
  }
  if (!dataUrl) { alert('拍照失败'); return; }
  closeCamera();
  // 转 blob 上传
  const resp = await fetch(dataUrl);
  const blob = await resp.blob();
  const fd = new FormData();
  fd.append('file', blob, 'photo_' + Date.now() + '.jpg');
  const res = await fetch('/api/upload', { method: 'POST', body: fd });
  const data = await res.json();
  if (data.error) { alert(data.error); return; }
  pendingAttachments.push(data);
  renderPreview();
}

// ── 语音消息播放 ──
let _voiceAudio = null;
function playVoiceMsg(el, url) {
  // 如果正在播放同一条，停止
  if (_voiceAudio && el.classList.contains('playing')) {
    _voiceAudio.pause(); _voiceAudio = null;
    el.classList.remove('playing');
    el.querySelector('.vb-play').textContent = '▶';
    return;
  }
  // 停止之前的播放
  document.querySelectorAll('.voice-bubble.playing').forEach(b => {
    b.classList.remove('playing');
    b.querySelector('.vb-play').textContent = '▶';
  });
  if (_voiceAudio) { _voiceAudio.pause(); _voiceAudio = null; }
  _voiceAudio = new Audio(url);
  el.classList.add('playing');
  el.querySelector('.vb-play').textContent = '⏸';
  _voiceAudio.play().catch(() => {});
  _voiceAudio.onended = () => {
    el.classList.remove('playing');
    el.querySelector('.vb-play').textContent = '▶';
    _voiceAudio = null;
  };
}

function playVideoClip(el, url) {
  // 打开一个全屏视频播放器
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.9);z-index:99999;display:flex;align-items:center;justify-content:center;cursor:pointer';
  const video = document.createElement('video');
  video.src = url;
  video.controls = true;
  video.autoplay = true;
  video.style.cssText = 'max-width:90%;max-height:85%;border-radius:8px';
  overlay.appendChild(video);
  overlay.onclick = (e) => { if (e.target === overlay) { video.pause(); overlay.remove(); } };
  document.body.appendChild(overlay);
  video.play().catch(() => {});
}

// ── 语音消息录制模式 ──
let _voiceMode = false;
let _voiceRecording = false;
let _voiceMediaRecorder = null;
let _voiceStream = null;
let _voiceChunks = [];
let _voiceStartTime = 0;
let _voiceTimerInterval = null;
let _voiceOverlay = null;
let _voiceCancelled = false;
// Android 原生录音
let _voiceNativeChunks = [];
let _voiceUseNative = false;

function toggleVoiceMode() {
  _voiceMode = !_voiceMode;
  const inputRow = $('inputRow');
  const voiceRow = $('voiceModeRow');
  if (_voiceMode) {
    inputRow.classList.add('voice-hidden');
    voiceRow.classList.add('active');
    _initVoiceHoldBtn();
  } else {
    inputRow.classList.remove('voice-hidden');
    voiceRow.classList.remove('active');
  }
}

function _initVoiceHoldBtn() {
  const btn = $('voiceHoldBtn');
  if (btn._voiceInited) return;
  btn._voiceInited = true;

  // 鼠标事件（PC端）
  btn.addEventListener('mousedown', e => { e.preventDefault(); _voiceStartRecord(e); });
  document.addEventListener('mousemove', e => { if (_voiceRecording) _voiceTrackPointer(e); });
  document.addEventListener('mouseup', e => { if (_voiceRecording) _voiceStopRecord(e); });

  // 触摸事件（手机端）
  btn.addEventListener('touchstart', e => { e.preventDefault(); _voiceStartRecord(e.touches[0]); }, {passive:false});
  document.addEventListener('touchmove', e => { if (_voiceRecording) _voiceTrackPointer(e.touches[0]); }, {passive:false});
  document.addEventListener('touchend', e => { if (_voiceRecording) _voiceStopRecord(e.changedTouches[0]); });
  document.addEventListener('touchcancel', e => { if (_voiceRecording) { _voiceCancelled = true; _voiceStopRecord(e.changedTouches?.[0]); } });
}

async function _voiceStartRecord(evt) {
  if (_voiceRecording || sending) return;
  _voiceRecording = true;
  _voiceCancelled = false;
  _voiceChunks = [];
  _voiceNativeChunks = [];
  _voiceStartTime = Date.now();

  // 创建录制浮层
  _voiceOverlay = document.createElement('div');
  _voiceOverlay.className = 'voice-record-overlay active';
  _voiceOverlay.innerHTML = `
    <div class="vr-bg"></div>
    <div class="vr-trash-zone" id="vrTrash">🗑️</div>
    <div class="vr-timer" id="vrTimer">0:00</div>
    <div class="vr-hint" id="vrHint">↑ 上滑取消</div>
  `;
  document.body.appendChild(_voiceOverlay);

  // 计时器
  _voiceTimerInterval = setInterval(() => {
    const sec = Math.floor((Date.now() - _voiceStartTime) / 1000);
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    const timer = document.getElementById('vrTimer');
    if (timer) timer.textContent = `${m}:${String(s).padStart(2, '0')}`;
  }, 200);

  // 按钮状态
  $('voiceHoldBtn').classList.add('recording');
  $('voiceHoldBtn').textContent = '松开 发送';

  // 开始录音
  _voiceUseNative = false;
  try {
    _voiceStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    _voiceMediaRecorder = new MediaRecorder(_voiceStream, { mimeType: _getVoiceMime() });
    _voiceMediaRecorder.ondataavailable = e => { if (e.data.size > 0) _voiceChunks.push(e.data); };
    _voiceMediaRecorder.start();
  } catch (e) {
    console.warn('[VoiceMsg] getUserMedia failed, trying native bridge:', e);
    // 回退到 Android 原生录音桥
    if (window.AionAudio) {
      _voiceUseNative = true;
      _voiceNativeChunks = [];
      window._voiceNativeOnChunk = (b64) => { _voiceNativeChunks.push(b64); };
      window.AionAudio.start();
    } else {
      alert('无法访问麦克风');
      _voiceCleanup();
      return;
    }
  }
}

function _getVoiceMime() {
  if (MediaRecorder.isTypeSupported('audio/webm;codecs=opus')) return 'audio/webm;codecs=opus';
  if (MediaRecorder.isTypeSupported('audio/webm')) return 'audio/webm';
  if (MediaRecorder.isTypeSupported('audio/mp4')) return 'audio/mp4';
  return '';
}

function _voiceTrackPointer(evt) {
  const trash = document.getElementById('vrTrash');
  const hint = document.getElementById('vrHint');
  if (!trash) return;
  const trashRect = trash.getBoundingClientRect();
  const cx = trashRect.left + trashRect.width / 2;
  const cy = trashRect.top + trashRect.height / 2;
  const dist = Math.sqrt((evt.clientX - cx) ** 2 + (evt.clientY - cy) ** 2);
  if (dist < 60) {
    trash.classList.add('hover');
    if (hint) hint.textContent = '松开 取消';
    _voiceCancelled = true;
  } else {
    trash.classList.remove('hover');
    if (hint) hint.textContent = '↑ 上滑取消';
    _voiceCancelled = false;
  }
}

async function _voiceStopRecord(evt) {
  if (!_voiceRecording) return;
  _voiceRecording = false;
  const duration = (Date.now() - _voiceStartTime) / 1000;

  // 检查是否在垃圾桶区域
  if (evt) {
    const trash = document.getElementById('vrTrash');
    if (trash) {
      const trashRect = trash.getBoundingClientRect();
      const cx = trashRect.left + trashRect.width / 2;
      const cy = trashRect.top + trashRect.height / 2;
      const dist = Math.sqrt((evt.clientX - cx) ** 2 + (evt.clientY - cy) ** 2);
      if (dist < 60) _voiceCancelled = true;
    }
  }

  if (_voiceCancelled || duration < 0.5) {
    _voiceCleanup();
    return;
  }

  let audioBlob;
  if (_voiceUseNative) {
    // Android 原生录音：PCM chunks → WAV
    if (window.AionAudio) window.AionAudio.stop();
    audioBlob = _buildWavFromNativeChunks(_voiceNativeChunks);
  } else {
    // MediaRecorder：等待停止并获取 blob
    if (_voiceMediaRecorder && _voiceMediaRecorder.state !== 'inactive') {
      audioBlob = await new Promise(resolve => {
        _voiceMediaRecorder.onstop = () => {
          resolve(new Blob(_voiceChunks, { type: _voiceMediaRecorder.mimeType || 'audio/webm' }));
        };
        _voiceMediaRecorder.stop();
      });
    }
  }

  _voiceCleanup();

  if (!audioBlob || audioBlob.size < 100) return;

  // 上传 + 转写 + 发送
  await _voiceSendMessage(audioBlob, duration);
}

function _voiceCleanup() {
  if (_voiceTimerInterval) { clearInterval(_voiceTimerInterval); _voiceTimerInterval = null; }
  if (_voiceOverlay) { _voiceOverlay.remove(); _voiceOverlay = null; }
  if (_voiceStream) { _voiceStream.getTracks().forEach(t => t.stop()); _voiceStream = null; }
  if (_voiceMediaRecorder) { try { _voiceMediaRecorder.stop(); } catch {} _voiceMediaRecorder = null; }
  if (_voiceUseNative && window.AionAudio) { try { window.AionAudio.stop(); } catch {} }
  _voiceRecording = false;
  _voiceChunks = [];
  _voiceNativeChunks = [];
  const btn = $('voiceHoldBtn');
  if (btn) { btn.classList.remove('recording'); btn.textContent = '按住 说话'; }
}

function _buildWavFromNativeChunks(chunks) {
  // 将 base64 PCM 块合并为 WAV 文件
  let totalLen = 0;
  const bufs = chunks.map(b64 => {
    const bin = atob(b64);
    const buf = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
    totalLen += buf.length;
    return buf;
  });
  // WAV header
  const sampleRate = 16000;
  const numChannels = 1;
  const bitsPerSample = 16;
  const header = new ArrayBuffer(44);
  const view = new DataView(header);
  const writeStr = (o, s) => { for (let i = 0; i < s.length; i++) view.setUint8(o + i, s.charCodeAt(i)); };
  writeStr(0, 'RIFF');
  view.setUint32(4, 36 + totalLen, true);
  writeStr(8, 'WAVE');
  writeStr(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, numChannels, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * numChannels * bitsPerSample / 8, true);
  view.setUint16(32, numChannels * bitsPerSample / 8, true);
  view.setUint16(34, bitsPerSample, true);
  writeStr(36, 'data');
  view.setUint32(40, totalLen, true);
  const wavBuf = new Uint8Array(44 + totalLen);
  wavBuf.set(new Uint8Array(header), 0);
  let offset = 44;
  for (const buf of bufs) { wavBuf.set(buf, offset); offset += buf.length; }
  return new Blob([wavBuf], { type: 'audio/wav' });
}

async function _voiceSendMessage(audioBlob, duration) {
  if (!currentConvId || sending) return;

  // 1. 上传音频文件
  const ext = audioBlob.type.includes('wav') ? 'wav' : (audioBlob.type.includes('mp4') ? 'mp4' : 'webm');
  const fd = new FormData();
  fd.append('file', audioBlob, `voice_${Date.now()}.${ext}`);
  let uploadRes;
  try {
    const res = await fetch('/api/upload', { method: 'POST', body: fd });
    uploadRes = await res.json();
    if (uploadRes.error) { alert(uploadRes.error); return; }
  } catch (e) {
    alert('语音上传失败'); return;
  }

  // 2. 转写音频（失败自动重试一次）
  const fd2 = new FormData();
  fd2.append('file', audioBlob, `voice.${ext}`);
  let transcript = '';
  for (let _try = 0; _try < 2; _try++) {
    try {
      const body2 = _try === 0 ? fd2 : (() => { const f = new FormData(); f.append('file', audioBlob, `voice.${ext}`); return f; })();
      const res2 = await fetch('/api/voice/transcribe', { method: 'POST', body: body2 });
      const r2 = await res2.json();
      transcript = r2.text || '';
      if (transcript) break;
      console.warn(`[VoiceMsg] Transcribe attempt ${_try+1} returned empty, ${_try === 0 ? 'retrying...' : 'giving up'}`);
    } catch (e) {
      console.warn(`[VoiceMsg] Transcribe attempt ${_try+1} failed:`, e);
    }
  }

  // 3. 构建语音附件
  const voiceAtt = {
    type: 'voice',
    url: uploadRes.url,
    duration: Math.round(duration * 10) / 10,
    transcript: transcript
  };

  // 4. 发送消息
  sending = true;
  _showStopBtn();

  const attachments = [voiceAtt];
  const tempUserMsg = { id: "temp_user", conv_id: currentConvId, role: "user", content: "", created_at: Date.now()/1000, attachments };
  upsertCurrentMessage(tempUserMsg);
  renderMessages();
  scrollBottom();

  _abortController = new AbortController();
  try {
    const contextLimit = parseInt($("contextSlider").value) || 30;
    const temperature = parseFloat($("tempSlider").value);
    const maxTokens = _getMaxTokens();
    const res = await fetch(`/api/conversations/${currentConvId}/send`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ content: "", context_limit: contextLimit, attachments, whisper_mode: whisperMode, temperature, max_tokens: maxTokens, tts_enabled: ttsEnabled, tts_voice: ttsVoiceId, client_id: _clientId }),
      signal: _abortController.signal
    });
    // 复用和 send() 完全相同的 SSE 处理逻辑
    await _processSSEStream(res);
  } catch (err) {
    if (err.name !== 'AbortError') console.error('[VoiceMsg] Send error:', err);
  } finally {
    sending = false;
    streamingAiId = null;
    _abortController = null;
    _showSendBtn();
  }
}

// ── Android 原生录音桥回调 ──
// AudioBridge.java 会调用 _voiceNativeOnChunk(b64)
// 在 _voiceStartRecord 中设置 window._voiceNativeOnChunk 来接收 PCM chunks

function toggleMsgMenu(id) {
  const menu = document.getElementById('menu_' + id);
  const wasOpen = menu && menu.classList.contains('show');
  closeMsgMenus();
  if (!wasOpen && menu) menu.classList.add('show');
}
function closeMsgMenus() {
  document.querySelectorAll('.msg-menu.show').forEach(m => m.classList.remove('show'));
}
document.addEventListener('click', e => {
  if (e.target.closest?.('.msg-feedback-popover, .msg-reasoning-popover, .msg-feedback-btn')) return;
  closeMsgFeedbackPopover();
  closeMsgReasoningPopover();
});

document.addEventListener('click', () => {
  closeMsgMenus();
});

function autoResize(el) {
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 200) + "px";
}

function openSidebar() { $("sidebar").classList.add("open"); $("overlay").classList.add("show"); }
function closeSidebar() { $("sidebar").classList.remove("open"); $("overlay").classList.remove("show"); }

// ── 设置/世界书/定位 → 已拆分为独立页面 ──

// ── 文件管理 ──
let fmCurrentConvId = null;

async function openFileManager() {
  $("fmList").style.display = "";
  $("fmEditor").style.display = "none";
  $("fileModal").classList.add("show");
  await loadFiles();
}

function closeFileManager() { $("fileModal").classList.remove("show"); }

async function loadFiles() {
  const files = await api("GET", "/api/files");
  $("fmFileList").innerHTML = files.length === 0
    ? '<div class="fm-empty">暂无文件，发送消息后自动生成</div>'
    : files.map(f => `
      <div class="fm-file-item" onclick="fmOpen('${f.conv_id}')">
        <span class="fm-title">${escHtml(f.filename)}</span>
        <span class="fm-size">${(f.size/1024).toFixed(1)}KB</span>
      </div>
    `).join("");
}

async function fmOpen(convId) {
  fmCurrentConvId = convId;
  const data = await api("GET", `/api/files/${convId}`);
  if (data.error) { alert(data.error); return; }
  $("fmEditorTitle").textContent = "编辑: " + data.filename;
  $("fmContent").value = data.content;
  $("fmList").style.display = "none";
  $("fmEditor").style.display = "flex";
}

function fmBack() {
  $("fmList").style.display = "";
  $("fmEditor").style.display = "none";
}

async function fmSave() {
  if (!fmCurrentConvId) return;
  const res = await api("PUT", `/api/files/${fmCurrentConvId}`, { content: $("fmContent").value });
  if (res.ok) {
    alert("保存成功，已同步到对话！");
    if (fmCurrentConvId === currentConvId) {
      setCurrentMessages(await api("GET", `/api/conversations/${currentConvId}/messages?limit=${MSG_PAGE_SIZE}`));
      hasMoreMessages = currentMessages.length >= MSG_PAGE_SIZE;
      renderMessages();
      conversations = await api("GET", "/api/conversations");
      renderConvList();
      const conv = conversations.find(c => c.id === currentConvId);
      if (conv) $("chatTitle").textContent = conv.title;
    }
    fmBack();
    await loadFiles();
  }
}

init().then(() => {
  // 初始化完成后自动打开 Home 作为默认页面
  setTimeout(() => openSubPage('/'), 100);
});

// ── 摄像头/监控日志/记忆库 → 已拆分为独立页面 ──

// ── 静音音频保活（阻止浏览器后台节流） ──
let _keepAliveCtx = null;
function startSilentKeepAlive() {
  try {
    if (_keepAliveCtx) return;
    _keepAliveCtx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = _keepAliveCtx.createOscillator();
    const gain = _keepAliveCtx.createGain();
    gain.gain.value = 0;          // 完全静音
    osc.connect(gain);
    gain.connect(_keepAliveCtx.destination);
    osc.start();
    // 用户交互后 resume（Chrome 要求）
    const resume = () => { if (_keepAliveCtx.state === 'suspended') _keepAliveCtx.resume(); };
    document.addEventListener('click', resume, { once: true });
    document.addEventListener('keydown', resume, { once: true });
  } catch(e) { console.warn('keepalive audio failed:', e); }
}

// ── 系统通知（后台标签也能弹出） ──
function sendSystemNotification(title, body) {
  if (!('Notification' in window)) return;
  if (Notification.permission !== 'granted') return;
  try { new Notification(title, { body, icon: '/public/icon-192.png' }); } catch(e) {}
}

// 初始化 TTS
(function initTTS() {
  $('ttsToggle').checked = ttsEnabled;
  refreshTTSVoices();
})();

// ══════════════════════════════════════════════════
// ── 密语时刻：BLE 玩具控制 ──
// ══════════════════════════════════════════════════
const TOY_SERVICE_UUID = 0xEE01, TOY_WRITE_UUID = 0xEE03, TOY_NOTIFY_UUID = 0xEE02;
let toyDevice = null, toyServer = null, toyWriteChar = null, toyConnected = false;
let whisperMode = false;
let toyActivePreset = -1;

// 原生 BLE 回调（Android APK 的 BleBridge.java 通过 evaluateJavascript 调用）
// BLE 状态跨页面同步（BroadcastChannel）
const _bleCh = (typeof BroadcastChannel !== 'undefined') ? new BroadcastChannel('toy_ble_state') : null;
function _bleNotify(connected) { if (_bleCh) _bleCh.postMessage({ connected }); }
if (_bleCh) _bleCh.onmessage = function(ev) {
  toyConnected = !!ev.data.connected;
  toyUpdateUI();
  if (toyConnected) toyLog('已连接（来自聊天室）', 'wl-sys');
  else toyLog('已断开（来自聊天室）', 'wl-err');
};

window.toyNativeBle = {
  onConnected()      { toyConnected = true; toyUpdateUI(); toyLog('已连接 ♡', 'wl-sys'); _bleNotify(true); },
  onDisconnected()   { toyConnected = false; toyUpdateUI(); toyLog('断开', 'wl-err'); _bleNotify(false); },
  onError(msg)       { toyLog(msg, 'wl-err'); },
  onLog(msg)         { toyLog(msg, 'wl-sys'); }
};

const TOY_MOTORS = [
  { label:'震动', gearsSpec:'0001', modeSpec:'0002',
    modes:[{id:1,name:'全身酥麻'},{id:2,name:'渐入佳境'},{id:3,name:'循序渐进'},{id:4,name:'欢呼雀跃'}] },
  { label:'电流', gearsSpec:'0003', modeSpec:'0004',
    modes:[{id:1,name:'温柔涟漪'},{id:2,name:'娇舌搅动'},{id:3,name:'风驰快感'},{id:4,name:'浪潮不断'}] },
  { label:'吮吸', gearsSpec:'0007', modeSpec:'0008',
    modes:[{id:1,name:'连绵不绝'},{id:2,name:'深海暗涌'},{id:3,name:'爆裂冲刺'},{id:4,name:'浪潮不断'}] },
];
const TOY_PNAMES = ['微风轻拂','春水初生','暗流涌动','如梦似幻','情潮渐涨','烈焰焚身','极乐之巅','魂飞魄散','失控'];
const TOY_PICONS = ['🌸','💧','🌊','✨','🔥','💥','⚡','💀','🌀'];
const TOY_DEF_PRESETS = [
  { motors:[{on:0,mode:1,speed:10},{on:0,mode:1,speed:0},{on:1,mode:1,speed:10}] },
  { motors:[{on:0,mode:1,speed:20},{on:0,mode:1,speed:10},{on:1,mode:3,speed:20}] },
  { motors:[{on:0,mode:2,speed:30},{on:0,mode:1,speed:20},{on:1,mode:2,speed:30}] },
  { motors:[{on:0,mode:2,speed:45},{on:0,mode:2,speed:25},{on:1,mode:4,speed:40}] },
  { motors:[{on:0,mode:3,speed:60},{on:1,mode:2,speed:20},{on:1,mode:2,speed:50}] },
  { motors:[{on:1,mode:3,speed:10},{on:1,mode:3,speed:30},{on:1,mode:4,speed:60}] },
  { motors:[{on:1,mode:2,speed:20},{on:1,mode:4,speed:40},{on:1,mode:4,speed:80}] },
  { motors:[{on:1,mode:1,speed:30},{on:1,mode:3,speed:80},{on:1,mode:3,speed:100}] },
  { motors:[{on:1,mode:4,speed:40},{on:1,mode:3,speed:90},{on:1,mode:3,speed:100}] },
];

let toyPresets = [];
function toyLoadPresets() {
  try { const s = localStorage.getItem('sosexy_presets_v3'); if (s) { toyPresets = JSON.parse(s); return; } } catch(e) {}
  toyPresets = JSON.parse(JSON.stringify(TOY_DEF_PRESETS));
}
function toySavePresets() { localStorage.setItem('sosexy_presets_v3', JSON.stringify(toyPresets)); }

function toyLog(msg, cls='') {
  const a = $('toyLogArea'); if (!a) return;
  const d = document.createElement('div'); d.className = cls;
  d.textContent = `[${new Date().toLocaleTimeString('zh-CN',{hour12:false})}] ${msg}`;
  a.appendChild(d); a.scrollTop = a.scrollHeight;
}

function toyHexToBytes(h) { const b=[]; for(let i=0;i<h.length;i+=2) b.push(parseInt(h.substr(i,2),16)); return b; }
function toyToHex2(n) { return n.toString(16).padStart(2,'0'); }
function toyBuildDualCmd(s1,v1,s2,v2) { return '02'+s1+'11'+toyToHex2(v1)+s2+'11'+toyToHex2(v2); }
function toyBuildStopCmd() { return '03000111000003110000071100'; }
function toySleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function toySendData2(hexCmd) {
  // 原生 BLE 桥接（Android APK）
  if (window.AionBle && window.AionBle.isConnected()) {
    toyLog('→ ' + hexCmd, 'wl-send');
    window.AionBle.sendData(hexCmd);
    return;
  }
  // Web Bluetooth（浏览器）
  if (!toyWriteChar) { toyLog('未连接','wl-err'); return; }
  const full = '00' + hexCmd;
  toyLog('→ ' + hexCmd, 'wl-send');
  const data = toyHexToBytes(full), chunks = [];
  for (let i = 0; i < data.length; i += 18) chunks.push(data.slice(i, i+18));
  const rnd = Math.floor(Math.random() * 255), pkts = [];
  for (let i = 0; i < chunks.length; i++) pkts.push([rnd, i+1, ...chunks[i]]);
  if (chunks.length > 0 && chunks[chunks.length-1].length === 18) pkts.push([rnd, chunks.length+1]);
  for (let i = 0; i < pkts.length; i++) {
    const p = new Uint8Array(pkts[i]);
    try {
      if (toyWriteChar.properties.write) await toyWriteChar.writeValueWithResponse(p);
      else await toyWriteChar.writeValueWithoutResponse(p);
    } catch(e) { toyLog('写入失败:'+e.message,'wl-err'); return; }
    if (pkts.length > 1 && i < pkts.length-1) await toySleep(30);
  }
}

async function toyApplyPreset(p) {
  for (let i = 0; i < 3; i++) {
    const m = p.motors[i], mo = TOY_MOTORS[i];
    await toySendData2(toyBuildDualCmd(mo.modeSpec, m.mode||1, mo.gearsSpec, m.on ? m.speed : 0));
    await toySleep(80);
  }
}

async function toyActivatePreset(idx) {
  toyActivePreset = idx; toyRenderGrid();
  const p = toyPresets[idx];
  toyLog('⚡ ' + TOY_PNAMES[idx], 'wl-sys');
  await toyApplyPreset(p);
}

function toyStopAll() {
  toyActivePreset = -1;
  toySendData2(toyBuildStopCmd());
  toyLog('⏹ 停止', 'wl-sys');
  toyRenderGrid();
}

// 处理 AI 发送的 [TOY:x] 指令
function toyExecCmd(cmd) {
  cmd = cmd.trim().toUpperCase();
  if (cmd === 'STOP' || cmd === '0') { toyStopAll(); return; }
  const n = parseInt(cmd);
  if (n >= 1 && n <= 9) { toyActivatePreset(n - 1); return; }
  toyLog('无效指令:' + cmd, 'wl-err');
}

function showToyCapsule(msgId, commands) {
  if (!msgId || !commands || !commands.length) return;
  const row = document.getElementById('m_' + msgId);
  if (!row) return;
  const msgBody = row.querySelector('.msg-body');
  if (!msgBody) return;
  commands.forEach(cmd => {
    const c = cmd.trim().toUpperCase();
    let label;
    if (c === 'STOP' || c === '0') label = '❤️ 停止';
    else { const n = parseInt(c); label = (n >= 1 && n <= 9) ? `❤️ ${TOY_PNAMES[n-1]}` : `❤️ ${cmd}`; }
    const pill = document.createElement('div');
    pill.className = 'toy-capsule';
    pill.textContent = label;
    msgBody.appendChild(pill);
  });
  scrollBottom();
}

function toyRenderGrid() {
  const g = $('toyPresetGrid'); if (!g) return;
  g.innerHTML = '';
  for (let i = 0; i < 9; i++) {
    const d = document.createElement('div');
    d.className = 'whisper-p-btn' + (i === toyActivePreset ? ' active' : '');
    d.innerHTML = `<span class="wp-icon">${TOY_PICONS[i]}</span><span class="wp-name">${TOY_PNAMES[i]}</span><button class="wp-edit" onclick="event.stopPropagation();toyOpenEditor(${i})">⚙</button>`;
    d.onclick = () => { if (toyConnected) toyActivatePreset(i); else toyLog('请先连接','wl-err'); };
    g.appendChild(d);
  }
}

async function toyToggleConnect() {
  if (toyConnected) { toyDisconnect(); return; }
  // 原生 BLE 桥接（Android APK）
  if (window.AionBle) { window.AionBle.connect(); return; }
  // Web Bluetooth（浏览器）
  if (!navigator.bluetooth) { toyLog('此浏览器不支持 Web Bluetooth','wl-err'); return; }
  try {
    toyLog('搜索中...', 'wl-sys');
    toyDevice = await navigator.bluetooth.requestDevice({ filters: [{ namePrefix: 'SOSEXY' }], optionalServices: [TOY_SERVICE_UUID] });
    toyLog(toyDevice.name || '已找到设备', 'wl-sys');
    toyDevice.addEventListener('gattserverdisconnected', () => { toyConnected = false; toyWriteChar = null; toyUpdateUI(); toyLog('断开','wl-err'); _bleNotify(false); });
    toyServer = await toyDevice.gatt.connect();
    const svc = await toyServer.getPrimaryService(TOY_SERVICE_UUID);
    toyWriteChar = await svc.getCharacteristic(TOY_WRITE_UUID);
    try {
      const notifyChar = await svc.getCharacteristic(TOY_NOTIFY_UUID);
      await notifyChar.startNotifications();
    } catch(e) {}
    toyConnected = true;
    toyUpdateUI();
    toyLog('已连接 ♡', 'wl-sys');
    _bleNotify(true);
  } catch(e) { toyLog('连接失败:'+e.message, 'wl-err'); }
}

function toyDisconnect() {
  toyStopAll();
  if (window.AionBle) {
    window.AionBle.disconnect();
  } else if (toyDevice && toyDevice.gatt.connected) {
    toyDevice.gatt.disconnect();
  }
  toyConnected = false; toyWriteChar = null;
  toyUpdateUI(); toyLog('已断开', 'wl-sys');
  _bleNotify(false);
}

function toyUpdateUI() {
  const dot = $('toyDot'), label = $('toyConnLabel'), btn = $('toyConnBtn');
  if (dot) { dot.className = 'whisper-dot ' + (toyConnected ? 'on' : 'off'); }
  if (label) { label.textContent = toyConnected ? (toyDevice?.name || '已连接') : '未连接'; }
  if (btn) { btn.textContent = toyConnected ? '断开' : '连接'; }
}

function openWhisper() {
  closeSidebar();
  // 检查原生 BLE 桥接的实际连接状态
  if (window.AionBle && typeof window.AionBle.isConnected === 'function') {
    toyConnected = window.AionBle.isConnected();
  }
  toyLoadPresets();
  toyRenderGrid();
  toyUpdateUI();
  $('whisperModeToggle').checked = whisperMode;
  $('whisperModal').classList.add('show');
}
function closeWhisper() { $('whisperModal').classList.remove('show'); }

// ── 预设编辑器 ──
function toyOpenEditor(idx) {
  const p = toyPresets[idx], isLoop = idx === 8;
  let h = `<h3>${TOY_PICONS[idx]} ${TOY_PNAMES[idx]}</h3>`;
  for (let mi = 0; mi < 3; mi++) {
    const ms = p.motors[mi], mo = TOY_MOTORS[mi];
    h += `<div class="toy-me-block"><div class="toy-me-head"><span>${mo.label}</span>
    <label class="toggle-switch" style="transform:scale(.8)"><input type="checkbox" id="teo${mi}" ${ms.on?'checked':''}><span class="toggle-slider"></span></label>
    </div><div class="toy-chip-row" id="tem${mi}">
    ${mo.modes.map(md => `<span class="toy-chip${md.id===ms.mode?' sel':''}" data-mid="${md.id}" onclick="toyESel(${mi},${md.id})">${md.name}</span>`).join('')}
    </div><div class="toy-ed-speed"><label>速度</label>
    <input type="range" min="0" max="100" value="${ms.speed}" id="tes${mi}" oninput="document.getElementById('tev${mi}').textContent=this.value">
    <span class="toy-ed-sv" id="tev${mi}">${ms.speed}</span></div></div>`;
  }
  if (isLoop) {
    h += `<div style="margin-top:6px"><div class="toy-me-head"><span>🌀 循环步骤</span></div><div id="toyLsc"></div>
    <button class="toy-add-step" onclick="toyAddLS()">+ 添加步骤</button></div>`;
  }
  h += `<div class="toy-sheet-btns"><button class="toy-sb-cancel" onclick="toyCloseEditor()">取消</button><button class="toy-sb-save" onclick="toySaveEd(${idx})">保存</button></div>`;
  $('toyEditContent').innerHTML = h;
  $('toyEditorOverlay').classList.add('show');
  if (isLoop) { window._toyLS = JSON.parse(JSON.stringify(p.loopSteps || [])); toyRenderLS(); }
}

function toyESel(mi, mid) {
  document.querySelectorAll(`#tem${mi} .toy-chip`).forEach(c => c.classList.toggle('sel', parseInt(c.dataset.mid) === mid));
}

function toyRenderLS() {
  const c = $('toyLsc'); if (!c) return;
  c.innerHTML = window._toyLS.map((s, i) => `<div class="toy-ls"><span class="sn">${i+1}</span>
  <select onchange="window._toyLS[${i}].presetIdx=+this.value">${[0,1,2,3,4,5,6,7].map(j => `<option value="${j}"${s.presetIdx===j?' selected':''}>${j+1}.${TOY_PNAMES[j]}</option>`).join('')}</select>
  <input type="number" min="1" max="60" value="${s.durationSec}" onchange="window._toyLS[${i}].durationSec=+this.value||3">s
  <button class="del" onclick="window._toyLS.splice(${i},1);toyRenderLS()">×</button></div>`).join('');
}

function toyAddLS() { window._toyLS.push({ presetIdx: 0, durationSec: 3 }); toyRenderLS(); }

function toySaveEd(idx) {
  const p = toyPresets[idx];
  for (let mi = 0; mi < 3; mi++) {
    p.motors[mi].on = document.getElementById(`teo${mi}`).checked ? 1 : 0;
    const sc = document.querySelector(`#tem${mi} .toy-chip.sel`);
    if (sc) p.motors[mi].mode = parseInt(sc.dataset.mid);
    p.motors[mi].speed = parseInt(document.getElementById(`tes${mi}`).value);
  }
  if (idx === 8) p.loopSteps = window._toyLS.filter(s => s.durationSec > 0);
  toySavePresets(); toyCloseEditor(); toyRenderGrid();
  toyLog(`预设${idx+1}已保存`, 'wl-sys');
}

function toyCloseEditor() { $('toyEditorOverlay').classList.remove('show'); }

function onWhisperModeChange() {
  whisperMode = $('whisperModeToggle').checked;
  toyLog(whisperMode ? '🔮 密语模式已开启' : '🔮 密语模式已关闭', 'wl-sys');
}

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

// 页面加载时检查未领取的礼物
(async function checkPendingGifts() {
  try {
    const res = await fetch('/api/gift/pending');
    const data = await res.json();
    if (data.ok && data.gifts && data.gifts.length > 0) {
      data.gifts.forEach(g => _showGiftPopup(g));
    }
  } catch(e) {}
})();

function _showGiftPopup(gift) {
  if (!gift || !gift.id || _isGiftKnown(gift.id) || _giftQueue.some(g => g.id === gift.id)) return;
  _giftQueue.push(gift);
  if (!_giftShowing) _presentNextGift();
}
function _presentNextGift() {
  if (!_giftQueue.length) { _giftShowing = false; return; }
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
          <p class="gift-message-from" style="text-align:center;opacity:0.7;font-size:0.85em;margin-bottom:4px">—— from ${gift.sender === 'connor' ? (chatroomConfig.connor_name || '第二AI') : ((worldBook && worldBook.ai_name) || 'AI')} ——</p>
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
    _presentNextGift();
  }, 800);
}

// URL 参数检查：从主页点击密语时刻跳转
(function checkWhisperParam() {
  const params = new URLSearchParams(location.search);
  if (params.get('whisper') === '1') {
    setTimeout(() => openWhisper(), 500);
    history.replaceState(null, '', '/chat');
  }
})();

// ── 子页面 iframe 浮层逻辑 ──
let currentSubPage = null;
window.getCurrentConversationId = function() { return currentConvId || ''; };
const _subPageNames = {'/':'主页','/settings':'设置','/memory':'记忆库','/diary':'日记本','/worldbook':'世界书','/schedule':'日程','/camera':'摄像头','/monitor-logs':'监控日志','/location':'定位','/heart-whispers':'心语','/wishes':'许愿池'};
function parseSubPageColor(value) {
  if (!value || value === 'transparent') return null;
  const hexMatch = value.trim().match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
  if (hexMatch) {
    const hex = hexMatch[1].length === 3 ? hexMatch[1].split('').map(part => part + part).join('') : hexMatch[1];
    return { red: parseInt(hex.slice(0, 2), 16), green: parseInt(hex.slice(2, 4), 16), blue: parseInt(hex.slice(4, 6), 16), alpha: 1 };
  }
  const rgbMatch = value.match(/rgba?\(([^)]+)\)/i);
  if (!rgbMatch) return null;
  const parts = rgbMatch[1].split(/[\s,\/]+/).filter(Boolean).map(Number);
  if (parts.length < 3) return null;
  return { red: parts[0], green: parts[1], blue: parts[2], alpha: parts.length > 3 ? parts[3] : 1 };
}
function isSubPageLightColor(value) {
  const color = parseSubPageColor(value);
  if (!color || color.alpha <= 0.05) return true;
  return ((color.red * 299) + (color.green * 587) + (color.blue * 114)) / 1000 > 150;
}
function subPageColorToRgbString(color) {
  return `rgb(${Math.round(color.red)}, ${Math.round(color.green)}, ${Math.round(color.blue)})`;
}
function getSolidSubPageColor(foregroundValue, backgroundValue) {
  const foreground = parseSubPageColor(foregroundValue);
  const background = parseSubPageColor(backgroundValue) || parseSubPageColor('#fff9f5');
  if (!foreground || foreground.alpha <= 0.05) return subPageColorToRgbString(background);
  if (foreground.alpha >= 0.98) return subPageColorToRgbString(foreground);
  const alpha = foreground.alpha;
  return subPageColorToRgbString({
    red: foreground.red * alpha + background.red * (1 - alpha),
    green: foreground.green * alpha + background.green * (1 - alpha),
    blue: foreground.blue * alpha + background.blue * (1 - alpha)
  });
}
function getSubPageThemeChromeColors() {
  const theme = localStorage.getItem('aion_chat_theme') || document.body.dataset.theme || 'dark';
  return theme === 'light'
    ? { safe: '#eef3ff', frame: '#eef3ff', style: 'light' }
    : { safe: '#050923', frame: '#03061c', style: 'dark' };
}
function applySubPageThemeChrome() {
  const ov = $('subPageOverlay');
  const colors = getSubPageThemeChromeColors();
  ov.style.setProperty('--subpage-safe-bg', colors.safe);
  ov.style.setProperty('--subpage-frame-bg', colors.frame);
  if (window.AionStatusBar) window.AionStatusBar.setBarStyle(colors.style);
}
function resetSubPageChrome() {
  applySubPageThemeChrome();
}
function syncSubPageChromeFromFrame(frame = activeSubPageFrame) {
  if (!frame) return;
  const ov = $('subPageOverlay');
  let doc;
  try { doc = frame.contentDocument; } catch(e) { return; }
  if (!doc || !doc.body) return;
  const frameWindow = frame.contentWindow;
  const rootStyle = frameWindow.getComputedStyle(doc.documentElement);
  const bodyStyle = frameWindow.getComputedStyle(doc.body);
  const topBar = doc.querySelector('.top-bar, .chat-header');
  const topBarColor = topBar ? frameWindow.getComputedStyle(topBar).backgroundColor : '';
  const rootBg = bodyStyle.getPropertyValue('--bg').trim() || rootStyle.getPropertyValue('--bg').trim();
  const bodyBg = bodyStyle.backgroundColor;
  const pageBg = rootBg || bodyBg || '#fff9f5';
  const safeColor = getSolidSubPageColor(topBarColor, pageBg);
  ov.style.setProperty('--subpage-safe-bg', safeColor);
  ov.style.setProperty('--subpage-frame-bg', rootBg || bodyBg || '#fff9f5');
  if (window.AionStatusBar) window.AionStatusBar.setBarStyle(isSubPageLightColor(safeColor) ? 'light' : 'dark');
}
function syncSubPageMode(url) {
  const path = (() => {
    try { return new URL(url, location.origin).pathname; } catch(e) { return url || ''; }
  })();
  const isHome = path === '/';
  const isImmersive = path === '/wishes';
  const ov = $('subPageOverlay');
  ov.classList.toggle('home-subpage', isHome);
  ov.classList.toggle('immersive-subpage', isImmersive);
  if (isHome) resetSubPageChrome();
  else applySubPageThemeChrome();
  $('subPageTitle').textContent = _subPageNames[path] || '';
  if (window.AionStatusBar) {
    if (isHome) applyAionTheme(localStorage.getItem('aion_chat_theme') || document.body.dataset.theme || 'dark');
    else window.AionStatusBar.setBarStyle(getSubPageThemeChromeColors().style);
  }
}
const persistentSubPageFrames = new Map();
const transientSubPageFrame = $('subPageFrame');
let activeSubPageFrame = null;

function subPagePath(url) {
  try { return new URL(url, location.origin).pathname; } catch(e) { return url || ''; }
}

function shouldNavigatePersistentSubPage(frame, url) {
  try {
    const requested = new URL(url, location.origin);
    // Plain app launches keep the preserved page state. Explicit route params
    // (for example, a global-search message anchor) must navigate the frame.
    if (!requested.search && !requested.hash) return false;
    let current;
    try {
      current = new URL(frame.contentWindow.location.href, location.origin);
    } catch(e) {
      current = new URL(frame.src || 'about:blank', location.origin);
    }
    return current.pathname !== requested.pathname
      || current.search !== requested.search
      || current.hash !== requested.hash;
  } catch(e) {
    return false;
  }
}

function isPersistentSubPage(url) {
  const path = subPagePath(url);
  return path === '/' || path === '/chatroom' || path === '/health';
}

function syncHealthRingPageVisibility() {
  const overlay = $('subPageOverlay');
  const visible = !!(overlay
    && overlay.classList.contains('show')
    && activeSubPageFrame
    && subPagePath(currentSubPage) === '/health');
  try {
    if (window.AionRingBle
        && typeof window.AionRingBle.setHealthPageVisible === 'function') {
      window.AionRingBle.setHealthPageVisible(visible);
    }
  } catch(e) {}
}

function attachSubPageFrameLoad(frame) {
  frame.addEventListener('load', () => {
    if (frame !== activeSubPageFrame || !frame.src || frame.src === 'about:blank') return;
    requestAnimationFrame(() => {
      try { syncSubPageMode(frame.contentWindow.location.href); }
      catch(e) { syncSubPageMode(frame.src); }
      syncSubPageChromeFromFrame(frame);
    });
  });
}

function getSubPageFrame(url) {
  const path = subPagePath(url);
  if (!isPersistentSubPage(path)) {
    transientSubPageFrame.src = url;
    return transientSubPageFrame;
  }
  let frame = persistentSubPageFrames.get(path);
  if (!frame) {
    frame = document.createElement('iframe');
    frame.className = 'sub-page-frame';
    frame.setAttribute('sandbox', 'allow-same-origin allow-scripts allow-forms allow-popups allow-modals');
    frame.setAttribute('allow', 'autoplay');
    frame.dataset.persistentPath = path;
    attachSubPageFrameLoad(frame);
    $('subPageFrames').appendChild(frame);
    persistentSubPageFrames.set(path, frame);
    frame.src = url;
  } else if (shouldNavigatePersistentSubPage(frame, url)) {
    frame.src = url;
  }
  return frame;
}

attachSubPageFrameLoad(transientSubPageFrame);

function openSubPage(url) {
  closeSidebar();
  syncSubPageMode(url);
  const frame = getSubPageFrame(url);
  document.querySelectorAll('.sub-page-frame').forEach(item => {
    item.style.display = item === frame ? 'block' : 'none';
  });
  activeSubPageFrame = frame;
  if (subPagePath(url) === '/') {
    try {
      frame.contentDocument?.getElementById('screen')?.classList.remove('navigating');
    } catch(e) {}
  }
  $('subPageOverlay').classList.add('show');
  currentSubPage = url;
  syncHealthRingPageVisibility();
}
function closeSubPage(skipReload = false) {
  const ov = $('subPageOverlay');
  if (!ov.classList.contains('show')) return;
  ov.classList.remove('show');
  ov.classList.remove('home-subpage');
  ov.classList.remove('immersive-subpage');
  if (activeSubPageFrame === transientSubPageFrame) {
    transientSubPageFrame.src = 'about:blank';
  }
  if (activeSubPageFrame) activeSubPageFrame.style.display = 'none';
  activeSubPageFrame = null;
  currentSubPage = null;
  syncHealthRingPageVisibility();
  applyAionTheme(localStorage.getItem('aion_chat_theme') || document.body.dataset.theme || 'dark');
  // 回到聊天页后重新加载消息列表（拿到后台生成完成的新消息）
  if (!skipReload && currentConvId) {
    refreshCurrentConversationFromServer();
  }
}
// 导航到 Home（从任何功能页返回 Home）
function navigateToHome() {
  const path = (() => {
    try { return new URL(currentSubPage || '', location.origin).pathname; } catch(e) { return currentSubPage || ''; }
  })();
  if (path === '/') return; // 已经在 Home
  openSubPage('/');
}
// Android 原生返回键回调
function handleNativeBack() {
  const ov = $('subPageOverlay');
  if (ov && ov.classList.contains('show')) {
    const path = (() => {
      try { return new URL(currentSubPage || '', location.origin).pathname; } catch(e) { return currentSubPage || ''; }
    })();
    if (path === '/') {
      // 在 Home → 弹对话框
      return 'dialog';
    }
    // 在其他功能页 → 回到 Home
    navigateToHome();
    return 'handled';
  }
  // 无浮层（Chat 聊天界面）→ 打开 Home
  openSubPage('/');
  return 'handled';
}
window.addEventListener('popstate', function(e) {
  // 保留 popstate 以防浏览器产生历史条目，统一导航到 Home 或关闭
  if ($('subPageOverlay').classList.contains('show')) {
    const path = (() => {
      try { return new URL(currentSubPage || '', location.origin).pathname; } catch(e) { return currentSubPage || ''; }
    })();
    if (path !== '/') navigateToHome();
    else closeSubPage();
  }
});

// ── 转账弹窗 ──
function openTransferDialog() {
  const aiName = (worldBook && worldBook.ai_name) || 'AI';
  $('transferDialogTitle').textContent = `给【${aiName}】转账`;
  $('transferAmountInput').value = '';
  $('transferDialogOverlay').classList.add('show');
  setTimeout(() => $('transferAmountInput').focus(), 100);
}
function closeTransferDialog() {
  $('transferDialogOverlay').classList.remove('show');
}
function confirmTransfer() {
  const val = $('transferAmountInput').value.trim();
  if (!val || isNaN(Number(val)) || Number(val) === 0) return;
  const n = Number(val);
  const tag = `[转账：${n}元]`;
  const input = $('input');
  const cur = input.value;
  input.value = cur ? cur + ' ' + tag : tag;
  autoResize(input);
  _updateSendBtnState();
  closeTransferDialog();
  input.focus();
}

// ── 钱包面板 ──
async function openWalletPanel() {
  $('walletPanelOverlay').classList.add('show');
  closeSidebar();
  try {
    const [balRes, txRes] = await Promise.all([
      api('GET', '/api/wallet/balance'),
      api('GET', '/api/wallet/transactions?limit=50')
    ]);
    $('walletBalanceValue').textContent = `¥${(balRes.balance || 0).toFixed(2)}`;
    const list = $('walletTxList');
    if (!txRes || txRes.length === 0) {
      list.innerHTML = '<div class="wallet-tx-empty">暂无转账记录</div>';
    } else {
      list.innerHTML = txRes.map(tx => {
        const isAi = tx.record_type === 'wallet_ai';
        const d = new Date(tx.created_at * 1000);
        const timeStr = `${d.getMonth()+1}/${d.getDate()} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
        const sign = tx.amount >= 0 ? '+' : '';
        const cls = tx.amount >= 0 ? 'positive' : 'negative';
        const aName = (worldBook && worldBook.ai_name) || 'AI';
        const uName = (worldBook && worldBook.user_name) || '用户';
        let desc = tx.description || (isAi ? `${aName}转账` : `${uName}转账`);
        desc = desc.replace(/AI转账给用户/g, `${aName}转账给${uName}`).replace(/用户转账/g, `${uName}转账`);
        return `<div class="wallet-tx-item"><div><div class="wallet-tx-desc">${escHtml(desc)}</div><div class="wallet-tx-time">${timeStr}</div></div><div class="wallet-tx-amount ${cls}">${sign}${tx.amount.toFixed(2)}</div></div>`;
      }).join('');
    }
  } catch(e) {
    $('walletTxList').innerHTML = '<div class="wallet-tx-empty">加载失败</div>';
  }
}
function closeWalletPanel() {
  $('walletPanelOverlay').classList.remove('show');
}

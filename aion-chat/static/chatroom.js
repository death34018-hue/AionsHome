/* ── Aion 聊天室前端逻辑 ── */

const API = '/api/chatroom';
const CHATROOM_THEME_KEY = 'aion_chat_theme';
let currentRoom = null;
let rooms = [];
let isSending = false;
let isAiChatting = false;
let chatroomModel = '';
let chatroomConnorModel = 'Codex';
let chatroomReplyOrder = 'random';
let isReplyOnce = false;
let chatroomModels = [];
let pendingAttachments = [];  // [{url, type, name}]
let crMessagesById = {};
let memSourceMemId = null;
let memSourceMessages = [];
let chatroomMemoryCache = [];
let chatroomMemoryKindFilter = 'all';
let chatroomMemoryKindMenuId = null;
let chatroomDailyCompressReview = null;
let _chatroomCompressionRequestBusy = false;
let crMsgDebugData = {};
let crSystemLogs = [];
let crSysLogHasUnreadError = false;
const CR_AMBIENT_LOCAL_ARMED_KEY = 'aion_chatroom_ambient_local_armed';
const CR_TTS_VOICES_CACHE_KEY = 'chatroom_tts_voices_v1';
const CR_SETTINGS_SNAPSHOT_PREFIX = 'chatroom_settings_snapshot_v2_';
const CR_SYNC_SEQ_KEY = 'aion_sync_seq_v1:chatroom';
let crSettingsDirtyAt = 0;
let crSettingsLoadingRoomId = null;
let crSyncReplayPromise = null;

window.addEventListener('aion-client-update-ready', () => {
  if (window.__aionClientUpdateScheduled) return;
  window.__aionClientUpdateScheduled = true;
  const reloadWhenIdle = () => {
    const active = document.activeElement;
    const editing = active && active.matches?.('input,textarea,select,[contenteditable="true"]');
    const settingsDirty = document.getElementById('settingsOverlay')?.classList.contains('active')
      && !!crSettingsDirtyAt;
    if (isSending || isAiChatting || editing || settingsDirty || _ttsEngine?.playing) {
      setTimeout(reloadWhenIdle, 1500);
      return;
    }
    location.reload();
  };
  setTimeout(reloadWhenIdle, 800);
});

document.addEventListener('click', () => {
  if (!chatroomMemoryKindMenuId) return;
  chatroomMemoryKindMenuId = null;
  renderChatroomMemories();
});

function crMakeAmbientClientId() {
  try {
    if (crypto?.randomUUID) return `ambient_${crypto.randomUUID()}`;
  } catch(e) {}
  return `ambient_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
}
const crAmbientClientId = crMakeAmbientClientId();
let crAmbientVoiceEnabled = false;
let crAmbientLocalArmed = localStorage.getItem(CR_AMBIENT_LOCAL_ARMED_KEY) === '1';
let crAmbientOwned = false;
let crAmbientOwner = null;
let crAmbientClaimInFlight = false;
let crAmbientHeartbeatTimer = null;
let crAmbientWakeWord = '现在立刻唤醒';
let crAmbientStopWord = '结束立刻唤醒';
let crAmbientMinChars = 500;
let crAmbientIntervalSec = 120;
let crAmbientCooldownSec = 180;
let crAmbientStream = null;
let crAmbientAudioCtx = null;
let crAmbientAnalyser = null;
let crAmbientUseNative = false;
let crAmbientNativeChunks = [];
let crAmbientNativeRecording = false;
let crAmbientNativeSegmentChunks = [];
let crAmbientVadTimer = null;
let crAmbientMediaRecorder = null;
let crAmbientChunks = [];
let crAmbientRecording = false;
let crAmbientSegmentStartedAt = 0;
let crAmbientSilenceStartedAt = 0;
let crAmbientSpeechFrames = 0;
let crAmbientNoiseFloor = 0.008;
let crAmbientTranscriptBuffer = [];
let crAmbientBufferExpanded = false;
let crAmbientLastCheckAt = 0;
let crAmbientWakePendingUntil = 0;
let crAmbientEvaluating = false;
let crAmbientDiscardSegment = false;
let crAmbientPausedForTts = false;
let crAmbientTtsResumeTimer = null;
let crAmbientLastUserToggleAt = 0;
let crAmbientResumeRetryTimer = null;

function applyChatroomTheme(theme) {
  const next = theme === 'light' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  document.body.dataset.theme = next;
  document.documentElement.style.backgroundColor = next === 'dark' ? '#03061c' : '#eef3ff';
  localStorage.setItem(CHATROOM_THEME_KEY, next);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', next === 'dark' ? '#050923' : '#eef3ff');
  if (window.AionStatusBar) window.AionStatusBar.setBarStyle(next);
}

window.applyChatroomTheme = applyChatroomTheme;
applyChatroomTheme(localStorage.getItem(CHATROOM_THEME_KEY) || 'dark');

window.addEventListener('storage', (e) => {
  if (e.key === CHATROOM_THEME_KEY) applyChatroomTheme(e.newValue || 'dark');
});

// ── 密语模式 ──
let crWhisperMode = false;
const crHandledToyEvents = new Set();

const AVATARS = {
  user: '/public/UserIcon.png',
  aion: '/public/gropicon1.png',
  connor: '/public/codexicon.png',
};

let crUserName = '我';
let crAiName = 'AI';
let crConnorName = '第二AI';

function crName(sender) {
  return { user: crUserName || '我', aion: crAiName || 'AI', connor: crConnorName || '第二AI' }[sender] || sender;
}

function applyChatroomNames(cfg = {}) {
  crAiName = cfg.ai_name || crAiName || 'AI';
  crUserName = cfg.user_name || crUserName || '我';
  crConnorName = cfg.connor_name || crConnorName || '第二AI';

  const aionVoice = document.getElementById('setTtsAionVoice');
  if (aionVoice?.previousElementSibling) aionVoice.previousElementSibling.textContent = `${crAiName} 音色`;
  const connorVoice = document.getElementById('setTtsConnorVoice');
  if (connorVoice?.previousElementSibling) connorVoice.previousElementSibling.textContent = `${crConnorName} 音色`;
  const walletTitle = document.querySelector('.wallet-panel-header span');
  if (walletTitle) walletTitle.textContent = `💰 ${crConnorName} 的钱包`;
  const optAion = document.getElementById('optAion');
  if (optAion) optAion.textContent = `${crAiName} 优先`;
  const optConnor = document.getElementById('optConnor');
  if (optConnor) optConnor.textContent = `${crConnorName} 优先`;
  const replyAionBtn = document.getElementById('replyAionBtn');
  if (replyAionBtn) replyAionBtn.textContent = `${crAiName} 说`;
  const replyConnorBtn = document.getElementById('replyConnorBtn');
  if (replyConnorBtn) replyConnorBtn.textContent = `${crConnorName} 说`;
  const aionModelLabel = document.querySelector('#fieldAionModel label');
  if (aionModelLabel) aionModelLabel.textContent = `${crAiName} 模型线路`;
  const connorModelLabel = document.querySelector('#fieldConnorModel label');
  if (connorModelLabel) connorModelLabel.textContent = `${crConnorName} 模型线路`;
  updateHeaderActions();
}

// ── 音效 ──
const sndSend = new Audio('/public/发送消息.mp3');
const sndRecv = new Audio('/public/收到消息.mp3');
function playSend() { sndSend.currentTime = 0; sndSend.play().catch(() => {}); }
function playRecv() { sndRecv.currentTime = 0; sndRecv.play().catch(() => {}); }

// ── TTS 语音合成（统一从服务端加载配置，init 时拉取）──
let crTtsEnabled = false;
let crTtsAionVoice = '';
let crTtsConnorVoice = '';
const crSeenTTSChunks = new Set();
const crSeenTTSDone = new Set();
const crTtsMsgSenders = {};
let crWs = null;
let crTtsAcceptAfter = Date.now() / 1000;
let crTtsPlaybackActiveAt = Date.now() / 1000;
const crSuppressedTTSMsgIds = new Set();
const crIsEmbedded = (() => {
  try { return window.parent && window.parent !== window; }
  catch(e) { return false; }
})();

function crGetParentAudio(key) {
  if (!crIsEmbedded) return null;
  try {
    const parentWin = window.parent;
    if (!parentWin || parentWin === window || !parentWin.document?.body) return null;
    const storeKey = `_cr_${key}Audio`;
    if (!parentWin[storeKey]) {
      const audio = parentWin.document.createElement('audio');
      audio.style.display = 'none';
      parentWin.document.body.appendChild(audio);
      parentWin[storeKey] = audio;
    }
    return parentWin[storeKey];
  } catch(e) {
    return null;
  }
}

function crCreateHiddenAudio() {
  try {
    const audio = document.createElement('audio');
    audio.preload = 'auto';
    audio.setAttribute('playsinline', '');
    audio.style.display = 'none';
    document.body.appendChild(audio);
    return audio;
  } catch(e) {
    return new Audio();
  }
}

function crReportAudioPlayFailure(label, err, audio) {
  const mediaErr = audio?.error;
  const detail = {
    name: err?.name || '',
    message: err?.message || '',
    mediaCode: mediaErr?.code || 0,
    mediaMessage: mediaErr?.message || '',
    networkState: audio?.networkState,
    readyState: audio?.readyState,
    src: audio?.currentSrc || audio?.src || '',
  };
  console.warn(`[chatroom TTS] ${label} play failed`, detail);
  const reason = detail.name || detail.mediaMessage || '浏览器没有开始播放';
  try { toast(`语音播放失败：${reason}`); } catch(e) {}
}

function crRememberTTSMsgSender(msgId, sender) {
  if (msgId && sender) crTtsMsgSenders[msgId] = sender;
}

function crSenderForTTSMsg(msgId) {
  if (crTtsMsgSenders[msgId]) return crTtsMsgSenders[msgId];
  const stored = crMessagesById[msgId]?.sender;
  if (stored) return stored;
  if (String(msgId || "").endsWith("_a")) return "aion";
  if (String(msgId || "").endsWith("_c")) return "connor";
  return currentRoom?.type === "connor_1v1" ? "connor" : "aion";
}

function crNotifyVoiceCallTTSStart(msgId, seq, item) {
  if (!window.VoiceCall || !window.VoiceCall.handleTTSChunkStart) return;
  const sender = crSenderForTTSMsg(msgId);
  window.VoiceCall.handleTTSChunkStart({
    surface: "chatroom",
    msgId,
    seq,
    url: item?.url || item,
    text: item?.text || "",
    sender,
    speakerName: crName(sender)
  });
}

function crNotifyVoiceCallTTSChunkEnd(msgId) {
  if (window.VoiceCall && window.VoiceCall.handleTTSChunkEnd) {
    window.VoiceCall.handleTTSChunkEnd({ surface: "chatroom", msgId });
  }
}

function crNotifyVoiceCallTTSEnd() {
  if (window.VoiceCall && window.VoiceCall.handleTTSEnd) {
    window.VoiceCall.handleTTSEnd({ surface: "chatroom" });
  }
}

// TTS 播放引擎：Audio 使用本地对象（可靠播放），离开页面时移交给 parent（尽力续播）
const _ttsEngine = (function() {
  // 在 parent 上预建一个 handoff audio，用于离开页面后续播当前片段
  const _handoffAudio = crGetParentAudio('ttsHandoff');

  const audio = crCreateHiddenAudio();
  let _cbId = 0; // 回调去重 ID，防止 onended/onerror/catch 多次触发
  let _resumeTimer = null;
  let _stopRequested = false;

  const clearResumeTimer = () => {
    if (_resumeTimer) {
      clearTimeout(_resumeTimer);
      _resumeTimer = null;
    }
  };

  const scheduleResume = () => {
    if (_stopRequested || !eng.playing || !eng.audio.src || eng.audio.ended || !eng.audio.paused) return;
    if (_resumeTimer) return;
    _resumeTimer = setTimeout(() => {
      _resumeTimer = null;
      if (_stopRequested || !eng.playing || !eng.audio.src || eng.audio.ended || !eng.audio.paused) return;
      eng.audio.play().catch(() => {
        scheduleResume();
      });
    }, 1500);
  };

  const eng = {
    audio: audio,
    playing: false,
    chunkQueues: {},
    playOrder: [],
    _next() {
      while (eng.playOrder.length > 0) {
        const msgId = eng.playOrder[0];
        const q = eng.chunkQueues[msgId];
        if (!q) { eng.playOrder.shift(); continue; }
        let item = q.chunks[q.nextPlay];
        let url = item && typeof item === "object" ? item.url : item;
        if (url === undefined) {
          if (q.finished) {
            const maxSeq = Object.keys(q.chunks).length > 0 ? Math.max(...Object.keys(q.chunks).map(Number)) : -1;
            if (q.nextPlay > maxSeq) { eng.playOrder.shift(); delete eng.chunkQueues[msgId]; continue; }
            while (q.nextPlay <= maxSeq && q.chunks[q.nextPlay] === undefined) q.nextPlay++;
            if (q.nextPlay > maxSeq) { eng.playOrder.shift(); delete eng.chunkQueues[msgId]; continue; }
            item = q.chunks[q.nextPlay];
            url = item && typeof item === "object" ? item.url : item;
          }
          if (url === undefined) {
            eng.playing = false;
            crAmbientResumeAfterTts();
            return;
          }
        }
        eng.playing = true;
	        _stopRequested = false;
	        clearResumeTimer();
	        crAmbientPauseForTts();
	        const myId = ++_cbId;
	        const seq = q.nextPlay;
	        let startNotified = false;
	        const notifyStart = () => {
	          if (startNotified) return;
	          startNotified = true;
	          crNotifyVoiceCallTTSStart(msgId, seq, item);
	        };
	        const advance = () => {
	          if (myId !== _cbId) return; // 过时回调，忽略
	          clearResumeTimer();
	          _cbId++;
	          eng.playing = false;
	          q.nextPlay++;
	          crNotifyVoiceCallTTSChunkEnd(msgId);
	          eng._next();
	        };
	        eng.audio.src = url;
	        eng.audio.onended = advance;
	        eng.audio.onerror = advance;
	        eng.audio.onplaying = () => {
	          clearResumeTimer();
	          notifyStart();
	        };
        eng.audio.onpause = () => {
          if (myId !== _cbId || eng.audio.ended) return;
          scheduleResume();
        };
	        eng.audio.play().then(notifyStart).catch((err) => {
          // 外部 App 抢占音频焦点时，play() 可能会短暂失败；保留当前分片，等待焦点恢复。
          crReportAudioPlayFailure('live chunk', err, eng.audio);
          scheduleResume();
        });
        return;
	      }
	      eng.playing = false;
	      crAmbientResumeAfterTts();
	      crNotifyVoiceCallTTSEnd();
	    },
	    enqueue(msgId, seq, url, text = "") {
	      if (!eng.chunkQueues[msgId]) {
	        eng.chunkQueues[msgId] = { nextPlay: 0, chunks: {}, finished: false };
	        eng.playOrder.push(msgId);
	      }
	      eng.chunkQueues[msgId].chunks[seq] = { url, text: text || "" };
	      if (!eng.playing) eng._next();
	    },
    finish(msgId) {
      const q = eng.chunkQueues[msgId];
      if (!q) return;
      q.finished = true;
      while (eng.playOrder.length > 0) {
        const id = eng.playOrder[0];
        const qq = eng.chunkQueues[id];
        if (!qq || !qq.finished) break;
        const maxSeq = Object.keys(qq.chunks).length > 0 ? Math.max(...Object.keys(qq.chunks).map(Number)) : -1;
        if (qq.nextPlay > maxSeq) { eng.playOrder.shift(); delete eng.chunkQueues[id]; } else break;
      }
      if (!eng.playing) eng._next();
    },
    stop() {
      _cbId++;
      _stopRequested = true;
      clearResumeTimer();
	      eng.audio.pause(); eng.audio.src = '';
	      eng.chunkQueues = {}; eng.playOrder = []; eng.playing = false;
	      crAmbientResumeAfterTts(500);
	      crNotifyVoiceCallTTSEnd();
	    }
  };

  // 页面卸载时，把当前正在播放的音频移交到 parent audio 续播
  if (_handoffAudio && !crIsEmbedded) {
    window.addEventListener('pagehide', () => {
      if (eng.playing && eng.audio.src && !eng.audio.paused) {
        try {
          _handoffAudio.src = eng.audio.src;
          _handoffAudio.currentTime = eng.audio.currentTime;
          _handoffAudio.play().catch(() => {});
        } catch(e) {}
      }
    });
  }

  return eng;
})();
let crTtsAudio = _ttsEngine.audio;

function crTtsPlaybackAllowed() {
  return true;
}

function crCurrentTTSVoice() {
  return crTtsAionVoice || crTtsConnorVoice || '';
}

function crSendTTSState() {
  if (!crWs || crWs.readyState !== WebSocket.OPEN) return;
  crWs.send(JSON.stringify({
    type: 'tts_state',
    enabled: crTtsEnabled,
    voice: crCurrentTTSVoice(),
    can_play: crTtsPlaybackAllowed(),
    active_at: crTtsPlaybackActiveAt,
    client_id: crAmbientClientId
  }));
}

function crRefreshTTSPlaybackState() {
  crSendTTSState();
}

let crTtsPlaybackStateLastSent = 0;
function crBumpTTSPlaybackState() {
  const now = Date.now();
  if (now - crTtsPlaybackStateLastSent < 1000) return;
  crTtsPlaybackStateLastSent = now;
  crTtsPlaybackActiveAt = now / 1000;
  crSendTTSState();
}

function crSuppressTTSMsg(msgId) {
  if (msgId) crSuppressedTTSMsgIds.add(msgId);
}

function crShouldAcceptTTSMsg(msgId, createdAt, targetClientId) {
  if (!msgId || crSuppressedTTSMsgIds.has(msgId)) return false;
  if (targetClientId && targetClientId !== crAmbientClientId) return false;
  const ts = Number(createdAt || 0);
  if (ts && ts < crTtsAcceptAfter) {
    crSuppressTTSMsg(msgId);
    return false;
  }
  return true;
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') crRefreshTTSPlaybackState();
  else crBumpTTSPlaybackState();
});
window.addEventListener('pagehide', crRefreshTTSPlaybackState);
window.addEventListener('pageshow', crBumpTTSPlaybackState);
document.addEventListener('freeze', crRefreshTTSPlaybackState);
window.addEventListener('focus', crBumpTTSPlaybackState);
document.addEventListener('pointerdown', crBumpTTSPlaybackState, { passive: true });
document.addEventListener('keydown', crBumpTTSPlaybackState);

// ── 音乐卡片 ──
let crMusicCards = {}; // { msgId: [{ id, name, artist, cover, audio_url }] }

// ── 密语胶囊 ──
function crToyLabel(cmd) {
  const c = String(cmd || '').trim().toUpperCase();
  if (c === 'STOP' || c === '0') return '❤️ 停止';
  const n = parseInt(c);
  return (n >= 1 && n <= 9) ? `❤️ ${CR_TOY_PNAMES[n - 1]}` : `❤️ ${cmd}`;
}

function crToyCommandsFromAttachments(atts) {
  if (!Array.isArray(atts)) return [];
  return atts
    .filter(item => item && typeof item === 'object' && item.type === 'toy')
    .flatMap(item => Array.isArray(item.commands) ? item.commands : (item.command ? [item.command] : []));
}

function renderToyAttachments(atts) {
  const commands = crToyCommandsFromAttachments(atts);
  if (!commands.length) return '';
  return commands.map(cmd => `<div class="toy-capsule" data-toy-command="${esc(String(cmd))}">${esc(crToyLabel(cmd))}</div>`).join('');
}

function crShowToyCapsule(msgId, commands) {
  if (!msgId || !commands || !commands.length) return;
  const row = document.querySelector(`[data-msg-id="${msgId}"]`) || document.getElementById(`streaming-${msgId}`);
  if (!row) return;
  const msgContent = row.querySelector('.msg-content');
  if (!msgContent) return;
  commands.forEach(cmd => {
    const c = String(cmd || '').trim().toUpperCase();
    if (!c) return;
    if (msgContent.querySelector(`.toy-capsule[data-toy-command="${c}"]`)) return;
    const pill = document.createElement('div');
    pill.className = 'toy-capsule';
    pill.dataset.toyCommand = c;
    pill.textContent = crToyLabel(cmd);
    msgContent.appendChild(pill);
  });
  scrollToBottom();
}

function crHandleToyCommand(data) {
  if (!data || !data.commands || !data.commands.length) return;
  const msgId = data.msg_id || '';
  const commands = data.commands.map(c => String(c || '').trim().toUpperCase()).filter(Boolean);
  if (!commands.length) return;
  const key = `${msgId}:${commands.join('|')}`;
  const alreadyHandled = crHandledToyEvents.has(key);
  if (!alreadyHandled) {
    crHandledToyEvents.add(key);
    try {
      if (window.opener && window.opener.toyExecCmd) {
        commands.forEach(c => window.opener.toyExecCmd(c));
      } else if (window.parent && window.parent !== window && window.parent.toyExecCmd) {
        commands.forEach(c => window.parent.toyExecCmd(c));
      }
    } catch(e) {}
    if (typeof toyExecCmd === 'function') commands.forEach(c => toyExecCmd(c));
  }
  crShowToyCapsule(msgId, commands);
}

function crRenderMusicCards(msgId) {
  const cards = crMusicCards[msgId];
  if (!cards || !cards.length) return;
  const row = document.querySelector(`[data-msg-id="${msgId}"]`) || document.getElementById(`streaming-${msgId}`);
  if (!row) return;
  row.querySelectorAll('.music-capsule').forEach(e => e.style.display = 'none');
  row.querySelectorAll('.music-cards-container').forEach(e => e.remove());
  const container = document.createElement('div');
  container.className = 'music-cards-container';
  cards.forEach(song => { container.innerHTML += crBuildMusicCardHtml(song); });
  const msgContent = row.querySelector('.msg-content');
  if (msgContent) msgContent.appendChild(container);
}

function crBuildMusicCardHtml(song) {
  const cover = song.cover ? esc(song.cover) : '';
  const coverImg = cover
    ? `<img class="music-cover" src="${cover}" alt="">`
    : `<div class="music-cover" style="display:flex;align-items:center;justify-content:center;font-size:20px;color:var(--text3)">🎵</div>`;
  const name = esc(song.name || '未知歌曲');
  const artist = esc(song.artist || '未知歌手');
  const songId = song.id;
  const onlineBtn = `<button class="music-btn secondary" onclick="crPlayMusicOnline(${songId})">▶ 在线播放</button>`;
  return `
    <div class="music-card">
      ${coverImg}
      <div class="music-info">
        <div class="music-name">${name}</div>
        <div class="music-artist">${artist}</div>
        <div class="music-btns">
          <button class="music-btn primary" onclick="crOpenInNetease(${songId})">🎶 网易云</button>
          ${onlineBtn}
        </div>
      </div>
    </div>`;
}

function crOpenInNetease(songId) {
  window.open('https://music.163.com/song?id=' + songId, '_blank');
}

function crPlayMusicOnline(songId) {
  let wrap = document.getElementById('crGlobalMusicWrap');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.id = 'crGlobalMusicWrap';
    wrap.style.cssText = 'position:fixed;top:calc(max(34px, env(safe-area-inset-top, 0px)) + 48px);left:0;right:0;z-index:999;display:none;align-items:center;gap:8px;background:var(--surface,#1e1e1e);padding:0 12px;height:36px;box-shadow:0 2px 8px rgba(0,0,0,0.25);border-bottom:1px solid var(--border,#333);';

    const playBtn = document.createElement('button');
    playBtn.id = 'crMusicPlayBtn';
    playBtn.textContent = '⏸';
    playBtn.style.cssText = 'background:none;border:none;font-size:16px;cursor:pointer;color:var(--text,#eee);padding:0 4px;line-height:1;flex-shrink:0;';

    const bar = document.createElement('input');
    bar.id = 'crMusicBar';
    bar.type = 'range'; bar.min = 0; bar.max = 1000; bar.value = 0;
    bar.style.cssText = 'flex:1;height:4px;accent-color:#e53935;cursor:pointer;';

    const volWrap = document.createElement('span');
    volWrap.style.cssText = 'display:flex;align-items:center;gap:2px;flex-shrink:0;';
    const volIcon = document.createElement('span');
    volIcon.textContent = '🔉';
    volIcon.style.cssText = 'font-size:13px;cursor:pointer;user-select:none;';
    const volBar = document.createElement('input');
    volBar.id = 'crMusicVol';
    volBar.type = 'range'; volBar.min = 0; volBar.max = 100;
    volBar.value = localStorage.getItem('musicVolume') ?? 50;
    volBar.style.cssText = 'width:52px;height:4px;accent-color:#ff9800;cursor:pointer;';

    const audio = document.createElement('audio');
    audio.id = 'crMusicAudio';
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
    closeBtn.onclick = () => { audio.pause(); audio.currentTime = 0; audio.src = ''; wrap.style.display = 'none'; bar.value = 0; };

    wrap.appendChild(playBtn);
    wrap.appendChild(bar);
    wrap.appendChild(volWrap);
    wrap.appendChild(audio);
    wrap.appendChild(closeBtn);
    document.body.appendChild(wrap);
  }
  const audio = document.getElementById('crMusicAudio');
  audio.src = '/api/music/stream/' + songId;
  wrap.style.display = 'flex';
  document.getElementById('crMusicBar').value = 0;
  document.getElementById('crMusicPlayBtn').textContent = '⏸';
  audio.play().catch(() => {});
}

function crEnqueueTTSChunk(msgId, seq, url, createdAt, targetClientId, text = "") {
  const voiceCallActive = !!(window.VoiceCall && window.VoiceCall.isActive && window.VoiceCall.isActive());
  if (!crTtsEnabled && !voiceCallActive) return;
  const key = `${msgId}:${seq}`;
  if (crSeenTTSChunks.has(key)) return;
  crSeenTTSChunks.add(key);
  if (!crShouldAcceptTTSMsg(msgId, createdAt, targetClientId)) return;
  _ttsEngine.enqueue(msgId, seq, url, text);
}

async function crPlayNextTTSChunk() {
  if (!_ttsEngine.playing) _ttsEngine._next();
}

function crFinishTTSForMsg(msgId, createdAt, targetClientId) {
  if (targetClientId && targetClientId !== crAmbientClientId) return;
  const ts = Number(createdAt || 0);
  if (crSuppressedTTSMsgIds.has(msgId) || (ts && ts < crTtsAcceptAfter)) {
    crSuppressedTTSMsgIds.delete(msgId);
    return;
  }
  if (crSeenTTSDone.has(msgId)) return;
  crSeenTTSDone.add(msgId);
  _ttsEngine.finish(msgId);
}

window.ChatroomVoiceCallAdapter = {
  getDefaultSpeakerName() {
    return currentRoom?.type === "connor_1v1" ? crName("connor") : crName("aion");
  },
  getSpeakerName(sender) {
    return crName(sender || (currentRoom?.type === "connor_1v1" ? "connor" : "aion"));
  },
  speakerForMessage(msgId) {
    return crSenderForTTSMsg(msgId);
  },
  async sendText(text) {
    const content = String(text || "").trim();
    if (!content) return;
    if (!currentRoom) throw new Error("请先选择聊天室");
    let waited = 0;
    while ((isSending || isAiChatting || isReplyOnce) && waited < 10000) {
      await new Promise(resolve => setTimeout(resolve, 200));
      waited += 200;
    }
    if (isSending || isAiChatting || isReplyOnce) throw new Error("上一轮回复仍在进行");

    isSending = true;
    sendBtn.disabled = true;
    playSend();
    const localRow = appendMessage({ sender: "user", content, created_at: Date.now() / 1000, attachments: [] });
    if (localRow) localRow.dataset.localEcho = "1";
    try {
      const resp = await fetch(`${API}/rooms/${currentRoom.id}/send`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content,
          model: chatroomModel,
          connor_model: chatroomConnorModel,
          attachments: [],
          tts_enabled: true,
          tts_aion_voice: crTtsAionVoice,
          tts_connor_voice: crTtsConnorVoice,
          whisper_mode: crWhisperMode
        }),
      });
      await consumeChatroomSSE(resp);
    } finally {
      isSending = false;
      sendBtn.disabled = false;
      endStreamingBubble();
      crRefocusComposerAfterSend();
    }
  }
};

function crStopTTS() {
  _ttsEngine.stop();
}

// ── TTS 重听 ──
let crReplayAudio = crCreateHiddenAudio();
let crReplayChunks = [];
let crReplayIdx = 0;
let crReplayToken = 0;
let crReplayDiscoverPromise = null;

async function crReplayTTS(msgId, triggerBtn = null) {
  const btn = triggerBtn || document.querySelector(`[data-msg-id="${msgId}"] .tts-replay-btn`);
  // 正在播放则停止
  if (btn && btn.classList.contains('playing')) {
    crReplayAudio.pause(); crReplayAudio.src = ''; crReplayChunks = [];
    btn.classList.remove('playing');
    crReplayToken++;
    crReplayDiscoverPromise = null;
    crAmbientResumeAfterTts(500);
    return;
  }
  crReplayAudio.pause(); crReplayChunks = [];
  document.querySelectorAll('.tts-replay-btn.playing').forEach(b => b.classList.remove('playing'));

  const token = ++crReplayToken;
  crReplayChunks = [`/api/tts/audio/${msgId}_s0`];
  crReplayIdx = 0;
  if (btn) btn.classList.add('playing');
  crAmbientPauseForTts();

  // 先在用户点击同步链路里立刻播放 s0，避免多次 HEAD 后丢失浏览器播放许可。
  _crPlayReplayChunk(btn, token);

  crReplayDiscoverPromise = crDiscoverReplayChunks(msgId).then(chunks => {
    if (token === crReplayToken && chunks.length) crReplayChunks = chunks;
    return chunks;
  });
}

async function crDiscoverReplayChunks(msgId) {
  const chunks = [];
  for (let i = 0; i < 120; i++) {
    const resp = await fetch(`/api/tts/audio/${msgId}_s${i}`, { method: 'HEAD' });
    if (!resp.ok) break;
    chunks.push(`/api/tts/audio/${msgId}_s${i}`);
  }
  return chunks;
}

function crFinishReplay(btn) {
  if (btn) btn.classList.remove('playing');
  crReplayDiscoverPromise = null;
  crAmbientResumeAfterTts();
}

function _crPlayReplayChunk(btn, token = crReplayToken) {
  if (token !== crReplayToken) return;
  if (crReplayIdx >= crReplayChunks.length) {
    if (crReplayDiscoverPromise) {
      const pending = crReplayDiscoverPromise;
      crReplayDiscoverPromise = null;
      pending.then(() => {
        if (token !== crReplayToken) return;
        if (crReplayIdx < crReplayChunks.length) _crPlayReplayChunk(btn, token);
        else crFinishReplay(btn);
      }).catch(() => crFinishReplay(btn));
      return;
    }
    crFinishReplay(btn);
    return;
  }
  crReplayAudio.src = crReplayChunks[crReplayIdx];
  crReplayAudio.onended = () => { crReplayIdx++; _crPlayReplayChunk(btn, token); };
  crReplayAudio.onerror = () => { crReplayIdx++; _crPlayReplayChunk(btn, token); };
  crReplayAudio.play().catch((err) => {
    crReportAudioPlayFailure('replay chunk', err, crReplayAudio);
    if (btn) btn.classList.remove('playing');
    crAmbientResumeAfterTts(500);
  });
}

function onTtsToggleChange() {
  crTtsEnabled = document.getElementById('setTtsEnabled').checked;
  crTtsAcceptAfter = Date.now() / 1000;
  crTtsPlaybackActiveAt = crTtsAcceptAfter;
  if (!crTtsEnabled) crStopTTS();
  // 持久化到服务端，所有窗口共享
  api('/config', { method: 'PUT', body: JSON.stringify({ tts_enabled: crTtsEnabled }) }).catch(() => {});
  crSendTTSState();
}

function onWhisperToggleChange() {
  crWhisperMode = !!document.getElementById('setWhisperMode')?.checked;
}

function crApplyTTSVoices(data) {
  const aionSel = document.getElementById('setTtsAionVoice');
  const connorSel = document.getElementById('setTtsConnorVoice');
  if (!aionSel || !connorSel) return;
  if (data?.voices && data.voices.length > 0) {
    const opts = data.voices.map(v => ({ uri: v.uri, name: v.customName || v.uri || 'Unknown' }));
    aionSel.innerHTML = opts.map(o =>
      `<option value="${o.uri}" ${o.uri === crTtsAionVoice ? 'selected' : ''}>${o.name}</option>`
    ).join('');
    connorSel.innerHTML = opts.map(o =>
      `<option value="${o.uri}" ${o.uri === crTtsConnorVoice ? 'selected' : ''}>${o.name}</option>`
    ).join('');
  } else if (!aionSel.options.length || !connorSel.options.length) {
    aionSel.innerHTML = '<option value="">无可用音色</option>';
    connorSel.innerHTML = '<option value="">无可用音色</option>';
  }
}

async function crLoadTTSVoices() {
  try {
    const cached = JSON.parse(localStorage.getItem(CR_TTS_VOICES_CACHE_KEY) || 'null');
    if (cached?.data) crApplyTTSVoices(cached.data);
  } catch(e) {}
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    const resp = await fetch('/api/tts/voices', { signal: controller.signal });
    clearTimeout(timer);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    crApplyTTSVoices(data);
    localStorage.setItem(CR_TTS_VOICES_CACHE_KEY, JSON.stringify({ savedAt: Date.now(), data }));
  } catch(e) {
    console.error('加载TTS音色失败:', e);
  }
}

// ── DOM ──
const roomListEl = document.getElementById('roomList');
const messagesEl = document.getElementById('messages');

// ── 消息分页状态 ──
let oldestMsgTs = null;   // 当前已加载的最早消息时间戳
let noMoreMessages = false; // 是否已加载全部历史
let loadingOlder = false; // 防重复加载锁
const composer = document.getElementById('composer');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('sendBtn');
const roomTitleEl = document.getElementById('roomTitle');
const menuBtn = document.getElementById('menuBtn');
const sidebar = document.getElementById('sidebar');
const backdrop = document.getElementById('sidebarBackdrop');
const aiChatBtn = document.getElementById('aiChatBtn');
const replyAionBtn = document.getElementById('replyAionBtn');
const replyConnorBtn = document.getElementById('replyConnorBtn');
const toastEl = document.getElementById('toast');
const chatSearchPanel = document.getElementById('chatSearchPanel');
const chatSearchForm = document.getElementById('chatSearchForm');
const chatSearchInput = document.getElementById('chatSearchInput');
const chatSearchMeta = document.getElementById('chatSearchMeta');
const chatSearchResults = document.getElementById('chatSearchResults');
let chatSearchKeyword = '';

function crShouldAutoFocusComposer() {
  const isCoarsePointer = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
  const hasTouch = (navigator.maxTouchPoints || 0) > 0;
  const isNarrow = window.innerWidth <= 768;
  return !(isCoarsePointer || hasTouch || isNarrow);
}

function crRefocusComposerAfterSend() {
  if (!crShouldAutoFocusComposer()) return;
  inputEl?.focus();
}

// ══════════════════════════════════════════════════
//  工具函数
// ══════════════════════════════════════════════════

function toast(msg, ms = 2000) {
  toastEl.textContent = msg;
  toastEl.classList.add('show');
  setTimeout(() => toastEl.classList.remove('show'), ms);
}

function timeStr(ts) {
  const d = new Date(ts * 1000);
  const now = new Date();
  const diffMs = now - d;
  const time = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  if (diffMs > 12 * 60 * 60 * 1000) {
    return d.getFullYear() + '-' + (d.getMonth() + 1) + '-' + d.getDate() + ' ' + time;
  }
  return time;
}

function isNearBottom() {
  return messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 100;
}

function scrollToBottom(force = false) {
  if (force || isNearBottom()) {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }
}

// 滚动到顶部时自动加载更早的消息
messagesEl.addEventListener('scroll', () => {
  if (messagesEl.scrollTop < 80) {
    loadOlderMessages();
  }
});

function resizeInput() {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 120) + 'px';
}

let inputResizeFrame = null;

function scheduleInputResize() {
  if (inputResizeFrame !== null) return;
  inputResizeFrame = requestAnimationFrame(() => {
    inputResizeFrame = null;
    resizeInput();
  });
}

function cancelPendingInputResize() {
  if (inputResizeFrame === null) return;
  cancelAnimationFrame(inputResizeFrame);
  inputResizeFrame = null;
}

window.addEventListener('pagehide', cancelPendingInputResize);

// ══════════════════════════════════════════════════
//  API 调用
// ══════════════════════════════════════════════════

async function api(path, opts = {}) {
  const timeoutMs = Number(opts.timeoutMs || 15000);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const fetchOpts = { ...opts };
  delete fetchOpts.timeoutMs;
  try {
    const resp = await fetch(API + path, {
      ...fetchOpts,
      headers: { 'Content-Type': 'application/json', ...(fetchOpts.headers || {}) },
      signal: controller.signal,
    });
    const text = await resp.text();
    let payload = null;
    if (text) {
      try { payload = JSON.parse(text); }
      catch(e) { payload = { detail: text }; }
    }
    if (!resp.ok) {
      const error = new Error(payload?.detail || payload?.error || `请求失败 (${resp.status})`);
      error.status = resp.status;
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

function crAmbientSetLocalArmed(armed) {
  crAmbientLocalArmed = !!armed;
  localStorage.setItem(CR_AMBIENT_LOCAL_ARMED_KEY, crAmbientLocalArmed ? '1' : '0');
  crAmbientUpdateToggleView();
}

function crAmbientUpdateToggleView() {
  const enabledEl = document.getElementById('setAmbientVoiceEnabled');
  if (enabledEl) enabledEl.checked = !!(crAmbientVoiceEnabled && crAmbientLocalArmed);
}

function crAmbientNativeBridge() {
  try {
    if (typeof _getNativeBridge === 'function') return _getNativeBridge('AionAudio');
  } catch(e) {}
  return null;
}

function crAmbientIsMobileLike() {
  return /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent || '');
}

function crAmbientSourceInfo() {
  if (crAmbientNativeBridge()) return { source: 'android_native', label: '手机麦克风' };
  if (crAmbientIsMobileLike()) return { source: 'phone_browser', label: '手机麦克风' };
  return { source: 'desktop_browser', label: '电脑麦克风' };
}

function crAmbientOwnerLabel(listener = crAmbientOwner) {
  if (!listener || !listener.active) return '';
  return listener.label || (listener.source && listener.source.includes('phone') ? '手机麦克风' : '电脑麦克风');
}

function crAmbientDesired() {
  return crAmbientVoiceEnabled && crAmbientLocalArmed && currentRoom && currentRoom.type === 'group';
}

function crAmbientIsCapturing() {
  return !!crAmbientStream || !!crAmbientUseNative;
}

function crAmbientApplyListenerState(listener) {
  crAmbientOwner = listener || null;
  const active = !!listener?.active;
  const owned = active && listener.client_id === crAmbientClientId;
  crAmbientOwned = owned;
  if (active && !owned) {
    const wasArmed = crAmbientLocalArmed || crAmbientIsCapturing();
    crAmbientSetLocalArmed(false);
    crAmbientStopHeartbeat();
    crAmbientStop(false);
    crAmbientSetStatus(`${crAmbientOwnerLabel(listener)}侦听中`, 'warn');
    crAmbientUpdateToggleView();
    if (wasArmed) toast(`${crAmbientOwnerLabel(listener)}已接管环境侦听`);
    return;
  }
  if (!active) {
    crAmbientOwned = false;
    if (crAmbientVoiceEnabled && crAmbientLocalArmed && currentRoom?.type === 'group') {
      crAmbientSyncRunning();
    } else if (crAmbientVoiceEnabled && !crAmbientLocalArmed) {
      crAmbientSetStatus('本端未开启', 'warn');
    }
    crAmbientUpdateToggleView();
  }
}

async function crAmbientClaim(takeover = false) {
  if (!crAmbientDesired() || crAmbientClaimInFlight) return false;
  crAmbientClaimInFlight = true;
  const src = crAmbientSourceInfo();
  try {
    const resp = await fetch(`${API}/ambient-voice/claim`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        client_id: crAmbientClientId,
        room_id: currentRoom?.id || '',
        source: src.source,
        label: src.label,
        takeover,
      }),
    });
    const result = await resp.json();
    if (result.claimed) {
      crAmbientOwner = result.listener;
      crAmbientOwned = true;
      crAmbientStartHeartbeat();
      crAmbientUpdateToggleView();
      return true;
    }
    crAmbientOwned = false;
    crAmbientOwner = result.listener || null;
    crAmbientStopHeartbeat();
    crAmbientStop(false);
    if (result.reason === 'occupied' && crAmbientOwner?.active) {
      crAmbientSetLocalArmed(false);
      crAmbientSetStatus(`${crAmbientOwnerLabel(crAmbientOwner)}侦听中`, 'warn');
    } else if (result.reason === 'disabled') {
      crAmbientSetLocalArmed(false);
      crAmbientSetStatus('关闭', '');
    } else {
      crAmbientSetStatus('待启动', 'warn');
    }
    crAmbientUpdateToggleView();
  } catch (e) {
    console.warn('[AmbientVoice] claim failed:', e);
    crAmbientOwned = false;
    crAmbientSetStatus('认领失败', 'err');
    crAmbientUpdateToggleView();
  } finally {
    crAmbientClaimInFlight = false;
  }
  return false;
}

function crAmbientStartHeartbeat() {
  if (crAmbientHeartbeatTimer) return;
  crAmbientHeartbeatTimer = setInterval(() => {
    if (crAmbientDesired()) crAmbientClaim(false);
    else crAmbientStopHeartbeat();
  }, 7000);
}

function crAmbientStopHeartbeat() {
  if (!crAmbientHeartbeatTimer) return;
  clearInterval(crAmbientHeartbeatTimer);
  crAmbientHeartbeatTimer = null;
}

async function crAmbientRelease() {
  crAmbientStopHeartbeat();
  const owned = crAmbientOwned;
  crAmbientOwned = false;
  crAmbientOwner = null;
  crAmbientUpdateToggleView();
  if (!owned) return;
  try {
    await fetch(`${API}/ambient-voice/release`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_id: crAmbientClientId }),
    });
  } catch (e) {
    console.warn('[AmbientVoice] release failed:', e);
  }
}

async function crAmbientRefreshListenerState() {
  try {
    const resp = await fetch(`${API}/ambient-voice/listener`);
    const result = await resp.json();
    if (result.listener) crAmbientApplyListenerState(result.listener);
  } catch (e) {
    console.warn('[AmbientVoice] listener state failed:', e);
  }
}

function crAmbientPauseForTts() {
  if (crAmbientTtsResumeTimer) {
    clearTimeout(crAmbientTtsResumeTimer);
    crAmbientTtsResumeTimer = null;
  }
  if (crAmbientResumeRetryTimer) {
    clearTimeout(crAmbientResumeRetryTimer);
    crAmbientResumeRetryTimer = null;
  }
  if (!crAmbientDesired() || !crAmbientOwned) return;
  crAmbientPausedForTts = true;
  crAmbientStop(false);
  crAmbientTranscriptBuffer = [];
  crAmbientWakePendingUntil = 0;
  crAmbientUpdateBufferView();
  crAmbientSetStatus('TTS播放中', 'warn');
}

function crAmbientScheduleResumeRetry(attempt = 1) {
  if (crAmbientResumeRetryTimer) clearTimeout(crAmbientResumeRetryTimer);
  if (attempt > 4) {
    if (crAmbientDesired() && crAmbientOwned && !crAmbientIsCapturing()) crAmbientSetStatus('待启动', 'warn');
    return;
  }
  crAmbientResumeRetryTimer = setTimeout(async () => {
    crAmbientResumeRetryTimer = null;
    if (crAmbientPausedForTts || _ttsEngine?.playing) return;
    if (!crAmbientDesired() || !crAmbientOwned || crAmbientIsCapturing()) return;
    crAmbientSetStatus('恢复侦听中', 'warn');
    await crAmbientSyncRunning({ resumeAfterTts: true });
    if (crAmbientDesired() && crAmbientOwned && !crAmbientIsCapturing()) {
      crAmbientScheduleResumeRetry(attempt + 1);
    }
  }, attempt === 1 ? 450 : 900);
}

function crAmbientResumeAfterTts(delayMs = 1200) {
  if (!crAmbientPausedForTts) return;
  if (_ttsEngine?.playing) return;
  if (typeof crReplayAudio !== 'undefined' && crReplayAudio && !crReplayAudio.paused && !crReplayAudio.ended) return;
  if (crAmbientTtsResumeTimer) clearTimeout(crAmbientTtsResumeTimer);
  crAmbientTtsResumeTimer = setTimeout(async () => {
    crAmbientTtsResumeTimer = null;
    if (!crAmbientPausedForTts) return;
    crAmbientPausedForTts = false;
    if (crAmbientDesired() && crAmbientOwned) {
      crAmbientSetStatus('恢复侦听中', 'warn');
      await crAmbientSyncRunning({ resumeAfterTts: true });
      if (!crAmbientIsCapturing()) crAmbientScheduleResumeRetry();
    }
  }, delayMs);
}

function crAmbientSetStatus(text, cls = '') {
  const el = document.getElementById('crAmbientVoiceStatus');
  if (!el) return;
  el.textContent = text;
  el.className = `ambient-voice-status ${cls}`.trim();
}

function crAmbientBufferText() {
  return crAmbientTranscriptBuffer.join('\n').trim();
}

function crAmbientUpdateBufferView() {
  const metaEl = document.getElementById('crAmbientBufferMeta');
  const previewEl = document.getElementById('crAmbientBufferPreview');
  const toggleEl = document.getElementById('crAmbientBufferToggle');
  const text = crAmbientBufferText();
  const chars = crAmbientTranscriptBuffer.join('').length;
  const count = crAmbientTranscriptBuffer.length;
  if (metaEl) metaEl.textContent = `${chars} 字 / ${count} 段`;
  if (toggleEl) toggleEl.textContent = crAmbientBufferExpanded ? '收起' : '查看';
  if (!previewEl) return;
  previewEl.classList.toggle('show', crAmbientBufferExpanded);
  previewEl.classList.toggle('empty', !text);
  previewEl.textContent = text || '暂无待检查片段';
}

function crToggleAmbientBufferView() {
  crAmbientBufferExpanded = !crAmbientBufferExpanded;
  crAmbientUpdateBufferView();
}

function crClearAmbientBuffer() {
  crAmbientTranscriptBuffer = [];
  crAmbientWakePendingUntil = 0;
  crAmbientUpdateBufferView();
  if (crAmbientCanRun()) crAmbientSetStatus('侦听中', 'on');
  toast('待检查片段已清空');
}

function crAmbientReadInputs(options = {}) {
  const wakeWord = (document.getElementById('setAmbientWakeWord')?.value || crAmbientWakeWord || '').trim();
  const stopWord = (document.getElementById('setAmbientStopWord')?.value || crAmbientStopWord || '').trim();
  const minChars = parseInt(document.getElementById('setAmbientMinChars')?.value || crAmbientMinChars, 10);
  const intervalSec = parseInt(document.getElementById('setAmbientIntervalSec')?.value || crAmbientIntervalSec, 10);
  const enabled = options.enabledOverride !== undefined ? !!options.enabledOverride : !!crAmbientVoiceEnabled;
  return {
    ambient_voice_enabled: enabled,
    ambient_voice_wake_word: wakeWord || '现在立刻唤醒',
    ambient_voice_stop_word: stopWord || '结束立刻唤醒',
    ambient_voice_min_chars: Math.max(80, Math.min(2000, Number.isFinite(minChars) ? minChars : 500)),
    ambient_voice_interval_seconds: Math.max(20, Math.min(600, Number.isFinite(intervalSec) ? intervalSec : 120)),
    ambient_voice_cooldown_seconds: crAmbientCooldownSec || 180,
  };
}

function crApplyAmbientVoiceConfig(cfg = {}) {
  crAmbientVoiceEnabled = !!cfg.ambient_voice_enabled;
  crAmbientWakeWord = (cfg.ambient_voice_wake_word || '现在立刻唤醒').trim() || '现在立刻唤醒';
  crAmbientStopWord = (cfg.ambient_voice_stop_word || '结束立刻唤醒').trim() || '结束立刻唤醒';
  crAmbientMinChars = Math.max(80, Math.min(2000, parseInt(cfg.ambient_voice_min_chars || 500, 10)));
  crAmbientIntervalSec = Math.max(20, Math.min(600, parseInt(cfg.ambient_voice_interval_seconds || 120, 10)));
  crAmbientCooldownSec = Math.max(30, Math.min(1800, parseInt(cfg.ambient_voice_cooldown_seconds || 180, 10)));
  const enabledEl = document.getElementById('setAmbientVoiceEnabled');
  const wakeEl = document.getElementById('setAmbientWakeWord');
  const stopEl = document.getElementById('setAmbientStopWord');
  const minEl = document.getElementById('setAmbientMinChars');
  const intervalEl = document.getElementById('setAmbientIntervalSec');
  crAmbientUpdateToggleView();
  if (wakeEl) wakeEl.value = crAmbientWakeWord;
  if (stopEl) stopEl.value = crAmbientStopWord;
  if (minEl) minEl.value = crAmbientMinChars;
  if (intervalEl) intervalEl.value = crAmbientIntervalSec;
  crAmbientSetStatus(
    crAmbientVoiceEnabled ? (crAmbientLocalArmed ? (crAmbientIsCapturing() ? '侦听中' : '待启动') : '本端未开启') : '关闭',
    crAmbientVoiceEnabled ? (crAmbientLocalArmed ? 'warn' : 'warn') : ''
  );
  crAmbientUpdateBufferView();
}

async function crSaveAmbientVoiceSettings(options = {}) {
  const next = crAmbientReadInputs(options);
  crAmbientVoiceEnabled = next.ambient_voice_enabled;
  crAmbientWakeWord = next.ambient_voice_wake_word;
  crAmbientStopWord = next.ambient_voice_stop_word;
  crAmbientMinChars = next.ambient_voice_min_chars;
  crAmbientIntervalSec = next.ambient_voice_interval_seconds;
  crAmbientCooldownSec = next.ambient_voice_cooldown_seconds;
  await api('/config', { method: 'PUT', body: JSON.stringify(next) });
  if (!options.silent) toast('环境侦听设置已保存');
  if (options.sync !== false) crAmbientSyncRunning();
}

async function crOnAmbientVoiceToggle() {
  const enabled = !!document.getElementById('setAmbientVoiceEnabled')?.checked;
  crAmbientLastUserToggleAt = Date.now();
  crAmbientSetLocalArmed(enabled);
  try {
    await crSaveAmbientVoiceSettings({ silent: true, sync: false, enabledOverride: enabled });
    if (enabled) {
      await crAmbientSyncRunning({ takeover: true });
      crAmbientUpdateToggleView();
      toast(crAmbientOwned ? `${crAmbientSourceInfo().label}环境侦听已开启` : '环境侦听等待接管');
    } else {
      await crAmbientRelease();
      crAmbientStop(true);
      crAmbientUpdateToggleView();
      toast('环境侦听已关闭');
    }
  } catch (e) {
    crAmbientUpdateToggleView();
    toast('环境侦听保存失败');
  }
}

function crAmbientCanRun() {
  return crAmbientDesired() && crAmbientOwned;
}

async function crAmbientSyncRunning(options = {}) {
  if (!crAmbientVoiceEnabled) {
    crAmbientPausedForTts = false;
    if (crAmbientResumeRetryTimer) { clearTimeout(crAmbientResumeRetryTimer); crAmbientResumeRetryTimer = null; }
    await crAmbientRelease();
    crAmbientStop(true);
    crAmbientUpdateToggleView();
    return;
  }
  if (!crAmbientLocalArmed) {
    crAmbientPausedForTts = false;
    if (crAmbientResumeRetryTimer) { clearTimeout(crAmbientResumeRetryTimer); crAmbientResumeRetryTimer = null; }
    await crAmbientRelease();
    crAmbientStop(false);
    crAmbientSetStatus('本端未开启', 'warn');
    crAmbientUpdateToggleView();
    return;
  }
  if (!currentRoom || currentRoom.type !== 'group') {
    await crAmbientRelease();
    crAmbientStop(false);
    crAmbientSetStatus('切到群聊后生效', 'warn');
    crAmbientUpdateToggleView();
    return;
  }
  if (crAmbientPausedForTts || _ttsEngine?.playing) {
    crAmbientSetStatus('TTS播放中', 'warn');
    return;
  }
  if (!crAmbientOwned) {
    const claimed = await crAmbientClaim(!!options.takeover);
    if (!claimed) return;
  }
  if (!crAmbientIsCapturing()) await crAmbientStart();
}

async function crAmbientStart() {
  if (crAmbientIsCapturing() || !crAmbientCanRun()) return;
  const bridge = crAmbientNativeBridge();
  if (bridge) {
    if (crAmbientStartNative(bridge)) return;
    console.warn('[AmbientVoice] native bridge start failed, falling back to getUserMedia');
  }
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
    crAmbientSetStatus('浏览器不支持', 'err');
    return;
  }
  try {
    crAmbientStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    crAmbientAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
    await crAmbientAudioCtx.resume().catch(() => {});
    const source = crAmbientAudioCtx.createMediaStreamSource(crAmbientStream);
    crAmbientAnalyser = crAmbientAudioCtx.createAnalyser();
    crAmbientAnalyser.fftSize = 1024;
    source.connect(crAmbientAnalyser);
    crAmbientNoiseFloor = 0.008;
    crAmbientTranscriptBuffer = [];
    crAmbientUpdateBufferView();
    crAmbientSetStatus('侦听中', 'on');
    crAmbientUpdateToggleView();
    crAmbientMonitor();
  } catch (e) {
    console.warn('[AmbientVoice] start failed:', e);
    crAmbientStop(false);
    crAmbientSetStatus('麦克风失败', 'err');
    toast('无法访问麦克风');
  }
}

function crAmbientStartNative(bridge) {
  crAmbientNativeChunks = [];
  crAmbientNativeSegmentChunks = [];
  crAmbientNativeRecording = false;
  crAmbientUseNative = true;
  crAmbientDiscardSegment = false;
  window._ambientNativeOnChunk = (b64) => crAmbientOnNativeChunk(b64);
  try { if (window.top !== window) window.top._ambientNativeOnChunk = window._ambientNativeOnChunk; } catch(e) {}
  try { if (window.parent !== window) window.parent._ambientNativeOnChunk = window._ambientNativeOnChunk; } catch(e) {}
  const ok = bridge.start();
  if (!ok) {
    crAmbientUseNative = false;
    window._ambientNativeOnChunk = null;
    return false;
  }
  crAmbientNoiseFloor = 0.008;
  crAmbientSpeechFrames = 0;
  crAmbientTranscriptBuffer = [];
  crAmbientUpdateBufferView();
  crAmbientSetStatus('手机侦听中', 'on');
  crAmbientUpdateToggleView();
  return true;
}

function crAmbientNativeLevel(b64) {
  try {
    const bin = atob(b64);
    if (!bin) return 0;
    let sum = 0;
    let count = 0;
    for (let i = 0; i + 1 < bin.length; i += 2) {
      let v = bin.charCodeAt(i) | (bin.charCodeAt(i + 1) << 8);
      if (v & 0x8000) v -= 0x10000;
      const n = v / 32768;
      sum += n * n;
      count++;
    }
    return count ? Math.sqrt(sum / count) : 0;
  } catch(e) {
    return 0;
  }
}

function crAmbientOnNativeChunk(b64) {
  if (!crAmbientUseNative || !crAmbientCanRun() || !b64) return;
  const now = Date.now();
  const level = crAmbientNativeLevel(b64);
  if (!crAmbientNativeRecording) {
    crAmbientNoiseFloor = crAmbientNoiseFloor * 0.96 + Math.min(level, 0.08) * 0.04;
  }
  const threshold = Math.max(0.018, crAmbientNoiseFloor * 3.0);
  const isSpeech = level > threshold;
  if (isSpeech) crAmbientSpeechFrames += 1;
  else crAmbientSpeechFrames = 0;

  if (!crAmbientNativeRecording && !crAmbientEvaluating && crAmbientSpeechFrames >= 3) {
    crAmbientNativeRecording = true;
    crAmbientNativeSegmentChunks = [];
    crAmbientSegmentStartedAt = now;
    crAmbientSilenceStartedAt = 0;
    crAmbientSetStatus('听到人声', 'on');
  }

  if (!crAmbientNativeRecording) return;
  crAmbientNativeSegmentChunks.push(b64);
  if (isSpeech) {
    crAmbientSilenceStartedAt = 0;
  } else if (!crAmbientSilenceStartedAt) {
    crAmbientSilenceStartedAt = now;
  }

  if ((crAmbientSilenceStartedAt && now - crAmbientSilenceStartedAt > 950)
      || now - crAmbientSegmentStartedAt > 16000) {
    crAmbientStopNativeSegment();
  }
}

function crAmbientStopNativeSegment(updateStatus = true) {
  if (!crAmbientNativeRecording) return;
  const duration = (Date.now() - crAmbientSegmentStartedAt) / 1000;
  const chunks = crAmbientNativeSegmentChunks.slice();
  const discard = crAmbientDiscardSegment;
  crAmbientNativeRecording = false;
  crAmbientNativeSegmentChunks = [];
  if (!discard && chunks.length) {
    crAmbientHandleBlob(_crBuildWav(chunks), duration);
  }
  if (updateStatus) crAmbientSetStatus(crAmbientWakePendingUntil > Date.now() ? '即时侦听' : '侦听中', 'on');
}

function crAmbientStop(updateStatus = true) {
  if (crAmbientVadTimer) {
    clearTimeout(crAmbientVadTimer);
    crAmbientVadTimer = null;
  }
  crAmbientDiscardSegment = true;
  if (crAmbientNativeRecording) crAmbientStopNativeSegment(false);
  if (crAmbientUseNative) {
    const bridge = crAmbientNativeBridge();
    if (bridge) try { bridge.stop(); } catch(e) {}
    const nativeHandler = window._ambientNativeOnChunk;
    crAmbientUseNative = false;
    crAmbientNativeChunks = [];
    crAmbientNativeSegmentChunks = [];
    crAmbientNativeRecording = false;
    window._ambientNativeOnChunk = null;
    try { if (window.top !== window && window.top._ambientNativeOnChunk === nativeHandler) window.top._ambientNativeOnChunk = null; } catch(e) {}
    try { if (window.parent !== window && window.parent._ambientNativeOnChunk === nativeHandler) window.parent._ambientNativeOnChunk = null; } catch(e) {}
  }
  if (crAmbientMediaRecorder && crAmbientMediaRecorder.state !== 'inactive') {
    try { crAmbientMediaRecorder.stop(); } catch {}
  }
  crAmbientMediaRecorder = null;
  crAmbientRecording = false;
  crAmbientChunks = [];
  crAmbientTranscriptBuffer = [];
  crAmbientUpdateBufferView();
  crAmbientWakePendingUntil = 0;
  if (crAmbientStream) {
    crAmbientStream.getTracks().forEach(t => t.stop());
    crAmbientStream = null;
  }
  if (crAmbientAudioCtx) {
    crAmbientAudioCtx.close().catch(() => {});
    crAmbientAudioCtx = null;
  }
  crAmbientAnalyser = null;
  if (updateStatus) {
    if (!crAmbientVoiceEnabled) crAmbientSetStatus('关闭', '');
    else if (!crAmbientLocalArmed) crAmbientSetStatus('本端未开启', 'warn');
    else if (crAmbientOwner?.active && crAmbientOwner.client_id !== crAmbientClientId) crAmbientSetStatus(`${crAmbientOwnerLabel()}侦听中`, 'warn');
    else crAmbientSetStatus('待启动', 'warn');
  }
  crAmbientUpdateToggleView();
}

function crAmbientLevel() {
  if (!crAmbientAnalyser) return 0;
  const data = new Uint8Array(crAmbientAnalyser.fftSize);
  crAmbientAnalyser.getByteTimeDomainData(data);
  let sum = 0;
  for (let i = 0; i < data.length; i++) {
    const v = (data[i] - 128) / 128;
    sum += v * v;
  }
  return Math.sqrt(sum / data.length);
}

function crAmbientMonitor() {
  if (!crAmbientStream || !crAmbientAnalyser || !crAmbientCanRun()) return;
  const now = Date.now();
  const level = crAmbientLevel();
  if (!crAmbientRecording) {
    crAmbientNoiseFloor = crAmbientNoiseFloor * 0.96 + Math.min(level, 0.08) * 0.04;
  }
  const threshold = Math.max(0.018, crAmbientNoiseFloor * 3.0);
  const isSpeech = level > threshold;
  if (isSpeech) crAmbientSpeechFrames += 1;
  else crAmbientSpeechFrames = 0;

  if (!crAmbientRecording && crAmbientSpeechFrames >= 3) {
    crAmbientStartSegment();
  }
  if (crAmbientRecording) {
    if (isSpeech) {
      crAmbientSilenceStartedAt = 0;
    } else if (!crAmbientSilenceStartedAt) {
      crAmbientSilenceStartedAt = now;
    }
    if ((crAmbientSilenceStartedAt && now - crAmbientSilenceStartedAt > 950)
        || now - crAmbientSegmentStartedAt > 16000) {
      crAmbientStopSegment();
    }
  }
  crAmbientVadTimer = setTimeout(crAmbientMonitor, 100);
}

function crAmbientStartSegment() {
  if (!crAmbientStream || crAmbientRecording || crAmbientEvaluating) return;
  const mime = _crGetVoiceMime();
  crAmbientChunks = [];
  crAmbientDiscardSegment = false;
  try {
    crAmbientMediaRecorder = new MediaRecorder(crAmbientStream, mime ? { mimeType: mime } : undefined);
    crAmbientMediaRecorder.ondataavailable = e => { if (e.data.size > 0) crAmbientChunks.push(e.data); };
    crAmbientMediaRecorder.onstop = () => {
      const duration = (Date.now() - crAmbientSegmentStartedAt) / 1000;
      const blob = new Blob(crAmbientChunks, { type: crAmbientMediaRecorder?.mimeType || mime || 'audio/webm' });
      const discard = crAmbientDiscardSegment;
      crAmbientChunks = [];
      crAmbientMediaRecorder = null;
      if (!discard) crAmbientHandleBlob(blob, duration);
    };
    crAmbientSegmentStartedAt = Date.now();
    crAmbientSilenceStartedAt = 0;
    crAmbientRecording = true;
    crAmbientMediaRecorder.start(250);
    crAmbientSetStatus('听到人声', 'on');
  } catch (e) {
    console.warn('[AmbientVoice] recorder failed:', e);
    crAmbientRecording = false;
  }
}

function crAmbientStopSegment() {
  if (!crAmbientRecording || !crAmbientMediaRecorder) return;
  crAmbientRecording = false;
  try {
    if (crAmbientMediaRecorder.state !== 'inactive') crAmbientMediaRecorder.stop();
  } catch {}
  crAmbientSetStatus(crAmbientWakePendingUntil > Date.now() ? '即时侦听' : '侦听中', 'on');
}

async function crAmbientHandleBlob(blob, duration) {
  if (!crAmbientCanRun() || duration < 0.55 || !blob || blob.size < 800) return;
  crAmbientSetStatus('识别中', 'on');
  const text = await crAmbientTranscribe(blob);
  if (!text) {
    crAmbientSetStatus('侦听中', 'on');
    return;
  }
  await crAmbientHandleTranscript(text);
}

async function crAmbientTranscribe(blob) {
  const ext = blob.type.includes('wav') ? 'wav' : (blob.type.includes('mp4') ? 'mp4' : 'webm');
  for (let i = 0; i < 2; i++) {
    try {
      const fd = new FormData();
      fd.append('file', blob, `ambient.${ext}`);
      const resp = await fetch('/api/voice/transcribe', { method: 'POST', body: fd });
      const data = await resp.json();
      const text = (data.text || '').trim();
      if (text) return text;
    } catch (e) {
      console.warn(`[AmbientVoice] ASR attempt ${i + 1} failed:`, e);
    }
  }
  return '';
}

function crAmbientTextHasPhrase(text, phrase) {
  const wake = (phrase || '').trim();
  if (!wake) return false;
  const compactText = text.replace(/\s+/g, '');
  const compactWake = wake.replace(/\s+/g, '');
  return text.includes(wake) || (compactWake && compactText.includes(compactWake));
}

function crAmbientTextHasWakeWord(text) {
  return crAmbientTextHasPhrase(text, crAmbientWakeWord);
}

function crAmbientTextHasStopWord(text) {
  return crAmbientTextHasPhrase(text, crAmbientStopWord);
}

function crAmbientRemoveWakeWord(text) {
  const wake = (crAmbientWakeWord || '').trim();
  if (!wake) return text.trim();
  return text.replaceAll(wake, '').replace(/\s+/g, ' ').trim();
}

async function crAmbientHandleTranscript(text) {
  const cleaned = text.replace(/\s+/g, ' ').trim();
  if (!cleaned || !crAmbientCanRun()) return;
  if (crAmbientTextHasStopWord(cleaned)) {
    const wasPending = crAmbientWakePendingUntil > Date.now();
    crAmbientWakePendingUntil = 0;
    crAmbientTranscriptBuffer = [];
    crAmbientUpdateBufferView();
    crAmbientSetStatus(wasPending ? '即时已结束' : '侦听中', wasPending ? 'warn' : 'on');
    if (wasPending) {
      toast('即时侦听已结束');
      setTimeout(() => { if (crAmbientCanRun()) crAmbientSetStatus('侦听中', 'on'); }, 1800);
    }
    return;
  }
  if (crAmbientTextHasWakeWord(cleaned)) {
    const rest = crAmbientRemoveWakeWord(cleaned);
    crAmbientTranscriptBuffer = [];
    crAmbientUpdateBufferView();
    if (rest.length >= 4) {
      await crAmbientEvaluate(rest, true);
    } else {
      crAmbientWakePendingUntil = Date.now() + 20000;
      crAmbientSetStatus('即时侦听', 'warn');
    }
    return;
  }
  if (crAmbientWakePendingUntil > Date.now()) {
    crAmbientWakePendingUntil = 0;
    await crAmbientEvaluate(cleaned, true);
    return;
  }

  crAmbientTranscriptBuffer.push(cleaned);
  while (crAmbientTranscriptBuffer.join('').length > 2200) crAmbientTranscriptBuffer.shift();
  crAmbientUpdateBufferView();
  const total = crAmbientTranscriptBuffer.join('').length;
  const dueByChars = total >= crAmbientMinChars;
  const dueByTime = total >= 80 && Date.now() - crAmbientLastCheckAt >= crAmbientIntervalSec * 1000;
  crAmbientSetStatus(`累计 ${total}`, 'on');
  if (dueByChars || dueByTime) {
    const transcript = crAmbientTranscriptBuffer.join('\n');
    crAmbientTranscriptBuffer = [];
    crAmbientUpdateBufferView();
    await crAmbientEvaluate(transcript, false);
  }
}

async function crAmbientEvaluate(transcript, forced) {
  if (!currentRoom || currentRoom.type !== 'group' || crAmbientEvaluating) return;
  if (!crAmbientOwned) {
    crAmbientSetStatus('等待接管', 'warn');
    return;
  }
  crAmbientEvaluating = true;
  crAmbientLastCheckAt = Date.now();
  crAmbientSetStatus(forced ? '唤醒中' : '筛选中', forced ? 'warn' : 'on');
  const src = crAmbientSourceInfo();
  try {
    const resp = await fetch(`${API}/rooms/${currentRoom.id}/ambient-voice/evaluate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        transcript,
        forced,
        listener_client_id: crAmbientClientId,
        listener_source: src.source,
        listener_label: src.label,
        model: chatroomModel,
        connor_model: chatroomConnorModel,
        tts_enabled: crTtsEnabled,
        tts_aion_voice: crTtsAionVoice,
        tts_connor_voice: crTtsConnorVoice,
      }),
    });
    const result = await resp.json();
    if (result.debug) crHandleDebug(result.debug);
    if (result.triggered) {
      const speakerName = crName(result.speaker);
      crAmbientSetStatus(`${speakerName} 已唤醒`, 'warn');
      toast(`${speakerName} 听见了，正在插话`);
      setTimeout(() => { if (crAmbientCanRun()) crAmbientSetStatus('侦听中', 'on'); }, 2500);
    } else if (result.skipped === 'cooldown') {
      crAmbientSetStatus(`冷却 ${result.cooldown_remaining || ''}s`, 'warn');
    } else if (result.skipped === 'listener_lost' || result.skipped === 'listener_required' || result.skipped === 'listener_expired') {
      crAmbientApplyListenerState(result.listener || null);
    } else {
      crAmbientSetStatus(crAmbientCanRun() ? '侦听中' : '待启动', crAmbientCanRun() ? 'on' : 'warn');
    }
  } catch (e) {
    console.warn('[AmbientVoice] evaluate failed:', e);
    crAmbientSetStatus('筛选失败', 'err');
  } finally {
    crAmbientEvaluating = false;
  }
}

window.addEventListener('beforeunload', () => {
  if (crAmbientOwned) {
    try {
      const body = JSON.stringify({ client_id: crAmbientClientId });
      navigator.sendBeacon?.(`${API}/ambient-voice/release`, new Blob([body], { type: 'application/json' }));
    } catch(e) {}
  }
  crAmbientStop(false);
});

function crHandleDebug(data) {
  const d = data?.data && data.data.type === 'debug' ? data.data : data;
  if (!d || d.type !== 'debug') return;
  if (d.room_id && currentRoom && d.room_id !== currentRoom.id) return;
  if (d.msg_id) crMsgDebugData[d.msg_id] = d;
  crAddSystemLog(d);
}

function crAddSystemLog(d) {
  if (!d) return;
  if (d.msg_id && crSystemLogs.some(log => log.msg_id === d.msg_id)) return;
  const now = new Date();
  const ts = String(now.getHours()).padStart(2, '0') + ':' +
    String(now.getMinutes()).padStart(2, '0') + ':' +
    String(now.getSeconds()).padStart(2, '0');
  crSystemLogs.unshift({ ...d, _ts: ts, _id: 'cr_slog_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6) });
  if (d.has_error) {
    crSysLogHasUnreadError = true;
    document.getElementById('crSysLogBtn')?.classList.add('cr-syslog-btn-flash');
  }
  crRenderSystemLogList();
}

function crFixed(value, digits = 4) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(digits) : (0).toFixed(digits);
}

function crBuildTokenHtml(u) {
  if (!u) return '🔤 token 无数据';
  const raw = u.raw;
  const parts = [];
  if (raw) {
    if ('promptTokenCount' in raw) {
      parts.push(`<span class="tok-label">输入:</span><span class="tok-value">${raw.promptTokenCount || 0}</span>`);
      if (raw.thoughtsTokenCount) parts.push(`<span class="tok-label">思考:</span><span class="tok-value tok-thinking">${raw.thoughtsTokenCount}</span>`);
      if (raw.cachedContentTokenCount) parts.push(`<span class="tok-label">缓存:</span><span class="tok-value tok-cached">${raw.cachedContentTokenCount}</span>`);
      parts.push(`<span class="tok-label">输出:</span><span class="tok-value">${raw.candidatesTokenCount || 0}</span>`);
      if (raw.toolUsePromptTokenCount) parts.push(`<span class="tok-label">工具:</span><span class="tok-value">${raw.toolUsePromptTokenCount}</span>`);
      parts.push(`<span class="tok-label">总计:</span><span class="tok-value">${raw.totalTokenCount || 0}</span>`);
    } else if ('prompt_tokens' in raw) {
      parts.push(`<span class="tok-label">输入:</span><span class="tok-value">${raw.prompt_tokens || 0}</span>`);
      if (raw.prompt_tokens_details?.cached_tokens) parts.push(`<span class="tok-label">缓存:</span><span class="tok-value tok-cached">${raw.prompt_tokens_details.cached_tokens}</span>`);
      parts.push(`<span class="tok-label">输出:</span><span class="tok-value">${raw.completion_tokens || 0}</span>`);
      if (raw.completion_tokens_details?.reasoning_tokens) parts.push(`<span class="tok-label">推理:</span><span class="tok-value tok-thinking">${raw.completion_tokens_details.reasoning_tokens}</span>`);
      parts.push(`<span class="tok-label">总计:</span><span class="tok-value">${raw.total_tokens || 0}</span>`);
    } else if ('input_tokens' in raw || 'output_tokens' in raw) {
      const input = raw.input_tokens || 0;
      const output = raw.output_tokens || 0;
      parts.push(`<span class="tok-label">输入:</span><span class="tok-value">${input}</span>`);
      parts.push(`<span class="tok-label">输出:</span><span class="tok-value">${output}</span>`);
      parts.push(`<span class="tok-label">总计:</span><span class="tok-value">${raw.total_tokens || input + output}</span>`);
    }
  }
  if (parts.length === 0) {
    parts.push(`<span class="tok-label">输入:</span><span class="tok-value">${u.prompt_tokens || 0}</span>`);
    parts.push(`<span class="tok-label">输出:</span><span class="tok-value">${u.completion_tokens || 0}</span>`);
    parts.push(`<span class="tok-label">总计:</span><span class="tok-value">${u.total_tokens || 0}</span>`);
  }
  return '🔤 ' + parts.join(' ');
}

function crBuildTokenDetailHtml(u) {
  if (!u || !u.raw) return '';
  let html = '<h4>🔤 Token 用量详情（服务器原始数据）</h4><div class="cr-syslog-token-raw">';
  for (const [k, v] of Object.entries(u.raw)) {
    if (v === null || v === undefined) continue;
    const val = typeof v === 'object' ? JSON.stringify(v) : String(v);
    html += `<div><span class="tok-label">${esc(k)}:</span> <span class="tok-value">${esc(val)}</span></div>`;
  }
  html += '</div>';
  return html;
}

function crFormatSystemLogForCopy(d) {
  const lines = [];
  lines.push(`[${d._ts || ''}] ${d.model || (d.log_kind === 'ambient_voice' ? 'ambient-sentinel' : '?')}`);
  if (d.has_error && d.error_text) lines.push(`错误: ${d.error_text}`);
  if (d.log_kind === 'ambient_voice') {
    if (d.ambient_source) lines.push(`音源: ${d.ambient_source}`);
    if (d.ambient_topic) lines.push(`话题: ${d.ambient_topic}`);
    if (d.ambient_reason) lines.push(`原因: ${d.ambient_reason}`);
    if (d.ambient_summary) lines.push(`去噪摘要:\n${d.ambient_summary}`);
  }
  if (d.recall_topic) lines.push(`话题: ${d.recall_topic}`);
  if (d.recall_keywords) lines.push(`关键词: ${d.recall_keywords}`);
  if (d.recall_query) lines.push(`向量匹配查询:\n${d.recall_query}`);
  if (Array.isArray(d.debug_top6) && d.debug_top6.length) {
    lines.push('记忆库 Top6:');
    d.debug_top6.forEach((m, i) => {
      lines.push(`${i + 1}. score=${m.score} vec=${m.vec_sim} kw=${m.kw_score} imp=${m.importance}\n${m.content || ''}`);
    });
  }
  if (Array.isArray(d.recalled_memories) && d.recalled_memories.length) {
    lines.push('实际召回记忆:');
    d.recalled_memories.forEach((m, i) => {
      lines.push(`${i + 1}. score=${m.score} type=${m.type || ''}\n${m.content || ''}`);
    });
  }
  if (Array.isArray(d.prompt_messages) && d.prompt_messages.length) {
    lines.push('完整 Prompt:');
    d.prompt_messages.forEach((m, i) => {
      lines.push(`--- ${i + 1}. ${m.role || ''} ---\n${m.content || ''}`);
    });
  }
  return lines.join('\n\n');
}

async function crCopySystemLogEntry(id) {
  const item = crSystemLogs.find(log => log._id === id);
  if (!item) return;
  try {
    await navigator.clipboard.writeText(crFormatSystemLogForCopy(item));
    toast('已复制本条系统日志全文');
  } catch (e) {
    console.error('复制聊天室系统日志失败:', e);
    toast('复制失败');
  }
}

async function crCopyAllSystemLogs() {
  if (!crSystemLogs.length) return;
  try {
    await navigator.clipboard.writeText(crSystemLogs.map(crFormatSystemLogForCopy).join('\n\n==============================\n\n'));
    toast('已复制全部系统日志');
  } catch (e) {
    console.error('复制全部聊天室系统日志失败:', e);
    toast('复制失败');
  }
}

function crAmbientSkipLabel(reason) {
  return ({
    sentinel_rejected: '哨兵未放行',
    cooldown: '冷却中',
    already_generating: '已有插话生成中',
    not_group_room: '不在群聊',
    disabled: '环境侦听关闭',
    too_short: '内容过短',
  })[reason] || reason || '';
}

function crBuildAmbientLogEntry(d, detailId) {
  const skipped = d.ambient_skipped || '';
  const status = d.ambient_should_wake ? '已唤醒' : (skipped ? crAmbientSkipLabel(skipped) : '未唤醒');
  const statusCls = d.ambient_should_wake ? 'wake' : (skipped === 'sentinel_rejected' ? 'reject' : 'skip');
  const speaker = d.ambient_speaker ? `，唤醒 ${esc(crName(d.ambient_speaker))}` : '';
  const topic = d.ambient_topic ? `，话题：${esc(d.ambient_topic)}` : '';
  const reason = d.ambient_reason || skipped || '';
  let detailHtml = '';
  if (d.has_error && d.error_text) {
    detailHtml += `<div style="color:#f44336;margin-bottom:8px;word-break:break-all;">${esc(d.error_text)}</div>`;
  }
  detailHtml += `<div class="debug-recall-keywords">模式: ${d.ambient_forced ? '即时唤醒（不经过哨兵）' : '普通环境哨兵'}</div>`;
  if (d.ambient_source) detailHtml += `<div class="debug-recall-keywords">音源: ${esc(d.ambient_source)}</div>`;
  detailHtml += `<div class="debug-recall-keywords">判定: <span class="ambient-log-status ${statusCls}">${esc(status)}</span>${speaker}</div>`;
  if (reason) detailHtml += `<div class="debug-recall-keywords">原因: ${esc(reason)}</div>`;
  if (d.ambient_topic) detailHtml += `<div class="debug-recall-keywords">话题: <span style="color:#4fc3f7">${esc(d.ambient_topic)}</span></div>`;
  if (d.ambient_importance !== undefined) detailHtml += `<div class="debug-recall-keywords">重要度: ${crFixed(d.ambient_importance, 2)}</div>`;
  if (d.ambient_summary) detailHtml += `<h4>去噪摘要</h4><div class="debug-recall-query">${esc(d.ambient_summary)}</div>`;
  const prompts = Array.isArray(d.prompt_messages) ? d.prompt_messages : [];
  if (prompts.length) {
    const text = prompts.map(m => m.content || '').join('\n\n');
    detailHtml += `<h4>ASR 原文片段（${d.ambient_transcript_chars || 0} 字）</h4><div class="debug-prompt-list"><div class="debug-prompt-text">${esc(text)}</div></div>`;
  }
  return `<div class="${d.has_error ? 'cr-syslog-entry error-entry' : 'cr-syslog-entry'}">
    <span class="cr-syslog-time">${d._ts}</span>
    <span class="cr-syslog-model">环境语音哨兵</span>
    <span class="ambient-log-status ${statusCls}">${esc(status)}</span>
    <span class="cr-syslog-tokens">${esc(reason)}${topic}</span>
    ${detailHtml ? `<button class="cr-syslog-detail-toggle" onclick="crToggleSysLogDetail('${detailId}')">详情 ▾</button>` : ''}
    ${detailHtml ? `<button class="cr-syslog-detail-toggle" onclick="crCopySystemLogEntry('${d._id}')">复制全文</button>` : ''}
    ${detailHtml ? `<div class="cr-syslog-detail" id="${detailId}">${detailHtml}</div>` : ''}
  </div>`;
}

function crRenderSystemLogList() {
  const el = document.getElementById('crSysLogList');
  const countEl = document.getElementById('crSysLogCount');
  if (!el) return;
  if (countEl) countEl.textContent = `共 ${crSystemLogs.length} 条（刷新后清空）`;
  if (crSystemLogs.length === 0) {
    el.innerHTML = '<div class="cr-syslog-empty">暂无日志</div>';
    return;
  }

  el.innerHTML = crSystemLogs.map(d => {
    const detailId = 'crsd_' + d._id;
    if (d.log_kind === 'ambient_voice') {
      return crBuildAmbientLogEntry(d, detailId);
    }
    const tokenText = crBuildTokenHtml(d.usage);
    const isError = !!d.has_error;
    const mems = Array.isArray(d.recalled_memories) ? d.recalled_memories : [];
    const memText = mems.length ? `🧠 召回 ${mems.length} 条记忆` : '🧠 无相关记忆';
    const memCls = mems.length ? 'cr-syslog-mem' : 'cr-syslog-mem none';
    let detailHtml = '';

    if (isError && d.error_text) {
      detailHtml += `<div style="color:#f44336;margin-bottom:8px;word-break:break-all;">⚠️ ${esc(d.error_text)}</div>`;
    }
    detailHtml += crBuildTokenDetailHtml(d.usage);
    if (d.is_search_needed !== undefined) {
      const searchTag = d.is_search_needed ? '<span style="color:#4caf50">✅ 需要搜索</span>' : '<span style="color:#ff9800">⏭️ 无需搜索</span>';
      detailHtml += `<div class="debug-recall-keywords">前置哨兵判断: ${searchTag}</div>`;
    }
    if (d.recall_topic) {
      detailHtml += `<div class="debug-recall-keywords">📌 话题: <span style="color:#4fc3f7">${esc(d.recall_topic)}</span></div>`;
    }
    if (d.recall_keywords) {
      detailHtml += `<div class="debug-recall-keywords">🏷️ 关键词: ${esc(d.recall_keywords)}</div>`;
    }
    if (d.recall_query) {
      detailHtml += `<h4>🔍 向量匹配查询</h4><div class="debug-recall-query">${esc(d.recall_query)}</div>`;
    }
    const top6 = Array.isArray(d.debug_top6) ? d.debug_top6 : [];
    if (top6.length) {
      const topItems = top6.map(m => {
        const score = Number(m.score || 0);
        const passed = score >= 0.45;
        return `<div class="debug-mem-item ${passed ? '' : 'below-threshold'}"><span class="score">${crFixed(score)}</span><span class="score-detail">vec:${crFixed(m.vec_sim, 3)} kw:${crFixed(m.kw_score, 3)} imp:${crFixed(m.importance, 2)}</span><span class="content">${esc(m.content || '')}</span>${!passed ? '<span class="threshold-tag">未达标</span>' : ''}</div>`;
      }).join('');
      detailHtml += `<h4>📊 记忆库 Top6 得分 (阈值 0.45)</h4>${topItems}`;
    }
    if (mems.length) {
      const memItems = mems.map(m => `<div class="debug-mem-item"><span class="score">${crFixed(m.score)}</span><span class="type">${esc(m.type || '')}</span><span class="content">${esc(m.content || '')}</span></div>`).join('');
      detailHtml += `<h4>🧠 实际召回记忆 (${mems.length} 条)</h4>${memItems}`;
    }
    const prompts = Array.isArray(d.prompt_messages) ? d.prompt_messages : [];
    if (prompts.length) {
      const pmItems = prompts.map(m => {
        const roleCls = m.role === 'user' ? 'user' : 'assistant';
        return `<div class="debug-prompt-item"><span class="debug-prompt-role ${roleCls}">[${esc(m.role || '')}]</span> <span class="debug-prompt-text">${esc(m.content || '')}</span></div>`;
      }).join('');
      detailHtml += `<h4>📝 完整 Prompt (${d.prompt_count || prompts.length} 条)</h4><div class="debug-prompt-list">${pmItems}</div>`;
    }

    const hasDetail = detailHtml.length > 0;
    const errorTag = isError ? '<span class="cr-syslog-error-tag">❌ 错误</span>' : '';
    const entryCls = isError ? 'cr-syslog-entry error-entry' : 'cr-syslog-entry';
    return `<div class="${entryCls}">
      <span class="cr-syslog-time">${d._ts}</span>
      ${errorTag}
      <span class="cr-syslog-model">📦 ${esc(d.model || '?')}</span>
      <span class="cr-syslog-tokens">${tokenText}</span>
      <span class="${memCls}">${memText}</span>
      ${hasDetail ? `<button class="cr-syslog-detail-toggle" onclick="crToggleSysLogDetail('${detailId}')">详情 ▾</button>` : ''}
      ${hasDetail ? `<button class="cr-syslog-detail-toggle" onclick="crCopySystemLogEntry('${d._id}')">复制全文</button>` : ''}
      ${hasDetail ? `<div class="cr-syslog-detail" id="${detailId}">${detailHtml}</div>` : ''}
    </div>`;
  }).join('');
}

function crToggleSysLogDetail(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.toggle('show');
  const btn = el.previousElementSibling;
  if (btn && btn.classList.contains('cr-syslog-detail-toggle')) {
    btn.textContent = el.classList.contains('show') ? '收起 ▴' : '详情 ▾';
  }
}

function crOpenSystemLog() {
  crSysLogHasUnreadError = false;
  document.getElementById('crSysLogBtn')?.classList.remove('cr-syslog-btn-flash');
  crRenderSystemLogList();
  document.getElementById('crSysLogModal')?.classList.add('show');
}

function crCloseSystemLog() {
  document.getElementById('crSysLogModal')?.classList.remove('show');
}

function crClearSystemLog() {
  crSystemLogs = [];
  crSysLogHasUnreadError = false;
  document.getElementById('crSysLogBtn')?.classList.remove('cr-syslog-btn-flash');
  crRenderSystemLogList();
}

async function fetchCurrentModel(configPromise) {
  try {
    const [convs, models, cfg] = await Promise.all([
      fetch('/api/conversations').then(resp => resp.json()),
      fetch('/api/models').then(resp => resp.json()),
      configPromise || api('/config'),
    ]);
    if (Array.isArray(models)) chatroomModels = models;
    if (cfg?.connor_model) chatroomConnorModel = cfg.connor_model;
    if (cfg?.reply_order) chatroomReplyOrder = cfg.reply_order;
    if (Array.isArray(convs) && convs.length > 0 && convs[0].model) {
      chatroomModel = convs[0].model;
    }
    updateHeaderActions();
  } catch {}
}

function renderModelOptions(selected) {
  const keys = chatroomModels.length ? chatroomModels.map(m => m.key) : [chatroomModel || 'Codex', 'Codex'];
  return [...new Set(keys.filter(Boolean))].map(k => `<option value="${esc(k)}"${k === selected ? ' selected' : ''}>${esc(k)}</option>`).join('');
}

function updateHeaderActions() {
  const isGroup = currentRoom && currentRoom.type === 'group';
  const manualMode = isGroup && chatroomReplyOrder === 'manual';
  if (aiChatBtn) aiChatBtn.style.display = isGroup ? '' : 'none';
  if (replyAionBtn) replyAionBtn.style.display = manualMode ? '' : 'none';
  if (replyConnorBtn) replyConnorBtn.style.display = manualMode ? '' : 'none';
}

// ══════════════════════════════════════════════════
//  房间列表
// ══════════════════════════════════════════════════

async function loadRooms() {
  rooms = await api('/rooms');
  renderRoomList();
}

let activeTab = 'group';

function switchTab(tab) {
  activeTab = tab;
  document.querySelectorAll('.room-tab').forEach(el => {
    el.classList.toggle('active', el.dataset.tab === tab);
  });
  renderRoomList();
}

function renderRoomList() {
  const filtered = rooms.filter(r => r.type === activeTab);
  roomListEl.innerHTML = filtered.map(r => {
    const active = currentRoom && currentRoom.id === r.id ? 'active' : '';
    const typeBadge = r.type === 'connor_1v1'
      ? '<span class="type-badge connor">私聊</span>'
      : '<span class="type-badge group">群聊</span>';
    return `
      <div class="room-item ${active}" onclick="selectRoom('${r.id}')">
        ${typeBadge}
        <span class="title">${esc(r.title)}</span>
        <span class="msg-count">${r.message_count || 0}</span>
        <button class="del-btn" onclick="event.stopPropagation(); deleteRoom('${r.id}')" title="删除">✕</button>
      </div>`;
  }).join('');
  if (!filtered.length) {
    roomListEl.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text3);font-size:13px;">暂无' + (activeTab === 'group' ? '群聊' : '私聊') + '</div>';
  }
}

async function createRoom(type) {
  const now = new Date();
  const dateStr = `${now.getFullYear()}-${now.getMonth() + 1}-${now.getDate()}`;
  const label = type === 'connor_1v1' ? '私聊' : '群聊';
  const title = `${label} ${dateStr}`;
  const result = await api('/rooms', {
    method: 'POST',
    body: JSON.stringify({ title, type }),
  });
  if (result.error) {
    // connor_1v1 已存在，直接切过去
    if (result.existing_id) {
      switchTab(type);
      selectRoom(result.existing_id);
      closeSidebar();
    } else {
      toast(result.error);
    }
    return;
  }
  switchTab(type);
  await loadRooms();
  selectRoom(result.id);
  closeSidebar();
}

async function deleteRoom(roomId) {
  if (!confirm('确定删除此聊天室？消息和记忆将一并删除。')) return;
  await api(`/rooms/${roomId}`, { method: 'DELETE' });
  if (currentRoom && currentRoom.id === roomId) {
    currentRoom = null;
    renderEmptyChat();
  }
  await loadRooms();
}

async function selectRoom(roomId) {
  const room = rooms.find(r => r.id === roomId);
  if (!room) return;
  currentRoom = room;
  // 自动切换到对应 tab
  if (activeTab !== room.type) switchTab(room.type);
  else renderRoomList();
  roomTitleEl.textContent = room.title;
  // 退出语音模式（如果在语音模式中切换房间）
  if (_crVoiceMode) {
    _crVoiceMode = false;
    document.getElementById('crVoiceModeRow').classList.remove('active');
  }
  composer.style.display = 'flex';
  updateHeaderActions();
  resetChatSearch();
  await loadMessages();
  crAmbientSyncRunning();
  closeSidebar();
}

// ══════════════════════════════════════════════════
//  消息
// ══════════════════════════════════════════════════

async function loadMessages() {
  if (!currentRoom) return;
  oldestMsgTs = null;
  noMoreMessages = false;
  loadingOlder = false;
  const snapshotKey = `chatroom_messages_snapshot_v1_${currentRoom.id}`;
  let snapshotShown = false;
  try {
    const bridge = (() => {
      try { return window.top?.AppSharedData || window.AppSharedData || null; }
      catch(e) { return window.AppSharedData || null; }
    })();
    const rawSnapshot = bridge?.get?.(snapshotKey) || localStorage.getItem(snapshotKey) || '';
    const snapshot = JSON.parse(rawSnapshot || 'null');
    if (snapshot && Array.isArray(snapshot.messages)) {
      renderMessages(snapshot.messages);
      scrollToBottom(true);
      snapshotShown = true;
    }
  } catch(e) {}

  let msgs;
  try {
    msgs = await api(`/rooms/${currentRoom.id}/messages?limit=100`);
  } catch(e) {
    if (snapshotShown) return;
    throw e;
  }
  if (msgs && msgs.length) {
    oldestMsgTs = msgs[0].created_at;
    noMoreMessages = msgs.length < 100;
  } else {
    noMoreMessages = true;
  }
  renderMessages(msgs);
  scrollToBottom(true);
  try {
    const payload = JSON.stringify({ savedAt: Date.now(), messages: msgs });
    if (payload.length < 900000) {
      localStorage.setItem(snapshotKey, payload);
      try {
        const bridge = window.top?.AppSharedData || window.AppSharedData;
        bridge?.put?.(snapshotKey, payload);
      } catch(e) {}
    }
  } catch(e) {}
}

async function refreshCurrentChatroomFromServer(options = {}) {
  if (!currentRoom) return false;
  const roomId = currentRoom.id;

  try {
    rooms = await api('/rooms');
    const room = rooms.find(r => r.id === roomId);
    if (currentRoom && currentRoom.id === roomId) {
      if (room) {
        currentRoom = room;
        roomTitleEl.textContent = room.title;
        if (activeTab !== room.type) switchTab(room.type);
        else renderRoomList();
        updateHeaderActions();
      } else {
        renderRoomList();
      }
    }
  } catch(e) {
    console.warn('[chatroom] refresh rooms failed:', e);
  }

  let msgs;
  try {
    msgs = await api(`/rooms/${roomId}/messages?limit=100`);
  } catch(e) {
    console.warn('[chatroom] refresh current room failed:', e);
    return false;
  }
  if (!currentRoom || currentRoom.id !== roomId) return false;

  if (msgs && msgs.length) {
    oldestMsgTs = msgs[0].created_at;
    noMoreMessages = msgs.length < 100;
  } else {
    oldestMsgTs = null;
    noMoreMessages = true;
  }
  loadingOlder = false;
  renderMessages(msgs);
  if (options && options.scroll) scrollToBottom(true);

  try {
    const payload = JSON.stringify({ savedAt: Date.now(), messages: msgs });
    if (payload.length < 900000) {
      const snapshotKey = `chatroom_messages_snapshot_v1_${roomId}`;
      localStorage.setItem(snapshotKey, payload);
      try {
        const bridge = window.top?.AppSharedData || window.AppSharedData;
        bridge?.put?.(snapshotKey, payload);
      } catch(e) {}
    }
  } catch(e) {}
  return true;
}
window.refreshCurrentChatroomFromServer = refreshCurrentChatroomFromServer;

async function loadOlderMessages() {
  if (!currentRoom || noMoreMessages || loadingOlder || !oldestMsgTs) return;
  loadingOlder = true;
  // 记住当前滚动高度以便加载后保持位置
  const prevHeight = messagesEl.scrollHeight;
  const msgs = await api(`/rooms/${currentRoom.id}/messages?limit=50&before=${oldestMsgTs}`);
  if (!msgs || !msgs.length) {
    noMoreMessages = true;
    loadingOlder = false;
    return;
  }
  if (msgs.length < 50) noMoreMessages = true;
  oldestMsgTs = msgs[0].created_at;
  // 将旧消息插入到顶部
  const fragment = document.createDocumentFragment();
  msgs.forEach(m => {
    if (m.id) crMessagesById[m.id] = m;
    const div = document.createElement('div');
    div.innerHTML = msgHTML(m);
    fragment.appendChild(div.firstElementChild);
  });
  messagesEl.prepend(fragment);
  // 保持滚动位置
  messagesEl.scrollTop = messagesEl.scrollHeight - prevHeight;
  loadingOlder = false;
}

function resetChatSearch() {
  chatSearchKeyword = '';
  if (chatSearchInput) chatSearchInput.value = '';
  if (chatSearchMeta) chatSearchMeta.textContent = currentRoom ? '输入关键词后回车' : '先选择一个聊天室';
  if (chatSearchResults) chatSearchResults.innerHTML = '';
  chatSearchPanel?.classList.remove('show');
}

function openChatSearch() {
  if (!currentRoom) {
    toast('先选择一个聊天室');
    return;
  }
  chatSearchPanel?.classList.add('show');
  setTimeout(() => chatSearchInput?.focus(), 0);
}

function closeChatSearch() {
  chatSearchPanel?.classList.remove('show');
}

function crMsgSelector(msgId) {
  const safeId = window.CSS?.escape ? CSS.escape(String(msgId)) : String(msgId).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  return `[data-msg-id="${safeId}"]`;
}

const CR_LEGACY_COMMAND_SYSTEM_NOTICE_RE = /^(?:🎵\s+.+点了一首|(?:⏰|📅|👀)\s*【[^】]+】设定了|📷\s+.+查看了监控|📊\s+.+查看了用户动态|💾\s+.+记住了)/;

function crIsAiSender(sender) {
  return sender === 'aion' || sender === 'connor';
}

function crSystemNoticeAfterMsgId(m) {
  if (!m || m.sender !== 'system' || !Array.isArray(m.attachments)) return '';
  const marker = m.attachments.find(a => a && typeof a === 'object' && a.type === 'system_notice_order' && a.after_msg_id);
  return marker ? String(marker.after_msg_id) : '';
}

function crIsLegacyCommandSystemNotice(m) {
  if (!m || m.sender !== 'system' || crSystemNoticeAfterMsgId(m)) return false;
  return CR_LEGACY_COMMAND_SYSTEM_NOTICE_RE.test((m.content || '').trim());
}

function crPreviousDisplaySender(msgs, idx) {
  for (let i = idx - 1; i >= 0; i--) {
    const sender = msgs[i]?.sender;
    if (sender && sender !== 'system') return sender;
  }
  return '';
}

function crMessagesForDisplay(msgs) {
  const out = [];
  const pendingById = new Map();
  let pendingLegacyNotices = [];
  const list = msgs || [];
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
    const afterMsgId = crSystemNoticeAfterMsgId(m);
    if (afterMsgId && indexById.has(afterMsgId) && idx < indexById.get(afterMsgId)) {
      if (!pendingById.has(afterMsgId)) pendingById.set(afterMsgId, []);
      pendingById.get(afterMsgId).push(m);
      continue;
    }

    if (crIsLegacyCommandSystemNotice(m) && !crIsAiSender(crPreviousDisplaySender(list, idx))) {
      pendingLegacyNotices.push(m);
      continue;
    }

    if (pendingLegacyNotices.length && !crIsAiSender(m?.sender)) {
      out.push(...pendingLegacyNotices);
      pendingLegacyNotices = [];
    }
    out.push(m);
    if (crIsAiSender(m?.sender)) appendPendingFor(m.id);
  }
  if (pendingLegacyNotices.length) out.push(...pendingLegacyNotices);
  for (const pending of pendingById.values()) out.push(...pending);
  return out;
}

function crSystemNoticeRowAfterMsgId(row) {
  if (!row?.classList?.contains('system-event-msg')) return false;
  return row.getAttribute('data-after-msg-id') || '';
}

function crMovePrecedingRelatedSystemNoticesAfter(row, msgId) {
  if (!row) return;
  const notices = [];
  let prev = row.previousElementSibling;
  while (prev?.classList?.contains('system-event-msg') && crSystemNoticeRowAfterMsgId(prev) === msgId) {
    notices.unshift(prev);
    prev = prev.previousElementSibling;
  }
  let anchor = row;
  notices.forEach(notice => {
    anchor.insertAdjacentElement('afterend', notice);
    anchor = notice;
  });
}

function crSearchSnippet(content, keyword) {
  const text = String(content || '').replace(/\s+/g, ' ').trim();
  if (!text) return '';
  const lower = text.toLocaleLowerCase();
  const needle = String(keyword || '').toLocaleLowerCase();
  const idx = needle ? lower.indexOf(needle) : -1;
  if (idx < 0) return text.length > 96 ? text.slice(0, 96) + '...' : text;
  const start = Math.max(0, idx - 28);
  const end = Math.min(text.length, idx + keyword.length + 56);
  return `${start > 0 ? '...' : ''}${text.slice(start, end)}${end < text.length ? '...' : ''}`;
}

function crHighlightFirst(text, keyword) {
  const value = String(text || '');
  const needle = String(keyword || '');
  if (!needle) return esc(value);
  const idx = value.toLocaleLowerCase().indexOf(needle.toLocaleLowerCase());
  if (idx < 0) return esc(value);
  return esc(value.slice(0, idx)) + '<mark>' + esc(value.slice(idx, idx + needle.length)) + '</mark>' + esc(value.slice(idx + needle.length));
}

function renderChatSearchResults(items) {
  if (!chatSearchResults) return;
  if (!items.length) {
    chatSearchResults.innerHTML = '<div class="chat-search-meta">没有匹配的消息</div>';
    return;
  }
  chatSearchResults.innerHTML = items.map(m => {
    const snippet = crSearchSnippet(m.content || '', chatSearchKeyword);
    const sender = esc(crName(m.sender || 'user'));
    const time = esc(timeStr(m.created_at));
    return `<button class="chat-search-result" type="button" onclick="jumpToChatSearchResult('${esc(m.id)}')">
      <div class="csr-head"><span>${sender}</span><span>${time}</span></div>
      <div class="csr-snippet">${crHighlightFirst(snippet, chatSearchKeyword)}</div>
    </button>`;
  }).join('');
}

async function runChatSearch(e) {
  e?.preventDefault();
  if (!currentRoom) {
    toast('先选择一个聊天室');
    return;
  }
  const keyword = (chatSearchInput?.value || '').trim();
  chatSearchKeyword = keyword;
  if (!keyword) {
    if (chatSearchMeta) chatSearchMeta.textContent = '输入关键词后回车';
    if (chatSearchResults) chatSearchResults.innerHTML = '';
    return;
  }
  if (chatSearchMeta) chatSearchMeta.textContent = '搜索中...';
  try {
    const result = await api(`/rooms/${currentRoom.id}/messages/search?q=${encodeURIComponent(keyword)}&limit=50`);
    const items = Array.isArray(result?.items) ? result.items : [];
    if (chatSearchMeta) chatSearchMeta.textContent = items.length ? `找到 ${items.length} 条，点击可跳转` : '没有匹配的消息';
    renderChatSearchResults(items);
  } catch (err) {
    console.error('搜索聊天记录失败:', err);
    if (chatSearchMeta) chatSearchMeta.textContent = '搜索失败';
  }
}

async function jumpToChatSearchResult(msgId) {
  if (!currentRoom || !msgId) return;
  let row = messagesEl.querySelector(crMsgSelector(msgId));
  if (!row) {
    if (chatSearchMeta) chatSearchMeta.textContent = '正在定位...';
    try {
      const result = await api(`/rooms/${currentRoom.id}/messages/around/${encodeURIComponent(msgId)}?before_count=80&after_count=30`);
      const msgs = Array.isArray(result?.messages) ? result.messages : [];
      if (!result?.ok || !msgs.length) {
        toast('这条消息可能已经被删除');
        return;
      }
      oldestMsgTs = msgs[0]?.created_at || null;
      noMoreMessages = !result.has_more_older;
      loadingOlder = false;
      renderMessages(msgs);
      row = messagesEl.querySelector(crMsgSelector(msgId));
    } catch (err) {
      console.error('定位聊天记录失败:', err);
      toast('定位失败');
      return;
    }
  }
  closeChatSearch();
  await crCenterSearchResult(msgId);
  row = messagesEl.querySelector(crMsgSelector(msgId));
  row?.classList.add('search-hit');
  setTimeout(() => row?.classList.remove('search-hit'), 1600);
}

let crSearchJumpToken = 0;

async function crCenterSearchResult(msgId) {
  const token = ++crSearchJumpToken;
  const nextFrame = () => new Promise(resolve => requestAnimationFrame(resolve));
  const center = () => {
    if (token !== crSearchJumpToken) return;
    const row = messagesEl.querySelector(crMsgSelector(msgId));
    if (!row) return;
    const listRect = messagesEl.getBoundingClientRect();
    const rowRect = row.getBoundingClientRect();
    const centeredTop = messagesEl.scrollTop
      + rowRect.top - listRect.top
      - (messagesEl.clientHeight - rowRect.height) / 2;
    messagesEl.scrollTop = Math.max(0, centeredTop);
  };

  // Let the freshly rendered message window settle before the first alignment.
  await nextFrame();
  await nextFrame();
  center();

  const pendingImages = Array.from(messagesEl.querySelectorAll('img:not([complete])'))
    .filter(img => !img.complete);
  const imageLayoutSettled = Promise.all(pendingImages.map(img => new Promise(resolve => {
    img.addEventListener('load', resolve, { once: true });
    img.addEventListener('error', resolve, { once: true });
  })));
  const fontLayoutSettled = document.fonts?.ready || Promise.resolve();

  await Promise.race([
    Promise.all([imageLayoutSettled, fontLayoutSettled]),
    new Promise(resolve => setTimeout(resolve, 700)),
  ]);
  await nextFrame();
  center();
}

if (chatSearchForm) chatSearchForm.addEventListener('submit', runChatSearch);

function renderMessages(msgs) {
  crMessagesById = {};
  if (!msgs || !msgs.length) {
    messagesEl.innerHTML = `
      <div class="empty-state">
        <div class="icon">${currentRoom.type === 'connor_1v1' ? '🤖' : '👥'}</div>
        <div>${currentRoom.type === 'connor_1v1' ? `和 ${esc(crConnorName)} 开始私聊吧` : '三人群聊，开始吧'}</div>
      </div>`;
    return;
  }
  msgs.forEach(m => { if (m.id) crMessagesById[m.id] = m; });
  messagesEl.innerHTML = crMessagesForDisplay(msgs).map(m => msgHTML(m)).join('');
  crApplySongGenIndicator();
}

function crMsgMenuHtml(sender, msgId, opts = {}) {
  if (!msgId) return '';
  const actionHtml = opts.deleteOnly || sender === 'system'
    ? ''
    : sender === 'user'
    ? `<button onclick="editChatroomMsg('${msgId}');closeMsgMenus()">\u7f16\u8f91</button>`
    : `<button onclick="regenerateChatroomMsg('${msgId}');closeMsgMenus()">\u91cd\u65b0\u751f\u6210</button>`;
  return `
    <div class="msg-menu-wrap">
      <button class="msg-menu-btn" onclick="toggleMsgMenu(event)">\u22ef</button>
      <div class="msg-menu-dropdown">
        ${actionHtml}
        <button class="danger" onclick="deleteMsg('${msgId}', this)">\u5220\u9664</button>
      </div>
    </div>`;
}

function crCanRateAiMsg(msg) {
  if (!msg?.id || !currentRoom) return false;
  if (currentRoom.type === 'group') return msg.sender === 'aion' || msg.sender === 'connor';
  if (currentRoom.type === 'connor_1v1') return msg.sender === 'connor';
  return false;
}

function crMsgFeedbackHtml(msg) {
  if (!crCanRateAiMsg(msg)) return '';
  const likeActive = msg.ai_feedback_rating === 'like' ? 'active' : '';
  const dislikeActive = msg.ai_feedback_rating === 'dislike' ? 'active' : '';
  return `<span class="msg-feedback-actions">
    <button class="msg-feedback-btn ${likeActive}" onclick="openCrMsgFeedback(event,'${msg.id}','like')" title="喜欢这条回复">👍</button>
    <button class="msg-feedback-btn ${dislikeActive}" onclick="openCrMsgFeedback(event,'${msg.id}','dislike')" title="不喜欢这条回复">👎</button>
    ${msg.reasoning_content ? `<button class="msg-feedback-btn msg-reasoning-btn" onclick="openCrMsgReasoning(event,'${msg.id}')" title="查看思考过程">💭</button>` : ''}
  </span>`;
}

function crMsgSenderLineHtml(sender, name, msgId, msg = null, opts = {}) {
  const menuHtml = crMsgMenuHtml(sender, msgId, { deleteOnly: opts.deleteOnly });
  const feedbackHtml = crMsgFeedbackHtml(msg || { id: msgId, sender });
  const ttsHtml = opts.tts && sender !== 'user' && msgId
    ? `<button class="tts-replay-btn" onclick="crReplayTTS('${msgId}', this)" title="重听语音">🔊</button>`
    : '';
  const memoryHtml = opts.memory && msgId
    ? `<button class="memory-record-hint" onclick="event.stopPropagation();crShowMemoryRecordCard('${msgId}')" title="已记录到记忆库">💡</button>`
    : '';
  if (sender !== 'user') {
    return `<div class="sender-line"><span class="sender-label ${sender}">${esc(name)}</span>${menuHtml}${feedbackHtml}${ttsHtml}${memoryHtml}</div>`;
  }
  return menuHtml ? `<div class="sender-line user-line">${menuHtml}</div>` : '';
}

function crEnsureMsgMenu(row, sender, msgId) {
  if (!row || !msgId) return;
  const content = row.querySelector('.msg-content');
  if (!content) return;
  const senderLines = Array.from(content.querySelectorAll('.sender-line'));
  if (senderLines.length) {
    const line = senderLines[0];
    if (sender !== 'user' && !line.querySelector('.tts-replay-btn')) {
      line.insertAdjacentHTML('beforeend', `<button class="tts-replay-btn" onclick="crReplayTTS('${msgId}', this)" title="重听语音">🔊</button>`);
    }
    if (crMemoryRecordMsgIds.has(msgId) && !line.querySelector('.memory-record-hint')) {
      line.insertAdjacentHTML('beforeend', `<button class="memory-record-hint" onclick="event.stopPropagation();crShowMemoryRecordCard('${msgId}')" title="已记录到记忆库">💡</button>`);
    }
    return;
  }
  const name = crName(sender);
  const senderLine = crMsgSenderLineHtml(sender, name, msgId, { id: msgId, sender }, {
    tts: sender !== 'user',
    memory: crMemoryRecordMsgIds.has(msgId),
  });
  if (!senderLine) return;
  const directLabel = Array.from(content.querySelectorAll('.sender-label')).find(Boolean);
  if (directLabel) {
    directLabel.insertAdjacentHTML('beforebegin', senderLine);
    directLabel.remove();
  } else {
    content.insertAdjacentHTML('afterbegin', senderLine);
  }
}

const CR_STRUCTURED_LINE_RE = /^\s*(```|[-*+]\s+|\d+[.)]\s+|[>|#]|\|)/;

function crBubbleParts(raw, isUser = false) {
  const text = raw || '';
  // 转账标签前后强制换行，确保卡片独占一个气泡
  const splitText = text.replace(/(\[转账(?:给[^\uff1a:]+?)?[：:]\s*-?\d+(?:\.\d+)?\s*元\])/g, isUser ? '\n$1\n' : '\n\n$1\n\n');
  if (isUser) return splitText.split(/\n+/).filter(p => p.trim());

  const singleLineParts = splitText.split(/\n+/).map(p => p.trim()).filter(Boolean);
  if (singleLineParts.length < 2) return singleLineParts;
  if (singleLineParts.some(p => CR_STRUCTURED_LINE_RE.test(p))) return splitText.split(/\n{2,}/).filter(p => p.trim());
  return singleLineParts;
}

function crMessageContentItems(raw, isUser = false) {
  const items = [];
  const monologueRe = /\[心里嘀咕[：:]\s*([^\]]+?)\]/g;
  for (const part of crBubbleParts(raw, isUser)) {
    let last = 0;
    let match;
    monologueRe.lastIndex = 0;
    while ((match = monologueRe.exec(part)) !== null) {
      const before = part.slice(last, match.index).trim();
      if (before) items.push({ type: 'bubble', text: before });
      const thought = (match[1] || '').trim();
      if (thought) items.push({ type: 'monologue', text: thought });
      last = monologueRe.lastIndex;
    }
    const tail = part.slice(last).trim();
    if (tail) items.push({ type: 'bubble', text: tail });
  }
  return items;
}

function crBubbleUnitHtml({ sender, name, avatar, msgId, msg, html, showHeader, includeActions, deleteOnly = false, preBubbleHtml = '' }) {
  const senderLine = showHeader
    ? crMsgSenderLineHtml(sender, name, msgId, msg, {
        tts: includeActions,
        memory: includeActions && crMemoryRecordMsgIds.has(msgId),
        deleteOnly,
      })
    : '';
  const emptyClass = String(html || '').trim() ? '' : ' empty-message';
  return `<div class="message-unit ${sender}${emptyClass}">
    <div class="msg-avatar-col"><img class="avatar" src="${avatar}" alt="${esc(name)}"></div>
    <div class="unit-content">
      ${senderLine}
      ${preBubbleHtml}
      <div class="bubble">${html}</div>
    </div>
  </div>`;
}

function crRenderMessageItems(items, { sender, name, avatar, msgId, msg, fmt, isUser }) {
  let firstBubble = true;
  let leadingMonologues = [];
  const htmlParts = [];
  items.forEach(item => {
    if (item.type === 'monologue') {
      if (firstBubble) {
        leadingMonologues.push(item.text);
      } else {
        htmlParts.push(`<div class="inner-monologue-line">${esc(item.text)}</div>`);
      }
      return;
    }
    const showHeader = firstBubble;
    const preBubbleHtml = showHeader && leadingMonologues.length
      ? leadingMonologues.map(text => `<div class="inner-monologue-line">${esc(text)}</div>`).join('')
      : '';
    firstBubble = false;
    leadingMonologues = [];
    htmlParts.push(crBubbleUnitHtml({
      sender, name, avatar, msgId, msg,
      html: fmt(item.text),
      showHeader,
      includeActions: !isUser && showHeader,
      preBubbleHtml,
    }));
  });
  if (leadingMonologues.length) {
    htmlParts.push(...leadingMonologues.map(text => `<div class="inner-monologue-line">${esc(text)}</div>`));
  }
  return htmlParts.join('');
}

function crBandVibrationNoteHtml(atts) {
  const note = (Array.isArray(atts) ? atts : []).find(item =>
    item && typeof item === 'object' && item.type === 'band_vibration'
  );
  if (!note) return '';
  const fallback = note.note
    ? `${note.pattern === 'call' ? '手环呼唤：' : '手环轻震：'}${note.note}`
    : (note.pattern === 'call'
      ? '手环震动：紧急呼叫！'
      : '手环震动：轻轻想了你一下');
  return `<div class="band-vibration-line">${esc(note.label || fallback)}</div>`;
}

function msgHTML(m) {
  const sender = m.sender || 'user';

  // 系统事件消息（点歌、闹钟等）
  if (sender === 'system') {
    const msgId = m.id || '';
    const afterMsgId = crSystemNoticeAfterMsgId(m);
    const afterAttr = afterMsgId ? ` data-after-msg-id="${esc(afterMsgId)}"` : '';
    const snapshotHtml = window.MonitorCameraSnapshot
      ? window.MonitorCameraSnapshot.renderMonitorCameraSnapshot(
          m.attachments,
          {
            escapeHtml: esc,
            imageAttrs: imageInteractionAttrs(),
          },
        )
      : '';
    return `<div class="system-event-msg" data-msg-id="${msgId}"${afterAttr}>
      <div class="system-event-line">
        <span class="system-event-text">${esc(m.content || '')}</span>
        ${crMsgMenuHtml('system', msgId)}
      </div>
      ${snapshotHtml}
    </div>`;
  }

  const name = crName(sender);
  const avatar = AVATARS[sender] || AVATARS.user;
  const time = timeStr(m.created_at);

  // 用户消息按单换行拆；AI优先按空行拆，兼容 Gemini CLI 的普通单换行段落。
  const isUser = sender === 'user';
  const originalRaw = m.content || '';
  const raw = crStripWishFulfillmentMarker(originalRaw);
  const messageAttachments = crWithWishFallbackAttachments(m);

  // 判断是否为纯语音消息（只有语音附件，content 是转写文本或为空）
  const hasVoiceAtt = messageAttachments.some(a => typeof a === 'object' && a.type === 'voice');
  const isVoiceOnly = hasVoiceAtt && (!raw || messageAttachments.some(a => typeof a === 'object' && a.type === 'voice' && a.transcript === raw));
  const hasWishFulfillmentAtt = messageAttachments.some(a => typeof a === 'object' && a.type === 'wish_fulfillment');
  const hasDateSummaryAtt = messageAttachments.some(a => typeof a === 'object' && a.type === 'date_summary');

  // AI 消息使用 escWithImages 解析 [[image:...]] 和转账卡片，用户消息也渲染转账卡片
  const fmt = isUser ? escWithTransfer : escWithImages;
  // 渲染附件图片、语音和结构化卡片
  const toyHtml = renderToyAttachments(messageAttachments);
  const attHtml = renderAttachments(messageAttachments);
  const bandVibrationHtml = crBandVibrationNoteHtml(messageAttachments);
  let bubblesHtml = '';
  if (isVoiceOnly) {
    bubblesHtml = `<div class="message-stack">${crBubbleUnitHtml({
      sender, name, avatar, msgId: m.id || '', msg: m,
      html: '',
      showHeader: true,
      includeActions: false,
      deleteOnly: true,
      preBubbleHtml: attHtml,
    })}</div>`;
  } else if (!hasDateSummaryAtt && (!hasWishFulfillmentAtt || !isUser)) {
    const items = crMessageContentItems(raw, isUser);
    bubblesHtml = items.length
      ? `<div class="message-stack">${crRenderMessageItems(items, { sender, name, avatar, msgId: m.id || '', msg: m, fmt, isUser })}</div>`
      : `<div class="message-stack">${crBubbleUnitHtml({
          sender, name, avatar, msgId: m.id || '', msg: m,
          html: '',
          showHeader: true,
          includeActions: true,
        })}</div>`;
  }

  const msgId = m.id || '';

  return `
    <div class="message-row ${sender}" data-msg-id="${msgId}">
      <div class="msg-body">
        <div class="msg-content">
          ${hasWishFulfillmentAtt || hasDateSummaryAtt ? attHtml : ''}
          ${bubblesHtml}
          ${toyHtml}
          ${hasWishFulfillmentAtt || hasDateSummaryAtt || isVoiceOnly ? '' : attHtml}
          ${bandVibrationHtml}
        </div>
      </div>
      <div class="message-meta">${time}</div>
    </div>`;
}

let crMsgFeedbackPopover = null;
let crMsgReasoningPopover = null;

function closeCrMsgReasoningPopover() {
  if (crMsgReasoningPopover) {
    crMsgReasoningPopover.remove();
    crMsgReasoningPopover = null;
  }
}

function openCrMsgReasoning(ev, msgId) {
  ev?.stopPropagation?.();
  closeCrMsgFeedbackPopover();
  closeCrMsgReasoningPopover();
  const reasoning = crMessagesById[msgId]?.reasoning_content || '';
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
  crMsgReasoningPopover = pop;
  const trigger = ev?.currentTarget;
  const rect = trigger?.getBoundingClientRect?.();
  const messageRect = trigger?.closest?.('.message-row')?.getBoundingClientRect?.() || rect;
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

function closeCrMsgFeedbackPopover() {
  if (crMsgFeedbackPopover) {
    crMsgFeedbackPopover.remove();
    crMsgFeedbackPopover = null;
  }
}

function openCrMsgFeedback(ev, msgId, rating) {
  ev?.stopPropagation?.();
  closeMsgMenus();
  closeCrMsgFeedbackPopover();
  const msg = crMessagesById[msgId] || {};
  const label = rating === 'like' ? '喜欢的原因' : '不喜欢的原因';
  const existing = msg.ai_feedback_rating === rating ? (msg.ai_feedback_reason || '') : '';
  const pop = document.createElement('div');
  pop.className = 'msg-feedback-popover';
  pop.innerHTML = `
    <div class="msg-feedback-title">${label}</div>
    <textarea id="crMsgFeedbackReason" rows="3" maxlength="600" placeholder="写一点具体原因，之后复盘会用到">${esc(existing)}</textarea>
    <div class="msg-feedback-footer">
      <button type="button" class="msg-feedback-cancel" onclick="closeCrMsgFeedbackPopover()">取消</button>
      <button type="button" class="msg-feedback-submit" onclick="submitCrMsgFeedback('${msgId}','${rating}')">确认</button>
    </div>`;
  document.body.appendChild(pop);
  crMsgFeedbackPopover = pop;

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

async function submitCrMsgFeedback(msgId, rating) {
  const reason = document.getElementById('crMsgFeedbackReason')?.value.trim() || '';
  if (!reason) {
    toast('先写一点原因');
    return;
  }
  try {
    const res = await api(`/messages/${encodeURIComponent(msgId)}/feedback`, {
      method: 'PATCH',
      body: JSON.stringify({ rating, reason }),
    });
    if (res.detail || res.error) {
      toast(res.detail || res.error || '反馈保存失败');
      return;
    }
    closeCrMsgFeedbackPopover();
    toast('反馈已记录');
  } catch (e) {
    console.error('反馈保存失败:', e);
    toast('反馈保存失败');
  }
}

/* ── 消息菜单 ── */
function toggleMsgMenu(e) {
  e.stopPropagation();
  const dropdown = e.currentTarget.nextElementSibling;
  // 关闭所有其他下拉
  document.querySelectorAll('.msg-menu-dropdown.show').forEach(d => { if (d !== dropdown) d.classList.remove('show'); });
  dropdown.classList.toggle('show');
}

function closeMsgMenus() {
  document.querySelectorAll('.msg-menu-dropdown.show').forEach(d => d.classList.remove('show'));
}

async function deleteMsg(msgId, btnEl) {
  try {
    await fetch(`${API}/messages/${msgId}`, { method: 'DELETE' });
    delete crMessagesById[msgId];
    const row = document.querySelector(`[data-msg-id="${msgId}"]`);
    if (row) row.remove();
  } catch (e) { console.error('删除失败', e); }
}

function removeRowsAfter(row, includeSelf = false) {
  if (!row) return;
  let n = includeSelf ? row : row.nextElementSibling;
  while (n) {
    const next = n.nextElementSibling;
    const id = n.getAttribute?.('data-msg-id');
    if (id) delete crMessagesById[id];
    n.remove();
    n = next;
  }
}

async function consumeChatroomSSE(resp) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop();
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      try { handleSSE(JSON.parse(line.slice(6))); } catch {}
    }
  }
}

function editChatroomMsg(msgId) {
  const msg = crMessagesById[msgId];
  if (!msg || msg.sender !== 'user' || isSending || isAiChatting) return;
  const row = document.querySelector(`[data-msg-id="${msgId}"]`);
  const bubble = row?.querySelector('.bubble');
  if (!bubble) return;
  row.classList.add('editing');
  bubble.innerHTML = `
    <textarea class="edit-textarea" id="edit_${msgId}"></textarea>
    <div class="edit-actions">
      <button class="edit-cancel" onclick="cancelChatroomEdit()">取消</button>
      <button class="edit-save" onclick="saveChatroomEdit('${msgId}')">确认</button>
    </div>`;
  const ta = document.getElementById(`edit_${msgId}`);
  ta.value = msg.content || '';
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 160) + 'px';
  ta.oninput = function() {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 160) + 'px';
  };
  ta.focus();
}

function cancelChatroomEdit() {
  loadMessages();
}

async function saveChatroomEdit(msgId) {
  const ta = document.getElementById(`edit_${msgId}`);
  const msg = crMessagesById[msgId];
  if (!ta || !msg || isSending || isAiChatting) return;
  const content = ta.value.trim();
  if (!content) { toast('内容不能为空'); return; }

  isSending = true;
  sendBtn.disabled = true;
  msg.content = content;
  const row = document.querySelector(`[data-msg-id="${msgId}"]`);
  removeRowsAfter(row, false);
  if (row) {
    const div = document.createElement('div');
    div.innerHTML = msgHTML(msg);
    row.replaceWith(div.firstElementChild);
  }

  try {
    const resp = await fetch(`${API}/messages/${msgId}/edit-resend`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        content,
        model: chatroomModel,
        connor_model: chatroomConnorModel,
        tts_enabled: crTtsEnabled,
        tts_aion_voice: crTtsAionVoice,
        tts_connor_voice: crTtsConnorVoice,
        whisper_mode: crWhisperMode,
      }),
    });
    await consumeChatroomSSE(resp);
  } catch (err) {
    toast('编辑重发失败: ' + err.message);
    await loadMessages();
  } finally {
    isSending = false;
    sendBtn.disabled = false;
    endStreamingBubble();
    crRefocusComposerAfterSend();
  }
}

async function regenerateChatroomMsg(msgId) {
  const msg = crMessagesById[msgId];
  if (!msg || msg.sender === 'user' || isSending || isAiChatting) return;
  isSending = true;
  sendBtn.disabled = true;
  const row = document.querySelector(`[data-msg-id="${msgId}"]`);
  removeRowsAfter(row, true);
  try {
    const resp = await fetch(`${API}/messages/${msgId}/regenerate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: chatroomModel,
        connor_model: chatroomConnorModel,
        tts_enabled: crTtsEnabled,
        tts_aion_voice: crTtsAionVoice,
        tts_connor_voice: crTtsConnorVoice,
        whisper_mode: crWhisperMode,
      }),
    });
    await consumeChatroomSSE(resp);
  } catch (err) {
    toast('重新生成失败: ' + err.message);
    await loadMessages();
  } finally {
    isSending = false;
    sendBtn.disabled = false;
    endStreamingBubble();
  }
}

// 点击空白处关闭下拉菜单
document.addEventListener('click', (e) => {
  if (e.target?.closest?.('.msg-feedback-popover, .msg-reasoning-popover, .msg-feedback-btn')) return;
  closeCrMsgFeedbackPopover();
  closeCrMsgReasoningPopover();
  document.querySelectorAll('.msg-menu-dropdown.show').forEach(d => d.classList.remove('show'));
});

function appendMessage(m) {
  if (m?.id) crMessagesById[m.id] = m;
  // 移除空状态
  const empty = messagesEl.querySelector('.empty-state');
  if (empty) empty.remove();
  // 移除 typing 指示器
  const typing = messagesEl.querySelector('.typing-indicator');
  if (typing) typing.remove();

  const div = document.createElement('div');
  div.innerHTML = msgHTML(m);
  const row = div.firstElementChild;
  messagesEl.appendChild(row);
  if (m?.id === crSongGenMsgId) crApplySongGenIndicator();
  if (crHasGeneratedSongAttachment(m)) crDismissSongGenIndicator({ room_id: m.room_id });
  if (crIsAiSender(m?.sender)) crMovePrecedingRelatedSystemNoticesAfter(row, m.id || '');
  if (m?.id && crMemoryRecordMsgIds.has(m.id)) crApplyMemoryHint(m.id);
  scrollToBottom(m.sender === 'user');
  return row;
}

function reconcileLocalUserEcho(msg) {
  if (!msg || msg.sender !== 'user') return false;
  if (msg.id) crMessagesById[msg.id] = msg;
  const localRow = messagesEl.querySelector('.message-row.user[data-local-echo="1"]');
  if (!localRow) return false;

  const div = document.createElement('div');
  div.innerHTML = msgHTML(msg);
  localRow.replaceWith(div.firstElementChild);
  return true;
}

function appendTyping(who) {
  const existing = messagesEl.querySelector('.typing-indicator');
  if (existing) existing.remove();
  const div = document.createElement('div');
  div.className = 'typing-indicator';
  div.textContent = `${who} 回复中...`;
  messagesEl.appendChild(div);
  scrollToBottom();
}

function updateTypingStatus(who, statusText) {
  const indicator = messagesEl.querySelector('.typing-indicator');
  if (indicator) {
    indicator.textContent = `${who} ${statusText}`;
  } else {
    appendTyping(who);
    const el = messagesEl.querySelector('.typing-indicator');
    if (el) el.textContent = `${who} ${statusText}`;
  }
}

function appendAiChatStatus(text) {
  const existing = messagesEl.querySelector('.ai-chat-status');
  if (existing) existing.remove();
  const div = document.createElement('div');
  div.className = 'ai-chat-status';
  div.textContent = text;
  messagesEl.appendChild(div);
  scrollToBottom();
}

function removeAiChatStatus() {
  const existing = messagesEl.querySelector('.ai-chat-status');
  if (existing) existing.remove();
}

let crSongGenRoomId = null;
let crSongGenMsgId = null;
let crSongGenSafetyTimer = null;

function crHasGeneratedSongAttachment(msg) {
  return Array.isArray(msg?.attachments) && msg.attachments.some(item => item && typeof item === 'object' && item.type === 'generated_song');
}

function crSongGenIndicatorHtml() {
  return `歌曲谱写中....<span class="sg-dots"><span></span><span></span><span></span></span>`;
}

function crDismissSongGenIndicator(data = null) {
  const roomId = data?.room_id || crSongGenRoomId;
  if (crSongGenRoomId && roomId && roomId !== crSongGenRoomId) return;
  crSongGenRoomId = null;
  crSongGenMsgId = null;
  if (crSongGenSafetyTimer) {
    clearTimeout(crSongGenSafetyTimer);
    crSongGenSafetyTimer = null;
  }
  const el = document.getElementById('cr_song_gen_loading');
  if (el) el.remove();
}

function crApplySongGenIndicator() {
  if (!currentRoom || !crSongGenRoomId || currentRoom.id !== crSongGenRoomId || !crSongGenMsgId) return;
  const row = messagesEl.querySelector(crMsgSelector(crSongGenMsgId));
  if (!row || row.querySelector('.song-gen-indicator')) return;
  const indicator = document.createElement('div');
  indicator.className = 'song-gen-indicator';
  indicator.id = 'cr_song_gen_loading';
  indicator.innerHTML = crSongGenIndicatorHtml();
  const msgContent = row.querySelector('.msg-content');
  (msgContent || row).appendChild(indicator);
  scrollToBottom();
}

function crHandleSongGenStart(data) {
  if (!data?.room_id) return;
  crDismissSongGenIndicator();
  crSongGenRoomId = data.room_id;
  crSongGenMsgId = data.msg_id || null;
  crApplySongGenIndicator();
  crSongGenSafetyTimer = setTimeout(() => crDismissSongGenIndicator({ room_id: data.room_id }), 300000);
}

// ── 流式消息累积 ──
let streamingBubble = null;
let streamingText = '';
let pendingStreamSender = null;
let pendingStreamId = null;

function startStreamingBubble(sender, id) {
  streamingText = '';
  const name = crName(sender);
  const avatar = AVATARS[sender] || AVATARS.user;
  const senderLine = crMsgSenderLineHtml(sender, name, id, { id, sender }, { tts: false });

  // 移除 typing
  const typing = messagesEl.querySelector('.typing-indicator');
  if (typing) typing.remove();

  const row = document.createElement('div');
  row.className = `message-row ${sender}`;
  row.id = `streaming-${id}`;
  row.innerHTML = `
    <div class="msg-body">
      <div class="msg-content">
        <div class="message-stack">
          <div class="message-unit ${sender}">
            <div class="msg-avatar-col"><img class="avatar" src="${avatar}" alt="${esc(name)}"></div>
            <div class="unit-content">
              ${senderLine}
              <div class="bubble"></div>
            </div>
          </div>
        </div>
      </div>
    </div>
    <div class="message-meta">${timeStr(Date.now() / 1000)}</div>`;
  messagesEl.appendChild(row);
  streamingBubble = row.querySelector('.bubble');
  scrollToBottom();
}

function feedStreamingChunk(text) {
  if (!streamingBubble) return;
  streamingText += text;
  streamingBubble.textContent = window.ScheduleCommandFilter
    ? window.ScheduleCommandFilter.stripScheduleCommands(streamingText)
    : streamingText;
  scrollToBottom();
}

function endStreamingBubble(messageOrAttachments) {
  // 先获取流式行的引用（后面 replaceChild 可能破坏 streamingBubble 的 DOM 位置）
  const streamRow = streamingBubble ? streamingBubble.closest('.message-row') : null;
  const finalMsg = messageOrAttachments && !Array.isArray(messageOrAttachments) ? messageOrAttachments : null;
  const attachments = finalMsg ? finalMsg.attachments : messageOrAttachments;

  if (finalMsg?.id) crMessagesById[finalMsg.id] = finalMsg;
  if (finalMsg?.id && streamRow) {
    const div = document.createElement('div');
    div.innerHTML = msgHTML(finalMsg);
    const renderedRow = div.firstElementChild;
    if (renderedRow) {
      streamRow.replaceWith(renderedRow);
      if (crIsAiSender(finalMsg.sender)) crMovePrecedingRelatedSystemNoticesAfter(renderedRow, finalMsg.id || '');
      if (crMemoryRecordMsgIds.has(finalMsg.id)) crApplyMemoryHint(finalMsg.id);
      crShowToyCapsule(finalMsg.id, crToyCommandsFromAttachments(finalMsg.attachments));
    }
    streamingBubble = null;
    streamingText = '';
    return;
  }

  // 流结束后，按段落拆分成多个气泡，并解析 [[image:...]] 和转账卡片
  if (streamingBubble && streamingText) {
    const sender = streamRow?.classList.contains('connor') ? 'connor' : (streamRow?.classList.contains('aion') ? 'aion' : 'user');
    const name = crName(sender);
    const avatar = AVATARS[sender] || AVATARS.user;
    const msgId = streamRow?.id?.startsWith('streaming-') ? streamRow.id.replace('streaming-', '') : '';
    const stack = streamingBubble.closest('.message-stack') || streamingBubble.closest('.msg-content');
    const items = crMessageContentItems(streamingText, sender === 'user');
    if (stack) {
      stack.innerHTML = crRenderMessageItems(items, {
        sender, name, avatar, msgId,
        msg: { id: msgId, sender },
        fmt: sender === 'user' ? escWithTransfer : escWithImages,
        isUser: sender === 'user',
      });
      const attHtml = renderAttachments(attachments);
      if (attHtml) stack.insertAdjacentHTML('afterend', attHtml);
    }
  }
  // 为流式气泡添加 TTS 重听按钮 + data-msg-id
  if (streamRow && streamRow.id && streamRow.id.startsWith('streaming-')) {
    const msgId = streamRow.id.replace('streaming-', '');
    streamRow.setAttribute('data-msg-id', msgId);
    const sender = streamRow.classList.contains('connor') ? 'connor' : (streamRow.classList.contains('aion') ? 'aion' : 'user');
    crEnsureMsgMenu(streamRow, sender, msgId);
    if (crMemoryRecordMsgIds.has(msgId)) crApplyMemoryHint(msgId);
    crShowToyCapsule(msgId, crToyCommandsFromAttachments(attachments));
  }
  streamingBubble = null;
  streamingText = '';
}

// ── [MEMORY] 记忆录入提示 ──
const crMemoryRecordMsgIds = new Set();
const crMemoryRecordContent = {};

function crShowMemoryRecordHint(msgId, content) {
  if (!msgId) return;
  crMemoryRecordMsgIds.add(msgId);
  if (content) {
    crMemoryRecordContent[msgId] = crMemoryRecordContent[msgId]
      ? `${crMemoryRecordContent[msgId]}\n${content}`
      : content;
  }
  crApplyMemoryHint(msgId);
}

function crApplyMemoryHint(msgId) {
  const row = document.getElementById(`streaming-${msgId}`) || document.querySelector(`[data-msg-id="${msgId}"]`);
  if (!row) return;
  const line = row.querySelector('.sender-line');
  if (!line || line.querySelector('.memory-record-hint')) return;
  const hint = document.createElement('button');
  hint.type = 'button';
  hint.className = 'memory-record-hint';
  hint.textContent = '💡';
  hint.title = '已记录到记忆库';
  hint.onclick = (e) => {
    e.stopPropagation();
    crShowMemoryRecordCard(msgId);
  };
  line.appendChild(hint);
}

function crShowMemoryRecordCard(msgId) {
  const content = crMemoryRecordContent[msgId];
  if (!content) return;
  const overlay = document.createElement('div');
  overlay.className = 'mr-card-overlay';
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
  overlay.innerHTML = `<div class="mr-card-popup">
    <div class="mr-card-label">-- 已记录到记忆库 --</div>
    <button class="mr-card-close" onclick="this.closest('.mr-card-overlay').remove()">x</button>
    <div class="mr-card-text">${esc(content)}</div>
  </div>`;
  document.body.appendChild(overlay);
}

// ══════════════════════════════════════════════════
//  发送消息
// ══════════════════════════════════════════════════

composer.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = inputEl.value.trim();
  if ((!text && !pendingAttachments.length) || !currentRoom || isSending) return;

  isSending = true;
  sendBtn.disabled = true;
  inputEl.value = '';
  resizeInput();

  const attachments = pendingAttachments.map(a => a.url);
  pendingAttachments = [];
  renderPreview();

  // 立即显示用户消息
  playSend();
  const localRow = appendMessage({ sender: 'user', content: text, created_at: Date.now() / 1000, attachments });
  if (localRow) localRow.dataset.localEcho = '1';

  try {
    const resp = await fetch(`${API}/rooms/${currentRoom.id}/send`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: text, model: chatroomModel, connor_model: chatroomConnorModel, attachments, tts_enabled: crTtsEnabled, tts_aion_voice: crTtsAionVoice, tts_connor_voice: crTtsConnorVoice, whisper_mode: crWhisperMode }),
    });

    await consumeChatroomSSE(resp);
  } catch (err) {
    toast('发送失败: ' + err.message);
  } finally {
    isSending = false;
    sendBtn.disabled = false;
    endStreamingBubble();
    crRefocusComposerAfterSend();
  }
});

function handleSSE(data) {
  switch (data.type) {
    case 'aion_start':
      crRememberTTSMsgSender(data.id, 'aion');
      appendTyping(crName('aion'));
      // 延迟创建流式气泡，等第一个 chunk 到达时再创建
      pendingStreamSender = 'aion';
      pendingStreamId = data.id;
      break;
    case 'aion_status':
      updateTypingStatus(crName('aion'), data.text);
      break;
    case 'aion_chunk':
      if (pendingStreamSender && !streamingBubble) {
        startStreamingBubble(pendingStreamSender, pendingStreamId);
        pendingStreamSender = null;
        pendingStreamId = null;
      }
      feedStreamingChunk(data.content);
      break;
    case 'aion_done':
      pendingStreamSender = null;
      pendingStreamId = null;
      // 用服务端清理后的干净文本替换流式累积的原始文本（包含工具指令）
      if (data.message && data.message.content != null && streamingBubble) {
        streamingText = data.message.content;
      }
      if (data.message?.id) crRememberTTSMsgSender(data.message.id, 'aion');
      endStreamingBubble(data.message);
      playRecv();
      break;
    case 'connor_start':
      crRememberTTSMsgSender(data.id, 'connor');
      appendTyping(crName('connor'));
      pendingStreamSender = 'connor';
      pendingStreamId = data.id;
      break;
    case 'connor_status':
      updateTypingStatus(crName('connor'), data.text);
      break;
    case 'connor_chunk':
      if (pendingStreamSender && !streamingBubble) {
        startStreamingBubble(pendingStreamSender, pendingStreamId);
        pendingStreamSender = null;
        pendingStreamId = null;
      }
      feedStreamingChunk(data.content);
      break;
    case 'connor_done':
      pendingStreamSender = null;
      pendingStreamId = null;
      // 用服务端清理后的干净文本替换流式累积的原始文本
      if (data.message && data.message.content != null && streamingBubble) {
        streamingText = data.message.content;
      }
      if (data.message?.id) crRememberTTSMsgSender(data.message.id, 'connor');
      endStreamingBubble(data.message);
      // 如果 connor_done 带了 message 且没有流式气泡（兼容旧路径），追加消息
      if (data.message
          && !document.getElementById(`streaming-${data.message.id}`)
          && !document.querySelector(`[data-msg-id="${data.message.id}"]`)) {
        appendMessage(data.message);
      }
      playRecv();
      break;
    case 'round_start':
      appendAiChatStatus(`AI 互聊 第 ${data.round}/${data.total} 轮`);
      break;
    case 'tts_chunk':
      crEnqueueTTSChunk(data.data.msg_id, data.data.seq, data.data.url, data.data.created_at, data.data.target_client_id, data.data.text);
      break;
    case 'tts_done':
      crFinishTTSForMsg(data.data.msg_id, data.data.created_at, data.data.target_client_id);
      break;
    case 'error':
      toast('错误: ' + data.content);
      break;
    case 'system_msg':
      if (data.message) {
        if (data.message.id) crMessagesById[data.message.id] = data.message;
        if (data.message.id && data.message.sender) crRememberTTSMsgSender(data.message.id, data.message.sender);
        if (!data.message.id || !messagesEl.querySelector(`[data-msg-id="${data.message.id}"]`)) {
          appendMessage(data.message);
        }
      }
      break;
    case 'memory_record':
      crShowMemoryRecordHint(data.msg_id, data.content);
      break;
    case 'debug':
      crHandleDebug(data);
      break;
    case 'music':
      if (data.msg_id && data.cards) {
        crMusicCards[data.msg_id] = data.cards;
        crRenderMusicCards(data.msg_id);
        scrollToBottom();
        if (data.autoplay && data.cards.length) crPlayMusicOnline(data.cards[0].id);
      }
      break;
    case 'toy_command':
      crHandleToyCommand(data);
      break;
    case 'moment_new':
      // 朋友圈动态已移至独立页面
      break;
  }
}

inputEl.addEventListener('input', scheduleInputResize);
inputEl.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.isComposing) {
    // Shift+Enter 或 Ctrl+Enter 发送，Enter 换行
    if (e.shiftKey || e.ctrlKey) {
      e.preventDefault();
      composer.requestSubmit();
    }
  }
});

// ══════════════════════════════════════════════════
//  AI 互聊
// ══════════════════════════════════════════════════

async function triggerAiChat() {
  if (!currentRoom || currentRoom.type !== 'group' || isSending || isAiChatting || isReplyOnce) return;
  isAiChatting = true;
  aiChatBtn.disabled = true;
  aiChatBtn.textContent = '⏳ 互聊中...';

  try {
    const resp = await fetch(`${API}/rooms/${currentRoom.id}/ai-chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: chatroomModel, connor_model: chatroomConnorModel, tts_enabled: crTtsEnabled, tts_aion_voice: crTtsAionVoice, tts_connor_voice: crTtsConnorVoice }),
    });

    await consumeChatroomSSE(resp);
  } catch (err) {
    toast('AI 互聊失败: ' + err.message);
  } finally {
    isAiChatting = false;
    aiChatBtn.disabled = false;
    aiChatBtn.textContent = '💬 让他们聊';
    endStreamingBubble();
    removeAiChatStatus();
  }
}

async function triggerReplyOnce(speaker) {
  if (!currentRoom || currentRoom.type !== 'group' || isSending || isAiChatting || isReplyOnce) return;
  if (!['aion', 'connor'].includes(speaker)) return;

  isReplyOnce = true;
  isAiChatting = true;
  if (replyAionBtn) replyAionBtn.disabled = true;
  if (replyConnorBtn) replyConnorBtn.disabled = true;
  if (aiChatBtn) aiChatBtn.disabled = true;
  const activeBtn = speaker === 'aion' ? replyAionBtn : replyConnorBtn;
  const oldText = activeBtn ? activeBtn.textContent : '';
  if (activeBtn) activeBtn.textContent = `${crName(speaker)} 回复中...`;

  try {
    const resp = await fetch(`${API}/rooms/${currentRoom.id}/reply-once`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        speaker,
        model: chatroomModel,
        connor_model: chatroomConnorModel,
        tts_enabled: crTtsEnabled,
        tts_aion_voice: crTtsAionVoice,
        tts_connor_voice: crTtsConnorVoice,
        whisper_mode: crWhisperMode,
      }),
    });
    await consumeChatroomSSE(resp);
  } catch (err) {
    toast(`${crName(speaker)} 回复失败: ` + err.message);
  } finally {
    isReplyOnce = false;
    isAiChatting = false;
    if (replyAionBtn) replyAionBtn.disabled = false;
    if (replyConnorBtn) replyConnorBtn.disabled = false;
    if (aiChatBtn) aiChatBtn.disabled = false;
    if (activeBtn) activeBtn.textContent = oldText;
    endStreamingBubble();
    updateHeaderActions();
  }
}

// ══════════════════════════════════════════════════
//  设置
// ══════════════════════════════════════════════════

function crSettingsSnapshotBridge() {
  try { return window.top?.AppSharedData || window.AppSharedData || null; }
  catch(e) { return window.AppSharedData || null; }
}

function crSettingsSnapshotKey(roomId) {
  return `${CR_SETTINGS_SNAPSHOT_PREFIX}${roomId}`;
}

function crSaveSettingsSnapshot(room, config) {
  if (!room?.id || !config) return;
  try {
    const key = crSettingsSnapshotKey(room.id);
    const payload = JSON.stringify({ schema: 2, savedAt: Date.now(), room, config });
    localStorage.setItem(key, payload);
    if (payload.length < 900000) crSettingsSnapshotBridge()?.put?.(key, payload);
  } catch(e) {}
}

function crLoadSettingsSnapshot(roomId) {
  try {
    const key = crSettingsSnapshotKey(roomId);
    const raw = crSettingsSnapshotBridge()?.get?.(key) || localStorage.getItem(key) || '';
    const snapshot = JSON.parse(raw || 'null');
    return snapshot?.schema === 2 ? snapshot : null;
  } catch(e) { return null; }
}

function crCurrentSettingsConfig() {
  return {
    ai_name: crAiName,
    user_name: crUserName,
    connor_name: crConnorName,
    tts_enabled: crTtsEnabled,
    tts_aion_voice: crTtsAionVoice,
    tts_connor_voice: crTtsConnorVoice,
    aion_model: chatroomModel,
    connor_model: chatroomConnorModel,
    reply_order: chatroomReplyOrder,
    ambient_voice_enabled: crAmbientVoiceEnabled,
    ambient_voice_wake_word: crAmbientWakeWord,
    ambient_voice_stop_word: crAmbientStopWord,
    ambient_voice_min_chars: crAmbientMinChars,
    ambient_voice_interval_seconds: crAmbientIntervalSec,
    ambient_voice_cooldown_seconds: crAmbientCooldownSec,
  };
}

function crPopulateSettings(room, cfg = {}) {
  if (!room) return;
  applyChatroomNames(cfg);
  if (cfg.tts_enabled !== undefined) crTtsEnabled = !!cfg.tts_enabled;
  if (cfg.tts_aion_voice !== undefined) crTtsAionVoice = cfg.tts_aion_voice || '';
  if (cfg.tts_connor_voice !== undefined) crTtsConnorVoice = cfg.tts_connor_voice || '';
  document.getElementById('setTtsEnabled').checked = crTtsEnabled;
  if (document.getElementById('setTtsAionVoice')) document.getElementById('setTtsAionVoice').value = crTtsAionVoice;
  if (document.getElementById('setTtsConnorVoice')) document.getElementById('setTtsConnorVoice').value = crTtsConnorVoice;
  crApplyAmbientVoiceConfig({ ...crCurrentSettingsConfig(), ...cfg });

  document.getElementById('setTitle').value = room.title || '';
  document.getElementById('setContextLimit').value = room.context_limit || room.context_minutes || 30;
  document.getElementById('setAiRounds').value = room.ai_chat_rounds || 1;
  chatroomModel = cfg.aion_model || chatroomModel;
  chatroomConnorModel = cfg.connor_model || chatroomConnorModel || 'Codex';
  document.getElementById('setAionModel').innerHTML = renderModelOptions(chatroomModel);
  document.getElementById('setConnorModel').innerHTML = renderModelOptions(chatroomConnorModel);
  document.getElementById('setAionModel').value = chatroomModel || '';
  document.getElementById('setConnorModel').value = chatroomConnorModel || 'Codex';

  document.getElementById('optAion').textContent = `${crAiName} 优先`;
  document.getElementById('optConnor').textContent = `${crConnorName} 优先`;
  chatroomReplyOrder = cfg.reply_order || chatroomReplyOrder || 'random';
  document.getElementById('setReplyOrder').value = chatroomReplyOrder;
  updateHeaderActions();

  const isConnor1v1 = room.type === 'connor_1v1';
  document.getElementById('fieldAiRounds').style.display = isConnor1v1 ? 'none' : '';
  document.getElementById('fieldReplyOrder').style.display = isConnor1v1 ? 'none' : '';
  document.getElementById('fieldAionModel').style.display = isConnor1v1 ? 'none' : '';
}

async function openSettings() {
  if (!currentRoom) { toast('请先选择一个房间'); return; }

  const roomId = currentRoom.id;
  const openStartedAt = Date.now();
  crSettingsDirtyAt = 0;
  crSettingsLoadingRoomId = roomId;
  document.getElementById('settingsOverlay').classList.add('active');
  const cached = crLoadSettingsSnapshot(roomId);
  crPopulateSettings(cached?.room || currentRoom, cached?.config || crCurrentSettingsConfig());
  crLoadTTSVoices(); // 音色慢也不阻塞主要设置。

  try {
    const result = await api(`/rooms/${roomId}/settings`, { timeoutMs: 10000 });
    if (crSettingsLoadingRoomId !== roomId || currentRoom?.id !== roomId) return;
    crSaveSettingsSnapshot(result.room, result.config);
    if (!crSettingsDirtyAt || crSettingsDirtyAt <= openStartedAt) {
      crPopulateSettings(result.room, result.config);
    }
  } catch(e) {
    if (!cached) toast(e.message || '设置刷新失败，当前显示本地值');
  }
}

function closeSettings() {
  crSettingsLoadingRoomId = null;
  document.getElementById('settingsOverlay').classList.remove('active');
}

// ── 人设页面 ──
const CR_PERSONA_SECTIONS = [
  { key: 'identity_core', label: '核心身份', lock: 'locked', wide: true },
  { key: 'relationship_core', label: '关系锚点', lock: 'adaptive', wide: true },
  { key: 'personality_core', label: '人格与判断', lock: 'adaptive', wide: true },
  { key: 'communication_style', label: '表达与互动方式', lock: 'adaptive', wide: true },
  { key: 'boundaries_and_forbidden', label: '边界与禁忌', lock: 'adaptive', wide: true },
  { key: 'relationship_protocol', label: '协议边界', lock: 'adaptive', wide: true },
  { key: 'tool_and_capability_rules', label: '能力与工具规则', lock: 'guarded' },
  { key: 'prompt_hygiene_rules', label: '提示边界', lock: 'guarded' },
  { key: 'evolution_notes', label: '用户信息', lock: 'guarded' },
];
const CR_PERSONA_LOCK_LABELS = {
  locked: '锁定',
  guarded: '审慎',
  adaptive: '可进化',
  temporary: '临时',
};
let crPersonaRendered = false;
let crPersonaEvolutionRuns = [];
let crPersonaSelectedRunIndex = -1;
let crPersonaEvolutionSaving = false;
let crPersonaResizeBound = false;
let crPersonaViewportWidth = window.innerWidth;
const crPersonaResizeFrames = new WeakMap();
const crPersonaTextareaMetrics = new WeakMap();

function crPersonaFieldId(key) {
  return `personaSection_${key}`;
}

function crCompactJoin(parts) {
  return parts.map(part => (part || '').trim()).filter(Boolean).join('\n\n');
}

function crEstimatePersonaRows(text) {
  const lines = String(text || '').split('\n');
  return lines.reduce((count, line) => count + Math.max(1, Math.ceil(line.length / 56)), 0);
}

function crResizePersonaTextarea(el) {
  if (!el) return;
  let metrics = crPersonaTextareaMetrics.get(el);
  if (!metrics) {
    const style = window.getComputedStyle(el);
    metrics = {
      lineHeight: parseFloat(style.lineHeight) || 19,
      verticalPadding: (parseFloat(style.paddingTop) || 0) + (parseFloat(style.paddingBottom) || 0) + 2,
    };
    crPersonaTextareaMetrics.set(el, metrics);
  }
  const estimatedRows = crEstimatePersonaRows(el.value);
  const charCount = String(el.value || '').length;
  const minRows = (charCount > 220 || estimatedRows > 12)
    ? 14
    : (charCount > 60 || estimatedRows > 5)
      ? 10
      : (charCount > 0 ? 5 : 4);
  const minHeight = Math.round((metrics.lineHeight * minRows) + metrics.verticalPadding);
  const maxHeight = Math.max(220, Math.min(window.innerHeight * 0.56, 560));
  el.style.height = 'auto';
  const scrollHeight = el.scrollHeight;
  const nextHeight = Math.min(Math.max(scrollHeight, minHeight), maxHeight);
  el.style.height = `${nextHeight}px`;
  el.style.overflowY = scrollHeight > maxHeight ? 'auto' : 'hidden';
}

function crSchedulePersonaTextareaResize(el) {
  if (!el || crPersonaResizeFrames.has(el)) return;
  const frame = requestAnimationFrame(() => {
    crPersonaResizeFrames.delete(el);
    crResizePersonaTextarea(el);
  });
  crPersonaResizeFrames.set(el, frame);
}

function crResizePersonaTextareas(root = document) {
  root.querySelectorAll?.('.persona-page-body textarea').forEach(crResizePersonaTextarea);
}

function crHandlePersonaViewportResize() {
  const nextWidth = window.innerWidth;
  if (nextWidth === crPersonaViewportWidth) return;
  crPersonaViewportWidth = nextWidth;
  if (document.getElementById('personaOverlay')?.classList.contains('active')) {
    crResizePersonaTextareas(document.getElementById('personaOverlay'));
  }
}

function crPersonaSectionForHeading(heading) {
  const text = String(heading || '').toLowerCase();
  const bare = text.replace(/^#{1,6}\s*/, '').replace(/^\*\*/, '').replace(/\*\*$/, '').trim();
  const userName = String(crUserName || '').trim().toLowerCase();
  if (
    text.includes('用户信息') ||
    text.includes('用户资料') ||
    text.includes('用户档案') ||
    text.includes('用户设定') ||
    text.includes('user info') ||
    text.includes('user profile') ||
    (userName && (bare === userName || (bare.includes(userName) && (
      bare.includes('信息') ||
      bare.includes('资料') ||
      bare.includes('档案') ||
      bare.includes('设定') ||
      bare.includes('profile')
    ))))
  ) return 'evolution_notes';
  if (text.includes('协议') || text.includes('protocol')) return 'relationship_protocol';
  if (text.includes('prompt') || text.includes('system') || text.includes('叙事') || text.includes('安全')) return 'prompt_hygiene_rules';
  if (text.includes('能力') || text.includes('工具') || text.includes('tool')) return 'tool_and_capability_rules';
  if (text.includes('边界') || text.includes('禁止') || text.includes('forbidden') || text.includes('boundary')) return 'boundaries_and_forbidden';
  if (text.includes('说话') || text.includes('表达') || text.includes('互动') || text.includes('情绪') || text.includes('提醒') || text.includes('communication')) return 'communication_style';
  if (text.includes('人格') || text.includes('性格') || text.includes('判断') || text.includes('personality')) return 'personality_core';
  if (text.includes('关系') || text.includes('锚点') || text.includes('relationship')) return 'relationship_core';
  if (text.includes('身份') || text.includes('identity')) return 'identity_core';
  return 'evolution_notes';
}

function crMigratePersonaSections(text) {
  const clean = (text || '').replace(/\r\n/g, '\n').trim();
  const result = {};
  if (!clean) return result;
  const headingRe = /^(#{1,6}\s+.+|\*\*[^*\n]+\*\*)\s*$/gm;
  const matches = [...clean.matchAll(headingRe)];
  if (!matches.length) {
    result.identity_core = clean;
    return result;
  }
  const intro = clean.slice(0, matches[0].index).trim();
  if (intro) result.identity_core = intro;
  matches.forEach((match, index) => {
    const bodyStart = match.index + match[0].length;
    const bodyEnd = index + 1 < matches.length ? matches[index + 1].index : clean.length;
    const body = clean.slice(bodyStart, bodyEnd).trim();
    if (!body) return;
    const key = crPersonaSectionForHeading(match[1]);
    const block = `${match[1].trim()}\n${body}`.trim();
    result[key] = result[key] ? `${result[key]}\n\n${block}` : block;
  });
  return result;
}

function crNormalizePersonaSections(cfg) {
  const stored = cfg?.connor_persona_sections || {};
  if (!stored || !Object.keys(stored).length) return crMigratePersonaSections(cfg?.connor_persona || '');
  return {
    identity_core: stored.identity_core || '',
    relationship_core: stored.relationship_core || '',
    personality_core: crCompactJoin([stored.personality_core, stored.personality_traits]),
    communication_style: crCompactJoin([
      stored.communication_style,
      stored.speech_style,
      stored.interaction_rules,
      stored.emotional_modes,
      stored.care_and_reminders,
    ]),
    boundaries_and_forbidden: stored.boundaries_and_forbidden || '',
    relationship_protocol: stored.relationship_protocol || '',
    tool_and_capability_rules: stored.tool_and_capability_rules || '',
    prompt_hygiene_rules: crCompactJoin([stored.prompt_hygiene_rules, stored.security_narrative_rules]),
    evolution_notes: stored.evolution_notes || '',
  };
}

function crRenderPersonaSections() {
  const container = document.getElementById('personaSections');
  if (!container || crPersonaRendered) return;
  container.innerHTML = CR_PERSONA_SECTIONS.map(section => `
    <details class="persona-section ${section.wide ? 'wide' : ''}">
      <summary>
        <span class="persona-section-title">${section.label}</span>
        <span class="persona-lock ${section.lock}">${CR_PERSONA_LOCK_LABELS[section.lock] || section.lock}</span>
      </summary>
      <textarea id="${crPersonaFieldId(section.key)}" spellcheck="false"></textarea>
    </details>
  `).join('');
  container.querySelectorAll('.persona-section').forEach(sectionEl => {
    sectionEl.addEventListener('toggle', () => crResizePersonaTextareas(sectionEl));
  });
  container.querySelectorAll('textarea').forEach(textarea => {
    textarea.addEventListener('input', () => crSchedulePersonaTextareaResize(textarea));
  });
  document.getElementById('personaExtraText')?.addEventListener('input', event => crSchedulePersonaTextareaResize(event.currentTarget));
  if (!crPersonaResizeBound) {
    window.addEventListener('resize', crHandlePersonaViewportResize, { passive: true });
    crPersonaResizeBound = true;
  }
  document.getElementById('personaConnorName')?.addEventListener('input', crSyncPersonaNameLabels);
  document.getElementById('personaEvolutionEnabled')?.addEventListener('change', crSavePersonaEvolutionToggle);
  crPersonaRendered = true;
}

function crFillPersonaSections(values = {}) {
  CR_PERSONA_SECTIONS.forEach(section => {
    const el = document.getElementById(crPersonaFieldId(section.key));
    if (el) {
      el.value = values?.[section.key] || '';
      crResizePersonaTextarea(el);
    }
  });
  crResizePersonaTextarea(document.getElementById('personaExtraText'));
}

function crCollectPersonaSections() {
  return CR_PERSONA_SECTIONS.reduce((acc, section) => {
    acc[section.key] = (document.getElementById(crPersonaFieldId(section.key))?.value || '').trim();
    return acc;
  }, {});
}

function crCompilePersonaSections() {
  return CR_PERSONA_SECTIONS.map(section => {
    const value = (document.getElementById(crPersonaFieldId(section.key))?.value || '').trim();
    return value ? `[${section.label}]\n${value}` : '';
  }).filter(Boolean).join('\n\n');
}

function crCurrentPersonaName() {
  return (document.getElementById('personaConnorName')?.value || '').trim() || crConnorName || '第二AI';
}

function crSyncPersonaNameLabels() {
  const name = crCurrentPersonaName();
  const title = document.getElementById('personaPageTitle');
  if (title) title.textContent = `🎭 ${name} 人设`;
  const evoTitle = document.getElementById('personaEvolutionTitle');
  if (evoTitle) evoTitle.textContent = `${name} 自动复盘`;
  const historyTitle = document.getElementById('personaEvolutionHistoryTitle');
  if (historyTitle) historyTitle.textContent = `${name} 自动复盘记录`;
}

function crBeijingDateInfo(date = new Date()) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(date).reduce((acc, part) => {
    acc[part.type] = part.value;
    return acc;
  }, {});
  return { date: `${parts.year}-${parts.month}-${parts.day}`, hour: Number(parts.hour || 0) };
}

function crAddDaysToBeijingDate(dateText, days) {
  const base = new Date(`${dateText}T00:00:00+08:00`);
  base.setUTCDate(base.getUTCDate() + days);
  return crBeijingDateInfo(base).date;
}

function crCurrentReviewDateString() {
  const now = crBeijingDateInfo();
  return now.hour < 5 ? crAddDaysToBeijingDate(now.date, -1) : now.date;
}

function crSetupPersonaEvolutionDate() {
  const input = document.getElementById('personaEvolutionDate');
  if (!input) return;
  const current = crCurrentReviewDateString();
  input.max = current;
  if (!input.value) input.value = current;
}

async function crPersonaEvolutionApi(path, opts = {}) {
  const resp = await fetch(`/api/persona-evolution/connor${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok || data?.ok === false || data?.detail) {
    throw new Error(data.message || data.detail || '请求失败');
  }
  return data;
}

function crEvolutionStatusLabel(status) {
  return {
    applied: '已自动改写',
    draft: '待确认',
    reviewed: '已复盘',
    skipped: '已跳过',
    failed: '失败',
    duplicate: '已执行',
  }[status] || status || '未知';
}

function crFormatRunTime(ts) {
  if (!ts) return '';
  try {
    return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false });
  } catch {
    return '';
  }
}

function crEvolutionDiffsOf(entry) {
  if (Array.isArray(entry?.diffs) && entry.diffs.length) return entry.diffs;
  if (Array.isArray(entry?.patches)) return entry.patches;
  return [];
}

function crNormalizeEvolutionAction(diff, run = {}) {
  const action = diff?.action || '';
  if (['added', 'modified', 'cleared'].includes(action)) return action;
  const key = diff?.section || diff?.target || '';
  const before = (run?.before_sections?.[key] || diff?.before_preview || '').trim();
  const after = (run?.after_sections?.[key] || diff?.after_preview || '').trim();
  if (!before && after) return 'added';
  if (before && !after) return 'cleared';
  return 'modified';
}

function crCompactEvolutionText(text, limit = 140) {
  const clean = (text || '').replace(/\s+/g, ' ').trim();
  return clean.length > limit ? `${clean.slice(0, limit)}...` : clean;
}

function crRenderEvolutionDiffs(diffs = [], run = {}) {
  if (!diffs.length) return '';
  const groups = [
    { key: 'added', title: '新增了什么' },
    { key: 'modified', title: '修改了什么' },
    { key: 'cleared', title: '删除了什么' },
  ];
  return groups.map(group => {
    const items = diffs.filter(diff => crNormalizeEvolutionAction(diff, run) === group.key);
    if (!items.length) return '';
    return `<div class="persona-evolution-change-group">
      <div class="persona-evolution-change-title">${group.title}</div>
      ${items.map(diff => `<div class="persona-evolution-change"><b>${esc(diff.label || diff.section || '未命名部分')}</b>${diff.reason ? `：${esc(crCompactEvolutionText(diff.reason))}` : ''}</div>`).join('')}
    </div>`;
  }).filter(Boolean).join('');
}

function crRenderPersonaEvolutionLatest(entry) {
  const el = document.getElementById('personaEvolutionLatest');
  if (!el) return;
  if (!entry) {
    el.textContent = '最近复盘：暂无记录';
    return;
  }
  const diffs = crEvolutionDiffsOf(entry);
  const changed = diffs.length ? ` · 已改 ${diffs.length} 个部分` : '';
  const when = entry.window_label || crFormatRunTime(entry.created_at);
  const timeText = when ? ` · ${when}` : '';
  el.textContent = `最近复盘：${crEvolutionStatusLabel(entry.status)} · 点评 ${entry.feedback_count || 0} 条${changed}${timeText}`;
}

function crRenderPersonaEvolutionRuns() {
  const el = document.getElementById('personaEvolutionRuns');
  if (!el) return;
  const runs = crPersonaEvolutionRuns;
  if (!runs.length) {
    el.innerHTML = `<div class="persona-history-empty">近 72 小时暂无复盘记录</div>`;
    return;
  }
  if (crPersonaSelectedRunIndex >= runs.length) crPersonaSelectedRunIndex = -1;
  const selected = crPersonaSelectedRunIndex >= 0 ? runs[crPersonaSelectedRunIndex] : null;
  const rows = runs.map((run, index) => {
    const diffs = crEvolutionDiffsOf(run);
    const active = index === crPersonaSelectedRunIndex;
    return `<div class="persona-history-row">
      <button class="persona-history-tab ${active ? 'active' : ''}" type="button" onclick="crSelectPersonaEvolutionRun(${index})">
        <span>${esc(run.window_label || crFormatRunTime(run.created_at))}</span>
        <span class="persona-history-status">${active ? '收起' : '查看'} · ${esc(crEvolutionStatusLabel(run.status))} · ${run.feedback_count || 0} 条${diffs.length ? ` · 改 ${diffs.length}` : ''}</span>
      </button>
      <button class="persona-history-delete" type="button" title="删除记录" onclick="crDeletePersonaEvolutionRun(event, ${index})">删</button>
    </div>`;
  }).join('');
  const detail = selected
    ? `<div class="persona-history-detail">${[
        selected.summary ? `<div class="persona-history-note">${esc(selected.summary)}</div>` : '',
        selected.user_message ? `<div class="persona-history-note">${esc(selected.user_message)}</div>` : '',
        selected.error ? `<div class="persona-history-note">${esc(selected.error)}</div>` : '',
        crRenderEvolutionDiffs(crEvolutionDiffsOf(selected), selected),
      ].filter(Boolean).join('') || '<div class="persona-history-note">本轮没有可显示的详细内容。</div>'}</div>`
    : `<div class="persona-history-note">选择一条记录查看详情。</div>`;
  el.innerHTML = rows + detail;
}

async function crLoadPersonaEvolutionRuns() {
  try {
    const data = await crPersonaEvolutionApi('/runs?limit=50');
    crPersonaEvolutionRuns = data.runs || [];
    crRenderPersonaEvolutionLatest(crPersonaEvolutionRuns[0] || null);
    crRenderPersonaEvolutionRuns();
  } catch (e) {
    console.warn('加载人设复盘记录失败', e);
  }
}

function crSelectPersonaEvolutionRun(index) {
  crPersonaSelectedRunIndex = crPersonaSelectedRunIndex === index ? -1 : index;
  crRenderPersonaEvolutionRuns();
}

async function crDeletePersonaEvolutionRun(event, index) {
  event.stopPropagation();
  const run = crPersonaEvolutionRuns[index];
  if (!run?.id) return;
  if (!confirm('删除这条复盘记录？')) return;
  try {
    await crPersonaEvolutionApi(`/runs/${encodeURIComponent(run.id)}`, { method: 'DELETE' });
    crPersonaSelectedRunIndex = -1;
    toast('记录已删除');
    await crLoadPersonaEvolutionRuns();
  } catch (e) {
    console.error(e);
    toast('删除失败');
  }
}

async function crSavePersonaEvolutionToggle() {
  if (crPersonaEvolutionSaving) return;
  const input = document.getElementById('personaEvolutionEnabled');
  if (!input) return;
  const enabled = !!input.checked;
  crPersonaEvolutionSaving = true;
  input.disabled = true;
  try {
    await crPersonaEvolutionApi('/config', {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    });
    toast(enabled ? '每日复盘已开启' : '每日复盘已关闭');
  } catch (e) {
    input.checked = !enabled;
    console.error(e);
    toast(e.message || '开关保存失败');
  } finally {
    input.disabled = false;
    crPersonaEvolutionSaving = false;
  }
}

async function crRunPersonaEvolutionTest() {
  const btn = document.getElementById('personaEvolutionTestBtn');
  const date = document.getElementById('personaEvolutionDate')?.value || crCurrentReviewDateString();
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = '复盘中';
  try {
    await crSavePersona({ keepOpen: true, silent: true });
    const result = await crPersonaEvolutionApi('/run-test', {
      method: 'POST',
      body: JSON.stringify({ date }),
    });
    crPersonaSelectedRunIndex = -1;
    crRenderPersonaEvolutionLatest(result);
    await crLoadPersonaEvolutionRuns();
    const cfg = await api('/config');
    crFillPersonaSections(crNormalizePersonaSections(cfg));
    toast(result?.ok === false ? '复盘失败' : '复盘已完成');
  } catch (e) {
    console.error(e);
    crRenderPersonaEvolutionLatest({ status: 'failed', message: '测试复盘失败', error: e.message || String(e) });
    toast('复盘失败');
  } finally {
    btn.disabled = false;
    btn.textContent = '立即复盘';
  }
}

async function crOpenPersonaEvolutionHistory() {
  crPersonaSelectedRunIndex = -1;
  crSyncPersonaNameLabels();
  await crLoadPersonaEvolutionRuns();
  const modal = document.getElementById('personaEvolutionModal');
  modal?.classList.add('show');
  modal?.setAttribute('aria-hidden', 'false');
}

function crClosePersonaEvolutionHistory() {
  const modal = document.getElementById('personaEvolutionModal');
  modal?.classList.remove('show');
  modal?.setAttribute('aria-hidden', 'true');
}

function crOnPersonaEvolutionBackdrop(event) {
  if (event.target?.id === 'personaEvolutionModal') crClosePersonaEvolutionHistory();
}

async function crOpenPersona() {
  crRenderPersonaSections();
  crSetupPersonaEvolutionDate();
  document.getElementById('personaOverlay').classList.add('active');
  closeSidebar();
  try {
    const cfg = await api('/config');
    applyChatroomNames(cfg);
    document.getElementById('personaConnorName').value = cfg.connor_name || '第二AI';
    document.getElementById('personaExtraEnabled').checked = !!cfg.connor_persona_extra_enabled;
    document.getElementById('personaExtraText').value = cfg.connor_persona_extra || '';
    document.getElementById('personaEvolutionEnabled').checked = !!cfg.connor_persona_evolution_enabled;
    crFillPersonaSections(crNormalizePersonaSections(cfg));
    crSyncPersonaNameLabels();
    await crLoadPersonaEvolutionRuns();
  } catch(e) {
    console.error(e);
    toast('人设加载失败');
  }
}

function crClosePersona() {
  document.getElementById('personaOverlay').classList.remove('active');
  crClosePersonaEvolutionHistory();
}

async function crSavePersona(options = {}) {
  const name = document.getElementById('personaConnorName').value.trim();
  const extraEnabled = document.getElementById('personaExtraEnabled').checked;
  const extra = document.getElementById('personaExtraText').value;
  const sections = crCollectPersonaSections();
  const persona = crCompilePersonaSections();
  await api('/config', {
    method: 'PUT',
    body: JSON.stringify({
      connor_name: name || undefined,
      connor_persona: persona,
      connor_persona_sections: sections,
      connor_persona_evolution_enabled: !!document.getElementById('personaEvolutionEnabled').checked,
      connor_persona_extra_enabled: extraEnabled,
      connor_persona_extra: extra,
    }),
  });
  if (name) applyChatroomNames({ connor_name: name });
  crSyncPersonaNameLabels();
  if (!options.keepOpen) crClosePersona();
  if (!options.silent) toast('人设已保存');
}

async function saveSettings() {
  if (!currentRoom) return;
  const roomId = currentRoom.id;
  const btn = document.getElementById('saveSettingsBtn');
  const oldText = btn?.textContent || '保存';
  if (btn) { btn.disabled = true; btn.textContent = '保存中...'; }
  const nextReplyOrder = document.getElementById('setReplyOrder').value || 'random';
  const nextAionModel = document.getElementById('setAionModel')?.value || chatroomModel;
  const nextConnorModel = document.getElementById('setConnorModel')?.value || chatroomConnorModel || 'Codex';
  const ambient = crAmbientReadInputs();
  const payload = {
      title: document.getElementById('setTitle').value,
      context_limit: parseInt(document.getElementById('setContextLimit').value) || 30,
      ai_chat_rounds: parseInt(document.getElementById('setAiRounds').value) || 1,
      aion_model: nextAionModel,
      connor_model: nextConnorModel,
      tts_enabled: !!document.getElementById('setTtsEnabled').checked,
      tts_aion_voice: document.getElementById('setTtsAionVoice').value,
      tts_connor_voice: document.getElementById('setTtsConnorVoice').value,
      reply_order: nextReplyOrder,
      ...ambient,
  };

  try {
    let result;
    try {
      result = await api(`/rooms/${roomId}/settings`, {
        method: 'PATCH', body: JSON.stringify(payload), timeoutMs: 15000,
      });
    } catch(saveError) {
      if (saveError?.status && saveError.status < 500) throw saveError;
      // 超时可能发生在服务器已落盘之后；只读核对避免误报失败。
      const checked = await api(`/rooms/${roomId}/settings`, { timeoutMs: 8000 }).catch(() => null);
      if (!checked || checked.config?.reply_order !== payload.reply_order || checked.room?.title !== payload.title) {
        throw saveError;
      }
      result = { ok: true, ...checked };
    }

    chatroomModel = result.config?.aion_model || nextAionModel;
    chatroomConnorModel = result.config?.connor_model || nextConnorModel;
    crTtsEnabled = !!(result.config?.tts_enabled ?? payload.tts_enabled);
    crTtsAionVoice = result.config?.tts_aion_voice ?? payload.tts_aion_voice;
    crTtsConnorVoice = result.config?.tts_connor_voice ?? payload.tts_connor_voice;
    chatroomReplyOrder = result.config?.reply_order || nextReplyOrder;
    crApplyAmbientVoiceConfig(result.config || payload);
    crTtsPlaybackActiveAt = Date.now() / 1000;
    crSendTTSState();
    updateHeaderActions();
    crAmbientSyncRunning().catch(() => {});

    currentRoom = { ...currentRoom, ...(result.room || {}), context_limit: result.room?.context_limit || payload.context_limit };
    rooms = rooms.map(room => room.id === roomId ? { ...room, ...currentRoom } : room);
    roomTitleEl.textContent = currentRoom.title;
    renderRoomList();
    crSaveSettingsSnapshot(currentRoom, result.config || payload);
    closeSettings();
    toast('已保存');
  } catch(e) {
    toast(`保存失败：${e.message || '网络异常'}`, 3500);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = oldText; }
  }
}

async function triggerDigest() {
  if (!currentRoom) return;
  toast('正在总结记忆...');
  const result = await api(`/rooms/${currentRoom.id}/digest`, {
    method: 'POST',
    body: JSON.stringify({ connor_model: chatroomConnorModel })
  });
  toast(result.message || '总结完成');
  loadMemories();
}

// ══════════════════════════════════════════════════
//  记忆库
// ══════════════════════════════════════════════════

function openMemory() {
  if (!currentRoom) { toast('请先选择一个房间'); return; }
  document.getElementById('memoryOverlay').classList.add('active');
  hideMemForm();
  loadMemories();
  loadChatroomCompressionDraft();
  closeSidebar();
}

function closeMemory() {
  document.getElementById('memoryOverlay').classList.remove('active');
}

async function memoryCompressionApi(method, url, body = null) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  const resp = await fetch(url, opts);
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.detail || data.message || '请求失败');
  return data;
}

async function loadChatroomCompressionDraft() {
  try {
    const result = await memoryCompressionApi('GET', '/api/memories/compress-daily/latest?target=chatroom');
    chatroomDailyCompressReview = result.review || null;
    renderChatroomCompressionReview();
  } catch {
    chatroomDailyCompressReview = null;
  }
}

function flattenChatroomDraft(field) {
  return ((chatroomDailyCompressReview?.payload?.chatroom?.batches) || []).flatMap(batch => batch[field] || []);
}

function chatroomDraftOldRows() {
  return chatroomDailyCompressReview?.counts?.chatroom?.old_rows || [];
}

function chatroomDraftTime(ts) {
  if (!ts) return '';
  const d = new Date(Number(ts) * 1000);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function crFormatMemoryOccurrence(m) {
  if (m?.memory_time_label) return m.memory_time_label;
  const start = Number(m?.source_start_ts || 0);
  const end = Number(m?.source_end_ts || 0);
  const created = Number(m?.created_at || 0);
  const fmt = ts => new Date(ts * 1000).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
  if (start > 0) {
    if (end > 0 && Math.abs(end - start) >= 60) {
      const sameDay = new Date(start * 1000).toDateString() === new Date(end * 1000).toDateString();
      const endText = sameDay
        ? new Date(end * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
        : fmt(end);
      return `发生：${fmt(start)}-${endText}`;
    }
    return `发生：${fmt(start)}`;
  }
  return created > 0 ? `记录：${fmt(created)}` : '';
}

function chatroomDraftKeywords(value) {
  if (!value) return [];
  if (Array.isArray(value)) return value;
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return String(value).split(/[,，、\n]/).map(s => s.trim()).filter(Boolean);
  }
}

function chatroomDraftBatches() {
  return chatroomDailyCompressReview?.payload?.chatroom?.batches || [];
}

function chatroomDraftCoveredSet(batch) {
  const ids = new Set(batch.covered_ids || []);
  (batch.discard_memory_ids || []).forEach(id => ids.add(id));
  ['compressed_daily', 'important_memories'].forEach(field => {
    (batch[field] || []).forEach(item => (item.source_memory_ids || []).forEach(id => ids.add(id)));
  });
  return ids;
}

function refreshChatroomDraftBatch(batch) {
  const oldIds = new Set((batch.old_rows || []).map(row => row.id).filter(Boolean));
  const covered = Array.from(chatroomDraftCoveredSet(batch)).filter(id => !oldIds.size || oldIds.has(id));
  batch.covered_ids = covered;
  batch.remaining = Math.max(0, Number(batch.input_count || 0) - covered.length);
}

function refreshChatroomDraftPayload() {
  chatroomDraftBatches().forEach(refreshChatroomDraftBatch);
}

function setChatroomDraftItemValue(batchIndex, field, itemIndex, prop, value) {
  const item = chatroomDraftBatches()?.[batchIndex]?.[field]?.[itemIndex];
  if (!item) return;
  if (prop === 'keywords') item[prop] = chatroomDraftKeywords(value);
  else if (prop === 'importance' || prop === 'compression_stage') item[prop] = Number(value || 0);
  else item[prop] = value;
}

function removeChatroomDraftItem(batchIndex, field, itemIndex) {
  const items = chatroomDraftBatches()?.[batchIndex]?.[field];
  if (!items) return;
  items.splice(itemIndex, 1);
  refreshChatroomDraftPayload();
  renderChatroomCompressionReview();
}

function addChatroomDraftItem(field) {
  const batches = chatroomDraftBatches();
  if (!batches.length) return;
  const batch = batches[0];
  const now = Math.floor(Date.now() / 1000);
  batch[field] = batch[field] || [];
  batch[field].push({
    content: '',
    source_memory_ids: [],
    source_msg_ids: [],
    keywords: [],
    importance: field === 'important_memories' ? 0.8 : 0.25,
    memory_time: batch.old_rows?.[0]?.source_start_ts || batch.old_rows?.[0]?.created_at || now,
    source_start_ts: batch.old_rows?.[0]?.source_start_ts || batch.old_rows?.[0]?.created_at || now,
    source_end_ts: batch.old_rows?.[0]?.source_end_ts || batch.old_rows?.[0]?.created_at || now,
    memory_kind: field === 'important_memories' ? 'long_term' : 'daily',
    memory_type: field === 'important_memories' ? 'important' : 'daily',
    compression_stage: field === 'important_memories' ? 0 : Number(batch.output_stage || 1),
    retain_source_detail: batch.retain_source_detail ?? (Number(batch.output_stage || 1) < 3),
    reason: '用户手动添加',
    room_id: currentRoom?.id || 'connor_unified',
    scope: 'connor'
  });
  renderChatroomCompressionReview();
}

function setChatroomOldRowCovered(batchIndex, memId, covered) {
  const batch = chatroomDraftBatches()?.[batchIndex];
  if (!batch || !memId) return;
  batch.discard_memory_ids = (batch.discard_memory_ids || []).filter(id => id !== memId);
  batch.covered_ids = (batch.covered_ids || []).filter(id => id !== memId);
  if (covered) {
    batch.discard_memory_ids.push(memId);
  } else {
    ['compressed_daily', 'important_memories'].forEach(field => {
      (batch[field] || []).forEach(item => {
        item.source_memory_ids = (item.source_memory_ids || []).filter(id => id !== memId);
      });
    });
  }
  refreshChatroomDraftBatch(batch);
  renderChatroomCompressionReview();
}

async function saveChatroomDailyCompressionDraft(silent = false) {
  if (!chatroomDailyCompressReview?.id) return null;
  refreshChatroomDraftPayload();
  const result = await memoryCompressionApi('PATCH', `/api/memories/compress-daily/${chatroomDailyCompressReview.id}`, {
    payload: chatroomDailyCompressReview.payload
  });
  if (!result.ok) throw new Error(result.message || '保存草稿失败');
  chatroomDailyCompressReview = result.review || chatroomDailyCompressReview;
  renderChatroomCompressionReview();
  if (!silent) toast(result.message || '压缩草稿已保存');
  return result;
}

function renderChatroomCompressItem(item, kindLabel, batchIndex, field, itemIndex) {
  const kws = chatroomDraftKeywords(item.keywords);
  const sourceCount = (item.source_memory_ids || []).length;
  return `<div class="chatroom-compress-item">
    <div class="chatroom-compress-meta">
      <span>${esc(kindLabel)}</span>
      <span>stage ${Number(item.compression_stage || 0)}</span>
      <span>${esc(chatroomDraftTime(item.memory_time || item.source_start_ts))}</span>
      <span>来源 ${sourceCount} 条日常</span>
      <span>重要度 ${Number(item.importance || 0).toFixed(2)}</span>
      ${kws.length ? `<span>${kws.map(k => esc(k)).join(' · ')}</span>` : ''}
    </div>
    <div class="chatroom-compress-edit">
      <textarea oninput="setChatroomDraftItemValue(${batchIndex},'${field}',${itemIndex},'content',this.value)">${esc(item.content || '')}</textarea>
      <div class="chatroom-compress-edit-row">
        <input value="${esc(kws.join(', '))}" placeholder="关键词" oninput="setChatroomDraftItemValue(${batchIndex},'${field}',${itemIndex},'keywords',this.value)">
        <input type="number" min="0" max="1" step="0.05" value="${Number(item.importance || 0).toFixed(2)}" oninput="setChatroomDraftItemValue(${batchIndex},'${field}',${itemIndex},'importance',this.value)">
        <input type="number" min="0" max="4" step="1" value="${Number(item.compression_stage || 0)}" oninput="setChatroomDraftItemValue(${batchIndex},'${field}',${itemIndex},'compression_stage',this.value)">
      </div>
      <input value="${esc(item.reason || '')}" placeholder="理由/证据摘要" oninput="setChatroomDraftItemValue(${batchIndex},'${field}',${itemIndex},'reason',this.value);setChatroomDraftItemValue(${batchIndex},'${field}',${itemIndex},'evidence_summary',this.value)">
      <div class="chatroom-compress-mini"><button onclick="removeChatroomDraftItem(${batchIndex},'${field}',${itemIndex})">删除这条</button></div>
    </div>
  </div>`;
}

function renderChatroomOldCompressItem(item, batchIndex, covered) {
  return `<div class="chatroom-compress-item">
    <label class="chatroom-compress-old-toggle">
      <input type="checkbox" ${covered ? 'checked' : ''} onchange="setChatroomOldRowCovered(${batchIndex},'${esc(item.id)}',this.checked)">
      <div>
        <div>${esc(item.content || '')}</div>
        <div class="chatroom-compress-meta">
          <span>${esc(chatroomDraftTime(item.source_start_ts || item.created_at))}</span>
          <span>stage ${Number(item.compression_stage || 0)}</span>
          <span>重要度 ${Number(item.importance || 0).toFixed(2)}</span>
          <span>${covered ? '确认后压缩/移除旧条目' : '保留这条旧记忆'}</span>
        </div>
      </div>
    </label>
  </div>`;
}

function renderChatroomDraftItems(field, label) {
  return chatroomDraftBatches().map((batch, batchIndex) => {
    const items = batch[field] || [];
    return items.map((item, itemIndex) => renderChatroomCompressItem(
      item,
      `${label} · ${batch.tier_label || batch.compression_tier || 'stage'}`,
      batchIndex,
      field,
      itemIndex
    )).join('');
  }).join('');
}

function renderChatroomOldRows() {
  return chatroomDraftBatches().map((batch, batchIndex) => {
    const covered = chatroomDraftCoveredSet(batch);
    return (batch.old_rows || []).map(row => renderChatroomOldCompressItem(row, batchIndex, covered.has(row.id))).join('');
  }).join('');
}

function chatroomDraftItemCount(field) {
  return chatroomDraftBatches().reduce((sum, batch) => sum + ((batch[field] || []).length), 0);
}

function chatroomOldRowCount() {
  return chatroomDraftBatches().reduce((sum, batch) => sum + ((batch.old_rows || []).length), 0);
}

function chatroomCompressionWarnings() {
  const warnings = [];
  chatroomDraftBatches().forEach(batch => {
    const input = Number(batch.input_count || 0);
    if (!input) return;
    const processed = chatroomDraftCoveredSet(batch).size;
    const created = (batch.compressed_daily || []).length + (batch.important_memories || []).length;
    const ratio = processed / input;
    const label = batch.tier_label || batch.compression_tier || '未命名档位';
    if (batch.compression_tier === 'recent' && ratio > 0.4) {
      warnings.push(`${label} 拟移除 ${processed}/${input} 条，近期记忆可能压得过狠。`);
    }
    if (batch.compression_tier === 'recent' && input >= 5 && created === 0) {
      warnings.push(`${label} 没有生成新记忆，请确认不是误删近期内容。`);
    }
    if (batch.compression_tier === 'archive' && (batch.compressed_daily || []).length > 1) {
      warnings.push(`${label} 生成了较多普通日常，事实档案层建议只保留重大事实。`);
    }
  });
  return warnings;
}

function renderChatroomCompressionReview() {
  const el = document.getElementById('chatroomCompressReview');
  if (!el) return;
  if (!chatroomDailyCompressReview || !['draft', 'failed'].includes(chatroomDailyCompressReview.status)) {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  const counts = chatroomDailyCompressReview.counts || {};
  const chat = counts.chatroom || {};
  const dailyCount = chatroomDraftItemCount('compressed_daily');
  const importantCount = chatroomDraftItemCount('important_memories');
  const oldRowCount = chatroomOldRowCount();
  const messages = (chat.messages || []).filter(Boolean);
  const errors = (chat.errors || []).filter(Boolean);
  const warnings = chatroomCompressionWarnings();
  const canApply = !_chatroomCompressionRequestBusy && chatroomDailyCompressReview.status === 'draft'
    && (dailyCount || importantCount || Number(chat.processed || 0));
  const actionDisabled = _chatroomCompressionRequestBusy ? 'disabled' : '';
  el.style.display = 'block';
  el.innerHTML = `
    <div class="chatroom-compress-head">
      <span>聊天室压缩草稿</span>
      <span class="chatroom-compress-status">${esc(chatroomDailyCompressReview.status === 'draft' ? '待确认' : '生成失败')}</span>
    </div>
    <div class="chatroom-compress-stats">
      <span>候选 ${Number(chatroomDailyCompressReview.candidate_count || 0)}</span>
      <span>拟移除 ${Number(chat.processed || 0)}</span>
      <span>新日常 ${Number(chat.created_daily || 0)}</span>
      <span>新长期重要 ${Number(chat.created_important || 0)}</span>
    </div>
    ${messages.length ? `<div class="chatroom-compress-msg">${esc(messages.join('\n'))}</div>` : ''}
    ${warnings.length ? `<div class="chatroom-compress-msg" style="color:#ad6800;">${esc(warnings.join('\n'))}</div>` : ''}
    ${errors.length ? `<div class="chatroom-compress-msg" style="color:var(--danger);">${esc(errors.join('\n'))}</div>` : ''}
    <div class="chatroom-compress-mini">
      <button onclick="addChatroomDraftItem('compressed_daily')">新增日常</button>
      <button onclick="addChatroomDraftItem('important_memories')">新增长期重要</button>
    </div>
    <details open>
      <summary>新日常 ${dailyCount}</summary>
      ${dailyCount ? renderChatroomDraftItems('compressed_daily', '日常') : '<div class="mem-empty">没有新日常</div>'}
    </details>
    <details ${importantCount ? 'open' : ''}>
      <summary>新长期重要 ${importantCount}</summary>
      ${importantCount ? renderChatroomDraftItems('important_memories', '长期重要') : '<div class="mem-empty">没有新长期重要</div>'}
    </details>
    <details>
      <summary>旧日常处理 ${oldRowCount}</summary>
      ${oldRowCount ? renderChatroomOldRows() : '<div class="mem-empty">没有旧日常会被处理</div>'}
    </details>
    <div class="chatroom-compress-actions">
      <button class="chatroom-compress-discard" onclick="saveChatroomDailyCompressionDraft(false)" ${actionDisabled}>保存草稿</button>
      <button class="chatroom-compress-discard" onclick="discardChatroomDailyCompressionDraft('${esc(chatroomDailyCompressReview.id)}')" ${actionDisabled}>废弃草稿</button>
      <button class="chatroom-compress-apply" onclick="applyChatroomDailyCompressionDraft('${esc(chatroomDailyCompressReview.id)}')" ${canApply ? '' : 'disabled'}>确认应用</button>
    </div>`;
}

function setChatroomCompressionBusy(value) {
  document.querySelectorAll('.chatroom-compress-actions button').forEach(btn => btn.disabled = value);
  const btn = document.getElementById('chatroomCompressDailyBtn');
  if (btn) btn.disabled = value;
}

function startChatroomCompressionElapsedTimer(onTick) {
  const startedAt = Date.now();
  const tick = () => {
    const seconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
    const minutes = Math.floor(seconds / 60);
    const remainder = seconds % 60;
    onTick(minutes ? `${minutes}分${remainder}秒` : `${remainder}秒`);
  };
  tick();
  const timer = setInterval(tick, 1000);
  return () => clearInterval(timer);
}

async function compressChatroomDailyMemories() {
  if (!currentRoom) return;
  if (_chatroomCompressionRequestBusy) {
    toast('压缩任务仍在处理中，请勿重复操作');
    return;
  }
  if (chatroomDailyCompressReview && chatroomDailyCompressReview.status === 'draft') {
    renderChatroomCompressionReview();
    toast('已经有一份聊天室压缩草稿，先确认应用或废弃它');
    return;
  }
  if (!confirm('将按 15-90 天、90-180 天、180-365 天、365 天以上分阶段生成聊天室压缩草稿；这一步不会修改记忆库。继续？')) return;
  _chatroomCompressionRequestBusy = true;
  const btn = document.getElementById('chatroomCompressDailyBtn');
  setChatroomCompressionBusy(true);
  const stopElapsedTimer = startChatroomCompressionElapsedTimer(elapsed => {
    if (btn) btn.textContent = `生成中，已等待 ${elapsed}`;
  });
  toast('正在生成聊天室压缩草稿...');
  try {
    const result = await memoryCompressionApi('POST', '/api/memories/compress-daily', { target: 'chatroom', days: 15 });
    chatroomDailyCompressReview = result.review || null;
    renderChatroomCompressionReview();
    toast(result.message || '聊天室压缩草稿已生成', 3000);
  } catch (err) {
    toast('生成压缩草稿失败: ' + err.message);
  } finally {
    stopElapsedTimer();
    _chatroomCompressionRequestBusy = false;
    setChatroomCompressionBusy(false);
    if (btn) {
      btn.textContent = '🗜️ 压缩日常';
    }
    if (chatroomDailyCompressReview) renderChatroomCompressionReview();
  }
}

async function applyChatroomDailyCompressionDraft(reviewId) {
  if (!reviewId) return;
  if (_chatroomCompressionRequestBusy) return;
  const chat = chatroomDailyCompressReview?.counts?.chatroom || {};
  if (!confirm(`确认应用这份聊天室压缩草稿？将移除旧日常 ${Number(chat.processed || 0)} 条，并写入新日常 ${Number(chat.created_daily || 0)} 条、新长期重要 ${Number(chat.created_important || 0)} 条。`)) return;
  _chatroomCompressionRequestBusy = true;
  setChatroomCompressionBusy(true);
  const stopElapsedTimer = startChatroomCompressionElapsedTimer(elapsed => {
    const applyBtn = document.querySelector('.chatroom-compress-apply');
    if (applyBtn) {
      applyBtn.disabled = true;
      applyBtn.textContent = `应用中，已等待 ${elapsed}`;
    }
  });
  toast('正在保存并应用聊天室压缩草稿...');
  try {
    await saveChatroomDailyCompressionDraft(true);
    const result = await memoryCompressionApi('POST', `/api/memories/compress-daily/${reviewId}/apply`);
    if (!result.ok) throw new Error(result.message || '应用失败');
    chatroomDailyCompressReview = null;
    renderChatroomCompressionReview();
    await loadMemories();
    toast(result.message || '聊天室压缩草稿已应用', 3000);
  } catch (err) {
    toast('应用压缩草稿失败: ' + err.message);
  } finally {
    stopElapsedTimer();
    _chatroomCompressionRequestBusy = false;
    setChatroomCompressionBusy(false);
    if (chatroomDailyCompressReview) renderChatroomCompressionReview();
  }
}

async function discardChatroomDailyCompressionDraft(reviewId) {
  if (!reviewId) return;
  if (!confirm('确定废弃这份聊天室压缩草稿？')) return;
  setChatroomCompressionBusy(true);
  try {
    const result = await memoryCompressionApi('POST', `/api/memories/compress-daily/${reviewId}/discard`);
    if (!result.ok) throw new Error(result.message || '废弃失败');
    chatroomDailyCompressReview = null;
    renderChatroomCompressionReview();
    setChatroomCompressionBusy(false);
    toast(result.message || '聊天室压缩草稿已废弃');
  } catch (err) {
    toast('废弃压缩草稿失败: ' + err.message);
    setChatroomCompressionBusy(false);
  }
}

// 点击遮罩关闭记忆库
document.getElementById('memoryOverlay').addEventListener('click', (e) => {
  if (e.target.id === 'memoryOverlay') closeMemory();
});

function restoreChatroomMemoryPosition(memId) {
  if (!memId) return;
  requestAnimationFrame(() => {
    const list = document.getElementById('memList');
    const item = list ? Array.from(list.querySelectorAll('.mem-item')).find(el => el.dataset.id === memId) : null;
    if (!item) return;
    item.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'auto' });
    item.classList.add('mem-return-focus');
    setTimeout(() => item.classList.remove('mem-return-focus'), 1200);
  });
}

function chatroomMemoryKind(m) {
  return m?.memory_kind === 'daily' ? 'daily' : 'long_term';
}

function setChatroomMemoryKindFilter(kind) {
  chatroomMemoryKindFilter = kind || 'all';
  document.querySelectorAll('.memory-tab').forEach(btn => {
    const active = btn.dataset.kind === chatroomMemoryKindFilter;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  renderChatroomMemories();
}

function filterChatroomMemories() {
  renderChatroomMemories();
}

function renderChatroomMemories(focusMemId = '') {
  const memListEl = document.getElementById('memList');
  const countBadge = document.getElementById('memCountBadge');
  const query = (document.getElementById('memSearch')?.value || '').trim().toLowerCase();
  const all = Array.isArray(chatroomMemoryCache) ? chatroomMemoryCache : [];
  const kindFiltered = chatroomMemoryKindFilter === 'all'
    ? all
    : all.filter(m => chatroomMemoryKind(m) === chatroomMemoryKindFilter);
  const filtered = query
    ? kindFiltered.filter(m => {
      const kindLabel = m.memory_kind_label || (chatroomMemoryKind(m) === 'daily' ? '日常' : '长期重要');
      return String(m.content || '').toLowerCase().includes(query)
        || String(m.keywords || '').toLowerCase().includes(query)
        || kindLabel.includes(query);
    })
    : kindFiltered;
  const filterText = chatroomMemoryKindFilter === 'daily' ? '日常' : (chatroomMemoryKindFilter === 'long_term' ? '长期重要' : '');
  if (countBadge) {
    countBadge.textContent = `共 ${all.length} 条`
      + (filterText ? `，${filterText} ${kindFiltered.length} 条` : '')
      + ((query || filterText) ? `，当前显示 ${filtered.length} 条` : '');
  }
  if (!all.length) {
    memListEl.innerHTML = '<div class="mem-empty">暂无记忆，可手动添加或总结生成</div>';
    return;
  }
  if (!filtered.length) {
    memListEl.innerHTML = '<div class="mem-empty">没有匹配的记忆</div>';
    return;
  }
  memListEl.innerHTML = filtered.map(m => {
    const date = crFormatMemoryOccurrence(m);
    const kw = m.keywords ? `关键词: ${esc(m.keywords)}` : '';
    const hasSource = Number(m.source_count || 0) > 0;
    const curKind = chatroomMemoryKind(m);
    const kindLabel = m.memory_kind_label || (curKind === 'daily' ? '日常' : '长期重要');
    const kindClass = curKind === 'daily' ? 'daily' : 'long-term';
    const menuOpen = chatroomMemoryKindMenuId === m.id;
    const kindMenu = menuOpen ? `<div class="mem-kind-menu" onclick="event.stopPropagation()">
      <button class="${curKind === 'long_term' ? 'active' : ''}" onclick="selectChatroomMemoryKind('${m.id}','long_term',event)">长期重要</button>
      <button class="${curKind === 'daily' ? 'active' : ''}" onclick="selectChatroomMemoryKind('${m.id}','daily',event)">日常</button>
    </div>` : '';
    const unresolved = Boolean(m.unresolved);
    const importance = m.importance == null
      ? ''
      : `<span class="mem-importance" title="重要程度">${Number(m.importance).toFixed(1)}</span>`;
    return `
      <div class="mem-item${unresolved ? ' mem-unresolved' : ''}" data-id="${m.id}">
        <div class="mem-head">
          <div class="mem-labels">
            <div class="mem-kind-wrap"><button class="mem-kind ${kindClass}" onclick="openChatroomMemoryKindMenu('${m.id}', event)" title="点击选择标签" aria-label="选择记忆标签">${esc(kindLabel)}</button>${kindMenu}</div>
            ${importance}
          </div>
          <div class="mem-actions">
            <button class="mem-pin${unresolved ? ' active' : ''}" onclick="toggleChatroomMemoryUnresolved('${m.id}')" title="${unresolved ? '取消未完成标记' : '标记为未完成'}">📌</button>
            ${hasSource ? `<button onclick="viewMemSource('${m.id}')" title="查看原文">📜</button>` : ''}
            <button onclick="editMemory('${m.id}')" title="编辑">✏️</button>
            <button class="del" onclick="deleteMemory('${m.id}')" title="删除">✕</button>
          </div>
        </div>
        <div class="mem-content">${esc(m.content)}</div>
        <div class="mem-meta">
          ${date ? `<span>${esc(date)}</span>` : ''}
          ${kw ? `<span>${kw}</span>` : ''}
        </div>
      </div>`;
  }).join('');
  restoreChatroomMemoryPosition(focusMemId);
}

async function loadMemories(focusMemId = '') {
  if (!currentRoom) return;
  const memListEl = document.getElementById('memList');
  try {
    const mems = await api(`/rooms/${currentRoom.id}/memories`);
    chatroomMemoryCache = Array.isArray(mems) ? mems : [];
    renderChatroomMemories(focusMemId);
  } catch (err) {
    chatroomMemoryCache = [];
    memListEl.innerHTML = `<div class="mem-empty">加载失败: ${err.message}</div>`;
  }
}

function showAddMemory() {
  document.getElementById('memEditId').value = '';
  document.getElementById('memContent').value = '';
  document.getElementById('memKeywords').value = '';
  document.getElementById('memImportance').value = '0.5';
  const kindEl = document.getElementById('memKind');
  if (kindEl) kindEl.value = 'long_term';
  document.getElementById('memForm').style.display = 'block';
  document.getElementById('memContent').focus();
}

function hideMemForm() {
  document.getElementById('memForm').style.display = 'none';
}

async function editMemory(memId) {
  let mem = chatroomMemoryCache.find(m => m.id === memId);
  if (!mem) {
    const mems = await api(`/rooms/${currentRoom.id}/memories`);
    chatroomMemoryCache = Array.isArray(mems) ? mems : [];
    mem = chatroomMemoryCache.find(m => m.id === memId);
  }
  if (!mem) { toast('找不到该记忆'); return; }

  document.getElementById('memEditId').value = memId;
  document.getElementById('memContent').value = mem.content || '';
  document.getElementById('memKeywords').value = mem.keywords || '';
  document.getElementById('memImportance').value = mem.importance ?? 0.5;
  const kindEl = document.getElementById('memKind');
  if (kindEl) kindEl.value = mem.memory_kind === 'daily' ? 'daily' : 'long_term';
  document.getElementById('memForm').style.display = 'block';
  document.getElementById('memContent').focus();
}

async function saveMemory() {
  if (!currentRoom) return;
  const editId = document.getElementById('memEditId').value;
  const content = document.getElementById('memContent').value.trim();
  if (!content) { toast('内容不能为空'); return; }

  const body = {
    content,
    keywords: document.getElementById('memKeywords').value.trim(),
    importance: parseFloat(document.getElementById('memImportance').value) || 0.5,
    memory_kind: document.getElementById('memKind')?.value === 'daily' ? 'daily' : 'long_term',
  };

  try {
    let result;
    if (editId) {
      result = await api(`/memories/${editId}`, { method: 'PUT', body: JSON.stringify(body) });
    } else {
      result = await api(`/rooms/${currentRoom.id}/memories`, { method: 'POST', body: JSON.stringify(body) });
    }
    if (result && result.error) {
      toast('保存失败: ' + result.error);
      return;
    }
    toast(editId ? '记忆已更新' : '记忆已添加');
    hideMemForm();
    loadMemories(editId || result?.id || '');
  } catch (err) {
    toast('保存失败: ' + err.message);
  }
}

async function deleteMemory(memId) {
  if (!confirm('确定删除此记忆？')) return;
  await api(`/memories/${memId}`, { method: 'DELETE' });
  toast('已删除');
  loadMemories();
}

async function toggleChatroomMemoryUnresolved(memId) {
  try {
    const result = await api(`/memories/${memId}/unresolved`, { method: 'PATCH' });
    if (!result?.ok) {
      toast(result?.message || '切换未完成状态失败');
      return;
    }
    const mem = chatroomMemoryCache.find(item => item.id === memId);
    if (mem) mem.unresolved = result.unresolved;
    renderChatroomMemories(memId);
  } catch (err) {
    toast('切换未完成状态失败: ' + err.message);
  }
}

function openChatroomMemoryKindMenu(memId, event) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }
  chatroomMemoryKindMenuId = chatroomMemoryKindMenuId === memId ? null : memId;
  renderChatroomMemories(memId);
}

async function selectChatroomMemoryKind(memId, nextKind, event) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }
  const mem = chatroomMemoryCache.find(item => item.id === memId);
  if (!mem) return;
  if (chatroomMemoryKind(mem) === nextKind) {
    chatroomMemoryKindMenuId = null;
    renderChatroomMemories(memId);
    return;
  }
  const prevKind = mem.memory_kind;
  const prevLabel = mem.memory_kind_label;
  mem.memory_kind = nextKind;
  mem.memory_kind_label = nextKind === 'daily' ? '日常' : '长期重要';
  chatroomMemoryKindMenuId = null;
  renderChatroomMemories(memId);
  try {
    const result = await api(`/memories/${memId}`, {
      method: 'PUT',
      body: JSON.stringify({ memory_kind: nextKind }),
    });
    if (result && result.error) throw new Error(result.error);
  } catch (err) {
    mem.memory_kind = prevKind;
    mem.memory_kind_label = prevLabel;
    renderChatroomMemories(memId);
    toast('切换记忆标签失败: ' + err.message);
  }
}

async function viewMemSourceLegacy(memId) {
  const overlay = document.getElementById('memSourceOverlay');
  const listEl = document.getElementById('memSourceList');
  overlay.style.display = 'block';
  listEl.innerHTML = '<div class="mem-empty">加载中...</div>';
  try {
    const result = await api(`/memories/${memId}/source`);
    if (!result.ok || !result.messages || !result.messages.length) {
      listEl.innerHTML = `<div class="mem-empty">${result.message || '没有找到原文记录'}</div>`;
      return;
    }
    listEl.innerHTML = result.messages.map(m => {
      const t = m.created_at ? new Date(m.created_at * 1000).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '';
      return `<div style="margin-bottom:10px; padding:8px; background:var(--bubble-other); border-radius:8px;">
        <div style="font-size:12px; color:var(--text2); margin-bottom:4px;">
          <strong>${esc(m.name)}</strong> <span style="margin-left:6px;">${esc(t)}</span>
        </div>
        <div style="white-space:pre-wrap; word-break:break-word; font-size:13px;">${esc(m.content)}</div>
      </div>`;
    }).join('');
  } catch(e) {
    listEl.innerHTML = '<div class="mem-empty">加载失败</div>';
  }
}

async function viewMemSource(memId) {
  memSourceMemId = memId;
  memSourceMessages = [];
  const overlay = document.getElementById('memSourceOverlay');
  const listEl = document.getElementById('memSourceList');
  overlay.style.display = 'block';
  document.getElementById('memSourceSummary').textContent = '';
  listEl.innerHTML = '<div class="mem-empty">加载中...</div>';
  try {
    const result = await api(`/memories/${memId}/source`);
    if (!result.ok || !result.messages || !result.messages.length) {
      listEl.innerHTML = `<div class="mem-empty">${result.message || '没有找到原文记录'}</div>`;
      return;
    }
    memSourceMessages = result.messages.map(m => ({ ...m, selected: !!m.selected, recommended: !!m.recommended }));
    renderMemSourceSelection();
  } catch(e) {
    listEl.innerHTML = '<div class="mem-empty">加载失败</div>';
  }
}

function renderMemSourceSelection() {
  const listEl = document.getElementById('memSourceList');
  if (!memSourceMessages.length) {
    listEl.innerHTML = '<div class="mem-empty">没有找到原文记录</div>';
    return;
  }
  listEl.innerHTML = memSourceMessages.map((m, idx) => {
    const t = m.created_at ? new Date(m.created_at * 1000).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '';
    const opacity = m.selected ? '1' : '0.42';
    const checked = m.selected ? 'checked' : '';
    return `<div style="margin-bottom:10px; padding:8px; background:var(--bubble-other); border-radius:8px; opacity:${opacity};">
      <div style="display:flex; gap:8px; align-items:flex-start;">
        <input type="checkbox" ${checked} onchange="toggleMemSourceMsg(${idx}, this.checked)" style="width:18px;height:18px;margin-top:2px;accent-color:var(--accent);flex-shrink:0;">
        <div style="flex:1;min-width:0;">
          <div style="font-size:12px; color:var(--text2); margin-bottom:4px;">
            <strong>${esc(m.name)}</strong> <span style="margin-left:6px;">${esc(t)}</span>
          </div>
          <div style="white-space:pre-wrap; word-break:break-word; font-size:13px;">${esc(m.content)}</div>
        </div>
      </div>
    </div>`;
  }).join('');
  updateMemSourceSummary();
}

function updateMemSourceSummary() {
  const total = memSourceMessages.length;
  const kept = memSourceMessages.filter(m => m.selected).length;
  document.getElementById('memSourceSummary').textContent = `将保留 ${kept}/${total} 条原文挂载到这条记忆；点击保存前不会写入数据库。`;
}

function toggleMemSourceMsg(index, checked) {
  if (!memSourceMessages[index]) return;
  memSourceMessages[index].selected = checked;
  renderMemSourceSelection();
}

function setMemSourceSelection(mode) {
  if (mode === 'all') memSourceMessages.forEach(m => m.selected = true);
  else if (mode === 'none') memSourceMessages.forEach(m => m.selected = false);
  else memSourceMessages.forEach(m => m.selected = !!m.recommended);
  renderMemSourceSelection();
}

async function saveMemSourceSelection() {
  if (!memSourceMemId) return;
  const selectedIds = memSourceMessages.filter(m => m.selected).map(m => m.id);
  if (!selectedIds.length && !confirm('你没有保留任何原文。保存后这条记忆将不再追溯原文，确定继续吗？')) return;
  const btn = document.getElementById('memSourceSaveBtn');
  btn.disabled = true;
  btn.textContent = '保存中...';
  try {
    const result = await api(`/memories/${memSourceMemId}/source-selection`, {
      method: 'POST',
      body: JSON.stringify({ source_message_ids: selectedIds }),
    });
    if (!result.ok) {
      toast(result.message || '保存失败');
      return;
    }
    toast('原文筛选已保存');
    closeMemSource();
    loadMemories();
  } catch (e) {
    toast('保存失败: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '保存筛选';
  }
}

function closeMemSource() {
  document.getElementById('memSourceOverlay').style.display = 'none';
  memSourceMemId = null;
  memSourceMessages = [];
}

// 点击遮罩关闭设置
document.getElementById('settingsOverlay').addEventListener('click', (e) => {
  if (e.target.id === 'settingsOverlay') closeSettings();
});
['input', 'change'].forEach(eventName => {
  document.getElementById('settingsOverlay').addEventListener(eventName, e => {
    if (e.isTrusted && e.target?.matches('input,select,textarea')) crSettingsDirtyAt = Date.now();
  });
});

// ══════════════════════════════════════════════════
//  Connor 状态
// ══════════════════════════════════════════════════

// ══════════════════════════════════════════════════
//  侧栏
// ══════════════════════════════════════════════════

function openSidebar() { sidebar.classList.add('open'); backdrop.classList.add('active'); }
function closeSidebar() { sidebar.classList.remove('open'); backdrop.classList.remove('active'); }
menuBtn.addEventListener('click', openSidebar);
backdrop.addEventListener('click', closeSidebar);

// ══════════════════════════════════════════════════
//  导航
// ══════════════════════════════════════════════════

function goHome() {
  if (window.parent !== window && typeof window.parent.navigateToHome === 'function') {
    window.parent.navigateToHome();
  } else {
    window.location.href = '/';
  }
}

function crOpenDiary() {
  const diaryUrl = '/diary?return=%2Fchatroom';
  if (window.parent !== window && typeof window.parent.openSubPage === 'function') {
    window.parent.openSubPage(diaryUrl);
  } else {
    window.location.href = diaryUrl;
  }
}

function renderEmptyChat() {
  roomTitleEl.textContent = '聊天室';
  currentRoom = null;
  composer.style.display = 'none';
  updateHeaderActions();
  messagesEl.innerHTML = `
    <div class="empty-state">
      <div class="icon">💬</div>
      <div>选择或创建一个聊天室开始吧</div>
    </div>`;
}

// ══════════════════════════════════════════════════
//  WebSocket 实时同步
// ══════════════════════════════════════════════════

function crRememberSyncSeq(event) {
  const seq = Number(event?.sync_seq || 0);
  if (!seq) return;
  const current = Number(localStorage.getItem(CR_SYNC_SEQ_KEY) || 0);
  if (seq > current) localStorage.setItem(CR_SYNC_SEQ_KEY, String(seq));
}

function crApplySettingsSync(data) {
  const cfg = data?.config || null;
  const room = data?.room || null;
  if (cfg) {
    applyChatroomNames(cfg);
    chatroomModel = cfg.aion_model || chatroomModel;
    chatroomConnorModel = cfg.connor_model || chatroomConnorModel;
    chatroomReplyOrder = cfg.reply_order || chatroomReplyOrder;
    crTtsEnabled = !!(cfg.tts_enabled ?? crTtsEnabled);
    crTtsAionVoice = cfg.tts_aion_voice ?? crTtsAionVoice;
    crTtsConnorVoice = cfg.tts_connor_voice ?? crTtsConnorVoice;
    crApplyAmbientVoiceConfig(cfg);
    updateHeaderActions();
  }
  if (room?.id) {
    rooms = rooms.map(item => item.id === room.id ? { ...item, ...room } : item);
    if (currentRoom?.id === room.id) {
      currentRoom = { ...currentRoom, ...room };
      roomTitleEl.textContent = currentRoom.title;
    }
    crSaveSettingsSnapshot(room, cfg || crCurrentSettingsConfig());
  } else if (currentRoom && cfg) {
    crSaveSettingsSnapshot(currentRoom, cfg);
  }
  if (document.getElementById('settingsOverlay').classList.contains('active')
      && !crSettingsDirtyAt
      && (!data?.room_id || data.room_id === currentRoom?.id)) {
    crPopulateSettings(room || currentRoom, cfg || crCurrentSettingsConfig());
  }
  renderRoomList();
}

function crApplyReplayedSyncEvent(event) {
  // sync_event replay uses the same legacy event shape and remains idempotent.
  if (!event?.type) return;
  if (event.type === 'chatroom_settings_updated') {
    crApplySettingsSync(event.data || {});
    return;
  }
  if (event.type === 'chatroom_room_created' || event.type === 'chatroom_room_updated'
      || event.type === 'chatroom_room_deleted') {
    loadRooms();
    return;
  }
  if (!currentRoom || event.data?.room_id !== currentRoom.id) return;
  const data = event.data || {};
  if (event.type === 'chatroom_msg_created') {
    if (!messagesEl.querySelector(`[data-msg-id="${data.id}"]`) && !document.getElementById(`streaming-${data.id}`)) {
      if (!reconcileLocalUserEcho(data)) appendMessage(data);
    }
  } else if (event.type === 'chatroom_msg_deleted') {
    delete crMessagesById[data.id];
    document.querySelector(`[data-msg-id="${data.id}"]`)?.remove();
  } else if (event.type === 'chatroom_msg_updated') {
    crMessagesById[data.id] = data;
    const row = document.querySelector(`[data-msg-id="${data.id}"]`);
    if (row) {
      const div = document.createElement('div');
      div.innerHTML = msgHTML(data);
      row.replaceWith(div.firstElementChild);
    }
  }
}

async function crReconcileSyncEvents() {
  if (crSyncReplayPromise) return crSyncReplayPromise;
  crSyncReplayPromise = (async () => {
    let after = Number(localStorage.getItem(CR_SYNC_SEQ_KEY) || 0);
    for (let batch = 0; batch < 20; batch++) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 10000);
      let response;
      try {
        response = await fetch(`/api/sync/changes?after=${after}&limit=200`, { signal: controller.signal });
      } finally {
        clearTimeout(timer);
      }
      if (!response.ok) throw new Error(`sync HTTP ${response.status}`);
      const result = await response.json();
      if (result.reset_required) {
        if (currentRoom) refreshCurrentChatroomFromServer();
        const latest = Number(result.latest_seq || 0);
        localStorage.setItem(CR_SYNC_SEQ_KEY, String(latest));
        break;
      }
      const events = Array.isArray(result.events) ? result.events : [];
      events.forEach(event => {
        crApplyReplayedSyncEvent(event);
        crRememberSyncSeq(event);
        after = Math.max(after, Number(event.sync_seq || 0));
      });
      if (!result.has_more || !events.length) break;
    }
  })().catch(e => console.warn('[chatroom sync] reconcile failed:', e))
    .finally(() => { crSyncReplayPromise = null; });
  return crSyncReplayPromise;
}

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws`);
  crWs = ws;

  ws.onopen = () => {
    ws.send(JSON.stringify({ type: 'register_client', client_id: crAmbientClientId }));
    crSendTTSState();
    ws.send(JSON.stringify({ type: 'ping' }));
    crReconcileSyncEvents();
  };

  ws.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      crRememberSyncSeq(data);
      if (data.type === 'pong') return;

      if (data.type === 'hug_pillow_command') {
        window.HugPillowAI?.handleCommandEvent(data.data);
        return;
      }

      if (data.type === 'tts_chunk' && data.data) {
        crEnqueueTTSChunk(data.data.msg_id, data.data.seq, data.data.url, data.data.created_at, data.data.target_client_id, data.data.text);
      }

      if (data.type === 'tts_done' && data.data) {
        crFinishTTSForMsg(data.data.msg_id, data.data.created_at, data.data.target_client_id);
      }

      if (data.type === 'memory_record' && data.data && !isSending && !isAiChatting) {
        crShowMemoryRecordHint(data.data.msg_id, data.data.content);
      }

      if (data.type === 'debug' && data.data) {
        crHandleDebug(data);
      }

      if (data.type === 'chatroom_ai_status' && currentRoom) {
        const d = data.data || {};
        if (d.room_id === currentRoom.id) {
          updateTypingStatus(crName(d.sender || 'aion'), d.text || '处理中...');
        }
      }

      if (data.type === 'ambient_voice_listener' && data.data) {
        crAmbientApplyListenerState(data.data);
      }

      if (data.type === 'chatroom_msg_created' && currentRoom) {
        const msg = data.data;
        if (msg.room_id === currentRoom.id) {
          if (msg.id && msg.sender) crRememberTTSMsgSender(msg.id, msg.sender);
          // 避免重复：流式回复本身已有 streaming 行；异步跟进消息即使还在发送中也要显示。
          const existing = document.getElementById(`streaming-${msg.id}`);
          if (!existing && !messagesEl.querySelector(`[data-msg-id="${msg.id}"]`)) {
            if (!reconcileLocalUserEcho(msg)) {
              appendMessage(msg);
              playRecv();
            }
          }
        }
      }

      if (data.type === 'chatroom_song_gen_start') {
        crHandleSongGenStart(data.data || {});
      }

      if (data.type === 'chatroom_song_gen_done' || data.type === 'chatroom_song_gen_failed') {
        crDismissSongGenIndicator(data.data || {});
      }

      if (data.type === 'chatroom_msg_deleted' && currentRoom) {
        const d = data.data;
        if (d.room_id === currentRoom.id) {
          delete crMessagesById[d.id];
          const row = document.querySelector(`[data-msg-id="${d.id}"]`);
          if (row) row.remove();
        }
      }

      if (data.type === 'chatroom_msg_updated' && currentRoom) {
        const msg = data.data;
        if (msg.room_id === currentRoom.id) {
          crMessagesById[msg.id] = msg;
          const row = document.querySelector(`[data-msg-id="${msg.id}"]`);
          if (row) {
            const div = document.createElement('div');
            div.innerHTML = msgHTML(msg);
            row.replaceWith(div.firstElementChild);
          }
        }
      }

      if (data.type === 'chatroom_settings_updated' && data.data) {
        crApplySettingsSync(data.data);
      }

      if (data.type === 'wish_updated' && data.data) {
        document.querySelectorAll('.wish-fulfill-card').forEach(card => {
          if (card.dataset.wishId === data.data.id) crApplyWishCardStatus(card, data.data.status || 'active');
        });
      }

      if (data.type === 'chatroom_room_created' || data.type === 'chatroom_room_deleted') {
        loadRooms();
      } else if (data.type === 'chatroom_room_updated' && data.data) {
        rooms = rooms.map(room => room.id === data.data.id ? { ...room, ...data.data } : room);
        if (currentRoom?.id === data.data.id) currentRoom = { ...currentRoom, ...data.data };
        renderRoomList();
      }

      // 音乐广播（来自 WS broadcast）— 仅在非发送状态下处理（发送时 SSE 已处理）
      if (data.type === 'music' && data.data && !isSending && !isAiChatting) {
        const d = data.data;
        if (d.msg_id && d.cards) {
          crMusicCards[d.msg_id] = d.cards;
          crRenderMusicCards(d.msg_id);
          scrollToBottom();
          if (d.autoplay && d.cards.length) crPlayMusicOnline(d.cards[0].id);
        }
      }

      // 玩具指令广播
      if (data.type === 'toy_command' && data.data) {
        crHandleToyCommand(data.data);
      }

      // Connor 钱包余额变动 → 自动刷新钱包面板
      if (data.type === 'connor_wallet_update') {
        if (document.getElementById('crWalletPanelOverlay').classList.contains('show')) {
          crOpenWalletPanel();
        }
      }
    } catch {}
  };

  ws.onclose = () => {
    if (crWs === ws) crWs = null;
    setTimeout(connectWS, 3000);
  };
  ws.onerror = () => ws.close();
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') crReconcileSyncEvents();
});

// ══════════════════════════════════════════════════
//  图片上传 & 预览 & 查看器
// ══════════════════════════════════════════════════

function crLuckinOrderStatusHtml(data) {
  const code = data && data.take_meal_code ? String(data.take_meal_code) : "";
  const takeOrderId = data && data.take_order_id ? String(data.take_order_id) : "";
  const status = data && data.order_status_name
    ? String(data.order_status_name)
    : (data && data.order_status !== undefined && data.order_status !== null ? `状态 ${data.order_status}` : "");
  const time = data && (data.take_meal_time || data.about_time) ? String(data.take_meal_time || data.about_time) : "";
  if (code) {
    const orderLine = takeOrderId ? `<div style="font-size:11px;color:rgba(255,255,255,.58);margin-top:3px">取餐序号：${esc(takeOrderId)}</div>` : "";
    const statusLine = status ? `<div style="font-size:11px;color:rgba(255,255,255,.58);margin-top:3px">${esc(status)}</div>` : "";
    return `<div style="font-size:13px;color:#fff;margin-top:8px">取餐码：<b style="font-size:18px;letter-spacing:.5px">${esc(code)}</b></div>${orderLine}${statusLine}`;
  }
  const main = status ? `当前状态：${status}` : "暂时还没有取餐码";
  const hint = time ? `预计：${time}` : "支付后稍等再查一次";
  return `<div style="font-size:12px;color:rgba(255,255,255,.72);margin-top:8px;line-height:1.35">${esc(main)}</div><div style="font-size:11px;color:rgba(255,255,255,.52);margin-top:3px">${esc(hint)}</div>`;
}

async function crQueryLuckinOrderStatus(btn) {
  const orderId = btn?.dataset?.orderId || "";
  const card = btn?.closest('.luckin-pay-card');
  const statusEl = card?.querySelector('.luckin-order-status');
  if (!orderId || !statusEl) return;
  const oldText = btn.textContent || "查询取餐码";
  btn.disabled = true;
  btn.textContent = "查询中...";
  statusEl.innerHTML = '<div style="font-size:12px;color:rgba(255,255,255,.72);margin-top:8px">正在查询订单状态...</div>';
  try {
    const res = await fetch(`${API}/luckin/order/${encodeURIComponent(orderId)}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.error) throw new Error(data.detail || data.error || "查询失败");
    statusEl.innerHTML = crLuckinOrderStatusHtml(data);
  } catch (err) {
    statusEl.innerHTML = `<div style="font-size:12px;color:#ffd1d1;margin-top:8px;line-height:1.35">${esc(err.message || "查询失败")}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = oldText;
  }
}

function buildLuckinPaymentCard(item) {
  const title = esc(item.title || "瑞幸咖啡订单");
  const shop = item.shop ? `<div style="font-size:12px;color:rgba(255,255,255,.72);margin-top:2px">${esc(item.shop)}</div>` : "";
  const address = item.address ? `<div style="font-size:12px;color:rgba(255,255,255,.6);margin-top:2px;line-height:1.35">${esc(item.address)}</div>` : "";
  const amount = item.amount ? `<div style="font-size:13px;color:#fff;margin-top:6px">待支付：${esc(item.amount)}</div>` : "";
  const orderId = item.order_id ? `<div style="font-size:11px;color:rgba(255,255,255,.55);margin-top:2px">订单号：${esc(item.order_id)}</div>` : "";
  const hasSpecWarning = /未匹配|切换失败/.test(item.note || "");
  const noteLabel = hasSpecWarning ? "规格提醒" : "备注";
  const noteColor = hasSpecWarning ? "#ffe1a8" : "rgba(255,255,255,.66)";
  const note = item.note ? `<div style="font-size:11px;color:${noteColor};margin-top:6px;line-height:1.35">${noteLabel}：${esc(item.note)}</div>` : "";
  const qrUrl = item.qr_url || item.url || "";
  const qr = qrUrl ? `<img src="${esc(qrUrl)}" onclick="openImageViewer(this.src)" style="width:168px;max-width:100%;border-radius:8px;background:#fff;padding:8px;display:block;margin:10px auto 6px;cursor:pointer">` : "";
  const payUrl = item.pay_url || "";
  const payButton = payUrl ? `<a href="${esc(payUrl)}" target="_blank" rel="noopener" style="display:inline-flex;align-items:center;justify-content:center;padding:7px 12px;border-radius:999px;background:rgba(255,255,255,.16);color:#fff;text-decoration:none;font-size:13px">打开支付页</a>` : "";
  const queryButton = item.order_id ? `<button type="button" data-order-id="${esc(item.order_id)}" onclick="crQueryLuckinOrderStatus(this)" style="border:none;display:inline-flex;align-items:center;justify-content:center;padding:7px 12px;border-radius:999px;background:rgba(75,210,176,.24);color:#fff;font-size:13px;cursor:pointer">查询取餐码</button>` : "";
  const buttons = payButton || queryButton ? `<div style="display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin-top:8px">${payButton}${queryButton}</div>` : "";
  return `<div class="luckin-pay-card" style="margin-top:8px;padding:12px;border:1px solid rgba(75,210,176,.45);background:rgba(16,86,76,.42);border-radius:10px;max-width:260px">
    <div style="font-weight:700;color:#fff">瑞幸订单 · 扫码确认支付</div>
    <div style="font-size:13px;color:#fff;margin-top:4px">${title}</div>
    ${shop}${address}${amount}${orderId}${note}${qr}<div class="luckin-order-status"></div>${buttons}
  </div>`;
}

function crEscJsSingle(s) {
  return String(s || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\r/g, '').replace(/\n/g, '\\n');
}

const CR_WISH_FULFILLMENT_MARK_RE = /\u2063wish_fulfillment:([A-Za-z0-9_-]+)\u2063/;

function crStripWishFulfillmentMarker(text) {
  return String(text || '').replace(CR_WISH_FULFILLMENT_MARK_RE, '');
}

function crWishFulfillmentFromContent(text) {
  const raw = String(text || '');
  const marker = raw.match(CR_WISH_FULFILLMENT_MARK_RE);
  if (!marker) return null;
  const clean = crStripWishFulfillmentMarker(raw).trim();
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

function crWithWishFallbackAttachments(message) {
  const atts = Array.isArray(message?.attachments) ? message.attachments : [];
  if (atts.some(item => item && typeof item === 'object' && item.type === 'wish_fulfillment')) return atts;
  const fallback = crWishFulfillmentFromContent(message?.content || '');
  return fallback ? [...atts, fallback] : atts;
}

function crWishCardStatusLabel(status) {
  return status === 'fulfilled' ? '已完成' : '池中';
}

function crApplyWishCardStatus(card, status) {
  const next = status === 'fulfilled' ? 'fulfilled' : 'active';
  card.dataset.status = next;
  const stateEl = card.querySelector('.wish-card-state');
  if (stateEl) stateEl.textContent = crWishCardStatusLabel(next);
  const hint = card.querySelector('.wish-card-hint');
  if (hint) hint.textContent = next === 'fulfilled' ? '愿望已标记完成' : '愿望已放回池中';
  card.querySelectorAll('[data-wish-action]').forEach(btn => {
    const action = btn.dataset.wishAction;
    btn.disabled = (action === next);
  });
}

async function crSetWishCardStatus(btn, wishId, status) {
  const card = btn?.closest('.wish-fulfill-card');
  if (!wishId || !card) return;
  if (status === 'active') {
    const hint = card.querySelector('.wish-card-hint');
    if (hint) hint.textContent = card.dataset.status === 'fulfilled' ? '愿望已经完成，记录保持不变' : '愿望还在池中';
    toast('愿望仍在池中');
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
      if (item.dataset.wishId === wishId) crApplyWishCardStatus(item, updated.status || status);
    });
    toast(status === 'fulfilled' ? '愿望已完成' : '愿望已放回池中');
  } catch (e) {
    if (hint) hint.textContent = '更新失败，稍后再试';
    buttons.forEach(item => { item.disabled = false; });
    toast('更新失败');
  }
}

function crBuildWishFulfillmentCard(item) {
  const wishId = String(item.wish_id || '');
  const status = item.status === 'fulfilled' ? 'fulfilled' : 'active';
  const idArg = crEscJsSingle(wishId);
  const activeDisabled = '';
  const fulfilledDisabled = status === 'fulfilled' ? ' disabled' : '';
  const authorName = esc(item.author_name || '许愿者');
  const content = esc(item.content || crStripWishFulfillmentMarker(item.message || ''));
  return `<div class="wish-fulfill-card" data-wish-id="${esc(wishId)}" data-status="${status}">
    <div class="wish-card-head">
      <span>许愿池愿望</span>
      <span class="wish-card-state">${crWishCardStatusLabel(status)}</span>
    </div>
    <div class="wish-card-from">来自【${authorName}】</div>
    <div class="wish-card-content">${content}</div>
    <div class="wish-card-actions">
      <button type="button" data-wish-action="active" onclick="crSetWishCardStatus(this,'${idArg}','active')"${activeDisabled}>放回池中</button>
      <button type="button" data-wish-action="fulfilled" onclick="crSetWishCardStatus(this,'${idArg}','fulfilled')"${fulfilledDisabled}>已完成</button>
    </div>
    <div class="wish-card-hint"></div>
  </div>`;
}

const crGeneratedSongPlayerStore = {};
let crGeneratedSongPlayerSeq = 0;
let crGeneratedSongAudio = null;
let crGeneratedSongProgressFrame = null;

function crRegisterGeneratedSongItem(item) {
  const key = `cr_song_${Date.now()}_${crGeneratedSongPlayerSeq++}`;
  crGeneratedSongPlayerStore[key] = item || {};
  return key;
}

function crExtractGeneratedSongLyrics(item) {
  const direct = (item && item.lyrics) ? String(item.lyrics).trim() : '';
  if (direct) return direct;
  const prompt = (item && item.prompt) ? String(item.prompt) : '';
  const match = prompt.match(/^\s*Lyrics\s*:\s*([\s\S]+)$/im);
  if (match && match[1].trim()) return match[1].trim();
  const desc = (item && item.description) ? String(item.description).trim() : '';
  return desc || '';
}

function crFormatGeneratedSongTime(seconds) {
  const sec = Number.isFinite(seconds) && seconds > 0 ? seconds : 0;
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

function crCloseGeneratedSongPlayer() {
  if (crGeneratedSongProgressFrame) {
    cancelAnimationFrame(crGeneratedSongProgressFrame);
    crGeneratedSongProgressFrame = null;
  }
  if (crGeneratedSongAudio) {
    crGeneratedSongAudio.pause();
    crGeneratedSongAudio.src = '';
    crGeneratedSongAudio = null;
  }
  const overlay = document.getElementById('crGeneratedSongPlayerOverlay');
  if (overlay) overlay.remove();
}

function crOpenGeneratedSongPlayer(key) {
  const item = crGeneratedSongPlayerStore[key];
  if (!item || !item.url) return;
  crCloseGeneratedSongPlayer();

  const title = item.title || 'AI 生成歌曲';
  const model = item.model || 'lyria-3-pro-preview';
  const lyrics = crExtractGeneratedSongLyrics(item);
  const overlay = document.createElement('div');
  overlay.id = 'crGeneratedSongPlayerOverlay';
  overlay.className = 'song-player-overlay';
  overlay.innerHTML = `
    <div class="song-player-sheet" role="dialog" aria-modal="true" aria-label="歌曲播放器">
      <button class="song-player-close" type="button" aria-label="关闭">×</button>
      <div class="song-player-head">
        <div class="song-player-cover" aria-hidden="true"><span></span></div>
        <div class="song-player-info">
          <div class="song-player-kicker">Generated Song</div>
          <div class="song-player-title">${esc(title)}</div>
          <div class="song-player-meta">${esc(model)}</div>
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
      <div class="song-player-lyrics">${lyrics ? esc(lyrics) : '<span class="song-player-empty">暂无歌词</span>'}</div>
    </div>
  `;
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) crCloseGeneratedSongPlayer();
  });
  document.body.appendChild(overlay);
  requestAnimationFrame(() => overlay.classList.add('active'));

  const audio = new Audio(item.url);
  crGeneratedSongAudio = audio;
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
    durationEl.textContent = crFormatGeneratedSongTime(duration);
    currentEl.textContent = crFormatGeneratedSongTime(current);
  }

  function stopProgressLoop() {
    if (crGeneratedSongProgressFrame) {
      cancelAnimationFrame(crGeneratedSongProgressFrame);
      crGeneratedSongProgressFrame = null;
    }
  }

  function startProgressLoop() {
    stopProgressLoop();
    const tick = () => {
      updateProgress();
      if (crGeneratedSongAudio === audio && !audio.paused && !audio.ended) {
        crGeneratedSongProgressFrame = requestAnimationFrame(tick);
      }
    };
    crGeneratedSongProgressFrame = requestAnimationFrame(tick);
  }

  overlay.querySelector('.song-player-close')?.addEventListener('click', crCloseGeneratedSongPlayer);
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

function crBuildGeneratedSongCard(item) {
  const key = crRegisterGeneratedSongItem(item);
  const keyArg = crEscJsSingle(key);
  const url = esc(item.url || '');
  const title = esc(item.title || 'AI 生成歌曲');
  const model = esc(item.model || 'lyria-3-pro-preview');
  const mime = esc(item.mime_type || 'audio/mpeg');
  return `<div class="generated-song-card" data-song-key="${esc(key)}">
    <div class="generated-song-main">
      <div class="generated-song-icon" aria-hidden="true"><span></span></div>
      <div class="generated-song-copy">
        <div class="generated-song-title">${title}</div>
        <div class="generated-song-meta">${model}</div>
      </div>
    </div>
    <div class="generated-song-actions">
      <button type="button" class="generated-song-open" onclick="crOpenGeneratedSongPlayer('${keyArg}')">打开播放器</button>
      <audio controls preload="metadata" src="${url}" type="${mime}"></audio>
    </div>
  </div>`;
}

function buildDateSummaryCard(item) {
  const title = esc(item.title || '约会');
  const summary = esc(item.summary || '');
  return `<div class="date-summary-card">
    <div class="date-summary-kicker">刚刚完成了约会</div>
    <div class="date-summary-title">${title}</div>
    ${summary ? `<div class="date-summary-text">${summary}</div>` : ''}
  </div>`;
}

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
  try {
    if (typeof _getNativeBridge === 'function') return _getNativeBridge('AionImageSaver');
  } catch (e) {}
  try { if (window.AionImageSaver) return window.AionImageSaver; } catch (e) {}
  try { if (window.parent && window.parent.AionImageSaver) return window.parent.AionImageSaver; } catch (e) {}
  try { if (window.top && window.top.AionImageSaver) return window.top.AionImageSaver; } catch (e) {}
  return null;
}

function saveImage(url) {
  fetch(url)
    .then(r => r.blob())
    .then(blob => {
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

function crSafeHttpUrl(value) {
  try {
    const url = new URL(value || '', window.location.href);
    return (url.protocol === 'http:' || url.protocol === 'https:') ? url.href : '';
  } catch (e) {
    return '';
  }
}

function crGetExternalLinkBridge() {
  try { if (window.AionExternal) return window.AionExternal; } catch (e) {}
  try { if (window.parent && window.parent.AionExternal) return window.parent.AionExternal; } catch (e) {}
  try { if (window.top && window.top.AionExternal) return window.top.AionExternal; } catch (e) {}
  return null;
}

function crOpenExternalLink(event, href) {
  const url = crSafeHttpUrl(href);
  if (!url) return true;
  const bridge = crGetExternalLinkBridge();
  if (!bridge || typeof bridge.open !== 'function') return true;
  if (event && typeof event.preventDefault === 'function') event.preventDefault();
  bridge.open(url);
  return false;
}

function crBuildLinkPreviewCard(item) {
  const href = crSafeHttpUrl(item?.url || '');
  if (!href) return '';
  let host = '';
  try { host = new URL(href).hostname.replace(/^www\./, ''); } catch (e) {}
  const title = esc(item.title || item.site_name || host || href);
  const description = esc(item.description || '');
  const source = esc(item.site_name || host || '网页链接');
  const image = crSafeHttpUrl(item.image || '');
  const favicon = crSafeHttpUrl(item.favicon || '');
  const thumb = image
    ? `<img class="link-preview-thumb-img" src="${esc(image)}" alt="" loading="lazy">`
    : `<div class="link-preview-thumb-placeholder">URL</div>`;
  const icon = favicon ? `<img class="link-preview-favicon" src="${esc(favicon)}" alt="" loading="lazy">` : '';
  const hrefArg = crEscJsSingle(href);
  return `<a class="link-preview-card" href="${esc(href)}" target="_blank" rel="noopener noreferrer" onclick="return crOpenExternalLink(event,'${hrefArg}')">
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
  let html = '';
  let musicHtml = '';
  let wishHtml = '';
  atts.forEach(item => {
    const url = typeof item === 'string' ? item : (item.url || '');
    const type = (typeof item === 'object' && item.type) || '';
    if (type === 'luckin_payment') {
      html += buildLuckinPaymentCard(item);
    } else if (type === 'date_summary') {
      html += buildDateSummaryCard(item);
    } else if (type === 'link_preview') {
      html += crBuildLinkPreviewCard(item);
    } else if (type === 'wish_fulfillment') {
      wishHtml += crBuildWishFulfillmentCard(item);
    } else if (type === 'music') {
      musicHtml += crBuildMusicCardHtml(item);
    } else if (type === 'generated_song') {
      html += crBuildGeneratedSongCard(item);
    } else if (type === 'toy') {
      return;
    } else if (type === 'voice') {
      const dur = item.duration || 0;
      const durStr = dur < 60 ? `${Math.round(dur)}"` : `${Math.floor(dur/60)}'${Math.round(dur%60)}"`;
      const waveBars = Array.from({length: 6}, () => `<span style="height:${4 + Math.random()*14}px"></span>`).join('');
      html += `<div class="voice-bubble" onclick="crPlayVoice(this,'${esc(url)}')">
        <span class="vb-play">▶</span>
        <span class="vb-wave">${waveBars}</span>
        <span class="vb-dur">${durStr}</span>
      </div>`;
      if (item.transcript) html += `<div class="vb-transcript">${esc(item.transcript)}</div>`;
    } else if (url) {
      if (/\.(mp3|wav|m4a|aac|ogg)$/i.test(url)) {
        html += `<audio src="${esc(url)}" controls preload="metadata"></audio>`;
      } else {
        html += `<img src="${esc(url)}" ${imageInteractionAttrs()}>`;
      }
    }
  });
  const musicBlock = musicHtml ? '<div class="music-cards-container">' + musicHtml + '</div>' : '';
  const wishBlock = wishHtml || '';
  const mediaBlock = html ? '<div class="msg-media">' + html + '</div>' : '';
  return musicBlock + wishBlock + mediaBlock;
}

async function handleChatroomFileSelect(input) {
  for (const file of input.files) {
    const fd = new FormData();
    fd.append('file', file);
    try {
      const res = await fetch(`${API}/upload`, { method: 'POST', body: fd });
      const data = await res.json();
      if (data.error) { toast(data.error); continue; }
      pendingAttachments.push(data);
    } catch (err) {
      toast('上传失败: ' + err.message);
    }
  }
  input.value = '';
  renderPreview();
}

function renderPreview() {
  const area = document.getElementById('previewArea');
  if (!pendingAttachments.length) { area.className = 'preview-area'; area.innerHTML = ''; return; }
  area.className = 'preview-area has-files';
  area.innerHTML = pendingAttachments.map((a, i) => {
    return `<div class="preview-item"><img src="${a.url}"><button class="preview-remove" onclick="removeChatroomAttachment(${i})">✕</button></div>`;
  }).join('');
}

function removeChatroomAttachment(i) {
  pendingAttachments.splice(i, 1);
  renderPreview();
}

function openImageViewer(src) {
  const viewer = document.getElementById('imageViewer');
  const viewerImg = document.getElementById('viewerImg');
  viewerImg.src = src;
  bindImageSaveOnly(document.getElementById('viewerImg'), closeImageViewer);
  viewer.classList.add('active');
}

function closeImageViewer() {
  document.getElementById('imageViewer').classList.remove('active');
}

// 文件选择绑定
document.getElementById('fileInput').addEventListener('change', function() {
  handleChatroomFileSelect(this);
});

// 粘贴图片
inputEl.addEventListener('paste', async (e) => {
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return;
  for (const item of items) {
    if (!item.type.startsWith('image/')) continue;
    e.preventDefault();
    const file = item.getAsFile();
    if (!file) continue;
    const fd = new FormData();
    fd.append('file', file);
    try {
      const res = await fetch(`${API}/upload`, { method: 'POST', body: fd });
      const data = await res.json();
      if (data.error) { toast(data.error); continue; }
      pendingAttachments.push(data);
      renderPreview();
    } catch (err) {
      toast('粘贴上传失败: ' + err.message);
    }
  }
});

// ESC 关闭图片查看器
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeImageViewer();
});

// ══════════════════════════════════════════════════
//  转义
// ══════════════════════════════════════════════════

function esc(str) {
  if (!str) return '';
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function crRenderInnerMonologues(html) {
  return String(html || '').replace(/\[心里嘀咕[：:]\s*([^\]]+?)\]/g, (_, content) =>
    `<span class="inner-monologue">${content.trim()}</span>`
  );
}

function crInnerMonologueText(s) {
  const match = String(s || '').match(/^\s*\[心里嘀咕[：:]\s*([^\]]+?)\]\s*$/);
  return match ? match[1].trim() : null;
}

function crRenderMessagePart(p, fmt) {
  const monologue = crInnerMonologueText(p);
  if (monologue !== null) return `<div class="inner-monologue-line">${esc(monologue)}</div>`;
  return `<div class="bubble">${fmt(p)}</div>`;
}

/** 渲染 [转账给XXX：N元] 或 [转账：N元] 为微信风格转账卡片 */
function renderTransferCards(html) {
  const transferRe = /\[\u8f6c\u8d26(?:\u7ed9([^\uff1a:]+?))?[\uff1a:]\s*(-?\d+(?:\.\d+)?)\s*\u5143\]/g;
  return html.replace(transferRe, (match, recipient, amount) => {
    const val = parseFloat(amount);
    const isNeg = val < 0;
    const absVal = Math.abs(val);
    const targetName = recipient ? recipient.trim() : '';
    if (isNeg) {
      return `<div class="transfer-card deduct"><div class="transfer-card-icon-wrap"><svg viewBox="0 0 40 40" width="28" height="28"><circle cx="20" cy="20" r="18" fill="none" stroke="#fff" stroke-width="2.5"/><line x1="14" y1="14" x2="26" y2="26" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/><line x1="26" y1="14" x2="14" y2="26" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/></svg></div><div class="transfer-card-body"><div class="transfer-card-amount">¥${absVal}</div><div class="transfer-card-desc">钱包扣除${targetName ? '（' + targetName + '）' : ''}</div></div><div class="transfer-card-footer">扣除</div></div>`;
    } else {
      const descText = targetName ? `转账给${targetName}` : '发起了一笔转账';
      return `<div class="transfer-card"><div class="transfer-card-icon-wrap"><svg viewBox="0 0 40 40" width="28" height="28"><circle cx="20" cy="20" r="18" fill="none" stroke="#fff" stroke-width="2.5"/><path d="M12 17h12M24 17l-3-3" stroke="#fff" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M28 23H16M16 23l3 3" stroke="#fff" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></div><div class="transfer-card-body"><div class="transfer-card-amount">¥${absVal}</div><div class="transfer-card-desc">${descText}</div></div><div class="transfer-card-footer">转账</div></div>`;
    }
  });
}

/** 转义文本并渲染转账卡片（用户消息） */
function escWithTransfer(str) {
  if (!str) return '';
  return crRenderInnerMonologues(renderTransferCards(esc(str)));
}

/** 将文本中的 [[image:...]] 标记渲染为 <img>，[转账：N元] 渲染为卡片，其余部分转义 */
function escWithImages(str) {
  if (!str) return '';
  const imgRe = /\[\[image:(\S+?)\]\]/g;
  let result = '';
  let lastIdx = 0;
  let match;
  while ((match = imgRe.exec(str)) !== null) {
    const before = str.slice(lastIdx, match.index);
    if (before) result += esc(before);
    // Connor 端 /uploads/ 在聊天室对应 /cr-uploads/
    let imgUrl = match[1];
    if (imgUrl.startsWith('/uploads/')) imgUrl = '/cr-uploads/' + imgUrl.slice('/uploads/'.length);
    const safeUrl = esc(imgUrl);
    result += `<img class="cr-inline-img" src="${safeUrl}" ${imageInteractionAttrs()} loading="lazy">`;
    lastIdx = imgRe.lastIndex;
  }
  const tail = str.slice(lastIdx);
  if (tail) result += esc(tail);
  return crRenderInnerMonologues(renderTransferCards(result));
}

// ══════════════════════════════════════════════════
//  ＋ 展开菜单
// ══════════════════════════════════════════════════

function crTogglePlusMenu() {
  const m = document.getElementById('crPlusMenu');
  m.classList.toggle('show');
}
function crClosePlusMenu() {
  document.getElementById('crPlusMenu').classList.remove('show');
}
document.addEventListener('click', e => {
  const wrap = document.querySelector('.plus-menu-wrap');
  const menu = document.getElementById('crPlusMenu');
  if (menu && (wrap?.contains(e.target) || menu.contains(e.target))) return;
  crClosePlusMenu();
});

// ══════════════════════════════════════════════════
//  Android 原生桥接（iframe 穿透）
// ══════════════════════════════════════════════════
// 聊天室可能在 iframe 中加载，原生桥注入在顶层 WebView，需要穿透访问
function _getNativeBridge(name) {
  try { if (window[name]) return window[name]; } catch(e) {}
  try { if (window.parent && window.parent[name]) return window.parent[name]; } catch(e) {}
  try { if (window.top && window.top[name]) return window.top[name]; } catch(e) {}
  return null;
}

// ══════════════════════════════════════════════════
//  拍照功能
// ══════════════════════════════════════════════════

let _crCamOverlay = null;
let _crCamStream = null;
let _crCamUseNative = false;
let _crCamNativeTimer = null;
let _crCamFacing = 'environment';

function crOpenCamera() {
  if (_crCamOverlay) _crCamOverlay.remove();
  _crCamFacing = 'environment';
  _crCamOverlay = document.createElement('div');
  _crCamOverlay.className = 'camera-overlay show';
  _crCamOverlay.innerHTML = `
    <div class="camera-preview">
      <video id="crCamVideo" autoplay playsinline muted></video>
      <img id="crCamImg" style="display:none">
    </div>
    <div class="camera-bar">
      <button class="cam-close-btn" onclick="crCloseCamera()">✕</button>
      <button class="cam-shutter-btn" onclick="crCapturePhoto()">📷</button>
      <button class="cam-flip-btn" onclick="crFlipCam()">🔄</button>
    </div>
  `;
  document.body.appendChild(_crCamOverlay);
  _crStartCam();
}

async function _crStartCam() {
  try {
    _crCamStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: _crCamFacing, width: { ideal: 640 }, height: { ideal: 480 } },
      audio: false
    });
    const vid = document.getElementById('crCamVideo');
    if (vid) { vid.srcObject = _crCamStream; vid.style.transform = _crCamFacing === 'user' ? 'scaleX(-1)' : 'none'; vid.style.display = 'block'; vid.play().catch(()=>{}); }
    const img = document.getElementById('crCamImg');
    if (img) img.style.display = 'none';
    _crCamUseNative = false;
    return;
  } catch (e) { console.warn('[CR-Camera] getUserMedia failed:', e); }
  const _cam = _getNativeBridge('AionCamera');
  if (_cam) {
    const ok = _cam.start(_crCamFacing === 'user' ? 'user' : 'environment');
    if (ok) {
      _crCamUseNative = true;
      const vid = document.getElementById('crCamVideo');
      const img = document.getElementById('crCamImg');
      if (vid) vid.style.display = 'none';
      if (img) { img.style.display = 'block'; img.style.transform = _crCamFacing === 'user' ? 'scaleX(-1)' : 'none'; }
      _crPollCamFrame();
      return;
    }
  }
  alert('无法打开摄像头');
  crCloseCamera();
}

function _crPollCamFrame() {
  const _cam = _getNativeBridge('AionCamera');
  if (!_crCamUseNative || !_cam) return;
  const frame = _cam.getFrame();
  if (frame) { const img = document.getElementById('crCamImg'); if (img) img.src = 'data:image/jpeg;base64,' + frame; }
  _crCamNativeTimer = requestAnimationFrame(_crPollCamFrame);
}

function _crStopCam() {
  if (_crCamNativeTimer) { cancelAnimationFrame(_crCamNativeTimer); _crCamNativeTimer = null; }
  if (_crCamUseNative) { const _cam = _getNativeBridge('AionCamera'); if (_cam) _cam.stop(); _crCamUseNative = false; }
  if (_crCamStream) { _crCamStream.getTracks().forEach(t => t.stop()); _crCamStream = null; }
}

function crCloseCamera() {
  _crStopCam();
  if (_crCamOverlay) { _crCamOverlay.remove(); _crCamOverlay = null; }
}

async function crFlipCam() {
  _crCamFacing = _crCamFacing === 'environment' ? 'user' : 'environment';
  if (_crCamUseNative) {
    const _cam = _getNativeBridge('AionCamera');
    if (_cam) _cam.flip();
    const img = document.getElementById('crCamImg');
    if (img) img.style.transform = _crCamFacing === 'user' ? 'scaleX(-1)' : 'none';
  } else {
    _crStopCam();
    await _crStartCam();
  }
}

async function crCapturePhoto() {
  let dataUrl = null;
  if (_crCamUseNative) {
    const _cam = _getNativeBridge('AionCamera');
    if (_cam) { const b64 = _cam.capture(); if (b64) dataUrl = 'data:image/jpeg;base64,' + b64; }
  } else if (_crCamStream) {
    const videoEl = document.getElementById('crCamVideo');
    if (videoEl) {
      const canvas = document.createElement('canvas');
      canvas.width = videoEl.videoWidth || 640;
      canvas.height = videoEl.videoHeight || 480;
      canvas.getContext('2d').drawImage(videoEl, 0, 0, canvas.width, canvas.height);
      dataUrl = canvas.toDataURL('image/jpeg', 0.85);
    }
  }
  if (!dataUrl) { alert('拍照失败'); return; }
  crCloseCamera();
  const resp = await fetch(dataUrl);
  const blob = await resp.blob();
  const fd = new FormData();
  fd.append('file', blob, 'photo_' + Date.now() + '.jpg');
  try {
    const res = await fetch(`${API}/upload`, { method: 'POST', body: fd });
    const data = await res.json();
    if (data.error) { toast(data.error); return; }
    pendingAttachments.push(data);
    renderPreview();
  } catch (err) { toast('上传失败: ' + err.message); }
}

// ══════════════════════════════════════════════════
//  语音消息
// ══════════════════════════════════════════════════

let _crVoiceMode = false;
let _crVoiceRecording = false;
let _crVoiceMediaRecorder = null;
let _crVoiceStream = null;
let _crVoiceChunks = [];
let _crVoiceStartTime = 0;
let _crVoiceTimerInterval = null;
let _crVoiceOverlay = null;
let _crVoiceCancelled = false;
let _crVoiceNativeChunks = [];
let _crVoiceUseNative = false;
let _crVoiceResumeAmbient = false;

// 语音消息播放
let _crVoiceAudio = null;
function crPlayVoice(el, url) {
  if (_crVoiceAudio && el.classList.contains('playing')) {
    _crVoiceAudio.pause(); _crVoiceAudio = null;
    el.classList.remove('playing'); el.querySelector('.vb-play').textContent = '▶';
    return;
  }
  document.querySelectorAll('.voice-bubble.playing').forEach(b => {
    b.classList.remove('playing'); b.querySelector('.vb-play').textContent = '▶';
  });
  if (_crVoiceAudio) { _crVoiceAudio.pause(); _crVoiceAudio = null; }
  _crVoiceAudio = new Audio(url);
  el.classList.add('playing'); el.querySelector('.vb-play').textContent = '⏸';
  _crVoiceAudio.play().catch(()=>{});
  _crVoiceAudio.onended = () => { el.classList.remove('playing'); el.querySelector('.vb-play').textContent = '▶'; _crVoiceAudio = null; };
}

function crToggleVoiceMode() {
  _crVoiceMode = !_crVoiceMode;
  const composerEl = document.getElementById('composer');
  const voiceRow = document.getElementById('crVoiceModeRow');
  if (_crVoiceMode) {
    composerEl.style.display = 'none';
    voiceRow.classList.add('active');
    _crInitVoiceHoldBtn();
  } else {
    if (currentRoom) composerEl.style.display = 'flex';
    voiceRow.classList.remove('active');
  }
}

function _crInitVoiceHoldBtn() {
  const btn = document.getElementById('crVoiceHoldBtn');
  if (btn._inited) return;
  btn._inited = true;
  btn.addEventListener('mousedown', e => { e.preventDefault(); _crVoiceStartRecord(e); });
  document.addEventListener('mousemove', e => { if (_crVoiceRecording) _crVoiceTrackPointer(e); });
  document.addEventListener('mouseup', e => { if (_crVoiceRecording) _crVoiceStopRecord(e); });
  btn.addEventListener('touchstart', e => { e.preventDefault(); _crVoiceStartRecord(e.touches[0]); }, {passive:false});
  document.addEventListener('touchmove', e => { if (_crVoiceRecording) _crVoiceTrackPointer(e.touches[0]); }, {passive:false});
  document.addEventListener('touchend', e => { if (_crVoiceRecording) _crVoiceStopRecord(e.changedTouches[0]); });
  document.addEventListener('touchcancel', e => { if (_crVoiceRecording) { _crVoiceCancelled = true; _crVoiceStopRecord(e.changedTouches?.[0]); } });
}

async function _crVoiceStartRecord(evt) {
  if (_crVoiceRecording || isSending) return;
  _crVoiceResumeAmbient = false;
  if (crAmbientUseNative && crAmbientCanRun()) {
    _crVoiceResumeAmbient = true;
    crAmbientStop(false);
  }
  _crVoiceRecording = true;
  _crVoiceCancelled = false;
  _crVoiceChunks = [];
  _crVoiceNativeChunks = [];
  _crVoiceStartTime = Date.now();

  _crVoiceOverlay = document.createElement('div');
  _crVoiceOverlay.className = 'voice-record-overlay active';
  _crVoiceOverlay.innerHTML = `
    <div class="vr-bg"></div>
    <div class="vr-trash-zone" id="crVrTrash">🗑️</div>
    <div class="vr-timer" id="crVrTimer">0:00</div>
    <div class="vr-hint" id="crVrHint">↑ 上滑取消</div>
  `;
  document.body.appendChild(_crVoiceOverlay);

  _crVoiceTimerInterval = setInterval(() => {
    const sec = Math.floor((Date.now() - _crVoiceStartTime) / 1000);
    const m = Math.floor(sec / 60), s = sec % 60;
    const timer = document.getElementById('crVrTimer');
    if (timer) timer.textContent = `${m}:${String(s).padStart(2, '0')}`;
  }, 200);

  const btn = document.getElementById('crVoiceHoldBtn');
  btn.classList.add('recording'); btn.textContent = '松开 发送';

  _crVoiceUseNative = false;

  // Android WebView：直接用原生录音桥（绕过 HTTPS 限制）
  // 注意：聊天室可能在 iframe 中加载，AionAudio 注入在顶层 WebView
  const _AionAudio = _getNativeBridge('AionAudio');
  if (_AionAudio) {
    _crVoiceUseNative = true; _crVoiceNativeChunks = [];
    window._voiceNativeOnChunk = (b64) => { _crVoiceNativeChunks.push(b64); };
    // 同时在顶层窗口注册回调（AudioBridge 的 evaluateJavascript 在顶层执行）
    try { if (window.top !== window) window.top._voiceNativeOnChunk = window._voiceNativeOnChunk; } catch(e) {}
    try { if (window.parent !== window) window.parent._voiceNativeOnChunk = window._voiceNativeOnChunk; } catch(e) {}
    const ok = _AionAudio.start();
    if (!ok) { toast('麦克风启动失败'); _crVoiceCleanup(); return; }
    return;
  }

  // 浏览器：使用 getUserMedia + MediaRecorder
  try {
    _crVoiceStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mime = _crGetVoiceMime();
    _crVoiceMediaRecorder = new MediaRecorder(_crVoiceStream, mime ? { mimeType: mime } : undefined);
    _crVoiceMediaRecorder.ondataavailable = e => { if (e.data.size > 0) _crVoiceChunks.push(e.data); };
    _crVoiceMediaRecorder.start();
  } catch (e) {
    console.warn('[CR-Voice] getUserMedia failed:', e);
    alert('无法访问麦克风');
    _crVoiceCleanup();
  }
}

function _crGetVoiceMime() {
  if (typeof MediaRecorder !== 'undefined') {
    if (MediaRecorder.isTypeSupported('audio/webm;codecs=opus')) return 'audio/webm;codecs=opus';
    if (MediaRecorder.isTypeSupported('audio/webm')) return 'audio/webm';
    if (MediaRecorder.isTypeSupported('audio/mp4')) return 'audio/mp4';
  }
  return '';
}

function _crVoiceTrackPointer(evt) {
  const trash = document.getElementById('crVrTrash');
  const hint = document.getElementById('crVrHint');
  if (!trash) return;
  const r = trash.getBoundingClientRect();
  const dist = Math.sqrt((evt.clientX - r.left - r.width/2)**2 + (evt.clientY - r.top - r.height/2)**2);
  if (dist < 60) {
    trash.classList.add('hover'); if (hint) hint.textContent = '松开 取消'; _crVoiceCancelled = true;
  } else {
    trash.classList.remove('hover'); if (hint) hint.textContent = '↑ 上滑取消'; _crVoiceCancelled = false;
  }
}

async function _crVoiceStopRecord(evt) {
  if (!_crVoiceRecording) return;
  _crVoiceRecording = false;
  const duration = (Date.now() - _crVoiceStartTime) / 1000;

  if (evt) {
    const trash = document.getElementById('crVrTrash');
    if (trash) {
      const r = trash.getBoundingClientRect();
      const dist = Math.sqrt((evt.clientX - r.left - r.width/2)**2 + (evt.clientY - r.top - r.height/2)**2);
      if (dist < 60) _crVoiceCancelled = true;
    }
  }

  if (_crVoiceCancelled || duration < 0.5) { _crVoiceCleanup(); return; }

  let audioBlob;
  if (_crVoiceUseNative) {
    const _aa = _getNativeBridge('AionAudio');
    if (_aa) try { _aa.stop(); } catch(e) {}
    audioBlob = _crBuildWav(_crVoiceNativeChunks);
  } else {
    if (_crVoiceMediaRecorder && _crVoiceMediaRecorder.state !== 'inactive') {
      audioBlob = await new Promise(resolve => {
        _crVoiceMediaRecorder.onstop = () => { resolve(new Blob(_crVoiceChunks, { type: _crVoiceMediaRecorder.mimeType || 'audio/webm' })); };
        _crVoiceMediaRecorder.stop();
      });
    }
  }

  _crVoiceCleanup();
  if (!audioBlob || audioBlob.size < 100) { toast('录音数据为空，请重试'); return; }
  await _crVoiceSend(audioBlob, duration);
}

function _crVoiceCleanup() {
  if (_crVoiceTimerInterval) { clearInterval(_crVoiceTimerInterval); _crVoiceTimerInterval = null; }
  if (_crVoiceOverlay) { _crVoiceOverlay.remove(); _crVoiceOverlay = null; }
  if (_crVoiceStream) { _crVoiceStream.getTracks().forEach(t => t.stop()); _crVoiceStream = null; }
  if (_crVoiceMediaRecorder) { try { _crVoiceMediaRecorder.stop(); } catch {} _crVoiceMediaRecorder = null; }
  if (_crVoiceUseNative) { const _aa = _getNativeBridge('AionAudio'); if (_aa) try { _aa.stop(); } catch {} }
  _crVoiceRecording = false; _crVoiceChunks = []; _crVoiceNativeChunks = [];
  const btn = document.getElementById('crVoiceHoldBtn');
  if (btn) { btn.classList.remove('recording'); btn.textContent = '按住 说话'; }
  if (_crVoiceResumeAmbient) {
    _crVoiceResumeAmbient = false;
    setTimeout(() => crAmbientSyncRunning(), 350);
  }
}

function _crBuildWav(chunks) {
  let totalLen = 0;
  const bufs = chunks.map(b64 => {
    const bin = atob(b64); const buf = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
    totalLen += buf.length; return buf;
  });
  const sampleRate = 16000, numCh = 1, bps = 16;
  const header = new ArrayBuffer(44), v = new DataView(header);
  const ws = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o+i, s.charCodeAt(i)); };
  ws(0,'RIFF'); v.setUint32(4, 36+totalLen, true); ws(8,'WAVE'); ws(12,'fmt ');
  v.setUint32(16,16,true); v.setUint16(20,1,true); v.setUint16(22,numCh,true);
  v.setUint32(24,sampleRate,true); v.setUint32(28,sampleRate*numCh*bps/8,true);
  v.setUint16(32,numCh*bps/8,true); v.setUint16(34,bps,true); ws(36,'data'); v.setUint32(40,totalLen,true);
  const wav = new Uint8Array(44+totalLen); wav.set(new Uint8Array(header), 0);
  let off = 44; for (const buf of bufs) { wav.set(buf, off); off += buf.length; }
  return new Blob([wav], { type: 'audio/wav' });
}

async function _crVoiceSend(audioBlob, duration) {
  if (!currentRoom || isSending) return;

  // 1. 上传音频
  const ext = audioBlob.type.includes('wav') ? 'wav' : (audioBlob.type.includes('mp4') ? 'mp4' : 'webm');
  const fd = new FormData();
  fd.append('file', audioBlob, `voice_${Date.now()}.${ext}`);
  let uploadRes;
  try {
    const res = await fetch(`${API}/upload`, { method: 'POST', body: fd });
    uploadRes = await res.json();
    if (uploadRes.error) { toast(uploadRes.error); return; }
  } catch (e) { toast('语音上传失败'); return; }

  // 2. 转写
  const fd2 = new FormData();
  fd2.append('file', audioBlob, `voice.${ext}`);
  let transcript = '';
  for (let _try = 0; _try < 2; _try++) {
    try {
      const body = _try === 0 ? fd2 : (() => { const f = new FormData(); f.append('file', audioBlob, `voice.${ext}`); return f; })();
      const res = await fetch('/api/voice/transcribe', { method: 'POST', body });
      const r = await res.json();
      transcript = r.text || '';
      if (transcript) break;
    } catch (e) { console.warn(`[CR-Voice] Transcribe attempt ${_try+1} failed:`, e); }
  }

  // 3. 构建语音附件并发送
  const voiceAtt = { type: 'voice', url: uploadRes.url, duration: Math.round(duration * 10) / 10, transcript };

  isSending = true;
  sendBtn.disabled = true;

  const attachments = [voiceAtt.url];
  const voiceAttachmentsFull = [voiceAtt];
  playSend();
  const localRow = appendMessage({ sender: 'user', content: '', created_at: Date.now()/1000, attachments: voiceAttachmentsFull });
  if (localRow) localRow.dataset.localEcho = '1';

  try {
    const resp = await fetch(`${API}/rooms/${currentRoom.id}/send`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        content: transcript || '',
        model: chatroomModel,
        connor_model: chatroomConnorModel,
        attachments,
        voice_attachments: voiceAttachmentsFull,
        tts_enabled: crTtsEnabled,
        tts_aion_voice: crTtsAionVoice,
        tts_connor_voice: crTtsConnorVoice
      }),
    });

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try { handleSSE(JSON.parse(line.slice(6))); } catch {}
      }
    }
  } catch (err) { toast('发送失败: ' + err.message); }
  finally {
    isSending = false;
    sendBtn.disabled = false;
    endStreamingBubble();
    crRefocusComposerAfterSend();
  }
}

// ══════════════════════════════════════════════════
//  密语时刻（BLE 玩具控制）
// ══════════════════════════════════════════════════

const CR_TOY_SERVICE_UUID = 0xEE01, CR_TOY_WRITE_UUID = 0xEE03, CR_TOY_NOTIFY_UUID = 0xEE02;
let crToyDevice = null, crToyServer = null, crToyWriteChar = null, crToyConnected = false;
let crToyActivePreset = -1;
let crToyPresets = [];

// BLE 状态跨页面同步（BroadcastChannel）
const crBleCh = (typeof BroadcastChannel !== 'undefined') ? new BroadcastChannel('toy_ble_state') : null;
function crBleNotify(connected) { if (crBleCh) crBleCh.postMessage({ connected }); }
if (crBleCh) crBleCh.onmessage = function(ev) {
  crToyConnected = !!ev.data.connected;
  crToyUpdateUI();
  if (crToyConnected) crToyLog('已连接（来自其他页面）', 'wl-sys');
  else crToyLog('已断开（来自其他页面）', 'wl-err');
};

// 原生 BLE 回调（Android APK）
window.toyNativeBle = window.toyNativeBle || {};
const _origOnConn = window.toyNativeBle.onConnected;
const _origOnDisc = window.toyNativeBle.onDisconnected;
window.toyNativeBle.onConnected = function() { crToyConnected = true; crToyUpdateUI(); crToyLog('已连接 ♡', 'wl-sys'); crBleNotify(true); if (_origOnConn) _origOnConn(); };
window.toyNativeBle.onDisconnected = function() { crToyConnected = false; crToyUpdateUI(); crToyLog('断开', 'wl-err'); crBleNotify(false); if (_origOnDisc) _origOnDisc(); };

const CR_TOY_MOTORS = [
  { label:'震动', gearsSpec:'0001', modeSpec:'0002',
    modes:[{id:1,name:'全身酥麻'},{id:2,name:'渐入佳境'},{id:3,name:'循序渐进'},{id:4,name:'欢呼雀跃'}] },
  { label:'电流', gearsSpec:'0003', modeSpec:'0004',
    modes:[{id:1,name:'温柔涟漪'},{id:2,name:'娇舌搅动'},{id:3,name:'风驰快感'},{id:4,name:'浪潮不断'}] },
  { label:'吮吸', gearsSpec:'0007', modeSpec:'0008',
    modes:[{id:1,name:'连绵不绝'},{id:2,name:'深海暗涌'},{id:3,name:'爆裂冲刺'},{id:4,name:'浪潮不断'}] },
];
const CR_TOY_PNAMES = ['微风轻拂','春水初生','暗流涌动','如梦似幻','情潮渐涨','烈焰焚身','极乐之巅','魂飞魄散','失控'];
const CR_TOY_PICONS = ['🌸','💧','🌊','✨','🔥','💥','⚡','💀','🌀'];
const CR_TOY_DEF_PRESETS = [
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

function crToyLoadPresets() {
  try { const s = localStorage.getItem('sosexy_presets_v3'); if (s) { crToyPresets = JSON.parse(s); return; } } catch(e) {}
  crToyPresets = JSON.parse(JSON.stringify(CR_TOY_DEF_PRESETS));
}
function crToySavePresets() { localStorage.setItem('sosexy_presets_v3', JSON.stringify(crToyPresets)); }

function crToyLog(msg, cls='') {
  const a = document.getElementById('crToyLogArea'); if (!a) return;
  const d = document.createElement('div'); d.className = cls;
  d.textContent = `[${new Date().toLocaleTimeString('zh-CN',{hour12:false})}] ${msg}`;
  a.appendChild(d); a.scrollTop = a.scrollHeight;
}

function crToyHexToBytes(h) { const b=[]; for(let i=0;i<h.length;i+=2) b.push(parseInt(h.substr(i,2),16)); return b; }
function crToyToHex2(n) { return n.toString(16).padStart(2,'0'); }
function crToyBuildDualCmd(s1,v1,s2,v2) { return '02'+s1+'11'+crToyToHex2(v1)+s2+'11'+crToyToHex2(v2); }
function crToyBuildStopCmd() { return '03000111000003110000071100'; }
function crToySleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function crToySendData2(hexCmd) {
  if (window.AionBle && window.AionBle.isConnected()) {
    crToyLog('→ ' + hexCmd, 'wl-send');
    window.AionBle.sendData(hexCmd);
    return;
  }
  if (!crToyWriteChar) { crToyLog('未连接','wl-err'); return; }
  const full = '00' + hexCmd;
  crToyLog('→ ' + hexCmd, 'wl-send');
  const data = crToyHexToBytes(full), chunks = [];
  for (let i = 0; i < data.length; i += 18) chunks.push(data.slice(i, i+18));
  const rnd = Math.floor(Math.random() * 255), pkts = [];
  for (let i = 0; i < chunks.length; i++) pkts.push([rnd, i+1, ...chunks[i]]);
  if (chunks.length > 0 && chunks[chunks.length-1].length === 18) pkts.push([rnd, chunks.length+1]);
  for (let i = 0; i < pkts.length; i++) {
    const p = new Uint8Array(pkts[i]);
    try {
      if (crToyWriteChar.properties.write) await crToyWriteChar.writeValueWithResponse(p);
      else await crToyWriteChar.writeValueWithoutResponse(p);
    } catch(e) { crToyLog('写入失败:'+e.message,'wl-err'); return; }
    if (pkts.length > 1 && i < pkts.length-1) await crToySleep(30);
  }
}

async function crToyApplyPreset(p) {
  for (let i = 0; i < 3; i++) {
    const m = p.motors[i], mo = CR_TOY_MOTORS[i];
    await crToySendData2(crToyBuildDualCmd(mo.modeSpec, m.mode||1, mo.gearsSpec, m.on ? m.speed : 0));
    await crToySleep(80);
  }
}

async function crToyActivatePreset(idx) {
  crToyActivePreset = idx; crToyRenderGrid();
  const p = crToyPresets[idx];
  crToyLog('⚡ ' + CR_TOY_PNAMES[idx], 'wl-sys');
  await crToyApplyPreset(p);
}

function crToyStopAll() {
  crToyActivePreset = -1;
  crToySendData2(crToyBuildStopCmd());
  crToyLog('⏹ 停止', 'wl-sys');
  crToyRenderGrid();
}

// AI 指令处理器（供 WS/SSE toy_command 事件调用）
function toyExecCmd(cmd) {
  cmd = cmd.trim().toUpperCase();
  if (cmd === 'STOP' || cmd === '0') { crToyStopAll(); return; }
  const n = parseInt(cmd);
  if (n >= 1 && n <= 9) { crToyActivatePreset(n - 1); return; }
  crToyLog('无效指令:' + cmd, 'wl-err');
}

function crToyRenderGrid() {
  const g = document.getElementById('crToyPresetGrid'); if (!g) return;
  g.innerHTML = '';
  for (let i = 0; i < 9; i++) {
    const d = document.createElement('div');
    d.className = 'whisper-p-btn' + (i === crToyActivePreset ? ' active' : '');
    d.innerHTML = `<span class="wp-icon">${CR_TOY_PICONS[i]}</span><span class="wp-name">${CR_TOY_PNAMES[i]}</span><button class="wp-edit" onclick="event.stopPropagation();crToyOpenEditor(${i})">⚙</button>`;
    d.onclick = () => { if (crToyConnected || (window.AionBle && window.AionBle.isConnected())) crToyActivatePreset(i); else crToyLog('请先连接','wl-err'); };
    g.appendChild(d);
  }
}

function crToyUpdateUI() {
  const dot = document.getElementById('crToyDot'), label = document.getElementById('crToyConnLabel'), btn = document.getElementById('crToyConnBtn');
  if (dot) dot.className = 'whisper-dot ' + (crToyConnected ? 'on' : 'off');
  if (label) label.textContent = crToyConnected ? (crToyDevice?.name || '已连接') : '未连接';
  if (btn) btn.textContent = crToyConnected ? '断开' : '连接';
}

async function crToyToggleConnect() {
  if (crToyConnected) { crToyDisconnect(); return; }
  if (window.AionBle) { window.AionBle.connect(); return; }
  if (!navigator.bluetooth) { crToyLog('此浏览器不支持 Web Bluetooth','wl-err'); return; }
  try {
    crToyLog('搜索中...', 'wl-sys');
    crToyDevice = await navigator.bluetooth.requestDevice({ filters: [{ namePrefix: 'SOSEXY' }], optionalServices: [CR_TOY_SERVICE_UUID] });
    crToyLog(crToyDevice.name || '已找到设备', 'wl-sys');
    crToyDevice.addEventListener('gattserverdisconnected', () => { crToyConnected = false; crToyWriteChar = null; crToyUpdateUI(); crToyLog('断开','wl-err'); });
    crToyServer = await crToyDevice.gatt.connect();
    const svc = await crToyServer.getPrimaryService(CR_TOY_SERVICE_UUID);
    crToyWriteChar = await svc.getCharacteristic(CR_TOY_WRITE_UUID);
    try { const nc = await svc.getCharacteristic(CR_TOY_NOTIFY_UUID); await nc.startNotifications(); } catch(e) {}
    crToyConnected = true;
    crToyUpdateUI();
    crToyLog('已连接 ♡', 'wl-sys');
    crBleNotify(true);
  } catch(e) { crToyLog('连接失败:'+e.message, 'wl-err'); }
}

function crToyDisconnect() {
  crToyStopAll();
  if (window.AionBle) { window.AionBle.disconnect(); }
  else if (crToyDevice && crToyDevice.gatt.connected) { crToyDevice.gatt.disconnect(); }
  crToyConnected = false; crToyWriteChar = null;
  crToyUpdateUI(); crToyLog('已断开', 'wl-sys');
  crBleNotify(false);
}

function crOpenWhisper() {
  crToyLoadPresets();
  // 检查原生 BLE 桥接的实际连接状态（从 Aion 页面连接的也能用）
  if (window.AionBle && typeof window.AionBle.isConnected === 'function') {
    crToyConnected = window.AionBle.isConnected();
  }
  crToyRenderGrid();
  crToyUpdateUI();
  document.getElementById('crWhisperModeToggle').checked = crWhisperMode;
  document.getElementById('crWhisperModal').classList.add('show');
}
function crCloseWhisper() { document.getElementById('crWhisperModal').classList.remove('show'); }

// ══════════════════════════════════════════════════
//  Connor 钱包
// ══════════════════════════════════════════════════

let crTransferTarget = 'connor'; // 'connor' | 'aion'

function crOpenTransferDialog() {
  crTransferTarget = 'connor';
  document.getElementById('crTransferTargetConnor').textContent = crConnorName;
  document.getElementById('crTransferTargetAion').textContent = crAiName;
  document.getElementById('crTransferTargetConnor').classList.add('active');
  document.getElementById('crTransferTargetAion').classList.remove('active');
  document.getElementById('crTransferDialogTitle').textContent = `给【${crConnorName}】转账`;
  document.getElementById('crTransferAmountInput').value = '';
  document.getElementById('crTransferDialogOverlay').classList.add('show');
  setTimeout(() => document.getElementById('crTransferAmountInput').focus(), 100);
}

function crSwitchTransferTarget(target) {
  crTransferTarget = target;
  const name = target === 'aion' ? crAiName : crConnorName;
  document.getElementById('crTransferDialogTitle').textContent = `给【${name}】转账`;
  document.getElementById('crTransferTargetConnor').classList.toggle('active', target === 'connor');
  document.getElementById('crTransferTargetAion').classList.toggle('active', target === 'aion');
}

function crCloseTransferDialog() {
  document.getElementById('crTransferDialogOverlay').classList.remove('show');
}

function crConfirmTransfer() {
  const val = document.getElementById('crTransferAmountInput').value.trim();
  if (!val || isNaN(Number(val)) || Number(val) === 0) return;
  const n = Number(val);
  const targetName = crTransferTarget === 'aion' ? crAiName : crConnorName;
  const tag = `[转账给${targetName}：${n}元]`;
  const cur = inputEl.value;
  inputEl.value = cur ? cur + ' ' + tag : tag;
  resizeInput();
  crCloseTransferDialog();
  inputEl.focus();
}

async function crOpenWalletPanel() {
  document.getElementById('crWalletPanelOverlay').classList.add('show');
  closeSidebar();
  try {
    const [balRes, txRes] = await Promise.all([
      fetch('/api/connor-wallet/balance').then(r => r.json()),
      fetch('/api/connor-wallet/transactions?limit=50').then(r => r.json())
    ]);
    document.getElementById('crWalletBalanceValue').textContent = `¥${(balRes.balance || 0).toFixed(2)}`;
    const list = document.getElementById('crWalletTxList');
    if (!txRes || txRes.length === 0) {
      list.innerHTML = '<div class="wallet-tx-empty">暂无转账记录</div>';
    } else {
      list.innerHTML = txRes.map(tx => {
        const isAi = tx.record_type === 'connor_wallet_ai';
        const d = new Date(tx.created_at * 1000);
        const timeStr = `${d.getMonth()+1}/${d.getDate()} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
        const sign = tx.amount >= 0 ? '+' : '';
        const cls = tx.amount >= 0 ? 'positive' : 'negative';
        const uName = '用户';
        let desc = tx.description || (isAi ? `${crConnorName}转账` : `${uName}转账`);
        return `<div class="wallet-tx-item"><div><div class="wallet-tx-desc">${esc(desc)}</div><div class="wallet-tx-time">${timeStr}</div></div><div class="wallet-tx-amount ${cls}">${sign}${tx.amount.toFixed(2)}</div></div>`;
      }).join('');
    }
  } catch(e) {
    document.getElementById('crWalletTxList').innerHTML = '<div class="wallet-tx-empty">加载失败</div>';
  }
}

function crCloseWalletPanel() {
  document.getElementById('crWalletPanelOverlay').classList.remove('show');
}

function crOnWhisperModeChange() {
  crWhisperMode = document.getElementById('crWhisperModeToggle').checked;
  crToyLog(crWhisperMode ? '🔮 密语模式已开启' : '🔮 密语模式已关闭', 'wl-sys');
}

// ── 预设编辑器 ──
function crToyOpenEditor(idx) {
  const p = crToyPresets[idx];
  let h = `<h3>${CR_TOY_PICONS[idx]} ${CR_TOY_PNAMES[idx]}</h3>`;
  for (let mi = 0; mi < 3; mi++) {
    const ms = p.motors[mi], mo = CR_TOY_MOTORS[mi];
    h += `<div class="toy-me-block"><div class="toy-me-head"><span>${mo.label}</span>
    <label class="toggle-switch" style="transform:scale(.8)"><input type="checkbox" id="crteo${mi}" ${ms.on?'checked':''}><span class="toggle-slider"></span></label>
    </div><div class="toy-chip-row" id="crtem${mi}">
    ${mo.modes.map(md => `<span class="toy-chip${md.id===ms.mode?' sel':''}" data-mid="${md.id}" onclick="crToyESel(${mi},${md.id})">${md.name}</span>`).join('')}
    </div><div class="toy-ed-speed"><label>速度</label>
    <input type="range" min="0" max="100" value="${ms.speed}" id="crtes${mi}" oninput="document.getElementById('crtev${mi}').textContent=this.value">
    <span class="toy-ed-sv" id="crtev${mi}">${ms.speed}</span></div></div>`;
  }
  h += `<div class="toy-sheet-btns"><button class="toy-sb-cancel" onclick="crToyCloseEditor()">取消</button><button class="toy-sb-save" onclick="crToySaveEd(${idx})">保存</button></div>`;
  document.getElementById('crToyEditContent').innerHTML = h;
  document.getElementById('crToyEditorOverlay').classList.add('show');
}

function crToyESel(mi, mid) {
  document.querySelectorAll(`#crtem${mi} .toy-chip`).forEach(c => c.classList.toggle('sel', parseInt(c.dataset.mid) === mid));
}

function crToySaveEd(idx) {
  const p = crToyPresets[idx];
  for (let mi = 0; mi < 3; mi++) {
    p.motors[mi].on = document.getElementById(`crteo${mi}`).checked ? 1 : 0;
    const sc = document.querySelector(`#crtem${mi} .toy-chip.sel`);
    if (sc) p.motors[mi].mode = parseInt(sc.dataset.mid);
    p.motors[mi].speed = parseInt(document.getElementById(`crtes${mi}`).value);
  }
  crToySavePresets(); crToyCloseEditor(); crToyRenderGrid();
  crToyLog(`预设${idx+1}已保存`, 'wl-sys');
}

function crToyCloseEditor() { document.getElementById('crToyEditorOverlay').classList.remove('show'); }

// ══════════════════════════════════════════════════
//  初始化
// ══════════════════════════════════════════════════

(async function init() {
  // Start independent Cloudflare requests together. Only the message request
  // must wait for the room list, reducing startup from many round trips to two.
  const configPromise = api('/config');
  const roomPromise = api('/rooms');
  const listenerPromise = crAmbientRefreshListenerState().catch(() => null);
  const modelPromise = fetchCurrentModel(configPromise);

  try {
    const cfg = await configPromise;
    applyChatroomNames(cfg);
    crTtsEnabled = !!cfg.tts_enabled;
    crTtsAionVoice = cfg.tts_aion_voice || '';
    crTtsConnorVoice = cfg.tts_connor_voice || '';
    chatroomConnorModel = cfg.connor_model || 'Codex';
    chatroomReplyOrder = cfg.reply_order || 'random';
    crApplyAmbientVoiceConfig(cfg);
  } catch(e) {}
  await listenerPromise;
  await modelPromise;
  try {
    rooms = await roomPromise;
    renderRoomList();
  } catch(e) {
    rooms = [];
    renderRoomList();
  }
  const initParams = new URLSearchParams(location.search);
  const targetRoomId = initParams.get('room');
  const targetMsgId = initParams.get('msg');
  // 默认打开最后一次聊天的房间
  if (targetRoomId && rooms.some(r => r.id === targetRoomId)) {
    await selectRoom(targetRoomId);
    if (targetMsgId) setTimeout(() => jumpToChatSearchResult(targetMsgId), 100);
  } else if (!currentRoom && rooms.length > 0) {
    await selectRoom(rooms[0].id);
  }
  crAmbientSyncRunning();
  connectWS();
  resizeInput();
})();

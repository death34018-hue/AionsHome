(() => {
  const MAX_RECORD_MS = 45000;
  const CALIBRATION_FRAMES = 20;
  // TTS caption pacing: increase these if text runs ahead of the voice.
  const CAPTION_SEGMENT_MIN_MS = 2600;
  const CAPTION_SEGMENT_MAX_MS = 9000;
  const CAPTION_SEGMENT_MS_PER_CHAR = 260;

  const state = {
    active: false,
    mode: localStorage.getItem('voice_call_mode') || 'handsfree',
    muted: false,
    speaking: false,
    processing: false,
    autoRecording: false,
    manualRecording: false,
    inputStarted: false,
    useNative: false,
    sampleRate: 48000,
    stream: null,
    ctx: null,
    processor: null,
    nativeBridge: null,
    nativeTargets: [],
    nativePrevious: [],
    frames: [],
    speechFrames: 0,
    silenceStartedAt: 0,
    segmentStartedAt: 0,
    calibration: [],
    noiseFloor: 0.006,
    micLevel: 0,
    adapter: null,
    surface: '',
    speaker: 'assistant',
    speakerName: 'AI',
    ttsMsgId: '',
    caption: '正在连接麦克风...',
    status: 'connecting',
    startedAt: 0,
    timer: null,
    captionSegmentTimer: null,
    captionSegments: [],
    captionSegmentIndex: 0,
    animation: null,
    t: 0,
    themeColorTargets: [],
    themeAtOpen: null,
    themeObserver: null,
  };

  const els = {};

  function esc(text) {
    return String(text || '').replace(/[&<>"']/g, ch => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[ch]));
  }

  function bridgeFrom(win, name) {
    try {
      if (!win) return null;
      if (typeof win._getNativeBridge === 'function') return win._getNativeBridge(name);
      return win[name] || null;
    } catch(e) {
      return null;
    }
  }

  function getNativeAudioBridge() {
    return bridgeFrom(window, 'AionAudio')
      || bridgeFrom(window.parent !== window ? window.parent : null, 'AionAudio')
      || bridgeFrom(window.top !== window ? window.top : null, 'AionAudio');
  }

  function accessibleWindows() {
    const wins = [window];
    try { if (window.parent && window.parent !== window) wins.push(window.parent); } catch(e) {}
    try { if (window.top && window.top !== window && !wins.includes(window.top)) wins.push(window.top); } catch(e) {}
    return wins;
  }

  function currentVoiceCallTheme() {
    return document.body?.dataset.theme === 'light' ? 'light' : 'dark';
  }

  function voiceCallChromeColor(theme = currentVoiceCallTheme()) {
    return theme === 'light' ? '#edf8ff' : '#010613';
  }

  function pageChromeColor(theme = currentVoiceCallTheme()) {
    return theme === 'light' ? '#eef3ff' : '#050923';
  }

  function setNativeStatusBarStyle(theme) {
    const bridge = bridgeFrom(window, 'AionStatusBar')
      || bridgeFrom(window.parent !== window ? window.parent : null, 'AionStatusBar')
      || bridgeFrom(window.top !== window ? window.top : null, 'AionStatusBar');
    if (bridge && typeof bridge.setBarStyle === 'function') {
      try { bridge.setBarStyle(theme); } catch(e) {}
    }
  }

  function collectThemeColorTargets() {
    const targets = [];
    const seen = new Set();
    const localMeta = document.querySelector('meta[name="theme-color"]');
    if (localMeta) {
      targets.push({ meta: localMeta, content: localMeta.getAttribute('content') || '' });
      seen.add(localMeta);
    }
    accessibleWindows().forEach(win => {
      try {
        const meta = win.document.querySelector('meta[name="theme-color"]');
        if (meta && !seen.has(meta)) {
          targets.push({ meta, content: meta.getAttribute('content') || '' });
          seen.add(meta);
        }
      } catch(e) {}
    });
    return targets;
  }

  function setVoiceCallThemeColor(active) {
    const theme = currentVoiceCallTheme();
    if (active) {
      if (!state.themeColorTargets.length) {
        state.themeColorTargets = collectThemeColorTargets();
        state.themeAtOpen = theme;
      }
      const color = voiceCallChromeColor(theme);
      state.themeColorTargets.forEach(target => {
        try { target.meta.setAttribute('content', color); } catch(e) {}
      });
      setNativeStatusBarStyle(theme);
      return;
    }

    const themeChanged = state.themeAtOpen && state.themeAtOpen !== theme;
    state.themeColorTargets.forEach(target => {
      try { target.meta.setAttribute('content', themeChanged ? pageChromeColor(theme) : target.content); } catch(e) {}
    });
    state.themeColorTargets = [];
    state.themeAtOpen = null;
    setNativeStatusBarStyle(theme);
  }

  function ensureThemeObserver() {
    if (state.themeObserver || typeof MutationObserver === 'undefined' || !document.body) return;
    state.themeObserver = new MutationObserver(() => {
      if (state.active) setVoiceCallThemeColor(true);
    });
    state.themeObserver.observe(document.body, { attributes: true, attributeFilter: ['data-theme'] });
    window.addEventListener('aion-theme-applied', () => {
      if (state.active) setVoiceCallThemeColor(true);
    });
  }

  function setNativeChunkHandler(handler) {
    const targets = [window];
    try { if (window.parent && window.parent !== window) targets.push(window.parent); } catch(e) {}
    try { if (window.top && window.top !== window && !targets.includes(window.top)) targets.push(window.top); } catch(e) {}
    state.nativeTargets = targets;
    state.nativePrevious = targets.map(target => {
      try {
        const previous = target._voiceNativeOnChunk;
        target._voiceNativeOnChunk = handler;
        return previous;
      } catch(e) {
        return undefined;
      }
    });
  }

  function restoreNativeChunkHandler() {
    state.nativeTargets.forEach((target, idx) => {
      try {
        target._voiceNativeOnChunk = state.nativePrevious[idx] || null;
      } catch(e) {}
    });
    state.nativeTargets = [];
    state.nativePrevious = [];
  }

  function ensureUI() {
    if (els.overlay) return;
    const overlay = document.createElement('div');
    overlay.className = 'voice-call-overlay';
    overlay.innerHTML = `
      <div class="voice-call-shell">
        <div class="voice-call-top">
          <button type="button" class="voice-call-icon-btn" data-action="minimize" aria-label="收起">
            <span class="voice-call-chevron"></span>
          </button>
          <div class="voice-call-title-wrap">
            <div class="voice-call-title" data-role="speaker">AI</div>
            <div class="voice-call-subtitle"><span class="voice-call-mini-bars"></span><span data-role="status">语音通话中</span></div>
          </div>
          <button type="button" class="voice-call-icon-btn voice-call-mode-icon" data-action="mode" aria-label="切换模式">
            <span></span>
          </button>
        </div>
        <div class="voice-call-visual">
          <canvas class="voice-call-wave-canvas" data-role="canvas"></canvas>
          <div class="voice-call-time-row">
            <span></span><span></span><span></span>
            <strong data-role="timer">00:00</strong>
            <span></span><span></span><span></span>
          </div>
        </div>
        <div class="voice-call-caption-card">
          <div class="voice-call-caption-head">
            <span class="voice-call-caption-bars"></span>
            <span class="voice-call-dot"></span>
            <span data-role="caption-status">正在连接</span>
          </div>
          <div class="voice-call-caption-text" data-role="caption">正在连接麦克风...</div>
        </div>
        <div class="voice-call-controls">
          <div class="voice-call-control">
            <button type="button" class="voice-call-round-btn" data-action="mute" aria-label="静音">
              <span class="voice-call-mic-icon"></span>
            </button>
            <span data-role="mute-label">静音</span>
          </div>
          <div class="voice-call-control voice-call-hold-control">
            <button type="button" class="voice-call-hold-btn" data-action="hold" aria-label="按住说话">按住说话</button>
            <span>按键发言</span>
          </div>
          <div class="voice-call-control">
            <button type="button" class="voice-call-round-btn danger" data-action="hangup" aria-label="结束通话">
              <span class="voice-call-phone-icon"></span>
            </button>
            <span>结束通话</span>
          </div>
        </div>
      </div>`;
    document.body.appendChild(overlay);

    els.overlay = overlay;
    els.canvas = overlay.querySelector('[data-role="canvas"]');
    els.speaker = overlay.querySelector('[data-role="speaker"]');
    els.status = overlay.querySelector('[data-role="status"]');
    els.caption = overlay.querySelector('[data-role="caption"]');
    els.captionStatus = overlay.querySelector('[data-role="caption-status"]');
    els.timer = overlay.querySelector('[data-role="timer"]');
    els.holdControl = overlay.querySelector('.voice-call-hold-control');
    els.modeBtn = overlay.querySelector('[data-action="mode"]');
    els.muteBtn = overlay.querySelector('[data-action="mute"]');
    els.muteLabel = overlay.querySelector('[data-role="mute-label"]');
    els.holdBtn = overlay.querySelector('[data-action="hold"]');

    overlay.querySelector('[data-action="minimize"]').addEventListener('click', close);
    overlay.querySelector('[data-action="hangup"]').addEventListener('click', close);
    overlay.querySelector('[data-action="mute"]').addEventListener('click', handleMicAction);
    overlay.querySelector('[data-action="mode"]').addEventListener('click', toggleMode);

    els.holdBtn.addEventListener('pointerdown', beginHold, { passive: false });
    window.addEventListener('pointerup', endHold, { passive: false });
    window.addEventListener('pointercancel', cancelHold, { passive: false });
    window.addEventListener('resize', resizeCanvas);
    ensureThemeObserver();
  }

  function setCaptionText(text) {
    state.caption = text || '';
    if (els.caption) els.caption.textContent = state.caption;
  }

  function charLength(text) {
    return Array.from(String(text || '')).length;
  }

  function splitLongCaption(text) {
    const clean = String(text || '').trim();
    if (!clean) return [];
    const limit = 34;
    if (charLength(clean) <= limit) return [clean];
    const chunks = [];
    let rest = clean;
    while (charLength(rest) > limit) {
      const chars = Array.from(rest);
      const sample = chars.slice(0, limit + 1).join('');
      const cuts = ['，', ',', '、', ' '].map(mark => sample.lastIndexOf(mark));
      const breakAt = Math.max(...cuts);
      const cut = breakAt >= 12 ? breakAt + 1 : limit;
      chunks.push(chars.slice(0, cut).join('').trim());
      rest = chars.slice(cut).join('').trim();
    }
    if (rest) chunks.push(rest);
    return chunks;
  }

  function splitCaptionSentences(text) {
    const clean = String(text || '').replace(/\s+/g, ' ').trim();
    if (!clean) return [];
    const sentences = clean.match(/[^。！？!?；;]+[。！？!?；;]?/g) || [clean];
    return sentences.flatMap(sentence => splitLongCaption(sentence)).filter(Boolean);
  }

  function clearCaptionSegments() {
    if (state.captionSegmentTimer) clearTimeout(state.captionSegmentTimer);
    state.captionSegmentTimer = null;
    state.captionSegments = [];
    state.captionSegmentIndex = 0;
  }

  function captionSegmentDelay(text) {
    return Math.min(CAPTION_SEGMENT_MAX_MS, Math.max(CAPTION_SEGMENT_MIN_MS, charLength(text) * CAPTION_SEGMENT_MS_PER_CHAR));
  }

  function startCaptionSegments(text) {
    clearCaptionSegments();
    const segments = splitCaptionSentences(text);
    if (!segments.length) {
      setCaptionText('正在播放语音...');
      return;
    }
    state.captionSegments = segments;
    const show = index => {
      if (!state.active) return;
      state.captionSegmentIndex = index;
      setCaptionText(segments[index]);
      if (index >= segments.length - 1 || !state.speaking) return;
      state.captionSegmentTimer = setTimeout(() => show(index + 1), captionSegmentDelay(segments[index]));
    };
    show(0);
  }

  function setStatus(status, caption, captionStatus) {
    state.status = status;
    if (caption != null && status !== 'speaking') clearCaptionSegments();
    if (caption != null) state.caption = caption;
    els.overlay?.classList.remove('connecting', 'listening', 'recording', 'transcribing', 'thinking', 'speaking', 'muted');
    els.overlay?.classList.add(status);
    if (state.muted) els.overlay?.classList.add('muted');
    if (els.status) {
      const labels = {
        connecting: '正在连接',
        listening: state.mode === 'handsfree' ? '正在聆听' : '按住发言',
        recording: '正在说话',
        transcribing: '实时转写',
        thinking: '等待回复',
        speaking: '语音播放中',
        muted: '已静音',
      };
      els.status.textContent = labels[status] || '语音通话中';
    }
    if (els.captionStatus) {
      els.captionStatus.textContent = captionStatus || (status === 'speaking' ? '正在说话' : status === 'transcribing' ? '正在转写' : '通话中');
    }
    if (els.caption) els.caption.textContent = state.caption || '';
  }

  function setSpeaker(sender, name) {
    state.speaker = sender || 'assistant';
    state.speakerName = name || state.adapter?.getSpeakerName?.(sender) || 'AI';
    if (els.speaker) els.speaker.textContent = state.speakerName;
    if (els.overlay) {
      els.overlay.dataset.speaker = state.speaker;
    }
  }

  function updateModeUI() {
    if (!els.overlay) return;
    els.overlay.dataset.mode = state.mode;
    els.holdControl.hidden = state.mode !== 'push';
    els.modeBtn.title = state.mode === 'handsfree' ? '切换到按键发言' : '切换到免操作';
    els.modeBtn.classList.toggle('push', state.mode === 'push');
    setStatus(state.status === 'listening' ? 'listening' : state.status, state.caption);
  }

  function toggleMode() {
    state.mode = state.mode === 'handsfree' ? 'push' : 'handsfree';
    localStorage.setItem('voice_call_mode', state.mode);
    resetRecordingState();
    updateModeUI();
  }

  function toggleMute() {
    state.muted = !state.muted;
    resetRecordingState();
    els.muteBtn?.classList.toggle('muted', state.muted);
    if (els.muteLabel) els.muteLabel.textContent = state.muted ? '已静音' : '静音';
    if (state.muted) setStatus('muted', '麦克风已静音', '已静音');
    else setStatus('listening', state.mode === 'handsfree' ? '我在听，你可以直接说话。' : '按住按钮后开始说话。', '通话中');
  }

  function handleMicAction() {
    const interrupted = window.VoiceCallPolicy?.requestInterruption({
      active: state.active,
      speaking: state.speaking,
      adapter: state.adapter,
      msgId: state.ttsMsgId,
    });
    if (!interrupted) {
      toggleMute();
      return;
    }
    state.speaking = false;
    state.ttsMsgId = '';
    state.muted = false;
    resetRecordingState();
    clearCaptionSegments();
    els.muteBtn?.classList.remove('muted');
    if (els.muteLabel) els.muteLabel.textContent = '静音';
    setStatus('listening', '已停止播放，我在听。', '通话中');
  }

  function resetRecordingState() {
    state.autoRecording = false;
    state.manualRecording = false;
    state.frames = [];
    state.speechFrames = 0;
    state.silenceStartedAt = 0;
    state.segmentStartedAt = 0;
  }

  async function open(options) {
    ensureUI();
    if (state.active) close();
    state.adapter = options?.adapter || null;
    state.surface = options?.surface || '';
    if (!state.adapter) return;
    state.active = true;
    state.muted = false;
    state.speaking = false;
    state.ttsMsgId = '';
    state.processing = false;
    state.calibration = [];
    state.noiseFloor = 0.006;
    state.startedAt = Date.now();
    resetRecordingState();
    setSpeaker('assistant', state.adapter.getDefaultSpeakerName?.() || state.adapter.getSpeakerName?.('assistant') || 'AI');
    els.overlay.classList.add('show');
    setVoiceCallThemeColor(true);
    updateModeUI();
    setStatus('connecting', '正在连接麦克风...', '正在连接');
    resizeCanvas();
    startTimer();
    startAnimation();
    try {
      await startInput();
      if (!state.active) return;
      setStatus('listening', state.mode === 'handsfree' ? '我在听，你可以直接说话。' : '按住按钮后开始说话。', '通话中');
    } catch(e) {
      setStatus('connecting', `麦克风不可用：${e.message || e}`, '连接失败');
    }
  }

  function close() {
    state.active = false;
    state.processing = false;
    state.speaking = false;
    state.ttsMsgId = '';
    resetRecordingState();
    clearCaptionSegments();
    stopInput();
    stopTimer();
    stopAnimation();
    if (els.overlay) els.overlay.classList.remove('show');
    setVoiceCallThemeColor(false);
  }

  async function startInput() {
    if (state.inputStarted) return;
    const bridge = getNativeAudioBridge();
    if (bridge && typeof bridge.start === 'function') {
      setNativeChunkHandler(onNativeChunk);
      try {
        const ok = bridge.start();
        if (ok !== false) {
          state.nativeBridge = bridge;
          state.useNative = true;
          state.inputStarted = true;
          state.sampleRate = 16000;
          return;
        }
      } catch(e) {
        restoreNativeChunkHandler();
      }
    }

    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error('当前环境不支持麦克风');
    }
    state.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    });
    state.ctx = new (window.AudioContext || window.webkitAudioContext)();
    state.sampleRate = state.ctx.sampleRate;
    const source = state.ctx.createMediaStreamSource(state.stream);
    state.processor = state.ctx.createScriptProcessor(2048, 1, 1);
    state.processor.onaudioprocess = event => onAudioFrame(event.inputBuffer.getChannelData(0));
    source.connect(state.processor);
    state.processor.connect(state.ctx.destination);
    state.inputStarted = true;
  }

  function stopInput() {
    if (state.useNative && state.nativeBridge?.stop) {
      try { state.nativeBridge.stop(); } catch(e) {}
    }
    restoreNativeChunkHandler();
    if (state.processor) {
      try { state.processor.disconnect(); } catch(e) {}
      state.processor = null;
    }
    if (state.ctx) {
      state.ctx.close().catch(() => {});
      state.ctx = null;
    }
    if (state.stream) {
      state.stream.getTracks().forEach(track => track.stop());
      state.stream = null;
    }
    state.inputStarted = false;
    state.useNative = false;
    state.nativeBridge = null;
  }

  function onNativeChunk(b64) {
    if (!state.active) return;
    const binary = atob(b64);
    const len = binary.length / 2;
    const frame = new Float32Array(len);
    for (let i = 0; i < len; i++) {
      const lo = binary.charCodeAt(i * 2);
      const hi = binary.charCodeAt(i * 2 + 1);
      const int16 = (hi << 8) | lo;
      frame[i] = int16 >= 32768 ? (int16 - 65536) / 32768 : int16 / 32768;
    }
    onAudioFrame(frame);
  }

  function onAudioFrame(input) {
    if (!state.active) return;
    const energy = input.reduce((sum, value) => sum + Math.abs(value), 0) / Math.max(1, input.length);
    state.micLevel = Math.min(1, energy * 38);

    if (state.muted || state.speaking || state.processing) {
      if (state.speaking) resetRecordingState();
      return;
    }

    if (state.mode === 'push') {
      if (state.manualRecording) state.frames.push(new Float32Array(input));
      return;
    }

    if (state.calibration.length < CALIBRATION_FRAMES) {
      state.calibration.push(energy);
      if (state.calibration.length === CALIBRATION_FRAMES) {
        const avg = state.calibration.reduce((sum, value) => sum + value, 0) / state.calibration.length;
        state.noiseFloor = Math.max(0.004, avg * 2.6);
      }
      return;
    }

    const now = Date.now();
    const isSpeech = energy > state.noiseFloor;
    if (!state.autoRecording) {
      if (isSpeech) {
        state.speechFrames++;
        if (state.speechFrames >= 3) {
          state.autoRecording = true;
          state.segmentStartedAt = now;
          state.frames = [new Float32Array(input)];
          setStatus('recording', '正在听你说话...', '正在说话');
        }
      } else {
        state.speechFrames = 0;
      }
      return;
    }

    state.frames.push(new Float32Array(input));
    if (isSpeech) {
      state.silenceStartedAt = 0;
    } else {
      if (!state.silenceStartedAt) state.silenceStartedAt = now;
      const silenceMs = window.VoiceCallPolicy?.silenceTimeoutMs(now - state.segmentStartedAt) || 1200;
      if (now - state.silenceStartedAt >= silenceMs) {
        processFrames();
      }
    }
    if (now - state.segmentStartedAt > MAX_RECORD_MS) processFrames();
  }

  function beginHold(event) {
    if (!state.active || state.mode !== 'push' || state.speaking || state.processing || state.muted) return;
    event.preventDefault();
    state.manualRecording = true;
    state.frames = [];
    state.segmentStartedAt = Date.now();
    els.holdBtn.classList.add('recording');
    setStatus('recording', '正在录音，松手后发送。', '正在说话');
  }

  function endHold(event) {
    if (!state.manualRecording) return;
    event.preventDefault();
    state.manualRecording = false;
    els.holdBtn.classList.remove('recording');
    processFrames();
  }

  function cancelHold(event) {
    if (!state.manualRecording) return;
    event.preventDefault();
    state.manualRecording = false;
    state.frames = [];
    els.holdBtn.classList.remove('recording');
    setStatus('listening', '录音已取消。', '通话中');
  }

  async function processFrames() {
    if (state.processing) return;
    if (!state.frames.length) {
      setStatus('listening', state.mode === 'handsfree' ? '我在听，你可以直接说话。' : '按住按钮后开始说话。', '通话中');
      return;
    }
    const frames = state.frames.slice();
    resetRecordingState();
    const total = frames.reduce((sum, frame) => sum + frame.length, 0);
    const duration = total / Math.max(1, state.sampleRate);
    if (duration < 0.35) {
      setStatus('listening', state.mode === 'handsfree' ? '我在听，你可以直接说话。' : '按住按钮后开始说话。', '通话中');
      return;
    }

    state.processing = true;
    setStatus('transcribing', '正在转写...', '实时转写');
    try {
      const wav = encodeWav(frames, state.sampleRate, total);
      const form = new FormData();
      form.append('file', new Blob([wav], { type: 'audio/wav' }), 'voice-call.wav');
      let resp = await fetch('/api/voice/remote-asr', { method: 'POST', body: form });
      if (!resp.ok) {
        const fallback = new FormData();
        fallback.append('file', new Blob([wav], { type: 'audio/wav' }), 'voice-call.wav');
        resp = await fetch('/api/voice/transcribe', { method: 'POST', body: fallback });
      }
      const data = await resp.json();
      const text = String(data.text || '').trim();
      if (!text) {
        state.processing = false;
        setStatus('listening', '没有听清，再说一次。', '通话中');
        return;
      }
      setStatus('thinking', text, '等待回复');
      await state.adapter.sendText(text);
      state.processing = false;
      if (!state.speaking) {
        setStatus('listening', state.mode === 'handsfree' ? '我在听，你可以继续说。' : '按住按钮后继续说话。', '通话中');
      }
    } catch(e) {
      state.processing = false;
      setStatus('listening', `语音发送失败：${e.message || e}`, '通话中');
    }
  }

  function encodeWav(frames, sampleRate, totalSamples) {
    const samples = new Float32Array(totalSamples);
    let offset = 0;
    frames.forEach(frame => {
      samples.set(frame, offset);
      offset += frame.length;
    });
    const buffer = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(buffer);
    const write = (pos, text) => {
      for (let i = 0; i < text.length; i++) view.setUint8(pos + i, text.charCodeAt(i));
    };
    write(0, 'RIFF');
    view.setUint32(4, 36 + samples.length * 2, true);
    write(8, 'WAVE');
    write(12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    write(36, 'data');
    view.setUint32(40, samples.length * 2, true);
    let pos = 44;
    for (let i = 0; i < samples.length; i++) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(pos, s < 0 ? s * 0x8000 : s * 0x7fff, true);
      pos += 2;
    }
    return buffer;
  }

  function handleTTSChunkStart(payload = {}) {
    if (!state.active) return;
    const sender = payload.sender || state.adapter?.speakerForMessage?.(payload.msgId || payload.msg_id) || 'assistant';
    const name = payload.speakerName || state.adapter?.getSpeakerName?.(sender) || state.adapter?.getDefaultSpeakerName?.() || 'AI';
    state.speaking = true;
    state.ttsMsgId = payload.msgId || payload.msg_id || '';
    resetRecordingState();
    setSpeaker(sender, name);
    setStatus('speaking', null, '正在说话');
    if (els.muteLabel) els.muteLabel.textContent = '插话';
    startCaptionSegments(payload.text || '正在播放语音...');
  }

  function handleTTSChunkEnd() {
    if (!state.active) return;
    if (!state.caption) setCaptionText('正在播放语音...');
  }

  function handleTTSEnd() {
    if (!state.active) return;
    state.speaking = false;
    state.ttsMsgId = '';
    if (els.muteLabel) els.muteLabel.textContent = state.muted ? '已静音' : '静音';
    clearCaptionSegments();
    setTimeout(() => {
      if (!state.active || state.speaking || state.processing || state.muted) return;
      setStatus('listening', state.mode === 'handsfree' ? '我在听，你可以继续说。' : '按住按钮后继续说话。', '通话中');
    }, 450);
  }

  function resizeCanvas() {
    const canvas = els.canvas;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    canvas.width = Math.max(1, Math.floor(rect.width));
    canvas.height = Math.max(1, Math.floor(rect.height));
  }

  function startAnimation() {
    if (state.animation) cancelAnimationFrame(state.animation);
    const step = () => {
      drawWave();
      state.animation = requestAnimationFrame(step);
    };
    step();
  }

  function stopAnimation() {
    if (state.animation) cancelAnimationFrame(state.animation);
    state.animation = null;
  }

  function colorAlpha(hex, alpha) {
    const value = String(hex || '#ffffff').replace('#', '');
    const r = parseInt(value.slice(0, 2), 16) || 255;
    const g = parseInt(value.slice(2, 4), 16) || 255;
    const b = parseInt(value.slice(4, 6), 16) || 255;
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }

  function drawHeroGlow(ctx, m) {
    const outer = ctx.createRadialGradient(m.cx, m.cy, m.radius * 0.16, m.cx, m.cy, m.radius * 1.18);
    outer.addColorStop(0, colorAlpha(m.palette.glow, m.theme === 'light' ? 0.38 : 0.30));
    outer.addColorStop(0.42, colorAlpha(m.palette.main, m.theme === 'light' ? 0.18 : 0.22));
    outer.addColorStop(0.74, colorAlpha(m.palette.soft, m.theme === 'light' ? 0.09 : 0.12));
    outer.addColorStop(1, 'rgba(0, 0, 0, 0)');
    ctx.fillStyle = outer;
    ctx.beginPath();
    ctx.arc(m.cx, m.cy, m.radius * 1.20, 0, Math.PI * 2);
    ctx.fill();
  }

  function drawAtmosphere(ctx, m) {
    const bg = ctx.createRadialGradient(m.cx, m.cy, m.radius * 0.06, m.cx, m.cy, m.radius * 1.18);
    if (m.theme === 'light') {
      bg.addColorStop(0, colorAlpha(m.palette.glow, 0.30));
      bg.addColorStop(0.46, 'rgba(80, 169, 255, 0.14)');
      bg.addColorStop(1, 'rgba(255, 255, 255, 0)');
    } else {
      bg.addColorStop(0, colorAlpha(m.palette.main, 0.20));
      bg.addColorStop(0.55, 'rgba(10, 48, 140, 0.20)');
      bg.addColorStop(1, 'rgba(0, 0, 0, 0)');
    }
    ctx.fillStyle = bg;
    ctx.beginPath();
    ctx.arc(m.cx, m.cy, m.radius * 1.20, 0, Math.PI * 2);
    ctx.fill();

    for (let i = 0; i < 7; i++) {
      const rr = m.radius * (0.34 + i * 0.12 + Math.sin(m.t * 0.021 + i) * 0.010 * m.level);
      ctx.lineWidth = Math.max(0.7, 0.95 * m.ratio);
      ctx.strokeStyle = m.theme === 'light'
        ? `rgba(255, 255, 255, ${0.34 - i * 0.026})`
        : `rgba(105, 179, 255, ${0.22 - i * 0.018})`;
      ctx.beginPath();
      ctx.arc(m.cx, m.cy, rr, 0, Math.PI * 2);
      ctx.stroke();
    }
  }

  function drawPulseRings(ctx, m) {
    for (let i = 0; i < 4; i++) {
      const phase = (m.t * 0.012 + i / 3) % 1;
      const rr = m.radius * (0.48 + phase * 0.70);
      const alpha = (1 - phase) * (m.speaking ? 0.28 : 0.11) * m.level;
      ctx.lineWidth = Math.max(0.8, (2.15 - phase * 0.55) * m.ratio);
      ctx.strokeStyle = colorAlpha(m.palette.glow, alpha);
      ctx.beginPath();
      ctx.arc(m.cx, m.cy, rr, 0, Math.PI * 2);
      ctx.stroke();
    }
  }

  function drawOrbitalRings(ctx, m) {
    ctx.save();
    ctx.translate(m.cx, m.cy);
    ctx.rotate(m.t * 0.0038);
    for (let i = 0; i < 7; i++) {
      const rr = m.radius * (0.52 + i * 0.078);
      const start = i * 1.17 + Math.sin(m.t * 0.01 + i) * 0.12;
      const len = Math.PI * (0.13 + (i % 3) * 0.055 + m.level * 0.055);
      ctx.lineWidth = Math.max(1, (i % 2 ? 1.15 : 1.75) * m.ratio);
      ctx.lineCap = 'round';
      ctx.strokeStyle = i % 2
        ? colorAlpha(m.palette.soft, 0.22 + m.level * 0.12)
        : colorAlpha(m.palette.main, 0.30 + m.level * 0.18);
      ctx.beginPath();
      ctx.arc(0, 0, rr, start, start + len);
      ctx.stroke();
      ctx.strokeStyle = i % 2
        ? colorAlpha(m.palette.soft, 0.12 + m.level * 0.07)
        : colorAlpha(m.palette.glow, 0.14 + m.level * 0.09);
      ctx.beginPath();
      ctx.arc(0, 0, rr, start + Math.PI, start + Math.PI + len * 0.72);
      ctx.stroke();
    }
    ctx.restore();
  }

  function drawParticleField(ctx, m) {
    const dots = 220;
    for (let i = 0; i < dots; i++) {
      const a = (i / dots) * Math.PI * 2 + Math.sin(m.t * 0.01 + i * 0.37) * 0.018;
      const pulse = Math.sin(m.t * 0.028 + i * 1.73);
      const rr = m.radius * (0.94 + pulse * 0.035 * m.level);
      const x = m.cx + Math.cos(a) * rr;
      const y = m.cy + Math.sin(a) * rr;
      const size = Math.max(0.45, (0.62 + Math.max(0, pulse) * 0.72) * m.ratio);
      ctx.fillStyle = m.theme === 'light'
        ? `rgba(255, 255, 255, ${0.32 + Math.max(0, pulse) * 0.25})`
        : colorAlpha(m.palette.main, 0.32 + Math.max(0, pulse) * 0.22);
      ctx.beginPath();
      ctx.arc(x, y, size, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function waveValue(i, bins, t, level) {
    const p = i / Math.max(1, bins - 1);
    const center = 1 - Math.abs(p - 0.5) * 2;
    const env = 0.24 + Math.pow(Math.max(0, center), 0.72) * 0.76;
    const fast = Math.sin(t * 0.19 + i * 0.83);
    const mid = Math.sin(t * 0.071 + i * 0.31);
    const slow = Math.sin(t * 0.031 - i * 0.17);
    const shimmer = Math.sin(t * 0.37 + i * 1.91) * 0.18;
    return env * (0.46 + Math.abs(fast * 0.48 + mid * 0.34 + slow * 0.18 + shimmer) * 0.64) * level;
  }

  function drawSpectralWaveform(ctx, m) {
    const waveBins = 104;
    const width = m.radius * 1.62;
    const startX = m.cx - width / 2;
    const gap = width / (waveBins - 1);
    const base = m.radius * (0.018 + m.level * 0.030);
    const max = m.radius * (0.18 + m.level * 0.28);
    ctx.save();
    ctx.shadowColor = colorAlpha(m.palette.glow, 0.58);
    ctx.shadowBlur = 8 * m.ratio;
    ctx.lineCap = 'round';

    for (let i = 0; i < waveBins; i++) {
      const x = startX + i * gap;
      const value = waveValue(i, waveBins, m.t, m.level);
      const drift = Math.sin(m.t * 0.045 + i * 0.18) * m.radius * 0.018;
      const h = base + max * value;
      const alpha = 0.18 + value * 0.42;
      const grad = ctx.createLinearGradient(x, m.cy - h, x, m.cy + h);
      grad.addColorStop(0, colorAlpha(m.palette.soft, alpha * 0.58));
      grad.addColorStop(0.48, colorAlpha(m.palette.glow, alpha * 0.82));
      grad.addColorStop(1, colorAlpha(m.palette.main, alpha * 0.62));
      ctx.strokeStyle = grad;
      ctx.lineWidth = Math.max(0.9, (1.05 + value * 0.95) * m.ratio);
      ctx.beginPath();
      ctx.moveTo(x, m.cy + drift - h);
      ctx.lineTo(x, m.cy + drift + h);
      ctx.stroke();
    }
    ctx.restore();
  }

  function drawRibbonWave(ctx, m, layer = 0) {
    const width = m.radius * (layer ? 1.48 : 1.26);
    const points = 170;
    const phase = layer ? Math.PI * 0.65 : 0;
    ctx.save();
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.shadowColor = colorAlpha(m.palette.glow, layer ? 0.34 : 0.52);
    ctx.shadowBlur = (layer ? 7 : 12) * m.ratio;
    ctx.strokeStyle = layer
      ? colorAlpha(m.palette.soft, m.theme === 'light' ? 0.46 : 0.40)
      : colorAlpha(m.palette.glow, m.theme === 'light' ? 0.78 : 0.68);
    ctx.lineWidth = Math.max(0.85, (layer ? 1.0 : 1.35) * m.ratio);
    ctx.beginPath();
    for (let i = 0; i <= points; i++) {
      const p = i / points;
      const x = m.cx - width / 2 + p * width;
      const envelope = Math.sin(Math.PI * p);
      const y = m.cy
        + Math.sin(p * Math.PI * (layer ? 4.4 : 5.8) + m.t * (layer ? -0.041 : 0.057) + phase) * m.radius * (0.070 + m.level * 0.045) * (0.35 + envelope)
        + Math.sin(p * Math.PI * 1.5 - m.t * 0.025 + phase) * m.radius * 0.024;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.restore();
  }

  function drawWave() {
    const canvas = els.canvas;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const w = canvas.width;
    const h = canvas.height;
    if (!w || !h) return;
    const ratio = Math.min(1.35, Math.max(1, window.devicePixelRatio || 1));
    const cx = w * 0.5;
    const cy = h * 0.50;
    const radius = Math.min(w, h) * 0.40;
    const theme = document.body.dataset.theme === 'light' ? 'light' : 'dark';
    const speaker = state.speaker || 'assistant';
    const palette = speaker === 'connor'
      ? { main: '#124cff', glow: '#35a8ff', soft: '#6d7dff' }
      : speaker === 'aion'
        ? { main: '#166dff', glow: '#39c5ff', soft: '#0d4fbd' }
        : { main: '#135eff', glow: '#38bfff', soft: '#0b4aa8' };
    const baseLevel = state.speaking ? 0.86 : state.autoRecording || state.manualRecording ? Math.max(0.34, state.micLevel) : 0.18;
    const level = Math.max(0.10, Math.min(1, baseLevel + Math.sin(state.t * 0.041) * (state.speaking ? 0.09 : 0.035)));
    const metrics = { ctx, w, h, ratio, cx, cy, radius, theme, speaker, palette, level, speaking: state.speaking, t: state.t };
    state.t += state.speaking ? 1.25 : 0.72;

    ctx.clearRect(0, 0, w, h);
    ctx.globalCompositeOperation = 'source-over';
    drawHeroGlow(ctx, metrics);
    drawAtmosphere(ctx, metrics);
    ctx.globalCompositeOperation = 'lighter';
    drawPulseRings(ctx, metrics);
    drawOrbitalRings(ctx, metrics);
    drawParticleField(ctx, metrics);
    drawSpectralWaveform(ctx, metrics);
    drawRibbonWave(ctx, metrics, 0);
    drawRibbonWave(ctx, metrics, 1);
    ctx.globalCompositeOperation = 'source-over';
  }

  function startTimer() {
    stopTimer();
    const tick = () => {
      if (!els.timer) return;
      const sec = Math.max(0, Math.floor((Date.now() - state.startedAt) / 1000));
      const mm = String(Math.floor(sec / 60)).padStart(2, '0');
      const ss = String(sec % 60).padStart(2, '0');
      els.timer.textContent = `${mm}:${ss}`;
    };
    tick();
    state.timer = setInterval(tick, 1000);
  }

  function stopTimer() {
    if (state.timer) clearInterval(state.timer);
    state.timer = null;
  }

  function openPrivate() {
    if (!window.PrivateVoiceCallAdapter) return;
    open({ surface: 'private', adapter: window.PrivateVoiceCallAdapter });
  }

  function openChatroom() {
    if (!window.ChatroomVoiceCallAdapter) return;
    open({ surface: 'chatroom', adapter: window.ChatroomVoiceCallAdapter });
  }

  window.VoiceCall = {
    isActive() {
      return state.active;
    },
    open,
    close,
    openPrivate,
    openChatroom,
    handleTTSChunkStart,
    handleTTSChunkEnd,
    handleTTSEnd,
  };
})();

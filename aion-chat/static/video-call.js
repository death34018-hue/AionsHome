/**
 * 视频通话模块 — video-call.js
 * 独立于语音唤醒的全新视频通话功能
 * 依赖 chat.html 中的全局变量: currentConvId, _clientId, ttsEnabled, ttsVoiceId, ws, sending, $()
 */

const videoCall = (() => {
  // ── 状态 ──
  let _active = false;        // 是否在视频通话中
  let _ringing = false;       // 是否在响铃中
  let _ringStartTime = 0;     // 响铃开始时间（用于判断 <5s / ≥5s 接听）
  let _overlay = null;        // DOM 遮罩层
  let _ringAudio = null;      // 铃声 Audio 对象
  let _cameraStream = null;   // 摄像头 MediaStream
  let _facingMode = 'environment'; // 默认后置摄像头
  let _swapped = false;       // 大小画面是否互换
  let _convId = null;         // 当前通话关联的对话 ID
  let _useNativeCamera = false; // 是否使用原生摄像头桥接
  let _nativeCamTimer = null;   // 原生摄像头 rAF ID

  // ── 语音状态 ──
  let _voiceStream = null;
  let _voiceCtx = null;
  let _voiceProcessor = null;
  let _sampleRate = 48000;
  let _useNativeAudio = false;
  let _ownNativeBridge = false;  // true = 我们启动的桥接，false = 复用语音唤醒的
  let _frames = [];
  let _speechN = 0;
  let _silenceN = 0;
  let _isRecording = false;
  let _waitN = 0;
  let _processing = false;
  let _noiseFloor = 0.005;
  let _calibFrames = [];
  let _calibrated = false;
  let _aiSpeaking = false;
  let _listeningEnabled = false;

  // ── 获取 AI 名称 ──
  function _getAiName() {
    if (typeof worldBook !== 'undefined' && worldBook.ai_name) return worldBook.ai_name;
    return 'AI';
  }

  // ── 工具函数 ──
  function _createElement(tag, attrs = {}, styles = {}) {
    const el = document.createElement(tag);
    Object.entries(attrs).forEach(([k, v]) => {
      if (k === 'textContent') el.textContent = v;
      else if (k === 'innerHTML') el.innerHTML = v;
      else el.setAttribute(k, v);
    });
    Object.assign(el.style, styles);
    return el;
  }

  // ── 来电界面 ──
  function _showIncomingUI(onAccept, onReject) {
    _removeOverlay();
    _overlay = _createElement('div', { id: 'videoCallOverlay' }, {
      position: 'fixed', top: 0, left: 0, width: '100%', height: '100%',
      zIndex: 99999, display: 'flex', flexDirection: 'column',
      alignItems: 'center', justifyContent: 'center',
      background: '#000'
    });

    // 背景图
    const bg = _createElement('img', { src: '/public/视频通话背景.jpg' }, {
      position: 'absolute', top: 0, left: 0, width: '100%', height: '100%',
      objectFit: 'cover', opacity: '0.4'
    });
    _overlay.appendChild(bg);

    // 内容容器
    const content = _createElement('div', {}, {
      position: 'relative', zIndex: 1, display: 'flex', flexDirection: 'column',
      alignItems: 'center', gap: '16px'
    });

    // 来电头像
    const avatar = _createElement('img', { src: '/public/视频来电头像.jpg' }, {
      width: '120px', height: '120px', borderRadius: '50%', objectFit: 'cover',
      border: '3px solid rgba(255,255,255,0.3)'
    });
    content.appendChild(avatar);

    // AI 名字 + 来电
    const aiName = _getAiName();
    const nameEl = _createElement('div', { textContent: `${aiName} 来电` }, {
      color: '#fff', fontSize: '22px', fontWeight: '500', marginTop: '8px'
    });
    content.appendChild(nameEl);

    // 按钮区域
    const btnArea = _createElement('div', {}, {
      display: 'flex', gap: '60px', marginTop: '60px', alignItems: 'center'
    });

    // 挂断按钮
    const rejectBtn = _createElement('div', {}, {
      display: 'flex', flexDirection: 'column', alignItems: 'center', cursor: 'pointer'
    });
    const rejectImg = _createElement('img', { src: '/public/挂断.png' }, {
      width: '64px', height: '64px'
    });
    const rejectLabel = _createElement('div', { textContent: '挂断' }, {
      color: '#fff', fontSize: '13px', marginTop: '8px'
    });
    rejectBtn.appendChild(rejectImg);
    rejectBtn.appendChild(rejectLabel);
    rejectBtn.onclick = onReject;

    // 接听按钮（晃动动画）
    const acceptBtn = _createElement('div', {}, {
      display: 'flex', flexDirection: 'column', alignItems: 'center', cursor: 'pointer'
    });
    const acceptImg = _createElement('img', { src: '/public/接听.png' }, {
      width: '64px', height: '64px', animation: 'vcShake 0.8s ease-in-out infinite'
    });
    const acceptLabel = _createElement('div', { textContent: '接听' }, {
      color: '#fff', fontSize: '13px', marginTop: '8px'
    });
    acceptBtn.appendChild(acceptImg);
    acceptBtn.appendChild(acceptLabel);
    acceptBtn.onclick = onAccept;

    btnArea.appendChild(rejectBtn);
    btnArea.appendChild(acceptBtn);
    content.appendChild(btnArea);

    _overlay.appendChild(content);

    // 注入 CSS 动画
    if (!document.getElementById('vcStyles')) {
      const style = document.createElement('style');
      style.id = 'vcStyles';
      style.textContent = `
        @keyframes vcShake {
          0%, 100% { transform: rotate(0deg); }
          15% { transform: rotate(15deg); }
          30% { transform: rotate(-15deg); }
          45% { transform: rotate(12deg); }
          60% { transform: rotate(-10deg); }
          75% { transform: rotate(5deg); }
        }
        #videoCallOverlay * { user-select: none; -webkit-user-select: none; }
      `;
      document.head.appendChild(style);
    }

    document.body.appendChild(_overlay);
  }

  // ── 视频通话界面 ──
  async function _showCallUI(initialStatus) {
    _removeOverlay();
    _active = true;
    _swapped = false;

    _overlay = _createElement('div', { id: 'videoCallOverlay' }, {
      position: 'fixed', top: 0, left: 0, width: '100%', height: '100%',
      zIndex: 99999, background: '#000', overflow: 'hidden'
    });

    // 大画面容器（默认 AI 照片）
    const mainView = _createElement('div', { id: 'vcMainView' }, {
      position: 'absolute', top: 0, left: 0, width: '100%', height: '100%'
    });
    const aiImg = _createElement('img', { id: 'vcAiPhoto', src: '/public/视频背景照片.jpg' }, {
      width: '100%', height: '100%', objectFit: 'cover'
    });
    mainView.appendChild(aiImg);
    _overlay.appendChild(mainView);

    // 小画面容器（默认用户摄像头，右上角）
    const pipView = _createElement('div', { id: 'vcPipView' }, {
      position: 'absolute', top: '50px', right: '16px', width: '120px', height: '170px',
      borderRadius: '12px', overflow: 'hidden', border: '2px solid rgba(255,255,255,0.3)',
      cursor: 'pointer', zIndex: 2, background: '#222'
    });
    const userVideo = _createElement('video', {
      id: 'vcUserVideo', autoplay: '', playsinline: '', muted: ''
    }, {
      width: '100%', height: '100%', objectFit: 'cover',
      transform: 'scaleX(-1)' // 前置摄像头镜像
    });
    pipView.appendChild(userVideo);
    // 原生摄像头回退用的 <img>（默认隐藏）
    const userImg = _createElement('img', { id: 'vcUserImg' }, {
      width: '100%', height: '100%', objectFit: 'cover', display: 'none',
      position: 'absolute', top: 0, left: 0
    });
    pipView.appendChild(userImg);
    _overlay.appendChild(pipView);

    // PiP 中的 AI 照片（互换时使用，默认隐藏）
    const pipAi = _createElement('img', { id: 'vcPipAi', src: '/public/视频背景照片.jpg' }, {
      width: '100%', height: '100%', objectFit: 'cover', display: 'none',
      position: 'absolute', top: 0, left: 0
    });
    pipView.appendChild(pipAi);

    // 主画面中的用户视频（互换时使用，默认隐藏）
    const mainVideo = _createElement('video', {
      id: 'vcMainVideo', autoplay: '', playsinline: '', muted: ''
    }, {
      width: '100%', height: '100%', objectFit: 'cover', display: 'none',
      position: 'absolute', top: 0, left: 0
    });
    mainView.appendChild(mainVideo);
    // 原生摄像头回退用的大画面 <img>（默认隐藏）
    const mainImg = _createElement('img', { id: 'vcMainImg' }, {
      width: '100%', height: '100%', objectFit: 'cover', display: 'none',
      position: 'absolute', top: 0, left: 0
    });
    mainView.appendChild(mainImg);

    // 点击 PiP 互换大小画面
    pipView.onclick = () => _toggleSwap();

    // 通话状态指示
    const statusBar = _createElement('div', { id: 'vcStatus' }, {
      position: 'absolute', top: '12px', left: '50%', transform: 'translateX(-50%)',
      color: '#fff', fontSize: '14px', background: 'rgba(0,0,0,0.5)',
      padding: '4px 16px', borderRadius: '16px', zIndex: 3,
      whiteSpace: 'nowrap'
    });
    statusBar.textContent = initialStatus || '通话中';
    _overlay.appendChild(statusBar);

    // 底部按钮栏
    const bottomBar = _createElement('div', {}, {
      position: 'absolute', bottom: '40px', left: 0, width: '100%',
      display: 'flex', justifyContent: 'center', alignItems: 'center',
      gap: '40px', zIndex: 3
    });

    // 翻转摄像头按钮
    const flipBtn = _createElement('div', {}, {
      width: '50px', height: '50px', borderRadius: '50%',
      background: 'rgba(255,255,255,0.2)', display: 'flex',
      alignItems: 'center', justifyContent: 'center',
      cursor: 'pointer', fontSize: '24px', color: '#fff',
      position: 'absolute', bottom: '130px', right: '20px'
    });
    flipBtn.textContent = '🔄';
    flipBtn.onclick = () => _flipCamera();
    _overlay.appendChild(flipBtn);

    // 挂断按钮
    const hangupBtn = _createElement('div', {}, {
      display: 'flex', flexDirection: 'column', alignItems: 'center', cursor: 'pointer'
    });
    const hangupImg = _createElement('img', { src: '/public/挂断.png' }, {
      width: '64px', height: '64px'
    });
    const hangupLabel = _createElement('div', { textContent: '挂断' }, {
      color: '#fff', fontSize: '13px', marginTop: '6px'
    });
    hangupBtn.appendChild(hangupImg);
    hangupBtn.appendChild(hangupLabel);
    hangupBtn.onclick = () => _hangup();
    bottomBar.appendChild(hangupBtn);
    _overlay.appendChild(bottomBar);

    document.body.appendChild(_overlay);

    // 启动摄像头
    await _startCamera();
    // 不在这里启动语音侦听，由调用方决定时机
  }

  // ── 画面互换 ──
  function _toggleSwap() {
    _swapped = !_swapped;
    const aiImg = document.getElementById('vcAiPhoto');
    const pipAi = document.getElementById('vcPipAi');

    if (_useNativeCamera) {
      // 原生摄像头模式：切换 <img> 元素显示位置
      const userImg = document.getElementById('vcUserImg');
      const mainImg = document.getElementById('vcMainImg');
      if (!aiImg) return;
      if (_swapped) {
        aiImg.style.display = 'none';
        if (mainImg) { mainImg.style.display = 'block'; mainImg.style.transform = _facingMode === 'user' ? 'scaleX(-1)' : 'none'; }
        if (userImg) userImg.style.display = 'none';
        if (pipAi) pipAi.style.display = 'block';
      } else {
        aiImg.style.display = 'block';
        if (mainImg) { mainImg.style.display = 'none'; mainImg.src = ''; }
        if (userImg) { userImg.style.display = 'block'; userImg.style.transform = _facingMode === 'user' ? 'scaleX(-1)' : 'none'; }
        if (pipAi) pipAi.style.display = 'none';
      }
      return;
    }

    // getUserMedia 模式
    const userVideo = document.getElementById('vcUserVideo');
    const mainVideo = document.getElementById('vcMainVideo');
    if (!aiImg || !userVideo) return;

    if (_swapped) {
      aiImg.style.display = 'none';
      mainVideo.style.display = 'block';
      mainVideo.srcObject = _cameraStream;
      mainVideo.style.transform = _facingMode === 'user' ? 'scaleX(-1)' : 'none';
      mainVideo.play().catch(() => {});
      userVideo.style.display = 'none';
      pipAi.style.display = 'block';
    } else {
      aiImg.style.display = 'block';
      mainVideo.style.display = 'none';
      mainVideo.srcObject = null;
      userVideo.style.display = 'block';
      pipAi.style.display = 'none';
    }
  }

  // ── 摄像头管理 ──
  async function _startCamera() {
    // 1) 先尝试 getUserMedia
    try {
      _cameraStream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: _facingMode, width: { ideal: 640 }, height: { ideal: 480 } },
        audio: false
      });
      const userVideo = document.getElementById('vcUserVideo');
      if (userVideo) {
        userVideo.srcObject = _cameraStream;
        userVideo.style.transform = _facingMode === 'user' ? 'scaleX(-1)' : 'none';
        userVideo.play().catch(() => {});
      }
      console.log('[VideoCall] Camera started with getUserMedia');
      return;
    } catch (e) {
      console.warn('[VideoCall] getUserMedia failed:', e);
    }

    // 2) 回退到原生 CameraBridge
    if (window.AionCamera) {
      const facing = _facingMode === 'user' ? 'user' : 'environment';
      const ok = window.AionCamera.start(facing);
      if (ok) {
        _useNativeCamera = true;
        // 隐藏 <video>，显示 <img>
        const vid = document.getElementById('vcUserVideo');
        const img = document.getElementById('vcUserImg');
        if (vid) vid.style.display = 'none';
        if (img) {
          img.style.display = 'block';
          img.style.transform = _facingMode === 'user' ? 'scaleX(-1)' : 'none';
        }
        // 启动 requestAnimationFrame 轮询
        _pollNativeFrame();
        console.log('[VideoCall] Camera started with native bridge');
        return;
      }
    }

    console.warn('[VideoCall] No camera available (getUserMedia + native bridge both failed)');
  }

  function _pollNativeFrame() {
    if (!_useNativeCamera || !window.AionCamera) return;
    const frame = window.AionCamera.getFrame();
    if (frame) {
      const src = 'data:image/jpeg;base64,' + frame;
      if (_swapped) {
        const img = document.getElementById('vcMainImg');
        if (img) img.src = src;
      } else {
        const img = document.getElementById('vcUserImg');
        if (img) img.src = src;
      }
    }
    _nativeCamTimer = requestAnimationFrame(_pollNativeFrame);
  }

  function _stopCamera() {
    if (_nativeCamTimer) { cancelAnimationFrame(_nativeCamTimer); _nativeCamTimer = null; }
    if (_useNativeCamera && window.AionCamera) {
      window.AionCamera.stop();
      _useNativeCamera = false;
    }
    if (_cameraStream) {
      _cameraStream.getTracks().forEach(t => t.stop());
      _cameraStream = null;
    }
  }

  async function _flipCamera() {
    _facingMode = _facingMode === 'environment' ? 'user' : 'environment';
    if (_useNativeCamera && window.AionCamera) {
      window.AionCamera.flip();
      const img = _swapped ? document.getElementById('vcMainImg') : document.getElementById('vcUserImg');
      if (img) img.style.transform = _facingMode === 'user' ? 'scaleX(-1)' : 'none';
    } else {
      _stopCamera();
      await _startCamera();
    }
  }

  // ── 截图 ──
  function _captureScreenshot() {
    // 原生摄像头桥接 — 直接返回高质量帧
    if (_useNativeCamera && window.AionCamera) {
      const b64 = window.AionCamera.capture();
      return b64 ? 'data:image/jpeg;base64,' + b64 : null;
    }
    // getUserMedia 模式 — 从 video 元素截图
    if (!_cameraStream) return null;
    const track = _cameraStream.getVideoTracks()[0];
    if (!track) return null;
    const settings = track.getSettings();
    const w = settings.width || 640;
    const h = settings.height || 480;
    const canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext('2d');

    const videoEl = _swapped
      ? document.getElementById('vcMainVideo')
      : document.getElementById('vcUserVideo');
    if (!videoEl) return null;

    ctx.drawImage(videoEl, 0, 0, w, h);
    return canvas.toDataURL('image/jpeg', 0.8);
  }

  // ── 语音监听 ──
  function _startVoiceListening() {
    _listeningEnabled = true;
    _aiSpeaking = false;
    _processing = false;
    _calibrated = false;
    _calibFrames = [];
    _resetVAD();

    // 优先用 Android 原生桥接
    if (window.AionAudio) {
      // 如果桥接已在录音（语音唤醒正在用），直接复用，数据会推给 _onNativeChunk
      if (window.AionAudio.isRecording()) {
        _useNativeAudio = true;
        _ownNativeBridge = false;
        _sampleRate = 16000;
        _listeningEnabled = true;
        console.log('[VideoCall] Voice reusing existing native bridge');
        _updateStatus('聆听中...');
        return;
      }
      const ok = window.AionAudio.start();
      if (ok) {
        _useNativeAudio = true;
        _ownNativeBridge = true;
        _sampleRate = 16000;
        _listeningEnabled = true;
        console.log('[VideoCall] Voice started with native bridge');
        _updateStatus('聆听中...');
        return;
      }
    }

    // 回退到 getUserMedia（复用摄像头的 audio）
    navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    }).then(stream => {
      _voiceStream = stream;
      _voiceCtx = new (window.AudioContext || window.webkitAudioContext)();
      _sampleRate = _voiceCtx.sampleRate;
      const source = _voiceCtx.createMediaStreamSource(stream);
      _voiceProcessor = _voiceCtx.createScriptProcessor(2048, 1, 1);
      _voiceProcessor.onaudioprocess = (e) => _onAudioFrame(e.inputBuffer.getChannelData(0));
      source.connect(_voiceProcessor);
      _voiceProcessor.connect(_voiceCtx.destination);
      console.log(`[VideoCall] Voice started with getUserMedia, sr=${_sampleRate}`);
    }).catch(e => {
      console.error('[VideoCall] Microphone unavailable:', e);
      _updateStatus('麦克风不可用');
    });
  }

  function _stopVoiceListening() {
    _listeningEnabled = false;
    if (_useNativeAudio && window.AionAudio && _ownNativeBridge) {
      window.AionAudio.stop();
    }
    _useNativeAudio = false;
    _ownNativeBridge = false;
    if (_voiceProcessor) { _voiceProcessor.disconnect(); _voiceProcessor = null; }
    if (_voiceCtx) { _voiceCtx.close().catch(() => {}); _voiceCtx = null; }
    if (_voiceStream) { _voiceStream.getTracks().forEach(t => t.stop()); _voiceStream = null; }
  }

  function _resetVAD() {
    _frames = [];
    _speechN = 0;
    _silenceN = 0;
    _isRecording = false;
    _waitN = 0;
  }

  // Android 原生桥接推送的音频帧
  function _onNativeChunk(b64) {
    if (!_listeningEnabled || _processing) return;
    const binary = atob(b64);
    const len = binary.length / 2;
    const float32 = new Float32Array(len);
    for (let i = 0; i < len; i++) {
      const lo = binary.charCodeAt(i * 2);
      const hi = binary.charCodeAt(i * 2 + 1);
      const int16 = (hi << 8) | lo;
      float32[i] = int16 >= 32768 ? (int16 - 65536) / 32768 : int16 / 32768;
    }
    _onAudioFrame(float32);
  }

  function _onAudioFrame(input) {
    if (!_listeningEnabled || _processing) return;
    const energy = input.reduce((s, v) => s + Math.abs(v), 0) / input.length;

    // 校准
    if (!_calibrated) {
      _calibFrames.push(energy);
      if (_calibFrames.length >= 20) {
        const avg = _calibFrames.reduce((a, b) => a + b, 0) / _calibFrames.length;
        _noiseFloor = Math.max(avg * 2.5, 0.003);
        _calibrated = true;
        _updateStatus('聆听中...');
        console.log(`[VideoCall] Calibrated: noiseFloor=${_noiseFloor.toFixed(5)}`);
      }
      return;
    }

    if (_aiSpeaking) { _resetVAD(); return; }

    const isSpeech = energy > _noiseFloor;
    const silenceLimit = 35;  // ~1.5s
    const waitLimit = 1400;   // ~60s 超时

    if (!_isRecording) {
      if (isSpeech) {
        _speechN++;
        _frames.push(new Float32Array(input));
        if (_speechN >= 8) {
          _isRecording = true;
          _silenceN = 0;
          _updateStatus('正在录音...');
        }
      } else {
        _speechN = 0;
        _frames = [];
        _waitN++;
        if (_waitN > waitLimit) {
          // 60s 超时 — 挂断
          _hangup();
          return;
        }
      }
    } else {
      _frames.push(new Float32Array(input));
      if (!isSpeech) {
        _silenceN++;
        if (_silenceN > silenceLimit) {
          _processAudio();
        }
      } else {
        _silenceN = 0;
      }
      // 最长 30 秒
      const frameSize = _useNativeAudio ? 640 : 2048;
      if (_frames.length > Math.ceil(30 * _sampleRate / frameSize)) {
        _processAudio();
      }
    }
  }

  async function _processAudio() {
    if (_processing) return;
    _processing = true;

    const frames = _frames;
    _resetVAD();

    // 合并帧
    const total = frames.reduce((s, f) => s + f.length, 0);
    const audio = new Float32Array(total);
    let offset = 0;
    for (const f of frames) { audio.set(f, offset); offset += f.length; }

    const duration = total / _sampleRate;
    console.log(`[VideoCall] Recorded ${duration.toFixed(1)}s`);
    if (duration < 0.3) { _processing = false; return; }

    // 转 WAV
    const wav = _encodeWAV(audio);
    _updateStatus('识别中...');

    try {
      const form = new FormData();
      form.append('file', new Blob([wav], { type: 'audio/wav' }), 'audio.wav');
      const resp = await fetch('/api/voice/remote-asr', { method: 'POST', body: form });
      const data = await resp.json();
      const text = (data.text || '').trim();
      console.log(`[VideoCall] ASR: "${text}"`);

      if (!text) {
        _processing = false;
        _updateStatus('聆听中...');
        return;
      }

      // 检查挂断关键词
      const hangupWords = ['再见', '拜拜', '挂断', '结束通话', '挂了'];
      if (hangupWords.some(kw => text.includes(kw))) {
        await _sendToChat(text, null); // 挂断不截图
        _hangup();
        _processing = false;
        return;
      }

      // 截图 + 发送
      _aiSpeaking = true;
      _updateStatus('AI 思考中...');
      const screenshot = _captureScreenshot();
      await _sendToChat(text, screenshot);
    } catch (e) {
      console.error('[VideoCall] ASR error:', e);
      _updateStatus('⚠ 识别出错');
    }

    _processing = false;
  }

  async function _sendToChat(text, screenshotDataUrl) {
    const convId = _convId || currentConvId;
    if (!convId) return;

    // 构建附件
    const attachments = [];
    if (screenshotDataUrl) {
      // 先上传截图
      try {
        const blob = await (await fetch(screenshotDataUrl)).blob();
        const form = new FormData();
        form.append('file', blob, `videocall_${Date.now()}.jpg`);
        const resp = await fetch('/api/upload', { method: 'POST', body: form });
        const data = await resp.json();
        if (data.url) attachments.push(data.url);
      } catch (e) {
        console.error('[VideoCall] Upload screenshot failed:', e);
      }
    }

    // 等待上一条消息发完（最多等 10 秒，不放弃）
    if (typeof sending !== 'undefined' && sending) {
      let waited = 0;
      while (sending && waited < 10000) {
        await new Promise(r => setTimeout(r, 200));
        waited += 200;
      }
    }

    // 通过 API 发送消息（独立 fetch，不依赖全局 send()）
    try {
      const contextLimit = parseInt(document.getElementById('contextSlider')?.value) || 30;
      const body = {
        content: text,
        context_limit: contextLimit,
        attachments,
        whisper_mode: false,
        fast_mode: true,
        tts_enabled: true,
        tts_voice: typeof ttsVoiceId !== 'undefined' ? ttsVoiceId : '',
        client_id: typeof _clientId !== 'undefined' ? _clientId : ''
      };

      const res = await fetch(`/api/conversations/${convId}/send`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });

      // 消耗 SSE 流（消息会通过 WS 广播到 chat.html）
      const reader = res.body.getReader();
      while (true) {
        const { done } = await reader.read();
        if (done) break;
      }
    } catch (e) {
      console.error('[VideoCall] Send failed:', e);
    }

    // AI 回复会通过 TTS 播放，播完后 tts_done 会触发恢复
  }

  // ── 通知 AI 说话状态（被 chat.html 的 TTS 系统调用） ──
  function setAiSpeaking(speaking) {
    if (!_active) return;
    _aiSpeaking = speaking;
    if (!speaking) {
      _resetVAD();
      _processing = false;
      _updateStatus('聆听中...');
    } else {
      _updateStatus('AI 说话中...');
    }
  }

  // ── 挂断 ──
  function _hangup() {
    _active = false;
    _ringing = false;
    _stopRingbell();
    _stopCamera();
    _stopVoiceListening();
    _removeOverlay();

    // 播放挂断音
    const audio = new Audio('/public/挂断音.mp3');
    audio.play().catch(() => {});
  }

  function _removeOverlay() {
    if (_overlay) {
      _overlay.remove();
      _overlay = null;
    }
  }

  // ── 铃声 ──
  function _startRingbell() {
    _ringAudio = new Audio('/public/ringbell.mp3');
    _ringAudio.loop = true;
    _ringAudio.play().catch(() => {});
  }

  function _stopRingbell() {
    if (_ringAudio) {
      _ringAudio.pause();
      _ringAudio.currentTime = 0;
      _ringAudio = null;
    }
  }

  // ── 切断当前 TTS 播放 ──
  function _stopCurrentTTS() {
    try {
      if (typeof ttsAudio !== 'undefined') {
        ttsAudio.pause();
        ttsAudio.src = '';
      }
      if (typeof ttsChunkQueues !== 'undefined') {
        // 清空所有等待中的 TTS 分段
        for (const k of Object.keys(ttsChunkQueues)) delete ttsChunkQueues[k];
      }
      if (typeof ttsPlayOrder !== 'undefined') ttsPlayOrder.length = 0;
      if (typeof ttsPlaying !== 'undefined') ttsPlaying = false;
    } catch(e) {
      console.warn('[VideoCall] stopCurrentTTS error:', e);
    }
  }

  // ── 状态更新 ──
  function _updateStatus(text) {
    const el = document.getElementById('vcStatus');
    if (el) el.textContent = text;
  }

  // ── WAV 编码 ──
  function _encodeWAV(samples) {
    const sr = _sampleRate;
    const buf = new ArrayBuffer(44 + samples.length * 2);
    const v = new DataView(buf);
    const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    w(0, 'RIFF');
    v.setUint32(4, 36 + samples.length * 2, true);
    w(8, 'WAVE');
    w(12, 'fmt ');
    v.setUint32(16, 16, true);
    v.setUint16(20, 1, true);
    v.setUint16(22, 1, true);
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

  // ═══════════════════════════════════════════
  // 公开 API
  // ═══════════════════════════════════════════

  /**
   * 用户主动发起视频通话
   */
  function userInitiate() {
    if (_active || _ringing) return;
    _convId = currentConvId;

    // 显示来电界面（模拟 AI 接听等待 3 秒）
    _ringing = true;
    _startRingbell();
    _showIncomingUI(
      // 接听（用户发起时不需要，3秒后自动进入，但仍然显示按钮）
      () => {
        _ringing = false;
        _stopRingbell();
        _enterCall(true);
      },
      // 挂断
      () => {
        _ringing = false;
        _stopRingbell();
        _hangup();
      }
    );

    // 3 秒后自动进入通话
    setTimeout(() => {
      if (_ringing) {
        _ringing = false;
        _stopRingbell();
        _enterCall(true);
      }
    }, 3000);
  }

  /**
   * AI 发起视频通话（通过 WS video_call_ring 触发）
   */
  function aiInitiate(data) {
    if (_active || _ringing) return;
    _convId = data.conv_id || currentConvId;
    _ringing = true;
    _ringStartTime = Date.now();

    // 掐断正在播放的 TTS 语音（避免铃声和 TTS 重叠）
    _stopCurrentTTS();

    // 开始循环播放铃声
    _startRingbell();

    // 显示来电界面
    _showIncomingUI(
      // 接听
      () => {
        const elapsed = Date.now() - _ringStartTime;
        _ringing = false;
        _stopRingbell();
        _enterCall(elapsed < 5000);
      },
      // 挂断
      () => {
        _ringing = false;
        _hangup();
      }
    );
  }

  /**
   * 进入通话
   * @param {boolean} fast - true: <5s 接起（接电话1.mp3），false: ≥5s（接电话2.mp3）
   */
  async function _enterCall(fast) {
    // 掉断正在播放的 TTS 语音
    _stopCurrentTTS();

    // 播放接听音效
    const pickupAudio = new Audio(fast ? '/public/接电话1.mp3' : '/public/接电话2.mp3');
    pickupAudio.play().catch(() => {});

    // 显示通话界面（摄像头启动，但不启动语音侦听）
    await _showCallUI('视频通话连接中...');

    // 等待接听音效播放完毕（至少 3 秒）
    await new Promise(resolve => {
      const minDelay = new Promise(r => setTimeout(r, 3000));
      const audioEnd = new Promise(r => {
        pickupAudio.onended = r;
        pickupAudio.onerror = r;
        // 安全超时：如果音频加载失败或时长超过 8 秒
        setTimeout(r, 8000);
      });
      Promise.all([minDelay, audioEnd]).then(resolve);
    });

    // 现在才启动语音侦听
    if (_active) {
      _startVoiceListening();
    }
  }

  /**
   * SSE 收到 video_call_incoming 时显示指示器（📹 AI 正在发起视频通话...）
   */
  function handleIncomingIndicator(data) {
    // 在消息下方添加指示器
    const msgId = data.msg_id;
    if (!msgId) return;

    // 延迟等待 DOM 渲染
    setTimeout(() => {
      const msgEl = document.getElementById(`m_${msgId}`);
      if (!msgEl) return;
      const existing = document.getElementById('vc_incoming_indicator');
      if (existing) existing.remove();

      const indicator = _createElement('div', { id: 'vc_incoming_indicator' }, {
        display: 'flex', alignItems: 'center', gap: '8px',
        padding: '6px 14px', margin: '6px 0 6px 48px',
        background: 'rgba(76,175,80,0.12)', color: '#388e3c',
        borderRadius: '12px', fontSize: '13px', fontWeight: '500',
        width: 'fit-content'
      });
      indicator.innerHTML = `📹 ${_getAiName()} 正在发起视频通话<span style="margin-left:4px" class="vc-dots">●</span><span class="vc-dots">●</span><span class="vc-dots">●</span>`;

      // 弹跳动画
      if (!document.getElementById('vcDotsStyle')) {
        const s = document.createElement('style');
        s.id = 'vcDotsStyle';
        s.textContent = `
          .vc-dots { animation: vcDotBounce 1.2s ease-in-out infinite; font-size: 10px; }
          .vc-dots:nth-child(2) { animation-delay: 0.2s; }
          .vc-dots:nth-child(3) { animation-delay: 0.4s; }
          @keyframes vcDotBounce { 0%,80%,100% { opacity: 0.3; } 40% { opacity: 1; } }
        `;
        document.head.appendChild(s);
      }

      msgEl.after(indicator);

      // 5 秒后自动移除（3秒延迟后弹出来电UI时指示器应已消失）
      setTimeout(() => {
        const el = document.getElementById('vc_incoming_indicator');
        if (el) el.remove();
      }, 5000);
    }, 200);
  }

  // ── 暴露给 Android 原生桥接的方法 ──
  // window.AionAudio 会调用 window.onAionAudioChunk(b64)
  // 视频通话模式下拦截原生音频帧
  const _origChunkHandler = window.onAionAudioChunk;
  window.onAionAudioChunk = function(b64) {
    if (_active && _useNativeAudio) {
      _onNativeChunk(b64);
    } else if (_origChunkHandler) {
      _origChunkHandler(b64);
    } else if (typeof remoteVoice !== 'undefined') {
      remoteVoice._onNativeChunk(b64);
    }
  };

  return {
    userInitiate,
    aiInitiate,
    handleIncomingIndicator,
    setAiSpeaking,
    _onNativeChunk,
    get active() { return _active; }
  };
})();

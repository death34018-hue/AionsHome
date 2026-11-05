(function(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) api.installAionTtsAudio(root);
})(typeof window !== 'undefined' ? window : null, function() {
  function installAionTtsAudio(root) {
    if (!root || root.createAionTtsAudio) return;

    const players = new Map();
    let nextPlayerId = 0;

    const playlistKey = 'tts_replay_playlists_v1';
    const readPlaylists = () => {
      try {
        const shared = root.AppSharedData || root.top?.AppSharedData;
        return JSON.parse(shared?.get?.(playlistKey) || root.localStorage?.getItem(playlistKey) || '{}');
      } catch (_) { return {}; }
    };
    const rememberPlaylist = (msgId, urls) => {
      if (!urls?.length) return;
      try {
        const saved = readPlaylists();
        delete saved[msgId];
        saved[msgId] = { urls, savedAt: Date.now() };
        while (Object.keys(saved).length > 64 || JSON.stringify(saved).length > 240000) {
          delete saved[Object.keys(saved)[0]];
        }
        const value = JSON.stringify(saved);
        root.localStorage?.setItem(playlistKey, value);
        (root.AppSharedData || root.top?.AppSharedData)?.put?.(playlistKey, value);
      } catch (_) {}
    };
    root.AionTtsReplayPlaylist = {
      remember: rememberPlaylist,
      async load(msgId, legacyDiscover) {
        const cached = readPlaylists()[msgId];
        const refresh = async () => {
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(), 10000);
          try {
            const response = await root.fetch(`/api/tts/segments/${encodeURIComponent(msgId)}`, { signal: controller.signal, cache: 'no-store' });
            let urls;
            if (response.ok) urls = (await response.json()).urls || [];
            else if (response.status === 404) urls = await legacyDiscover();
            else throw new Error(`语音列表加载失败 (${response.status})`);
            rememberPlaylist(msgId, urls);
            return urls;
          } finally { clearTimeout(timer); }
        };
        if (cached?.urls?.length) {
          // Revalidate quietly, without delaying local playback after a network change.
          if (Date.now() - cached.savedAt > 60000) refresh().catch(() => {});
          return cached.urls;
        }
        return refresh();
      },
    };

    class NativeTtsAudio {
      constructor(bridge) {
        this.bridge = bridge;
        const nonce = Math.random().toString(36).slice(2, 10);
        this.playerId = `tts-${Date.now().toString(36)}-${nonce}-${++nextPlayerId}`;
        this.src = '';
        this.currentSrc = '';
        this.paused = true;
        this.ended = false;
        this.error = null;
        this.onplaying = null;
        this.onpause = null;
        this.onended = null;
        this.onerror = null;
        players.set(this.playerId, this);
      }

      play() {
        if (!this.src) return Promise.reject(new Error('TTS audio source is empty'));
        this.currentSrc = this.src;
        this.paused = false;
        this.ended = false;
        this.error = null;
        if (!this.bridge.play(this.playerId, this.src)) {
          this.paused = true;
          return Promise.reject(new Error('Native TTS playback was rejected'));
        }
        return Promise.resolve();
      }

      prefetch(urls) {
        for (const url of urls.slice(0, 3)) this.bridge.prefetch?.(url);
      }

      prefetchQueued(queues, order) {
        const urls = [];
        for (const id of order) {
          const q = queues[id];
          if (!q) continue;
          for (const seq of Object.keys(q.chunks).map(Number).sort((a, b) => a - b)) {
            if (seq < q.nextPlay) continue;
            const chunk = q.chunks[seq];
            urls.push(typeof chunk === 'object' ? chunk.url : chunk);
            if (urls.length === 3) { this.prefetch(urls); return; }
          }
        }
        this.prefetch(urls);
      }

      // A chat message keeps its turn across clips and synthesis gaps.
      beginSpeech() {
        if (this._speechActive) return;
        this._speechActive = true;
        this.bridge.beginSpeech?.(this.playerId);
      }

      endSpeech() {
        if (!this._speechActive) return;
        this._speechActive = false;
        this.bridge.endSpeech?.(this.playerId);
      }

      pause() {
        this.endSpeech();
        if (this.paused) return;
        this.bridge.stop(this.playerId);
        this.paused = true;
        if (typeof this.onpause === 'function') this.onpause();
      }

      removeAttribute(name) {
        if (name === 'src') this.src = '';
      }

      _onNativeEvent(type) {
        if (type === 'playing') {
          this.paused = false;
          if (typeof this.onplaying === 'function') this.onplaying();
          return;
        }
        if (type === 'ended') {
          this.paused = true;
          this.ended = true;
          if (typeof this.onended === 'function') this.onended();
          return;
        }
        if (type === 'error') {
          this.paused = true;
          this.error = { code: 4, message: 'Native TTS playback failed' };
          if (typeof this.onerror === 'function') this.onerror();
        }
      }
    }

    // Long-form reading needs a real pause and seek, unlike one-shot chat clips.
    class NativeReadingAudio extends NativeTtsAudio {
      constructor(bridge, url) {
        super(bridge);
        this.duration = NaN;
        this._currentTime = 0;
        this.src = url || '';
      }

      get src() { return this._src || ''; }
      set src(value) {
        if (this._src) this.bridge.stop(this.playerId);
        players.delete(this.playerId);
        this._src = String(value || '');
        this._loaded = false;
        this._loading = false;
        this.paused = true;
        this.duration = NaN;
        this._currentTime = 0;
      }

      get currentTime() { return this._currentTime; }
      set currentTime(value) {
        this._currentTime = Math.max(0, Number(value) || 0);
        if (this._loaded) this.bridge.seekAudio(this.playerId, this._currentTime);
      }

      load() {
        if (!this.src) return;
        if (this._loaded || this._loading) this.bridge.stop(this.playerId);
        players.delete(this.playerId);
        this.playerId = `tts-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}-${++nextPlayerId}`;
        players.set(this.playerId, this);
        this.currentSrc = this.src;
        this._loading = true;
        this._loaded = false;
        this.paused = true;
        this.ended = false;
        this.error = null;
        this.duration = NaN;
        this._currentTime = 0;
        if (!this.bridge.prepareAudio(this.playerId, this.src)) {
          this._onNativeEvent('error');
        }
      }

      play() {
        if (!this.src) return Promise.reject(new Error('TTS audio source is empty'));
        if (!this._loaded && !this._loading) this.load();
        if (this.error) return Promise.reject(new Error('Native reading playback was rejected'));
        this.paused = false;
        if (this._loaded) this.bridge.resumeAudio(this.playerId);
        return Promise.resolve();
      }

      pause() {
        if (this.paused) return;
        this.paused = true;
        if (this._loaded) this.bridge.pauseAudio(this.playerId);
        if (typeof this.onpause === 'function') this.onpause();
      }

      _onNativeEvent(type, event = {}) {
        if (Number.isFinite(event.duration)) this.duration = event.duration;
        if (Number.isFinite(event.currentTime)) this._currentTime = event.currentTime;
        if (type === 'loadedmetadata') {
          this._loaded = true;
          this._loading = false;
          if (typeof this.onloadedmetadata === 'function') this.onloadedmetadata();
          if (!this.paused) this.bridge.resumeAudio(this.playerId);
          return;
        }
        if (type === 'timeupdate') {
          if (typeof this.ontimeupdate === 'function') this.ontimeupdate();
          return;
        }
        if (type === 'playing') {
          if (this.paused) return;
          if (typeof this.onplay === 'function') this.onplay();
        }
        if (type === 'ended' || type === 'error') {
          this._loaded = false;
          this._loading = false;
          players.delete(this.playerId);
        }
        super._onNativeEvent(type);
      }
    }

    // Navigation can discard the JS queue before its final native callback arrives.
    root.addEventListener?.('pagehide', () => {
      for (const player of players.values()) player.endSpeech();
    });

    root.onAionNativeTtsEvent = event => {
      if (!event || !event.playerId) return;
      const player = players.get(String(event.playerId));
      if (player) player._onNativeEvent(String(event.type || ''), event);
      if (!root.document || typeof root.document.querySelectorAll !== 'function') return;
      root.document.querySelectorAll('iframe').forEach(frame => {
        try {
          const childHandler = frame.contentWindow && frame.contentWindow.onAionNativeTtsEvent;
          if (typeof childHandler === 'function') childHandler(event);
        } catch (_) {
          // Cross-origin child frames are intentionally ignored.
        }
      });
    };

    root.createAionTtsAudio = () => {
      const bridge = root.AionTtsAudio;
      if (bridge && typeof bridge.play === 'function' && typeof bridge.stop === 'function') {
        return new NativeTtsAudio(bridge);
      }
      return new root.Audio();
    };

    root.createTtsAudio = url => {
      const bridge = root.AionTtsAudio;
      if (bridge && ['prepareAudio', 'pauseAudio', 'resumeAudio', 'seekAudio', 'stop']
          .every(method => typeof bridge[method] === 'function')) {
        return new NativeReadingAudio(bridge, url);
      }
      return new root.Audio(url);
    };
  }

  return { installAionTtsAudio };
});

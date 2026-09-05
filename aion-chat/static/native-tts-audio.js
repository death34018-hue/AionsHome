(function(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) api.installAionTtsAudio(root);
})(typeof window !== 'undefined' ? window : null, function() {
  function installAionTtsAudio(root) {
    if (!root || root.createAionTtsAudio) return;

    const players = new Map();
    let nextPlayerId = 0;

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

      pause() {
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

    root.onAionNativeTtsEvent = event => {
      if (!event || !event.playerId) return;
      const player = players.get(String(event.playerId));
      if (player) player._onNativeEvent(String(event.type || ''));
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
  }

  return { installAionTtsAudio };
});

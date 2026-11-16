/* Per-recording native ASR sessions shared with same-origin chat iframes. */
(function (factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory;
  else window.AionLocalVoice = factory(window);
})(function (win) {
  'use strict';
  function host() {
    try { if (win.top.AionLocalAsr) return win.top; } catch (_) {}
    return win;
  }
  const root = host();
  const bridge = root.AionLocalAsr;
  const sessions = root._aionLocalAsrSessions || (root._aionLocalAsrSessions = new Map());
  const own = new Set();
  let subPageVisible = true;
  root._aionLocalAsrDispatch = event => {
    const session = event && sessions.get(event.id);
    if (session) session.receive(event);
  };
  const api = {
    isVisible() { return subPageVisible && !win.document?.hidden; },
    available() { try { return !!bridge && bridge.available(); } catch (_) { return false; } },
    mode() { try { return api.available() ? bridge.getMode() : 'cloud'; } catch (_) { return 'cloud'; } },
    setMode(value) { if (api.available()) bridge.setMode(value); },
    enabled() { return api.available() && api.mode() === 'local'; },
    transcribeBlob(blob) {
      if (!api.enabled()) return Promise.reject(new Error('当前设备未选择本地识别'));
      if (typeof bridge.transcribeWav !== 'function')
        return Promise.reject(new Error('请更新 Android 小家以使用本地环境转写'));
      const id = 'clip-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
      return new Promise((resolve, reject) => {
        let done = false;
        function settle(text, error) {
          if (done) return;
          done = true;
          clearTimeout(timer);
          sessions.delete(id); own.delete(session);
          if (error) reject(error); else resolve(text);
        }
        const session = {
          id, transcription: true,
          receive(event) {
            if (event.type === 'final') settle((event.text || '').trim());
            else if (event.type === 'error') settle(null, new Error(event.message || '本地转写失败'));
            else if (event.type === 'canceled') session.cancel();
          },
          cancel(error = new Error('本地转写已取消')) {
            if (done) return;
            settle(null, error);
            try { bridge.cancel(id); } catch (_) {}
          }
        };
        const timer = setTimeout(() => session.cancel(new Error('本地转写超时')), 20000);
        sessions.set(id, session); own.add(session);
        blob.arrayBuffer().then(buffer => {
          if (done) return;
          const bytes = new Uint8Array(buffer);
          let binary = '';
          for (let i = 0; i < bytes.length; i += 32768)
            binary += String.fromCharCode(...bytes.subarray(i, i + 32768));
          if (!bridge.transcribeWav(id, btoa(binary))) throw new Error('本地识别正在处理另一段语音');
        }).catch(error => session.cancel(error));
      });
    },
    cancelTranscriptions() {
      for (const session of Array.from(own)) if (session.transcription) session.cancel();
    },
    start(callbacks = {}) {
      if (!api.enabled()) return null;
      if (!api.isVisible()) throw new Error('请回到聊天页面再录音');
      const id = Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
      let done = false, finishing = false, timer, resolve;
      const result = new Promise(r => { resolve = r; });
      function settle(value) {
        if (done) return;
        done = true;
        clearTimeout(timer);
        sessions.delete(id); own.delete(session);
        resolve(value);
      }
      const session = {
        id,
        receive(event) {
          if (done) return;
          if (event.type === 'partial') callbacks.onText?.(event.text || '');
          else if (event.type === 'ready') callbacks.onReady?.();
          else if (event.type === 'final') settle(event);
          else if (event.type === 'error') { settle(null); callbacks.onError?.(event.message || '本地识别失败'); }
          else if (event.type === 'canceled') { settle(null); callbacks.onCancel?.(); }
        },
        finish() {
          if (!done && !finishing) {
            finishing = true;
            timer = setTimeout(() => {
              session.cancel(); callbacks.onError?.('识别等待超时，请重试');
            }, 20000);
            try { bridge.finish(id); }
            catch (_) { session.cancel(); callbacks.onError?.('本地识别收尾失败'); }
          }
          return result;
        },
        cancel() {
          if (done) return;
          settle(null);
          try { bridge.cancel(id); } catch (_) {}
        }
      };
      sessions.set(id, session); own.add(session);
      try {
        if (!bridge.start(id)) throw new Error('麦克风正在使用中或无法启动');
      } catch (e) { session.cancel(); throw e; }
      return session;
    },
    wavBlob(base64) {
      const binary = atob(base64);
      const bytes = Uint8Array.from(binary, c => c.charCodeAt(0));
      return new Blob([bytes], { type: 'audio/wav' });
    }
  };
  function cancelOwn() {
    for (const session of Array.from(own)) {
      session.receive({ type: 'canceled' });
      try { bridge.cancel(session.id); } catch (_) {}
    }
  }
  win.addEventListener('pagehide', cancelOwn);
  win.document?.addEventListener('visibilitychange', () => { if (win.document.hidden) cancelOwn(); });
  const previousVisibility = win.onAionSubPageVisibilityChanged;
  win.onAionSubPageVisibilityChanged = visible => {
    subPageVisible = !!visible;
    if (!subPageVisible) cancelOwn();
    previousVisibility?.(visible);
  };
  return api;
});

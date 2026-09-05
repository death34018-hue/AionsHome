(function(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.VoiceCallPolicy = api;
})(typeof window !== 'undefined' ? window : globalThis, function() {
  function silenceTimeoutMs(segmentDurationMs) {
    return Number(segmentDurationMs || 0) < 2000 ? 800 : 1200;
  }

  function requestInterruption({ active, speaking, adapter, msgId } = {}) {
    if (!active || !speaking || typeof adapter?.interruptTTS !== 'function') return false;
    adapter.interruptTTS(msgId);
    return true;
  }

  return { silenceTimeoutMs, requestInterruption };
});

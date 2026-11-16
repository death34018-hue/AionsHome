(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.AionAmbientTimeline = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {
  const WINDOW_MS = 5 * 60 * 1000;
  const labels = { cough: '咳嗽', humming: '哼歌', singing: '唱歌', snoring: '鼾声', speech: '说话' };
  const clock = at => new Date(at).toLocaleTimeString('zh-CN', { hour12: false });

  function createTimeline(now = Date.now, initial = []) {
    let items = initial.filter(e => labels[e.kind] && Number.isFinite(e.start) && Number.isFinite(e.end));
    function prune() {
      const cutoff = now() - WINDOW_MS;
      items = items.filter(e => e.end > cutoff && e.start <= now() + 5000)
        .map(e => ({ ...e, start: Math.max(e.start, cutoff) }))
        .sort((a, b) => a.start - b.start).slice(-300);
    }
    function events() { prune(); return items.map(e => ({ ...e })); }
    function add(event) {
      prune();
      if (!labels[event.kind] || !Number.isFinite(event.start) || !Number.isFinite(event.end)) return;
      const entry = { kind: event.kind, start: event.start, end: Math.max(event.start, event.end), recordedAt: now() };
      if (entry.kind === 'speech') entry.text = String(event.text || '').trim().slice(0, 1000);
      const last = items[items.length - 1];
      if (entry.kind !== 'speech' && last?.kind === entry.kind && entry.start >= last.start && entry.start - last.end <= 1500) {
        last.end = Math.max(last.end, entry.end);
        last.recordedAt = entry.recordedAt;
      } else items.push(entry);
      prune();
    }
    function text() {
      return events().map(e => {
        const duration = e.end - e.start >= 2000 ? `–${clock(e.end)}` : '';
        return `${clock(e.start)}${duration} ${e.kind === 'speech' ? `说：${e.text}` : labels[e.kind]}`;
      }).join('\n');
    }
    return { add, events, text, clear() { items = []; } };
  }

  function createSpeechSegments(onClip) {
    let chunks = [], start = 0, end = 0, previous = null;
    function flush() {
      if (chunks.length) onClip({ chunks, start, end });
      chunks = [];
    }
    function push(frame) {
      if (chunks.length && frame.start - end > 1500) flush();
      if (frame.kind === 'speech') {
        if (!chunks.length) {
          start = frame.start;
          // One quiet frame keeps the first phoneme if it crossed the model window.
          if (previous?.kind === 'background' && frame.start - previous.end < 100) {
            chunks.push(previous.pcm);
            start = previous.start;
          }
        }
        chunks.push(frame.pcm);
        end = frame.end;
        if (end - start >= 15000) flush();
      } else if (chunks.length) {
        if (frame.kind === 'background') { chunks.push(frame.pcm); end = frame.end; }
        flush();
      }
      previous = frame;
    }
    return { push, reset() { chunks = []; previous = null; } };
  }
  return { createTimeline, createSpeechSegments, labels };
});

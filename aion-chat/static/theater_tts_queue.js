(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.TheaterTTSQueue = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  function createQueue() {
    return {
      nextSeq: 0,
      chunks: new Map(),
      finished: false,
    };
  }

  function normalizeSeq(seq) {
    const value = Number(seq);
    return Number.isInteger(value) && value >= 0 ? value : null;
  }

  function upsert(queue, seq, url) {
    const value = normalizeSeq(seq);
    if (value === null || value < queue.nextSeq) return false;
    queue.chunks.set(value, { seq: value, url });
    return true;
  }

  function markFinished(queue) {
    queue.finished = true;
  }

  function peekNext(queue) {
    const exact = queue.chunks.get(queue.nextSeq);
    if (exact) return exact;
    if (!queue.finished) return null;

    const nextAvailable = [...queue.chunks.keys()]
      .filter((seq) => seq >= queue.nextSeq)
      .sort((a, b) => a - b)[0];
    if (nextAvailable === undefined) return null;
    queue.nextSeq = nextAvailable;
    return queue.chunks.get(nextAvailable) || null;
  }

  function advance(queue) {
    const item = peekNext(queue);
    if (!item) return null;
    queue.chunks.delete(item.seq);
    queue.nextSeq = item.seq + 1;
    return item;
  }

  function isDrained(queue) {
    return queue.finished && peekNext(queue) === null;
  }

  function formatTime(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '--:--';

    const total = Math.floor(seconds);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;

    if (hours > 0) {
      return `${hours}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
    }
    return `${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
  }

  return {
    createQueue,
    upsert,
    markFinished,
    peekNext,
    advance,
    isDrained,
    formatTime,
  };
});

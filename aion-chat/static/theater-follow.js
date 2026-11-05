/* Coarse reading cues use source character ranges, independently of audio files. */
const TheaterFollow = (() => {
  function segmentRange(player) {
    const segment = player?.segments[player.seq];
    if (!segment || player.legacy) return null;
    if (Number.isInteger(segment.start) && Number.isInteger(segment.end)) return segment;
    // Existing studio recordings store Python character counts, not UTF-16 lengths.
    let start = 0;
    for (let i = 0; i <= player.seq; i++) {
      const s = player.segments[i];
      if (!(s.chars > 0)) return null;
      if (Number.isInteger(s.end)) start = s.end;
      else if (i === player.seq) return {start, end: start + s.chars};
      else start += s.chars;
    }
    return null;
  }

  function paragraphs(text, base, escape) {
    let html = '', pos = 0;
    for (const part of text.split(/(\n\s*\n)/)) {
      if (part && !/^\n\s*\n$/.test(part)) {
        html += `<p data-source-start="${base + pos}" data-source-end="${base + pos + part.length}">${escape(part).replace(/\n/g, '\n<span class="line-indent" aria-hidden="true"></span>')}</p>`;
      }
      pos += part.length;
    }
    return html;
  }

  function textPoint(root, offset) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    let node, last = null;
    while ((node = walker.nextNode())) {
      if (node.nodeType === 3) {
        if (offset < node.length) return [node, offset];
        offset -= node.length;
        last = [node, node.length];
      } else if (node.tagName === 'BR') offset = Math.max(0, offset - 1);
    }
    return last;
  }

  function rangesFor(root, content, cue) {
    const chars = Array.from(content);
    const start = chars.slice(0, cue.start).join('').length;
    const end = chars.slice(0, cue.end).join('').length;
    const paragraphs = [...root.querySelectorAll('[data-source-start]')];
    const ranges = [];
    const add = (el, from, to) => {
      const a = textPoint(el, from), b = textPoint(el, to);
      if (!a || !b) return;
      const range = document.createRange();
      range.setStart(...a); range.setEnd(...b);
      ranges.push(range);
    };
    if (paragraphs.length) {
      for (const p of paragraphs) {
        const from = Number(p.dataset.sourceStart), to = Number(p.dataset.sourceEnd);
        if (to > start && from < end) add(p, Math.max(start - from, 0), Math.min(end, to) - from);
      }
    } else {
      // Dialogue hides meta blocks and trims the body; keep the same transformation.
      const prefix = n => content.slice(0, n).replace(/<meta>[\s\S]*?(?:<\/meta>|$)/g, '').trimStart().length;
      add(root, prefix(start), prefix(end));
    }
    return ranges;
  }

  function mount(reader, getState) {
    const area = document.createElement('div'); area.className = 'studio-reader-area';
    reader.before(area); area.append(reader);
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'studio-follow-toggle'; button.hidden = true;
    button.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 5h11M4 10h8M4 15h6M18 8v12m-4-4 4 4 4-4"/><path class="follow-off-mark" d="m3 3 18 18"/></svg>';
    area.append(button);
    let enabled = localStorage.getItem('studio_follow_reading') !== 'false';
    let until = 0, timer = null, held = false, lastKey = '', lastRoot = null, wasPlaying = false, lastProgress = -1;
    let highlightRanges = [];
    const clearHighlight = () => { globalThis.CSS?.highlights?.delete('studio-reading'); };
    function label() {
      button.setAttribute('aria-pressed', String(enabled));
      button.setAttribute('aria-label', enabled ? '关闭跟随朗读' : '开启跟随朗读');
      button.title = enabled ? (until ? '跟随已暂缓，停手 10 秒后恢复；点击关闭' : '跟随朗读已开启，点击关闭') : '开启跟随朗读';
      button.dataset.waiting = String(enabled && until > 0);
    }
    function resetWait() { clearTimeout(timer); timer = null; until = 0; label(); }
    function wait() {
      if (!enabled || !getState().player || getState().player.paused || document.hidden) return;
      until = Date.now() + 10000;
      clearTimeout(timer);
      timer = setTimeout(() => {
        timer = null;
        if (held) { wait(); return; }
        until = 0; label(); update(true);
      }, 10000);
      label();
    }
    function update(force = false, allowPaused = false) {
      const state = getState(), {root, content, player, visible} = state;
      button.hidden = !root || !content;
      const playing = !!player && !player.paused;
      if (!playing) resetWait();
      if (playing && !wasPlaying) { resetWait(); force = true; }
      wasPlaying = playing;
      if (!enabled || !root || !player || !visible) { clearHighlight(); lastKey = ''; return; }
      const cue = segmentRange(player);
      if (!cue) { clearHighlight(); lastKey = ''; return; }
      const key = `${player.id}:${player.seq}:${content.length}`;
      const changed = lastKey !== key || lastRoot !== root || !highlightRanges[0]?.startContainer.isConnected;
      if (changed) {
        highlightRanges = rangesFor(root, content, cue);
        lastKey = key; lastRoot = root;
        if (globalThis.CSS?.highlights && globalThis.Highlight) CSS.highlights.set('studio-reading', new Highlight(...highlightRanges));
      }
      const progress = state.duration > 0 && Number.isFinite(state.duration)
        ? Math.max(0, Math.min(1, (Number(state.currentTime) || 0) / state.duration)) : 0;
      if ((!playing && !allowPaused) || held || until > Date.now() || (!changed && !force && progress === lastProgress)) return;
      lastProgress = progress;
      const rects = highlightRanges.flatMap(r => [...r.getClientRects()]).filter(r => r.width > 0 && r.height > 0);
      if (!rects.length) return;
      const first = rects[0], last = rects[rects.length - 1];
      // Move from the first line to the last over this segment's actual duration.
      // Audio time remains the clock, so pauses, buffering and seeks do not drift.
      const readingY = first.top + first.height / 2 + progress * (last.top + last.height / 2 - first.top - first.height / 2);
      const viewport = reader.getBoundingClientRect();
      const target = Math.max(0, Math.min(reader.scrollHeight - reader.clientHeight,
        reader.scrollTop + readingY - viewport.top - viewport.height / 2));
      if (Math.abs(target - reader.scrollTop) < 1) return;
      reader.scrollTo({top: target,
        behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'});
    }
    button.addEventListener('click', () => {
      enabled = !enabled; localStorage.setItem('studio_follow_reading', String(enabled));
      resetWait(); lastKey = ''; update(true, true);
    });
    reader.addEventListener('pointerdown', () => {
      held = true; wait();
      // Stop any in-flight smooth scroll as soon as the reader takes over.
      reader.scrollTo({top: reader.scrollTop, behavior: 'instant'});
    }, {passive: true});
    const release = () => { if (held) { held = false; wait(); } };
    document.addEventListener('pointerup', release, {passive: true});
    document.addEventListener('pointercancel', release, {passive: true});
    reader.addEventListener('wheel', wait, {passive: true});
    reader.addEventListener('scroll', () => { if (until) wait(); }, {passive: true});
    document.addEventListener('keydown', e => {
      if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(e.key)
          && !e.target.closest('input,textarea,select,button,dialog,[contenteditable="true"]')) wait();
    });
    label();
    return {update, resume() { resetWait(); update(true, true); }};
  }
  return {mount, paragraphs, segmentRange, rangesFor};
})();
if (typeof module !== 'undefined') module.exports = TheaterFollow;

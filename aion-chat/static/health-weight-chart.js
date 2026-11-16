(function (global) {
  'use strict';

  const DAY_MS = 86400000;
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const WEEKDAYS = ['日', '一', '二', '三', '四', '五', '六'];
  const NICE_STEPS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50];
  let instanceSeq = 0;

  function pad(n) { return String(n).padStart(2, '0'); }

  function parseDay(ds) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(ds || ''));
    if (!m) return null;
    return Math.round(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])) / DAY_MS);
  }

  function dayToStr(day) {
    const d = new Date(day * DAY_MS);
    return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
  }

  function dayLabel(day, withYear) {
    const d = new Date(day * DAY_MS);
    return withYear
      ? `${String(d.getUTCFullYear()).slice(2)}/${pad(d.getUTCMonth() + 1)}`
      : `${pad(d.getUTCMonth() + 1)}/${pad(d.getUTCDate())}`;
  }

  function weekday(day) { return WEEKDAYS[new Date(day * DAY_MS).getUTCDay()]; }

  function round(value, digits) {
    const f = Math.pow(10, digits);
    return Math.round(value * f) / f;
  }

  function niceTicks(lo, hi, maxTicks) {
    const span = Math.max(hi - lo, 0.1);
    const step = NICE_STEPS.find(s => span / s <= maxTicks) || NICE_STEPS[NICE_STEPS.length - 1];
    const min = round(Math.floor(lo / step + 1e-9) * step, 2);
    const max = round(Math.ceil(hi / step - 1e-9) * step, 2);
    const ticks = [];
    for (let v = min; v <= max + step / 2; v += step) ticks.push(round(v, 2));
    return { min, max, step, ticks };
  }

  function normalizePoints(weights) {
    return (weights || [])
      .map(w => ({ day: parseDay(w.date), date: w.date, value: Number(w.weight_kg), note: w.note || '' }))
      .filter(p => p.day !== null && Number.isFinite(p.value) && p.value > 0)
      .sort((a, b) => a.day - b.day);
  }

  function computeView(weights, periods, range, todayStr) {
    const points = normalizePoints(weights);
    let end = parseDay(todayStr);
    if (end === null) end = Math.floor(Date.now() / DAY_MS);
    if (points.length && points[points.length - 1].day > end) end = points[points.length - 1].day;
    let start;
    if (range === 'all') start = points.length ? Math.min(points[0].day, end - 6) : end - 29;
    else start = end - (Math.max(1, Number(range) || 30) - 1);
    const visible = points.filter(p => p.day >= start && p.day <= end);
    const bands = (periods || []).map(p => {
      const s = parseDay(p.start_date);
      const e = parseDay(p.end_date || p.start_date);
      if (s === null || e === null || e < start || s > end) return null;
      return { start: Math.max(s, start), end: Math.min(e, end) };
    }).filter(Boolean);
    let stats = null;
    let y = null;
    if (visible.length) {
      const values = visible.map(p => p.value);
      const min = Math.min(...values);
      const max = Math.max(...values);
      const avg = values.reduce((a, b) => a + b, 0) / values.length;
      stats = {
        min, max, avg,
        count: visible.length,
        first: visible[0],
        last: visible[visible.length - 1],
        change: visible.length > 1 ? round(visible[visible.length - 1].value - visible[0].value, 1) : null
      };
      const padY = Math.max((max - min) * 0.15, 0.3);
      y = niceTicks(min - padY, max + padY, 5);
    }
    return { start, end, points, visible, bands, stats, y, totalCount: points.length };
  }

  function svgEl(tag, attrs) {
    const el = document.createElementNS(SVG_NS, tag);
    Object.entries(attrs || {}).forEach(([k, v]) => el.setAttribute(k, String(v)));
    return el;
  }

  function create(container, options) {
    const opts = Object.assign({ height: 188, onEdit: null, onSelect: null }, options || {});
    const uid = `wc${++instanceSeq}`;
    const state = { weights: [], periods: [], range: '30', today: '', view: null, width: 0, selected: null, geom: null };
    container.classList.add('wc-root');
    const tip = document.createElement('div');
    tip.className = 'wc-tip';
    tip.hidden = true;
    let svg = null;
    let selLayer = null;
    let dragging = false;

    function layout() {
      const W = state.width;
      const H = opts.height;
      const L = 36, R = 12, T = 12, B = 24;
      const v = state.view;
      const span = Math.max(v.end - v.start, 1);
      const x = day => L + ((day - v.start) / span) * (W - L - R);
      const y = val => T + ((v.y.max - val) / (v.y.max - v.y.min)) * (H - T - B);
      return { W, H, L, R, T, B, x, y };
    }

    function renderEmpty() {
      const v = state.view;
      const msg = v.totalCount
        ? '这段时间没有体重记录'
        : '还没有体重记录，记一笔就能看到曲线';
      const hint = v.totalCount ? '<div class="wc-empty-hint">切到「全部」可以看到以前的记录</div>' : '';
      container.innerHTML = `<div class="wc-empty" style="height:${opts.height}px"><div>${msg}</div>${hint}</div>`;
      svg = null;
      selLayer = null;
    }

    function render() {
      state.width = Math.round(container.clientWidth || 0);
      state.view = computeView(state.weights, state.periods, state.range, state.today);
      if (state.width < 60) return;
      const v = state.view;
      if (!v.visible.length) {
        state.selected = null;
        tip.hidden = true;
        renderEmpty();
        return;
      }
      const g = layout();
      state.geom = g;
      svg = svgEl('svg', {
        class: 'wc-svg', width: g.W, height: g.H, viewBox: `0 0 ${g.W} ${g.H}`, role: 'img',
        'aria-label': `体重曲线，${v.stats.count} 条记录，最低 ${v.stats.min.toFixed(1)} 公斤，最高 ${v.stats.max.toFixed(1)} 公斤`
      });
      const defs = svgEl('defs');
      const grad = svgEl('linearGradient', { id: `${uid}-fill`, x1: 0, y1: 0, x2: 0, y2: 1 });
      grad.append(svgEl('stop', { offset: '0', class: 'wc-stop-top' }), svgEl('stop', { offset: '1', class: 'wc-stop-bottom' }));
      defs.append(grad);
      svg.append(defs);

      v.bands.forEach(b => {
        const x1 = Math.max(g.L, g.x(b.start - 0.5));
        const x2 = Math.min(g.W - g.R, g.x(b.end + 0.5));
        svg.append(svgEl('rect', { class: 'wc-band', x: x1, y: g.T, width: Math.max(2, x2 - x1), height: g.H - g.T - g.B, rx: 3 }));
      });

      const digits = v.y.step < 1 ? 1 : 0;
      v.y.ticks.forEach(t => {
        const ty = g.y(t);
        svg.append(svgEl('line', { class: 'wc-grid', x1: g.L, x2: g.W - g.R, y1: ty, y2: ty }));
        const label = svgEl('text', { class: 'wc-ylabel', x: g.L - 6, y: ty + 3.5, 'text-anchor': 'end' });
        label.textContent = t.toFixed(digits);
        svg.append(label);
      });

      const span = v.end - v.start;
      const withYear = span > 400;
      const xDays = [...new Set([0, 1 / 3, 2 / 3, 1].map(f => Math.round(v.start + span * f)))];
      xDays.forEach((day, i) => {
        const anchor = i === 0 ? 'start' : i === xDays.length - 1 ? 'end' : 'middle';
        const label = svgEl('text', { class: 'wc-xlabel', x: g.x(day), y: g.H - 6, 'text-anchor': anchor });
        label.textContent = dayLabel(day, withYear);
        svg.append(label);
      });

      const coords = v.visible.map(p => [g.x(p.day), g.y(p.value)]);
      const line = coords.map((c, i) => `${i ? 'L' : 'M'}${c[0].toFixed(1)},${c[1].toFixed(1)}`).join(' ');
      const baseY = g.H - g.B;
      if (coords.length > 1) {
        const area = `${line} L${coords[coords.length - 1][0].toFixed(1)},${baseY} L${coords[0][0].toFixed(1)},${baseY} Z`;
        svg.append(svgEl('path', { class: 'wc-area', d: area, fill: `url(#${uid}-fill)` }));
        svg.append(svgEl('path', { class: 'wc-line', d: line }));
      }
      if (coords.length <= 45) {
        coords.forEach(c => svg.append(svgEl('circle', { class: 'wc-dot', cx: c[0], cy: c[1], r: 2.6 })));
      }
      const last = coords[coords.length - 1];
      svg.append(svgEl('circle', { class: 'wc-last', cx: last[0], cy: last[1], r: 4.5 }));

      selLayer = svgEl('g', { class: 'wc-sel' });
      svg.append(selLayer);
      svg.append(svgEl('rect', { class: 'wc-hit', x: 0, y: 0, width: g.W, height: g.H }));

      container.innerHTML = '';
      container.append(svg, tip);
      bindPointer();
      if (state.selected) {
        const still = v.visible.find(p => p.date === state.selected);
        if (still) select(still, false); else clearSelection();
      }
    }

    function nearest(clientX) {
      const rect = svg.getBoundingClientRect();
      const px = clientX - rect.left;
      let best = null;
      let bestDist = Infinity;
      state.view.visible.forEach(p => {
        const d = Math.abs(state.geom.x(p.day) - px);
        if (d < bestDist) { best = p; bestDist = d; }
      });
      return best;
    }

    function select(point, notify = true) {
      if (!point || !selLayer) return;
      state.selected = point.date;
      const g = state.geom;
      const px = g.x(point.day);
      const py = g.y(point.value);
      selLayer.innerHTML = '';
      selLayer.append(
        svgEl('line', { class: 'wc-cross', x1: px, x2: px, y1: g.T, y2: g.H - g.B }),
        svgEl('circle', { class: 'wc-sel-dot', cx: px, cy: py, r: 6 })
      );
      tip.innerHTML = '';
      const head = document.createElement('div');
      head.className = 'wc-tip-head';
      const when = document.createElement('span');
      when.textContent = `${dayLabel(point.day, false)} 周${weekday(point.day)}`;
      const value = document.createElement('strong');
      value.textContent = `${point.value.toFixed(1)} kg`;
      head.append(when, value);
      tip.append(head);
      if (point.note) {
        const note = document.createElement('div');
        note.className = 'wc-tip-note';
        note.textContent = point.note;
        tip.append(note);
      }
      if (typeof opts.onEdit === 'function') {
        const edit = document.createElement('button');
        edit.type = 'button';
        edit.className = 'wc-tip-edit';
        edit.textContent = '编辑';
        edit.addEventListener('click', ev => { ev.stopPropagation(); opts.onEdit(point.date); });
        tip.append(edit);
      }
      tip.hidden = false;
      const tw = tip.offsetWidth;
      const th = tip.offsetHeight;
      const left = Math.min(Math.max(px - tw / 2, 0), g.W - tw);
      const top = py > g.T + th + 16 ? py - th - 12 : Math.min(py + 12, g.H - th);
      tip.style.left = `${left}px`;
      tip.style.top = `${top}px`;
      if (notify && typeof opts.onSelect === 'function') opts.onSelect(point.date);
    }

    function clearSelection() {
      state.selected = null;
      tip.hidden = true;
      if (selLayer) selLayer.innerHTML = '';
    }

    function bindPointer() {
      svg.addEventListener('pointerdown', ev => {
        dragging = true;
        select(nearest(ev.clientX));
      });
      svg.addEventListener('pointermove', ev => {
        if (dragging || ev.pointerType === 'mouse') select(nearest(ev.clientX));
      });
      const stop = () => { dragging = false; };
      svg.addEventListener('pointerup', stop);
      svg.addEventListener('pointercancel', stop);
      svg.addEventListener('pointerleave', stop);
    }

    function onDocPointer(ev) {
      if (state.selected && !container.contains(ev.target)) clearSelection();
    }
    document.addEventListener('pointerdown', onDocPointer);

    let ro = null;
    if (typeof ResizeObserver === 'function') {
      ro = new ResizeObserver(() => {
        if (Math.round(container.clientWidth || 0) !== state.width) render();
      });
      ro.observe(container);
    } else {
      global.addEventListener('resize', render);
    }

    return {
      update(next) {
        Object.assign(state, next || {});
        render();
        return state.view;
      },
      view() { return state.view; },
      clearSelection,
      destroy() {
        document.removeEventListener('pointerdown', onDocPointer);
        if (ro) ro.disconnect();
        container.innerHTML = '';
      }
    };
  }

  const api = { create, computeView, niceTicks, parseDay, dayToStr };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  global.HealthWeightChart = api;
})(typeof window !== 'undefined' ? window : globalThis);

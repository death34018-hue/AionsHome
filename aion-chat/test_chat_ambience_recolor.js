'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

function createMockEnv({ stored = null, theme = 'dark', desktop = true } = {}) {
  const storage = new Map();
  if (stored !== null) storage.set('aion_chat_ambience', stored);

  const mutationCallbacks = [];
  const timers = [];
  const rafQueue = [];
  const imageLoads = [];
  const bgDraws = [];
  const pageBgDraws = [];

  function makeElement(tag) {
    const classes = new Set();
    const attrs = {};
    const props = {};
    const children = [];
    const el = {
      tagName: tag.toUpperCase(),
      style: {
        setProperty(k, v) { props[k] = String(v); },
        removeProperty(k) { delete props[k]; },
        getPropertyValue(k) { return props[k] || ''; }
      },
      dataset: {},
      children,
      className: '',
      width: 0,
      height: 0,
      clientWidth: desktop ? 1200 : 390,
      clientHeight: 844,
      classList: {
        add: x => classes.add(x),
        remove: x => classes.delete(x),
        toggle: (x, f) => (f ? classes.add(x) : classes.delete(x)),
        contains: x => classes.has(x)
      },
      setAttribute(k, v) { attrs[k] = String(v); },
      getAttribute(k) { return attrs[k]; },
      removeAttribute(k) { delete attrs[k]; },
      prepend(child) { children.unshift(child); },
      append(...nodes) { children.push(...nodes); },
      addEventListener() {},
      querySelectorAll() { return []; },
      getContext(type) {
        if (type !== '2d') return null;
        return {
          createImageData(w, h) {
            return { width: w, height: h, data: new Uint8ClampedArray(w * h * 4) };
          },
          putImageData(imgData) {
            const snap = {
              className: el.className,
              width: imgData.width,
              height: imgData.height,
              firstPixel: [imgData.data[0], imgData.data[1], imgData.data[2], imgData.data[3]]
            };
            if (el.className === 'chat-ambience-bg') bgDraws.push(snap);
          },
          drawImage(src) {
            if (el.className === 'chat-ambience-page-bg') {
              pageBgDraws.push({ width: el.width, height: el.height, srcClass: src.className });
            }
          },
          getImageData(x, y, w, h) {
            const data = new Uint8ClampedArray(w * h * 4);
            for (let i = 0; i < w * h; i++) {
              const p = i * 4;
              // 给第一像素一个明确测试亮度，其余保持递增
              const val = (i * 37) % 256;
              data[p] = val;
              data[p + 1] = val;
              data[p + 2] = val;
              data[p + 3] = 255;
            }
            return { width: w, height: h, data };
          },
          setTransform() {},
          clearRect() {},
          scale() {},
          translate() {},
          fillRect() {},
          createRadialGradient() { return { addColorStop() {} }; },
          createLinearGradient() { return { addColorStop() {} }; }
        };
      }
    };
    return el;
  }

  const area = makeElement('div');
  area.className = 'chat-area';
  const body = makeElement('body');
  body.dataset.theme = theme;

  let timerId = 0;
  let rafId = 0;
  const activeTimers = new Map();
  const activeRafs = new Map();

  const context = {
    console, Math, Number, String, Boolean, Array, Object, JSON, Promise, RegExp,
    Uint8Array, Uint8ClampedArray, Uint32Array, ArrayBuffer,
    devicePixelRatio: 1,
    innerWidth: desktop ? 1200 : 390,
    localStorage: {
      getItem: k => (storage.has(k) ? storage.get(k) : null),
      setItem: (k, v) => storage.set(k, String(v))
    },
    document: {
      hidden: false,
      body,
      createElement: makeElement,
      getElementById: () => null,
      querySelector: sel => (sel === '.chat-area' ? area : null),
      querySelectorAll: () => [],
      addEventListener() {}
    },
    matchMedia: () => ({
      matches: desktop,
      addEventListener() {}
    }),
    ResizeObserver: class { observe() {} },
    IntersectionObserver: class { observe() {} },
    MutationObserver: class {
      constructor(cb) { mutationCallbacks.push(cb); }
      observe() {}
    },
    Image: class {
      set src(url) {
        this._src = url;
        this.naturalWidth = 8;
        this.naturalHeight = 8;
        imageLoads.push(this);
      }
      get src() { return this._src; }
    },
    requestAnimationFrame(cb) {
      const id = ++rafId;
      activeRafs.set(id, cb);
      rafQueue.push({ id, cb });
      return id;
    },
    cancelAnimationFrame(id) {
      activeRafs.delete(id);
    },
    setTimeout(cb) {
      const id = ++timerId;
      activeTimers.set(id, cb);
      timers.push({ id, cb });
      return id;
    },
    clearTimeout(id) {
      activeTimers.delete(id);
    },
    addEventListener() {}
  };
  context.window = context;
  context.window.AionAmbienceSync = { save() {} };

  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, 'static/chat-ambience.js'), 'utf8'), context);

  async function flush() {
    while (rafQueue.length) {
      const item = rafQueue.shift();
      if (activeRafs.has(item.id)) {
        activeRafs.delete(item.id);
        item.cb(16);
      }
    }
    while (imageLoads.length) {
      const img = imageLoads.shift();
      img.onload?.();
      for (let i = 0; i < 12; i++) await Promise.resolve();
    }
    while (rafQueue.length) {
      const item = rafQueue.shift();
      if (activeRafs.has(item.id)) {
        activeRafs.delete(item.id);
        item.cb(32);
      }
    }
  }

  function flushTimers() {
    while (timers.length) {
      const item = timers.shift();
      if (activeTimers.has(item.id)) {
        activeTimers.delete(item.id);
        item.cb();
      }
    }
  }

  const bgCanvas = area.children[0].children.find(c => c.className === 'chat-ambience-bg');
  const pageBgCanvas = body.children.find(c => c.className === 'chat-ambience-page-bg');

  return {
    api: context.window.AionChatAmbience,
    storage,
    body,
    bgCanvas,
    pageBgCanvas,
    bgDraws,
    pageBgDraws,
    flush,
    flushTimers,
    setTheme(nextTheme) {
      body.dataset.theme = nextTheme;
      for (const cb of mutationCallbacks) cb([{ attributeName: 'data-theme' }]);
    }
  };
}

function lutPixel(lut, idx) {
  const o = idx * 3;
  return [lut[o], lut[o + 1], lut[o + 2]];
}

test('dark LUT keeps deep sky near black, colors nebula accurately, and preserves bright stars', () => {
  const env = createMockEnv();
  const cases = [
    ['#ff5a5f', ([r, g, b]) => r > g * 1.4 && r > b * 1.4],
    ['#ff9d42', ([r, g, b]) => r > g && g > b],
    ['#57c96b', ([r, g, b]) => g > r * 1.4 && g > b * 1.3]
  ];
  for (const [hex, checkNebula] of cases) {
    const lut = env.api.buildLut(hex, false, 70);
    for (let idx = 0; idx <= 10; idx++) {
      const [r, g, b] = lutPixel(lut, idx);
      assert.ok(r < 20 && g < 20 && b < 20, `${hex} sky idx=${idx} lifted: ${r},${g},${b}`);
    }
    const nebula = lutPixel(lut, 45);
    assert.ok(checkNebula(nebula), `${hex} nebula idx=45 unexpected: ${nebula}`);
    const star = lutPixel(lut, 255);
    assert.ok(star[0] > 230 && star[1] > 230 && star[2] > 230, `${hex} star idx=255 dim: ${star}`);
  }
});

test('light LUT transforms midtones cleanly into target pastel hues with bright white star core', () => {
  const env = createMockEnv();
  const cases = [
    ['#45a8ff', ([r, g, b]) => b > r && b >= g],
    ['#57c96b', ([r, g, b]) => g > r && g > b],
    ['#ffd84a', ([r, g, b]) => r >= g && g > b]
  ];
  for (const [hex, checkHue] of cases) {
    const lut = env.api.buildLut(hex, true, 70);
    const mid = lutPixel(lut, 205);
    assert.ok(mid[0] > 165 && mid[1] > 165 && mid[2] > 165, `${hex} midtone too dark: ${mid}`);
    assert.ok(checkHue(mid), `${hex} midtone hue wrong: ${mid}`);
    const core = lutPixel(lut, 255);
    assert.ok(core[0] > 245 && core[1] > 245 && core[2] > 245, `${hex} core not white: ${core}`);
  }
});

test('neutral gray custom tint outputs achromatic silver-gray without red hue leak', () => {
  const env = createMockEnv();
  for (const light of [false, true]) {
    const lut = env.api.buildLut('#808080', light, 85);
    for (const idx of [10, 50, 128, 205, 250]) {
      const [r, g, b] = lutPixel(lut, idx);
      assert.ok(Math.abs(r - g) <= 1 && Math.abs(g - b) <= 1, `light=${light} idx=${idx} not gray: ${r},${g},${b}`);
    }
  }
});

test('strength slider increases chroma from 20% to 100% and v1 cache migrates strength to 70%', () => {
  const oldCached = createMockEnv({ stored: JSON.stringify({ effect: 'rain', tint: '#45a8ff', strength: 12 }) });
  assert.deepEqual({ ...oldCached.api.get() }, { effect: 'rain', tint: '#45a8ff', strength: 70 });

  const v2Cached = createMockEnv({ stored: JSON.stringify({ v: 2, effect: 'snow', tint: '#ff5a5f', strength: 92 }) });
  assert.deepEqual({ ...v2Cached.api.get() }, { effect: 'snow', tint: '#ff5a5f', strength: 92 });

  const lowLut = oldCached.api.buildLut('#45a8ff', true, 20);
  const highLut = oldCached.api.buildLut('#45a8ff', true, 100);
  const chroma = ([r, g, b]) => Math.max(r, g, b) - Math.min(r, g, b);
  assert.ok(chroma(lutPixel(highLut, 205)) > chroma(lutPixel(lowLut, 205)));
});

test('tint renders on canvas, clears size when unset, skips pageBg on mobile, and re-renders on theme change', async () => {
  const desktop = createMockEnv({ desktop: true, theme: 'dark' });
  desktop.api.set({ tint: '#45a8ff' });
  await desktop.flush();
  assert.equal(desktop.bgCanvas.style.opacity, '1');
  assert.equal(desktop.bgDraws.length, 1);
  assert.equal(desktop.pageBgCanvas.style.opacity, '1');
  assert.equal(desktop.pageBgDraws.length, 1);

  desktop.setTheme('light');
  await desktop.flush();
  assert.equal(desktop.bgDraws.length, 2);
  assert.equal(desktop.pageBgDraws.length, 2);

  desktop.api.set({ tint: '' });
  await desktop.flush();
  assert.equal(desktop.bgCanvas.style.opacity, '0');
  assert.equal(desktop.pageBgCanvas.style.opacity, '0');
  desktop.flushTimers();
  assert.equal(desktop.bgCanvas.width, 0);
  assert.equal(desktop.pageBgCanvas.width, 0);

  const mobile = createMockEnv({ desktop: false, theme: 'dark' });
  mobile.api.set({ tint: '#ff5a5f' });
  await mobile.flush();
  assert.equal(mobile.bgCanvas.style.opacity, '1');
  assert.equal(mobile.pageBgDraws.length, 0);
  assert.equal(mobile.pageBgCanvas.width, 0);
});

function hexToRgb(hex) {
  return [
    parseInt(hex.slice(1, 3), 16),
    parseInt(hex.slice(3, 5), 16),
    parseInt(hex.slice(5, 7), 16)
  ];
}

test('dark text palette produces bright moonlight text, tinted bold accents, and soft system text', () => {
  const env = createMockEnv();
  const cases = [
    ['#ff9d42', ([r, g, b]) => r > g && g > b],
    ['#ff5a5f', ([r, g, b]) => r > g && r > b],
    ['#57c96b', ([r, g, b]) => g > r && g > b]
  ];
  for (const [hex, checkHue] of cases) {
    const p = env.api.buildTextPalette(hex, false, 70);
    const bodyRgb = hexToRgb(p.text);
    const strongRgb = hexToRgb(p.strong);
    const sysRgb = hexToRgb(p.text3);
    assert.ok(bodyRgb[0] > 215 && bodyRgb[1] > 215 && bodyRgb[2] > 215, `${hex} body not moonlight: ${p.text}`);
    assert.ok(checkHue(strongRgb), `${hex} strong hue wrong: ${p.strong}`);
    assert.ok(Math.min(...sysRgb) > 100 && Math.max(...sysRgb) < 210, `${hex} system text contrast off: ${p.text3}`);
  }
});

test('light text palette produces crisp dark ink, warm caramel gold without green tint, and matching accents', () => {
  const env = createMockEnv();
  const cases = [
    ['#45a8ff', ([r, g, b]) => b > r && b >= g],
    ['#57c96b', ([r, g, b]) => g > r && g > b],
    ['#ffd84a', ([r, g, b]) => r >= g && g > b]
  ];
  for (const [hex, checkHue] of cases) {
    const p = env.api.buildTextPalette(hex, true, 70);
    const bodyRgb = hexToRgb(p.text);
    const strongRgb = hexToRgb(p.strong);
    const accRgb = hexToRgb(p.accent);
    assert.ok(bodyRgb[0] < 90 && bodyRgb[1] < 90 && bodyRgb[2] < 90, `${hex} ink too bright: ${p.text}`);
    assert.ok(checkHue(strongRgb), `${hex} strong hue wrong: ${p.strong}`);
    assert.ok(checkHue(accRgb), `${hex} accent hue wrong: ${p.accent}`);
  }
  const goldBody = hexToRgb(env.api.buildTextPalette('#ffd84a', true, 70).text);
  assert.ok(goldBody[0] > goldBody[1] && goldBody[1] > goldBody[2], `gold ink turned green: ${goldBody}`);
});

test('neutral gray text palette stays achromatic in both themes', () => {
  const env = createMockEnv();
  for (const light of [false, true]) {
    const p = env.api.buildTextPalette('#808080', light, 80);
    for (const key of ['text', 'text2', 'text3', 'strong', 'accent', 'quoteBar']) {
      const [r, g, b] = hexToRgb(p[key]);
      assert.ok(Math.abs(r - g) <= 1 && Math.abs(g - b) <= 1, `light=${light} ${key} not gray: ${p[key]}`);
    }
  }
});

test('text palette CSS variables sync immediately on tint, theme switch, and clear without touching speaker vars', () => {
  const env = createMockEnv({ theme: 'dark' });
  assert.equal(env.body.getAttribute('data-ambience-tint'), undefined);

  env.api.set({ tint: '#ff9d42' });
  assert.equal(env.body.getAttribute('data-ambience-tint'), '1');
  const darkText = env.body.style.getPropertyValue('--text');
  const darkStrong = env.body.style.getPropertyValue('--ambience-strong');
  assert.ok(darkText && darkStrong);
  assert.equal(env.body.style.getPropertyValue('--connor-accent'), '');

  env.setTheme('light');
  const lightText = env.body.style.getPropertyValue('--text');
  const lightStrong = env.body.style.getPropertyValue('--ambience-strong');
  assert.notEqual(lightText, darkText);
  assert.notEqual(lightStrong, darkStrong);

  env.api.set({ tint: '' });
  assert.equal(env.body.getAttribute('data-ambience-tint'), undefined);
  for (const key of ['--text', '--text2', '--text3', '--accent', '--ambience-strong', '--ambience-quote-bar']) {
    assert.equal(env.body.style.getPropertyValue(key), '');
  }
});

// 聊天氛围层：在背景图之上、消息之下叠加天气动效，并通过离线亮度 LUT 重绘底图色调。
// 效果和颜色由服务端保存，用户与 AI 共用；浓淡只存本机。
// 手机优先：粒子数量固定并循环复用，窄屏限 30 帧，没有东西可画时停掉循环并释放画布。
(() => {
  'use strict';
  const KEY = 'aion_chat_ambience';
  const EFFECTS = [
    ['rain', '🌧️', '下雨'], ['snow', '❄️', '下雪'], ['wind', '🍃', '刮风'], ['leaves', '🍂', '落叶'],
    ['sakura', '🌸', '樱花'], ['sun', '☀️', '阳光'], ['firefly', '✨', '萤火虫'], ['bubble', '🫧', '泡泡'],
    ['heart', '💗', '飘爱心'], ['dandelion', '🌼', '蒲公英'], ['fog', '🌫️', '晨雾'], ['aurora', '🌌', '极光'],
    ['hearth', '🔥', '炉火']
  ];
  const BURST_BUTTONS = [['confetti', '🎉', '撒花'], ['fireworks', '🎆', '烟花'], ['meteor', '🌠', '流星']];
  const TINTS = [
    ['#ff5a5f', '赤'], ['#ff9d42', '橙'], ['#ffd84a', '黄'], ['#57c96b', '绿'],
    ['#45a8ff', '蓝'], ['#4656c9', '靛'], ['#a05cff', '紫']
  ];
  const MIN_STRENGTH = 20;
  const MAX_STRENGTH = 100;
  const DEFAULT_STRENGTH = 70;
  const STATE_VERSION = 2;
  const TAU = Math.PI * 2;
  const BG_URLS = { dark: '/public/chat-bg-dark.jpg', light: '/public/chat-bg-light.jpg' };
  const BG_MAX_HEIGHT = { dark: 1280, light: 1120 };
  const IS_LITTLE_ENDIAN = (() => {
    const buf = new ArrayBuffer(4);
    new Uint32Array(buf)[0] = 0x0a0b0c0d;
    return new Uint8Array(buf)[0] === 0x0d;
  })();

  const area = document.querySelector('.chat-area');
  if (!area) return;
  const SIMS = { rain, snow, wind, leaves, sakura, sun, firefly, bubble, heart, dandelion, fog, aurora, hearth };
  const BURSTS = { confetti, fireworks, meteor };
  const has = (map, key) => Object.prototype.hasOwnProperty.call(map, key);

  function normalize(raw) {
    let data = raw;
    if (typeof raw === 'string') {
      try { data = JSON.parse(raw); } catch (_) { data = null; }
    }
    const next = { v: STATE_VERSION, effect: 'none', tint: '', strength: DEFAULT_STRENGTH };
    if (!data || typeof data !== 'object') return next;
    if (has(SIMS, data.effect)) next.effect = data.effect;
    if (typeof data.tint === 'string' && /^#[0-9a-f]{6}$/i.test(data.tint)) next.tint = data.tint.toLowerCase();
    // 旧版（无 v: 2）把浓淡当作遮罩透明度（默认 12%），迁移时重置为新算法默认 70%
    if (data.v === STATE_VERSION) {
      const strength = Number(data.strength);
      if (Number.isFinite(strength)) next.strength = Math.min(MAX_STRENGTH, Math.max(MIN_STRENGTH, Math.round(strength)));
    }
    return next;
  }

  let state = normalize(null);
  try { state = normalize(localStorage.getItem(KEY)); } catch (_) {}

  // ── 图层 ──
  const layer = document.createElement('div');
  layer.className = 'chat-ambience';
  layer.setAttribute('aria-hidden', 'true');
  const bgCanvas = document.createElement('canvas');
  bgCanvas.className = 'chat-ambience-bg';
  bgCanvas.width = bgCanvas.height = 0;
  const canvas = document.createElement('canvas');
  canvas.className = 'chat-ambience-canvas';
  canvas.width = canvas.height = 0;
  layer.append(bgCanvas, canvas);
  area.prepend(layer);

  const pageBgCanvas = document.createElement('canvas');
  pageBgCanvas.className = 'chat-ambience-page-bg';
  pageBgCanvas.setAttribute('aria-hidden', 'true');
  pageBgCanvas.width = pageBgCanvas.height = 0;
  document.body.prepend(pageBgCanvas);

  const bgCtx = bgCanvas.getContext('2d');
  const pageBgCtx = pageBgCanvas.getContext('2d');
  const desktopQuery = typeof window.matchMedia === 'function' ? window.matchMedia('(min-width: 769px)') : null;
  const isDesktop = () => (desktopQuery ? desktopQuery.matches : window.innerWidth >= 769);
  const ctx = canvas.getContext('2d');
  const subPage = document.getElementById('subPageOverlay');
  let width = 0;
  let height = 0;
  let dpr = 1;
  let frameGap = 0;
  let sim = null;
  let simArea = 0;
  let bursts = [];
  let raf = 0;
  let last = 0;
  let visible = true;

  const rand = (min, max) => min + Math.random() * (max - min);
  const isLight = () => document.body.dataset.theme === 'light';
  const density = () => Math.min(1.6, Math.max(0.5, width * height / 630000));

  function makeSprite(size, paint) {
    const sprite = document.createElement('canvas');
    sprite.width = sprite.height = size * 2;
    const c = sprite.getContext('2d');
    c.scale(2, 2);
    paint(c, size);
    return sprite;
  }
  // 一颗发光的小圆点，stops 是从圆心到边缘的颜色
  function glowSprite(size, stops) {
    return makeSprite(size, (c, s) => {
      const g = c.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2);
      for (const [at, color] of stops) g.addColorStop(at, color);
      c.fillStyle = g;
      c.fillRect(0, 0, s, s);
    });
  }
  // 整片的光只画一次，画在四分之一大小的小图上，之后每帧贴图即可。
  // pad 是四周多画出去的比例，给会转动的图留出边
  function lightMap(paint, pad = 0) {
    const spanX = width * (1 + pad * 2);
    const spanY = height * (1 + pad * 2);
    const map = document.createElement('canvas');
    map.width = Math.max(1, Math.ceil(spanX / 4));
    map.height = Math.max(1, Math.ceil(spanY / 4));
    const c = map.getContext('2d');
    c.scale(map.width / spanX, map.height / spanY);
    c.translate(width * pad, height * pad);
    paint(c);
    return map;
  }
  // 让之后画的东西绕 (x, y) 转一个角度
  function pivot(x, y, angle) {
    const cos = Math.cos(angle);
    const sin = Math.sin(angle);
    ctx.setTransform(
      cos * dpr, sin * dpr, -sin * dpr, cos * dpr,
      (x - x * cos + y * sin) * dpr, (y - x * sin - y * cos) * dpr
    );
  }
  // 飘出屏幕的从另一边回来
  function wrap(p, pad) {
    if (p.x < -pad) p.x = width + pad;
    else if (p.x > width + pad) p.x = -pad;
    if (p.y < -pad) p.y = height + pad;
    else if (p.y > height + pad) p.y = -pad;
  }
  // 把坐标系一次摆到 (x, y)、旋转并缩放；frame 在每个效果画完后统一复位
  function place(x, y, rot, scaleX, scaleY) {
    const cos = Math.cos(rot) * dpr;
    const sin = Math.sin(rot) * dpr;
    ctx.setTransform(cos * scaleX, sin * scaleX, -sin * scaleY, cos * scaleY, x * dpr, y * dpr);
  }

  // ── 下雨：按远近分三层，每层一笔画完 ──
  function rain() {
    const slant = 0.2;
    const light = isLight();
    const color = light ? '#6c84b4' : '#cfe0ff';
    const layers = [0, 1, 2].map(z => ({
      alpha: (light ? 0.24 : 0.18) + z * 0.13,
      lineWidth: 0.8 + z * 0.4,
      len: 10 + z * 7,
      speed: 520 + z * 240,
      drops: []
    }));
    const count = Math.round(150 * density());
    for (let index = 0; index < count; index++) {
      const y = rand(-30, height);
      layers[index % 3].drops.push({ x: rand(0, width + height * slant) - y * slant, y, pace: rand(0.85, 1.15) });
    }
    return {
      step(dt) {
        for (const item of layers) {
          for (const d of item.drops) {
            const dy = item.speed * d.pace * dt;
            d.y += dy;
            d.x -= dy * slant;
            if (d.y - item.len > height) {
              d.y = -rand(0, 80);
              d.x = rand(0, width + height * slant);
            }
          }
        }
      },
      draw() {
        ctx.strokeStyle = color;
        ctx.lineCap = 'round';
        for (const item of layers) {
          ctx.globalAlpha = item.alpha;
          ctx.lineWidth = item.lineWidth;
          ctx.beginPath();
          for (const d of item.drops) {
            ctx.moveTo(d.x, d.y);
            ctx.lineTo(d.x + item.len * slant, d.y - item.len);
          }
          ctx.stroke();
        }
      }
    };
  }

  // ── 下雪 ──
  function snow() {
    const light = isLight();
    // 浅色背景上给雪花加一圈冷灰边，才看得见
    const sprite = glowSprite(32, light
      ? [[0, 'rgba(255,255,255,1)'], [0.42, 'rgba(255,255,255,.96)'], [0.62, 'rgba(138,160,202,.45)'], [1, 'rgba(138,160,202,0)']]
      : [[0, 'rgba(255,255,255,1)'], [0.45, 'rgba(255,255,255,.85)'], [1, 'rgba(255,255,255,0)']]);
    function reset(f, anywhere) {
      f.r = rand(1.4, 4.4);
      f.size = f.r * 4.4;
      f.vy = 20 + f.r * 13 + rand(0, 14);
      f.amp = rand(10, 34);
      f.freq = rand(0.4, 1.1);
      f.phase = rand(0, TAU);
      f.alpha = rand(0.5, 0.95);
      f.x = rand(-20, width + 20);
      f.y = anywhere ? rand(-f.size, height) : -f.size - rand(0, 40);
      return f;
    }
    const flakes = Array.from({ length: Math.round(85 * density()) }, () => reset({}, true));
    return {
      step(dt) {
        for (const f of flakes) {
          f.phase += f.freq * dt;
          f.y += f.vy * dt;
          f.x += (10 + Math.cos(f.phase) * f.amp * f.freq) * dt;
          if (f.y - f.size > height) reset(f, false);
          else if (f.x > width + 24) f.x = -24;
          else if (f.x < -24) f.x = width + 24;
        }
      },
      draw() {
        for (const f of flakes) {
          ctx.globalAlpha = f.alpha;
          ctx.drawImage(sprite, f.x - f.size / 2, f.y - f.size / 2, f.size, f.size);
        }
      }
    };
  }

  // ── 飘动的小东西：落叶、花瓣、泡泡、爱心、蒲公英共用，只是贴图和参数不同 ──
  // from：down 从上往下落，up 从下往上升，side 从左往右飘；steady 表示不翻转、不打转
  function flutter(sprites, o) {
    const from = o.from || 'down';
    const tilt = o.tilt === undefined ? 0.6 : o.tilt;
    function reset(l, anywhere) {
      l.sprite = sprites[Math.floor(Math.random() * sprites.length)];
      l.size = rand(...o.size);
      l.vy = rand(...o.fall);
      l.amp = rand(...o.sway);
      l.freq = rand(0.9, 1.7);
      l.phase = rand(0, TAU);
      l.rot = o.steady ? 0 : rand(0, TAU);
      l.spin = o.steady ? 0 : rand(-0.6, 0.6);
      l.flip = o.steady ? 0 : rand(0, TAU);
      l.flipSpeed = o.steady ? 0 : rand(1.2, 2.8);
      l.alpha = rand(...(o.alpha || [0.75, 0.95]));
      l.x = rand(-40, width + 10);
      l.y = rand(-l.size, height);
      if (!anywhere && from === 'down') l.y = -l.size - rand(0, 60);
      if (!anywhere && from === 'up') l.y = height + l.size + rand(0, 60);
      if (!anywhere && from === 'side') l.x = -l.size - rand(0, 60);
      return l;
    }
    const items = Array.from({ length: Math.max(6, Math.round(o.count * density())) }, () => reset({}, true));
    return {
      step(dt) {
        for (const l of items) {
          l.phase += l.freq * dt;
          l.rot += l.spin * dt;
          l.flip += l.flipSpeed * dt;
          l.y += (l.vy + Math.sin(l.phase * 2) * 12) * dt;
          l.x += (o.drift + Math.cos(l.phase) * l.amp * l.freq) * dt;
          // 飘出去的那一侧才回收，刚生成在屏幕外的不算
          if (l.x - l.size > width
            || (from !== 'up' && l.y - l.size > height + 20)
            || (from !== 'down' && l.y + l.size < -20)) reset(l, false);
        }
      },
      draw() {
        for (const l of items) {
          let flip = Math.cos(l.flip);
          if (Math.abs(flip) < 0.14) flip = flip < 0 ? -0.14 : 0.14;
          ctx.globalAlpha = l.alpha;
          place(l.x, l.y, l.rot + Math.cos(l.phase) * tilt, flip, 1);
          ctx.drawImage(l.sprite, -l.size / 2, -l.size / 2, l.size, l.size);
        }
      }
    };
  }

  const LEAF_COLORS = [
    ['#f2a23a', '#c5631c'], ['#e4663a', '#a8381d'], ['#f4c64e', '#c98f1f'],
    ['#c9803a', '#8a4a1c'], ['#b9b24a', '#7d7a26']
  ];
  function leafSprite([from, to]) {
    return makeSprite(48, (c, s) => {
      c.translate(s / 2, s / 2);
      const g = c.createLinearGradient(-s * 0.3, -s * 0.4, s * 0.3, s * 0.4);
      g.addColorStop(0, from);
      g.addColorStop(1, to);
      c.beginPath();
      c.moveTo(0, -s * 0.44);
      c.bezierCurveTo(s * 0.34, -s * 0.24, s * 0.3, s * 0.2, 0, s * 0.38);
      c.bezierCurveTo(-s * 0.3, s * 0.2, -s * 0.34, -s * 0.24, 0, -s * 0.44);
      c.fillStyle = g;
      c.fill();
      c.strokeStyle = 'rgba(90,45,10,.5)';
      c.lineWidth = s * 0.028;
      c.lineCap = 'round';
      c.beginPath();
      c.moveTo(0, -s * 0.36);
      c.lineTo(0, s * 0.48);
      for (const y of [-0.16, 0.02, 0.18]) {
        c.moveTo(0, s * y);
        c.lineTo(s * 0.15, s * (y - 0.1));
        c.moveTo(0, s * y);
        c.lineTo(-s * 0.15, s * (y - 0.1));
      }
      c.stroke();
    });
  }
  function leaves() {
    return flutter(LEAF_COLORS.map(leafSprite), { count: 15, size: [18, 32], fall: [38, 78], sway: [26, 64], drift: 16 });
  }

  // ── 樱花：比落叶更小、更多、更慢 ──
  const PETAL_COLORS = [['#ffe6ef', '#ff86b2'], ['#ffd6e6', '#ff6fa3'], ['#fff0f5', '#ffa3c4']];
  function petalSprite([tip, base]) {
    const edge = isLight() ? 'rgba(206,84,132,.55)' : 'rgba(255,170,200,.35)';
    return makeSprite(32, (c, s) => {
      c.translate(s / 2, s / 2);
      const g = c.createLinearGradient(0, -s * 0.42, 0, s * 0.42);
      g.addColorStop(0, tip);
      g.addColorStop(1, base);
      c.beginPath();
      c.moveTo(0, s * 0.42);
      c.bezierCurveTo(s * 0.44, s * 0.18, s * 0.4, -s * 0.3, s * 0.1, -s * 0.42);
      c.quadraticCurveTo(0, -s * 0.28, -s * 0.1, -s * 0.42);
      c.bezierCurveTo(-s * 0.4, -s * 0.3, -s * 0.44, s * 0.18, 0, s * 0.42);
      c.fillStyle = g;
      c.fill();
      c.strokeStyle = edge;
      c.lineWidth = s * 0.03;
      c.stroke();
    });
  }
  function sakura() {
    return flutter(PETAL_COLORS.map(petalSprite), { count: 34, size: [13, 23], fall: [26, 52], sway: [22, 54], drift: 24 });
  }

  function bubbleSprite() {
    const rgb = isLight() ? '112,146,214' : '255,255,255';
    return makeSprite(32, (c, s) => {
      const g = c.createRadialGradient(s * 0.4, s * 0.38, s * 0.05, s / 2, s / 2, s * 0.46);
      g.addColorStop(0, `rgba(${rgb},.05)`);
      g.addColorStop(0.8, `rgba(${rgb},.12)`);
      g.addColorStop(1, `rgba(${rgb},.5)`);
      c.fillStyle = g;
      c.beginPath();
      c.arc(s / 2, s / 2, s * 0.46, 0, TAU);
      c.fill();
      c.strokeStyle = `rgba(${rgb},.75)`;
      c.lineWidth = s * 0.035;
      c.stroke();
      // 左上角一道高光
      c.strokeStyle = 'rgba(255,255,255,.9)';
      c.lineWidth = s * 0.06;
      c.lineCap = 'round';
      c.beginPath();
      c.arc(s / 2, s / 2, s * 0.32, Math.PI * 1.1, Math.PI * 1.4);
      c.stroke();
    });
  }
  function bubble() {
    return flutter([bubbleSprite()], {
      count: 16, size: [10, 30], fall: [-55, -22], sway: [10, 30], drift: 0,
      from: 'up', steady: true, tilt: 0, alpha: [0.6, 0.95]
    });
  }

  const HEART_COLORS = [['#ff9cc0', '#ff4f86'], ['#ffb3c7', '#ff6b8f'], ['#ff8f9f', '#f0405e']];
  function heartSprite([from, to]) {
    return makeSprite(32, (c, s) => {
      const g = c.createLinearGradient(0, s * 0.15, 0, s * 0.9);
      g.addColorStop(0, from);
      g.addColorStop(1, to);
      c.beginPath();
      c.moveTo(s * 0.5, s * 0.88);
      c.bezierCurveTo(s * 0.02, s * 0.56, s * 0.1, s * 0.12, s * 0.5, s * 0.32);
      c.bezierCurveTo(s * 0.9, s * 0.12, s * 0.98, s * 0.56, s * 0.5, s * 0.88);
      c.fillStyle = g;
      c.fill();
    });
  }
  function heart() {
    return flutter(HEART_COLORS.map(heartSprite), {
      count: 14, size: [14, 26], fall: [-58, -26], sway: [14, 36], drift: 0,
      from: 'up', steady: true, tilt: 0.3, alpha: [0.55, 0.9]
    });
  }

  function seedSprite() {
    const light = isLight();
    return makeSprite(32, (c, s) => {
      c.translate(s / 2, s * 0.42);
      c.strokeStyle = light ? 'rgba(112,122,156,.85)' : 'rgba(255,255,255,.88)';
      c.lineWidth = s * 0.03;
      c.lineCap = 'round';
      c.beginPath();
      // 顶上一把散开的绒毛，下面一根细柄
      for (let index = 0; index < 9; index++) {
        const angle = Math.PI * (1.08 + index * 0.105);
        c.moveTo(0, 0);
        c.lineTo(Math.cos(angle) * s * 0.4, Math.sin(angle) * s * 0.4);
      }
      c.moveTo(0, 0);
      c.lineTo(0, s * 0.42);
      c.stroke();
      c.fillStyle = light ? 'rgba(120,88,56,.9)' : 'rgba(214,190,150,.95)';
      c.beginPath();
      c.ellipse(0, s * 0.47, s * 0.035, s * 0.08, 0, 0, TAU);
      c.fill();
    });
  }
  function dandelion() {
    return flutter([seedSprite()], {
      count: 16, size: [18, 30], fall: [-8, 12], sway: [8, 20], drift: 34,
      from: 'side', steady: true, tilt: 0.25, alpha: [0.7, 0.95]
    });
  }

  // ── 萤火虫：慢慢游走、一明一暗的光点 ──
  function firefly() {
    const sprite = glowSprite(24, isLight()
      ? [[0, 'rgba(255,246,170,1)'], [0.3, 'rgba(240,214,70,.9)'], [0.6, 'rgba(150,160,40,.35)'], [1, 'rgba(150,160,40,0)']]
      : [[0, 'rgba(255,255,220,1)'], [0.3, 'rgba(214,255,130,.85)'], [1, 'rgba(160,255,90,0)']]);
    const bugs = Array.from({ length: Math.max(10, Math.round(26 * density())) }, () => ({
      x: rand(0, width),
      y: rand(0, height),
      size: rand(10, 20),
      heading: rand(0, TAU),
      speed: rand(8, 22),
      phase: rand(0, TAU),
      blink: rand(0.6, 1.6)
    }));
    return {
      gap: 48,
      step(dt) {
        for (const b of bugs) {
          b.heading += rand(-1.5, 1.5) * dt;
          b.phase += b.blink * dt;
          b.x += Math.cos(b.heading) * b.speed * dt;
          b.y += Math.sin(b.heading) * b.speed * dt;
          wrap(b, 12);
        }
      },
      draw() {
        for (const b of bugs) {
          // 暗下去时留一点余光，不至于整片消失
          const glow = 0.5 + 0.5 * Math.sin(b.phase);
          ctx.globalAlpha = 0.1 + 0.9 * glow * glow;
          ctx.drawImage(sprite, b.x - b.size / 2, b.y - b.size / 2, b.size, b.size);
        }
      }
    };
  }

  // 把一张左右接得上的图横向循环平移着贴出来
  function scroll(map, offset, y, drawHeight) {
    const shift = ((offset % width) + width) % width;
    ctx.drawImage(map, -shift, y, width, drawHeight);
    ctx.drawImage(map, width - shift - 0.5, y, width + 1, drawHeight);
  }

  // ── 晨雾：两层横向首尾相接的雾图朝相反方向飘，交错时形状一直在变 ──
  function fog() {
    const light = isLight();
    const rgb = light ? '255,255,255' : '196,208,236';
    const layers = [26, -15].map((speed, index) => ({
      speed,
      phase: index * 2.4,
      map: lightMap(c => {
        for (let count = 0; count < 10; count++) {
          const x = rand(0, width);
          const y = rand(height * 0.08, height * 0.95);
          const rx = rand(width * 0.25, width * 0.6);
          const ry = rand(height * 0.05, height * 0.12);
          const g = c.createRadialGradient(0, 0, 0, 0, 0, 1);
          // 浅色背景上白雾不显眼，给得浓一些
          g.addColorStop(0, `rgba(${rgb},${light ? rand(0.22, 0.38) : rand(0.1, 0.18)})`);
          g.addColorStop(1, `rgba(${rgb},0)`);
          c.fillStyle = g;
          // 左右各补画一份，平移到边缘时接得上
          for (const shift of [-width, 0, width]) {
            c.save();
            c.translate(x + shift, y);
            c.scale(rx, ry);
            c.fillRect(-1, -1, 2, 2);
            c.restore();
          }
        }
      })
    }));
    let time = 0;
    return {
      gap: 48,
      step(dt) { time += dt; },
      draw() {
        for (const item of layers) {
          ctx.globalAlpha = 0.75 + 0.25 * Math.sin(time * 0.5 + item.phase);
          scroll(item.map, time * item.speed, 0, height);
        }
      }
    };
  }

  // ── 极光：两条光带各画成一张左右接得上的图，朝相反方向流动，同时起伏、伸缩、明暗变化 ──
  function aurora() {
    const light = isLight();
    const lightness = light ? 55 : 66;
    const ribbons = [[150, 190, 0.02, 22], [285, 170, 0.1, -14]].map(([hueTop, hueBottom, drop, speed], index) => ({
      speed,
      phase: index * 2.1,
      map: lightMap(c => {
        const p = Array.from({ length: 5 }, () => rand(0, TAU));
        for (let x = 0; x < width; x += 4) {
          // 波形只用整数倍频率，图的左右两端才接得上
          const u = x / width * TAU;
          const top = height * (drop + 0.06 + 0.05 * Math.sin(u + p[0]) + 0.025 * Math.sin(u * 3 + p[1]));
          const length = height * (0.24 + 0.08 * Math.sin(u * 2 + p[2]));
          const alpha = (0.3 + 0.16 * Math.sin(u * 7 + p[3]) + 0.1 * Math.sin(u * 13 + p[4])) * (light ? 1.25 : 1);
          const g = c.createLinearGradient(0, top, 0, top + length);
          g.addColorStop(0, `hsla(${hueTop},90%,${lightness}%,0)`);
          g.addColorStop(0.3, `hsla(${hueTop},90%,${lightness}%,${alpha})`);
          g.addColorStop(1, `hsla(${hueBottom},85%,${lightness}%,0)`);
          c.fillStyle = g;
          c.fillRect(x, top, 4.5, length);
        }
      })
    }));
    let time = 0;
    return {
      gap: 48,
      step(dt) { time += dt; },
      draw() {
        for (const r of ribbons) {
          ctx.globalAlpha = 0.7 + 0.3 * Math.sin(time * 0.9 + r.phase);
          scroll(
            r.map, time * r.speed,
            Math.sin(time * 0.5 + r.phase) * height * 0.015,
            height * (1 + 0.1 * Math.sin(time * 0.37 + r.phase))
          );
        }
      }
    };
  }

  // ── 炉火：底部一片忽明忽暗的暖光，加几粒往上飘的火星 ──
  function hearth() {
    const strength = isLight() ? 0.8 : 1;
    const map = lightMap(c => {
      const g = c.createRadialGradient(width / 2, height * 1.08, 0, width / 2, height * 1.08, height * 0.6);
      g.addColorStop(0, `rgba(255,150,60,${0.55 * strength})`);
      g.addColorStop(0.5, `rgba(255,112,40,${0.2 * strength})`);
      g.addColorStop(1, 'rgba(255,90,30,0)');
      c.fillStyle = g;
      c.fillRect(0, 0, width, height);
    });
    const sprite = glowSprite(16, [[0, 'rgba(255,240,190,1)'], [0.35, 'rgba(255,170,70,.9)'], [1, 'rgba(255,110,30,0)']]);
    function reset(e, anywhere) {
      e.x = rand(width * 0.05, width * 0.95);
      e.endY = height * rand(0.45, 0.75);
      e.y = anywhere ? rand(e.endY, height) : height + rand(0, 20);
      e.vy = rand(30, 70);
      e.amp = rand(6, 18);
      e.phase = rand(0, TAU);
      e.size = rand(5, 10);
      return e;
    }
    const embers = Array.from({ length: Math.max(6, Math.round(14 * density())) }, () => reset({}, true));
    let time = 0;
    return {
      gap: 48,
      step(dt) {
        time += dt;
        for (const e of embers) {
          e.phase += 2.2 * dt;
          e.y -= e.vy * dt;
          e.x += Math.cos(e.phase) * e.amp * dt;
          if (e.y < e.endY) reset(e, false);
        }
      },
      draw() {
        ctx.globalAlpha = 0.78 + 0.12 * Math.sin(time * 7.3) + 0.08 * Math.sin(time * 12.7 + 1);
        ctx.drawImage(map, 0, 0, width, height);
        for (const e of embers) {
          // 越往上越淡
          ctx.globalAlpha = Math.min(1, (e.y - e.endY) / (height * 0.2));
          ctx.drawImage(sprite, e.x - e.size / 2, e.y - e.size / 2, e.size, e.size);
        }
      }
    };
  }

  // ── 阳光：左上角的暖光和光柱，再加些浮尘 ──
  function sun() {
    const light = isLight();
    const rgb = light ? '255,240,190' : '255,226,160';
    const strength = light ? 1 : 0.7;
    const ox = -width * 0.08;
    const oy = -height * 0.05;
    const reach = Math.hypot(width, height) * 1.15;
    const pad = 0.12;
    const glow = lightMap(c => {
      const g = c.createRadialGradient(ox, oy, 0, ox, oy, reach * 0.8);
      g.addColorStop(0, `rgba(${rgb},${0.42 * strength})`);
      g.addColorStop(0.45, `rgba(${rgb},${0.14 * strength})`);
      g.addColorStop(1, `rgba(${rgb},0)`);
      c.fillStyle = g;
      c.fillRect(0, 0, width, height);
    });
    // 光柱分成两组各画一张图，绕着光源朝相反方向慢慢摆动，明暗也错开
    const beams = [0, 1].map(group => ({
      phase: group * 2.6,
      turn: group ? -1 : 1,
      map: lightMap(c => {
        for (let index = group; index < 6; index += 2) {
          const angle = (16 + index * 11 + rand(-3, 3)) * Math.PI / 180;
          const spread = rand(0.025, 0.07);
          const alpha = rand(0.16, 0.28) * strength;
          const g = c.createLinearGradient(ox, oy, ox + Math.cos(angle) * reach, oy + Math.sin(angle) * reach);
          g.addColorStop(0, `rgba(${rgb},${alpha})`);
          g.addColorStop(0.7, `rgba(${rgb},${alpha * 0.45})`);
          g.addColorStop(1, `rgba(${rgb},0)`);
          c.fillStyle = g;
          // 宽的一层打底，窄的一层叠出更亮的光芯
          for (const part of [1, 0.4]) {
            c.beginPath();
            c.moveTo(ox, oy);
            c.lineTo(ox + Math.cos(angle - spread * part) * reach, oy + Math.sin(angle - spread * part) * reach);
            c.lineTo(ox + Math.cos(angle + spread * part) * reach, oy + Math.sin(angle + spread * part) * reach);
            c.closePath();
            c.fill();
          }
        }
      }, pad)
    }));
    const mote = glowSprite(16, light
      ? [[0, 'rgba(255,250,225,1)'], [0.4, 'rgba(255,244,200,.95)'], [0.65, 'rgba(222,160,70,.4)'], [1, 'rgba(222,160,70,0)']]
      : [[0, 'rgba(255,250,225,1)'], [0.45, 'rgba(255,236,180,.8)'], [1, 'rgba(255,236,180,0)']]);
    const motes = Array.from({ length: Math.round(36 * density()) }, () => ({
      x: rand(0, width),
      y: rand(0, height),
      size: rand(1, 2.6) * 4.4,
      vx: rand(4, 20),
      vy: rand(2, 14),
      phase: rand(0, TAU),
      speed: rand(0.8, 2)
    }));
    let time = 0;
    return {
      gap: 48,
      step(dt) {
        time += dt;
        for (const m of motes) {
          m.phase += m.speed * dt;
          m.x += (m.vx + Math.cos(m.phase) * 8) * dt;
          m.y += (m.vy + Math.sin(m.phase * 0.7) * 8) * dt;
          wrap(m, 10);
        }
      },
      draw() {
        ctx.globalAlpha = 0.85 + 0.15 * Math.sin(time * 0.6);
        ctx.drawImage(glow, 0, 0, width, height);
        for (const b of beams) {
          ctx.globalAlpha = 0.72 + 0.28 * Math.sin(time * 0.8 + b.phase);
          pivot(ox, oy, b.turn * 0.035 * Math.sin(time * 0.45 + b.phase));
          ctx.drawImage(b.map, -width * pad, -height * pad, width * (1 + pad * 2), height * (1 + pad * 2));
        }
        settle();
        for (const m of motes) {
          ctx.globalAlpha = 0.35 + 0.25 * (1 + Math.sin(m.phase * 2));
          ctx.drawImage(mote, m.x - m.size / 2, m.y - m.size / 2, m.size, m.size);
        }
      }
    };
  }

  // ── 刮风：带回旋的风线 + 被吹走的细尘 ──
  function wind() {
    const light = isLight();
    const rgb = light ? '100,116,156' : '226,236,255';
    const peak = light ? 0.55 : 0.5;
    function resetWisp(w, first) {
      w.y = rand(height * 0.06, height * 0.94);
      w.len = rand(170, 300);
      w.speed = rand(300, 460);
      w.amp = rand(5, 16);
      w.wave = rand(190, 340);
      w.phase = rand(0, TAU);
      w.lineWidth = rand(1.1, 2);
      w.loopAt = Math.random() < 0.6 ? rand(width * 0.2, width * 0.8) : -1;
      w.loopR = rand(13, 22);
      w.head = first ? rand(-width * 0.6, width) : -rand(40, width * 0.7);
      return w;
    }
    function resetSpeck(s, anywhere) {
      s.x = anywhere ? rand(0, width) : -rand(10, 140);
      s.y = rand(0, height);
      s.speed = rand(380, 640);
      s.len = rand(8, 22);
      s.phase = rand(0, TAU);
      return s;
    }
    const wisps = Array.from({ length: Math.max(4, Math.round(8 * density())) }, () => resetWisp({}, true));
    const specks = Array.from({ length: Math.round(34 * density()) }, () => resetSpeck({}, true));
    return {
      step(dt) {
        for (const w of wisps) {
          w.head += w.speed * dt;
          if (w.head - w.len > width + 30) resetWisp(w, false);
        }
        for (const s of specks) {
          s.phase += dt * 3;
          s.x += s.speed * dt;
          s.y += (14 + Math.sin(s.phase) * 18) * dt;
          if (s.x - s.len > width) resetSpeck(s, false);
        }
      },
      draw() {
        ctx.lineCap = 'round';
        ctx.lineJoin = 'round';
        for (const w of wisps) {
          const tail = w.head - w.len;
          if (w.head < 0 || tail > width) continue;
          const g = ctx.createLinearGradient(tail, 0, w.head, 0);
          g.addColorStop(0, `rgba(${rgb},0)`);
          g.addColorStop(0.55, `rgba(${rgb},${peak})`);
          g.addColorStop(0.9, `rgba(${rgb},${peak * 0.8})`);
          g.addColorStop(1, `rgba(${rgb},0)`);
          ctx.strokeStyle = g;
          ctx.lineWidth = w.lineWidth;
          ctx.beginPath();
          const loopLength = w.loopR * 3.6;
          for (let t = tail; t <= w.head; t += 6) {
            let x = t;
            let y = w.y + Math.sin(t / w.wave * TAU + w.phase) * w.amp;
            if (w.loopAt >= 0 && t > w.loopAt) {
              // 走过的距离小于圆周长，线条就会打一个回旋
              const theta = Math.min(1, (t - w.loopAt) / loopLength) * TAU;
              x -= w.loopR * Math.sin(theta);
              y -= w.loopR * (1 - Math.cos(theta));
            }
            if (t === tail) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
          }
          ctx.stroke();
        }
        // 细尘合成一笔
        ctx.strokeStyle = `rgba(${rgb},${light ? 0.28 : 0.22})`;
        ctx.lineWidth = 1;
        ctx.beginPath();
        for (const s of specks) {
          ctx.moveTo(s.x - s.len, s.y - 1.5);
          ctx.lineTo(s.x, s.y);
        }
        ctx.stroke();
      }
    };
  }

  // ── 瞬间特效：持续约十秒后自动结束，step 返回 false 表示播完，随后整组粒子被丢弃 ──
  const BURST_SECONDS = 10;
  const CONFETTI_COLORS = ['#ff5a5f', '#ff9d42', '#ffd84a', '#57c96b', '#45a8ff', '#a05cff', '#ff7eb6'];
  function confetti() {
    const pieces = Array.from({ length: Math.round(100 * density()) }, () => ({
      x: rand(0, width),
      y: -rand(10, height * 0.35),
      w: rand(6, 11),
      h: rand(4, 7),
      vy: rand(220, 360),
      amp: rand(20, 60),
      freq: rand(1.5, 3),
      phase: rand(0, TAU),
      rot: rand(0, TAU),
      spin: rand(-5, 5),
      flip: rand(0, TAU),
      flipSpeed: rand(4, 9),
      color: CONFETTI_COLORS[Math.floor(Math.random() * CONFETTI_COLORS.length)]
    }));
    let time = 0;
    return {
      step(dt) {
        time += dt;
        let alive = false;
        for (const p of pieces) {
          if (p.gone) continue;
          p.phase += p.freq * dt;
          p.rot += p.spin * dt;
          p.flip += p.flipSpeed * dt;
          p.y += p.vy * dt;
          p.x += Math.cos(p.phase) * p.amp * dt;
          if (p.y > height + 12) {
            // 落到底的纸片回到顶上再落一次，直到来不及在结束前落完
            if (time + (height + 50) / p.vy > BURST_SECONDS) {
              p.gone = true;
              continue;
            }
            p.x = rand(0, width);
            p.y = -rand(10, 40);
          }
          alive = true;
        }
        return alive;
      },
      draw() {
        for (const p of pieces) {
          if (p.gone || p.y < -12) continue;
          place(p.x, p.y, p.rot, 1, Math.cos(p.flip));
          ctx.fillStyle = p.color;
          ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h);
        }
      }
    };
  }

  function fireworks() {
    const tone = isLight() ? '85%, 52%' : '100%, 72%';
    const riseTime = 0.75;
    const life = 1.7;
    // 每隔半秒多升空一朵，最后一朵也赶在结束前放完
    const count = Math.floor((BURST_SECONDS - riseTime - life - 0.3) / 0.65) + 1;
    const shells = Array.from({ length: count }, (_, index) => ({
      delay: index * 0.65 + rand(0, 0.3),
      x: rand(width * 0.15, width * 0.85),
      y: height + 10,
      targetY: rand(height * 0.14, height * 0.45),
      color: `hsl(${Math.floor(rand(0, 360))}, ${tone})`,
      age: 0,
      sparks: null
    }));
    return {
      step(dt) {
        let alive = false;
        for (const s of shells) {
          if (s.delay > 0) {
            s.delay -= dt;
            alive = true;
            continue;
          }
          if (s.done) continue;
          s.age += dt;
          if (!s.sparks) {
            // 升空：先快后慢，到顶后炸开
            const k = Math.min(1, s.age / riseTime);
            s.y = height + 10 - (height + 10 - s.targetY) * (1 - (1 - k) * (1 - k));
            if (k >= 1) {
              s.age = 0;
              s.sparks = Array.from({ length: 40 }, () => {
                const angle = rand(0, TAU);
                const speed = rand(50, 190);
                return { x: s.x, y: s.targetY, vx: Math.cos(angle) * speed, vy: Math.sin(angle) * speed };
              });
            }
            alive = true;
          } else if (s.age < life) {
            const drag = Math.max(0, 1 - 1.6 * dt);
            for (const p of s.sparks) {
              p.vx *= drag;
              p.vy = p.vy * drag + 70 * dt;
              p.x += p.vx * dt;
              p.y += p.vy * dt;
            }
            alive = true;
          } else {
            // 放完的这一朵马上丢掉火星，不等整场结束
            s.done = true;
            s.sparks = null;
          }
        }
        return alive;
      },
      draw() {
        ctx.lineCap = 'round';
        for (const s of shells) {
          if (s.delay > 0 || s.done) continue;
          ctx.strokeStyle = s.color;
          ctx.beginPath();
          if (!s.sparks) {
            ctx.globalAlpha = 0.8;
            ctx.lineWidth = 2;
            ctx.moveTo(s.x, s.y + 18);
            ctx.lineTo(s.x, s.y);
          } else {
            // 每朵烟花的火星合成一笔
            ctx.globalAlpha = 1 - s.age / life;
            ctx.lineWidth = 2.2;
            for (const p of s.sparks) {
              ctx.moveTo(p.x - p.vx * 0.06, p.y - p.vy * 0.06);
              ctx.lineTo(p.x, p.y);
            }
          }
          ctx.stroke();
        }
      }
    };
  }

  function meteor() {
    const rgb = isLight() ? '104,112,196' : '255,255,255';
    const dx = -0.8;
    const dy = 0.6;
    // 大约每秒划过一颗，最后一颗也赶在结束前飞出屏幕
    const count = Math.floor((BURST_SECONDS - 1.2) / 0.95) + 1;
    const stars = Array.from({ length: count }, (_, index) => ({
      delay: index * 0.95 + rand(0, 0.4),
      x: rand(width * 0.35, width * 1.1),
      y: rand(-40, height * 0.25),
      len: rand(130, 220),
      speed: rand(650, 900),
      lineWidth: rand(1.6, 2.6)
    }));
    return {
      step(dt) {
        let alive = false;
        for (const s of stars) {
          if (s.delay > 0) {
            s.delay -= dt;
            alive = true;
            continue;
          }
          if (s.gone) continue;
          s.x += dx * s.speed * dt;
          s.y += dy * s.speed * dt;
          if (s.x > -s.len && s.y < height + s.len) alive = true;
          else s.gone = true;
        }
        return alive;
      },
      draw() {
        ctx.lineCap = 'round';
        for (const s of stars) {
          if (s.delay > 0 || s.gone) continue;
          const tailX = s.x - dx * s.len;
          const tailY = s.y - dy * s.len;
          const g = ctx.createLinearGradient(s.x, s.y, tailX, tailY);
          g.addColorStop(0, `rgba(${rgb},.95)`);
          g.addColorStop(1, `rgba(${rgb},0)`);
          ctx.strokeStyle = g;
          ctx.lineWidth = s.lineWidth;
          ctx.beginPath();
          ctx.moveTo(s.x, s.y);
          ctx.lineTo(tailX, tailY);
          ctx.stroke();
          ctx.fillStyle = `rgba(${rgb},.9)`;
          ctx.beginPath();
          ctx.arc(s.x, s.y, s.lineWidth * 1.1, 0, TAU);
          ctx.fill();
        }
      }
    };
  }

  // ── 动画循环 ──
  // 页面不可见或被子页面盖住时暂停
  function shown() {
    return visible && !document.hidden && !(subPage && subPage.classList.contains('show'));
  }
  // 画布只在有东西要画时才占内存
  function fit() {
    const w = Math.round(width * dpr);
    const h = Math.round(height * dpr);
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  function settle() {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.globalAlpha = 1;
  }
  function frame(now) {
    raf = 0;
    if (!shown()) return;
    // 限帧：手机 30 帧；光、雾这类慢动作自带更低的帧率
    const gap = bursts.length || !sim ? frameGap : Math.max(frameGap, sim.gap || 0);
    if (last && now - last < gap) {
      raf = requestAnimationFrame(frame);
      return;
    }
    const dt = last ? Math.min(0.1, (now - last) / 1000) : 0.016;
    last = now;
    ctx.clearRect(0, 0, width, height);
    if (sim) {
      sim.step(dt);
      sim.draw();
      settle();
    }
    if (bursts.length) {
      bursts = bursts.filter(item => {
        // 正常会在十秒内自己播完，这里再兜个底
        item.age = (item.age || 0) + dt;
        const alive = item.step(dt) && item.age < BURST_SECONDS + 2;
        if (alive) item.draw();
        settle();
        return alive;
      });
    }
    if (sim || bursts.length) raf = requestAnimationFrame(frame);
    else sync();
  }
  function sync() {
    // 看不见的时候不攒着特效，免得回来才播
    if (!shown()) bursts = [];
    const active = Boolean(sim || bursts.length);
    if (!active && canvas.width) canvas.width = canvas.height = 0;
    if (!active || !shown()) {
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
    } else if (!raf) {
      last = 0;
      raf = requestAnimationFrame(frame);
    }
  }
  function rebuild() {
    simArea = width * height;
    sim = state.effect !== 'none' && simArea > 0 ? SIMS[state.effect]() : null;
    if (sim) fit();
    sync();
  }
  function burst(name) {
    if (!has(BURSTS, name) || !shown() || !width || !height) return;
    fit();
    bursts.push(BURSTS[name]());
    if (bursts.length > 2) bursts.shift();
    sync();
  }
  function measure() {
    width = layer.clientWidth;
    height = layer.clientHeight;
    const narrow = width < 700;
    dpr = Math.min(window.devicePixelRatio || 1, narrow ? 1.5 : 2);
    // 手机限 30 帧：省电，也减轻气泡毛玻璃跟着重绘的负担
    frameGap = narrow ? 30 : 0;
    // 键盘弹出这类小幅尺寸变化不重置粒子
    const nextArea = width * height;
    if (state.effect !== 'none' && (!sim || !simArea || Math.abs(nextArea - simArea) / simArea > 0.3)) rebuild();
    else if (sim || bursts.length) fit();
  }

  // ── 离线 1D 亮度 LUT 重着色 ──
  const lumaCache = { dark: null, light: null };
  const lumaPending = { dark: null, light: null };
  let bgImageData = null;
  let tintRaf = 0;
  let tintSeq = 0;
  let clearTimer = 0;

  const clamp01 = x => (x < 0 ? 0 : x > 1 ? 1 : x);
  const smoothstep = (e0, e1, x) => {
    const t = clamp01((x - e0) / (e1 - e0));
    return t * t * (3 - 2 * t);
  };

  function hexToRgb(hex) {
    if (typeof hex !== 'string' || !/^#[0-9a-f]{6}$/i.test(hex)) return [1, 1, 1];
    return [
      parseInt(hex.slice(1, 3), 16) / 255,
      parseInt(hex.slice(3, 5), 16) / 255,
      parseInt(hex.slice(5, 7), 16) / 255
    ];
  }

  function hslToRgb01(h, s, l) {
    const c = (1 - Math.abs(2 * l - 1)) * s;
    const hp = (((h % 360) + 360) % 360) / 60;
    const x = c * (1 - Math.abs((hp % 2) - 1));
    const m = l - c * 0.5;
    let r = 0, g = 0, b = 0;
    const sector = Math.floor(hp) % 6;
    if (sector === 0) { r = c; g = x; }
    else if (sector === 1) { r = x; g = c; }
    else if (sector === 2) { g = c; b = x; }
    else if (sector === 3) { g = x; b = c; }
    else if (sector === 4) { r = x; b = c; }
    else { r = c; b = x; }
    return [clamp01(r + m), clamp01(g + m), clamp01(b + m)];
  }

  function targetColorMeta(hex) {
    const [r, g, b] = hexToRgb(hex);
    const mx = Math.max(r, g, b);
    const mn = Math.min(r, g, b);
    const df = mx - mn;
    let h = 0;
    if (df > 1e-6) {
      if (mx === r) h = (60 * ((g - b) / df) + 360) % 360;
      else if (mx === g) h = (60 * ((b - r) / df) + 120) % 360;
      else h = (60 * ((r - g) / df) + 240) % 360;
    }
    const sTarget = mx > 1e-6 ? df / mx : 0;
    const chromaFactor = Math.min(1, sTarget / 0.25);
    const [pr, pg, pb] = hslToRgb01(h, 1, 0.5);
    const yHue = 0.2126 * pr + 0.7152 * pg + 0.0722 * pb;
    const arc = ((280 - h + 180) % 360) - 180;
    const dirSign = arc >= 0 ? 1 : -1;
    return { h, yHue, dirSign, chromaFactor };
  }

  function hslHex(h, sPct, lPct) {
    const [r, g, b] = hslToRgb01(h, clamp01(sPct / 100), clamp01(lPct / 100));
    const toHex = c => Math.round(c * 255).toString(16).padStart(2, '0');
    return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
  }

  function hslaCss(h, sPct, lPct, alpha) {
    const [r, g, b] = hslToRgb01(h, clamp01(sPct / 100), clamp01(lPct / 100));
    return `rgba(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)}, ${alpha})`;
  }

  const clampStrength = value => Math.min(MAX_STRENGTH, Math.max(MIN_STRENGTH, Number(value) || DEFAULT_STRENGTH));

  function buildTextPalette(hex, light, strength = DEFAULT_STRENGTH) {
    const { h: ht, chromaFactor } = targetColorMeta(hex);
    const k = clampStrength(strength) / 70;
    const satScale = (0.45 + 0.55 * k) * chromaFactor;
    if (light) {
      const hInk = (ht - 13 * smoothstep(35, 50, ht) * (1 - smoothstep(65, 85, ht)) + 360) % 360;
      return {
        text: hslHex(hInk, Math.min(46, 28 * satScale), 19),
        text2: hslHex(hInk, Math.min(40, 24 * satScale), 36),
        text3: hslHex(hInk, Math.min(34, 22 * satScale), 47),
        strong: hslHex(hInk, Math.min(72, 58 * satScale), 31),
        accent: hslHex(hInk, Math.min(68, 54 * satScale), 40),
        accent2: hslHex(hInk, Math.min(68, 56 * satScale), 34),
        quoteBar: hslHex(hInk, Math.min(65, 50 * satScale), 42),
        borderStrong: hslaCss(hInk, Math.min(62, 46 * satScale), 42, 0.42),
        linkUnderline: hslaCss(hInk, Math.min(68, 54 * satScale), 40, 0.42)
      };
    }
    return {
      text: hslHex(ht, Math.min(58, 34 * satScale), 94 - Math.max(0, k - 1) * 4),
      text2: hslHex(ht, Math.min(50, 32 * satScale), 78),
      text3: hslHex(ht, Math.min(42, 28 * satScale), 64),
      strong: hslHex(ht, Math.min(90, 76 * satScale), 82 - Math.max(0, k - 1) * 5),
      accent: hslHex(ht, Math.min(88, 78 * satScale), 74),
      accent2: hslHex(ht, Math.min(84, 72 * satScale), 78),
      quoteBar: hslHex(ht, Math.min(82, 68 * satScale), 72),
      borderStrong: hslaCss(ht, Math.min(72, 58 * satScale), 76, 0.36),
      linkUnderline: hslaCss(ht, Math.min(88, 78 * satScale), 74, 0.46)
    };
  }

  function buildRecolorLut(hex, light, strength = DEFAULT_STRENGTH) {
    const { h: ht, yHue, dirSign, chromaFactor } = targetColorMeta(hex);
    const k = clampStrength(strength) / 70;
    const lut = new Uint8Array(256 * 3);
    for (let i = 0; i < 256; i++) {
      const v = i / 255;
      let hue, sat, lum;
      if (light) {
        const t = clamp01((v - 0.65) / (0.99 - 0.65));
        hue = (ht + dirSign * (12 * (1 - t) - 10 * t) + 360) % 360;
        const lBase = 0.71 + 0.285 * Math.pow(t, 0.92);
        const lShift = (1 - k) * 0.025 * (1 - t);
        lum = Math.min(0.996, Math.max(0.66, lBase + 0.035 * Math.max(0, yHue - 0.55) * (1 - t) + lShift));
        const starFade = 1 - smoothstep(0.84, 0.99, t);
        const sScale = 0.35 + 0.65 * k;
        sat = Math.min(0.78, Math.max(0, (0.44 + 0.16 * smoothstep(0, 0.70, t)) * starFade * sScale * chromaFactor));
      } else {
        const lumComp = Math.pow(0.28 / (0.18 + yHue), 0.35);
        const lSky = 0.010 + 0.014 * smoothstep(0.015, 0.055, v);
        const nebT = clamp01((v - 0.042) / (0.36 - 0.042));
        const nebScale = 0.72 + 0.28 * k;
        const lNeb = 0.38 * Math.pow(nebT, 0.85) * lumComp * nebScale;
        const starT = smoothstep(0.28, 0.95, v);
        lum = clamp01((lSky + lNeb) * (1 - starT) + (0.45 + 0.55 * v) * starT);
        hue = (ht + dirSign * (10 * (1 - smoothstep(0.04, 0.35, v)) - 6 * smoothstep(0.12, 0.45, v)) + 360) % 360;
        const sScale = 0.40 + 0.60 * k;
        sat = Math.min(0.95, Math.max(0, (0.55 + 0.30 * smoothstep(0.045, 0.14, v)) * (1 - 0.88 * smoothstep(0.30, 0.85, v)) * sScale * chromaFactor));
      }
      const [r, g, b] = hslToRgb01(hue, sat, lum);
      const o = i * 3;
      lut[o] = Math.round(r * 255);
      lut[o + 1] = Math.round(g * 255);
      lut[o + 2] = Math.round(b * 255);
    }
    return lut;
  }

  function loadLuma(theme) {
    if (lumaCache[theme]) return Promise.resolve(lumaCache[theme]);
    if (lumaPending[theme]) return lumaPending[theme];
    lumaPending[theme] = new Promise(resolve => {
      const img = new Image();
      img.decoding = 'async';
      img.onload = () => {
        try {
          const srcW = img.naturalWidth || img.width;
          const srcH = img.naturalHeight || img.height;
          if (!srcW || !srcH) { resolve(null); return; }
          const maxH = BG_MAX_HEIGHT[theme];
          const scale = srcH > maxH ? maxH / srcH : 1;
          const w = Math.max(1, Math.round(srcW * scale));
          const h = Math.max(1, Math.round(srcH * scale));
          const off = document.createElement('canvas');
          off.width = w;
          off.height = h;
          const oc = off.getContext('2d', { willReadFrequently: true }) || off.getContext('2d');
          if (!oc) { off.width = off.height = 0; resolve(null); return; }
          oc.drawImage(img, 0, 0, w, h);
          const pixels = oc.getImageData(0, 0, w, h).data;
          const luma = new Uint8Array(w * h);
          if (theme === 'light') {
            for (let i = 0, p = 0; i < luma.length; i++, p += 4) {
              luma[i] = Math.round(0.299 * pixels[p] + 0.587 * pixels[p + 1] + 0.114 * pixels[p + 2]);
            }
          } else {
            for (let i = 0, p = 0; i < luma.length; i++, p += 4) {
              luma[i] = Math.round(0.25 * pixels[p] + 0.30 * pixels[p + 1] + 0.45 * pixels[p + 2]);
            }
          }
          off.width = off.height = 0;
          lumaCache[theme] = { width: w, height: h, data: luma };
          resolve(lumaCache[theme]);
        } catch (_) {
          resolve(null);
        }
      };
      img.onerror = () => resolve(null);
      img.src = BG_URLS[theme];
    }).finally(() => { lumaPending[theme] = null; });
    return lumaPending[theme];
  }

  function clearPageBg(immediate) {
    pageBgCanvas.style.opacity = '0';
    if (immediate) pageBgCanvas.width = pageBgCanvas.height = 0;
  }

  function syncPageBg() {
    if (!state.tint || !bgCanvas.width || !bgCanvas.height || !isDesktop() || !pageBgCtx) {
      clearPageBg(true);
      return;
    }
    if (pageBgCanvas.width !== bgCanvas.width || pageBgCanvas.height !== bgCanvas.height) {
      pageBgCanvas.width = bgCanvas.width;
      pageBgCanvas.height = bgCanvas.height;
    }
    pageBgCtx.drawImage(bgCanvas, 0, 0);
    pageBgCanvas.style.opacity = '1';
  }

  function drawRecolored(entry, tintHex, light, strength) {
    if (!entry || !bgCtx) return;
    const { width: w, height: h, data: luma } = entry;
    const lut = buildRecolorLut(tintHex, light, strength);
    if (bgCanvas.width !== w || bgCanvas.height !== h || !bgImageData) {
      bgCanvas.width = w;
      bgCanvas.height = h;
      bgImageData = bgCtx.createImageData(w, h);
    }
    const out = bgImageData.data;
    if (IS_LITTLE_ENDIAN && out.buffer) {
      const lut32 = new Uint32Array(256);
      for (let i = 0; i < 256; i++) {
        const o = i * 3;
        lut32[i] = ((0xff << 24) | (lut[o + 2] << 16) | (lut[o + 1] << 8) | lut[o]) >>> 0;
      }
      const out32 = new Uint32Array(out.buffer, out.byteOffset, luma.length);
      for (let i = 0; i < luma.length; i++) out32[i] = lut32[luma[i]];
    } else {
      for (let i = 0, p = 0; i < luma.length; i++, p += 4) {
        const o = luma[i] * 3;
        out[p] = lut[o];
        out[p + 1] = lut[o + 1];
        out[p + 2] = lut[o + 2];
        out[p + 3] = 255;
      }
    }
    bgCtx.putImageData(bgImageData, 0, 0);
    bgCanvas.style.opacity = '1';
    syncPageBg();
  }

  function paintTintNow() {
    const tintHex = state.tint;
    if (!tintHex) return;
    const light = isLight();
    const theme = light ? 'light' : 'dark';
    const cached = lumaCache[theme];
    if (cached) {
      drawRecolored(cached, tintHex, light, state.strength);
      return;
    }
    const seq = ++tintSeq;
    loadLuma(theme).then(entry => {
      if (seq !== tintSeq || !entry || state.tint !== tintHex || isLight() !== light) return;
      drawRecolored(entry, tintHex, light, state.strength);
    });
  }

  const TEXT_VARS = [
    '--text', '--text2', '--text3', '--text-secondary', '--text-muted',
    '--accent', '--accent2', '--border-strong',
    '--ambience-strong', '--ambience-quote-bar', '--ambience-link-underline'
  ];

  function applyTextPalette() {
    const style = document.body.style;
    if (!state.tint) {
      document.body.removeAttribute('data-ambience-tint');
      for (const key of TEXT_VARS) style.removeProperty(key);
      return;
    }
    const palette = buildTextPalette(state.tint, isLight(), state.strength);
    document.body.setAttribute('data-ambience-tint', '1');
    style.setProperty('--text', palette.text);
    style.setProperty('--text2', palette.text2);
    style.setProperty('--text3', palette.text3);
    style.setProperty('--text-secondary', palette.text2);
    style.setProperty('--text-muted', palette.text3);
    style.setProperty('--accent', palette.accent);
    style.setProperty('--accent2', palette.accent2);
    style.setProperty('--border-strong', palette.borderStrong);
    style.setProperty('--ambience-strong', palette.strong);
    style.setProperty('--ambience-quote-bar', palette.quoteBar);
    style.setProperty('--ambience-link-underline', palette.linkUnderline);
  }

  // ── 设置项 ──
  function applyTint() {
    applyTextPalette();
    if (!state.tint) {
      tintSeq++;
      if (tintRaf) {
        cancelAnimationFrame(tintRaf);
        tintRaf = 0;
      }
      bgCanvas.style.opacity = '0';
      clearPageBg(false);
      clearTimeout(clearTimer);
      clearTimer = setTimeout(() => {
        if (!state.tint) {
          bgCanvas.width = bgCanvas.height = 0;
          bgImageData = null;
          clearPageBg(true);
        }
      }, 380);
      return;
    }
    clearTimeout(clearTimer);
    if (!tintRaf) {
      tintRaf = requestAnimationFrame(() => {
        tintRaf = 0;
        paintTintNow();
      });
    }
  }
  function render() {
    const custom = Boolean(state.tint) && !TINTS.some(([color]) => color === state.tint);
    for (const root of document.querySelectorAll('[data-chat-ambience]')) {
      for (const button of root.querySelectorAll('button[data-effect]')) {
        button.setAttribute('aria-pressed', String(button.dataset.effect === state.effect));
      }
      for (const button of root.querySelectorAll('button[data-tint]')) {
        button.setAttribute('aria-pressed', String(button.dataset.tint === state.tint));
      }
      root.querySelector('.chat-ambience-custom').classList.toggle('is-active', custom);
      root.querySelector('input[type="color"]').value = state.tint || '#ffffff';
      const range = root.querySelector('input[type="range"]');
      range.value = String(state.strength);
      range.disabled = !state.tint;
      root.querySelector('output').textContent = `${state.strength}%`;
    }
  }
  function update(patch, persist) {
    const previous = state.effect;
    state = normalize({ ...state, ...patch });
    if (persist) {
      try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (_) {}
    }
    applyTint();
    render();
    if (state.effect !== previous) rebuild();
  }

  // ── 与服务端同步：手动修改上报，AI 或其他设备的修改直接套用 ──
  let reportTimer = 0;
  let pending = {};
  function report(patch) {
    Object.assign(pending, patch);
    clearTimeout(reportTimer);
    reportTimer = setTimeout(() => {
      window.AionAmbienceSync.save(pending);
      pending = {};
    }, 300);
  }
  function applyRemote(remote) {
    const patch = {};
    if (typeof remote.effect === 'string' && !('effect' in pending)) patch.effect = remote.effect;
    if (typeof remote.tint === 'string' && !('tint' in pending)) patch.tint = remote.tint;
    update(patch, true);
    if (remote.burst) burst(remote.burst);
  }
  function change(patch) {
    update(patch, true);
    report(patch);
  }

  function buildControl(root) {
    const icons = (items, attribute) => items.map(([id, icon, label]) =>
      `<button type="button" class="chat-ambience-effect" ${attribute}="${id}" title="${label}" aria-label="${label}">${icon}</button>`
    ).join('');
    root.classList.add('chat-ambience-control');
    root.setAttribute('role', 'group');
    root.setAttribute('aria-label', '聊天氛围');
    root.innerHTML = `
      <div class="chat-ambience-row">
        <span>氛围</span>
        <div class="chat-ambience-effects">${icons(EFFECTS, 'data-effect')}</div>
      </div>
      <div class="chat-ambience-row">
        <span>特效</span>
        <div class="chat-ambience-effects">${icons(BURST_BUTTONS, 'data-burst')}</div>
      </div>
      <div class="chat-ambience-row">
        <span>颜色</span>
        <div class="chat-ambience-swatches">
          <button type="button" class="chat-ambience-swatch" data-tint="" title="不叠加" aria-label="不叠加颜色"></button>
          ${TINTS.map(([color, label]) => `<button type="button" class="chat-ambience-swatch" data-tint="${color}" style="background:${color}" title="${label}" aria-label="${label}"></button>`).join('')}
          <span class="chat-ambience-swatch chat-ambience-custom" title="自定义颜色"><input type="color" aria-label="自定义颜色"></span>
        </div>
      </div>
      <div class="chat-ambience-row">
        <span>浓淡</span>
        <input type="range" class="chat-ambience-strength" min="${MIN_STRENGTH}" max="${MAX_STRENGTH}" step="1" aria-label="颜色浓淡">
        <output></output>
      </div>`;
    root.addEventListener('click', event => {
      const effect = event.target.closest('button[data-effect]');
      const swatch = event.target.closest('button[data-tint]');
      const once = event.target.closest('button[data-burst]');
      // 再点一次已选中的图标即关闭效果
      if (effect) change({ effect: effect.dataset.effect === state.effect ? 'none' : effect.dataset.effect });
      else if (swatch) change({ tint: swatch.dataset.tint });
      else if (once) burst(once.dataset.burst);
    });
    root.addEventListener('input', event => {
      if (event.target.type === 'color') change({ tint: event.target.value });
      else if (event.target.type === 'range') update({ strength: event.target.value }, true);
    });
  }

  document.querySelectorAll('[data-chat-ambience]').forEach(buildControl);
  applyTint();
  render();

  new ResizeObserver(measure).observe(layer);
  new IntersectionObserver(entries => {
    visible = entries[entries.length - 1].isIntersecting;
    sync();
  }).observe(layer);
  document.addEventListener('visibilitychange', sync);
  if (subPage) new MutationObserver(sync).observe(subPage, { attributes: true, attributeFilter: ['class'] });
  // 换主题后雨雪颜色与底图调色跟着换
  let lastLight = isLight();
  new MutationObserver(() => {
    const nextLight = isLight();
    if (nextLight === lastLight) return;
    lastLight = nextLight;
    if (sim) rebuild();
    applyTint();
  }).observe(document.body, { attributes: true, attributeFilter: ['data-theme'] });
  if (desktopQuery) {
    if (desktopQuery.addEventListener) desktopQuery.addEventListener('change', syncPageBg);
    else if (desktopQuery.addListener) desktopQuery.addListener(syncPageBg);
  }
  window.addEventListener('storage', event => {
    if (event.key === KEY || event.key === null) update(normalize(event.newValue), false);
  });

  window.AionChatAmbience = {
    get: () => ({ effect: state.effect, tint: state.tint, strength: state.strength }),
    set: change,
    burst,
    applyRemote,
    buildLut: buildRecolorLut,
    buildTextPalette
  };
})();

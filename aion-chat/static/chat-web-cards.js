/* Store the artwork in its attachment. SVG drawings run in the chat after a
   whitelist pass; legacy HTML pages only play in the isolated player. */
(() => {
  const sources = new Map();
  const prepared = new Map();
  const drawn = new WeakSet();
  const sessions = new WeakMap();
  const pending = new Set();
  let nativeSession;
  const escape = value => String(value).replace(/[&<>"']/g, c =>
    ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));

  function render(attachments) {
    const item = attachments?.find(a => ['web_card','web_card_incomplete','web_card_pending'].includes(a?.type));
    if (!item) return '';
    if (item.type === 'web_card_pending') return '<small class="web-card-note">正在画小动画…</small>';
    if (item.type === 'web_card_incomplete') {
      const reason = {too_large:'超过了 ' + (Number(item.limit) || 16000).toLocaleString('en-US') + ' 字符上限，可以让我做个简单点的。',
        invalid_html:'没有收到可用的 HTML，可以让我重写。', invalid:'没有收到能显示的动画，可以让我重画。'};
      return '<small class="web-card-note">' + (reason[item.reason] || '这次没画完，可以让我重画。') + '</small>';
    }
    if (!/^[a-f0-9]{32}$/.test(item.id) || typeof item.html !== 'string') return '';
    const known = sources.get(item.id);
    if (!known || known.revision <= item.revision) sources.set(item.id, item);
    const current = sources.get(item.id), title = escape(current.title);
    const rename = '<button type="button" class="web-card-title" data-web-action="rename" title="改标题" aria-label="改标题：' +
      title + '"><strong>' + title + '</strong> ✎</button>';
    if (current.format === 'svg') {
      const art = prepare(current);
      return '<chat-web-card class="chat-web-card svg-card" data-web-card="' + item.id + '"><header>' + rename +
        '<button type="button" data-web-action="replay">重来</button><button type="button" data-web-action="edit">改动画</button></header>' +
        '<div class="web-card-art" style="aspect-ratio:' + art.ratio + '"></div><small role="status" aria-live="polite">' +
        (art.root ? '' : '这段动画没法显示，可以点「改动画」重画。') + '</small></chat-web-card>';
    }
    return '<chat-web-card class="chat-web-card" data-web-card="' + item.id + '"><header>' + rename +
      '<button type="button" data-web-action="play" aria-expanded="false">打开</button></header><div class="web-card-stage" hidden></div>' +
      '<small role="status" aria-live="polite"></small></chat-web-card>';
  }

  // ---- SVG drawings ----
  // Only drawing and animation elements survive; nothing may run script,
  // load a resource, or style the chat page.
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const TAGS = new Map(('svg g defs symbol use title desc style path rect circle ellipse line polyline polygon text tspan ' +
    'textPath linearGradient radialGradient stop pattern clipPath mask marker filter feBlend feColorMatrix ' +
    'feComponentTransfer feComposite feDropShadow feFlood feFuncA feFuncB feFuncG feFuncR feGaussianBlur feMerge ' +
    'feMergeNode feMorphology feOffset feTurbulence feDisplacementMap animate animateMotion animateTransform set mpath')
    .split(' ').map(name => [name.toLowerCase(), name]));
  const ANIMATIONS = new Set(['animate', 'animateMotion', 'animateTransform', 'set']);
  const EXTERNAL = /url\((?!\s*['"]?\s*#)|image-set\(|src\(|javascript:|\\/i;
  const SCOPE_ESCAPE = /:host|slotted|@font-face|@import/i;
  const ART_STYLE = ':host>svg{display:block!important;width:100%!important;height:100%!important;position:static!important}' +
    ':host(.paused) *{animation-play-state:paused!important}';

  function cleanCss(css) {
    try {
      const sheet = new CSSStyleSheet();
      sheet.replaceSync(css);
      return [...sheet.cssRules].map(rule => rule.cssText)
        .filter(text => !EXTERNAL.test(text) && !SCOPE_ESCAPE.test(text)).join('\n');
    } catch { return ''; }
  }

  function copyAttributes(from, to) {
    for (const {name, value} of from.attributes) {
      const lower = name.toLowerCase();
      if (lower.startsWith('on') || EXTERNAL.test(value)) continue;
      if (lower === 'href' || lower === 'xlink:href') {
        if (value.trim().startsWith('#')) to.setAttribute('href', value.trim());
        continue;
      }
      if (lower.includes(':') || lower.startsWith('xmlns')) continue;
      try { to.setAttribute(name, value); } catch {}
    }
  }

  function cleanChildren(from, to) {
    for (const node of from.childNodes) {
      if (node.nodeType === Node.TEXT_NODE || node.nodeType === Node.CDATA_SECTION_NODE) {
        to.append(node.data);
        continue;
      }
      const name = node.nodeType === Node.ELEMENT_NODE && TAGS.get(node.localName.toLowerCase());
      if (!name) continue;
      if (ANIMATIONS.has(name) && /^on|href|^style$/i.test(node.getAttribute('attributeName') || '')) continue;
      const el = document.createElementNS(SVG_NS, name);
      copyAttributes(node, el);
      if (name === 'style') el.textContent = cleanCss(node.textContent);
      else cleanChildren(node, el);
      to.append(el);
    }
  }

  function parseSvg(source) {
    const xml = new DOMParser().parseFromString(source, 'image/svg+xml');
    if (!xml.getElementsByTagName('parsererror').length && xml.documentElement?.localName === 'svg') return xml.documentElement;
    // HTML parsing tolerates stray "&" and closes tags cut off at the end.
    return new DOMParser().parseFromString(source, 'text/html').querySelector('svg');
  }

  function prepare(item) {
    const known = prepared.get(item.id);
    if (known?.revision === item.revision) return known;
    let root = null, w = 4, h = 3;
    const source = parseSvg(item.html);
    if (source) {
      root = document.createElementNS(SVG_NS, 'svg');
      copyAttributes(source, root);
      for (const name of ['width', 'height', 'x', 'y', 'style']) root.removeAttribute(name);
      const box = (source.getAttribute('viewBox') || '').trim().split(/[\s,]+/).map(Number);
      const size = name => /^\s*[\d.]+\s*(px)?\s*$/.test(source.getAttribute(name) || '') ? parseFloat(source.getAttribute(name)) : 0;
      [w, h] = box.length === 4 && box.every(Number.isFinite) && box[2] > 0 && box[3] > 0 ? box.slice(2) : [size('width'), size('height')];
      if (!(w > 0 && h > 0)) [w, h] = [320, 320];
      if (box.length !== 4) root.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
      root.setAttribute('role', 'img');
      root.setAttribute('aria-label', item.title);
      cleanChildren(source, root);
      if (!root.querySelector(':not(title):not(desc):not(style)')) root = null;
    }
    const ratio = Math.min(2.5, Math.max(0.5, w / h)).toFixed(4);
    const result = {revision: item.revision, root, ratio};
    prepared.set(item.id, result);
    return result;
  }

  function draw(card) {
    const model = sources.get(card.dataset.webCard);
    const host = card.querySelector('.web-card-art');
    const art = model && host && prepare(model);
    if (!art?.root) return;
    const shadow = host.shadowRoot || host.attachShadow({mode: 'open'});
    const style = document.createElement('style');
    style.textContent = ART_STYLE;
    const svg = art.root.cloneNode(true);
    shadow.replaceChildren(style, svg);
    host.style.aspectRatio = art.ratio;
    if (host.classList.contains('paused')) svg.pauseAnimations();
    drawn.add(card);
  }

  // Off-screen drawings pause, so a long chat never animates in the background.
  function setPlaying(card, playing) {
    const host = card.querySelector('.web-card-art');
    const svg = host?.shadowRoot?.querySelector('svg');
    if (!svg) return;
    host.classList.toggle('paused', !playing);
    if (playing) svg.unpauseAnimations(); else svg.pauseAnimations();
  }

  const artObserver = new IntersectionObserver(entries => {
    for (const {target, isIntersecting} of entries) {
      if (isIntersecting && !drawn.has(target)) draw(target);
      setPlaying(target, isIntersecting);
    }
  }, {rootMargin: '150px 0px'});

  // ---- Legacy HTML pages ----

  // The artwork always runs on a virtual screen 360px wide, so vh, canvas
  // sizes and media queries match on every phone, then is scaled into the
  // card. The browser scales its iframe directly; the app's native player
  // loads a small shell that does the same.
  const VIEW_WIDTH = 360;
  const PLAYER_LIMIT = 32000;  // The app's native player rejects longer documents.
  const policy = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; media-src data: blob:; font-src data:; form-action 'none'; base-uri 'none'";
  const head = '<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="' + policy +
    '"><meta name="viewport" content="width=device-width, initial-scale=1">';
  // Inside a script, "</" and "<!--" would end or confuse the element.
  const literal = value => JSON.stringify(value).replace(/<(\/|!--)/g, '<\\$1');
  // Embedded helpers count toward the player limit; drop comment lines and indentation.
  const compact = fn => String(fn).replace(/^\s*\/\/.*\n/gm, '').replace(/\n\s+/g, '\n');

  function artworkDocument(source) {
    return head + '<style>html{color-scheme:light dark}:where(body){margin:0;font:16px system-ui}*{box-sizing:border-box}</style>' +
      '<script>(' + compact(artworkAgent) + ')()</script>' + source;
  }

  function shellDocument(source, key) {
    // key contains a generated card id, revision and random digits only.
    return head + '<style>html,body{margin:0;height:100%;overflow:hidden}iframe{position:absolute;left:0;top:0;border:0;' +
      'transform-origin:0 0;opacity:0}</style><iframe sandbox="allow-scripts" title="作品"></iframe><script>' +
      'const frame=document.querySelector("iframe");(' + compact(fitPlayer) + ')(frame,document.documentElement,' + VIEW_WIDTH +
      ',e=>console.error("web-card:' + key + ':"+e));frame.srcdoc=' + literal(artworkDocument(source)) + '</script>';
  }

  // Runs in the artwork. Reports errors and how far visible content reaches,
  // ignoring decorations that extend past the screen or are clipped by a box.
  function artworkAgent() {
    const send = data => parent.postMessage(data, '*');
    const report = m => send({error: String(m).slice(0, 350)});
    addEventListener('error', e => { if (e.message) report(e.message + ' (line ' + e.lineno + ')'); });
    addEventListener('unhandledrejection', e => report(e.reason?.message || e.reason));
    const media = new Set(['img','canvas','svg','video']);
    const solid = new Set([...media, 'button','input','select','textarea','progress','meter']);
    let timer = 0, last = 0;
    function measure() {
      timer = 0;
      last = performance.now();
      if (!document.body) return;
      const W = innerWidth, H = innerHeight, boxes = new Map();
      const open = [-Infinity, -Infinity, Infinity, Infinity, false];
      const box = el => {
        if (!el || el === document.body || el === document.documentElement) return open;
        if (boxes.has(el)) return boxes.get(el);
        const css = getComputedStyle(el), [l, t, r, b, loose] = box(el.parentElement);
        const own = el.getBoundingClientRect(), x = css.overflowX !== 'visible', y = css.overflowY !== 'visible';
        const result = [x ? Math.max(l, own.left) : l, y ? Math.max(t, own.top) : t,
          x ? Math.min(r, own.right) : r, y ? Math.min(b, own.bottom) : b, loose || /absolute|fixed/.test(css.position)];
        boxes.set(el, result);
        return result;
      };
      let left = 0, right = 0, bottom = 0;
      for (const el of document.body.getElementsByTagName('*')) {
        if (el.ownerSVGElement || !(solid.has(el.localName) ||
            [...el.childNodes].some(n => n.nodeType === 3 && n.data.trim()))) continue;
        const r = el.getBoundingClientRect();
        if (!r.width || !r.height) continue;
        const css = getComputedStyle(el);
        if (css.visibility === 'hidden' || css.opacity === '0') continue;
        const [cl, ct, cr, cb, inherited] = box(el.parentElement);
        const l = Math.max(r.left, cl), t = Math.max(r.top, ct), rr = Math.min(r.right, cr), b = Math.min(r.bottom, cb);
        if (rr <= l || b <= t) continue;
        // Positioned pictures and pass-through particles are decoration.
        if ((inherited || /absolute|fixed/.test(css.position)) && (media.has(el.localName) ||
            css.pointerEvents === 'none' || l >= W || rr <= 0 || t >= H || b <= 0)) continue;
        left = Math.min(left, l); right = Math.max(right, rr); bottom = Math.max(bottom, b);
      }
      const bg = [document.documentElement, document.body].map(el => getComputedStyle(el))
        .find(s => s.backgroundImage !== 'none' || !/^(transparent|rgba\(.*, 0\))$/.test(s.backgroundColor));
      send({fit: 1, w: W, h: H, left, right, bottom, bg: bg ? [bg.backgroundColor, bg.backgroundImage] : null});
    }
    // Timers, not animation frames: a frame that isn't shown yet gets no frames.
    const run = () => { try { measure(); } catch {} };
    function now() { clearTimeout(timer); timer = setTimeout(run); }
    function later() { if (!timer) timer = setTimeout(run, Math.max(0, last + 300 - performance.now())); }
    addEventListener('resize', now);
    addEventListener('load', now);
    addEventListener('message', e => {
      if (e.source !== parent || !e.data?.unlock) return;
      document.documentElement.style.setProperty('overflow-y', 'auto', 'important');
      document.body.style.setProperty('overflow-y', 'visible', 'important');
    });
    addEventListener('DOMContentLoaded', () => {
      now();
      new MutationObserver(later).observe(document.body, {subtree: true, childList: true, characterData: true,
        attributes: true, attributeFilter: ['class', 'style', 'hidden', 'open']});
      document.addEventListener('transitionend', later, true);
      document.addEventListener('animationend', later, true);
    });
  }

  // Runs in the chat page or the native shell; host is the visible area.
  // Starts from the card's aspect ratio; when content still overflows, makes
  // the virtual screen taller (vh layouts grow with it, so the next step
  // follows the measured rate) and scales it down to fit.
  function fitPlayer(frame, host, width, tell) {
    const win = frame.ownerDocument.defaultView;
    let W = 0, H = 0, vw = 0, vh = 0, base = 0, probe = null, tries = 0, stuck = false, pending = 0;
    function size(w, h) {
      W = Math.ceil(w); H = Math.ceil(h);
      frame.style.width = W + 'px';
      frame.style.height = H + 'px';
      const s = Math.min(vw / W, vh / H);
      frame.style.transform = 'translate(' + (vw - W * s) / 2 + 'px,' + (vh - H * s) / 2 + 'px) scale(' + s + ')';
    }
    function show() { frame.style.opacity = '1'; }
    function reset() {
      vw = host.clientWidth; vh = host.clientHeight;
      // The native player starts at 1px.
      if (vw < 40 || vh < 40) return;
      base = width * vh / vw;
      probe = null; tries = 0; stuck = false;
      size(width, base);
    }
    function receive(e) {
      const d = e.data;
      if (e.source !== frame.contentWindow || !d) return;
      if (d.error) { tell(String(d.error).slice(0, 350)); return; }
      if (!d.fit || d.w !== W || d.h !== H) return;
      // A url() here could leave the sandbox's network policy; gradients only.
      if (d.bg) host.style.background = /url\(/i.test(d.bg[1]) ? d.bg[0] : d.bg[1] + ' ' + d.bg[0];
      const wide = Math.max(0, d.right - W, -d.left), tall = Math.max(0, d.bottom - H);
      if ((wide <= 1 && tall <= 1) || stuck) { probe = null; tries = 0; show(); return; }
      if (wide > 1 && W < width * 1.6 && tries++ < 5) {
        const w = Math.min(width * 1.6, W + 2 * wide + 4);
        base = w * vh / vw;
        probe = null;
        size(w, Math.max(H, base));
        return;
      }
      // A taller screen that doesn't reduce the overflow (fixed 100vh plus a
      // header) is undone. Past 1.45x the card's height text gets too small.
      // Either way the artwork scrolls for the rest.
      const futile = probe && tall >= probe.tall - 1;
      if (++tries > 5 || H >= base * 1.45 || futile) {
        stuck = true;
        frame.contentWindow.postMessage({unlock: 1}, '*');
        if (futile) size(W, base); else show();
        return;
      }
      const rate = probe ? (probe.tall - tall) / (H - probe.h) : 1;
      probe = {h: H, tall};
      size(W, Math.min(base * 1.45, H + (tall + 8) / Math.max(rate, .15)));
    }
    win.addEventListener('message', receive);
    const watcher = new win.ResizeObserver(() => {
      if (host.clientWidth === vw && host.clientHeight === vh) return;
      clearTimeout(pending);
      pending = setTimeout(reset, 60);
    });
    watcher.observe(host);
    reset();
    const fallback = setTimeout(show, 1500);
    return () => {
      win.removeEventListener('message', receive);
      watcher.disconnect();
      clearTimeout(pending);
      clearTimeout(fallback);
    };
  }

  function status(card, text) { card.querySelector('[role=status]').textContent = text; }
  function close(card) {
    const session = sessions.get(card);
    if (nativeSession?.card === card) {
      window.AionWebCard?.close(nativeSession.key);
      nativeSession = null;
    }
    sessions.delete(card);
    session?.dispose?.();
    const stage = card.querySelector('.web-card-stage');
    stage.replaceChildren();
    stage.style.removeProperty('background');
    stage.hidden = true;
    const toggle = card.querySelector('[data-web-action=play]');
    toggle.textContent = '打开';
    toggle.setAttribute('aria-expanded', 'false');
    return session;
  }

  // A native WebView is a sibling of the chat WebView. Track the inline slot
  // through nested same-origin chat tabs, including clipped or covered slots.
  function place(session) {
    if (nativeSession !== session) return;
    const {card, key} = session;
    if (!card.isConnected) { close(card); return; }
    let win = window;
    const rect = card.querySelector('.web-card-stage').getBoundingClientRect();
    let x = rect.x, y = rect.y;
    const stage = card.querySelector('.web-card-stage');
    let left = x, top = y, right = x + rect.width, bottom = y + rect.height;
    let expected = stage, shown = !document.hidden;
    while (true) {
      left = Math.max(0, left); top = Math.max(0, top);
      right = Math.min(win.innerWidth, right); bottom = Math.min(win.innerHeight, bottom);
      for (let el = expected.parentElement; el; el = el.parentElement) {
        const css = win.getComputedStyle(el), bounds = el.getBoundingClientRect();
        if (css.overflowX !== 'visible') { left = Math.max(left, bounds.left); right = Math.min(right, bounds.right); }
        if (css.overflowY !== 'visible') { top = Math.max(top, bounds.top); bottom = Math.min(bottom, bounds.bottom); }
      }
      if (expected === stage) {
        if (session.header) top = Math.max(top, session.header.getBoundingClientRect().bottom);
        for (const bar of session.bottomBars) {
          const bounds = bar.getBoundingClientRect();
          if (bounds.height && bounds.left < right && bounds.right > left) bottom = Math.min(bottom,bounds.top);
        }
      }
      // At a rounded card corner the hit belongs to the surrounding message.
      // Probe inside the painted card. Keep obscuring dialogs above the player.
      const insetX = Math.min(16, (right-left)/2);
      const insetY = Math.min(4, (bottom-top)/2);
      shown = shown && right - left > 1 && bottom - top > 1 &&
        [[left+insetX,top+insetY],[right-insetX,top+insetY],[left+insetX,bottom-insetY],[right-insetX,bottom-insetY],[(left+right)/2,(top+bottom)/2]]
          .every(([px,py]) => expected.contains(win.document.elementFromPoint(px, py)));
      if (win === win.parent || !win.frameElement) break;
      const frame = win.frameElement, offset = frame.getBoundingClientRect();
      left += offset.x; right += offset.x; top += offset.y; bottom += offset.y;
      x += offset.x; y += offset.y; win = win.parent;
      expected = frame;
    }
    const coords = [left, top, shown ? right-left : 0, bottom-top, x-left, y-top,
                    rect.width, rect.height, win.innerWidth].map(Math.round);
    const next = coords.join(',');
    if (next !== session.coords) {
      window.AionWebCard.place(key, ...coords);
      session.coords = next;
    }
    win.requestAnimationFrame(() => place(session));
  }

  function open(card) {
    const model = sources.get(card.dataset.webCard);
    if (!model) return;
    if ((window.AionExternal || window.AionTtsAudio) && !window.AionWebCard) {
      status(card, '这个版本缺少网页播放器。请安装小家 App 1.32 或更新版本，也可以用手机浏览器打开这段聊天。');
      return;
    }
    if (nativeSession) close(nativeSession.card);
    close(card);
    status(card, '');
    const stage = card.querySelector('.web-card-stage');
    const key = model.id + '-' + model.revision + '-' + Math.random().toString(36).slice(2);
    const session = {card, key, revision:model.revision};
    sessions.set(card, session);
    stage.hidden = false;
    const toggle = card.querySelector('[data-web-action=play]');
    toggle.textContent = '收起';
    toggle.setAttribute('aria-expanded', 'true');
    if (window.AionWebCard) {
      const doc = shellDocument(model.html, key);
      if (doc.length > PLAYER_LIMIT) {
        close(card);
        status(card, '这张网页太长，App 播放器放不下，可以用手机浏览器打开这段聊天。');
        return;
      }
      nativeSession = session;
      const chat = card.closest('#messages')?.parentElement;
      session.header = chat?.querySelector('.chat-header');
      session.bottomBars = chat?.querySelectorAll('.composer,.input-area,.voice-mode-row') || [];
      window.AionWebCard.open(key, doc);
      place(session);
      return;
    }
    const frame = document.createElement('iframe');
    frame.setAttribute('sandbox', 'allow-scripts');
    frame.referrerPolicy = 'no-referrer';
    frame.title = model.title;
    stage.append(frame);
    session.frame = frame;
    session.dispose = fitPlayer(frame, stage, VIEW_WIDTH, error => report(session, error));
    frame.srcdoc = artworkDocument(model.html);
  }

  customElements.define('chat-web-card', class extends HTMLElement {
    connectedCallback() { if (this.classList.contains('svg-card')) artObserver.observe(this); }
    disconnectedCallback() {
      artObserver.unobserve(this);
      if (sessions.has(this)) close(this);
    }
  });

  async function change(card, body) {
    const item = sources.get(card.dataset.webCard);
    if (!item || pending.has(item.id)) return;
    const row = card.closest('[data-msg-id]');
    if (!row) return;
    const scope = row.classList.contains('msg-row') ? 'private' : 'chatroom';
    pending.add(item.id);
    status(card, body.title ? '正在保存标题…' : '作者正在修改动画…');
    try {
      const response = await fetch('/api/web-cards/' + scope + '/' + encodeURIComponent(row.dataset.msgId) + '/' + item.id, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({revision:item.revision, ...body})
      });
      const data = await response.json();
      if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : '这次修改没有完成');
      sources.set(item.id, data);
      const current = document.querySelector('[data-web-card="' + item.id + '"]');
      if (!current) return;
      current.querySelector('strong').textContent = data.title;
      current.querySelector('[data-web-action=rename]').setAttribute('aria-label', '改标题：' + data.title);
      if (body.instruction) draw(current);
      status(current, body.title ? '标题已保存' : '动画已更新');
    } catch (err) { status(document.querySelector('[data-web-card="' + item.id + '"]') || card, err.message || '这次修改没有完成'); }
    finally { pending.delete(item.id); }
  }

  function report(session, error) {
    if (session && error) status(session.card, '这张旧网页运行出错了。');
  }
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-web-action]');
    const card = button?.closest('[data-web-card]');
    if (!card) return;
    event.stopPropagation();
    const action = button.dataset.webAction;
    if (action === 'play') {
      if (sessions.has(card)) close(card); else open(card);
    } else if (action === 'replay') {
      draw(card);
    } else if (action === 'rename') {
      const name = sources.get(card.dataset.webCard).title;
      const title = window.prompt('卡片标题', name)?.trim();
      if (title && title !== name) change(card, {title: title.slice(0, 60)});
    } else {
      const instruction = window.prompt('想让作者怎样修改这个动画？')?.trim();
      if (instruction) change(card, {instruction: instruction.slice(0, 600)});
    }
  });
  window.addEventListener('pagehide', () => { if (nativeSession) close(nativeSession.card); });
  document.addEventListener('visibilitychange', () => { if (document.hidden && nativeSession) close(nativeSession.card); });
  window.ChatWebCards = {render, nativeError(key, error) {
    if (nativeSession?.key === key) report(nativeSession, error);
  }};
})();

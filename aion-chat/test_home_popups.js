'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

function page({ home = true, embedded = false, storage = new Map(), gifts = [] } = {}) {
  const elements = new Map();
  const listeners = {};
  const requests = [];
  const sockets = [];
  const timers = [];
  function listen(type, cb) {
    const previous = listeners[type];
    listeners[type] = event => { previous?.(event); cb(event); };
  }
  function element() {
    const classes = new Set();
    return {
      style: { setProperty() {} },
      classList: { add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x) },
      appendChild(child) { if (child.id) elements.set(child.id, child); },
      remove() { elements.delete(this.id); },
      set innerHTML(html) {
        this.html = html;
        for (const match of html.matchAll(/id="([^"]+)"/g)) {
          const child = element(); child.id = match[1]; elements.set(child.id, child);
        }
      },
      get innerHTML() { return this.html || ''; },
    };
  }
  const context = {
    console, URL, Set, Map, Promise,
    location: { pathname: home ? '/' : '/chat', protocol: 'http:', host: 'localhost' },
    localStorage: { getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v) },
    document: {
      hidden: false, readyState: 'complete',
      body: Object.assign(element(), { dataset: { homePopups: home ? 'enabled' : '' } }),
      getElementById: id => elements.get(id), createElement: element,
      addEventListener: listen,
    },
    setTimeout: cb => { timers.push(cb); return timers.length; }, clearTimeout() {},
    requestAnimationFrame: cb => cb(),
    fetch: async (url, options) => {
      requests.push({ url, options });
      return { ok: true, json: async () => url === '/api/gift/pending'
        ? { ok: true, gifts } : { ai_name: '星野', connor_name: '月白' } };
    },
    WebSocket: class { constructor() { sockets.push(this); } close() {} },
    Audio: class { play() { return Promise.resolve(); } },
    addEventListener: listen,
  };
  context.window = context;
  context.parent = embedded ? {} : context;
  context.frameElement = embedded ? { dataset: { aionSubPageVisible: '0' } } : null;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, 'static/home-popups.js'), 'utf8'), context);
  return { context, elements, requests, sockets, listeners, timers, storage };
}
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const alarm = { type: 'schedule_alarm', data: { id: 'alarm-1', trigger_at: '2026-09-23 12:00', content: '喝水' } };

test('hidden Home waits for the existing iframe lifecycle, then shows a gift once', async () => {
  const p = page({ embedded: true, gifts: [{ id: 'g1', sender: 'connor', message: '送给你', image_path: 'gift.png' }] });
  await settle();
  assert.equal(p.elements.has('giftOverlay'), false);
  p.context.onAionSubPageVisibilityChanged(true);
  await settle();
  assert.ok(p.elements.get('giftOverlay').innerHTML.includes('月白'));
  p.context._openGiftBox();
  assert.equal(p.requests.filter(r => r.url === '/api/gift/g1/receive').length, 1);
  const next = page({ storage: p.storage, gifts: [{ id: 'g1' }] });
  await settle();
  assert.equal(next.elements.has('giftOverlay'), false);
});

test('other pages save alarms without rendering; Home restores, deduplicates and dismisses them', async () => {
  const other = page({ home: false });
  other.context.AionHomePopups.handleEvent(alarm);
  other.context.AionHomePopups.handleEvent(alarm);
  assert.equal(other.elements.has('alarmOverlay'), false);
  assert.equal(other.sockets.length, 0);
  const home = page({ storage: other.storage });
  await settle();
  assert.equal(home.elements.get('alarmContent').textContent, '喝水');
  home.context.dismissAlarm();
  home.context.AionHomePopups.handleEvent(alarm);
  assert.equal(home.elements.get('alarmOverlay').classList.contains('show'), false);
  home.context.AionHomePopups.handleEvent({ ...alarm, data: { ...alarm.data, trigger_at: '2026-09-24 12:00', content: '明天喝水' } });
  assert.equal(home.elements.get('alarmContent').textContent, '明天喝水');
});

test('Home websocket receives alarms and gifts and reconnect checks missed gifts', async () => {
  const p = page(); await settle();
  assert.equal(p.sockets.length, 1);
  p.sockets[0].onmessage({ data: JSON.stringify(alarm) });
  assert.equal(p.elements.get('alarmContent').textContent, '喝水');
  p.sockets[0].onmessage({ data: JSON.stringify({ type: 'gift_pending', data: { id: 'g2', message: '礼物' } }) });
  assert.ok(p.elements.has('giftOverlay'));
  const before = p.requests.filter(r => r.url === '/api/gift/pending').length;
  p.sockets[0].onopen(); await settle();
  assert.ok(p.requests.filter(r => r.url === '/api/gift/pending').length > before);
});

test('acknowledgement in another page clears visible alarm and gift without reopening', async () => {
  const p = page({ gifts: [{ id: 'g3' }] }); await settle();
  p.context.AionHomePopups.handleEvent(alarm);
  const other = page({ storage: p.storage, gifts: [{ id: 'g3' }] }); await settle();
  other.context.dismissAlarm();
  other.context._openGiftBox();
  for (const key of p.storage.keys()) p.listeners.storage({ key });
  assert.equal(p.elements.has('giftOverlay'), false);
  assert.equal(p.elements.get('alarmOverlay').classList.contains('show'), false);
});

test('leaving Home while receiving the first gift does not strand the next gift', async () => {
  const p = page({ gifts: [{ id: 'first' }, { id: 'second', message: '第二份礼物' }] });
  await settle();
  await p.context._receiveGift('first');
  p.context.onAionSubPageVisibilityChanged(false);
  while (p.timers.length) p.timers.shift()();
  assert.equal(p.elements.has('giftOverlay'), false);
  p.context.onAionSubPageVisibilityChanged(true);
  assert.match(p.elements.get('giftOverlay').innerHTML, /第二份礼物/);
});

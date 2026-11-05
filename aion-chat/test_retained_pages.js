'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = file => fs.readFileSync(path.join(__dirname, 'static', file), 'utf8');
const block = (s, start, end) => s.slice(s.indexOf(start), s.indexOf(end, s.indexOf(start)));

test('floating chat follows the visible subpage and returning to private chat', () => {
  const pages = [];
  const element = { classList: { remove() {}, toggle() {}, contains: () => true } };
  const ctx = vm.createContext({
    URL, location: { origin: 'http://home' }, $: () => element,
    window: { AionFloatingChat: { setVisiblePage: page => pages.push(page) }, AionSubPageNavigation: { hide() {} } },
    _subPageNames: {}, resetSubPageChrome() {}, applySubPageThemeChrome() {},
    activeSubPageFrame: null, transientSubPageFrame: {}, currentSubPage: '/theater',
    notifySubPageLifecycle() {}, syncHealthRingPageVisibility() {}, applyAionTheme() {},
    localStorage: { getItem: () => 'dark' }, currentConvId: null,
  });
  const source = read('chat.js');
  vm.runInContext(block(source, 'function syncSubPageMode(', 'const persistentSubPageFrames =')
    + block(source, 'function closeSubPage(', '// 导航到 Home'), ctx);
  ctx.syncSubPageMode('/reading?book=1');
  ctx.syncSubPageMode('/theater');
  ctx.syncSubPageMode('/chatroom?room=1');
  ctx.closeSubPage();
  assert.deepEqual(pages, ['/reading', '/theater', '/chatroom', '/chat']);
  delete ctx.window.AionFloatingChat;
  assert.doesNotThrow(() => ctx.syncSubPageMode('/reading'), 'older apps and browsers still navigate');
});

function subPageLifecycle(userAgent, hidden = false) {
  const events = {}, foreground = [];
  const frame = { dataset: {}, contentWindow: { onAionAppForegroundChanged: value => foreground.push(value) } };
  const ctx = vm.createContext({
    navigator: { userAgent }, document: { hidden, addEventListener: (name, fn) => events[name] = fn },
    window: {}, activeSubPageFrame: frame, _sendTTSState() {},
  });
  vm.runInContext(block(read('chat.js'), 'let _aionAppForeground =', 'window.onAionPhoneCameraCaptureState ='), ctx);
  ctx.notifySubPageLifecycle(frame, true);
  return { ctx, events, foreground };
}

test('desktop subpages start in foreground and follow browser visibility', () => {
  const { ctx, events, foreground } = subPageLifecycle('Chrome');
  assert.deepEqual(foreground, [true], 'the visible billiards iframe must be allowed to draw');
  ctx.document.hidden = true; events.visibilitychange();
  ctx.document.hidden = false; events.visibilitychange();
  assert.deepEqual(foreground, [true, false, true]);
  assert.deepEqual(subPageLifecycle('Chrome', true).foreground, [false]);
});

test('Android subpages still wait for and obey native foreground callbacks', () => {
  const { ctx, events, foreground } = subPageLifecycle('Chrome AionChatApp/1.0');
  assert.deepEqual(foreground, [false]);
  ctx.window.onAionAppForegroundChanged(true);
  ctx.window.onAionAppForegroundChanged(false);
  ctx.document.hidden = false; events.visibilitychange?.();
  assert.deepEqual(foreground, [false, true, false]);
});

test('hidden retained pages stop sockets and reconnect timers; showing them reconciles once', () => {
  const sockets = [], events = {}, timers = new Map();
  let reconciles = 0, messages = 0, timerId = 0;
  class Socket {
    constructor() { sockets.push(this); }
    close() { this.closed = true; this.onclose?.(); }
  }
  const ctx = vm.createContext({
    WebSocket: Socket, location: { protocol: 'http:', host: 'test' },
    _commonWs: null, _commonReconnectTimer: null, _commonRememberSyncSeq() {},
    document: { visibilityState: 'visible', addEventListener: (type, fn) => events[type] = fn },
    window: { frameElement: { dataset: { aionSubPageVisible: '1' } }, addEventListener() {} },
    setTimeout: fn => { timers.set(++timerId, fn); return timerId; },
    clearTimeout: id => timers.delete(id),
  });
  const source = read('common.js');
  vm.runInContext(block(source, 'function connectCommonWS(', 'const _homePopups =')
    + block(source, 'function connectRetainedPageWS(', '// Reveal page content'), ctx);
  ctx.connectRetainedPageWS(() => messages++, { reconcile: () => reconciles++ });
  assert.equal(sockets.length, 1);
  sockets[0].onopen();
  assert.equal(reconciles, 1);
  ctx.window.onAionSubPageVisibilityChanged(true);
  assert.equal(sockets.length, 1);
  sockets[0].onclose();
  assert.equal(timers.size, 1);
  ctx.window.onAionSubPageVisibilityChanged(false);
  assert.equal(timers.size, 0);
  assert.equal(sockets[0].closed, true);
  assert.equal(sockets[0].onmessage, null);
  events.visibilitychange();
  assert.equal(sockets.length, 1, 'visible browser must not wake a hidden iframe');
  ctx.window.onAionSubPageVisibilityChanged(true);
  sockets[1].onopen();
  sockets[1].onmessage({ data: '{"type":"family_event"}' });
  assert.equal(reconciles, 2);
  assert.equal(messages, 1);
  ctx.document.visibilityState = 'hidden';
  events.visibilitychange();
  assert.equal(sockets[1].closed, true);
  ctx.document.visibilityState = 'visible';
  events.visibilitychange();
  assert.equal(sockets.length, 3);
});

test('memory refresh leaves open drafts and an older page intact', async () => {
  let requests = 0;
  const dialog = {open:true}, list = {scrollTop:0}, detail = {classList:{contains:()=>false}};
  const ctx = vm.createContext({el:id=>({dialog,list,detail}[id]),state:{page:1},refreshPending:false,
    request(){requests++;}, clearTimeout(){}, searchTimer:null});
  const source = read('memory-library.js');
  vm.runInContext(block(source,'  function refreshBlocked()', '  function snapshotBridge()'),ctx);
  vm.runInContext(block(source,'  async function loadList(', '  function closeDetail()'),ctx);
  await ctx.loadList({quiet:true});
  assert.equal(ctx.refreshPending,true);
  dialog.open=false;ctx.state.page=2;ctx.refreshPending=false;
  await ctx.loadList({quiet:true});
  assert.equal(ctx.refreshPending,true);
  ctx.state.page=1;list.scrollTop=200;
  await ctx.loadList({quiet:true});
  assert.equal(requests,0);
});

test('quiet family refresh keeps settings edits, scroll position and cached data on failure', async () => {
  let editing = false, finish, renderCount = 0;
  const elements = {
    timelineList: { innerHTML: 'cached' }, timelineHours: { value: '24' }, timelinePane: { scrollTop: 150 },
    relationshipDateEditor: { classList: { contains: () => false } },
  };
  const ctx = vm.createContext({
    document: { querySelector: () => editing }, $: id => elements[id],
    timelineRequestId: 0, timelineLoading: false, autonomyRequestId: 0, autonomyLoading: false,
    autonomyData: { version: 'cached' }, activeNicheActor: null,
    api: () => new Promise(resolve => finish = resolve),
    renderRoles() { renderCount++; }, renderPrivateSpaceTabs() {},
    renderTimelineEvent: item => item.id,
  });
  const source = read('family-dynamics.html');
  vm.runInContext(block(source, 'function familySettingsEditing()', 'function renderPrivateSpaceTabs()')
    + block(source, 'async function loadTimeline(', 'function showTimelineDetail('), ctx);
  const config = ctx.loadAutonomy({ quiet: true });
  editing = true;
  finish({ version: 'server' });
  await config;
  assert.equal(renderCount, 0);
  assert.equal(ctx.autonomyData.version, 'cached');
  const timeline = ctx.loadTimeline({ quiet: true });
  assert.equal(elements.timelineList.innerHTML, 'cached');
  finish({ items: [{ id: 'new' }] });
  await timeline;
  assert.equal(elements.timelinePane.scrollTop, 150);
  assert.equal(elements.timelineList.innerHTML, 'new');
  ctx.api = async () => { throw Error('offline'); };
  await ctx.loadTimeline({ quiet: true });
  assert.equal(elements.timelineList.innerHTML, 'new');
  editing = false;
  ctx.autonomyData.roles = [{ actor: 'test', config: { min_interval_minutes: 10, max_interval_minutes: 20, actions: { walk: true } } }];
  elements.settings_test = { querySelectorAll: () => [{ dataset: { action: 'walk' }, checked: false }] };
  elements.min_test = { value: '10' };
  elements.max_test = { value: '20' };
  assert.equal(ctx.familySettingsEditing(), true, 'collapsed checkbox edits are still drafts');
  ctx.api = () => { throw Error('must not refresh a collapsed draft'); };
  await ctx.loadAutonomy({ quiet: true });
  assert.equal(renderCount, 0);
});

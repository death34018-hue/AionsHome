'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = file => fs.readFileSync(path.join(__dirname, 'static', file), 'utf8');
const block = (s, start, end) => s.slice(s.indexOf(start), s.indexOf(end, s.indexOf(start)));

test('a warm attention call opens its source without replacing the chat document or a ready room', () => {
  const pages = [], selected = [];
  const room = {id: 'first', type: 'group'};
  const frame = {contentWindow: {}};
  const ctx = vm.createContext({
    URL, location: {origin: 'https://home', pathname: '/chat'}, currentConvId: 'private',
    persistentSubPageFrames: new Map([['/chatroom', frame]]),
    openSubPage: url => pages.push(url), closeSubPage: () => pages.push('/chat'),
    selectConv: id => selected.push(id),
  });
  const roomCtx = vm.createContext({URL, location: ctx.location, rooms: [room, {id: 'second', type: 'connor_1v1'}], currentRoom: room,
    selectRoom: async id => { selected.push(id); }});
  const source = read('chat.js'), roomSource = read('chatroom.js');
  for (const [context, file, name] of [[ctx, source, 'openAttentionConversation'], [roomCtx, roomSource, 'openChatroomRoute']]) {
    const start = file.indexOf('function ' + name + '(');
    assert.notEqual(start, -1, name + ' must handle warm calls');
    vm.runInContext(file.slice(start, file.indexOf('\n}', start) + 2), context);
  }
  frame.contentWindow.openChatroomRoute = roomCtx.openChatroomRoute;
  ctx.openSubPage = url => {
    pages.push(url);
    ctx.persistentSubPageFrames.get('/chatroom')?.contentWindow.openChatroomRoute(url);
  };
  for (const route of ['/chatroom?room=first', '/chatroom?room=second', '/chat?conv=private', '/chat?conv=another']) {
    assert.equal(ctx.openAttentionConversation('https://home' + route), true);
  }
  assert.deepEqual(pages, ['/chatroom?room=first', '/chatroom?room=second', '/chat', '/chat']);
  assert.deepEqual(selected, ['second', 'another']);
  assert.equal(ctx.openAttentionConversation('https://other/chat?conv=another'), false);
  ctx.persistentSubPageFrames.clear();
  ctx.openAttentionConversation('https://home/chatroom?room=new');
  assert.equal(pages.at(-1), '/chatroom?room=new');
});

test('ordinary re-entry from their private chat switches immediately to a group and checks latest activity', async () => {
  const source = read('chatroom.js'), selected = [];
  const peer = {id:'peer', type:'peer_dm'}, first = {id:'first', type:'group'}, newer = {id:'newer', type:'group'};
  const context = vm.createContext({
    URL, location:{origin:'https://home'}, rooms:[peer, first], currentRoom:peer, activeTab:'peer_dm',
    selectRoom(id) { selected.push(id); context.currentRoom = context.rooms.find(room => room.id === id); context.activeTab = context.currentRoom.type; },
    renderRoomList() {}, api: async () => [peer, newer, first],
  });
  vm.runInContext(block(source, 'let crRoomSelectionVersion =', 'async function selectRoom('), context);
  assert.equal(context.openChatroomRoute('/chatroom'), true);
  assert.deepEqual(selected, ['first']);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(selected, ['first', 'newer']);
  assert.equal(context.openChatroomRoute('/chatroom?room=peer'), true);
  assert.equal(selected.at(-1), 'peer');
});

test('floating chat follows the visible subpage and returning to private chat', () => {
  const pages = [];
  const element = { classList: { remove() {}, toggle() {}, contains: () => true } };
  const ctx = vm.createContext({
    URL, location: { origin: 'http://home' }, $: () => element,
    window: { AionFloatingChat: { setVisiblePage: page => pages.push(page) }, AionSubPageNavigation: { hide() {} } },
    _subPageNames: {}, resetSubPageChrome() {}, applySubPageThemeChrome() {},
    activeSubPageFrame: null, transientSubPageFrame: {}, currentSubPage: '/theater',
    notifySubPageLifecycle() {}, syncHealthRingPageVisibility() {}, applyAionTheme() {}, reportPhoneHomePage() {},
    localStorage: { getItem: () => 'dark' }, currentConvId: null,
  });
  const source = read('chat.js');
  vm.runInContext(block(read('chat.html'), 'function syncSubPageLayout(', '// Start the desktop')
    + block(source, 'function syncSubPageMode(', 'const persistentSubPageFrames =')
    + block(source, 'function closeSubPage(', '// 导航到 Home'), ctx);
  ctx.syncSubPageMode('/reading?book=1');
  ctx.syncSubPageMode('/theater');
  ctx.syncSubPageMode('/chatroom?room=1');
  ctx.closeSubPage();
  assert.deepEqual(pages, ['/reading', '/theater', '/chatroom', '/chat']);
  delete ctx.window.AionFloatingChat;
  assert.doesNotThrow(() => ctx.syncSubPageMode('/reading'), 'older apps and browsers still navigate');
});

test('phone activity follows visible theater, chat and native foreground state', async () => {
  const reports = [], timers = [];
  const element = { classList: { remove() {}, toggle() {}, contains: () => true } };
  const ctx = vm.createContext({
    URL, location: { origin: 'http://home' }, $: () => element,
    navigator: { userAgent: 'Chrome AionChatApp/1.0' },
    document: { hidden: false, addEventListener() {} },
    window: { AionSubPageNavigation: { hide() {} } },
    api: async (method, url, body) => { reports.push({ method, url, body }); },
    setInterval: fn => { timers.push(fn); },
    _subPageNames: {}, resetSubPageChrome() {}, applySubPageThemeChrome() {},
    activeSubPageFrame: null, transientSubPageFrame: {}, currentSubPage: null,
    syncHealthRingPageVisibility() {}, applyAionTheme() {}, _sendTTSState() {},
    localStorage: { getItem: () => 'dark' }, currentConvId: null,
  });
  const source = read('chat.js');
  vm.runInContext(block(read('chat.html'), 'function syncSubPageLayout(', '// Start the desktop')
    + block(source, 'function syncSubPageMode(', 'const persistentSubPageFrames =')
    + block(source, 'let _aionAppForeground =', 'window.onAionPhoneCameraCaptureState =')
    + block(source, 'function closeSubPage(', '// 导航到 Home'), ctx);
  ctx.window.onAionAppForegroundChanged(true);
  ctx.syncSubPageMode('/theater?story=1');
  ctx.closeSubPage();
  ctx.syncSubPageMode('/theater');
  ctx.syncSubPageMode('/chatroom');
  ctx.syncSubPageMode('/theater');
  ctx.window.onAionAppForegroundChanged(false);
  timers.forEach(fn => fn());
  ctx.window.onAionAppForegroundChanged(true);
  timers.forEach(fn => fn());
  assert.deepEqual(reports.map(row => row.body.data.home_page.value),
    ['home', 'theater', 'home', 'theater', 'home', 'theater', 'background', 'theater', 'theater']);
  assert.ok(reports.every(row => row.method === 'POST' && row.url === '/api/device-context/phone'));
  ctx.navigator.userAgent = 'Chrome';
  ctx.syncSubPageMode('/theater');
  ctx.window.onAionAppForegroundChanged(true);
  timers.forEach(fn => fn());
  assert.equal(reports.length, 9, 'desktop browsing must not change phone activity');
});

function subPageLifecycle(userAgent, hidden = false) {
  const events = {}, foreground = [];
  const frame = { dataset: {}, contentWindow: { onAionAppForegroundChanged: value => foreground.push(value) } };
  const ctx = vm.createContext({
    navigator: { userAgent }, document: { hidden, addEventListener: (name, fn) => events[name] = fn },
    window: {}, activeSubPageFrame: frame, _sendTTSState() {},
    setInterval() {}, api: async () => ({}),
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

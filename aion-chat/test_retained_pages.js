'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = file => fs.readFileSync(path.join(__dirname, 'static', file), 'utf8');
const block = (s, start, end) => s.slice(s.indexOf(start), s.indexOf(end, s.indexOf(start)));

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

test('memory background refresh preserves an open editor and a reader on older pages', async () => {
  let editing = false, finish;
  const list = { scrollTop: 0 };
  const ctx = vm.createContext({
    document: { querySelector: () => editing }, $: id => id === 'memList' ? list : { value: '' },
    _memoryKindMenuId: null, _memoryLoading: false, _memoryRequestId: 0, _memoryKindFilter: 'all',
    _allMemories: [{ id: 'cached' }], MEMORY_PAGE_SIZE: 50, URLSearchParams,
    api: () => new Promise(resolve => finish = resolve),
    renderMemories() { throw Error('must not replace reader state'); },
  });
  vm.runInContext(block(read('memory.html'), 'function memoryRefreshBlocked()', 'function loadMoreMemories()'), ctx);
  const pending = ctx.loadMemories({ quiet: true });
  editing = true;
  finish({ items: [{ id: 'new' }] });
  await pending;
  assert.equal(ctx._allMemories[0].id, 'cached');
  assert.equal(ctx._memoryLoading, false);
  editing = false;
  list.scrollTop = 200;
  finish = null;
  await ctx.loadMemories({ quiet: true });
  assert.equal(finish, null, 'do not reset pagination while reading');
  ctx._allMemories = Array.from({ length: 100 }, (_, id) => ({ id }));
  ctx.renderMemories = () => {};
  ctx.saveMemorySnapshot = () => {};
  ctx.upsertMemory = item => ctx._allMemories.push(item);
  const manual = ctx.refreshMemoryList();
  assert.equal(list.scrollTop, 0);
  finish({ items: [{ id: 'latest' }], next_cursor: 'next', has_more: true });
  await manual;
  assert.equal(ctx._allMemories.length, 1);
  assert.equal(ctx._allMemories[0].id, 'latest');
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

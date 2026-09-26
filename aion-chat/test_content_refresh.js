'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const read = file => fs.readFileSync(path.join(__dirname, 'static', file), 'utf8');
const block = (s, a, b) => s.slice(s.indexOf(a), s.indexOf(b, s.indexOf(a)));

test('diary author switch cannot be overwritten by a slower previous request', async () => {
  const pending = [];
  const ctx = vm.createContext({
    currentAuthor: 'aion', PAGE_SIZE: 20, diaryRequestId: 0, diaryLoading: false,
    allItems: [], renderList() {}, console,
    api: () => new Promise(resolve => pending.push(resolve)),
  });
  vm.runInContext(block(read('diary.html'), 'async function loadDiaries', 'function switchAuthor'), ctx);
  const old = ctx.loadDiaries();
  ctx.currentAuthor = 'connor';
  const latest = ctx.loadDiaries();
  pending[1]({ items: [{ id: 'new-author' }], total: 1 });
  await latest;
  pending[0]({ items: [{ id: 'old-author' }], total: 1 });
  await old;
  assert.equal(ctx.allItems[0].id, 'new-author');
});

test('memory reconnection reads the current server list without applying old event data', () => {
  let handler, options, loads = 0;
  const ctx = vm.createContext({
    connectRetainedPageWS(fn, opts) { handler = fn; options = opts; },
    loadMemories() { loads++; },
  });
  vm.runInContext(block(read('memory.html'), 'connectRetainedPageWS(msg =>', '(async function init()'), ctx);
  assert.equal(typeof options?.reconcile, 'function');
  options.reconcile();
  handler({ type: 'memory_updated', data: { id: 'old', content: 'historical' } });
  assert.equal(loads, 2);
});

test('diary reconnect is wired to a fresh list, including missed deletions', () => {
  let options, handler, loads = 0;
  const ctx = vm.createContext({
    connectCommonWS(fn, opts) { handler = fn; options = opts; },
    loadDiaries() { loads++; },
  });
  vm.runInContext(block(read('diary.html'), 'function connectDiaryWS', '(async function init()'), ctx);
  ctx.connectDiaryWS();
  options.reconcile();
  handler({ type: 'diary_updated' });
  assert.equal(loads, 2);
});

test('overlapping wish refreshes retain the latest response', async () => {
  const pending = [];
  const ctx = vm.createContext({
    wishRequestId: 0, state: {},
    api: () => new Promise(resolve => pending.push(resolve)),
    renderAuthorChoices() {}, updateImplementLabels() {}, renderBook() {},
  });
  vm.runInContext(block(read('wishes.html'), 'async function loadWishes', 'function renderAuthorChoices'), ctx);
  const old = ctx.loadWishes(), latest = ctx.loadWishes();
  pending[1]({ items: [{ id: 'new' }] });
  await latest;
  pending[0]({ items: [{ id: 'old' }] });
  await old;
  assert.equal(ctx.state.wishes[0].id, 'new');
});

test('schedule refresh ignores a late response from before a change', async () => {
  const pending = [], rendered = [];
  const ctx = vm.createContext({
    scheduleRequestId: 0, api() {}, console,
    ScheduleUI: { loadScheduleLists: () => new Promise(resolve => pending.push(resolve)) },
    renderScheduleList: items => rendered.push(items[0]),
  });
  vm.runInContext(block(read('schedule.html'), 'async function loadSchedules', 'function switchScheduleTab'), ctx);
  const old = ctx.loadSchedules(), latest = ctx.loadSchedules();
  pending[1]({ active: ['new'], history: null, errors: [] });
  await latest;
  pending[0]({ active: ['old'], history: null, errors: [] });
  await old;
  assert.deepEqual(rendered, ['new']);
});

test('family timeline preserves the latest selected range when requests finish out of order', async () => {
  const pending = [];
  const el = { value: '24' };
  const ctx = vm.createContext({
    timelineRequestId: 0, timelineLoading: false, $: () => el,
    api: () => new Promise(resolve => pending.push(resolve)),
    renderTimelineEvent: item => item.id,
  });
  vm.runInContext(block(read('family-dynamics.html'), 'async function loadTimeline', 'function showTimelineDetail'), ctx);
  const old = ctx.loadTimeline(), latest = ctx.loadTimeline();
  pending[1]({ items: [{ id: 'new' }] });
  await latest;
  pending[0]({ items: [{ id: 'old' }] });
  await old;
  assert.equal(el.innerHTML, 'new');
});

test('changed page scripts remain valid JavaScript', () => {
  for (const file of ['memory.html', 'diary.html', 'moments.html', 'schedule.html', 'wishes.html', 'family-dynamics.html']) {
    for (const match of read(file).matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) {
      new vm.Script(match[1], { filename: file });
    }
  }
});

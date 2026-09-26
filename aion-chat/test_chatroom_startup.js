const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(`${__dirname}/static/chatroom.js`, 'utf8');

test('room messages appear while listener, model and companionship requests are still pending', async () => {
  const calls = [];
  const never = new Promise(() => {});
  const context = {
    api: async path => path === '/config' ? {} : [{ id: 'room-1' }],
    crAmbientRefreshListenerState: () => never,
    fetchCurrentModel: () => never,
    crLoadProactiveCompanionshipStatus: () => never,
    applyChatroomNames() {}, crApplyAmbientVoiceConfig() {},
    renderRoomList() {}, renderEmptyChat() {}, rooms: [], currentRoom: null,
    selectRoom: async id => { calls.push(id); },
    crAmbientSyncRunning() {}, connectWS() { calls.push('ws'); }, resizeInput() {},
    URLSearchParams, location: { search: '' }, console, setTimeout,
  };
  vm.runInNewContext(source.slice(source.lastIndexOf('(async function init()')), context);
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(calls.includes('room-1'), 'messages must not wait for auxiliary requests');
  assert.ok(calls.includes('ws'));
});

test('late model initialization preserves newer model settings', async () => {
  let resolveRequests;
  const pending = new Promise(resolve => { resolveRequests = resolve; });
  const context = {
    chatroomModel: '', chatroomConnorModel: 'Codex', chatroomReplyOrder: 'random',
    fetch: async url => { await pending; return { json: async () => url.includes('conversations') ? [{ model: 'old-aion' }] : [] }; },
    updateHeaderActions() {},
  };
  vm.runInNewContext(source.slice(source.indexOf('async function fetchCurrentModel('), source.indexOf('function renderModelOptions(')), context);
  const loading = context.fetchCurrentModel(Promise.resolve({ connor_model: 'old-connor', reply_order: 'aion_first' }));
  context.chatroomModel = 'new-aion'; context.chatroomConnorModel = 'new-connor'; context.chatroomReplyOrder = 'manual';
  resolveRequests();
  await loading;
  assert.equal(context.chatroomModel, 'new-aion');
  assert.equal(context.chatroomConnorModel, 'new-connor');
  assert.equal(context.chatroomReplyOrder, 'manual');
});

test('generation waits for startup settings without blocking reading', () => {
  const notices = [];
  const context = { crStartupModelsReady: false, toast: text => notices.push(text) };
  vm.runInNewContext(source.match(/function crRequireModels\([^]*?\r?\n\}/)[0], context);
  assert.equal(context.crRequireModels(), false);
  assert.equal(notices.length, 1);
  context.crStartupModelsReady = true;
  assert.equal(context.crRequireModels(), true);
});

test('voice recording does not start before model settings are ready', async () => {
  const notices = [];
  const context = { crRequireModels: () => { notices.push('loading'); return false; } };
  vm.runInNewContext(source.match(/async function _crVoiceStartRecord\([^]*?\r?\n\}/)[0], context);
  await context._crVoiceStartRecord({});
  assert.deepEqual(notices, ['loading']);
});

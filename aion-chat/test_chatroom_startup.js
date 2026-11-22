const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(`${__dirname}/static/chatroom.js`, 'utf8');

test('room messages appear while listener, model and companionship requests are still pending', async () => {
  const calls = [];
  const never = new Promise(() => {});
  const context = {
    api: async path => path === '/config' ? {} : [
      { id: 'peer', type: 'peer_dm' }, { id: 'private', type: 'connor_1v1' }, { id: 'room-1', type: 'group' },
    ],
    crAmbientRefreshListenerState: () => never,
    fetchCurrentModel: () => never,
    crLoadProactiveCompanionshipStatus: () => never,
    applyChatroomNames() {}, crApplyAmbientVoiceConfig() {},
    renderRoomList() {}, renderEmptyChat() {}, rooms: [], currentRoom: null,
    selectRoom: async id => { calls.push(id); },
    crAmbientSyncRunning() {}, connectWS() { calls.push('ws'); }, resizeInput() {},
    URLSearchParams, location: { search: '' }, console, setTimeout, window: {},
  };
  vm.runInNewContext(source.slice(source.lastIndexOf('(async function init()')), context);
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(calls.includes('room-1'), 'messages must not wait for auxiliary requests');
  assert.ok(!calls.includes('peer') && !calls.includes('private'));
  assert.ok(calls.includes('ws'));
});

test('late model initialization preserves newer model settings', async () => {
  let resolveRequests;
  const pending = new Promise(resolve => { resolveRequests = resolve; });
  const context = {
    chatroomModel: '', chatroomConnorModel: 'Codex', chatroomReplyOrder: 'random',
    fetch: async url => { await pending; return { ok: true, json: async () => url.includes('conversations') ? [{ model: 'old-aion' }] : [] }; },
    document: { getElementById: () => null },
    updateHeaderActions() {},
  };
  vm.runInNewContext(source.slice(source.indexOf('async function fetchChatroomModels('), source.indexOf('function updateHeaderActions(')), context);
  const loading = context.fetchCurrentModel(Promise.resolve({ aion_model: 'old-aion', connor_model: 'old-connor', reply_order: 'aion_first' }));
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

function modelHarness(fetch) {
  const selects = {
    setAionModel: { value: 'cheap-flash', innerHTML: '' },
    setConnorModel: { value: 'Codex-6-Sol', innerHTML: '' },
  };
  const context = {
    chatroomModel: 'cheap-flash', chatroomConnorModel: 'Codex', chatroomReplyOrder: 'random',
    chatroomModels: [], fetch, console: { warn() {} },
    document: { getElementById: id => selects[id] || null },
    updateHeaderActions() {}, esc: text => text,
  };
  const start = source.includes('async function fetchChatroomModels(')
    ? source.indexOf('async function fetchChatroomModels(')
    : source.indexOf('async function fetchCurrentModel(');
  vm.runInNewContext(source.slice(start, source.indexOf('function updateHeaderActions(')), context);
  return { context, selects };
}

const modelRows = [{ key: 'cheap-flash' }, { key: 'Codex-6-Sol' }, { key: 'Codex-6.1-Sol' }];

test('startup uses the saved group model regardless of which settings request finishes first', async () => {
  for (const configFirst of [true, false]) {
    let releaseConfig, releaseConversations;
    const config = new Promise(resolve => { releaseConfig = resolve; });
    const conversations = new Promise(resolve => { releaseConversations = resolve; });
    const { context } = modelHarness(async url => ({
      ok: true, json: async () => url.includes('conversations') ? conversations : modelRows,
    }));
    const loading = context.fetchCurrentModel(config);
    const finishConfig = () => releaseConfig({ aion_model: 'group-model' });
    const finishConversations = () => releaseConversations([{ model: 'private-model' }]);
    (configFirst ? finishConfig : finishConversations)();
    await new Promise(resolve => setImmediate(resolve));
    (configFirst ? finishConversations : finishConfig)();
    await loading;
    assert.equal(context.chatroomModel, 'group-model', `configFirst=${configFirst}`);
  }
});

test('available models survive an unrelated conversation request failure', async () => {
  const { context } = modelHarness(async url => {
    if (url.includes('conversations')) throw new Error('network failure');
    return { ok: true, json: async () => modelRows };
  });
  await context.fetchCurrentModel(Promise.resolve({ connor_model: 'Codex-6-Sol' }));
  assert.equal(context.chatroomModels.length, 3);
  assert.equal(context.chatroomConnorModel, 'Codex-6-Sol');
});

test('late model list updates open selectors before conversation loading finishes and preserves a user selection', async () => {
  let releaseConversations;
  const conversations = new Promise(resolve => { releaseConversations = resolve; });
  const { context, selects } = modelHarness(async url => ({
    ok: true, json: async () => url.includes('conversations') ? conversations : modelRows,
  }));
  const loading = context.fetchCurrentModel(Promise.resolve({}));
  await new Promise(resolve => setImmediate(resolve));
  assert.match(selects.setConnorModel.innerHTML, /Codex-6\.1-Sol/);
  assert.equal(selects.setConnorModel.value, 'Codex-6-Sol');
  releaseConversations([]);
  await loading;
});

test('opening settings retries a model list that failed during startup', async () => {
  let attempts = 0;
  const { context, selects } = modelHarness(async url => {
    if (url.includes('conversations')) return { ok: true, json: async () => [] };
    if (++attempts === 1) throw new Error('temporary network failure');
    return { ok: true, json: async () => modelRows };
  });
  await context.fetchCurrentModel(Promise.resolve({}));
  Object.assign(context, {
    currentRoom: { id: 'room-1' }, Date,
    crLoadSettingsSnapshot: () => null, crPopulateSettings() {}, crCurrentSettingsConfig: () => ({}),
    crLoadTTSVoices() {}, crSaveSettingsSnapshot() {}, toast() {},
    crIntimacyEnabled: () => false, crApplyIntimacyState() {}, crSyncIntimacy() {},
    api: async () => ({ room: { id: 'room-1' }, config: {} }),
  });
  selects.settingsOverlay = { classList: { add() {} } };
  vm.runInNewContext(source.slice(source.indexOf('async function openSettings()'), source.indexOf('function closeSettings()')), context);
  await context.openSettings();
  await new Promise(resolve => setImmediate(resolve));
  assert.match(selects.setConnorModel.innerHTML, /Codex-6\.1-Sol/);
});

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, 'static/theater.html'), 'utf8');
const studio = fs.readFileSync(path.join(__dirname, 'static/theater-studio.js'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const pending = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };

function initHarness(api, storage = new Map(), window = {}) {
  const elements = new Map();
  const calls = [];
  const context = vm.createContext({
    window, api, models: [], personas: [], conversations: [], currentConvId: null,
    ttsEnabled: false, ttsVoice: '',
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
    $: id => { if (!elements.has(id)) elements.set(id, { value: '', innerHTML: '' }); return elements.get(id); },
    loadTTSVoices: () => api('GET', '/api/tts/voices'),
    renderPersonaList() {}, renderConvList() {}, updateTheaterSelectionSummary() {},
    renderMessages() {}, showToast() {}, escHtml: text => text,
    async selectConv(id) { context.currentConvId = id; calls.push(id); },
    connectWS() { calls.push('ws'); },
  });
  vm.runInContext(html.slice(html.indexOf('async function init()'), html.indexOf('/* ── TTS 音色加载')), context);
  return { context, elements, calls };
}

test('opening the second novel moves it first and restores it on the next visit from any address', async () => {
  const lanStorage = new Map();
  const server = { last: null, progress: {} };
  let ids = ['old', 'reading'];
  const api = async (_, url) => url === '/api/theater/conversations' ? ids.map(id => ({ id })) : [];
  const openPage = async (storage = lanStorage) => {
    const window = { theaterLastConversation: async () => server.last };
    const { context } = initHarness(api, storage, window);
    Object.assign(context, {
      selectionToken: 0, performance, modes: new Map(), novel: () => true, base: '/api/theater/studio',
      setTimeout: () => 1, clearTimeout() {},
      fetch: async (url, options) => {
        const cid = url.split('/').at(-2), body = JSON.parse(options.body);
        server.progress[cid] = body;
        if (body.book) server.last = cid;
        return { ok: true };
      },
      request: async path => ({ book: { mode: 'novel' }, chapters: [{ id: 'one' }, { id: 'two' }],
        progress: server.progress[path.split('/').at(-1)] }),
      closeSidebar() {}, modeUI() {}, renderNovel() {}, showToast(message) { throw Error(message); },
    });
    vm.runInContext(studio.slice(studio.indexOf('  function setPlanningView('), studio.indexOf('  function readerSize(')), context);
    vm.runInContext(studio.slice(studio.indexOf('  selectConv = async'), studio.indexOf('  renderMessages = function')), context);
    await context.init();
    return context;
  };
  const first = await openPage();
  await first.selectConv('reading');
  first.selectChapter = null;
  vm.runInContext("selected='two'; setPlanningView(false)", first);
  assert.equal(first.conversations[0].id, 'reading');
  const reopened = await openPage();
  assert.equal(reopened.currentConvId, 'reading');
  assert.equal(reopened.conversations[0].id, 'reading');
  const remote = await openPage(new Map());
  assert.equal(remote.currentConvId, 'reading', 'another address opens the last story');
  assert.equal(remote.selected, 'two', 'and the last chapter');
  assert.equal(remote.planningView, false);
  ids = ['old'];
  assert.equal((await openPage()).currentConvId, 'old', 'a deleted story falls back to the first available one');
});

test('stories open while model, voice and persona requests are delayed', async () => {
  for (const slowModels of [true, false]) {
    const never = new Promise(() => {});
    const f = initHarness(async (_, url) => {
      if (url === '/api/theater/conversations') return [{ id: 'story', model: 'saved-model' }];
      if (url === '/api/models' && !slowModels) return [{ key: 'saved-model' }];
      return never;
    });
    f.context.init();
    await tick();
    assert.equal(f.context.currentConvId, 'story', 'reading must not wait for settings or voices');
    assert.ok(f.calls.includes('ws'));
  }
});

test('late models restore the story model and preserve a newer user selection', async () => {
  for (const chosen of ['', 'new-model']) {
    const modelRequest = pending();
    const f = initHarness(async (_, url) => {
      if (url === '/api/models') return modelRequest.promise;
      if (url === '/api/theater/conversations') return [{ id: 'story', model: 'saved-model' }];
      return [];
    });
    f.context.init();
    await tick();
    f.context.$('modelSelect').value = chosen;
    modelRequest.resolve([{ key: 'saved-model' }, { key: 'new-model' }]);
    await tick();
    assert.equal(f.context.$('modelSelect').value, chosen || 'saved-model');
  }
});

test('studio opens stories before library metadata and names finish loading', async () => {
  const library = pending(), names = pending();
  let opened = false, rerenders = 0;
  const context = vm.createContext({
    window: {}, mount() {}, action: fn => fn(), request: () => library.promise, api: () => names.promise,
    modes: new Map(), discussionNames: {}, currentConvId: null,
    async init() { opened = true; context.currentConvId = 'story'; },
    renderConvList() {}, novel: () => true, renderNovel() { rerenders++; },
    $() { throw Error('must not show the empty library for an existing story'); },
  });
  vm.runInContext(studio.slice(studio.indexOf('  async function boot()'), studio.lastIndexOf('  return {')), context);
  context.boot();
  await tick();
  assert.ok(opened, 'library metadata must not hold up story loading');
  library.resolve([{ id: 'story', mode: 'novel' }]);
  names.resolve({ user_name: 'Reader', ai_name: 'Writer' });
  await tick();
  assert.equal(context.modes.get('story'), 'novel');
  assert.equal(context.discussionNames.user_name, 'Reader');
  assert.equal(rerenders, 1);
});

test('the embedded page becomes visible on DOM readiness without waiting for images', () => {
  const handlers = new Map();
  const frame = {};
  let revealed = false;
  const context = {
    document: { addEventListener: (event, handler) => handlers.set(event, handler) },
    window: { frameElement: frame, parent: { AionSubPageNavigation: { ready(value) { revealed = value === frame; } } } },
  };
  vm.runInNewContext(html.slice(html.indexOf('/* ── 启动 ── */'), html.indexOf('</script>', html.indexOf('/* ── 启动 ── */'))), context);
  handlers.get('DOMContentLoaded')?.();
  assert.ok(revealed);
});

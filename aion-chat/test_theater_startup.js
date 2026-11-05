const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, 'static/theater.html'), 'utf8');
const studio = fs.readFileSync(path.join(__dirname, 'static/theater-studio.js'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const pending = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };

function initHarness(api, storage = new Map()) {
  const elements = new Map();
  const calls = [];
  const context = vm.createContext({
    api, models: [], personas: [], conversations: [], currentConvId: null,
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

test('opening the second novel moves it first and restores it on the next visit', async () => {
  const storage = new Map();
  let ids = ['old', 'reading'];
  const api = async (_, url) => url === '/api/theater/conversations' ? ids.map(id => ({ id })) : [];
  const openPage = async () => {
    const { context } = initHarness(api, storage);
    Object.assign(context, {
      selectionToken: 0, performance, modes: new Map(), novel: () => true,
      request: async () => ({ book: { mode: 'novel' }, chapters: [] }),
      closeSidebar() {}, modeUI() {}, renderNovel() {}, showToast(message) { throw Error(message); },
    });
    vm.runInContext(studio.slice(studio.indexOf('  selectConv = async'), studio.indexOf('  renderMessages = function')), context);
    await context.init();
    return context;
  };
  const first = await openPage();
  await first.selectConv('reading');
  assert.equal(first.conversations[0].id, 'reading');
  const reopened = await openPage();
  assert.equal(reopened.currentConvId, 'reading');
  assert.equal(reopened.conversations[0].id, 'reading');
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
    mount() {}, action: fn => fn(), request: () => library.promise, api: () => names.promise,
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

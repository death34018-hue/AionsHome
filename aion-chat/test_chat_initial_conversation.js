'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const CHAT_JS = fs.readFileSync(path.join(__dirname, 'static', 'chat.js'), 'utf8');

function sourceBetween(startMarker, endMarker) {
  const start = CHAT_JS.indexOf(startMarker);
  const end = CHAT_JS.indexOf(endMarker, start);
  assert.notEqual(start, -1, `missing ${startMarker}`);
  assert.notEqual(end, -1, `missing ${endMarker}`);
  return CHAT_JS.slice(start, end);
}

async function runInit({ initialConversations, lastConversationId = null, apiOverride = () => undefined }) {
  const requests = [];
  const storage = new Map();
  if (lastConversationId) storage.set('aion_last_conv', lastConversationId);

  const elements = new Map([
    ['modelSelect', { value: 'test-model' }],
    ['chatTitle', { textContent: 'Aion Chat' }],
    ['messages', { addEventListener() {} }],
    ['contextSlider', { value: '30' }],
    ['contextValue', { textContent: '' }],
    ['tempSlider', { value: '0.7' }],
    ['tempValue', { textContent: '' }],
    ['maxTokensSlider', { value: '0' }],
    ['maxTokensValue', { textContent: '' }],
    ['sendBtn', { disabled: true }],
  ]);

  const context = {
    models: [],
    worldBook: {},
    chatroomConfig: {},
    conversations: [],
    currentConvId: null,
    privateConversationLoadId: 0,
    sending: false,
    streamingAiId: null,
    currentMessages: [],
    msgDebugData: {},
    _heartWhisperMsgIds: new Set(),
    _heartWhisperContent: {},
    _memoryRecordMsgIds: new Set(),
    _memoryRecordContent: {},
    MSG_PAGE_SIZE: 50,
    location: { search: '' },
    URLSearchParams,
    setTimeout,
    Notification: { permission: 'denied' },
    window: {},
    localStorage: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
    },
    $: id => elements.get(id) || { value: '', textContent: '', addEventListener() {} },
    api: async (method, url, body) => {
      requests.push({ method, url, body });
      const overridden = apiOverride(method, url, body);
      if (overridden !== undefined) return overridden;
      if (method === 'GET' && url === '/api/models') {
        return [{ key: 'test-model', provider: 'openai' }];
      }
      if (method === 'GET' && url === '/api/worldbook') return {};
      if (method === 'GET' && url === '/api/chatroom/config') return {};
      if (method === 'GET' && url === '/api/conversations') return initialConversations;
      if (method === 'POST' && url === '/api/conversations') {
        return {
          id: 'conv-created',
          title: body.title,
          model: body.model,
          created_at: 1,
          updated_at: 1,
          message_count: 0,
        };
      }
      if (method === 'GET' && /\/messages\?limit=/.test(url)) return [];
      if (method === 'GET' && /\/api\/(heart-whispers|memories)\/by-conv\//.test(url)) return [];
      throw new Error(`unexpected request: ${method} ${url}`);
    },
    renderModelSelect() {},
    loadProactiveCompanionshipStatus: async () => {},
    renderConvList() {},
    renderMessages() { context.renderCount = (context.renderCount || 0) + 1; },
    appliedHints: [],
    _applyHeartHint(id) { context.appliedHints.push(['heart', id]); },
    _applyMemoryHint(id) { context.appliedHints.push(['memory', id]); },
    setCurrentMessages(messages) { context.currentMessages = messages; },
    closeSidebar() {},
    connectWS() {},
    jumpToChatMessage() {},
    loadOlderMessages() {},
  };

  vm.createContext(context);
  const source = [
    sourceBetween('async function init()', 'function escHtml'),
    sourceBetween('async function newConversation()', 'async function refreshCurrentConversationFromServer'),
  ].join('\n');
  vm.runInContext(`${source}\nthis.runInit = init;`, context);
  await context.runInit();

  return { context, requests, storage };
}

test('an empty installation creates and selects its first conversation', async () => {
  const { context, requests, storage } = await runInit({ initialConversations: [] });

  assert.equal(context.currentConvId, 'conv-created');
  assert.equal(storage.get('aion_last_conv'), 'conv-created');
  assert.deepEqual(
    Array.from(context.conversations, conversation => conversation.id),
    ['conv-created'],
  );
  assert.equal(
    requests.filter(request => request.method === 'POST' && request.url === '/api/conversations').length,
    1,
  );
});

test('an existing last conversation is selected without creating another one', async () => {
  const existing = {
    id: 'conv-existing',
    title: '现在的窗口',
    model: 'test-model',
    created_at: 1,
    updated_at: 2,
    message_count: 3,
  };
  const { context, requests } = await runInit({
    initialConversations: [existing],
    lastConversationId: existing.id,
  });

  assert.equal(context.currentConvId, existing.id);
  assert.equal(context.$('chatTitle').textContent, existing.title);
  assert.equal(
    requests.filter(request => request.method === 'POST' && request.url === '/api/conversations').length,
    0,
  );
});

test('desktop opens before a pending chat initialization and remains on initialization failure', async () => {
  const opened = [];
  let rejectInit;
  const context = {
    location: { search: '' }, URLSearchParams,
    window: {},
    openSubPage: url => opened.push(url),
    init: () => new Promise((resolve, reject) => { rejectInit = reject; }),
    console: { warn() {} },
  };
  vm.createContext(context);
  vm.runInContext(sourceBetween('function startChatApp()', '// ── 摄像头/监控日志'), context);
  const initialized = context.startChatApp();
  assert.deepEqual(opened, ['/']);
  rejectInit(new Error('offline'));
  await initialized;
  assert.deepEqual(opened, ['/']);
});

test('HTML desktop shell supports navigation and native back before chat scripts load', () => {
  const html = fs.readFileSync(path.join(__dirname, 'static', 'chat.html'), 'utf8');
  const shell = html.match(/<script id="initial-desktop">([\s\S]*?)<\/script>/)[1];
  const classes = new Set();
  const frames = [];
  const overlay = { classList: {
    add: name => classes.add(name), remove: name => classes.delete(name),
    toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
  } };
  const context = {
    window: {}, URL, URLSearchParams,
    location: { search: '', origin: 'https://test.invalid' },
    document: {
      createElement: () => ({ dataset: {}, style: {}, setAttribute() {} }),
      getElementById: id => id === 'subPageOverlay' ? overlay : { appendChild: frame => frames.push(frame) },
    },
  };
  vm.createContext(context);
  vm.runInContext(shell, context);
  assert.equal(frames.length, 1);
  assert.equal(frames[0].src, 'https://test.invalid/');
  assert.equal(classes.has('show'), true);
  context.window.openSubPage('/settings');
  assert.equal(frames[0].src, 'https://test.invalid/settings');
  assert.equal(context.window.__aionStartupPage, '/settings');
  assert.equal(context.window.handleNativeBack(), 'handled');
  assert.equal(frames[0].src, 'https://test.invalid/');
  assert.equal(context.window.handleNativeBack(), 'dialog');
});

test('chat startup preserves a destination already chosen on the early desktop', async () => {
  for (const target of ['/settings', '/chat']) {
    const opened = [];
    const context = {
      window: { __aionStartupPage: target },
      location: { search: '' }, URLSearchParams,
      openSubPage: url => opened.push(url), init: async () => {}, console,
    };
    vm.createContext(context);
    vm.runInContext(sourceBetween('function startChatApp()', '// ── 摄像头/监控日志'), context);
    await context.startChatApp();
    assert.deepEqual(opened, target === '/chat' ? [] : [target]);
  }
});

test('bootstrap requests start together and slow annotations do not block message display', async () => {
  const pending = new Map();
  const annotationResolvers = [];
  let finished = false;
  const initialization = runInit({
    initialConversations: [], lastConversationId: 'existing',
    apiOverride(method, url) {
      if (['/api/models', '/api/worldbook', '/api/conversations'].includes(url)) {
        return new Promise(resolve => pending.set(url, resolve));
      }
      if (/\/api\/(heart-whispers|memories)\/by-conv\//.test(url)) {
        return new Promise(resolve => annotationResolvers.push(resolve));
      }
    },
  }).then(result => { finished = true; return result; });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(pending.size, 3);
  pending.get('/api/models')([{ key: 'test-model' }]);
  pending.get('/api/worldbook')({});
  pending.get('/api/conversations')([{ id: 'existing', title: 'Test', model: 'test-model' }]);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(finished, true);
  assert.equal(annotationResolvers.length, 2);
  const { context } = await initialization;
  assert.equal(context.$('sendBtn').disabled, false);
  const rendersBeforeAnnotations = context.renderCount;
  annotationResolvers[0]([{ msg_id: 'message', content: 'heart' }]);
  annotationResolvers[1]([{ msg_id: 'message', content: 'memory' }]);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(context.renderCount, rendersBeforeAnnotations);
  assert.deepEqual(context.appliedHints, [['heart', 'message'], ['memory', 'message']]);
});

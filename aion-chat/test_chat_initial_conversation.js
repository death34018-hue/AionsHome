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

async function runInit({ initialConversations, lastConversationId = null }) {
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
    renderMessages() {},
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

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const chatroomSource = fs.readFileSync(`${__dirname}/static/chatroom.js`, 'utf8');
const chatSource = fs.readFileSync(`${__dirname}/static/chat.js`, 'utf8');

test('crLogTitle marks random pool entries with dice icon and model key', () => {
  const start = chatroomSource.indexOf('function crLogTitle(');
  const end = chatroomSource.indexOf('function crPromptLabel(');
  const context = {
    crName: actor => ({ aion: 'Aion', connor: 'Connor' }[actor] || actor),
  };
  vm.runInNewContext(chatroomSource.slice(start, end), context);

  assert.equal(
    context.crLogTitle({ actor: 'aion', model: 'gemini-4-argon', model_key: '官4-Argon', random_pool: true }),
    'Aion · 🎲 官4-Argon (gemini-4-argon)'
  );
  assert.equal(
    context.crLogTitle({ actor: 'connor', model: 'gpt-6.2-astra', model_key: 'Codex-6.2-Astra', random_pool: false }),
    'Connor · Codex-6.2-Astra (gpt-6.2-astra)'
  );
  assert.equal(
    context.crLogTitle({ actor: 'aion', model: 'Pool-A', model_key: 'Pool-A', random_pool: true }),
    'Aion · 🎲 Pool-A'
  );
});

test('chatroom random pool helpers normalize and track selected models', () => {
  const start = chatroomSource.indexOf('function crNormalizeModelList(');
  const end = chatroomSource.indexOf('function crClearRandomPool(', start);
  assert.notEqual(start, -1);
  assert.ok(end > start);
  const elements = {};
  const context = {
    crAionRandomEnabled: false,
    crAionRandomModels: [],
    crConnorRandomEnabled: false,
    crConnorRandomModels: [],
    chatroomModels: [{ key: 'Model-A' }, { key: 'Model-B' }],
    crSettingsDirtyAt: 0,
    Date,
    esc: s => String(s),
    document: { getElementById: id => elements[id] || null },
  };
  vm.runInNewContext(chatroomSource.slice(start, end), context);

  assert.deepEqual(
    Array.from(context.crNormalizeModelList([' Model-A ', 'Model-A', '', 'Model-B'])),
    ['Model-A', 'Model-B']
  );

  context.crApplyRandomModelConfig({
    aion_random_model_enabled: true,
    aion_random_models: ['Model-B'],
    connor_random_model_enabled: false,
    connor_random_models: ['Codex'],
  });
  assert.equal(context.crAionRandomEnabled, true);
  assert.deepEqual(Array.from(context.crAionRandomModels), ['Model-B']);
  assert.equal(context.crConnorRandomEnabled, false);
  assert.deepEqual(Array.from(context.crConnorRandomModels), ['Codex']);
});

test('private chat system log renders random pool indicator and model key', () => {
  const start = chatSource.indexOf('function systemLogStatus(');
  const end = chatSource.indexOf('function toggleSysLogDetail(');
  const logListEl = { innerHTML: '' };
  const countEl = { textContent: '' };
  const context = {
    $: id => ({ sysLogList: logListEl, sysLogCount: countEl }[id] || null),
    escHtml: s => String(s ?? ''),
    _buildTokenHtml: () => 'tokens',
    _buildTokenDetailHtml: () => '',
    systemLogs: [
      {
        _id: 'log1',
        _ts: '10:00:00',
        model: 'gemini-4-argon',
        model_key: '官4-Argon',
        random_pool: true,
        recalled_memories: [],
      },
    ],
  };
  vm.runInNewContext(chatSource.slice(start, end), context);
  context.renderSystemLogList();
  assert.match(logListEl.innerHTML, /🎲 官4-Argon \(gemini-4-argon\)/);
  assert.match(logListEl.innerHTML, /随机模型池抽取/);
});

test('chatroom sender line places model badge right after speaker button for AI messages only', () => {
  const start = chatroomSource.indexOf('function crMsgSenderLineHtml(');
  const end = chatroomSource.indexOf('function crSyncMsgModelBadge(');
  const context = {
    crMsgDebugData: {},
    crMsgMenuHtml: () => '<span class="menu"></span>',
    crMsgFeedbackHtml: () => '<span class="fb"></span>',
    timeStr: () => '18:00',
    esc: s => String(s ?? ''),
  };
  vm.runInNewContext(chatroomSource.slice(start, end), context);

  const aiHtml = context.crMsgSenderLineHtml(
    'aion',
    'Aion',
    'm1',
    { id: 'm1', sender: 'aion', created_at: 100, model_key: 'AGY ACP · Gemini 4 Argon (Low)' },
    { tts: true }
  );
  assert.match(
    aiHtml,
    /<button class="tts-replay-btn"[^>]*>🔊<\/button><span class="msg-model-badge"[^>]*>AGY ACP · Gemini 4 Argon \(Low\)<\/span>/
  );

  const userHtml = context.crMsgSenderLineHtml(
    'user',
    'Ithil',
    'u1',
    { id: 'u1', sender: 'user', created_at: 100, model_key: 'should-not-show' },
    { tts: true }
  );
  assert.doesNotMatch(userHtml, /msg-model-badge/);
});

test('private chat message header places model badge right after speaker button for assistant only', () => {
  const start = chatSource.indexOf('function renderPrivateMessageHTML(');
  const end = chatSource.indexOf('function updatePrivateStreamingMessage(');
  const context = {
    msgDebugData: {},
    window: {},
    worldBook: { user_name: 'Ithil', ai_name: 'Aion' },
    fmtTime: () => '18:00',
    escHtml: s => String(s ?? ''),
    withWishFallbackAttachments: () => [],
    renderBandVibrationNote: () => '',
    stripWishFulfillmentMarker: s => s,
    privateMessageContentItemsWithInlinePats: s => [{ type: 'text', text: s }],
    privateMarkdownMessageHtml: s => s,
    renderAttachments: () => '',
  };
  vm.runInNewContext(chatSource.slice(start, end), context);

  const aiHtml = context.renderPrivateMessageHTML({
    id: 'p1',
    role: 'assistant',
    content: 'hi',
    created_at: 100,
    model_key: '官4-Argon',
  });
  assert.match(
    aiHtml,
    /<button class="tts-replay-btn"[^>]*>🔊<\/button><span class="msg-model-badge"[^>]*>官4-Argon<\/span>/
  );

  const userHtml = context.renderPrivateMessageHTML({
    id: 'p2',
    role: 'user',
    content: 'hello',
    created_at: 100,
    model_key: '官4-Argon',
  });
  assert.doesNotMatch(userHtml, /msg-model-badge/);
});


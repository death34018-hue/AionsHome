'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'static', 'chatroom.js'), 'utf8');

function extract(name, next) {
  const start = source.indexOf(`function ${name}(`);
  const end = source.indexOf(next, start);
  assert.ok(start >= 0 && end > start);
  return source.slice(start, end);
}
function harness() {
  const rows = [];
  const suppressed = new Set();
  const textarea = { style: {}, focus() { this.focused = true; } };
  const partial = { id: 'streaming-partial', content: '已收到的正文',
    classList: { contains: () => false },
    replaceWith(row) { rows.splice(rows.indexOf(this), 1, row); } };
  rows.push(partial);
  const context = {
    rows, suppressed, textarea, window: {}, crStartupModelsReady: true,
    streamingBubble: { closest: () => partial }, streamingText: '已收到的正文', streamingLookup: null,
    pendingStreamId: 'partial', pendingStreamSender: 'aion', crMessageRevision: 0,
    crMessagesById: { user: { id: 'user', sender: 'user', content: '上一条消息' } },
    _crControl: { active: {} }, isSending: true, isAiChatting: false,
    _ttsEngine: { playOrder: ['partial'] }, crTtsEnabled: true,
    crSuppressTTSMsg(id) { suppressed.add(id); },
    crStopTTS() { context._ttsEngine.playOrder = []; },
    crIsAiSender: () => false, crMemoryRecordMsgIds: new Set(),
    crShowToyCapsule() {}, crToyCommandsFromAttachments: () => [],
    msgHTML: JSON.stringify, toast() {}, crRefocusComposerAfterSend() {},
    appendMessage(msg) { rows.push(msg); },
    messagesEl: {
      querySelectorAll: () => [],
      querySelector(selector) { return rows.find(row => selector.includes(`"${row.id}"`)); },
    },
    document: {
      createElement() { return { set innerHTML(html) { this.firstElementChild = JSON.parse(html); } }; },
      getElementById() { return textarea; },
      querySelector() { return { classList: { add() {} }, querySelector: () => ({ innerHTML: '' }) }; },
    },
  };
  vm.createContext(context);
  for (const [name, next] of [
    ['crCancelTTSMessage', 'function crSuppressTTSMsg'],
    ['crRequireModels', 'let pendingAttachments'],
    ['endStreamingBubble', 'const crMemoryRecordMsgIds'],
    ['crHandleReplyFailure', 'function handleSSE'],
    ['crCanEditMessage', 'function cancelChatroomEdit'],
  ]) vm.runInContext(extract(name, next), context);
  return context;
}
test('failure keeps received text and reason, suppresses failed audio, and permits editing', () => {
  const ctx = harness();
  assert.equal(ctx.crCanEditMessage(), false);
  ctx.crHandleReplyFailure({ message: { id: 'partial', sender: 'aion',
    content: '已收到的正文\n\n[本次回复超时]' } });
  assert.equal(ctx.rows.length, 1);
  assert.match(ctx.rows[0].content, /已收到的正文/);
  assert.match(ctx.rows[0].content, /超时/);
  assert.ok(ctx.crMessagesById.partial);
  assert.equal(ctx.crMessagesById.partial.sender, 'aion');
  assert.ok(ctx.suppressed.has('partial'));
  assert.ok(!ctx.suppressed.has('next-reply'));
  assert.equal(ctx.crTtsEnabled, true);
  assert.equal(ctx.crCanEditMessage(), true);
  ctx.editChatroomMsg('user');
  assert.equal(ctx.textarea.value, '上一条消息');
  assert.equal(ctx.textarea.focused, true);
});

test('a whisper-only completion appears without public streaming chunks and is not duplicated', () => {
  const ctx = harness();
  ctx.rows.length = 0;
  ctx.streamingBubble = null;
  ctx.streamingText = '';
  const message = { id: 'whisper-only', sender: 'aion', content: '', attachments: [{ type: 'private_whisper_sent', count: 1 }] };
  ctx.endStreamingBubble(message);
  assert.equal(ctx.rows.length, 1);
  assert.equal(ctx.rows[0], message);
  ctx.endStreamingBubble(message);
  assert.equal(ctx.rows.length, 1);
});

test('native final reply replaces a stalled streaming row and ignores late stream chunks', () => {
  const ctx = harness();
  ctx.currentRoom = { id: 'r' };
  ctx.crFinalMessageIds = new Set();
  ctx.crSseMessageIds = { aion: 'partial' };
  ctx.reconcileLocalUserEcho = () => false;
  ctx.document.getElementById = id => ctx.rows.find(row => row.id === id);
  ctx.document.createElement = () => ({ set innerHTML(html) {
    this.firstElementChild = { ...JSON.parse(html), classList: { contains: () => false } };
  } });
  vm.runInContext(extract('crApplyReceivedMessage', 'const crNativeMessages'), ctx);
  const handler = source.indexOf('function handleSSE(');
  vm.runInContext(source.slice(handler, source.indexOf('\n}', handler) + 2), ctx);
  const final = { room_id: 'r', id: 'partial', sender: 'aion', content: '完整回复', attachments: [] };
  ctx.crApplyReceivedMessage(final);
  assert.equal(ctx.rows.length, 1);
  assert.equal(ctx.rows[0].content, '完整回复');
  assert.equal(ctx.streamingBubble, null);
  ctx.handleSSE({ type: 'aion_chunk', content: '迟到的半句话' });
  assert.equal(ctx.rows[0].content, '完整回复');
  ctx.crApplyReceivedMessage(final);
  assert.equal(ctx.rows.length, 1);
});

test('failed AI message retains regenerate/delete actions without voice replay', () => {
  const ctx = harness();
  Object.assign(ctx, { crMsgFeedbackHtml: () => '', esc: String });
  vm.runInContext(extract('crMsgMenuHtml', 'function crCanRateAiMsg'), ctx);
  vm.runInContext(extract('crMsgSenderLineHtml', 'function crEnsureMsgMenu'), ctx);
  const html = ctx.crMsgSenderLineHtml('aion', 'AI', 'failed', {
    id: 'failed', sender: 'aion', attachments: [{ type: 'chatroom_reply_failure' }],
  }, { tts: true });
  assert.match(html, /regenerateChatroomMsg\('failed'\)/);
  assert.match(html, /deleteMsg\('failed'/);
  assert.doesNotMatch(html, /crReplayTTS/);
  assert.match(ctx.crMsgSenderLineHtml('aion', 'AI', 'ok', {}, { tts: true }), /crReplayTTS/);
});

test('persisted failed message rejects late TTS after a refresh', () => {
  const ctx = harness();
  Object.assign(ctx, { crSuppressedTTSMsgIds: new Set(), crAmbientClientId: 'client', crTtsAcceptAfter: 0 });
  ctx.crMessagesById.failed = { attachments: [{ type: 'chatroom_reply_failure' }] };
  vm.runInContext(extract('crShouldAcceptTTSMsg', "document.addEventListener('visibilitychange'"), ctx);
  assert.equal(ctx.crShouldAcceptTTSMsg('failed', 1), false);
  assert.equal(ctx.crShouldAcceptTTSMsg('ok', 1), true);
});

test('regenerating a failed message stops the pending turn before retrying', async () => {
  for (const stopConfirmed of [false, true]) {
    const ctx = harness();
    const calls = [];
    Object.assign(ctx, { currentRoom: { id: 'room' }, API: '/api/chatroom',
      chatroomModel: 'model', chatroomConnorModel: 'companion',
      crTtsAionVoice: 'voice', crTtsConnorVoice: 'voice2', crWhisperMode: false,
      crShowGenerationStop() {}, removeRowsAfter() {}, consumeChatroomSSE: async () => {},
    });
    ctx.crMessagesById.failed = { id: 'failed', sender: 'aion' };
    ctx.crIsAiSender = sender => sender === 'aion' || sender === 'connor';
    ctx._crControl.active.replyFailed = true;
    ctx._crControl.stop = async () => {
      calls.push('stop');
      if (stopConfirmed) ctx._crControl.active = null;
    };
    ctx._crControl.begin = () => { calls.push('begin'); return {}; };
    ctx._crControl.isCurrent = () => false;
    ctx._crControl.fetch = async (_, url) => { calls.push('fetch'); assert.match(url, /failed\/regenerate$/); };
    vm.runInContext('async ' + extract('regenerateChatroomMsg', '// 点击空白处关闭下拉菜单'), ctx);
    await ctx.regenerateChatroomMsg('failed');
    assert.deepEqual(calls, stopConfirmed ? ['stop', 'begin', 'fetch'] : ['stop']);
  }
});
test('editing a failed turn waits for stop before starting another request', async () => {
  const ctx = harness();
  ctx._crControl.active.replyFailed = true;
  ctx.textarea.value = '修改后的消息';
  let starts = 0;
  ctx._crControl.begin = () => { starts++; };
  ctx._crControl.stop = async () => { ctx._crControl.retryStop = {}; };
  vm.runInContext('async ' + extract('saveChatroomEdit', 'async function regenerateChatroomMsg'), ctx);
  await ctx.saveChatroomEdit('user');
  assert.equal(starts, 0);
  assert.equal(ctx.crMessagesById.user.content, '上一条消息');
  assert.equal(ctx.textarea.disabled, false);
});
test('confirmed stop allows editing and resending with automatic TTS unchanged', async () => {
  const ctx = harness();
  const calls = [];
  Object.assign(ctx, { currentRoom: { id: 'room' }, API: '/api/chatroom',
    chatroomModel: 'model', chatroomConnorModel: 'companion',
    crTtsAionVoice: 'voice', crTtsConnorVoice: 'voice2', crWhisperMode: false,
    crShowGenerationStop() {}, removeRowsAfter() {}, consumeChatroomSSE: async () => {},
  });
  ctx.document.querySelector = () => null;
  ctx._crControl.active.replyFailed = true;
  ctx._crControl.stop = async () => { calls.push('stop'); ctx._crControl.active = null; };
  ctx._crControl.begin = () => { calls.push('begin'); return {}; };
  ctx._crControl.isCurrent = () => false;
  ctx._crControl.fetch = async (_, url, options) => {
    calls.push('fetch');
    assert.equal(JSON.parse(options.body).content, '修改后的消息');
    assert.equal(JSON.parse(options.body).tts_enabled, true);
    return {};
  };
  ctx.textarea.value = '修改后的消息';
  vm.runInContext('async ' + extract('saveChatroomEdit', 'async function regenerateChatroomMsg'), ctx);
  await ctx.saveChatroomEdit('user');
  assert.deepEqual(calls, ['stop', 'begin', 'fetch']);
});

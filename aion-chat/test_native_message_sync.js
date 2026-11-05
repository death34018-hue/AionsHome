const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');

test('native inbox paints only this room, once, and does not roll back newer live edits', () => {
  const events = [
    { native_seq: 1, sync_seq: 10, type: 'chatroom_msg_created', data: { room_id: 'r', id: 'm', content: 'first' } },
    { native_seq: 2, sync_seq: 11, type: 'chatroom_msg_created', data: { room_id: 'other', id: 'x' } },
  ];
  const painted = [];
  const ctx = { window: {}, console };
  vm.createContext(ctx);
  const file = __dirname + '/static/native-message-sync.js';
  if (fs.existsSync(file)) vm.runInContext(fs.readFileSync(file, 'utf8'), ctx);
  assert.equal(typeof ctx.window.NativeMessageSync?.create, 'function');
  const sync = ctx.window.NativeMessageSync.create({
    read: () => JSON.stringify(events), target: () => 'r', field: 'room_id',
    apply: event => painted.push(event.data.content || event.type),
  });
  sync.drain(); sync.drain();
  assert.deepEqual(painted, ['first']);
  sync.observe({ sync_seq: 15, data: { room_id: 'r', id: 'm' } });
  events.push({ native_seq: 3, sync_seq: 12, type: 'chatroom_msg_updated', data: { room_id: 'r', id: 'm', content: 'stale' } });
  events.push({ native_seq: 4, sync_seq: 16, type: 'chatroom_msg_deleted', data: { room_id: 'r', id: 'm' } });
  sync.drain();
  assert.deepEqual(painted, ['first', 'chatroom_msg_deleted']);
  assert.equal(sync.observe({ sync_seq: 11, data: { room_id: 'r', id: 'm' } }), false,
    'a delayed live create must not resurrect the native deletion');
});

test('a delayed private stream reset cannot erase a reply already delivered by native push', async () => {
  const source = fs.readFileSync(__dirname + '/static/chat.js', 'utf8');
  const payload = ['start', 'stream_reset', 'chunk'].map(type => `data: ${JSON.stringify({ type, id: 'm', content: 'old partial' })}\n`).join('');
  let read = false;
  const ctx = { window: {}, console, TextDecoder,
    currentMessages: [{ id: 'm', content: 'complete reply' }], serverMessageIds: new Set(['m']),
    _chatControl: { accepts: () => true }, renderMessages() {}, _startTypingAnim() {}, _stopTypingAnim() {},
    voiceInCall: false, finishTTSForMsg() {}, playRecv() {},
  };
  vm.createContext(ctx);
  const helper = source.indexOf('function privateStreamAlreadyFinal(');
  if (helper >= 0) vm.runInContext(source.slice(helper, source.indexOf('\n}', helper) + 2), ctx);
  vm.runInContext(source.slice(source.indexOf('async function _processSSEStream('), source.indexOf('// ── 消息操作')), ctx);
  await ctx._processSSEStream({ body: { getReader: () => ({ read: async () => {
    if (read) return { done: true }; read = true;
    return { done: false, value: Buffer.from(payload) };
  } }) } });
  assert.equal(ctx.currentMessages[0].content, 'complete reply');
});

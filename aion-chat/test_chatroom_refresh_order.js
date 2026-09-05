'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, 'static/chatroom.js'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

function harness() {
  const pending = [], renders = [], stored = new Map();
  const room = { id: 'room-a', type: 'group', title: 'Test' };
  const ctx = {
    currentRoom: room, rooms: [room], activeTab: 'group', roomTitleEl: {},
    crRoomMessageLoad: null, crRoomMessageGeneration: 0, crRenderedRoomId: room.id, crMessageRevision: 0,
    crMessagesById: { m1: { id: 'm1', content: 'visible', created_at: 1 } },
    isSending: false, isAiChatting: false, isReplyOnce: false,
    console, window: {}, Date, setTimeout, clearTimeout,
    localStorage: { getItem: key => stored.get(key), setItem: (key, value) => stored.set(key, value) },
    api: async (url, options) => url === '/rooms' ? [room] : new Promise(resolve => pending.push({ url, options, resolve })),
    renderRoomList() {}, updateHeaderActions() {}, switchTab() {}, scrollToBottom() {},
    renderMessages(msgs) {
      ctx.crRenderedRoomId = ctx.currentRoom.id;
      ctx.crMessagesById = Object.fromEntries(msgs.map(m => [m.id, m]));
      renders.push(msgs.map(m => m.content));
    },
  };
  vm.createContext(ctx);
  vm.runInContext(source.slice(source.indexOf('async function loadMessages()'), source.indexOf('async function loadOlderMessages()')), ctx);
  return { ctx, pending, renders, stored };
}

test('foreground refreshes share one request while it is in flight', async () => {
  const { ctx, pending } = harness();
  const one = ctx.refreshCurrentChatroomFromServer();
  const two = ctx.refreshCurrentChatroomFromServer();
  await tick();
  assert.equal(pending.length, 1);
  pending[0].resolve([{ id: 'm1', content: 'latest', created_at: 2 }]);
  await Promise.all([one, two]);
});

test('manual room selection supersedes a late foreground response and its cache write', async () => {
  const { ctx, pending, stored } = harness();
  const older = ctx.refreshCurrentChatroomFromServer();
  await tick();
  const newer = ctx.loadMessages();
  await tick();
  pending[1].resolve([{ id: 'm1', content: 'latest', created_at: 2 }]);
  await newer;
  pending[0].resolve([{ id: 'm1', content: 'old', created_at: 1 }]);
  await older;
  assert.equal(ctx.crMessagesById.m1.content, 'latest');
  assert.equal(JSON.parse(stored.get('chatroom_messages_snapshot_v1_room-a')).messages[0].content, 'latest');
});

test('a response crossing a live edit/delete is discarded and fetched again', async () => {
  const { ctx, pending, renders } = harness();
  const refresh = ctx.refreshCurrentChatroomFromServer();
  await tick();
  ctx.crMessageRevision++;
  ctx.crMessagesById = { m2: { id: 'm2', content: 'edited', created_at: 2 } };
  pending[0].resolve([{ id: 'm1', content: 'deleted', created_at: 1 }]);
  await tick();
  assert.equal(renders.length, 0, 'must never display the stale response');
  assert.equal(pending.length, 2);
  pending[1].resolve([{ id: 'm2', content: 'edited', created_at: 2 }]);
  await refresh;
  assert.equal(ctx.crMessagesById.m1, undefined);
  assert.equal(ctx.crMessagesById.m2.content, 'edited');
});

test('a response for a room that has been left cannot paint the new room', async () => {
  const { ctx, pending, renders } = harness();
  const refresh = ctx.loadMessages();
  await tick();
  ctx.currentRoom = { id: 'room-b', type: 'group' };
  pending[0].resolve([{ id: 'm1', content: 'wrong room', created_at: 1 }]);
  await refresh;
  assert.equal(renders.length, 0);
});

test('an unchanged background refresh avoids repaint, while explicit reload restores an editor row', async () => {
  const { ctx, pending, renders } = harness();
  const messages = Object.values(ctx.crMessagesById);
  const background = ctx.refreshCurrentChatroomFromServer();
  await tick();
  pending[0].resolve(messages);
  await background;
  assert.equal(renders.length, 0);
  const explicit = ctx.loadMessages();
  await tick();
  pending[1].resolve(messages);
  await explicit;
  assert.equal(renders.length, 1);
});

test('reconnect reconciles historical creates, edits and deletes with one fresh snapshot', async () => {
  const { ctx } = harness();
  let refreshes = 0;
  let releaseRefresh;
  ctx.CR_SYNC_SEQ_KEY = 'sync';
  ctx.crSyncReplayPromise = null;
  ctx.AbortController = AbortController;
  ctx.fetch = async () => ({ ok: true, json: async () => ({
    events: ['created', 'updated', 'deleted'].map((kind, i) => ({
      type: `chatroom_msg_${kind}`, sync_seq: i + 1,
      data: { room_id: 'room-a', id: 'm1', content: 'historical' },
    })), has_more: false,
  }) });
  ctx.refreshCurrentChatroomFromServer = async options => {
    assert.equal(options.force, true);
    refreshes++;
    return new Promise(resolve => { releaseRefresh = resolve; });
  };
  vm.runInContext(source.slice(source.indexOf('function crApplyReplayedSyncEvent('), source.indexOf('function connectWS()')), ctx);
  const replay = ctx.crReconcileSyncEvents();
  await tick();
  assert.equal(ctx.crMessagesById.m1.content, 'visible');
  assert.equal(refreshes, 1);
  assert.equal(ctx.localStorage.getItem('sync'), undefined);
  releaseRefresh(true);
  await replay;
  assert.equal(ctx.localStorage.getItem('sync'), '3');
});

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const queueApi = require('./static/theater_tts_queue.js');
const html = fs.readFileSync(path.join(__dirname, 'static', 'theater.html'), 'utf8');
const ttsBlock = html
  .split('/* ── TTS 播放 ── */')[1]
  .split('/* ── 角色管理 ── */')[0];

function createPlaybackContext({ fetchImpl } = {}) {
  const elements = new Map();
  const makeElement = () => ({
    classList: {
      values: new Set(),
      add(value) { this.values.add(value); },
      remove(value) { this.values.delete(value); },
      contains(value) { return this.values.has(value); },
    },
    textContent: '',
    disabled: false,
    value: 0,
    max: 0,
  });
  const getElement = (id) => {
    if (!elements.has(id)) elements.set(id, makeElement());
    return elements.get(id);
  };

  class FakeAudio {
    static instances = [];
    static failOnPlay = new Set();

    constructor(url) {
      this.url = url;
      this.src = url;
      this.currentTime = 0;
      this.duration = 10;
      this.paused = true;
      FakeAudio.instances.push(this);
    }

    play() {
      if (FakeAudio.failOnPlay.has(this.url)) {
        this.paused = true;
        if (this.onerror) this.onerror(new Error('missing audio'));
        return Promise.reject(new Error('missing audio'));
      }
      this.paused = false;
      if (this.onplay) this.onplay();
      return Promise.resolve();
    }

    pause() {
      this.paused = true;
      if (this.onpause) this.onpause();
    }

    load() {}

    finish() {
      this.currentTime = this.duration;
      this.paused = true;
      if (this.onended) this.onended();
    }

    fail() {
      this.paused = true;
      if (this.onerror) this.onerror(new Error('missing audio'));
    }
  }

  const context = {
    Audio: FakeAudio,
    Map,
    Set,
    Number,
    Promise,
    console,
    document: {
      getElementById: getElement,
      querySelector(selector) {
        if (selector === '.chat-area') return getElement('chatArea');
        if (selector.includes('.tts-replay-btn')) return getElement(`button:${selector}`);
        return null;
      },
      querySelectorAll() { return []; },
    },
    fetch: fetchImpl || (async () => ({ ok: false, json: async () => ({ segments: [] }) })),
    showToast() {},
    window: { TheaterTTSQueue: queueApi },
  };
  vm.createContext(context);
  vm.runInContext(`
    const $ = id => document.getElementById(id);
    let ttsQueue = new Map();
    let ttsMergedUrls = new Map();
    let ttsPlaying = false;
    let currentAudio = null;
    let manualReplay = null;
    let activeTTS = null;
    let ttsPlaybackToken = 0;
    let ttsSuppressedMessages = new Set();
    let ttsDeletedMessages = new Set();
    let ttsDeletedConversations = new Set();
    let ttsMessageConversations = new Map();
    let ttsLiveMessages = new Set();
    let pendingManualReplayMsgId = null;
    let currentConvId = 'tc_test';
    let currentMessages = [];
  `, context);
  vm.runInContext(ttsBlock, context);
  return { context, FakeAudio };
}

test('playback controller consumes out-of-order arrivals exactly once in sequence', () => {
  const { context, FakeAudio } = createPlaybackContext();

  context.handleTTSChunk({ msg_id: 'tm_order', seq: 2, url: 'u2' });
  assert.equal(FakeAudio.instances.length, 0);
  context.handleTTSChunk({ msg_id: 'tm_order', seq: 0, url: 'u0' });
  context.handleTTSChunk({ msg_id: 'tm_order', seq: 1, url: 'u1' });
  assert.deepEqual(FakeAudio.instances.map((audio) => audio.url), ['u0']);

  FakeAudio.instances[0].finish();
  assert.deepEqual(FakeAudio.instances.map((audio) => audio.url), ['u0', 'u1']);
  FakeAudio.instances[1].finish();
  assert.deepEqual(FakeAudio.instances.map((audio) => audio.url), ['u0', 'u1', 'u2']);
  FakeAudio.instances[2].finish();
  context.handleTTSDone({ msg_id: 'tm_order' });
  assert.deepEqual(FakeAudio.instances.map((audio) => audio.url), ['u0', 'u1', 'u2']);
});

test('explicit stop suppresses late chunks until the live message is done', () => {
  const { context, FakeAudio } = createPlaybackContext();

  context.handleTTSChunk({ msg_id: 'tm_stop', seq: 0, url: 's0' });
  context.stopTTSPlayback();
  context.handleTTSChunk({ msg_id: 'tm_stop', seq: 1, url: 's1' });
  context.handleTTSDone({ msg_id: 'tm_stop' });

  assert.deepEqual(FakeAudio.instances.map((audio) => audio.url), ['s0']);
});

test('deletion tombstone ignores every late TTS event', () => {
  const { context, FakeAudio } = createPlaybackContext();

  context.discardMessageTTS('tm_deleted', true);
  context.handleTTSChunk({ msg_id: 'tm_deleted', seq: 0, url: 'd0' });
  context.handleTTSDone({ msg_id: 'tm_deleted' });
  context.handleTTSMerged({ msg_id: 'tm_deleted', url: 'merged-deleted' });

  assert.equal(FakeAudio.instances.length, 0);
});

test('manual replay preserves unrelated pending live queues', async () => {
  const { context } = createPlaybackContext();
  vm.runInContext(`
    const laterQueue = TTSQueue.createQueue();
    TTSQueue.upsert(laterQueue, 0, 'later0');
    ttsQueue.set('tm_later', laterQueue);
  `, context);

  await context.replayTTS('tm_replay');

  assert.equal(vm.runInContext(`ttsQueue.has('tm_later')`, context), true);
});

test('stop then replay keeps live ordering state until tts_done', async () => {
  const { context, FakeAudio } = createPlaybackContext();

  context.handleTTSChunk({ msg_id: 'tm_live', conv_id: 'tc_test', seq: 2, url: 'live2' });
  context.stopTTSPlayback();
  FakeAudio.failOnPlay.add('/api/theater/tts/audio/tm_live');
  await context.replayTTS('tm_live');
  await new Promise((resolve) => setImmediate(resolve));

  context.handleTTSChunk({ msg_id: 'tm_live', conv_id: 'tc_test', seq: 2, url: 'live2' });
  assert.equal(FakeAudio.instances.filter((audio) => audio.url === 'live2').length, 0);
  context.handleTTSChunk({ msg_id: 'tm_live', conv_id: 'tc_test', seq: 0, url: 'live0' });
  assert.equal(FakeAudio.instances.at(-1).url, 'live0');
});

test('replaying another message waits for active playback instead of restarting it', async () => {
  const { context, FakeAudio } = createPlaybackContext();

  context.handleTTSChunk({ msg_id: 'tm_active', conv_id: 'tc_test', seq: 0, url: 'active0' });
  context.handleTTSDone({ msg_id: 'tm_active', conv_id: 'tc_test' });
  await context.replayTTS('tm_replay_later');

  assert.deepEqual(FakeAudio.instances.map((audio) => audio.url), ['active0']);
  FakeAudio.instances[0].finish();
  await Promise.resolve();
  assert.equal(FakeAudio.instances.at(-1).url, '/api/theater/tts/audio/tm_replay_later');
});

test('clicking the active replay button stops only that message', async () => {
  const { context } = createPlaybackContext();

  context.handleTTSChunk({ msg_id: 'tm_active', conv_id: 'tc_test', seq: 0, url: 'active0' });
  context.handleTTSChunk({ msg_id: 'tm_later', conv_id: 'tc_test', seq: 2, url: 'later2' });
  await context.replayTTS('tm_active');

  assert.equal(vm.runInContext(`ttsQueue.has('tm_later')`, context), true);
  assert.equal(vm.runInContext(`ttsSuppressedMessages.has('tm_active')`, context), true);
  assert.equal(vm.runInContext(`ttsSuppressedMessages.has('tm_later')`, context), false);
});

test('deleting a conversation tombstones queued pre-persistence TTS', () => {
  const { context, FakeAudio } = createPlaybackContext();

  context.handleTTSChunk({ msg_id: 'tm_pending', conv_id: 'tc_deleted', seq: 2, url: 'pending2' });
  context.discardConversationTTS('tc_deleted');
  context.handleTTSChunk({ msg_id: 'tm_pending', conv_id: 'tc_deleted', seq: 0, url: 'pending0' });

  assert.equal(vm.runInContext(`ttsDeletedConversations.has('tc_deleted')`, context), true);
  assert.equal(vm.runInContext(`ttsQueue.has('tm_pending')`, context), false);
  assert.equal(FakeAudio.instances.length, 0);
});

test('tts_done during manifest lookup finishes the installed replay queue', async () => {
  let resolveManifest;
  const manifestPending = new Promise((resolve) => { resolveManifest = resolve; });
  const { context, FakeAudio } = createPlaybackContext({
    fetchImpl: () => manifestPending,
  });

  context.handleTTSChunk({ msg_id: 'tm_manifest_race', conv_id: 'tc_test', seq: 2, url: 'live2' });
  context.stopTTSPlayback();
  FakeAudio.failOnPlay.add('/api/theater/tts/audio/tm_manifest_race');
  await context.replayTTS('tm_manifest_race');
  context.handleTTSDone({ msg_id: 'tm_manifest_race', conv_id: 'tc_test' });
  resolveManifest({
    ok: true,
    json: async () => ({ segments: [{ seq: 0, url: 'manifest0' }] }),
  });
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(FakeAudio.instances.at(-1).url, 'manifest0');
  FakeAudio.instances.at(-1).finish();
  assert.equal(vm.runInContext(`activeTTS`, context), null);
});

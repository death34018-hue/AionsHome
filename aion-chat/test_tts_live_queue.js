const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

test('phone TTS settings reach the background service even without a page socket', () => {
  for (const [file, name, state] of [
    ['chat.js', '_sendTTSState', {ws: null, ttsEnabled: true, ttsVoiceId: 'voice', ttsPlaybackActiveAt: 20, _clientId: 'private'}],
    ['chatroom.js', 'crSendTTSState', {crWs: null, crTtsEnabled: true, crCurrentTTSVoice: () => 'voice', crTtsPlaybackActiveAt: 30, crAmbientClientId: 'room'}],
  ]) {
    const source = fs.readFileSync(path.join(__dirname, 'static', file), 'utf8');
    const start = source.indexOf(`function ${name}(`);
    const end = source.indexOf('\n}', start) + 2;
    const sent = [];
    const context = vm.createContext({ ...state, window: {
      AionTtsAudio: { setAutoPlaybackState: (...args) => sent.push(args) },
    }});
    vm.runInContext(source.slice(start, end), context);
    context[name]();
    assert.equal(sent.length, 1, file);
    assert.equal(sent[0][0], true);
    assert.equal(sent[0][1], 'voice');
  }
});

test('private TTS finishes a missing first segment and unblocks the next message', async () => {
  const source = fs.readFileSync(path.join(__dirname, 'static/chat.js'), 'utf8');
  const start = source.indexOf('function enqueueTTSChunk(');
  const end = source.indexOf('// 重听 TTS 音频', start);
  const played = [];
  const audio = { play() { played.push(this.src); return Promise.resolve(); } };
  const context = vm.createContext({
    ttsEnabled: true, ttsPlaying: false, ttsChunkQueues: {}, ttsPlayOrder: [],
    ttsAudio: audio, ttsManualStop: false, ttsSuppressedMsgIds: new Set(),
    ttsAcceptAfter: 0, _clientId: 'desktop', voiceInCall: false, window: {},
    shouldAcceptTTSMsg: () => true, clearTTSResumeTimer() {},
    _notifyVoiceCallPrivateTTSStart() {}, _notifyVoiceCallPrivateTTSEnd() {},
  });
  vm.runInContext(source.slice(start, end), context);
  context.enqueueTTSChunk('message', 1, '/segment1', 1, 'desktop');
  context.enqueueTTSChunk('next', 0, '/next', 1, 'desktop');
  assert.deepEqual(played, []);
  context.finishTTSForMsg('message', 2, 'desktop');
  await Promise.resolve();
  assert.deepEqual(played, ['/segment1']);
  audio.onended();
  await Promise.resolve();
  assert.deepEqual(played, ['/segment1', '/next']);
});

test('chatroom ignores another device copy without marking its segment as played', () => {
  const source = fs.readFileSync(path.join(__dirname, 'static/chatroom.js'), 'utf8');
  const start = source.indexOf('function crEnqueueTTSChunk(');
  const end = source.indexOf('async function crPlayNextTTSChunk(', start);
  const queued = [];
  const context = vm.createContext({
    crTtsEnabled: true, window: {}, crSeenTTSChunks: new Set(),
    crShouldAcceptTTSMsg: (_id, _time, target) => target === 'phone',
    _ttsEngine: { enqueue: (...args) => queued.push(args) },
  });
  vm.runInContext(source.slice(start, end), context);
  context.crEnqueueTTSChunk('message', 0, '/segment0', 1, 'desktop');
  context.crEnqueueTTSChunk('message', 0, '/segment0', 1, 'phone');
  context.crEnqueueTTSChunk('message', 0, '/segment0', 1, 'phone');
  assert.equal(queued.length, 1);
});

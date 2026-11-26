const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

test('remote TTS off stops queued speech and updates playback presence', () => {
  const source = fs.readFileSync(path.join(__dirname, 'static/chatroom.js'), 'utf8');
  const start = source.indexOf('function crApplySettingsSync(');
  const calls = [];
  const context = vm.createContext({
    crTtsEnabled: true, crTtsAionVoice: 'a', crTtsConnorVoice: 'c',
    crTtsAcceptAfter: 0, crTtsPlaybackActiveAt: 0,
    chatroomModel: 'a', chatroomConnorModel: 'c', chatroomReplyOrder: 'random', currentRoom: null,
    applyChatroomNames() {}, crApplyAmbientVoiceConfig() {}, crApplyRandomModelConfig() {},
    updateHeaderActions() {}, renderRoomList() {},
    document: {getElementById: () => ({classList: {contains: () => false}})},
    crStopTTS: () => calls.push('stop'), crSendTTSState: () => calls.push('sync'),
  });
  vm.runInContext(source.slice(start, source.indexOf('\n}', start) + 2), context);
  context.crApplySettingsSync({config: {tts_enabled: false}});
  assert.equal(context.crTtsEnabled, false);
  assert.deepEqual(calls, ['stop', 'sync']);
  assert.ok(context.crTtsAcceptAfter > 0, 'old buffered chunks must be rejected');
});

test('background Android pages yield new speech to the native service', () => {
  for (const [file, name] of [['chat.js', 'isTTSPlaybackAllowed'], ['chatroom.js', 'crTtsPlaybackAllowed']]) {
    const source = fs.readFileSync(path.join(__dirname, 'static', file), 'utf8');
    const start = source.indexOf(`function ${name}(`);
    const context = vm.createContext({window: {AionTtsAudio: {isForeground: () => false}}});
    vm.runInContext(source.slice(start, source.indexOf('\n}', start) + 2), context);
    assert.equal(context[name](), false, file);
    context.window.AionTtsAudio.isForeground = () => true;
    assert.equal(context[name](), true);
    context.window = {};
    assert.equal(context[name](), true, 'browser playback stays available');
  }
});

test('private and room messages retain speech ownership across synthesis gaps', async () => {
  for (const room of [false, true]) {
    const events = [];
    const audio = {
      beginSpeech() { if (!this.held) { this.held = true; events.push('begin'); } },
      endSpeech() { if (this.held) { this.held = false; events.push('end'); } },
      play() { events.push(this.src); return Promise.resolve(); },
      pause() {},
    };
    const context = vm.createContext({
      ttsEnabled: true, ttsPlaying: false, ttsChunkQueues: {}, ttsPlayOrder: [],
      ttsAudio: audio, ttsManualStop: false, ttsSuppressedMsgIds: new Set(),
      ttsAcceptAfter: 0, _clientId: 'phone', voiceInCall: false, window: {},
      shouldAcceptTTSMsg: () => true, clearTTSResumeTimer() {},
      _notifyVoiceCallPrivateTTSStart() {}, _notifyVoiceCallPrivateTTSEnd() {},
      crCreateHiddenAudio: () => audio, crGetParentAudio: () => null,
      crAmbientPauseForTts() {}, crAmbientResumeAfterTts() {},
      crNotifyVoiceCallTTSStart() {}, crNotifyVoiceCallTTSChunkEnd() {}, crNotifyVoiceCallTTSEnd() {},
    });
    const source = fs.readFileSync(path.join(__dirname, 'static', room ? 'chatroom.js' : 'chat.js'), 'utf8');
    const start = source.indexOf(room ? 'const _ttsEngine = (function()' : 'function enqueueTTSChunk(');
    const end = source.indexOf(room ? 'let crTtsAudio' : '// 重听 TTS 音频', start);
    vm.runInContext(source.slice(start, end), context);
    const enqueue = room
      ? (...args) => vm.runInContext('_ttsEngine', context).enqueue(...args)
      : (...args) => context.enqueueTTSChunk(...args);
    const finish = room
      ? id => vm.runInContext('_ttsEngine', context).finish(id)
      : id => context.finishTTSForMsg(id);
    enqueue('message', 0, '/message-0');
    enqueue('later', 0, '/later-0');
    audio.onended();
    assert.deepEqual(events, ['begin', '/message-0'], room ? 'room gap' : 'private gap');
    enqueue('message', 1, '/message-1');
    finish('message');
    assert.deepEqual(events, ['begin', '/message-0', '/message-1']);
    audio.onended();
    assert.deepEqual(events, ['begin', '/message-0', '/message-1', 'end', 'begin', '/later-0']);
    finish('later');
    audio.onended();
    assert.equal(events.at(-1), 'end');

    enqueue('interrupted', 0, '/interrupted');
    audio.onended();
    assert.equal(audio.held, true);
    if (room) vm.runInContext('_ttsEngine.connectionLost()', context);
    else context.finishPendingTTS();
    assert.equal(audio.held, false, 'a dropped socket must release an unfinished message');
  }
});

test('an abnormal reply suppresses late audio and stops its queued playback', () => {
  for (const [file, name, orderName, suppressName, stopName] of [
    ['chat.js', 'cancelTTSMessage', 'ttsPlayOrder', 'suppressTTSMsg', 'stopLiveTTSQueue'],
    ['chatroom.js', 'crCancelTTSMessage', '_ttsEngine', 'crSuppressTTSMsg', 'crStopTTS'],
  ]) {
    const source = fs.readFileSync(path.join(__dirname, 'static', file), 'utf8');
    const start = source.indexOf(`function ${name}(`);
    const end = source.indexOf('\n}', start) + 2;
    const suppressed = new Set();
    let stops = 0;
    const context = vm.createContext({
      [orderName]: orderName === '_ttsEngine' ? { playOrder: ['bad'] } : ['bad'],
      [suppressName]: id => suppressed.add(id),
      [stopName]: () => { stops++; },
    });
    vm.runInContext(source.slice(start, end), context);
    context[name]('unrelated');
    assert.equal(stops, 0);
    context[name]('bad');
    assert.equal(stops, 1);
    assert.ok(suppressed.has('bad'));
    const pending = ['good', 'bad'];
    context[orderName] = orderName === '_ttsEngine'
      ? { playOrder: pending, chunkQueues: { bad: {} } } : pending;
    context.ttsChunkQueues = { bad: {} };
    context[name]('bad');
    assert.equal(stops, 1, 'must not interrupt the other speaker');
    assert.deepEqual(pending, ['good']);
  }
});

test('phone TTS settings reach the background service even without a page socket', () => {
  for (const [file, name, state] of [
    ['chat.js', '_sendTTSState', {ws: null, ttsEnabled: true, ttsVoiceId: 'voice', ttsPlaybackActiveAt: 20, _clientId: 'private'}],
    ['chatroom.js', 'crSendTTSState', {currentRoom: {type: 'group'}, crWs: null, crTtsEnabled: true, crCurrentTTSVoice: () => 'voice', crTtsPlaybackActiveAt: 30, crAmbientClientId: 'room'}],
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

test('a late completion marker cannot re-enable cancelled message audio', () => {
  for (const [file, name, blocked, client, acceptAfter] of [
    ['chat.js', 'finishTTSForMsg', 'ttsSuppressedMsgIds', '_clientId', 'ttsAcceptAfter'],
    ['chatroom.js', 'crFinishTTSForMsg', 'crSuppressedTTSMsgIds', 'crAmbientClientId', 'crTtsAcceptAfter'],
  ]) {
    const source = fs.readFileSync(path.join(__dirname, 'static', file), 'utf8');
    const start = source.indexOf(`function ${name}(`);
    const suppressed = new Set(['bad']);
    const context = vm.createContext({ currentRoom: {type: 'group'}, [blocked]: suppressed, [client]: 'phone', [acceptAfter]: 0 });
    vm.runInContext(source.slice(start, source.indexOf('\n}', start) + 2), context);
    context[name]('bad', 1, 'phone');
    assert.ok(suppressed.has('bad'));
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
    currentRoom: {type: 'group'}, crTtsEnabled: true, window: {}, crSeenTTSChunks: new Set(),
    crShouldAcceptTTSMsg: (_id, _time, target) => target === 'phone',
    _ttsEngine: { enqueue: (...args) => queued.push(args) },
  });
  vm.runInContext(source.slice(start, end), context);
  context.crEnqueueTTSChunk('message', 0, '/segment0', 1, 'desktop');
  context.crEnqueueTTSChunk('message', 0, '/segment0', 1, 'phone');
  context.crEnqueueTTSChunk('message', 0, '/segment0', 1, 'phone');
  assert.equal(queued.length, 1);
  context.currentRoom.type = 'peer_dm';
  context.crEnqueueTTSChunk('other', 0, '/segment0', 2, 'phone');
  assert.equal(queued.length, 1);
});

test('expressive clip follows ordinary audio and works with full reading off', async () => {
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
  context.enqueueTTSChunk('m', 0, '/ordinary', 1, 'desktop');
  context.finishTTSForMsg('m', 2, 'desktop');
  context.enqueueTTSChunk('m_expressive', 0, '/expressive', 3, 'desktop', '台词', true, 'm');
  context.finishTTSForMsg('m_expressive', 3, 'desktop');
  assert.deepEqual(played, ['/ordinary']);
  audio.onended();
  await Promise.resolve();
  assert.deepEqual(played, ['/ordinary', '/expressive']);
  audio.onended();
  context.ttsEnabled = false;
  context.enqueueTTSChunk('normal-off', 0, '/must-not-play', 4, 'desktop');
  context.enqueueTTSChunk('voice-only', 0, '/voice-only', 4, 'desktop', '台词', true);
  assert.deepEqual(played, ['/ordinary', '/expressive', '/voice-only']);
});

test('expressive buttons toggle off and switch immediately on both chat surfaces', () => {
  for (const room of [false, true]) {
    const audio = {src: ''};
    const queued = [];
    let stops = 0;
    const context = vm.createContext({window: {}, ttsAudio: audio, ttsPlaying: false,
      _ttsEngine: {audio, playing: false}, _clientId: 'private', crAmbientClientId: 'room',
      finishTTSForMsg() {}, crFinishTTSForMsg() {},
    });
    const stop = () => { stops++; audio.src = ''; context.ttsPlaying = false; context._ttsEngine.playing = false; };
    const enqueue = (id, seq, url) => { queued.push({id, url}); audio.src = 'http://localhost' + url; context.ttsPlaying = true; context._ttsEngine.playing = true; };
    Object.assign(context, {stopLiveTTSQueue: stop, crStopTTS: stop, enqueueTTSChunk: enqueue, crEnqueueTTSChunk: enqueue});
    vm.runInContext(fs.readFileSync(path.join(__dirname, 'static/expressive-voice.js'), 'utf8'), context);
    const source = fs.readFileSync(path.join(__dirname, 'static', room ? 'chatroom.js' : 'chat.js'), 'utf8');
    vm.runInContext(source.slice(source.lastIndexOf('let expressiveReplaySequence = 0;')), context);
    const first = {dataset: {voiceUrl: '/api/tts/audio/first_expressive'}};
    const second = {dataset: {voiceUrl: '/api/tts/audio/second_expressive'}};
    const voice = context.window.ExpressiveVoice;
    voice.play(first);
    voice.play(first);
    assert.equal(queued.length, 1);
    assert.equal(audio.src, '');
    voice.play(first);
    voice.play(second);
    assert.equal(queued.length, 3);
    assert.equal(audio.src, 'http://localhost/api/tts/audio/second_expressive');
    assert.equal(stops, 4);
    assert.equal(new Set(queued.map(q => q.id)).size, 3);
    context.ttsPlaying = false; context._ttsEngine.playing = false;
    voice.play(second);
    assert.equal(queued.length, 4, 'completed audio can be replayed');
  }
});

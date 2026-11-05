const assert = require('assert');

let installAionTtsAudio;
try {
  ({ installAionTtsAudio } = require('./static/native-tts-audio.js'));
} catch (_) {
  // The first TDD run intentionally reaches this branch until the adapter exists.
}

assert.strictEqual(
  typeof installAionTtsAudio,
  'function',
  'native TTS adapter should export its browser installer',
);

function createRoot(nativeBridge = null) {
  class FakeHtmlAudio {
    constructor() {
      this.kind = 'html';
    }
  }
  return {
    AionTtsAudio: nativeBridge,
    Audio: FakeHtmlAudio,
  };
}

function testNativePlaybackAndEvents() {
  const calls = [];
  const bridge = {
    play(playerId, url) {
      calls.push(['play', playerId, url]);
      return true;
    },
    stop(playerId) {
      calls.push(['stop', playerId]);
    },
  };
  const root = createRoot(bridge);
  installAionTtsAudio(root);

  const audio = root.createAionTtsAudio();
  const events = [];
  audio.onplaying = () => events.push('playing');
  audio.onended = () => events.push('ended');
  audio.onerror = () => events.push('error');
  audio.src = '/api/tts/audio/message_s0';

  return audio.play().then(() => {
    assert.deepStrictEqual(calls[0], ['play', audio.playerId, '/api/tts/audio/message_s0']);
    assert.strictEqual(audio.paused, false);

    root.onAionNativeTtsEvent({ playerId: audio.playerId, type: 'playing' });
    assert.deepStrictEqual(events, ['playing']);
    root.onAionNativeTtsEvent({ playerId: audio.playerId, type: 'ended' });
    assert.deepStrictEqual(events, ['playing', 'ended']);
    assert.strictEqual(audio.ended, true);
    assert.strictEqual(audio.paused, true);
  });
}

function testNativeStopIsScopedToPlayer() {
  const calls = [];
  const root = createRoot({
    play() { return true; },
    stop(playerId) { calls.push(playerId); },
  });
  installAionTtsAudio(root);
  const first = root.createAionTtsAudio();
  const second = root.createAionTtsAudio();
  first.src = '/first.mp3';
  second.src = '/second.mp3';

  return Promise.all([first.play(), second.play()]).then(() => {
    first.pause();
    assert.deepStrictEqual(calls, [first.playerId]);
    assert.strictEqual(first.paused, true);
    assert.strictEqual(second.paused, false);
  });
}

function testBrowserFallback() {
  const root = createRoot();
  installAionTtsAudio(root);
  assert.strictEqual(root.createAionTtsAudio().kind, 'html');
}

async function testMessageOwnershipSurvivesChunkCompletionAndStopsInAGap() {
  const calls = [];
  const listeners = {};
  const root = createRoot({
    play() { return true; }, stop(id) { calls.push(['stop', id]); },
    beginSpeech(id) { calls.push(['begin', id]); },
    endSpeech(id) { calls.push(['end', id]); },
  });
  root.addEventListener = (name, callback) => { listeners[name] = callback; };
  installAionTtsAudio(root);
  const audio = root.createAionTtsAudio();
  audio.beginSpeech();
  audio.src = '/first';
  await audio.play();
  root.onAionNativeTtsEvent({ playerId: audio.playerId, type: 'ended' });
  assert.deepStrictEqual(calls, [['begin', audio.playerId]]);
  audio.beginSpeech();
  audio.pause(); // Stopping between chunks still has to release the message's turn.
  audio.endSpeech();
  assert.deepStrictEqual(calls, [['begin', audio.playerId], ['end', audio.playerId]]);
  audio.beginSpeech();
  audio.src = '/last';
  await audio.play();
  listeners.pagehide();
  assert.deepStrictEqual(calls.slice(-2), [['begin', audio.playerId], ['end', audio.playerId]]);
  assert.equal(audio.paused, false, 'navigation lets the current native clip finish');
}

function testNativeEventsReachEmbeddedChatroom() {
  const bridge = { play() { return true; }, stop() {} };
  const child = createRoot(bridge);
  const parent = createRoot(bridge);
  parent.document = {
    querySelectorAll() {
      return [{ contentWindow: child }];
    },
  };
  installAionTtsAudio(parent);
  installAionTtsAudio(child);
  const parentAudio = parent.createAionTtsAudio();
  const childAudio = child.createAionTtsAudio();
  let childPlaying = false;
  childAudio.onplaying = () => { childPlaying = true; };

  assert.notStrictEqual(parentAudio.playerId, childAudio.playerId);
  parent.onAionNativeTtsEvent({ playerId: childAudio.playerId, type: 'playing' });
  assert.strictEqual(childPlaying, true);
}

async function testTheaterPlaybackControls() {
  const calls = [];
  const bridge = {
    play() { return true; }, stop(id) { calls.push(['stop', id]); },
    prepareAudio(id, url) { calls.push(['prepare', id, url]); return true; },
    pauseAudio(id) { calls.push(['pause', id]); },
    resumeAudio(id) { calls.push(['resume', id]); },
    seekAudio(id, seconds) { calls.push(['seek', id, seconds]); },
  };
  const root = createRoot(bridge);
  installAionTtsAudio(root);
  const audio = root.createTtsAudio('/chapter.mp3');
  let updates = 0, ended = 0;
  audio.onloadedmetadata = () => { audio.currentTime = 12; };
  audio.ontimeupdate = () => updates++;
  audio.onended = () => ended++;
  audio.load();
  await audio.play();
  assert.deepStrictEqual(calls, [['prepare', audio.playerId, '/chapter.mp3']]);
  root.onAionNativeTtsEvent({playerId: audio.playerId, type: 'loadedmetadata', duration: 60, currentTime: 0});
  assert.deepStrictEqual(calls.slice(-2), [['seek', audio.playerId, 12], ['resume', audio.playerId]]);
  assert.equal(audio.duration, 60);
  root.onAionNativeTtsEvent({playerId: audio.playerId, type: 'timeupdate', currentTime: 18});
  assert.equal(audio.currentTime, 18);
  assert.equal(updates, 1);
  audio.pause();
  await audio.play();
  assert.deepStrictEqual(calls.slice(-2), [['pause', audio.playerId], ['resume', audio.playerId]]);
  assert.equal(calls.filter(c => c[0] === 'prepare').length, 1, 'resume must not restart the chapter');
  root.onAionNativeTtsEvent({playerId: audio.playerId, type: 'ended', currentTime: 60});
  assert.equal(ended, 1);
  assert.equal(audio.currentTime, 60);
  audio.src = '';
  root.onAionNativeTtsEvent({playerId: audio.playerId, type: 'ended'});
  assert.equal(ended, 1, 'released chapter must ignore late events');
}

async function testTheaterPauseDuringLoadingAndFallback() {
  const calls = [];
  const root = createRoot({
    prepareAudio() { return true; }, pauseAudio() {}, seekAudio() {},
    resumeAudio() { calls.push('resume'); }, stop() { calls.push('stop'); },
  });
  installAionTtsAudio(root);
  const audio = root.createTtsAudio('/chapter.mp3');
  await audio.play();
  audio.pause();
  root.onAionNativeTtsEvent({playerId: audio.playerId, type: 'loadedmetadata', duration: 10});
  assert.deepStrictEqual(calls, [], 'paused loading must not start speaking');
  await audio.play();
  assert.deepStrictEqual(calls, ['resume']);
  audio.src = '';
  assert.deepStrictEqual(calls, ['resume', 'stop']);
  const oldApp = createRoot({play() {}, stop() {}});
  installAionTtsAudio(oldApp);
  assert.equal(oldApp.createTtsAudio('/chapter.mp3').kind, 'html');
}

async function testReplayPlaylistSurvivesReloadAndOfflinePlayback() {
  const saved = new Map();
  const storage = { getItem: key => saved.get(key), setItem: (key, value) => saved.set(key, value) };
  const root = createRoot();
  root.localStorage = storage;
  root.fetch = async () => ({ ok: true, json: async () => ({ urls: ['/api/tts/audio/m_s0', '/api/tts/audio/m_s1'] }) });
  installAionTtsAudio(root);
  const first = await root.AionTtsReplayPlaylist.load('m');
  const reopened = createRoot();
  reopened.localStorage = storage;
  reopened.fetch = async () => { throw new Error('offline'); };
  installAionTtsAudio(reopened);
  assert.deepStrictEqual(await reopened.AionTtsReplayPlaylist.load('m'), first);
  assert.equal(first.length, 2, 'cached replay includes later clips, not only s0');
}

async function testPrefetchOnlyIncludesUpcomingClips() {
  const urls = [];
  const root = createRoot({ play() { return true; }, stop() {}, prefetch: url => urls.push(url) });
  installAionTtsAudio(root);
  root.createAionTtsAudio().prefetchQueued({ m: { nextPlay: 1, chunks: {
    0: { url: 'played' }, 1: { url: 'current' }, 2: { url: 'next' }, 3: { url: 'later' }, 4: { url: 'too-far' },
  } } }, ['m']);
  assert.deepStrictEqual(urls, ['current', 'next', 'later']);
}

Promise.resolve()
  .then(testNativePlaybackAndEvents)
  .then(testNativeStopIsScopedToPlayer)
  .then(testBrowserFallback)
  .then(testMessageOwnershipSurvivesChunkCompletionAndStopsInAGap)
  .then(testNativeEventsReachEmbeddedChatroom)
  .then(testTheaterPlaybackControls)
  .then(testTheaterPauseDuringLoadingAndFallback)
  .then(testReplayPlaylistSurvivesReloadAndOfflinePlayback)
  .then(testPrefetchOnlyIncludesUpcomingClips)
  .then(() => console.log('native TTS audio adapter tests passed'))
  .catch(error => {
    console.error(error);
    process.exitCode = 1;
  });

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

Promise.resolve()
  .then(testNativePlaybackAndEvents)
  .then(testNativeStopIsScopedToPlayer)
  .then(testBrowserFallback)
  .then(testNativeEventsReachEmbeddedChatroom)
  .then(() => console.log('native TTS audio adapter tests passed'))
  .catch(error => {
    console.error(error);
    process.exitCode = 1;
  });

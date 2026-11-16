const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createTimeline, createSpeechSegments } = require('./static/ambient-timeline.js');

test('five minute timeline merges continuous sounds and keeps capture times', () => {
  let now = 1000000;
  const log = createTimeline(() => now);
  log.add({ kind: 'cough', start: now - 299000, end: now - 298000 });
  log.add({ kind: 'humming', start: now - 5000, end: now - 4000 });
  log.add({ kind: 'humming', start: now - 4000, end: now - 3000 });
  log.add({ kind: 'speech', start: now - 2000, end: now - 1000, text: '盐放多了' });
  assert.equal(log.events().length, 3);
  assert.equal(log.events()[1].end - log.events()[1].start, 2000);
  assert.match(log.text(), /说：盐放多了/);
  now += 4000;
  assert.equal(log.events().length, 2);
  assert.equal(log.events()[1].start, 998000);
});

test('late speech is ordered by capture time; separate humming bursts stay separate', () => {
  const log = createTimeline(() => 1000000);
  log.add({ kind: 'humming', start: 990000, end: 991000 });
  log.add({ kind: 'humming', start: 995000, end: 996000 });
  log.add({ kind: 'speech', start: 992000, end: 994000, text: '刚才说的话' });
  assert.deepEqual(log.events().map(e => e.kind), ['humming', 'speech', 'humming']);
});

test('only speech reaches ASR; reset discards unfinished speech during TTS', () => {
  const clips = [];
  const segments = createSpeechSegments(clip => clips.push(clip));
  const frame = (kind, start) => ({ kind, start, end: start + 975, pcm: kind });
  segments.push(frame('humming', 0));
  segments.push(frame('cough', 975));
  assert.equal(clips.length, 0);
  segments.push(frame('speech', 1950));
  segments.push(frame('speech', 2925));
  segments.push(frame('background', 3900));
  assert.equal(clips.length, 1);
  assert.equal(clips[0].start, 1950);
  assert.ok(!clips[0].chunks.includes('humming'));
  segments.push(frame('speech', 4875));
  segments.reset();
  segments.push(frame('background', 5850));
  assert.equal(clips.length, 1);
});

const fs = require('node:fs');
const vm = require('node:vm');
function ambientPage() {
  let now = 1000000;
  const calls = [], timers = [];
  const source = fs.readFileSync(`${__dirname}/static/chatroom.js`, 'utf8').replace(/\r\n/g, '\n');
  const storage = { getItem: () => null, setItem() {} };
  const context = vm.createContext({
    Date: class extends Date { static now() { return now; } }, console, Promise, AbortController,
    window: { AionLocalVoice: { cancelTranscriptions() {} } }, sessionStorage: storage, localStorage: storage,
    CR_AMBIENT_LOCAL_ARMED_KEY: 'test', crAmbientClientId: 'test',
    currentRoom: { id: 'group', type: 'group' }, _ttsEngine: { playing: false, playOrder: [] },
    AionAmbientTimeline: { createTimeline, createSpeechSegments },
    document: { getElementById: () => null }, _getNativeBridge: () => null,
    crAmbientSetStatus() {}, crAmbientUpdateToggleView() {}, toast() {},
    setTimeout: fn => { timers.push(fn); return timers.length; }, clearTimeout() {}, clearInterval() {},
    crAmbientEvaluate: async (text, forced) => calls.push({ text, forced }),
    crAmbientSyncRunning: async () => calls.push('resume'),
    crAmbientIsCapturing: () => true,
  });
  const run = code => vm.runInContext(code, context);
  run(source.slice(source.indexOf('let crAmbientVoiceEnabled'), source.indexOf('let crAmbientResumeRetryTimer') + 'let crAmbientResumeRetryTimer = null;'.length));
  function load(name) {
    const declaration = source.includes(`async function ${name}(`) ? `async function ${name}(` : `function ${name}(`;
    const start = source.indexOf(declaration);
    assert.ok(start >= 0, name);
    run(source.slice(start, source.indexOf('\n}\n', start) + 3));
  }
  for (const name of ['crAmbientDesired', 'crAmbientCanRun', 'crAmbientTtsIsPlaying', 'crAmbientSoundNativeBridge',
    'crAmbientGetTimeline', 'crAmbientRecordEvent', 'crAmbientBufferText', 'crAmbientUpdateBufferView',
    'crAmbientMaybeEvaluate', 'crAmbientHandleTranscript', 'crAmbientTextHasPhrase', 'crAmbientTextHasWakeWord',
    'crAmbientTextHasStopWord', 'crAmbientRemoveWakeWord', 'crAmbientPauseForTts', 'crAmbientResumeAfterTts',
    'crAmbientStop', 'crAmbientHandleBlob']) {
    load(name);
  }
  run("crAmbientVoiceEnabled=true; crAmbientLocalArmed=true; crAmbientOwned=true; crAmbientWakeWord='老公'; crAmbientIntervalSec=20; crAmbientGetTimeline()");
  return { run, load, context, calls, timers, advance: ms => { now += ms; } };
}

test('sound-only events reach the sentinel on interval, wake word bypass stays intact', async () => {
  const { run, calls, advance } = ambientPage();
  advance(1000);
  run("crAmbientRecordEvent({kind:'humming',start:Date.now(),end:Date.now()})");
  await run('crAmbientMaybeEvaluate()');
  assert.equal(calls.length, 0);
  advance(20000);
  await run('crAmbientMaybeEvaluate()');
  assert.deepEqual(calls, [{ text: '', forced: false }]);
  await run("crAmbientHandleTranscript('老公，盐放多了怎么办')");
  assert.equal(calls[1].forced, true);
  assert.match(calls[1].text, /盐放多了/);
});

test('TTS preserves history, discards late ASR and waits through playback gaps', async () => {
  const { run, context, timers, calls } = ambientPage();
  run("crAmbientRecordEvent({kind:'cough',start:999000,end:999500})");
  let finish;
  context.clip = { size: 1000 };
  context.crAmbientTranscribe = () => new Promise(resolve => { finish = resolve; });
  const pending = run('crAmbientHandleBlob(clip, 1, 999000, 1000000)');
  run('crAmbientPauseForTts()');
  finish('这是扬声器回声');
  await pending;
  assert.equal(run('crAmbientGetTimeline().events().length'), 1);
  run("_ttsEngine.playOrder=['unfinished-message']; crAmbientResumeAfterTts()");
  assert.equal(timers.length, 0);
  run('_ttsEngine.playOrder=[]; crReplayActive=true; crAmbientResumeAfterTts()');
  assert.equal(timers.length, 0);
  run('crReplayActive=false');
  run('_ttsEngine.playOrder=[]; crAmbientResumeAfterTts()');
  assert.equal(timers.length, 1);
  // A new message starts while the tail-delay timer is pending.
  run('_ttsEngine.playing=true');
  await timers[0]();
  assert.equal(calls.length, 0);
  assert.equal(run('crAmbientPausedForTts'), true);
  run('_ttsEngine.playing=false; crAmbientResumeAfterTts()');
  await timers[1]();
  assert.deepEqual(calls, ['resume']);
});

test('stopping cloud transcription aborts the request without a retry', async () => {
  const source = fs.readFileSync(`${__dirname}/static/chatroom.js`, 'utf8').replace(/\r\n/g, '\n');
  const start = source.indexOf('async function crAmbientTranscribe(');
  let calls = 0, signal;
  const context = vm.createContext({ console, FormData, window: {},
    fetch: (_, options) => {
      calls++;
      signal = options.signal;
      return new Promise((resolve, reject) => signal?.addEventListener('abort', () => reject(signal.reason)));
    }
  });
  vm.runInContext(source.slice(start, source.indexOf('\nfunction crAmbientTextHasPhrase', start)), context);
  context.clip = new Blob(['audio'], { type: 'audio/wav' });
  context.controller = new AbortController();
  const pending = vm.runInContext('crAmbientTranscribe(clip, controller.signal)', context);
  assert.ok(signal, 'ASR fetch must receive the cancellation signal');
  context.controller.abort();
  await assert.rejects(pending, /abort/i);
  assert.equal(calls, 1);
});

test('a wake word heard while the sentinel is pending is dispatched afterwards', async () => {
  const { run, load, context } = ambientPage();
  const requests = [];
  let resolveFirst;
  Object.assign(context, { API: '/api/chatroom', chatroomModel: 'a', chatroomConnorModel: 'b',
    crTtsEnabled: false, crTtsAionVoice: '', crTtsConnorVoice: '',
    crAmbientSourceInfo: () => ({ source: 'test', label: 'test' }),
    fetch: async (_, options) => {
      requests.push(JSON.parse(options.body));
      if (requests.length === 1) await new Promise(resolve => { resolveFirst = resolve; });
      return { json: async () => ({ triggered: false }) };
    }
  });
  load('crAmbientEvaluate');
  const ordinary = run("crAmbientEvaluate('', false)");
  await run("crAmbientHandleTranscript('老公，盐放多了怎么办')");
  resolveFirst();
  await ordinary;
  assert.deepEqual(requests.map(r => r.forced), [false, true]);
  assert.match(requests[1].transcript, /盐放多了/);
});

test('a transcript finishing after switching rooms does not enter the new timeline', async () => {
  const { run, context } = ambientPage();
  let finish;
  context.clip = { size: 1000 };
  context.crAmbientTranscribe = () => new Promise(resolve => { finish = resolve; });
  const pending = run('crAmbientHandleBlob(clip, 1, 999000, 1000000)');
  run("currentRoom={id:'other',type:'group'}");
  finish('之前房间的话');
  await pending;
  assert.equal(run('crAmbientGetTimeline().events().length'), 0);
});

test('standalone fillers do not enter history or consume a wake; meaningful speech stays', async () => {
  const { run, calls, advance } = ambientPage();
  for (const text of ['嗯', '哦。', '嗯，哦……', '呃…啊？']) {
    await run(`crAmbientHandleTranscript(${JSON.stringify(text)})`);
  }
  advance(21000);
  await run('crAmbientMaybeEvaluate()');
  assert.equal(run('crAmbientGetTimeline().events().length'), 0);
  assert.equal(calls.length, 0);
  await run("crAmbientHandleTranscript('老公')");
  const wakeUntil = run('crAmbientWakePendingUntil');
  await run("crAmbientHandleTranscript('嗯嗯')");
  assert.equal(run('crAmbientWakePendingUntil'), wakeUntil);
  await run("crAmbientHandleTranscript('嗯，盐放多了')");
  assert.equal(calls[0].forced, true);
  assert.equal(run('crAmbientGetTimeline().events().at(-1).text'), '嗯，盐放多了');
  // Even a filler can be an explicitly configured control word.
  run("crAmbientWakeWord='嗯'");
  await run("crAmbientHandleTranscript('嗯')");
  assert.ok(run('crAmbientWakePendingUntil') > 0);
});

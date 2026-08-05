'use strict';

const assert = require('node:assert/strict');
const ui = require('./static/english-corner.js');
let failures = 0;
let testCount = 0;

function test(name, fn) {
  testCount += 1;
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (error) {
    failures += 1;
    console.error(`not ok - ${name}`);
    console.error(error.stack || error);
  }
}

test('context choices use ten-message steps and all when total is zero', () => {
  assert.deepEqual(
    ui.buildContextOptions(0),
    { options: [0], defaultValue: 0 },
  );
});

test('context choices include the actual total and default to all below fifty', () => {
  assert.deepEqual(
    ui.buildContextOptions(34),
    { options: [10, 20, 30, 34], defaultValue: 34 },
  );
});

test('context choices default to fifty for larger timelines', () => {
  assert.deepEqual(
    ui.buildContextOptions(86),
    { options: [10, 20, 30, 40, 50, 60, 70, 80, 86], defaultValue: 50 },
  );
});

test('learning-card navigation wraps in both directions', () => {
  assert.equal(ui.nextLearningIndex(2, 3, 1), 0);
  assert.equal(ui.nextLearningIndex(0, 3, -1), 2);
  assert.equal(ui.nextLearningIndex(1, 3, 4), 2);
  assert.equal(ui.nextLearningIndex(4, 0, 1), 0);
});

test('centered learning header includes global progress and pack date', () => {
  const header = ui.learningHeaderPresentation(
    { pack: { created_at: 1721952000 } },
    1,
    5,
  );
  assert.equal(header.progress, '2 / 5');
  assert.match(header.packInfo, /^学习包 · /);
  assert.notEqual(header.packInfo, '学习包 · 收在学习角');
  assert.deepEqual(
    ui.learningHeaderPresentation(null, 0, 0),
    { progress: '等待新一幕', packInfo: '' },
  );
});

test('speaker groups contain every unique participant in utterance order', () => {
  const card = {
    utterances: [
      { speaker: 'user' },
      { speaker: 'aion' },
      { speaker: 'aion' },
      { speaker: 'connor' },
      { speaker: 'user' },
      { speaker: 'aion' },
    ],
  };
  assert.deepEqual(ui.cardSpeakerGroups(card), ['user', 'aion', 'connor']);
});

test('configured participant names render with only safe same-origin avatars', () => {
  const overview = {
    participants: [
      {
        id: 'user',
        name: 'Configured User',
        avatar_url: '/public/UserIcon.png',
      },
      {
        id: 'aion',
        name: 'Configured Main',
        avatar_url: 'https://evil.example/stolen.png',
      },
      {
        id: 'connor',
        name: 'Configured Second',
        avatar_url: '/public/codexicon.png',
      },
    ],
  };

  assert.deepEqual(
    ui.participantMeta(overview, 'user', 'https://home.example'),
    {
      id: 'user',
      name: 'Configured User',
      avatar: '/public/UserIcon.png',
    },
  );
  assert.deepEqual(
    ui.participantMeta(overview, 'aion', 'https://home.example'),
    {
      id: 'aion',
      name: 'Configured Main',
      avatar: '/public/gropicon1.png',
    },
  );
  assert.equal(
    ui.participantMeta(
      overview,
      'connor',
      'https://home.example',
    ).name,
    'Configured Second',
  );
});

test('generation choices stay limited to the two configured AI actors', () => {
  const overview = {
    participants: [
      { id: 'user', name: 'Configured User' },
      { id: 'aion', name: 'Configured Main' },
      { id: 'connor', name: 'Configured Second' },
    ],
    actors: [
      { id: 'user', name: 'must be filtered' },
      { id: 'connor', name: 'Configured Second' },
      { id: 'aion', name: 'Configured Main' },
    ],
  };

  assert.deepEqual(
    ui.generationActorOptions(overview).map((actor) => actor.id),
    ['connor', 'aion'],
  );
});

test('ready public audio maps only to a safe same-origin path', () => {
  assert.equal(
    ui.publicAudioUrl(
      { status: 'ready', url: '/api/english-corner/audio/17' },
      'https://home.example',
    ),
    'https://home.example/api/english-corner/audio/17',
  );
  assert.equal(
    ui.publicAudioUrl(
      { status: 'ready', url: 'https://home.example/api/english-corner/audio/18' },
      'https://home.example',
    ),
    'https://home.example/api/english-corner/audio/18',
  );
  assert.equal(
    ui.publicAudioUrl(
      { status: 'ready', url: 'https://other.example/audio.mp3' },
      'https://home.example',
    ),
    '',
  );
  assert.equal(
    ui.publicAudioUrl(
      { status: 'failed', url: '/api/english-corner/audio/19' },
      'https://home.example',
    ),
    '',
  );
});

test('translation reducer toggles only the selected utterance', () => {
  const initial = {
    expandedTranslations: { 10: true, 11: true },
    learning: [],
    learned: [],
    undo: null,
  };
  const next = ui.reduceUIState(initial, {
    type: 'toggle-translation',
    utteranceId: 10,
  });
  assert.deepEqual(next.expandedTranslations, { 10: false, 11: true });
  assert.deepEqual(initial.expandedTranslations, { 10: true, 11: true });
});

test('learn and undo reducer restores the card to its carousel position', () => {
  const first = { id: 1, title: 'First' };
  const second = { id: 2, title: 'Second' };
  const learned = ui.reduceUIState(
    {
      expandedTranslations: {},
      learning: [first, second],
      learned: [],
      undo: null,
    },
    { type: 'learn-card', cardId: 1 },
  );
  assert.deepEqual(learned.learning.map((card) => card.id), [2]);
  assert.deepEqual(learned.learned.map((card) => card.id), [1]);
  assert.equal(learned.undo.index, 0);

  const undone = ui.reduceUIState(learned, { type: 'undo-learn' });
  assert.deepEqual(undone.learning.map((card) => card.id), [1, 2]);
  assert.deepEqual(undone.learned, []);
  assert.equal(undone.undo, null);
});

test('relearn reducer returns a learned card to the learning carousel', () => {
  const card = { id: 7, title: 'Again', status: 'learned' };
  const next = ui.reduceUIState(
    {
      expandedTranslations: {},
      learning: [],
      learned: [card],
      undo: null,
    },
    { type: 'relearn-card', cardId: 7 },
  );
  assert.equal(next.learning[0].id, 7);
  assert.equal(next.learning[0].status, 'learning');
  assert.deepEqual(next.learned, []);
});

test('audio retry reducer updates only the failed utterance', () => {
  const card = {
    id: 1,
    utterances: [
      { id: 21, audio: { status: 'failed', message: 'failed' } },
      {
        id: 22,
        audio: {
          status: 'ready',
          url: '/api/english-corner/audio/22',
        },
      },
    ],
  };
  const initial = {
    expandedTranslations: {},
    learning: [card],
    learned: [],
    undo: null,
  };
  const next = ui.reduceUIState(initial, {
    type: 'audio-retrying',
    utteranceId: 21,
  });

  assert.equal(next.learning[0].utterances[0].audio.status, 'retrying');
  assert.equal(next.learning[0].utterances[1].audio.status, 'ready');
  assert.equal(initial.learning[0].utterances[0].audio.status, 'failed');

  const ready = ui.reduceUIState(next, {
    type: 'audio-updated',
    utteranceId: 21,
    audio: {
      id: 4,
      utterance_id: 21,
      status: 'ready',
      url: '/api/english-corner/audio/21',
    },
  });
  assert.equal(ready.learning[0].utterances[0].audio.status, 'ready');
  assert.equal(
    ready.learning[0].utterances[0].audio.url,
    '/api/english-corner/audio/21',
  );
});

test('archive review maps saved audio for AI and user turns', () => {
  const rows = ui.buildArchiveReviewRows(
    {
      utterances: [
        {
          id: 31,
          speaker: 'aion',
          english: 'Ready.',
          audio: {
            status: 'ready',
            url: '/api/english-corner/audio/31',
          },
        },
        {
          id: 32,
          speaker: 'user',
          english: 'I will read this.',
          audio: {
            status: 'ready',
            url: '/api/english-corner/audio/32',
          },
        },
        {
          id: 33,
          speaker: 'connor',
          english: 'Retry me.',
          audio: { status: 'failed', message: 'failed' },
        },
        {
          id: 34,
          speaker: 'aion',
          english: 'My row went missing.',
          audio: null,
        },
      ],
    },
    'https://home.example',
  );

  assert.deepEqual(rows.map((row) => row.audioAction), [
    {
      type: 'play',
      utteranceId: 31,
      url: 'https://home.example/api/english-corner/audio/31',
    },
    {
      type: 'play',
      utteranceId: 32,
      url: 'https://home.example/api/english-corner/audio/32',
    },
    { type: 'retry', utteranceId: 33 },
    { type: 'retry', utteranceId: 34 },
  ]);
});

test('mixed speaker audio keeps ready playback and targets only recovery rows', () => {
  const actions = ui.speakerAudioActions(
    {
      utterances: [
        {
          id: 41,
          speaker: 'aion',
          audio: {
            status: 'ready',
            url: '/api/english-corner/audio/41',
          },
        },
        {
          id: 42,
          speaker: 'aion',
          audio: { status: 'failed' },
        },
        {
          id: 43,
          speaker: 'aion',
          audio: null,
        },
        {
          id: 44,
          speaker: 'connor',
          audio: null,
        },
      ],
    },
    'aion',
    'https://home.example',
  );

  assert.deepEqual(actions, [
    {
      type: 'play',
      items: [{
        utteranceId: 41,
        url: 'https://home.example/api/english-corner/audio/41',
      }],
    },
    { type: 'retry', utteranceId: 42 },
    { type: 'retry', utteranceId: 43 },
  ]);
});

test('voice options deduplicate URIs and prefer the remembered selection', () => {
  assert.deepEqual(
    ui.normalizeVoiceOptions(
      {
        voices: [
          {
            uri: 'speech:custom:first',
            customName: 'First voice',
          },
          {
            uri: 'speech:custom:remembered',
            customName: 'English voice',
          },
          {
            uri: 'speech:custom:first',
            customName: 'Duplicate',
          },
          { uri: '   ', customName: 'Invalid' },
        ],
      },
      'speech:custom:remembered',
    ),
    {
      options: [
        {
          uri: 'speech:custom:first',
          label: 'First voice',
        },
        {
          uri: 'speech:custom:remembered',
          label: 'English voice',
        },
      ],
      selected: 'speech:custom:remembered',
    },
  );
  assert.equal(
    ui.normalizeVoiceOptions(
      { voices: [{ uri: 'speech:custom:first' }] },
      'speech:custom:missing',
    ).selected,
    'speech:custom:first',
  );
});

test('a retry response that remains failed never announces success', () => {
  const failed = ui.audioRetryPresentation({
    status: 'failed',
    message: 'Audio generation failed; retry is available.',
  });
  const ready = ui.audioRetryPresentation({
    status: 'ready',
    url: '/api/english-corner/audio/9',
  });

  assert.equal(failed.success, false);
  assert.match(failed.message, /重试/);
  assert.deepEqual(ready, {
    success: true,
    message: '这一句的音频已经重新准备好。',
  });
});

test('generation completion stays in the sheet when authoritative refresh fails', () => {
  assert.deepEqual(
    ui.generationRefreshPresentation({
      ok: false,
      error: '服务器刷新失败',
    }),
    {
      closeSheet: false,
      showSuccess: false,
      error: '学习包已生成，但服务器刷新失败。请重试同步。',
    },
  );
  assert.deepEqual(
    ui.generationRefreshPresentation({ ok: true }),
    { closeSheet: true, showSuccess: true, error: '' },
  );
});

test('generation success notice replaces older timers and hides automatically', () => {
  const visibility = [];
  const scheduled = [];
  const cancelled = [];
  const controller = ui.createTransientNoticeController(
    (message, visible) => visibility.push({ message, visible }),
    (callback, delay) => {
      const token = { callback, delay };
      scheduled.push(token);
      return token;
    },
    (token) => cancelled.push(token),
  );

  controller.show('第一条', 2500);
  controller.show('新卡片已生成', 2500);

  assert.deepEqual(cancelled, [scheduled[0]]);
  assert.equal(scheduled[1].delay, 2500);
  assert.deepEqual(visibility.at(-1), {
    message: '新卡片已生成',
    visible: true,
  });
  scheduled[1].callback();
  assert.deepEqual(visibility.at(-1), {
    message: '',
    visible: false,
  });
});

test('UUID fallback produces RFC 4122 version 4 IDs without randomUUID', () => {
  const cryptoLike = {
    getRandomValues(bytes) {
      for (let index = 0; index < bytes.length; index += 1) {
        bytes[index] = index * 17;
      }
      return bytes;
    },
  };
  const fromBytes = ui.createUuidV4(cryptoLike, () => 0.5);
  const fromMath = ui.createUuidV4(null, () => 0);
  const v4Pattern = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

  assert.match(fromBytes, v4Pattern);
  assert.match(fromMath, v4Pattern);
  assert.equal(fromBytes[14], '4');
  assert.match(fromBytes[19], /[89ab]/);
});

test('lost generation response keeps and reuses the persisted request', () => {
  const values = new Map();
  const storage = {
    getItem(key) {
      return values.has(key) ? values.get(key) : null;
    },
    setItem(key, value) {
      values.set(key, value);
    },
    removeItem(key) {
      values.delete(key);
    },
  };
  const first = ui.selectGenerationRequest(
    null,
    'aion',
    20,
    () => 'request-that-reached-server',
    undefined,
    'speech:custom:english-trained',
  );
  assert.equal(first.status, 'created');
  assert.deepEqual(first.request, {
    requestId: 'request-that-reached-server',
    actor: 'aion',
    contextLimit: 20,
    ttsVoice: 'speech:custom:english-trained',
  });
  assert.equal(
    ui.persistPendingGeneration(storage, first.request),
    true,
  );

  const afterLostResponse = ui.settlePendingGeneration(
    first.request,
    { type: 'request-error', status: 0 },
  );
  assert.deepEqual(afterLostResponse, first.request);
  const restored = ui.loadPendingGeneration(storage);
  const retry = ui.selectGenerationRequest(
    restored,
    'aion',
    20,
    () => 'must-not-create-a-new-request',
    undefined,
    'speech:custom:english-trained',
  );
  assert.equal(retry.status, 'reused');
  assert.deepEqual(retry.request, first.request);
});

test('pending request freezes the offered context snapshot across retries', () => {
  const first = ui.selectGenerationRequest(
    null,
    'aion',
    34,
    () => 'snapshot-request',
    1721880000.125,
    'speech:custom:english-trained',
  );
  assert.deepEqual(first.request, {
    requestId: 'snapshot-request',
    actor: 'aion',
    contextLimit: 34,
    ttsVoice: 'speech:custom:english-trained',
    snapshotEnd: 1721880000.125,
  });

  const retry = ui.selectGenerationRequest(
    first.request,
    'aion',
    34,
    () => 'must-not-change-request',
    1721880060.5,
    'speech:custom:english-trained',
  );
  assert.equal(retry.status, 'reused');
  assert.deepEqual(retry.request, first.request);
});

test('generation POST payload carries the frozen context bound', () => {
  assert.deepEqual(
    ui.generationPostPayload({
      requestId: 'snapshot-request',
      actor: 'connor',
      contextLimit: 20,
      ttsVoice: 'speech:custom:english-trained',
      snapshotEnd: 1721880000.125,
    }),
    {
      request_id: 'snapshot-request',
      actor: 'connor',
      context_limit: 20,
      tts_voice: 'speech:custom:english-trained',
      learning_day_end: 1721880000.125,
    },
  );
});

test('pending generation never crosses actor or context parameters implicitly', () => {
  const pending = {
    requestId: 'pending-one',
    actor: 'aion',
    contextLimit: 20,
    ttsVoice: 'speech:custom:english-trained',
  };
  assert.deepEqual(
    ui.selectGenerationRequest(
      pending,
      'connor',
      20,
      () => 'new-one',
      undefined,
      'speech:custom:english-trained',
    ),
    { status: 'conflict', request: pending },
  );
  assert.deepEqual(
    ui.selectGenerationRequest(
      pending,
      'aion',
      30,
      () => 'new-two',
      undefined,
      'speech:custom:english-trained',
    ),
    { status: 'conflict', request: pending },
  );
  assert.deepEqual(
    ui.selectGenerationRequest(
      pending,
      'aion',
      20,
      () => 'new-three',
      undefined,
      'speech:custom:different',
    ),
    { status: 'conflict', request: pending },
  );
  assert.equal(
    ui.selectGenerationRequest(
      null,
      'connor',
      30,
      () => 'explicit-new',
      undefined,
      'speech:custom:english-trained',
    ).request.requestId,
    'explicit-new',
  );
});

test('only validation rejection or authoritative card reload clears pending', () => {
  const pending = {
    requestId: 'server-committed',
    actor: 'aion',
    contextLimit: 10,
    ttsVoice: 'speech:custom:english-trained',
  };
  assert.deepEqual(
    ui.settlePendingGeneration(
      pending,
      { type: 'request-error', status: 502 },
    ),
    pending,
  );
  assert.equal(
    ui.settlePendingGeneration(
      pending,
      { type: 'request-error', status: 400 },
    ),
    null,
  );
  assert.deepEqual(
    ui.settlePendingGeneration(
      pending,
      {
        type: 'authoritative-reload',
        cards: [{ pack: { request_id: 'different-request' } }],
      },
    ),
    pending,
  );
  assert.equal(
    ui.settlePendingGeneration(
      pending,
      {
        type: 'authoritative-reload',
        cards: [{ pack: { request_id: 'server-committed' } }],
      },
    ),
    null,
  );
});

test('only the latest selected actor context request may update options', () => {
  const latest = { actor: 'connor', token: 4 };
  assert.equal(
    ui.isCurrentContextRequest({ actor: 'aion', token: 3 }, latest),
    false,
  );
  assert.equal(
    ui.isCurrentContextRequest({ actor: 'aion', token: 4 }, latest),
    false,
  );
  assert.equal(
    ui.isCurrentContextRequest({ actor: 'connor', token: 4 }, latest),
    true,
  );
});

test('translation focus helper restores keyboard focus to the same toggle', () => {
  let requestedId = '';
  let focusOptions = null;
  const fakeDocument = {
    getElementById(id) {
      requestedId = id;
      return {
        focus(options) {
          focusOptions = options;
        },
      };
    },
  };

  assert.equal(ui.restoreTranslationFocus(fakeDocument, 42), true);
  assert.equal(requestedId, 'translation-toggle-42');
  assert.deepEqual(focusOptions, { preventScroll: true });
});

test('translation disclosure updates only the target sentence DOM', () => {
  const button = {
    attributes: {},
    focusOptions: null,
    setAttribute(name, value) {
      this.attributes[name] = value;
    },
    focus(options) {
      this.focusOptions = options;
    },
  };
  const translation = { hidden: true };
  const fakeDocument = {
    getElementById(id) {
      if (id === 'translation-toggle-17') return button;
      if (id === 'translation-17') return translation;
      return null;
    },
  };

  assert.equal(
    ui.applyTranslationDisclosure(fakeDocument, 17, true),
    true,
  );
  assert.equal(button.attributes['aria-expanded'], 'true');
  assert.equal(translation.hidden, false);
  assert.deepEqual(button.focusOptions, { preventScroll: true });
  assert.equal(
    ui.applyTranslationDisclosure(fakeDocument, 99, true),
    false,
  );
});

test('card navigation maps direction to explicit motion classes', () => {
  assert.equal(ui.cardMotionClass(1), 'card-enter-next');
  assert.equal(ui.cardMotionClass(-1), 'card-enter-previous');
  assert.equal(ui.cardMotionClass(0), '');
});

test('audio accessibility helper enforces a 44 pixel interactive target', () => {
  const button = { style: {} };
  ui.applyAudioHitTarget(button);
  assert.equal(button.style.minWidth, '44px');
  assert.equal(button.style.minHeight, '44px');
});

test('playback controller pauses old audio and ignores its late end event', () => {
  const created = [];
  function createFakeAudio(url) {
    const listeners = {};
    const audio = {
      url,
      pauseCalls: 0,
      addEventListener(type, listener) {
        listeners[type] = listener;
      },
      pause() {
        this.pauseCalls += 1;
      },
      play() {
        return Promise.resolve();
      },
      emit(type) {
        listeners[type]();
      },
    };
    created.push(audio);
    return audio;
  }

  const controller = ui.createPlaybackController(createFakeAudio);
  controller.play('/audio/first');
  const firstSnapshot = controller.getSnapshot();
  controller.play('/audio/second');
  const secondSnapshot = controller.getSnapshot();

  assert.equal(created[0].pauseCalls, 1);
  assert.ok(secondSnapshot.token > firstSnapshot.token);
  assert.equal(secondSnapshot.audio, created[1]);

  created[0].emit('ended');
  assert.equal(controller.getSnapshot().audio, created[1]);

  created[1].emit('ended');
  assert.equal(controller.getSnapshot(), null);
});

if (failures) {
  throw new Error(`EnglishCornerUI: ${failures} test(s) failed`);
}
console.log(`EnglishCornerUI: ${testCount} tests passed`);

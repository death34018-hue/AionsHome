(function englishCornerModule(globalScope) {
  'use strict';

  const AVATAR_FALLBACKS = {
    user: '/public/UserIcon.png',
    aion: '/public/gropicon1.png',
    connor: '/public/codexicon.png',
  };

  function buildContextOptions(total) {
    const normalized = Math.max(0, Math.floor(Number(total) || 0));
    if (normalized === 0) {
      return { options: [0], defaultValue: 0 };
    }
    const options = [];
    for (let value = 10; value <= normalized; value += 10) {
      options.push(value);
    }
    if (options[options.length - 1] !== normalized) {
      options.push(normalized);
    }
    return {
      options,
      defaultValue: normalized < 50 ? normalized : 50,
    };
  }

  function nextLearningIndex(index, length, delta) {
    const size = Math.max(0, Math.floor(Number(length) || 0));
    if (size === 0) return 0;
    const current = Number.isFinite(Number(index)) ? Math.trunc(Number(index)) : 0;
    const step = Number.isFinite(Number(delta)) ? Math.trunc(Number(delta)) : 0;
    return ((current + step) % size + size) % size;
  }

  function formatPackDate(timestamp) {
    if (!timestamp) return '收在学习角';
    const date = new Date(Number(timestamp) * 1000);
    if (Number.isNaN(date.getTime())) return '收在学习角';
    return new Intl.DateTimeFormat('zh-CN', {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    }).format(date);
  }

  function learningHeaderPresentation(card, index, total) {
    const size = Math.max(0, Math.floor(Number(total) || 0));
    if (!size) return { progress: '等待新一幕', packInfo: '' };
    const current = Math.min(
      size - 1,
      Math.max(0, Math.floor(Number(index) || 0)),
    );
    const createdAt = card && card.pack ? card.pack.created_at : null;
    return {
      progress: `${current + 1} / ${size}`,
      packInfo: `学习包 · ${formatPackDate(createdAt)}`,
    };
  }

  function cardSpeakerGroups(card) {
    const result = [];
    const seen = new Set();
    const utterances = card && Array.isArray(card.utterances)
      ? card.utterances
      : [];
    utterances.forEach((utterance) => {
      const speaker = String(utterance && utterance.speaker || '').trim();
      if (!speaker || seen.has(speaker)) return;
      seen.add(speaker);
      result.push(speaker);
    });
    return result;
  }

  function safeSameOriginAvatarUrl(value, fallback, baseOrigin) {
    if (!value) return fallback;
    try {
      const base = new URL(baseOrigin || 'http://localhost');
      const candidate = new URL(String(value), base);
      if (
        candidate.origin === base.origin
        && /^https?:$/.test(candidate.protocol)
        && candidate.pathname.startsWith('/public/')
      ) {
        return `${candidate.pathname}${candidate.search}`;
      }
    } catch (_error) {
      // Fall through to the stable generic role asset.
    }
    return fallback;
  }

  function participantMeta(overview, speaker, baseOrigin) {
    const participants = overview && Array.isArray(overview.participants)
      ? overview.participants
      : [];
    const actors = overview && Array.isArray(overview.actors)
      ? overview.actors
      : [];
    const configured = (
      participants.find((item) => item.id === speaker)
      || actors.find((item) => item.id === speaker)
      || {}
    );
    const fallbackName = speaker === 'user'
      ? '用户'
      : speaker === 'connor'
      ? '第二位 AI'
      : 'AI';
    const fallbackAvatar = (
      AVATAR_FALLBACKS[speaker] || AVATAR_FALLBACKS.aion
    );
    return {
      id: speaker,
      name: String(configured.name || fallbackName).trim() || fallbackName,
      avatar: safeSameOriginAvatarUrl(
        configured.avatar_url || configured.avatar,
        fallbackAvatar,
        baseOrigin,
      ),
    };
  }

  function generationActorOptions(overview) {
    const actors = overview && Array.isArray(overview.actors)
      ? overview.actors
      : [];
    const participants = overview && Array.isArray(overview.participants)
      ? overview.participants
      : [];
    const source = actors.length ? actors : participants;
    const seen = new Set();
    const filtered = source.filter((actor) => {
      if (
        !actor
        || !['aion', 'connor'].includes(actor.id)
        || seen.has(actor.id)
      ) {
        return false;
      }
      seen.add(actor.id);
      return true;
    });
    return filtered.length
      ? filtered
      : [
        { id: 'aion', name: 'AI' },
        { id: 'connor', name: '第二位 AI' },
      ];
  }

  function normalizeVoiceOptions(payload, preferredVoice) {
    const source = payload && Array.isArray(payload.voices)
      ? payload.voices
      : [];
    const seen = new Set();
    const options = [];
    source.forEach((voice) => {
      const uri = String(voice && voice.uri || '').trim();
      if (!uri || seen.has(uri)) return;
      seen.add(uri);
      options.push({
        uri,
        label: String(voice.customName || uri).trim() || uri,
      });
    });
    const preferred = String(preferredVoice || '').trim();
    return {
      options,
      selected: options.some((voice) => voice.uri === preferred)
        ? preferred
        : options[0]?.uri || '',
    };
  }

  function publicAudioUrl(audio, baseOrigin) {
    if (!audio || audio.status !== 'ready' || !audio.url) return '';
    try {
      const fallbackOrigin = (
        typeof globalScope.location !== 'undefined'
        && globalScope.location.origin
      ) || 'http://localhost';
      const base = new URL(baseOrigin || fallbackOrigin);
      const candidate = new URL(String(audio.url), base);
      if (
        candidate.origin !== base.origin
        || !/^https?:$/.test(candidate.protocol)
        || !candidate.pathname.startsWith('/api/english-corner/audio/')
      ) {
        return '';
      }
      return candidate.href;
    } catch (_error) {
      return '';
    }
  }

  function buildArchiveReviewRows(card, baseOrigin) {
    const utterances = card && Array.isArray(card.utterances)
      ? card.utterances
      : [];
    return utterances.map((utterance) => {
      const audioAction = utteranceAudioAction(utterance, baseOrigin);
      return { utterance, audioAction };
    });
  }

  function utteranceAudioAction(utterance, baseOrigin) {
    if (!utterance) return null;
    const readyUrl = publicAudioUrl(utterance.audio, baseOrigin);
    if (readyUrl) {
      return {
        type: 'play',
        utteranceId: utterance.id,
        url: readyUrl,
      };
    }
    if (utterance.audio && utterance.audio.status === 'retrying') {
      return { type: 'busy', utteranceId: utterance.id };
    }
    if (utterance.audio || utterance.speaker !== 'user') {
      return { type: 'retry', utteranceId: utterance.id };
    }
    return null;
  }

  function speakerAudioActions(card, speaker, baseOrigin) {
    const playable = [];
    const recovery = [];
    const utterances = card && Array.isArray(card.utterances)
      ? card.utterances
      : [];
    utterances
      .filter((utterance) => utterance.speaker === speaker)
      .forEach((utterance) => {
        const readyUrl = publicAudioUrl(utterance.audio, baseOrigin);
        if (readyUrl) {
          playable.push({
            utteranceId: utterance.id,
            url: readyUrl,
          });
        } else if (
          utterance.audio
          && utterance.audio.status === 'retrying'
        ) {
          recovery.push({
            type: 'busy',
            utteranceId: utterance.id,
          });
        } else {
          recovery.push({
            type: 'retry',
            utteranceId: utterance.id,
          });
        }
      });
    return [
      ...(playable.length ? [{ type: 'play', items: playable }] : []),
      ...recovery,
    ];
  }

  function audioRetryPresentation(audio) {
    if (audio && audio.status === 'ready') {
      return {
        success: true,
        message: '这一句的音频已经重新准备好。',
      };
    }
    return {
      success: false,
      message: '这一句的音频仍未准备好，可以稍后再次重试。',
    };
  }

  function generationRefreshPresentation(refreshResult) {
    if (refreshResult && refreshResult.ok) {
      return { closeSheet: true, showSuccess: true, error: '' };
    }
    const detail = String(
      refreshResult && refreshResult.error
        ? refreshResult.error
        : '无法读取最新卡片',
    ).replace(/[。.!！]+$/, '');
    return {
      closeSheet: false,
      showSuccess: false,
      error: `学习包已生成，但${detail}。请重试同步。`,
    };
  }

  function createTransientNoticeController(
    update,
    schedule,
    cancelSchedule,
  ) {
    let timer = null;
    return {
      show(message, duration = 2500) {
        if (timer !== null) cancelSchedule(timer);
        update(String(message || ''), true);
        timer = schedule(() => {
          timer = null;
          update('', false);
        }, duration);
      },
      hide() {
        if (timer !== null) cancelSchedule(timer);
        timer = null;
        update('', false);
      },
    };
  }

  function createUuidV4(cryptoLike, randomSource) {
    if (cryptoLike && typeof cryptoLike.randomUUID === 'function') {
      return cryptoLike.randomUUID();
    }
    const bytes = new Uint8Array(16);
    let filledSecurely = false;
    if (cryptoLike && typeof cryptoLike.getRandomValues === 'function') {
      try {
        cryptoLike.getRandomValues(bytes);
        filledSecurely = true;
      } catch (_error) {
        filledSecurely = false;
      }
    }
    if (!filledSecurely) {
      const random = typeof randomSource === 'function'
        ? randomSource
        : Math.random;
      for (let index = 0; index < bytes.length; index += 1) {
        const value = Math.max(0, Math.min(0.999999999999, Number(random()) || 0));
        bytes[index] = Math.floor(value * 256);
      }
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (value) => (
      value.toString(16).padStart(2, '0')
    )).join('');
    return [
      hex.slice(0, 8),
      hex.slice(8, 12),
      hex.slice(12, 16),
      hex.slice(16, 20),
      hex.slice(20),
    ].join('-');
  }

  const PENDING_GENERATION_STORAGE_KEY = (
    'aionshome.learningCorner.pendingGeneration.v2'
  );

  function normalizePendingGeneration(value) {
    if (!value || typeof value !== 'object') return null;
    const requestId = String(value.requestId || '').trim();
    const actor = String(value.actor || '').trim();
    const ttsVoice = String(value.ttsVoice || '').trim();
    const contextLimit = Number(value.contextLimit);
    const hasSnapshotEnd = (
      value.snapshotEnd !== undefined
      && value.snapshotEnd !== null
      && value.snapshotEnd !== ''
    );
    const snapshotEnd = Number(value.snapshotEnd);
    if (
      !requestId
      || requestId.length > 128
      || !['aion', 'connor'].includes(actor)
      || !ttsVoice
      || ttsVoice.length > 512
      || !Number.isInteger(contextLimit)
      || contextLimit < 0
      || contextLimit > 10000
      || (
        hasSnapshotEnd
        && (!Number.isFinite(snapshotEnd) || snapshotEnd <= 0)
      )
    ) {
      return null;
    }
    return {
      requestId,
      actor,
      contextLimit,
      ttsVoice,
      ...(hasSnapshotEnd ? { snapshotEnd } : {}),
    };
  }

  function selectGenerationRequest(
    pending,
    actor,
    contextLimit,
    createRequestId,
    snapshotEnd,
    ttsVoice,
  ) {
    const normalizedPending = normalizePendingGeneration(pending);
    const requestedActor = String(actor || '').trim();
    const requestedLimit = Number(contextLimit);
    const requestedVoice = String(ttsVoice || '').trim();
    if (normalizedPending) {
      if (
        normalizedPending.actor === requestedActor
        && normalizedPending.contextLimit === requestedLimit
        && normalizedPending.ttsVoice === requestedVoice
      ) {
        return { status: 'reused', request: normalizedPending };
      }
      return { status: 'conflict', request: normalizedPending };
    }
    const request = normalizePendingGeneration({
      requestId: createRequestId(),
      actor: requestedActor,
      contextLimit: requestedLimit,
      ttsVoice: requestedVoice,
      ...(Number.isFinite(Number(snapshotEnd)) && Number(snapshotEnd) > 0
        ? { snapshotEnd: Number(snapshotEnd) }
        : {}),
    });
    if (!request) {
      return { status: 'invalid', request: null };
    }
    return { status: 'created', request };
  }

  function generationPostPayload(request) {
    const normalized = normalizePendingGeneration(request);
    if (!normalized) return null;
    return {
      request_id: normalized.requestId,
      actor: normalized.actor,
      context_limit: normalized.contextLimit,
      tts_voice: normalized.ttsVoice,
      ...(normalized.snapshotEnd
        ? { learning_day_end: normalized.snapshotEnd }
        : {}),
    };
  }

  function cardsContainGenerationRequest(cards, requestId) {
    const target = String(requestId || '').trim();
    if (!target || !Array.isArray(cards)) return false;
    return cards.some((card) => (
      card
      && card.pack
      && String(card.pack.request_id || '').trim() === target
    ));
  }

  function settlePendingGeneration(pending, outcome) {
    const normalized = normalizePendingGeneration(pending);
    if (!normalized) return null;
    if (
      outcome
      && outcome.type === 'request-error'
      && [400, 422].includes(Number(outcome.status))
    ) {
      return null;
    }
    if (
      outcome
      && outcome.type === 'authoritative-reload'
      && cardsContainGenerationRequest(
        outcome.cards,
        normalized.requestId,
      )
    ) {
      return null;
    }
    return normalized;
  }

  function persistPendingGeneration(storage, pending) {
    const normalized = normalizePendingGeneration(pending);
    if (!storage || !normalized) return false;
    try {
      storage.setItem(
        PENDING_GENERATION_STORAGE_KEY,
        JSON.stringify(normalized),
      );
      return true;
    } catch (_error) {
      return false;
    }
  }

  function loadPendingGeneration(storage) {
    if (!storage) return null;
    try {
      const raw = storage.getItem(PENDING_GENERATION_STORAGE_KEY);
      const normalized = normalizePendingGeneration(
        raw ? JSON.parse(raw) : null,
      );
      if (!normalized && raw) {
        storage.removeItem(PENDING_GENERATION_STORAGE_KEY);
      }
      return normalized;
    } catch (_error) {
      return null;
    }
  }

  function clearPendingGeneration(storage) {
    if (!storage) return false;
    try {
      storage.removeItem(PENDING_GENERATION_STORAGE_KEY);
      return true;
    } catch (_error) {
      return false;
    }
  }

  function isCurrentContextRequest(request, latest) {
    return Boolean(
      request
      && latest
      && request.actor === latest.actor
      && Number(request.token) === Number(latest.token),
    );
  }

  function translationToggleId(utteranceId) {
    const safeId = String(utteranceId).replace(/[^a-zA-Z0-9_-]/g, '-');
    return `translation-toggle-${safeId}`;
  }

  function restoreTranslationFocus(documentLike, utteranceId) {
    if (!documentLike || typeof documentLike.getElementById !== 'function') {
      return false;
    }
    const target = documentLike.getElementById(
      translationToggleId(utteranceId),
    );
    if (!target || typeof target.focus !== 'function') return false;
    target.focus({ preventScroll: true });
    return true;
  }

  function applyTranslationDisclosure(
    documentLike,
    utteranceId,
    expanded,
  ) {
    if (!documentLike || typeof documentLike.getElementById !== 'function') {
      return false;
    }
    const toggle = documentLike.getElementById(
      translationToggleId(utteranceId),
    );
    const translation = documentLike.getElementById(
      `translation-${utteranceId}`,
    );
    if (
      !toggle
      || typeof toggle.setAttribute !== 'function'
      || !translation
    ) {
      return false;
    }
    toggle.setAttribute('aria-expanded', String(Boolean(expanded)));
    translation.hidden = !expanded;
    restoreTranslationFocus(documentLike, utteranceId);
    return true;
  }

  function cardMotionClass(delta) {
    const direction = Number(delta);
    if (direction > 0) return 'card-enter-next';
    if (direction < 0) return 'card-enter-previous';
    return '';
  }

  function applyAudioHitTarget(button) {
    if (!button || !button.style) return button;
    button.style.minWidth = '44px';
    button.style.minHeight = '44px';
    return button;
  }

  function createPlaybackController(createAudio) {
    let current = null;
    let tokenCounter = 0;

    function supersedeCurrent() {
      if (!current) return;
      const previous = current;
      current = null;
      previous.audio.pause();
      previous.resolve({ status: 'superseded' });
    }

    function play(url) {
      supersedeCurrent();
      const token = tokenCounter + 1;
      tokenCounter = token;
      const audio = createAudio(url);
      return new Promise((resolve, reject) => {
        const entry = { audio, token, resolve, reject };
        current = entry;

        const finish = (status) => {
          if (!current || current.token !== token) return;
          current = null;
          resolve({ status });
        };
        const fail = (error) => {
          if (!current || current.token !== token) return;
          current = null;
          reject(error instanceof Error ? error : new Error('Audio playback failed'));
        };

        audio.addEventListener('ended', () => finish('ended'), { once: true });
        audio.addEventListener(
          'error',
          () => fail(new Error('Audio playback failed')),
          { once: true },
        );
        try {
          const playResult = audio.play();
          if (playResult && typeof playResult.catch === 'function') {
            playResult.catch(fail);
          }
        } catch (error) {
          fail(error);
        }
      });
    }

    function stop() {
      supersedeCurrent();
    }

    function getSnapshot() {
      return current
        ? { audio: current.audio, token: current.token }
        : null;
    }

    return { play, stop, getSnapshot };
  }

  function replaceUtteranceAudio(card, utteranceId, audio) {
    return {
      ...card,
      utterances: (card.utterances || []).map((utterance) => (
        Number(utterance.id) === Number(utteranceId)
          ? { ...utterance, audio: { ...audio } }
          : utterance
      )),
    };
  }

  function reduceUIState(state, action) {
    const current = state || {};
    const learning = Array.isArray(current.learning) ? current.learning : [];
    const learned = Array.isArray(current.learned) ? current.learned : [];

    if (action.type === 'toggle-translation') {
      const key = String(action.utteranceId);
      return {
        ...current,
        expandedTranslations: {
          ...(current.expandedTranslations || {}),
          [key]: !Boolean((current.expandedTranslations || {})[key]),
        },
      };
    }

    if (action.type === 'learn-card') {
      const index = learning.findIndex(
        (card) => Number(card.id) === Number(action.cardId),
      );
      if (index < 0) return current;
      const card = { ...learning[index], status: 'learned' };
      return {
        ...current,
        learning: learning.filter((_item, itemIndex) => itemIndex !== index),
        learned: [card, ...learned.filter(
          (item) => Number(item.id) !== Number(card.id),
        )],
        undo: { card, index },
      };
    }

    if (action.type === 'undo-learn' && current.undo) {
      const restored = { ...current.undo.card, status: 'learning' };
      const withoutCard = learning.filter(
        (item) => Number(item.id) !== Number(restored.id),
      );
      const index = Math.max(
        0,
        Math.min(Number(current.undo.index) || 0, withoutCard.length),
      );
      return {
        ...current,
        learning: [
          ...withoutCard.slice(0, index),
          restored,
          ...withoutCard.slice(index),
        ],
        learned: learned.filter(
          (item) => Number(item.id) !== Number(restored.id),
        ),
        undo: null,
      };
    }

    if (action.type === 'relearn-card') {
      const card = learned.find(
        (item) => Number(item.id) === Number(action.cardId),
      );
      if (!card) return current;
      const restored = { ...card, status: 'learning' };
      return {
        ...current,
        learning: [
          restored,
          ...learning.filter(
            (item) => Number(item.id) !== Number(restored.id),
          ),
        ],
        learned: learned.filter(
          (item) => Number(item.id) !== Number(restored.id),
        ),
      };
    }

    if (action.type === 'audio-retrying' || action.type === 'audio-updated') {
      const nextAudio = action.type === 'audio-retrying'
        ? { status: 'retrying' }
        : action.audio;
      return {
        ...current,
        learning: learning.map((card) => replaceUtteranceAudio(
          card,
          action.utteranceId,
          nextAudio,
        )),
        learned: learned.map((card) => replaceUtteranceAudio(
          card,
          action.utteranceId,
          nextAudio,
        )),
      };
    }

    return current;
  }

  const exported = {
    buildContextOptions,
    nextLearningIndex,
    learningHeaderPresentation,
    cardSpeakerGroups,
    participantMeta,
    generationActorOptions,
    normalizeVoiceOptions,
    publicAudioUrl,
    buildArchiveReviewRows,
    utteranceAudioAction,
    speakerAudioActions,
    audioRetryPresentation,
    generationRefreshPresentation,
    createTransientNoticeController,
    createUuidV4,
    selectGenerationRequest,
    generationPostPayload,
    settlePendingGeneration,
    persistPendingGeneration,
    loadPendingGeneration,
    isCurrentContextRequest,
    restoreTranslationFocus,
    applyTranslationDisclosure,
    cardMotionClass,
    applyAudioHitTarget,
    createPlaybackController,
    reduceUIState,
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = exported;
  }
  globalScope.EnglishCornerUI = exported;

  if (typeof document === 'undefined') return;

  const API_ROOT = '/api/english-corner';
  let pendingGenerationStorage = null;
  try {
    pendingGenerationStorage = window.localStorage;
  } catch (_error) {
    pendingGenerationStorage = null;
  }
  let pendingGeneration = loadPendingGeneration(pendingGenerationStorage);
  const VOICE_STORAGE_KEY = 'aionshome.learningCorner.ttsVoice.v1';
  const elements = {};
  let state = {
    overview: { counts: { learning: 0, learned: 0 }, actors: [] },
    learning: [],
    learned: [],
    expandedTranslations: {},
    undo: null,
    index: 0,
    loading: true,
    busyCardId: null,
    generating: false,
    generationNeedsRefresh: false,
    archiveTab: 'learning',
    voicesReady: false,
  };
  let undoTimer = null;
  let generationNotice = null;
  const playbackController = createPlaybackController(
    (url) => new Audio(url),
  );
  let gesture = null;
  let contextRequestToken = 0;

  function updatePendingGeneration(nextPending) {
    const normalized = normalizePendingGeneration(nextPending);
    if (normalized) {
      if (!persistPendingGeneration(pendingGenerationStorage, normalized)) {
        return false;
      }
      pendingGeneration = normalized;
      return true;
    }
    if (!clearPendingGeneration(pendingGenerationStorage)) {
      return false;
    }
    pendingGeneration = null;
    return true;
  }

  function byId(id) {
    return document.getElementById(id);
  }

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function actorMeta(speaker) {
    return participantMeta(
      state.overview,
      speaker,
      window.location.origin,
    );
  }

  async function requestJson(path, options) {
    let response;
    try {
      response = await fetch(path, {
        credentials: 'same-origin',
        headers: {
          Accept: 'application/json',
          ...(options && options.body
            ? { 'Content-Type': 'application/json' }
            : {}),
          ...((options && options.headers) || {}),
        },
        ...options,
      });
    } catch (error) {
      const offline = !navigator.onLine;
      const requestError = new Error(
        offline
          ? '当前处于离线状态，内容会保留，联网后可以重试。'
          : '暂时无法连接服务器，请稍后重试。',
      );
      requestError.status = 0;
      throw requestError;
    }
    const contentType = response.headers.get('content-type') || '';
    const payload = contentType.includes('application/json')
      ? await response.json()
      : null;
    if (!response.ok) {
      const requestError = new Error(
        (payload && (payload.detail || payload.message))
        || `请求失败（${response.status}）`,
      );
      requestError.status = response.status;
      throw requestError;
    }
    return payload;
  }

  async function fetchAllCards(status) {
    const items = [];
    let offset = 0;
    let total = 0;
    do {
      const payload = await requestJson(
        `${API_ROOT}/cards?status=${encodeURIComponent(status)}&limit=100&offset=${offset}`,
      );
      const page = Array.isArray(payload.items) ? payload.items : [];
      items.push(...page);
      total = Math.max(0, Number(payload.total) || 0);
      offset += page.length;
      if (page.length === 0) break;
    } while (items.length < total);
    return items;
  }

  function setBanner(message, kind, retry) {
    elements.statusBanner.hidden = !message;
    elements.statusBanner.dataset.kind = kind || 'info';
    elements.statusMessage.textContent = message || '';
    elements.statusRetry.hidden = !retry;
  }

  function rememberedVoice() {
    try {
      return String(
        pendingGeneration?.ttsVoice
        || pendingGenerationStorage?.getItem(VOICE_STORAGE_KEY)
        || '',
      ).trim();
    } catch (_error) {
      return String(pendingGeneration?.ttsVoice || '').trim();
    }
  }

  function rememberVoice(voice) {
    try {
      pendingGenerationStorage?.setItem(
        VOICE_STORAGE_KEY,
        String(voice || '').trim(),
      );
    } catch (_error) {
      // Voice selection still works for this page session.
    }
  }

  function renderVoiceOptions(normalized, fallbackVoice = '') {
    const options = [...normalized.options];
    const pendingVoice = String(fallbackVoice || '').trim();
    if (
      pendingVoice
      && !options.some((voice) => voice.uri === pendingVoice)
    ) {
      options.unshift({
        uri: pendingVoice,
        label: '上次待确认请求的音色',
      });
    }
    elements.voiceSelect.replaceChildren();
    options.forEach((voice) => {
      const option = document.createElement('option');
      option.value = voice.uri;
      option.textContent = voice.label;
      elements.voiceSelect.append(option);
    });
    const selected = options.some(
      (voice) => voice.uri === pendingVoice,
    ) ? pendingVoice : normalized.selected;
    elements.voiceSelect.value = selected || '';
    elements.voiceSelect.disabled = !selected;
    state.voicesReady = Boolean(selected);
    elements.voiceMeta.textContent = selected
      ? '三个人的全部英文都会使用这个音色。'
      : '没有可用音色，请先配置硅基流动音色。';
    if (selected) rememberVoice(selected);
    if (!state.generating) {
      elements.generateSubmit.disabled = !state.voicesReady;
    }
  }

  async function loadVoiceOptions() {
    elements.voiceSelect.disabled = true;
    elements.voiceMeta.textContent = '正在读取可用音色…';
    const preferred = rememberedVoice();
    try {
      const payload = await requestJson('/api/tts/voices');
      renderVoiceOptions(
        normalizeVoiceOptions(payload, preferred),
        pendingGeneration?.ttsVoice || '',
      );
    } catch (error) {
      if (pendingGeneration?.ttsVoice) {
        renderVoiceOptions(
          { options: [], selected: '' },
          pendingGeneration.ttsVoice,
        );
        elements.voiceMeta.textContent = (
          '音色列表暂时不可用，将继续重试上次待确认请求。'
        );
      } else {
        renderVoiceOptions({ options: [], selected: '' });
        elements.voiceMeta.textContent = error.message;
      }
    }
  }

  async function loadAll(options) {
    const preserveCardId = (
      options
      && options.preserveCardId
    ) || (state.learning[state.index] && state.learning[state.index].id);
    if (!state.learning.length && !state.learned.length) {
      state.loading = true;
      render();
    }
    try {
      const [overview, learning, learned] = await Promise.all([
        requestJson(`${API_ROOT}/overview`),
        fetchAllCards('learning'),
        fetchAllCards('learned'),
      ]);
      state = {
        ...state,
        overview,
        learning,
        learned,
        loading: false,
      };
      const pendingBeforeReload = pendingGeneration;
      const settledPending = settlePendingGeneration(
        pendingBeforeReload,
        {
          type: 'authoritative-reload',
          cards: [...learning, ...learned],
        },
      );
      const confirmedRequestId = (
        pendingBeforeReload && settledPending === null
      ) ? pendingBeforeReload.requestId : '';
      if (confirmedRequestId) {
        updatePendingGeneration(null);
        state.generationNeedsRefresh = false;
      }
      const preservedIndex = learning.findIndex(
        (card) => Number(card.id) === Number(preserveCardId),
      );
      state.index = preservedIndex >= 0
        ? preservedIndex
        : Math.min(state.index, Math.max(0, learning.length - 1));
      setBanner('', 'info', false);
      render();
      return { ok: true, confirmedRequestId };
    } catch (error) {
      state.loading = false;
      setBanner(error.message, navigator.onLine ? 'error' : 'offline', true);
      render();
      return { ok: false, error: error.message };
    }
  }

  function updateOverviewCounts() {
    state.overview = {
      ...state.overview,
      counts: {
        learning: state.learning.length,
        learned: state.learned.length,
      },
    };
  }

  function render() {
    renderHeader();
    renderStage();
    renderArchive();
  }

  function renderHeader() {
    const learningCount = state.overview.counts
      ? Number(state.overview.counts.learning) || 0
      : state.learning.length;
    elements.learningCount.textContent = String(learningCount);
    const header = learningHeaderPresentation(
      state.learning[state.index],
      state.index,
      state.learning.length,
    );
    elements.progressText.textContent = header.packInfo
      ? `${header.progress} · ${header.packInfo}`
      : header.progress;
    elements.progressDots.replaceChildren();
    const dotCount = Math.min(7, state.learning.length);
    for (let index = 0; index < dotCount; index += 1) {
      const dot = node('span', 'progress-dot');
      const activeDot = state.learning.length <= 7
        ? state.index
        : Math.round((state.index / Math.max(1, state.learning.length - 1)) * 6);
      if (index === activeDot) dot.classList.add('is-active');
      elements.progressDots.append(dot);
    }
  }

  function renderStage({
    motionClass = '',
    resetScroll = false,
  } = {}) {
    elements.cardRegion.replaceChildren();
    if (resetScroll) elements.cardRegion.scrollTop = 0;
    elements.previousCard.disabled = state.learning.length < 2;
    elements.nextCard.disabled = state.learning.length < 2;
    if (state.loading) {
      const loading = node('div', 'state-card state-card--loading');
      loading.setAttribute('role', 'status');
      loading.append(
        node('span', 'loading-lantern', '✦'),
        node('h2', '', '正在点亮小木屋…'),
        node('p', '', '把今天留下的英语小片段收进来。'),
      );
      elements.cardRegion.append(loading);
      return;
    }
    if (!state.learning.length) {
      const empty = node('div', 'state-card');
      empty.append(
        node('span', 'empty-moon', '☾'),
        node('h2', '', '今晚的卡片架还是空的'),
        node('p', '', '想学的时候再亲手生成三幕，不催你，也不自动开始。'),
      );
      const generate = node('button', 'peach-button', '生成今晚的三幕');
      generate.type = 'button';
      generate.addEventListener('click', openGenerateSheet);
      empty.append(generate);
      elements.cardRegion.append(empty);
      return;
    }
    const card = state.learning[state.index];
    elements.cardRegion.append(renderLearningCard(card, motionClass));
  }

  function renderLearningCard(card, motionClass = '') {
    const article = node('article', 'learning-card');
    if (motionClass) article.classList.add(motionClass);
    article.dataset.cardId = String(card.id);
    article.setAttribute('aria-labelledby', `card-title-${card.id}`);

    const heading = node('header', 'scene-heading');
    const eyebrow = node('span', 'scene-eyebrow', 'TONIGHT’S LITTLE SCENE');
    const title = node('h2', 'scene-title', card.title || 'A little scene');
    title.id = `card-title-${card.id}`;
    heading.append(eyebrow, title);
    article.append(heading);

    const dialogue = node('div', 'dialogue');
    (card.utterances || []).forEach((utterance) => {
      dialogue.append(renderUtterance(card, utterance));
    });
    article.append(dialogue);

    if (Array.isArray(card.vocabulary) && card.vocabulary.length) {
      article.append(renderVocabulary(card.vocabulary));
    }

    const action = node('button', 'learn-action', '✓ 这一幕我学会啦');
    action.type = 'button';
    action.disabled = state.busyCardId === card.id;
    if (action.disabled) action.textContent = '正在收好这一幕…';
    action.addEventListener('click', () => learnCard(card, article));
    article.append(action);
    return article;
  }

  function renderUtterance(card, utterance) {
    const isUser = utterance.speaker === 'user';
    const turn = node('section', `turn ${isUser ? 'turn--user' : 'turn--ai'}`);
    const meta = actorMeta(utterance.speaker);
    const identity = node('div', 'identity');
    const avatar = node('img', 'avatar');
    avatar.src = meta.avatar;
    avatar.alt = '';
    avatar.width = 30;
    avatar.height = 30;
    const name = node('span', 'speaker-name', meta.name);
    identity.append(avatar, name);

    const audioAction = utteranceAudioAction(
      utterance,
      window.location.origin,
    );
    if (audioAction) {
      identity.append(renderSingleAudioButton(audioAction, meta));
    }

    const sentence = node('button', 'sentence-button');
    sentence.type = 'button';
    sentence.id = translationToggleId(utterance.id);
    const expanded = Boolean(
      state.expandedTranslations[String(utterance.id)],
    );
    sentence.setAttribute('aria-expanded', String(expanded));
    sentence.setAttribute(
      'aria-controls',
      `translation-${utterance.id}`,
    );
    sentence.textContent = utterance.english || '';
    sentence.addEventListener('click', () => {
      state = reduceUIState(state, {
        type: 'toggle-translation',
        utteranceId: utterance.id,
      });
      applyTranslationDisclosure(
        document,
        utterance.id,
        Boolean(state.expandedTranslations[String(utterance.id)]),
      );
    });

    const translation = node(
      'p',
      'translation',
      utterance.translation || '',
    );
    translation.id = `translation-${utterance.id}`;
    translation.hidden = !expanded;
    turn.append(identity, sentence, translation);
    return turn;
  }

  function renderSingleAudioButton(audioAction, meta, extraClass = '') {
    const className = ['audio-button', extraClass].filter(Boolean).join(' ');
    const button = node('button', className);
    applyAudioHitTarget(button);
    button.type = 'button';
    if (audioAction.type === 'play') {
      button.textContent = '◖';
      button.setAttribute('aria-label', `播放 ${meta.name} 的这一句英文`);
      button.title = '播放这一句英文';
      button.addEventListener('click', async () => {
        try {
          await playAudioFile(audioAction.url);
          setBanner('', 'info', false);
        } catch (_error) {
          markAudioPlaybackFailed(audioAction.utteranceId);
          render();
          setBanner(
            '这句音频暂时无法播放，已经为它显示单独重试入口。',
            'error',
            false,
          );
        }
      });
    } else if (audioAction.type === 'retry') {
      button.textContent = '↻';
      button.setAttribute('aria-label', `重试 ${meta.name} 的这一句音频`);
      button.title = '只重试这一句';
      button.addEventListener(
        'click',
        () => retryAudio(audioAction.utteranceId),
      );
    } else {
      button.textContent = '…';
      button.disabled = true;
      button.setAttribute(
        'aria-label',
        `${meta.name} 的这一句音频正在重试`,
      );
    }
    return button;
  }

  function renderVocabulary(vocabulary) {
    const section = node('section', 'vocabulary');
    const heading = node('h3', 'vocabulary-title', '这一幕里，也许值得记住');
    section.append(heading);
    const list = node('dl', 'vocabulary-list');
    vocabulary.forEach((item) => {
      const row = node('div', 'vocabulary-row');
      const termWrap = node('div', 'vocabulary-term');
      const term = node('dt', '', item.term || '');
      const details = node(
        'span',
        'vocabulary-details',
        [item.ipa, item.part_of_speech].filter(Boolean).join(' · '),
      );
      const meaning = node('dd', '', item.meaning || '');
      termWrap.append(term, details);
      row.append(termWrap, meaning);
      list.append(row);
    });
    section.append(list);
    return section;
  }

  function markAudioPlaybackFailed(utteranceId) {
    state = reduceUIState(state, {
      type: 'audio-updated',
      utteranceId,
      audio: {
        utterance_id: utteranceId,
        status: 'failed',
        message: 'Audio is unavailable; retry is available.',
      },
    });
  }

  async function playSpeakerAudioItems(items) {
    for (const item of items) {
      try {
        const result = await playAudioFile(item.url);
        if (result && result.status === 'superseded') return;
      } catch (_error) {
        markAudioPlaybackFailed(item.utteranceId);
        render();
        setBanner(
          '这句音频暂时无法播放，已经为它显示单独重试入口。',
          'error',
          false,
        );
        return;
      }
    }
    setBanner('', 'info', false);
  }

  function playAudioFile(url) {
    return playbackController.play(url);
  }

  async function retryAudio(utteranceId) {
    state = reduceUIState(state, {
      type: 'audio-retrying',
      utteranceId,
    });
    render();
    try {
      const audio = await requestJson(
        `${API_ROOT}/audio/${encodeURIComponent(utteranceId)}/retry`,
        { method: 'POST' },
      );
      state = reduceUIState(state, {
        type: 'audio-updated',
        utteranceId,
        audio,
      });
      const presentation = audioRetryPresentation(audio);
      setBanner(
        presentation.message,
        presentation.success ? 'success' : 'error',
        false,
      );
    } catch (error) {
      state = reduceUIState(state, {
        type: 'audio-updated',
        utteranceId,
        audio: { status: 'failed', message: error.message },
      });
      setBanner(error.message, 'error', false);
    }
    render();
  }

  async function learnCard(card, article) {
    if (state.busyCardId) return;
    state.busyCardId = card.id;
    renderStage();
    try {
      await requestJson(`${API_ROOT}/cards/${card.id}/status`, {
        method: 'PATCH',
        body: JSON.stringify({ status: 'learned' }),
      });
      const liveArticle = elements.cardRegion.querySelector('.learning-card');
      if (liveArticle) liveArticle.classList.add('is-exiting');
      await animationPause(230);
      state = reduceUIState(state, {
        type: 'learn-card',
        cardId: card.id,
      });
      state.busyCardId = null;
      state.index = Math.min(
        state.index,
        Math.max(0, state.learning.length - 1),
      );
      updateOverviewCounts();
      showUndo();
      render();
    } catch (error) {
      state.busyCardId = null;
      setBanner(error.message, 'error', true);
      render();
    }
  }

  function animationPause(milliseconds) {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      window.setTimeout(resolve, milliseconds);
    });
  }

  function showUndo() {
    window.clearTimeout(undoTimer);
    elements.undoToast.hidden = false;
    elements.undoToast.classList.add('is-visible');
    undoTimer = window.setTimeout(() => {
      state.undo = null;
      elements.undoToast.classList.remove('is-visible');
      elements.undoToast.hidden = true;
    }, 5000);
  }

  async function undoLearn() {
    if (!state.undo) return;
    const undoCardId = state.undo.card.id;
    const undoIndex = state.undo.index;
    elements.undoButton.disabled = true;
    try {
      await requestJson(
        `${API_ROOT}/cards/${undoCardId}/status`,
        {
          method: 'PATCH',
          body: JSON.stringify({ status: 'learning' }),
        },
      );
      state = reduceUIState(state, { type: 'undo-learn' });
      state.index = Math.max(0, Math.min(undoIndex, state.learning.length - 1));
      updateOverviewCounts();
      window.clearTimeout(undoTimer);
      elements.undoToast.hidden = true;
      elements.undoToast.classList.remove('is-visible');
      render();
    } catch (error) {
      setBanner(error.message, 'error', true);
    } finally {
      elements.undoButton.disabled = false;
    }
  }

  function changeCard(delta) {
    if (state.learning.length < 2 || state.busyCardId) return;
    state.index = nextLearningIndex(
      state.index,
      state.learning.length,
      delta,
    );
    renderHeader();
    renderStage({
      motionClass: cardMotionClass(delta),
      resetScroll: true,
    });
    elements.cardRegion.focus({ preventScroll: true });
  }

  function renderArchive() {
    if (!elements.archiveDialog.open) return;
    const activeItems = state.archiveTab === 'learning'
      ? state.learning
      : state.learned;
    elements.archiveLearningTab.classList.toggle(
      'is-active',
      state.archiveTab === 'learning',
    );
    elements.archiveLearnedTab.classList.toggle(
      'is-active',
      state.archiveTab === 'learned',
    );
    elements.archiveLearningTab.setAttribute(
      'aria-selected',
      String(state.archiveTab === 'learning'),
    );
    elements.archiveLearnedTab.setAttribute(
      'aria-selected',
      String(state.archiveTab === 'learned'),
    );
    elements.archiveLearningTab.textContent = `正在学 ${state.learning.length}`;
    elements.archiveLearnedTab.textContent = `已学会 ${state.learned.length}`;
    elements.archiveList.replaceChildren();
    if (!activeItems.length) {
      elements.archiveList.append(node(
        'p',
        'archive-empty',
        state.archiveTab === 'learning'
          ? '卡片架上暂时没有正在学的内容。'
          : '学会的卡片会安静地收在这里。',
      ));
      return;
    }
    activeItems.forEach((card) => {
      elements.archiveList.append(renderArchiveCard(card));
    });
  }

  function renderArchiveCard(card) {
    const details = node('details', 'archive-card');
    const summary = node('summary', 'archive-summary');
    const copy = node('span');
    copy.append(
      node('strong', '', card.title || 'A little scene'),
      node('small', '', formatPackDate(card.pack && card.pack.created_at)),
    );
    summary.append(copy, node('span', 'archive-chevron', '⌄'));
    details.append(summary);
    const body = node('div', 'archive-card-body');
    buildArchiveReviewRows(card, window.location.origin).forEach((row) => {
      const utterance = row.utterance;
      const meta = actorMeta(utterance.speaker);
      const line = node(
        'div',
        `archive-line ${utterance.speaker === 'user' ? 'is-user' : ''}`,
      );
      const lineHeading = node('div', 'archive-line-heading');
      lineHeading.append(node('b', '', meta.name));
      if (row.audioAction) {
        lineHeading.append(renderArchiveAudioButton(row.audioAction, meta));
      }
      line.append(
        lineHeading,
        node('p', '', utterance.english || ''),
        node('small', '', utterance.translation || ''),
      );
      body.append(line);
    });
    if (card.vocabulary && card.vocabulary.length) {
      body.append(renderVocabulary(card.vocabulary));
    }
    const action = node(
      'button',
      'archive-action',
      state.archiveTab === 'learning' ? '回到这一幕' : '重新学习',
    );
    action.type = 'button';
    if (state.archiveTab === 'learning') {
      action.addEventListener('click', () => {
        const index = state.learning.findIndex(
          (item) => Number(item.id) === Number(card.id),
        );
        if (index >= 0) state.index = index;
        elements.archiveDialog.close();
        render();
        elements.cardRegion.focus();
      });
    } else {
      action.addEventListener('click', () => relearnCard(card.id));
    }
    body.append(action);
    details.append(body);
    return details;
  }

  function renderArchiveAudioButton(audioAction, meta) {
    return renderSingleAudioButton(
      audioAction,
      meta,
      'archive-audio-button',
    );
  }

  async function relearnCard(cardId) {
    try {
      await requestJson(`${API_ROOT}/cards/${cardId}/status`, {
        method: 'PATCH',
        body: JSON.stringify({ status: 'learning' }),
      });
      state = reduceUIState(state, { type: 'relearn-card', cardId });
      state.index = 0;
      updateOverviewCounts();
      render();
      setBanner('这张卡片已经回到“正在学”。', 'success', false);
    } catch (error) {
      setBanner(error.message, 'error', true);
    }
  }

  function openArchive() {
    state.archiveTab = 'learning';
    elements.archiveDialog.showModal();
    renderArchive();
  }

  function openGenerateSheet() {
    renderActorChoices();
    elements.generateError.hidden = true;
    elements.generateError.textContent = '';
    elements.generateDialog.showModal();
    if (!state.voicesReady) loadVoiceOptions();
    const selected = elements.actorChoices.querySelector(
      'input[name="generator"]:checked',
    );
    if (selected) loadContextOptions(selected.value);
  }

  function renderActorChoices() {
    elements.actorChoices.replaceChildren();
    const actors = generationActorOptions(state.overview);
    actors.forEach((actor, index) => {
      const label = node('label', 'actor-choice');
      const input = document.createElement('input');
      input.type = 'radio';
      input.name = 'generator';
      input.value = actor.id;
      input.checked = pendingGeneration
        ? actor.id === pendingGeneration.actor
        : index === 0;
      const avatar = node('img');
      avatar.src = actorMeta(actor.id).avatar;
      avatar.alt = '';
      const copy = node('span');
      copy.append(
        node('strong', '', actor.name || actorMeta(actor.id).name),
        node('small', '', '用这一位的视角与表达生成'),
      );
      label.append(input, avatar, copy);
      input.addEventListener('change', () => loadContextOptions(actor.id));
      elements.actorChoices.append(label);
    });
  }

  async function loadContextOptions(actor) {
    const requestMeta = {
      actor,
      token: contextRequestToken + 1,
    };
    contextRequestToken = requestMeta.token;
    elements.contextSelect.disabled = true;
    elements.contextSelect.dataset.learningDayEnd = '';
    elements.contextMeta.textContent = '正在数今天留下的消息…';
    try {
      const payload = await requestJson(
        `${API_ROOT}/context-options?actor=${encodeURIComponent(actor)}`,
      );
      const selected = elements.actorChoices.querySelector(
        'input[name="generator"]:checked',
      );
      const latest = {
        actor: selected ? selected.value : '',
        token: contextRequestToken,
      };
      if (!isCurrentContextRequest(requestMeta, latest)) {
        return { stale: true };
      }
      const fallback = buildContextOptions(payload.context_total);
      const options = Array.isArray(payload.options)
        ? payload.options
        : fallback.options;
      const defaultValue = payload.default !== undefined
        ? payload.default
        : fallback.defaultValue;
      const pendingLimit = (
        pendingGeneration
        && pendingGeneration.actor === actor
      ) ? pendingGeneration.contextLimit : null;
      const displayedOptions = [...options];
      if (
        pendingLimit !== null
        && !displayedOptions.some(
          (value) => Number(value) === Number(pendingLimit),
        )
      ) {
        displayedOptions.push(pendingLimit);
      }
      elements.contextSelect.replaceChildren();
      displayedOptions.forEach((value) => {
        const option = document.createElement('option');
        option.value = String(value);
        option.textContent = pendingLimit !== null
          && Number(value) === Number(pendingLimit)
          && !options.some(
            (offered) => Number(offered) === Number(pendingLimit),
          )
          ? `重试上次待确认请求（${value} 条）`
          : Number(payload.context_total) === 0
          ? '自由创作（0 条）'
          : Number(value) === Number(payload.context_total)
          ? `全部 ${value} 条`
          : `最近 ${value} 条`;
        option.selected = Number(value) === Number(
          pendingLimit === null ? defaultValue : pendingLimit,
        );
        elements.contextSelect.append(option);
      });
      if (!displayedOptions.length) {
        const option = document.createElement('option');
        option.value = '0';
        option.textContent = '自由创作（0 条）';
        elements.contextSelect.append(option);
      }
      elements.contextSelect.disabled = false;
      const snapshotEnd = Number(payload.learning_day_end);
      elements.contextSelect.dataset.learningDayEnd = (
        Number.isFinite(snapshotEnd) && snapshotEnd > 0
      ) ? String(snapshotEnd) : '';
      const total = Math.max(0, Number(payload.context_total) || 0);
      elements.contextMeta.textContent = total === 0
        ? '今天还没有可用消息，也可以让角色按个性自由创作。'
        : `从学习日 05:00 起，共有 ${total} 条可用消息。`;
      elements.learningDay.textContent = formatLearningDay(
        payload.learning_day_start,
      );
      return { stale: false };
    } catch (error) {
      const selected = elements.actorChoices.querySelector(
        'input[name="generator"]:checked',
      );
      const latest = {
        actor: selected ? selected.value : '',
        token: contextRequestToken,
      };
      if (!isCurrentContextRequest(requestMeta, latest)) {
        return { stale: true };
      }
      elements.contextMeta.textContent = error.message;
      elements.generateError.textContent = error.message;
      elements.generateError.hidden = false;
      return { stale: false, error: error.message };
    }
  }

  function formatLearningDay(timestamp) {
    if (!timestamp) return '学习日每天从 05:00 开始';
    const date = new Date(Number(timestamp) * 1000);
    if (Number.isNaN(date.getTime())) {
      return '学习日每天从 05:00 开始';
    }
    const day = new Intl.DateTimeFormat('zh-CN', {
      month: 'long',
      day: 'numeric',
    }).format(date);
    return `${day} 05:00 起算`;
  }

  function createRequestId() {
    return createUuidV4(window.crypto, Math.random);
  }

  async function submitGeneration(event) {
    event.preventDefault();
    if (state.generating) return;
    let actor = '';
    let contextLimit = 0;
    let ttsVoice = '';
    let generationRequest = pendingGeneration;
    if (!state.generationNeedsRefresh) {
      const data = new FormData(elements.generateForm);
      actor = String(data.get('generator') || '');
      contextLimit = Number(elements.contextSelect.value);
      ttsVoice = String(elements.voiceSelect.value || '').trim();
      if (!actor || !Number.isFinite(contextLimit)) return;
      if (!ttsVoice) {
        elements.generateError.textContent = (
          '请先选择一个用于朗读全部英文的音色。'
        );
        elements.generateError.hidden = false;
        return;
      }
      const snapshotEnd = Number(
        elements.contextSelect.dataset.learningDayEnd,
      );
      if (
        !pendingGeneration
        && (!Number.isFinite(snapshotEnd) || snapshotEnd <= 0)
      ) {
        elements.generateError.textContent = (
          '消息范围还没有准备好，请等待计数完成后再生成。'
        );
        elements.generateError.hidden = false;
        return;
      }
      let decision = selectGenerationRequest(
        pendingGeneration,
        actor,
        contextLimit,
        createRequestId,
        snapshotEnd,
        ttsVoice,
      );
      if (decision.status === 'conflict') {
        const startNew = window.confirm(
          '上次生成请求还没有得到服务器确认。要放弃它，并按当前角色、消息数量和音色开始一条新请求吗？',
        );
        if (!startNew) {
          elements.generateError.textContent = (
            '已保留上次待确认请求；恢复原角色、消息数量与音色即可安全重试。'
          );
          elements.generateError.hidden = false;
          return;
        }
        if (!updatePendingGeneration(null)) {
          elements.generateError.textContent = (
            '无法重置本地待确认请求，请检查浏览器存储后再试。'
          );
          elements.generateError.hidden = false;
          return;
        }
        decision = selectGenerationRequest(
          null,
          actor,
          contextLimit,
          createRequestId,
          snapshotEnd,
          ttsVoice,
        );
      }
      if (
        !decision.request
        || !updatePendingGeneration(decision.request)
      ) {
        elements.generateError.textContent = (
          '无法保存本次请求标识，因此尚未向服务器发送。请允许本地存储后重试。'
        );
        elements.generateError.hidden = false;
        return;
      }
      generationRequest = decision.request;
    }
    const confirmationRequestId = generationRequest
      ? generationRequest.requestId
      : '';
    state.generating = true;
    elements.generateSubmit.disabled = true;
    elements.generateSubmit.textContent = state.generationNeedsRefresh
      ? '正在同步最新卡片…'
      : '正在编织三幕…';
    elements.generateError.hidden = true;
    try {
      if (!state.generationNeedsRefresh) {
        await requestJson(`${API_ROOT}/packs`, {
          method: 'POST',
          body: JSON.stringify(generationPostPayload(generationRequest)),
        });
        state.generationNeedsRefresh = true;
      }
      const refreshResult = await loadAll();
      const confirmed = (
        confirmationRequestId
        && refreshResult.confirmedRequestId === confirmationRequestId
      );
      const presentation = generationRefreshPresentation({
        ok: refreshResult.ok && confirmed,
        error: refreshResult.error || (
          confirmed ? '' : '服务器尚未确认新卡片'
        ),
      });
      if (presentation.closeSheet) {
        state.generationNeedsRefresh = false;
        elements.generateDialog.close();
      }
      if (presentation.showSuccess) {
        generationNotice.show('新卡片已生成');
      } else {
        elements.generateError.textContent = presentation.error;
        elements.generateError.hidden = false;
      }
    } catch (error) {
      const settledPending = settlePendingGeneration(
        pendingGeneration,
        { type: 'request-error', status: error.status },
      );
      if (settledPending === null) {
        updatePendingGeneration(null);
        state.generationNeedsRefresh = false;
      }
      elements.generateError.textContent = error.message;
      elements.generateError.hidden = false;
    } finally {
      state.generating = false;
      elements.generateSubmit.disabled = false;
      elements.generateSubmit.disabled = !state.voicesReady;
      elements.generateSubmit.textContent = state.generationNeedsRefresh
        ? '重试同步最新卡片'
        : '生成三幕英语小剧场';
    }
  }

  function setupGestures() {
    elements.cardRegion.addEventListener('pointerdown', (event) => {
      if (
        event.button !== 0
        || event.target.closest('button, input, select, a, summary, details')
      ) {
        return;
      }
      gesture = {
        id: event.pointerId,
        x: event.clientX,
        y: event.clientY,
      };
      elements.cardRegion.setPointerCapture(event.pointerId);
    });
    elements.cardRegion.addEventListener('pointerup', (event) => {
      if (!gesture || gesture.id !== event.pointerId) return;
      const deltaX = event.clientX - gesture.x;
      const deltaY = event.clientY - gesture.y;
      gesture = null;
      if (
        Math.abs(deltaX) >= 56
        && Math.abs(deltaX) > Math.abs(deltaY) * 1.25
      ) {
        changeCard(deltaX < 0 ? 1 : -1);
      }
    });
    elements.cardRegion.addEventListener('pointercancel', () => {
      gesture = null;
    });
  }

  function bindElements() {
    [
      'archiveButton', 'learningCount', 'progressText', 'progressDots',
      'generateButton', 'statusBanner', 'statusMessage', 'statusRetry',
      'previousCard', 'nextCard', 'cardRegion', 'archiveDialog',
      'archiveClose', 'archiveLearningTab', 'archiveLearnedTab',
      'archiveList', 'generateDialog', 'generateClose', 'generateForm',
      'actorChoices', 'contextSelect', 'contextMeta', 'voiceSelect',
      'voiceMeta', 'learningDay', 'generationToast',
      'generateError', 'generateSubmit', 'undoToast', 'undoButton',
    ].forEach((id) => {
      elements[id] = byId(id);
    });
  }

  function bindEvents() {
    elements.archiveButton.addEventListener('click', openArchive);
    elements.generateButton.addEventListener('click', openGenerateSheet);
    elements.archiveClose.addEventListener(
      'click',
      () => elements.archiveDialog.close(),
    );
    elements.generateClose.addEventListener(
      'click',
      () => {
        if (!state.generating) elements.generateDialog.close();
      },
    );
    elements.previousCard.addEventListener('click', () => changeCard(-1));
    elements.nextCard.addEventListener('click', () => changeCard(1));
    elements.statusRetry.addEventListener('click', () => loadAll());
    elements.undoButton.addEventListener('click', undoLearn);
    elements.archiveLearningTab.addEventListener('click', () => {
      state.archiveTab = 'learning';
      renderArchive();
    });
    elements.archiveLearnedTab.addEventListener('click', () => {
      state.archiveTab = 'learned';
      renderArchive();
    });
    elements.generateForm.addEventListener('submit', submitGeneration);
    elements.voiceSelect.addEventListener('change', () => {
      const voice = String(elements.voiceSelect.value || '').trim();
      state.voicesReady = Boolean(voice);
      rememberVoice(voice);
      elements.generateSubmit.disabled = !voice;
    });
    document.addEventListener('keydown', (event) => {
      if (
        elements.archiveDialog.open
        || elements.generateDialog.open
        || event.target.closest('input, textarea, select, button')
      ) {
        return;
      }
      if (event.key === 'ArrowLeft') changeCard(-1);
      if (event.key === 'ArrowRight') changeCard(1);
    });
    window.addEventListener('offline', () => {
      setBanner(
        '你现在离线了，眼前的内容会好好保留。',
        'offline',
        true,
      );
    });
    window.addEventListener('online', () => {
      setBanner('网络已恢复，正在同步卡片…', 'success', false);
      loadAll({ preserveCardId: state.learning[state.index] && state.learning[state.index].id });
    });
    setupGestures();
  }

  function initialize() {
    bindElements();
    generationNotice = createTransientNoticeController(
      (message, visible) => {
        elements.generationToast.textContent = message;
        elements.generationToast.hidden = !visible;
        elements.generationToast.classList.toggle('is-visible', visible);
      },
      (callback, delay) => window.setTimeout(callback, delay),
      (timer) => window.clearTimeout(timer),
    );
    bindEvents();
    render();
    loadAll();
    loadVoiceOptions();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialize, { once: true });
  } else {
    initialize();
  }
}(typeof window !== 'undefined' ? window : globalThis));

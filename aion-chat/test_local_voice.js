const { test } = require('node:test');
const assert = require('node:assert/strict');
const create = require('./static/local-voice.js');

function fixture() {
  const calls = [];
  const host = { AionLocalAsr: {
    available: () => true, getMode: () => 'local',
    start: id => { calls.push(['start', id]); return true; },
    finish: id => calls.push(['finish', id]), cancel: id => calls.push(['cancel', id])
  }, addEventListener() {} };
  host.top = host;
  const api = create(host);
  return { api, calls, host, dispatch: e => host._aionLocalAsrDispatch(e) };
}

test('release waits for final text; partial results cannot send', async () => {
  const { api, calls, dispatch } = fixture();
  const seen = [];
  const s = api.start({ onText: t => seen.push(t) });
  dispatch({ id: s.id, type: 'partial', text: 'hello' });
  let settled = false;
  const result = s.finish().then(r => { settled = true; return r; });
  await Promise.resolve();
  assert.equal(settled, false);
  dispatch({ id: s.id, type: 'final', text: 'hello world', wav: 'AA==', duration: 1 });
  assert.equal((await result).text, 'hello world');
  assert.deepEqual(seen, ['hello']);
  s.cancel();
  assert.equal(calls.filter(c => c[0] === 'cancel').length, 0);
});

test('cancel discards queued final and does not affect next recording', async () => {
  const { api, dispatch } = fixture();
  const first = api.start({});
  const result = first.finish();
  first.cancel();
  const seen = [];
  const next = api.start({ onText: t => seen.push(t) });
  dispatch({ id: first.id, type: 'final', text: 'old' });
  dispatch({ id: next.id, type: 'partial', text: 'new' });
  assert.equal(await result, null);
  assert.deepEqual(seen, ['new']);
  next.cancel();
});

test('native error settles finishing and reports failure once', async () => {
  const { api, dispatch } = fixture();
  const errors = [];
  const s = api.start({ onError: e => errors.push(e) });
  const result = s.finish();
  dispatch({ id: s.id, type: 'error', message: 'busy' });
  dispatch({ id: s.id, type: 'error', message: 'late' });
  assert.equal(await result, null);
  assert.deepEqual(errors, ['busy']);
});

test('hiding a persistent iframe cancels pending finish and gates sending', async () => {
  const { api, calls, host, dispatch } = fixture();
  const s = api.start({});
  const result = s.finish();
  assert.equal(typeof host.onAionSubPageVisibilityChanged, 'function');
  host.onAionSubPageVisibilityChanged(false);
  dispatch({ id: s.id, type: 'final', text: 'stale' });
  assert.equal(await result, null);
  assert.equal(api.isVisible(), false);
  assert.equal(calls.filter(c => c[0] === 'cancel').length, 1);
  host.onAionSubPageVisibilityChanged(true);
  assert.equal(api.isVisible(), true);
});

test('ambient clip returns native text without opening another microphone', async () => {
  const { api, calls, host, dispatch } = fixture();
  host.AionLocalAsr.transcribeWav = (id, wav) => {
    calls.push(['clip', id, wav]);
    queueMicrotask(() => dispatch({ id, type: 'final', text: '刚刚说的话' }));
    return true;
  };
  const text = await api.transcribeBlob(new Blob(['wav'], { type: 'audio/wav' }));
  assert.equal(text, '刚刚说的话');
  assert.equal(calls.filter(c => c[0] === 'start').length, 0);
  assert.equal(calls[0][2], 'd2F2');
});

test('stopping ambient transcription discards a delayed native result', async () => {
  const { api, host, dispatch } = fixture();
  let started;
  const ready = new Promise(r => { started = r; });
  host.AionLocalAsr.transcribeWav = id => { started(id); return true; };
  const result = api.transcribeBlob(new Blob(['wav']));
  const rejected = assert.rejects(result, /取消/);
  const id = await ready;
  api.cancelTranscriptions();
  dispatch({ id, type: 'final', text: 'TTS 回声' });
  await rejected;
});

test('ambient ASR follows this device; native failure never silently uploads', async () => {
  const source = fs.readFileSync(`${__dirname}/static/chatroom.js`, 'utf8').replace(/\r\n/g, '\n');
  const start = source.indexOf('async function crAmbientTranscribe(');
  let local = true, fail = false, uploads = 0;
  const context = vm.createContext({
    console, FormData, window: { AionLocalVoice: {
      enabled: () => local,
      transcribeBlob: async () => { if (fail) throw new Error('native failed'); return '手机本地'; }
    } },
    fetch: async () => { uploads++; return { json: async () => ({ text: '云端结果' }) }; }
  });
  vm.runInContext(source.slice(start, source.indexOf('\nfunction crAmbientTextHasPhrase', start)), context);
  context.clip = new Blob(['audio'], { type: 'audio/wav' });
  assert.equal(await vm.runInContext('crAmbientTranscribe(clip)', context), '手机本地');
  assert.equal(uploads, 0);
  fail = true;
  await assert.rejects(vm.runInContext('crAmbientTranscribe(clip)', context), /native failed/);
  assert.equal(uploads, 0);
  local = false;
  assert.equal(await vm.runInContext('crAmbientTranscribe(clip)', context), '云端结果');
  assert.equal(uploads, 1);
  context.window = {};
  assert.equal(await vm.runInContext('crAmbientTranscribe(clip)', context), '云端结果');
});

// Exercise the actual two composer functions without loading the rest of the chat UI.
const fs = require('node:fs');
const vm = require('node:vm');
for (const [file, prefix, send, sending, target] of [
  ['chat', '_voice', '_voiceSendMessage', 'sending', 'currentConvId'],
  ['chatroom', '_crVoice', '_crVoiceSend', 'isSending', 'currentRoom.id']
]) {
  function composer() {
    const requests = [];
    const element = { classList: { remove() {} }, style: {}, value: '30', textContent: '' };
    const control = { begin: () => ({ controller: new AbortController() }),
      fetch: async (_, url, opts) => { requests.push({ url, body: JSON.parse(opts.body) }); return {}; },
      isCurrent: () => false };
    const context = vm.createContext({
      console, Date, Blob, FormData, AbortController, clearInterval, setTimeout,
      window: { AionLocalVoice: { wavBlob: () => new Blob(['test'], { type: 'audio/wav' }) } },
      document: { hidden: false, getElementById: () => null }, $: () => element,
      currentConvId: 'chat1', currentRoom: { id: 'room1' }, sending: false, isSending: false,
      crRequireModels: () => true, _getNativeBridge: () => null, alert() {}, toast() {},
      fetch: async url => { requests.push({ url }); return { json: async () => ({ url: '/voice.wav' }) }; },
      API: '/api/chatroom', _chatControl: control, _crControl: control,
      _showStopBtn() {}, upsertCurrentMessage() {}, renderMessages() {}, scrollBottom() {},
      _getMaxTokens: () => 100, _processSSEStream: async () => {}, whisperMode: false,
      ttsEnabled: false, ttsVoiceId: '', _clientId: '', crShowGenerationStop() {},
      playSend() {}, appendMessage: () => null, consumeChatroomSSE: async () => {},
      chatroomModel: 'a', chatroomConnorModel: 'b', crTtsEnabled: false,
      crTtsAionVoice: '', crTtsConnorVoice: ''
    });
    const source = fs.readFileSync(`${__dirname}/static/${file}.js`, 'utf8').replace(/\r\n/g, '\n');
    const start = source.indexOf(`let ${prefix}Mode =`);
    const last = source.indexOf(`async function ${send}(`);
    const end = source.indexOf('\n}\n', last) + 3;
    vm.runInContext(source.slice(start, end), context);
    return { context, requests, run: s => vm.runInContext(s, context) };
  }

  test(`${file}: cancel never finalizes or uploads; navigation discards final`, async () => {
    const { context, requests, run } = composer();
    let canceled = 0, finished = 0, resolve;
    context.session = { cancel: () => canceled++, finish: () => { finished++; return new Promise(r => resolve = r); } };
    run(`${prefix}Recording=true; ${prefix}StartTime=Date.now()-1000; ${prefix}LocalSession=session; ${prefix}Cancelled=true;`);
    await run(`${prefix}StopRecord()`);
    assert.equal(canceled, 1); assert.equal(finished, 0); assert.equal(requests.length, 0);
    run(`${prefix}Recording=true; ${prefix}StartTime=Date.now()-1000; ${prefix}LocalSession=session; ${prefix}Cancelled=false; ${prefix}Target=${target};`);
    const finish = run(`${prefix}StopRecord()`);
    run(`${target}='different'`);
    resolve({ text: 'do not send', wav: 'AA==', duration: 1 });
    await finish;
    assert.equal(requests.length, 0);
  });

  test(`${file}: sends native transcript without cloud transcription`, async () => {
    const { requests, run } = composer();
    await run(`${send}(new Blob(['audio'], {type:'audio/wav'}), 1, 'Hello 世界')`);
    assert.equal(requests.length, 2);
    assert.match(requests[0].url, /upload$/);
    assert.match(requests[1].url, /send$/);
    const body = requests[1].body;
    const attachment = (body.voice_attachments || body.attachments)[0];
    assert.equal(attachment.transcript, 'Hello 世界');
  });

  if (file === 'chatroom') test('chatroom: delayed ambient resume cannot start during another recording', async () => {
    const { context, run } = composer();
    let starts = 0;
    Object.assign(context, { crAmbientVoiceEnabled: true, crAmbientLocalArmed: true,
      crAmbientPausedForTts: false, _ttsEngine: null, crAmbientOwned: true,
      crAmbientIsCapturing: () => false, crAmbientStart: async () => starts++ });
    context.currentRoom.type = 'group';
    const source = fs.readFileSync(`${__dirname}/static/chatroom.js`, 'utf8').replace(/\r\n/g, '\n');
    const start = source.indexOf('async function crAmbientSyncRunning(');
    run(source.slice(start, source.indexOf('\nasync function crAmbientStart(', start)));
    run('_crVoiceRecording=true');
    await run('crAmbientSyncRunning()');
    assert.equal(starts, 0);
    run('_crVoiceRecording=false; _crVoiceFinishing=true');
    await run('crAmbientSyncRunning()');
    assert.equal(starts, 0);
  });
}

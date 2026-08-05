const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, 'static', 'theater.html'), 'utf8');
const regenerateBlock = html
  .split('/* ── 重新生成 ── */')[1]
  .split('/* ── 文件上传 ── */')[0];

test('regenerate sends the selected old message before tracking the new message', async () => {
  const fetchCalls = [];
  const toasts = [];
  const elements = {
    sendBtn: { disabled: false },
    contextSlider: { value: '20' },
    tempSlider: { value: '0.7' },
    modelSelect: { value: 'new-model' },
  };
  const context = {
    TextDecoder,
    console,
    document: {
      querySelectorAll() { return []; },
      querySelector() { return null; },
    },
    fetch: async (url, options) => {
      fetchCalls.push({ url, options });
      return {
        ok: true,
        body: { getReader: () => ({ read: async () => ({ done: true }) }) },
      };
    },
    showToast(message) { toasts.push(message); },
    discardMessageTTS() {},
    renderMessages() {},
  };
  context.globalElements = elements;
  vm.createContext(context);
  vm.runInContext(`
    const $ = id => globalElements[id];
    let currentConvId = 'tc_test';
    let currentPersonaId = 'new-persona';
    let currentMessages = [{ id: 'tm_old' }];
    let isStreaming = false;
    let ttsEnabled = false;
    let ttsVoice = '';
    ${regenerateBlock}
  `, context);

  await context.regenerateMsg('tm_old');

  assert.equal(fetchCalls.length, 1);
  assert.deepEqual(JSON.parse(fetchCalls[0].options.body), {
    message_id: 'tm_old',
    model: 'new-model',
    persona_id: 'new-persona',
  });
  assert.deepEqual(toasts, []);
});

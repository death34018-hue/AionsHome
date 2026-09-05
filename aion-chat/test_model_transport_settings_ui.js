const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'static', 'settings.html'), 'utf8');
const renderSource = source.match(/function renderModelTransportModes\(\) \{[\s\S]*?\n\}\n\nfunction collectModelTransportModes/)[0]
  .replace(/\n\nfunction collectModelTransportModes$/, '');

const list = { innerHTML: '' };
const context = {
  modelTransportRows: [
    { key: 'Codex-Sol', transport_mode: 'safe_live', supports_safe_live: true },
    { key: '普通中转模型', transport_mode: 'legacy', supports_safe_live: false },
  ],
  $: id => id === 'modelTransportModeList' ? list : null,
  escHtml: value => String(value),
  escAttr: value => String(value),
};

vm.runInNewContext(`${renderSource}\nrenderModelTransportModes();`, context);

assert.match(list.innerHTML, /Codex-Sol/);
assert.doesNotMatch(list.innerHTML, /普通中转模型/);
assert.doesNotMatch(list.innerHTML, /<select/);
assert.match(list.innerHTML, /<button[^>]*>旧版保守模式/);
assert.match(list.innerHTML, /<button[^>]*>安全实时流式/);

console.log('model transport settings UI tests passed');

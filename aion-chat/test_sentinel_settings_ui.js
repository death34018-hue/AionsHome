const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'static', 'settings.html'), 'utf8');
assert.match(source, /<option value="original">/);
assert.match(source, /<option value="codex_luna">GPT-6 Luna/);
assert.match(source, /sentinel_route:\s*\$\("sentinelRouteInput"\)\.value/);

const functionSource = source.match(/function updateSentinelRouteUi\(\) \{[\s\S]*?\n\}/)[0];
const inputs = [{ disabled: false }, { disabled: false }, { disabled: false }];
const elements = {
  sentinelRouteInput: { value: 'codex_luna' },
  sentinelOriginalFields: {
    style: {},
    querySelectorAll: () => inputs,
  },
  sentinelRouteHint: { textContent: '' },
};
const context = { $: id => elements[id] };
vm.runInNewContext(`${functionSource}\nupdateSentinelRouteUi();`, context);

assert.equal(elements.sentinelOriginalFields.style.opacity, '0.48');
assert.ok(inputs.every(input => input.disabled));
assert.match(elements.sentinelRouteHint.textContent, /推理固定关闭/);

console.log('sentinel settings UI tests passed');

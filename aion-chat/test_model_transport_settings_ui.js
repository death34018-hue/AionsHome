const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'static', 'settings.html'), 'utf8').replace(/\r\n/g, '\n');
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

assert.match(list.innerHTML, /已统一启用/);
assert.doesNotMatch(list.innerHTML, /<button|<select|旧版保守模式/);
console.log('unified model transport UI passed');

async function testSettingsLoadWithoutWaitingForModels() {
  const initSource = source.match(/\(async function init\(\) \{[\s\S]*?\n\}\)\(\);/)[0];
  const fields = new Map();
  const field = id => {
    if (!fields.has(id)) fields.set(id, { value: '' });
    return fields.get(id);
  };
  const saved = { gemini_key: 'saved-test-key', custom_model_routes: [{ id: 'saved-route' }],
    image_gen_provider: 'flowai', flowai_image_model: 'grok_imagine_image_2_0' };
  const initContext = {
    $: field,
    api: (method, url) => url === '/api/settings'
      ? Promise.resolve(saved)
      : new Promise(() => {}), // Model discovery can remain pending indefinitely.
    normalizeRoutesForUi: routes => routes,
    updateSentinelRouteUi() {},
    updateAsrProviderUi() {},
    renderCustomRoutes() {},
    renderModelTransportModes() {},
    refreshPhoneScreenStatus() {},
    refreshFloatingChatStatus() {},
    refreshMediaCacheStatus() {},
  };
  const imageUi = source.match(/function updateImageGenProviderUi\(\) \{[\s\S]*?\n\}/)[0];
  vm.runInNewContext(imageUi + '\n' + initSource, initContext);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(field('geminiKeyInput').value, saved.gemini_key,
    'Saved keys should display even while model discovery is pending');
  assert.deepEqual(initContext.customModelRoutes, saved.custom_model_routes);
  assert.equal(field('flowaiModelInput').value, 'grok_imagine_image_2_0');
  const options = source.match(/<select[^>]+id="flowaiModelInput"[^>]*>([\s\S]*?)<\/select>/)[1];
  assert.deepEqual(Array.from(options.matchAll(/value="([^"]+)"/g), m => m[1]),
    ['gpt_image2_5_flare', 'seedream_5_0_flash', 'grok_imagine_image_2_0']);
  assert.equal(field('imageGenFlowConfig').hidden, false);
  assert.equal(field('imageGenApiConfig').hidden, true);
  console.log('settings load independently of model discovery passed');
}

testSettingsLoadWithoutWaitingForModels().catch(error => {
  console.error(error);
  process.exitCode = 1;
});

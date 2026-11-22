const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, 'static/theater.html'), 'utf8');
const block = html.split('/* ── 角色管理 ── */')[1].split('/* ── 侧栏 ── */')[0];

function setup() {
  const elements = {};
  const calls = [];
  const context = vm.createContext({
    personas: [{id:'one', name:'角色甲', persona:'第一份设定'}, {id:'two', name:'角色乙', persona:'第二份设定'}],
    currentPersonaId:'one', currentConvId:'story', conversations:[{id:'story', persona_id:'one'}],
    $: id => elements[id] ||= {value:'', textContent:'', disabled:false, classList:{
      shown:false, contains() {return this.shown;}, add() {this.shown=true;}, remove() {this.shown=false;},
    }},
    document:{querySelectorAll:()=>Object.values(elements)},
    escHtml: value => String(value), confirm:()=>true, showToast() {}, closeModelSettings() {},
    api: async (method, url, data) => {
      calls.push({method, url, data});
      if (url === '/api/theater/prompt-modules') return data.prompt_modules;
      return method === 'DELETE' ? {ok:true} : {id:method === 'POST' ? 'created' : url.split('/').pop(), ...data};
    },
  });
  context.$('personaSave');
  vm.runInContext(block, context);
  return {context, elements, calls};
}

test('save stays open and switching to another role keeps edits separate from the active role', async () => {
  const {context:c, elements:e, calls} = setup();
  c.openPersonaModal();
  const longText = '长篇设定\n'.repeat(1200);
  e.personaText.value = longText;
  await c.savePersona();
  assert.equal(e.personaModalOverlay.classList.shown, true);
  assert.equal(e.personaSaveStatus.textContent, '已保存');
  c.openPersonaModal('two');
  assert.equal(e.personaText.value, '第二份设定');
  e.personaText.value = '第二个角色的新设定';
  await c.savePersona();
  c.openPersonaModal('one');
  assert.equal(e.personaText.value, longText);
  assert.equal(c.personas[1].persona, '第二个角色的新设定');
  assert.equal(c.currentPersonaId, 'one');
  assert.deepEqual(calls.map(call => call.url), ['/api/theater/personas/one', '/api/theater/personas/two']);
});

test('a newly saved role becomes editable in place and subsequent saves update it', async () => {
  const {context:c, elements:e, calls} = setup();
  c.openPersonaModal('');
  e.personaName.value = '新角色'; e.personaText.value = '新设定';
  await c.savePersona();
  assert.equal(e.personaEditId.value, 'created');
  e.personaText.value = '补充设定';
  await c.savePersona();
  assert.equal(c.personas.length, 3);
  assert.deepEqual(calls.map(call => call.method), ['POST', 'PUT']);
  assert.equal(e.personaModalOverlay.classList.shown, true);
});

test('the header shows the active role, follows saved names and selected model, and ignores the role being edited', async () => {
  const {context:c, elements:e} = setup();
  c.openPersonaModal('two');
  assert.equal(e.theaterSelectedPersona.textContent, '角色甲');
  e.modelSelect.value = 'provider/model-long-name';
  c.updateTheaterSelectionSummary();
  assert.equal(e.theaterSelectedModel.textContent, 'provider/model-long-name');
  await c.useEditingPersona();
  assert.equal(e.theaterSelectedPersona.textContent, '角色乙');
  e.personaName.value = '角色乙的新名字';
  await c.savePersona();
  assert.equal(e.theaterSelectedPersona.textContent, '角色乙的新名字');
  assert.equal(e.theaterSelectionSummary.title, '人设：角色乙的新名字 · 模型：provider/model-long-name');
});

test('failed saves and cancelled switches preserve the unsaved draft', async () => {
  const {context:c, elements:e} = setup();
  c.openPersonaModal('one');
  e.personaText.value = '未保存的人设';
  c.api = async () => ({error:'not found'});
  await c.savePersona();
  assert.equal(e.personaText.value, '未保存的人设');
  assert.equal(c.personas[0].persona, '第一份设定');
  assert.match(e.personaSaveStatus.textContent, /保存失败/);
  assert.equal(e.personaSave.disabled, false);
  c.confirm = () => false;
  c.openPersonaModal('two');
  assert.equal(e.personaEditId.value, 'one');
  assert.equal(e.personaList.value, 'one');
  assert.equal(c.closePersonaModal(), false);
  assert.equal(e.personaModalOverlay.classList.shown, true);
});

test('using a role is explicit and deleting the final role leaves the manager ready to create', async () => {
  const {context:c, elements:e, calls} = setup();
  c.openPersonaModal('two');
  await c.useEditingPersona();
  assert.equal(c.currentPersonaId, 'two');
  assert.equal(calls[0].url, '/api/theater/conversations/story');
  await c.deletePersona('two');
  assert.equal(e.personaEditId.value, 'one');
  await c.deletePersona('one');
  assert.equal(e.personaModalOverlay.classList.shown, true);
  assert.equal(e.personaEditId.value, '');
  assert.equal(e.personaText.value, '');
});

test('applyLoadedPromptModules populates both boxes and avoids clobbering unsaved edits', () => {
  const {context:c, elements:e} = setup();
  c.applyLoadedPromptModules([
    { id: 'writing_prompt_1', title: '写作提示1', enabled: true, content: '已加载提示一' },
    { id: 'writing_prompt_2', title: '写作提示2', enabled: false, content: '已加载禁令二' },
  ]);
  assert.equal(e.personaPromptEnabled1.checked, true);
  assert.equal(e.personaPromptText1.value, '已加载提示一');
  assert.equal(e.personaPromptState1.textContent, '已勾选 · 发送时附带');
  assert.equal(e.personaPromptEnabled2.checked, false);
  assert.equal(e.personaPromptText2.value, '已加载禁令二');
  assert.equal(e.personaPromptState2.textContent, '未勾选 · 不发送');

  c.openPersonaModal('one');
  e.personaPromptText1.value = '正在编辑的提示一';
  c.personaEdited();
  c.applyLoadedPromptModules([
    { id: 'writing_prompt_1', title: '写作提示1', enabled: false, content: '晚到的服务端旧数据' },
  ]);
  assert.equal(e.personaPromptText1.value, '正在编辑的提示一');
  assert.equal(e.personaSaveStatus.textContent, '未保存');
});

test('editing only shared writing prompts saves only prompt-modules and keeps role switching smooth', async () => {
  const {context:c, elements:e, calls} = setup();
  let confirmCalls = 0;
  c.confirm = () => { confirmCalls++; return false; };

  c.openPersonaModal('two');
  assert.equal(e.personaUse.disabled, false);

  e.personaPromptEnabled1.checked = true;
  e.personaPromptText1.value = '全局文风提示';
  c.personaEdited();
  assert.equal(e.personaSaveStatus.textContent, '未保存');
  assert.equal(e.personaUse.disabled, true);
  assert.equal(c.closePersonaModal(), false);
  assert.equal(confirmCalls, 1);

  // Switching to another role or creating a new role keeps unsaved shared prompt edits intact without a role-discard prompt.
  c.openPersonaModal('one');
  assert.equal(confirmCalls, 1);
  assert.equal(e.personaEditId.value, 'one');
  assert.equal(e.personaPromptEnabled1.checked, true);
  assert.equal(e.personaPromptText1.value, '全局文风提示');
  assert.equal(e.personaSaveStatus.textContent, '未保存');

  c.openPersonaModal('');
  assert.equal(e.personaEditId.value, '');
  assert.equal(e.personaPromptEnabled1.checked, true);
  assert.equal(e.personaPromptText1.value, '全局文风提示');

  await c.savePersona();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, 'PUT');
  assert.equal(calls[0].url, '/api/theater/prompt-modules');
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].data.prompt_modules)), [
    { id: 'writing_prompt_1', title: '写作提示1', enabled: true, content: '全局文风提示' },
    { id: 'writing_prompt_2', title: '写作提示2', enabled: false, content: '' },
  ]);
  assert.equal(c.personas.length, 2); // No bogus empty role created.
  assert.equal(e.personaSaveStatus.textContent, '已保存');

  c.openPersonaModal('two');
  assert.equal(e.personaPromptEnabled1.checked, true);
  assert.equal(e.personaPromptText1.value, '全局文风提示');
  assert.equal(e.personaSaveStatus.textContent, '');
});

test('deleting final role keeps shared writing prompts and saving both role and modules handles failures cleanly', async () => {
  const {context:c, elements:e, calls} = setup();
  c.applyLoadedPromptModules([
    { id: 'writing_prompt_1', title: '写作提示1', enabled: true, content: '长期保留提示' },
  ]);
  c.openPersonaModal('one');
  e.personaText.value = '更新后的人设甲';
  e.personaPromptEnabled2.checked = true;
  e.personaPromptText2.value = '新增禁令二';
  c.personaEdited();
  await c.savePersona();

  assert.deepEqual(calls.map(call => `${call.method} ${call.url}`), [
    'PUT /api/theater/personas/one',
    'PUT /api/theater/prompt-modules',
  ]);
  assert.equal(e.personaSaveStatus.textContent, '已保存');

  await c.deletePersona('two');
  await c.deletePersona('one');
  assert.equal(e.personaEditId.value, '');
  assert.equal(e.personaName.value, '');
  assert.equal(e.personaText.value, '');
  assert.equal(e.personaPromptEnabled1.checked, true);
  assert.equal(e.personaPromptText1.value, '长期保留提示');
  assert.equal(e.personaPromptEnabled2.checked, true);
  assert.equal(e.personaPromptText2.value, '新增禁令二');

  // Failed prompt-modules save keeps edits intact.
  e.personaPromptText2.value = '未保存成功的禁令';
  c.personaEdited();
  c.api = async () => ({ error: 'disk error' });
  await c.savePersona();
  assert.equal(e.personaPromptText2.value, '未保存成功的禁令');
  assert.match(e.personaSaveStatus.textContent, /保存失败/);
});

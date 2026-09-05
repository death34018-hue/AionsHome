const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('AI photo details show the saved prompt; uploads hide it and clear stale text', () => {
  const html = fs.readFileSync(path.join(__dirname, 'static/album.html'), 'utf8');
  const js = fs.readFileSync(path.join(__dirname, 'static/album.js'), 'utf8');
  const nodes = Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(match => [match[1], {}]));
  nodes.detailsDialog.showModal = () => {};
  let photo = { source: 'generated', title: '', taken_on: '2026-08-30', prompt: '原始提示词\n<保留文字，不作为 HTML>' };
  const handler = js.match(/\$\('detailsButton'\)\.onclick = (\(\) => \{[\s\S]*?\n  \});/)[1];
  const openDetails = vm.runInNewContext('(' + handler + ')', { $: id => nodes[id], current: () => photo, state: {}, loadPhotoViews: () => {} });
  openDetails();
  assert.equal(nodes.promptSection?.hidden, false, 'AI 照片详情应显示提示词');
  assert.equal(nodes.promptText.textContent, photo.prompt);
  photo = { ...photo, prompt: '' };
  openDetails();
  assert.equal(nodes.promptText.textContent, '（无）');
  photo = { ...photo, source: 'upload', prompt: '不应显示的旧内容' };
  openDetails();
  assert.equal(nodes.promptSection.hidden, true);
  assert.equal(nodes.promptText.textContent, '');
});

test('details loads configured viewer names and does not report unread on a failed request', async () => {
  const js = fs.readFileSync(path.join(__dirname, 'static/album.js'), 'utf8');
  const match = js.match(/  async function loadPhotoViews\(photoId\) \{[\s\S]*?\n  \}/);
  assert.ok(match, 'details must fetch current view status');
  const nodes = { photoViewStatus: {}, detailsDialog: { open: true } };
  let result = { viewed_by: [] };
  let fail = false;
  const context = vm.createContext({ $: id => nodes[id], current: () => ({ id: 'photo-1' }), detailViewRequest: 0,
    api: async url => { assert.equal(url, '/photos/photo-1'); if (fail) throw Error('offline'); return result; } });
  vm.runInContext(match[0], context);
  await context.loadPhotoViews('photo-1');
  assert.equal(nodes.photoViewStatus.textContent, '尚未回看');
  result = { viewed_by: [{ name: '星河' }, { name: '月光' }] };
  await context.loadPhotoViews('photo-1');
  assert.equal(nodes.photoViewStatus.textContent, '已回看 · 星河、月光');
  fail = true;
  await context.loadPhotoViews('photo-1');
  assert.equal(nodes.photoViewStatus.textContent, '回看状态暂不可用');
});

test('outside dismissal distinguishes panel padding and preserves unsaved edits', () => {
  const js = fs.readFileSync(path.join(__dirname, 'static/album.js'), 'utf8');
  let closed = false;
  let discard = false;
  const dialog = { close: () => { closed = true; }, getBoundingClientRect: () => ({left:0,right:390,top:400,bottom:844}) };
  const state = { dirty: true, detailBusy: false };
  const context = vm.createContext({ $: () => dialog, state, confirm: () => discard });
  vm.runInContext(js.match(/  function outsideDetails\(event\) \{[\s\S]*?\n  \}/)[0], context);
  vm.runInContext(js.match(/  function closeDetails\(\) \{[\s\S]*?\n  \}/)[0], context);
  assert.equal(context.outsideDetails({target:dialog,clientX:100,clientY:200}), true);
  assert.equal(context.outsideDetails({target:dialog,clientX:100,clientY:500}), false);
  assert.equal(context.closeDetails(), false);
  assert.equal(closed, false);
  discard = true;
  assert.equal(context.closeDetails(), true);
  assert.equal(closed, true);
  closed = false; state.detailBusy = true;
  assert.equal(context.closeDetails(), false);
  assert.equal(closed, false);
});

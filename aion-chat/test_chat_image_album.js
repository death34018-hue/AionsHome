const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup(fetch) {
  const filename = path.join(__dirname, 'static/chat-image-album.js');
  assert.ok(fs.existsSync(filename), '聊天图片应提供添加到相册操作');
  const context = {
    window: {}, fetch, FormData, URL,
    location: { href: 'http://localhost/chat' },
    document: { createElement: () => ({ disabled: false, setAttribute() {} }) },
  };
  vm.runInNewContext(fs.readFileSync(filename, 'utf8'), context);
  return context.window.ChatImageAlbum.createButton('/uploads/example.png');
}

test('adds the original bytes through the manual album upload endpoint', async () => {
  const bytes = new Uint8Array([137, 80, 78, 71, 1, 2, 3]);
  let uploaded;
  const button = setup(async (url, options) => {
    if (!options) {
      assert.equal(url, '/uploads/example.png');
      return new Response(bytes, { headers: { 'Content-Type': 'image/png' } });
    }
    assert.equal(url, '/api/album/upload');
    assert.equal(options.method, 'POST');
    uploaded = options.body;
    return Response.json({ id: 'photo-1', source: 'upload' });
  });
  await button.onclick();
  assert.deepEqual(new Uint8Array(await uploaded.get('file').arrayBuffer()), bytes);
  assert.equal(uploaded.get('file').name, 'example.png');
  assert.equal(uploaded.has('taken_on'), false, '服务器默认当天，之后可修改');
  assert.equal(button.textContent, '已添加到相册');
  assert.equal(button.disabled, true);
});

test('failure is visible and the same action can be retried', async () => {
  let shouldFail = true;
  const button = setup(async (url, options) => {
    if (!options) return new Response('image', { headers: { 'Content-Type': 'image/png' } });
    return shouldFail ? Response.json({ detail: '磁盘空间不足' }, { status: 400 }) : Response.json({ id: 'photo-2' });
  });
  await button.onclick();
  assert.equal(button.disabled, false);
  assert.match(button.textContent, /磁盘空间不足/);
  shouldFail = false;
  await button.onclick();
  assert.equal(button.textContent, '已添加到相册');
});

test('rapid clicks while adding submit only one copy', async () => {
  let release;
  let uploads = 0;
  const wait = new Promise(resolve => { release = resolve; });
  const button = setup(async (url, options) => {
    if (!options) { await wait; return new Response('image'); }
    uploads++;
    return Response.json({ id: 'photo-3' });
  });
  const first = button.onclick();
  await button.onclick();
  release();
  await first;
  assert.equal(uploads, 1);
});

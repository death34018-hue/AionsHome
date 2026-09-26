const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const context = { URL, window: { location: { href: 'https://chat.test/chat', origin: 'https://chat.test' } } };
vm.runInNewContext(fs.readFileSync(`${__dirname}/static/chat-image-preview.js`, 'utf8'), context);
const preview = context.window.ChatImagePreview;

test('local attachment previews are lazy thumbnails, with original URL retained', () => {
  const attrs = preview.attributes('/uploads/photo.jpg');
  assert.match(attrs, /src="\/api\/chat-media\/thumbnail\?src=/);
  assert.match(attrs, /data-original-src="\/uploads\/photo.jpg"/);
  assert.match(attrs, /loading="lazy"/);
  assert.equal(preview.original({ dataset: { originalSrc: '/uploads/photo.jpg' }, src: '/thumb' }), '/uploads/photo.jpg');
  assert.equal(preview.original({ src: '/original' }), '/original');
  assert.match(preview.attributes('/uploads/a" onload="alert(1).jpg'), /&quot;/);
});

test('remote images and GIFs retain their source and still load lazily', () => {
  for (const url of ['https://other.test/a.jpg', '/uploads/moving.gif']) {
    assert.ok(preview.attributes(url).startsWith(`src="${url}"`));
    assert.match(preview.attributes(url), /loading="lazy"/);
  }
});

test('both chat click and long-press handlers use the original image', () => {
  for (const name of ['chat', 'chatroom']) {
    const source = fs.readFileSync(`${__dirname}/static/${name}.js`, 'utf8');
    const open = source.match(/function openImageFromElement\([^]*?\r?\n\}/)[0];
    const attrs = source.match(/function imageInteractionAttrs\([^]*?\r?\n\}/)[0];
    const calls = [];
    const ctx = { window: context.window, ChatImagePreview: preview, imageLongPressSuppressClickUntil: 0, openImageViewer: url => calls.push(url) };
    vm.runInNewContext(`${open}\n${attrs}`, ctx);
    ctx.openImageFromElement(null, { dataset: { originalSrc: '/uploads/original.jpg' }, src: '/thumbnail' });
    assert.deepEqual(calls, ['/uploads/original.jpg']);
    assert.match(ctx.imageInteractionAttrs(), /original\(this\)/);
  }
});

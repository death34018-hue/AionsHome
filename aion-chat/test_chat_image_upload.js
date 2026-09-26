const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

let browser, page;
before(async () => {
  browser = await chromium.launch({ headless: true });
  page = await browser.newPage();
  await page.route('http://image-upload.test/', route => route.fulfill({ body: '<html></html>', contentType: 'text/html' }));
  await page.goto('http://image-upload.test/');
  const script = path.join(__dirname, 'static/chat-image-upload.js');
  if (fs.existsSync(script)) await page.addScriptTag({ path: script });
});
after(async () => { await browser?.close(); });

test('large phone-sized image is compressed locally to at most 400 KiB', async () => {
  assert.equal(await page.evaluate(() => typeof window.ChatImageUpload), 'object');
  const result = await page.evaluate(async () => {
    const canvas = document.createElement('canvas');
    canvas.width = 4000; canvas.height = 3000;
    const ctx = canvas.getContext('2d');
    const pixels = ctx.createImageData(canvas.width, canvas.height);
    let seed = 42;
    for (let i = 0; i < pixels.data.length; i += 4) {
      seed = (seed * 1664525 + 1013904223) >>> 0;
      pixels.data[i] = seed & 255;
      pixels.data[i + 1] = (seed >>> 8) & 255;
      pixels.data[i + 2] = (seed >>> 16) & 255;
      pixels.data[i + 3] = 255;
    }
    ctx.putImageData(pixels, 0, 0);
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.96));
    window.largePhoto = new File([blob], 'phone.jpg', { type: blob.type });
    const output = await ChatImageUpload.compress(largePhoto);
    const decoded = await createImageBitmap(output);
    const result = { before: blob.size, after: output.size, width: decoded.width, height: decoded.height, type: output.type };
    decoded.close();
    return result;
  });
  assert.ok(result.before > 4 * 1024 * 1024);
  assert.ok(result.after <= 400 * 1024);
  assert.ok(Math.max(result.width, result.height) <= 1600);
  assert.equal(result.width / result.height, 4 / 3);
  assert.equal(result.type, 'image/jpeg');
  console.log('Phone photo compression:', result);
});

test('small images, GIFs and non-images retain their original bytes', async () => {
  const unchanged = await page.evaluate(async () => {
    const files = [new File(['small'], 'small.png', { type: 'image/png' }), new File([new Uint8Array(600000)], 'animation.gif', { type: 'image/gif' }), new File(['voice'], 'voice.webm', { type: 'audio/webm' })];
    return Promise.all(files.map(async file => await ChatImageUpload.compress(file) === file));
  });
  assert.deepEqual(unchanged, [true, true, true]);
});

test('preview exists before upload and request carries only the compressed file', async () => {
  let bytes = 0;
  await page.route('**/api/upload', async route => {
    bytes = route.request().postDataBuffer().length;
    await route.fulfill({ json: { url: '/uploads/test.jpg', type: 'image/jpeg', name: 'phone.jpg' } });
  });
  const result = await page.evaluate(async () => {
    const list = [], states = [];
    await ChatImageUpload.add('/api/upload', largePhoto, list, () => states.push({ uploading: !!list[0]?.uploading, preview: list[0]?.previewUrl, url: list[0]?.url }));
    const item = list[0];
    const output = { states, url: item.url, preview: item.previewUrl, uploading: item.uploading };
    ChatImageUpload.release(item);
    return output;
  });
  assert.ok(bytes < 410 * 1024);
  assert.equal(result.states[0].uploading, true);
  assert.match(result.states[0].preview, /^blob:/);
  assert.equal(result.states[0].url, undefined);
  assert.equal(result.url, '/uploads/test.jpg');
  assert.equal(result.uploading, false);
  assert.match(result.preview, /^blob:/);
});

test('failed upload removes pending item and permits another attempt', async () => {
  await page.route('**/api/upload', route => route.fulfill({ status: 524, contentType: 'text/html', body: '<html>timeout</html>' }));
  const result = await page.evaluate(async () => {
    const list = [], errors = [];
    for (let i = 0; i < 2; i++) {
      try { await ChatImageUpload.add('/api/upload', new File(['small'], 'small.jpg', { type: 'image/jpeg' }), list, () => {}); }
      catch (e) { errors.push(e.message); }
    }
    return { count: list.length, errors };
  });
  assert.equal(result.count, 0);
  assert.equal(result.errors.length, 2);
  assert.match(result.errors[0], /524/);
  assert.doesNotMatch(result.errors[0], /Unexpected|<html>/);
});

test('removing an image while compressing never uploads or restores it', async () => {
  let requests = 0;
  await page.route('**/api/upload', route => { requests++; return route.fulfill({ json: { url: '/uploads/test.jpg' } }); });
  const count = await page.evaluate(async () => {
    const list = [];
    const pending = ChatImageUpload.add('/api/upload', largePhoto, list, () => {});
    ChatImageUpload.release(list[0]);
    list.splice(0, 1);
    await pending;
    return list.length;
  });
  assert.equal(count, 0);
  assert.equal(requests, 0);
});

test('both chat file selectors display local previews and allow removing attachments', async () => {
  await page.route('**/api/**/upload', route => route.fulfill({ json: { url: '/uploads/test.jpg', type: 'image/jpeg' } }));
  await page.route('**/api/upload', route => route.fulfill({ json: { url: '/uploads/test.jpg', type: 'image/jpeg' } }));
  for (const room of [false, true]) {
    const source = fs.readFileSync(path.join(__dirname, `static/${room ? 'chatroom' : 'chat'}.js`), 'utf8');
    const selector = room ? 'handleChatroomFileSelect' : 'handleFileSelect';
    const remove = room ? 'removeChatroomAttachment' : 'removeAttachment';
    const functions = [selector, 'renderPreview', remove].map(name => {
      const match = source.match(new RegExp(`(?:async )?function ${name}\\([^]*?\\r?\\n\\}`));
      assert.ok(match, name);
      return match[0];
    }).join('\n');
    const result = await page.evaluate(async ({ functions, selector, remove }) => {
      document.body.innerHTML = '<div id="previewArea"></div>';
      window.$ = id => document.getElementById(id);
      window.esc = window.escHtml = value => String(value).replaceAll('"', '&quot;');
      window.API = '/api/chatroom';
      window.pendingAttachments = [];
      window.alert = window.toast = message => { throw new Error(message); };
      (0, eval)(functions);
      const input = { files: [largePhoto], value: 'selected' };
      await window[selector](input);
      const result = { reset: input.value, count: pendingAttachments.length, src: document.querySelector('#previewArea img').src, uploading: pendingAttachments[0].uploading };
      window[remove](0);
      result.removed = pendingAttachments.length === 0 && document.getElementById('previewArea').innerHTML === '';
      return result;
    }, { functions, selector, remove });
    assert.equal(result.reset, '');
    assert.equal(result.count, 1);
    assert.match(result.src, /^blob:/);
    assert.equal(result.uploading, false);
    assert.equal(result.removed, true);
  }
});

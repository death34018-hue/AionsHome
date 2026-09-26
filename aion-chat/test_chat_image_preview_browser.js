const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { chromium } = require('playwright');

test('a long chat downloads nearby thumbnails; opening the viewer requests the original', async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 700 } });
    const requests = [];
    await page.route('http://chat.test/**', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/') return route.fulfill({ contentType: 'text/html', body: '<div id="messages" style="height:500px;overflow:auto"></div><img id="viewer" hidden>' });
      requests.push(url.pathname + url.search);
      return route.fulfill({ contentType: 'image/png', body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9uoAAAAASUVORK5CYII=', 'base64') });
    });
    await page.goto('http://chat.test/');
    await page.addScriptTag({ path: path.join(__dirname, 'static/chat-image-preview.js') });
    await page.evaluate(() => {
      const box = document.getElementById('messages');
      box.innerHTML = Array.from({ length: 40 }, (_, i) => `<div style="height:220px"><img ${ChatImagePreview.attributes('/uploads/' + i + '.jpg')}></div>`).join('');
      box.scrollTop = box.scrollHeight;
    });
    await page.waitForFunction(() => document.querySelector('#messages div:last-child img').naturalWidth > 0);
    const thumbnails = requests.filter(url => url.startsWith('/api/chat-media/thumbnail'));
    assert.ok(thumbnails.length > 0 && thumbnails.length < 20, `loaded ${thumbnails.length} of 40 previews`);
    assert.equal(requests.some(url => url.startsWith('/uploads/')), false);
    await page.evaluate(() => {
      const viewer = document.getElementById('viewer');
      viewer.hidden = false;
      viewer.src = ChatImagePreview.original(document.querySelector('#messages div:last-child img'));
    });
    await page.waitForFunction(() => document.getElementById('viewer').naturalWidth > 0);
    assert.ok(requests.includes('/uploads/39.jpg'));
    console.log(`Visible chat loaded ${thumbnails.length}/40 thumbnails and zero originals before opening viewer`);
  } finally { await browser.close(); }
});

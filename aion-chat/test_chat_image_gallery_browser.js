const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

async function desktopChat(t, entry) {
  const browser = await chromium.launch({ channel: 'chrome' });
  t.after(() => browser.close());
  const page = await browser.newPage();
  const original = Buffer.from(await page.evaluate(() => {
    const canvas = document.createElement('canvas');
    canvas.width = 32; canvas.height = 24;
    canvas.getContext('2d').fillRect(0, 0, 32, 24);
    return canvas.toDataURL('image/jpeg').split(',')[1];
  }), 'base64');
  await page.routeWebSocket('**/*', () => {});
  await page.route('**/*', route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === '/api/chat-media/thumbnail') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==', 'base64') });
    if (pathname === '/uploads/photo.jpg') return route.fulfill({ contentType: 'image/jpeg', body: original });
    if (pathname === '/chat' || pathname === '/chatroom') return route.fulfill({ path: path.join(__dirname, 'static', pathname + '.html') });
    if (pathname.startsWith('/static/') && fs.existsSync(path.join(__dirname, pathname))) return route.fulfill({ path: path.join(__dirname, pathname) });
    return route.fulfill({ json: [] });
  });
  await page.goto('http://localhost' + entry, { waitUntil: 'domcontentloaded' });
  const chat = entry.includes('chatroom')
    ? await (await page.waitForSelector('iframe[data-startup-path="/chatroom"]')).contentFrame()
    : page;
  await chat.waitForFunction(() => typeof renderAttachments === 'function');
  await chat.evaluate(() => {
    const box = document.createElement('div');
    box.style.cssText = 'position:fixed;top:10px;left:10px;z-index:9999';
    box.innerHTML = renderAttachments(['/uploads/photo.jpg?size=original']);
    document.body.append(box);
  });
  return { page, chat, original };
}

test('saving a chatroom preview inside the desktop shell downloads the selected original', async t => {
  const { page, chat, original } = await desktopChat(t, '/chat?page=/chatroom');
  await chat.locator('img[data-original-src]').click();
  const [download] = await Promise.all([
    page.waitForEvent('download', { timeout: 4000 }),
    chat.getByRole('button', { name: '保存图片', exact: true }).click(),
  ]);
  assert.equal(download.suggestedFilename(), 'photo.jpg');
  assert.deepEqual(fs.readFileSync(await download.path()), original);
});

test('right-clicking a private chat image copies its full-size pixels as PNG', async t => {
  const { chat } = await desktopChat(t, '/chat?conv=1');
  // Capture only this test's write, leaving the user's system clipboard alone.
  await chat.evaluate(() => {
    navigator.clipboard.write = async ([item]) => {
      const bitmap = await createImageBitmap(await item.getType('image/png'));
      window.copiedSize = [bitmap.width, bitmap.height];
      bitmap.close();
    };
  });
  await chat.locator('img[data-original-src]').click({ button: 'right' });
  const copy = chat.getByRole('button', { name: '复制图片', exact: true });
  assert.equal(await copy.count(), 1);
  await copy.click();
  await chat.waitForFunction(() => window.copiedSize);
  assert.deepEqual(await chat.evaluate(() => window.copiedSize), [32, 24]);
});

async function setup(t, chat = 'chat') {
  const browser = await chromium.launch({ headless: true, channel: 'chrome' });
  t.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width: 390, height: 780 }, hasTouch: true });
  await page.route('http://gallery.test/**', route => route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="800"><rect width="600" height="800" fill="coral"/></svg>' }));
  await page.setContent('<main class="msg-row message-row user"><div class="msg-content" id="messages"></div></main>');
  for (const name of [chat + '.css', 'image-viewer.css']) {
    const file = path.join(__dirname, 'static', name);
    if (fs.existsSync(file)) await page.addStyleTag({ path: file });
  }
  for (const name of ['chat-image-preview.js', 'image-viewer.js']) {
    const file = path.join(__dirname, 'static', name);
    if (fs.existsSync(file)) await page.addScriptTag({ path: file });
  }
  const source = fs.readFileSync(path.join(__dirname, 'static', chat + '.js'), 'utf8');
  const functions = ['renderAttachments', 'imageInteractionAttrs', 'openImageFromElement', 'openImageViewer'].map(name => source.match(new RegExp('function ' + name + '\\([^]*?\\r?\\n\\}'))[0]).join('\n');
  await page.addScriptTag({ content: `let imageLongPressSuppressClickUntil = 0; const worldBook = {}; const aiName = 'AI'; const escHtml = s => s; const esc = s => s; function startImageLongPress() {} function cancelImageLongPress() {} function moveImageLongPress() {} function saveImage() {} function showImageSaveMenu(url) { window.saveMenuOriginal = url; }\n${functions}` });
  return page;
}

for (const chat of ['chat', 'chatroom']) test(`${chat}: multiple attachments stack, expand vertically and open the selected original`, async t => {
  const page = await setup(t, chat);
  await page.evaluate(() => messages.innerHTML = renderAttachments(['http://gallery.test/one.png', 'http://gallery.test/two.png', 'http://gallery.test/three.png']));
  await page.locator('.chat-image-group img').first().evaluate(img => img.decode());
  const toggle = page.getByRole('button', { name: /展开/ });
  assert.equal(await toggle.count(), 1, 'multiple images must have an expand control');
  const before = await page.locator('.chat-image-group').boundingBox();
  const button = await toggle.boundingBox();
  const first = await page.locator('.chat-image-group img').first().boundingBox();
  assert.ok(button.x + button.width <= first.x, 'control stays to the left');
  await toggle.click();
  const expanded = await page.locator('.chat-image-group').boundingBox();
  assert.ok(expanded.height > before.height * 2);
  assert.equal(await page.getByRole('button', { name: /收起/ }).count(), 1);
  await page.locator('.chat-image-group img').nth(1).click();
  assert.match(await page.locator('.aion-image-viewer img').getAttribute('src'), /two.png$/);
  await page.getByRole('button', { name: '下一张', exact: true }).click();
  assert.match(await page.locator('.aion-image-viewer img').getAttribute('src'), /three.png$/);
  await page.mouse.move(195, 350);
  await page.mouse.down();
  await page.waitForFunction(() => window.saveMenuOriginal);
  await page.mouse.up();
  assert.equal(await page.evaluate(() => window.saveMenuOriginal), 'http://gallery.test/three.png', 'long press saves the current original after navigation');
  assert.equal(await page.locator('.aion-image-viewer').count(), 1, 'long press does not close the viewer');
  await page.getByRole('button', { name: '关闭大图', exact: true }).click();
  await page.getByRole('button', { name: /收起/ }).click();
  assert.equal((await page.locator('.chat-image-group').boundingBox()).height, before.height);
  await page.evaluate(() => messages.innerHTML = renderAttachments(['http://gallery.test/one.png']));
  assert.equal(await page.getByRole('button', { name: /展开/ }).count(), 0);
});

test('pinch zoom and pan do not change photos or close the viewer; swipe works again after reset', async t => {
  const page = await setup(t);
  assert.equal(await page.evaluate(() => typeof window.AionImageViewer?.open), 'function');
  await page.evaluate(() => AionImageViewer.open(['http://gallery.test/one.png', 'http://gallery.test/two.png']));
  await page.locator('.aion-image-viewer img').evaluate(img => img.decode());
  const client = await page.context().newCDPSession(page);
  const touch = (type, points) => client.send('Input.dispatchTouchEvent', { type, touchPoints: points.map(([id,x,y]) => ({ id, x, y })) });
  await touch('touchStart', [[1,150,350],[2,240,350]]);
  await touch('touchMove', [[1,90,350],[2,300,350]]);
  await touch('touchEnd', []);
  const transform = () => page.locator('.aion-image-viewer img').evaluate(img => new DOMMatrix(getComputedStyle(img).transform).a);
  assert.ok(await transform() > 2, 'pinch must scale the image, independent of browser page zoom');
  await touch('touchStart', [[1,230,350]]);
  await touch('touchMove', [[1,90,350]]);
  await touch('touchEnd', []);
  assert.match(await page.locator('.aion-image-viewer img').getAttribute('src'), /one.png$/);
  assert.ok(Math.abs(await page.locator('.aion-image-viewer img').evaluate(img => new DOMMatrix(getComputedStyle(img).transform).e)) > 20);
  await page.getByRole('button', { name: '重置缩放', exact: true }).click();
  assert.equal(await transform(), 1);
  await touch('touchStart', [[1,280,350]]);
  await touch('touchMove', [[1,80,350]]);
  await touch('touchEnd', []);
  assert.match(await page.locator('.aion-image-viewer img').getAttribute('src'), /two.png$/);
  assert.equal(await transform(), 1);
});

test('album keeps its controls and uses image zoom inside the full page dialog', async t => {
  const browser = await chromium.launch({ headless:true, channel:'chrome' });
  t.after(() => browser.close());
  const page = await browser.newPage({ viewport:{width:390,height:780}, hasTouch:true });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('http://album.test/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/album') return route.fulfill({ path:path.join(__dirname,'static/album.html'), contentType:'text/html' });
    if (url.pathname.startsWith('/static/')) return route.fulfill({ path:path.join(__dirname,url.pathname) });
    if (url.pathname === '/api/album/photos') return route.fulfill({ json:{ total:2, photos:[1,2].map(i => ({ id:String(i), title:'照片'+i, url:'/photo'+i+'.svg', thumbnail_url:'/photo'+i+'.svg', taken_on:'2026-10-24', source:'upload', favorite:false })) } });
    return route.fulfill({ contentType:'image/svg+xml', body:'<svg xmlns="http://www.w3.org/2000/svg" width="600" height="800"><rect width="600" height="800" fill="coral"/></svg>' });
  });
  await page.goto('http://album.test/album');
  await page.locator('.photo-card').first().click();
  await page.locator('#fullImage').evaluate(img => img.decode());
  const client = await page.context().newCDPSession(page);
  const touch = (type, points) => client.send('Input.dispatchTouchEvent', {type,touchPoints:points.map(([id,x,y])=>({id,x,y}))});
  await touch('touchStart', [[1,140,350],[2,250,350]]);
  await touch('touchMove', [[1,60,350],[2,330,350]]);
  await touch('touchEnd', []);
  assert.ok(await page.locator('#fullImage').evaluate(img=>new DOMMatrix(getComputedStyle(img).transform).a)>2);
  assert.equal(await page.locator('#photoDialog').evaluate(d=>d.open),true);
  await page.waitForTimeout(500); // Wait out the synthetic click guard after a pinch.
  await page.locator('#imageStage').click({position:{x:195,y:350}});
  await page.getByRole('button',{name:'下一张',exact:true}).click();
  assert.match(await page.locator('#fullImage').getAttribute('src'), /photo2.svg$/);
  assert.equal(await page.locator('#fullImage').evaluate(img=>new DOMMatrix(getComputedStyle(img).transform).a),1);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#photoDialog').evaluate(d=>d.open),false);
  assert.deepEqual(errors,[]);
});

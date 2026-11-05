// Isolated browser acceptance: no live uploads, settings writes, or image generation.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_PATH || 'playwright');

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 390, height: 844}});
    const errors = [], writes = [];
    page.on('pageerror', error => errors.push(error.message));
    const names = {user: '月亮', aion: '星星', connor: '云朵'};
    const records = Object.entries(names).map(([identity, name]) => ({id: 'anchor_' + identity, name, identity, aliases: '', appearance: '', image: ''}));
    let failSave = true;
    const settings = fs.readFileSync(path.join(__dirname, 'static/settings.html'), 'utf8');
    const button = settings.match(/<button[^>]*onclick="ImageAnchors.open\(\)"[^>]*>[^<]*<\/button>/)[0];
    assert.ok(fs.readFileSync(path.join(__dirname, 'static/theater-studio.js'), 'utf8').includes('return window.ImageAnchors.open();'));
    await page.route('http://anchors.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/') return route.fulfill({contentType: 'text/html; charset=utf-8', body: `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/common.css"><link rel="stylesheet" href="/static/image-anchors.css"><body>${button}<script>window.showToast = text => window.lastToast = text;</script><script src="/static/image-anchors.js"></script></body>`});
      if (url.pathname.startsWith('/static/')) return route.fulfill({path: path.join(__dirname, url.pathname.slice(1))});
      if (url.pathname.endsWith('/anchor-identities')) return route.fulfill({json: names});
      if (url.pathname.endsWith('/anchors')) return route.fulfill({json: records});
      if (route.request().method() === 'PUT') {
        if (failSave) return route.fulfill({status: 500, json: {detail: '暂时无法保存，请重试'}});
        if (url.pathname.endsWith('/photo')) {
          const form = await new Response(route.request().postDataBuffer(), {headers: {
            'Content-Type': route.request().headers()['content-type']
          }}).formData();
          assert.equal(form.get('file').name, 'portrait.png');
          writes.push({...JSON.parse(form.get('fields')), image: '/image-anchors/new.png'});
          records[0].image = writes.at(-1).image;
        } else {
          writes.push(route.request().postDataJSON());
        }
        return route.fulfill({json: writes.at(-1)});
      }
      assert.notEqual(url.pathname, '/api/upload', 'portraits must bypass chat uploads');
      return route.fulfill({contentType: 'image/png', body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9uoAAAAASUVORK5CYII=', 'base64')});
    });
    await page.goto('http://anchors.test/');
    await page.getByRole('button', {name: '管理人物锚点'}).click();
    const dialog = page.getByRole('dialog');
    await dialog.waitFor();
    assert.equal(await dialog.locator('.image-anchor-card').count(), 3);
    assert.equal(await dialog.locator('[data-field=identity]').first().inputValue(), 'user');
    await dialog.locator('[data-field=name]').first().fill('月亮的新名字');
    await dialog.locator('[data-field=photo]').first().setInputFiles({name: 'portrait.png', mimeType: 'image/png', buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9uoAAAAASUVORK5CYII=', 'base64')});
    const bounds = await dialog.evaluate(el => ({width: el.clientWidth, scroll: el.scrollWidth}));
    assert.ok(bounds.width <= 390 && bounds.scroll <= bounds.width, 'mobile dialog has no horizontal overflow');
    if (process.env.ANCHOR_SCREENSHOT_DIR) await page.screenshot({path: path.join(process.env.ANCHOR_SCREENSHOT_DIR, 'image-anchors-mobile.png')});
    await dialog.getByRole('button', {name: '保存人物', exact: true}).click();
    await page.getByRole('status').filter({hasText: '暂时无法保存'}).waitFor();
    assert.equal(await dialog.locator('[data-field=name]').first().inputValue(), '月亮的新名字');
    failSave = false;
    await dialog.getByRole('button', {name: '保存人物', exact: true}).click();
    await dialog.waitFor({state: 'detached'});
    assert.equal(writes.length, 3);
    assert.equal(writes[0].image, '/image-anchors/new.png');
    assert.equal(writes[0].identity, 'user');
    assert.equal(writes[0].name, '月亮的新名字');
    assert.ok(await page.evaluate(() => window.lastToast.includes('共用')));
    await page.setViewportSize({width: 1100, height: 850});
    await page.getByRole('button', {name: '管理人物锚点'}).click();
    await dialog.waitFor();
    assert.equal(await dialog.locator('.image-anchor-card img').first().getAttribute('src'), '/image-anchors/new.png');
    if (process.env.ANCHOR_SCREENSHOT_DIR) await page.screenshot({path: path.join(process.env.ANCHOR_SCREENSHOT_DIR, 'image-anchors-desktop.png')});
    await dialog.getByRole('button', {name: '取消', exact: true}).click();
    assert.deepEqual(errors, []);
    console.log('Shared anchor editor: mobile layout, upload, failed-save retry, identity persistence, and desktop open passed.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});

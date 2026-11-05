const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {chromium} = require('playwright');

test('editing and deleting memories keeps the place in the list', async () => {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({viewport:{width:390,height:720}});
    const rows = Array.from({length:43}, (_, i) => ({
      id:String(i + 1), content:'Memory ' + (i + 1), memory_kind:'daily'
    }));
    await page.route('http://library.test/**', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/static/common.js') return route.fulfill({contentType:'text/javascript',
        body:'window.connectRetainedPageWS = cb => window.onLibraryEvent = cb; window.api = (method,url,data) => fetch(url,{method,body:JSON.stringify(data)}).then(r => r.json());'});
      if (url.pathname === '/memory' || url.pathname.startsWith('/static/')) {
        const file = url.pathname === '/memory' ? 'memory.html' : path.basename(url.pathname);
        return route.fulfill({path:path.join(__dirname,'static',file)});
      }
      if (url.pathname === '/api/memory-library/main') {
        const n = Math.min(+url.searchParams.get('page'), Math.ceil(rows.length / 20));
        return route.fulfill({json:{items:rows.slice((n-1)*20,n*20),total:rows.length,page:n}});
      }
      if (url.pathname.endsWith('/sources')) return route.fulfill({json:{messages:[],total:0,page:1}});
      if (url.pathname.startsWith('/api/memory-library/main/')) {
        const index = rows.findIndex(row => row.id === path.basename(url.pathname));
        const item = rows[index];
        if (route.request().method() === 'DELETE') rows.splice(index,1);
        if (route.request().method() === 'PUT') Object.assign(item,route.request().postDataJSON());
        return route.fulfill({json:{item,related:[],originals:[]}});
      }
      return route.fulfill({json:{}});
    });
    await page.goto('http://library.test/memory');
    await page.click('#next');
    const list = page.locator('#list');
    const scrollTop = () => list.evaluate(node => node.scrollTop);
    for (const [id, action] of [['30','delete'],['31','delete'],['32','edit']]) {
      const button = page.locator(`[data-id="${id}"] [data-action="${action}"]`);
      await button.scrollIntoViewIfNeeded();
      const before = await scrollTop();
      assert.ok(before > 150);
      await button.click();
      // A notification can arrive while the confirmation dialog is still open.
      await page.evaluate(() => window.onLibraryEvent({type:'memory_updated'}));
      if (action === 'delete') {
        await page.click('#confirmDelete');
        await page.locator(`[data-id="${id}"]`).waitFor({state:'detached'});
      } else {
        await page.fill('#editText','Edited memory');
        await page.click('#saveEdit');
        await page.locator('#detail.open').waitFor();
        await page.click('#closeDetail');
      }
      await page.waitForFunction(() => !document.querySelector('#dialog').open &&
        document.querySelector('#list').getAttribute('aria-busy') === 'false');
      assert.equal(await scrollTop(),before,`list position after ${action} of ${id}`);
      assert.equal(await page.inputValue('#pageInput'),'2');
    }
    // The final page now has a single record. Its nearest neighbour is at the end of page two.
    await page.setViewportSize({width:1024,height:720});
    await page.click('#next');
    await page.locator('[data-id="43"] .entry-open').click();
    await page.click('#delete');
    await page.click('#confirmDelete');
    await page.waitForFunction(() => document.querySelector('#pageInput').value === '2');
    assert.equal(await list.evaluate(node => node.scrollHeight - node.clientHeight - node.scrollTop),0);
  } finally {
    await browser.close();
  }
});

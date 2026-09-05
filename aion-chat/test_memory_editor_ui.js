// Run with Node and Playwright available in NODE_PATH. Uses fixture APIs only.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

const root = path.join(__dirname, 'static');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const memory = { id: 'memory-1', content: '周末一起去散步，路过熟悉的小店。\n回来后把这件事记下来。'.repeat(12), keywords: '["散步","周末"]', importance: 0.7, type: 'shared_moment', memory_kind: 'long_term' };
const helper = `
window.$ = id => document.getElementById(id);
window.escHtml = window.esc = value => { const el = document.createElement('div'); el.textContent = value; return el.innerHTML; };
window.showToast = window.toast = text => { window.lastToast = text; };
window.connectCommonWS = callback => { window.memorySync = callback; };
window.api = async (first, second, third) => {
  const main = /^[A-Z]+$/.test(first);
  const response = await fetch(main ? second : '/api/chatroom' + first, main ? { method: first, body: third ? JSON.stringify(third) : undefined } : second);
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || '保存失败');
  return result;
};`;

async function run() {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const mode of ['main', 'chatroom']) {
      const page = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
      const writes = [];
      let failSave = false;
      let records = [{ ...memory }];
      await page.route('**/*', async route => {
        const url = new URL(route.request().url());
        if (url.pathname === '/frame') return route.fulfill({ contentType: 'text/html', body: '<meta name="viewport" content="width=device-width, initial-scale=1"><style>body { margin: 0; overflow: hidden; } iframe { border: 0; width: 100%; height: 844px; }</style><iframe src="/fixture"></iframe>' });
        if (url.pathname.startsWith('/api/')) {
          if (['PUT', 'POST'].includes(route.request().method())) {
            const body = route.request().postDataJSON();
            writes.push({ url: url.pathname, body });
            await new Promise(resolve => setTimeout(resolve, 80));
            if (failSave) return route.fulfill({ status: 500, json: { detail: '测试：连接中断' } });
            records[0] = { ...records[0], ...body };
            return route.fulfill({ json: records[0] });
          }
          if (url.pathname.includes('compress-daily')) return route.fulfill({ json: {} });
          if (url.pathname.endsWith('/anchor')) return route.fulfill({ json: {} });
          return route.fulfill({ json: mode === 'main' ? { items: records, total: 1, kind_totals: { all: 1, daily: 0, long_term: 1 } } : records });
        }
        if (url.pathname === '/fixture') {
          let html = read(mode === 'main' ? 'memory.html' : 'chatroom.html');
          const inline = mode === 'main' ? html.match(/<script>([\s\S]*?)<\/script>/)[1] : '';
          html = html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '');
          const chat = read('chatroom.js');
          const logic = mode === 'main' ? inline : `
            let currentRoom = { id: 'room-1' };
            let chatroomMemoryCache = ${JSON.stringify(records)};
            let chatroomMemoryKindFilter = 'all', chatroomMemoryKindMenuId = null;
            const crFormatMemoryOccurrence = () => '';
            ${chat.slice(chat.indexOf('function restoreChatroomMemoryPosition('), chat.indexOf('async function deleteMemory(memId)'))}
            document.getElementById('memoryOverlay').classList.add('active');
            renderChatroomMemories();`;
          const editor = fs.existsSync(path.join(root, 'memory-editor.js')) ? '<script src="/static/memory-editor.js"></script>' : '';
          return route.fulfill({ contentType: 'text/html', body: html.replace('</body>', `<script src="/static/theme.js"></script>${editor}<script>${helper}\n${logic}</script></body>`) });
        }
        const file = path.join(root, path.basename(url.pathname));
        if (url.pathname.startsWith('/static/') && fs.existsSync(file)) {
          return route.fulfill({ path: file, contentType: file.endsWith('.css') ? 'text/css' : 'text/javascript' });
        }
        return route.fulfill({ status: 204, body: '' });
      });
      await page.goto('http://memory.test/fixture');
      await page.locator('button[title="编辑"]').click();
      const textarea = page.locator('textarea:visible');
      await textarea.fill('保留这份修改，保存后可以重新打开。');
      await page.setViewportSize({ width: 390, height: 360 });
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const save = page.getByRole('button', { name: /^(保存|保存记忆|保存修改)$/ });
      const geometry = await save.evaluate(el => {
        const r = el.getBoundingClientRect();
        return { top: r.top, bottom: r.bottom, height: r.height, viewport: window.innerHeight, reachable: el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)) };
      });
      assert.ok(geometry.top >= 0 && geometry.bottom <= geometry.viewport && geometry.reachable && geometry.height >= 44, `${mode}: save must stay visible and tappable with the keyboard open: ${JSON.stringify(geometry)}`);
      assert.ok((await textarea.boundingBox()).width >= 340, `${mode}: editor should use the phone width`);
      await page.setViewportSize({ width: 390, height: 844 });
      // Rendering an incoming update must not destroy the unsaved draft.
      await page.evaluate(mode => {
        if (mode === 'main') window.memorySync({ type: 'memory_updated', data: { id: 'memory-1', content: '另一端刚刚刷新了列表。' } });
        else renderChatroomMemories();
      }, mode);
      assert.equal(await textarea.inputValue(), '保留这份修改，保存后可以重新打开。');
      await page.getByText('更多设置 · 关键词、重要度').click();
      await page.getByRole('spinbutton', { name: '重要度' }).fill('0');
      await page.getByRole('textbox', { name: '关键词' }).fill('散步，周末');
      await page.getByRole('combobox', { name: '记忆标签' }).selectOption('daily');
      failSave = true;
      await save.click();
      await page.getByRole('alert').waitFor({ state: 'visible' });
      assert.equal(await textarea.inputValue(), '保留这份修改，保存后可以重新打开。');
      assert.ok(await save.isEnabled());
      assert.match(await page.getByRole('alert').textContent(), /修改仍保留/);
      failSave = false;
      await save.click();
      // Ignore a second submit while the request is in flight.
      await page.locator('.memory-editor form').evaluate(el => el.requestSubmit());
      await page.locator('.memory-editor').waitFor({ state: 'hidden' });
      assert.equal(writes.length, 2, 'one failed request and one successful retry');
      const submitted = writes[1];
      assert.equal(submitted.body.importance, 0, 'zero importance must survive saving');
      assert.equal(submitted.body.content, '保留这份修改，保存后可以重新打开。');
      assert.equal(submitted.url, mode === 'main' ? '/api/memories/memory-1' : '/api/chatroom/memories/memory-1');
      if (mode === 'main') {
        assert.equal(submitted.body.type, 'daily');
        assert.deepEqual(JSON.parse(submitted.body.keywords), ['散步', '周末']);
      } else {
        assert.equal(submitted.body.memory_kind, 'daily');
        assert.equal(submitted.body.keywords, '散步，周末');
      }
      await page.locator('button[title="编辑"]').click();
      assert.equal(await textarea.inputValue(), submitted.body.content);
      await textarea.fill('尚未保存的内容');
      page.once('dialog', dialog => dialog.dismiss());
      await page.getByRole('button', { name: '取消', exact: true }).click();
      assert.equal(await textarea.inputValue(), '尚未保存的内容');
      page.once('dialog', dialog => dialog.accept());
      await page.keyboard.press('Escape');
      await page.locator('.memory-editor').waitFor({ state: 'hidden' });
      assert.equal(writes.length, 2, 'cancelling must not write a memory');
      if (mode === 'chatroom') {
        await page.getByRole('button', { name: '+ 手动添加', exact: true }).click();
        assert.equal(await textarea.inputValue(), '');
        await textarea.fill('新添加的一条记忆');
        await page.getByRole('button', { name: '保存记忆', exact: true }).click();
        await page.locator('.memory-editor').waitFor({ state: 'hidden' });
        assert.equal(writes[2].url, '/api/chatroom/rooms/room-1/memories');
      }
      // Both themes inherit the page palette; capture the actual editor for QA.
      await page.locator('button[title="编辑"]').click();
      if (process.env.MEMORY_EDITOR_SCREENSHOTS) {
        await page.screenshot({ path: path.join(process.env.MEMORY_EDITOR_SCREENSHOTS, `${mode}-mobile.png`) });
        await page.evaluate(() => window.AionTheme.apply('dark'));
        await textarea.fill(memory.content);
        await page.screenshot({ path: path.join(process.env.MEMORY_EDITOR_SCREENSHOTS, `${mode}-mobile-dark.png`) });
        await page.setViewportSize({ width: 1100, height: 860 });
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        await page.screenshot({ path: path.join(process.env.MEMORY_EDITOR_SCREENSHOTS, `${mode}-desktop.png`) });
      }
      if (mode === 'main') {
        await page.setViewportSize({ width: 390, height: 844 });
        await page.goto('http://memory.test/frame');
        const embedded = page.frameLocator('iframe');
        await embedded.locator('button[title="编辑"]').click();
        await page.setViewportSize({ width: 390, height: 360 });
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        const footerBox = await embedded.getByRole('button', { name: '保存修改', exact: true }).boundingBox();
        assert.ok(footerBox.y >= 0 && footerBox.y + footerBox.height <= 360, 'embedded editor must follow the parent keyboard viewport');
        if (process.env.MEMORY_EDITOR_SCREENSHOTS) await page.screenshot({ path: path.join(process.env.MEMORY_EDITOR_SCREENSHOTS, 'main-embedded-keyboard.png') });
      }
      await page.close();
      console.log(`${mode}: mobile layout, refresh, failed save/retry, request fields, cancel and reopen passed`);
    }
  } finally { await browser.close(); }
}
run().catch(error => { console.error(error); process.exitCode = 1; });

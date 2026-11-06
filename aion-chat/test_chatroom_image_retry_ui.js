const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

test('image retry lets the user cancel, save a prompt, then explicitly retry it', async t => {
  const browser = await chromium.launch({ headless: true, channel: 'chrome' });
  t.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width: 390, height: 780 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const saved = { prompt: '实际生图提示词\n窗边的合照', attempt: 0 };
  const submissions = [];
  let rejectSave = false;
  const message = () => ({
    id: 'failed', room_id: 'room', sender: 'connor', content: '图片生成失败',
    attachments: [{ type: 'chatroom_image_retry', attempt: saved.attempt, running: false }],
  });
  await page.route('http://image-retry.test/**', route => {
    const request = route.request();
    if (new URL(request.url()).pathname === '/') {
      return route.fulfill({ contentType: 'text/html', body: '<main id="messages"></main>' });
    }
    if (new URL(request.url()).pathname.startsWith('/public/')) return route.fulfill({ status: 204 });
    assert.equal(new URL(request.url()).pathname, '/api/chatroom/rooms/room/messages/failed/retry-image');
    if (request.method() === 'GET') return route.fulfill({ json: saved });
    const body = request.postDataJSON();
    if (request.method() === 'PUT') {
      if (rejectSave) return route.fulfill({ status: 503, json: { detail: '暂时无法保存' } });
      assert.equal(body.attempt, saved.attempt);
      saved.prompt = body.prompt;
      saved.attempt++;
      return route.fulfill({ json: { message: message() } });
    }
    assert.equal(request.method(), 'POST');
    submissions.push({ prompt: saved.prompt, attempt: body.attempt });
    return route.fulfill({ json: { accepted: true } });
  });
  await page.goto('http://image-retry.test/');
  for (const file of ['chatroom.css', 'ai-message-editor.css']) {
    await page.addStyleTag({ path: path.join(__dirname, 'static', file) });
  }
  await page.addScriptTag({ path: path.join(__dirname, 'static', 'ai-message-editor.js') });
  const source = fs.readFileSync(path.join(__dirname, 'static', 'chatroom.js'), 'utf8');
  const functions = ['api', 'crRetryImage', 'crEditImagePrompt', 'msgHTML', 'crBubbleUnitHtml', 'crMessageUnitHtml']
    .map(name => source.match(new RegExp('(async )?function ' + name + '\\([^]*?\\r?\\n\\}'))[0]).join('\n');
  await page.addScriptTag({ content: `
    const API = '/api/chatroom';
    const currentRoom = { id: 'room' };
    const crMessagesById = { failed: ${JSON.stringify(message())} };
    let crMessageRevision = 0;
    const AVATARS = { user: '/public/test-avatar.png' };
    const crMemoryRecordMsgIds = new Set();
    const crName = () => '测试角色';
    const esc = value => {
      const node = document.createElement('span');
      node.textContent = value || '';
      return node.innerHTML;
    };
    const escWithTransfer = esc, escWithImages = esc;
    const crStripWishFulfillmentMarker = value => value;
    const crWithWishFallbackAttachments = msg => msg.attachments;
    const renderToyAttachments = () => '';
    const renderAttachments = () => '';
    const crBandVibrationNoteHtml = () => '';
    const crPrivateWhisperNoteHtml = () => '';
    const crMsgSenderLineHtml = () => '';
    const MessageRowFocus = { focusRowFromClick() {} };
    function toast(text) { window.lastToast = text; }
    ${functions}
    document.getElementById('messages').innerHTML = msgHTML(crMessagesById.failed);
  ` });

  await page.getByRole('button', { name: '编辑', exact: true }).click();
  const input = page.getByRole('textbox', { name: '生图提示词' });
  await input.waitFor();
  assert.equal(await input.inputValue(), saved.prompt);
  const bounds = await input.boundingBox();
  assert.ok(bounds.width > 250 && bounds.x >= 0 && bounds.x + bounds.width <= 390);
  await input.fill('取消的草稿');
  await page.getByRole('button', { name: '取消', exact: true }).click();
  assert.equal(saved.prompt, '实际生图提示词\n窗边的合照');
  assert.equal(submissions.length, 0);

  await page.getByRole('button', { name: '编辑', exact: true }).click();
  await input.waitFor();
  await input.fill('  新提示词\n保留换行和空格  ');
  rejectSave = true;
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await page.getByRole('alert').filter({ hasText: '暂时无法保存' }).waitFor();
  assert.equal(await input.inputValue(), '  新提示词\n保留换行和空格  ');
  rejectSave = false;
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await page.getByRole('button', { name: '编辑', exact: true }).waitFor();
  assert.equal(saved.prompt, '  新提示词\n保留换行和空格  ');
  assert.equal(submissions.length, 0);
  await page.getByRole('button', { name: '编辑', exact: true }).click();
  await input.waitFor();
  assert.equal(await input.inputValue(), saved.prompt);
  await page.getByRole('button', { name: '取消', exact: true }).click();

  const submitted = page.waitForResponse(response => response.request().method() === 'POST');
  await page.getByRole('button', { name: '重试', exact: true }).click();
  await submitted;
  assert.deepEqual(submissions, [{ prompt: '  新提示词\n保留换行和空格  ', attempt: 1 }]);
  assert.equal(await page.getByRole('button', { name: '编辑', exact: true }).isDisabled(), true);
  assert.equal(await page.getByRole('button', { name: '重试中…', exact: true }).isDisabled(), true);
  assert.deepEqual(errors, []);
});

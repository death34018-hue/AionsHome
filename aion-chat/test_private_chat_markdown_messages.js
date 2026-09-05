'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const ROOT = __dirname;

function functionBlock(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.notEqual(start, -1, `missing ${startMarker}`);
  assert.notEqual(end, -1, `missing ${endMarker}`);
  return source.slice(start, end);
}

function loadPrivateMessageRenderer() {
  const source = fs.readFileSync(path.join(ROOT, 'static', 'chat.js'), 'utf8');
  const markdown = require('./static/chatroom-markdown.js');
  const rendererSource = functionBlock(
    source,
    'const PRIVATE_MESSAGE_SPECIAL_TOKEN_RE',
    'function renderMessages',
  );
  const context = {
    _memoryRecordMsgIds: new Set(),
    escHtml: markdown.escapeHtml,
    fmtTime: () => '16:42',
    formatMsg: markdown.escapeHtml,
    renderAttachments: () => '',
    renderBandVibrationNote: () => '',
    stripWishFulfillmentMarker: value => value,
    withWishFallbackAttachments: message => message.attachments || [],
    worldBook: { user_name: 'Test User', ai_name: 'Primary AI' },
    window: { ChatroomMarkdown: markdown },
  };
  vm.createContext(context);
  vm.runInContext(`${rendererSource}\nthis.renderPrivateMessage = renderPrivateMessageHTML;`, context);
  return context.renderPrivateMessage;
}

test('private AI markdown renders without a bubble below one identity header', () => {
  const html = loadPrivateMessageRenderer()({
    id: 'private-ai-1', role: 'assistant', created_at: 1, attachments: [],
    content: '## 醒得正好。\n\n**先喝水。**\n\n*慢慢起身。*',
  });

  assert.equal((html.match(/class="msg-avatar"/g) || []).length, 1);
  assert.doesNotMatch(html, /class="msg-bubble/);
  assert.match(html, /class="private-message-header"/);
  assert.match(html, /class="private-ai-message-content markdown-body"/);
  assert.match(html, /<h2>醒得正好。<\/h2>/);
  assert.match(html, /<strong>先喝水。<\/strong>/);
  assert.ok(html.indexOf('class="private-message-header"') < html.indexOf('class="private-ai-message-content'));
});

test('private user lines and heading become separate right-aligned bubbles below one header', () => {
  const html = loadPrivateMessageRenderer()({
    id: 'private-user-1', role: 'user', created_at: 1, attachments: [],
    content: '先听我说。\n# 我的大字报\n今天也开心。',
  });

  assert.equal((html.match(/class="msg-avatar"/g) || []).length, 1);
  assert.equal((html.match(/class="msg-bubble markdown-body"/g) || []).length, 3);
  assert.match(html, /class="private-message-unit user-message-unit"/);
  assert.match(html, /class="private-message-header user-message-header"/);
  assert.match(html, /<p>先听我说。<\/p>/);
  assert.match(html, /<h1>我的大字报<\/h1>/);
  assert.ok(html.indexOf('class="private-message-header') < html.indexOf('class="msg-bubble markdown-body'));
});

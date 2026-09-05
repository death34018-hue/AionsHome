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

function loadMessageRenderer() {
  const source = fs.readFileSync(path.join(ROOT, 'static', 'chatroom.js'), 'utf8');
  const markdown = require('./static/chatroom-markdown.js');
  const rendererSource = [
    functionBlock(source, 'function crMsgMenuHtml', 'function crCanRateAiMsg'),
    functionBlock(source, 'function crMsgSenderLineHtml', 'function crEnsureMsgMenu'),
    functionBlock(source, 'const CR_STRUCTURED_LINE_RE', 'let crProactiveCompanionshipStatus'),
    functionBlock(source, 'function crBubbleUnitHtml', 'function crBandVibrationNoteHtml'),
    functionBlock(source, 'function msgHTML', 'let crMsgFeedbackPopover'),
  ].join('\n');

  const context = {
    AVATARS: {
      user: '/avatar-user.png',
      aion: '/avatar-aion.png',
      connor: '/avatar-connor.png',
    },
    crMemoryRecordMsgIds: new Set(),
    crName: sender => ({ user: 'Test User', aion: 'Primary AI', connor: 'Second AI' })[sender],
    crStripWishFulfillmentMarker: value => value,
    crWithWishFallbackAttachments: message => message.attachments || [],
    crBandVibrationNoteHtml: () => '',
    crMsgFeedbackHtml: () => '',
    esc: markdown.escapeHtml,
    escWithTransfer: markdown.escapeHtml,
    escWithImages: markdown.escapeHtml,
    renderToyAttachments: () => '',
    renderAttachments: () => '',
    timeStr: () => '16:42',
    window: { LoungeVisitUI: null, ChatroomMarkdown: markdown },
  };
  vm.createContext(context);
  vm.runInContext(`${rendererSource}\nthis.renderMessage = msgHTML;`, context);
  return context.renderMessage;
}

function renderStreamingMessage(sender = 'aion') {
  const source = fs.readFileSync(path.join(ROOT, 'static', 'chatroom.js'), 'utf8');
  const rendererSource = [
    functionBlock(source, 'function crMessageUnitHtml', 'const CR_MESSAGE_SPECIAL_TOKEN_RE'),
    functionBlock(source, 'function startStreamingBubble', 'function crStripScheduleCommands'),
  ].join('\n');
  let appended = null;
  const messagesEl = {
    querySelector: () => null,
    appendChild: row => { appended = row; },
  };
  const context = {
    AVATARS: { user: '/avatar-user.png', aion: '/avatar-aion.png', connor: '/avatar-connor.png' },
    crName: actor => actor === 'connor' ? 'Second AI' : 'Primary AI',
    crMsgSenderLineHtml: actor => `<div class="sender-line"><span>${actor}</span></div>`,
    document: {
      createElement: () => ({
        className: '',
        id: '',
        innerHTML: '',
        querySelector: () => ({}),
      }),
    },
    esc: value => String(value),
    messagesEl,
    scrollToBottom: () => {},
  };
  vm.createContext(context);
  vm.runInContext(`${rendererSource}\nstartStreamingBubble('${sender}', 'stream-1');`, context);
  return appended?.innerHTML || '';
}

test('markdown renders expressive text without executing raw HTML or remote images', () => {
  const markdown = require('./static/chatroom-markdown.js');
  const html = markdown.render([
    '# 大字报',
    '',
    '**粗体**、*斜体*和~~删除线~~',
    '',
    '> 先喝几口水。',
    '',
    '- 第一件事',
    '- 第二件事',
    '',
    '<script>alert(1)</script>',
    '![追踪图](https://tracker.example/pixel.png)',
    '[危险链接](javascript:alert(1))',
  ].join('\n'));

  assert.match(html, /<h1>大字报<\/h1>/);
  assert.match(html, /<strong>粗体<\/strong>/);
  assert.match(html, /<em>斜体<\/em>/);
  assert.match(html, /<s>删除线<\/s>/);
  assert.match(html, /<blockquote>/);
  assert.match(html, /<ul>/);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script|<img|javascript:/i);
  assert.match(html, /追踪图/);
  assert.match(html, /危险链接/);
});

test('user markdown gives plain lines and a heading separate bubbles under one avatar', () => {
  const html = loadMessageRenderer()({
    id: 'user-1', sender: 'user', created_at: 1, attachments: [],
    content: '先听我说。\n# 我的大字报\n**今天也要开心。**',
  });

  assert.equal((html.match(/class="avatar"/g) || []).length, 1);
  assert.equal((html.match(/class="bubble markdown-body"/g) || []).length, 3);
  assert.match(html, /<p>先听我说。<\/p>/);
  assert.match(html, /<h1>我的大字报<\/h1>/);
  assert.match(html, /<strong>今天也要开心。<\/strong>/);
});

test('user bubble splitting keeps markdown lists, quotes, and fenced code intact', () => {
  const markdown = require('./static/chatroom-markdown.js');
  const parts = markdown.splitUserBubbleParts([
    '开场。',
    '- 第一项',
    '- 第二项',
    '> 第一行引用',
    '> 第二行引用',
    '```js',
    'const answer = 42;',
    '```',
  ].join('\n'));

  assert.deepEqual(parts, [
    '开场。',
    '- 第一项\n- 第二项',
    '> 第一行引用\n> 第二行引用',
    '```js\nconst answer = 42;\n```',
  ]);
});

test('user identity header sits above the right-aligned bubble', () => {
  const html = loadMessageRenderer()({
    id: 'user-layout-1', sender: 'user', created_at: 1, attachments: [],
    content: '气泡贴着右侧安全线。',
  });

  const unit = html.indexOf('class="message-unit user user-message-unit"');
  const header = html.indexOf('class="message-header user-message-header"');
  const avatar = html.indexOf('class="avatar"');
  const content = html.indexOf('class="unit-content"');
  const bubble = html.indexOf('class="bubble markdown-body"');

  assert.ok(unit >= 0, 'user message should expose its right-aligned layout');
  assert.ok(unit < header && header < avatar && avatar < content, 'avatar should sit in the user header above the content');
  assert.ok(content < bubble, 'user bubble should begin below the identity header');
});

test('AI markdown is one wide text flow without a bubble or repeated avatar', () => {
  const html = loadMessageRenderer()({
    id: 'ai-1', sender: 'aion', created_at: 1, attachments: [],
    content: '## 醒得正好。\n\n先喝水，再慢慢起身。\n\n*别着急。*',
  });

  assert.equal((html.match(/class="avatar"/g) || []).length, 1);
  assert.doesNotMatch(html, /class="bubble/);
  assert.equal((html.match(/class="ai-message-content markdown-body"/g) || []).length, 1);
  assert.match(html, /<h2>醒得正好。<\/h2>/);
  assert.match(html, /<em>别着急。<\/em>/);
});

test('AI identity header sits above the full-width reply body', () => {
  const html = loadMessageRenderer()({
    id: 'ai-layout-1', sender: 'connor', created_at: 1, attachments: [],
    content: '正文从头像下方顶格开始。',
  });

  const headerStart = html.indexOf('class="message-header"');
  const avatar = html.indexOf('class="avatar"');
  const senderLine = html.indexOf('class="sender-line"');
  const headerEnd = html.indexOf('class="unit-content"');
  const body = html.indexOf('class="message-text-flow connor"');

  assert.ok(headerStart >= 0, 'AI message should expose a dedicated header row');
  assert.ok(headerStart < avatar && avatar < headerEnd, 'avatar should stay inside the header row');
  assert.ok(headerStart < senderLine && senderLine < headerEnd, 'name and actions should stay inside the header row');
  assert.ok(headerEnd < body, 'reply body should begin after the header row');
});

test('streaming AI reply uses the same identity header above its live body', () => {
  const html = renderStreamingMessage('aion');

  assert.match(html, /class="message-unit aion ai-message-unit"/);
  assert.match(html, /class="message-header"/);
  assert.ok(html.indexOf('class="message-header"') < html.indexOf('class="message-live-content'));
});

test('plain AI line breaks become readable paragraphs inside the same text flow', () => {
  const html = loadMessageRenderer()({
    id: 'ai-2', sender: 'connor', created_at: 1, attachments: [],
    content: '第一段。\n第二段。\n第三段。',
  });

  assert.equal((html.match(/class="avatar"/g) || []).length, 1);
  assert.equal((html.match(/class="ai-message-content markdown-body"/g) || []).length, 1);
  assert.equal((html.match(/<p>/g) || []).length, 3);
});

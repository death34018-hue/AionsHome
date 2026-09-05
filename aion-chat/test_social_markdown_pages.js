'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const ROOT = __dirname;
const markdown = require('./static/chatroom-markdown.js');

function functionBlock(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.notEqual(start, -1, `missing ${startMarker}`);
  assert.notEqual(end, -1, `missing ${endMarker}`);
  return source.slice(start, end);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

test('moment cards render Markdown in the post body while comments remain plain text', () => {
  const source = fs.readFileSync(path.join(ROOT, 'static', 'moments.html'), 'utf8');
  const rendererSource = functionBlock(source, 'function renderMomentCard', 'function prependMomentOnce');
  const context = {
    escapeHtml,
    getAuthorName: author => author,
    getAvatarUrl: () => '/avatar.png',
    relativeTime: () => '刚刚',
    renderMarkdownBody: markdown.render,
    renderMomentImages: () => '',
  };
  vm.createContext(context);
  vm.runInContext(`${rendererSource}\nthis.renderMomentCardForTest = renderMomentCard;`, context);

  const html = context.renderMomentCardForTest({
    id: 'moment-1',
    author: 'aion',
    created_at: 1,
    content: '# 今日\n\n**很开心。**',
    comments: [{ id: 'comment-1', author: 'user', content: '**普通评论**' }],
    reactions: [],
    attachments: [],
  });

  assert.match(html, /class="moment-content markdown-body social-markdown"/);
  assert.match(html, /<h1>今日<\/h1>/);
  assert.match(html, /<strong>很开心。<\/strong>/);
  assert.match(html, /：\*\*普通评论\*\*/);
});

test('diary cards render Markdown only in the expanded body', () => {
  const source = fs.readFileSync(path.join(ROOT, 'static', 'diary.html'), 'utf8');
  const rendererSource = functionBlock(source, 'function renderList()', 'function bindDiaryListEvents');
  const container = { innerHTML: '' };
  const context = {
    allItems: [{
      id: 'diary-1',
      author: 'connor',
      created_at: 1,
      title: '# 普通标题',
      mood: '**开心**',
      content: '## 今天\n\n*慢慢写。*',
    }],
    bindDiaryListEvents: () => {},
    document: { getElementById: () => container },
    escHtml: escapeHtml,
    expandedDiaryIds: new Set(['diary-1']),
    formatSource: () => '',
    nameMap: { connor: 'Connor' },
    renderMarkdownBody: markdown.render,
    totalItems: 1,
    updateDiaryTtsButtons: () => {},
  };
  vm.createContext(context);
  vm.runInContext(`${rendererSource}\nthis.renderDiaryListForTest = renderList;`, context);

  context.renderDiaryListForTest();

  assert.match(container.innerHTML, /<div class="diary-title"># 普通标题<\/div>/);
  assert.match(container.innerHTML, /class="diary-content markdown-body social-markdown"/);
  assert.match(container.innerHTML, /<h2>今天<\/h2>/);
  assert.match(container.innerHTML, /<em>慢慢写。<\/em>/);
  assert.match(container.innerHTML, /<span class="diary-mood">\*\*开心\*\*<\/span>/);
});

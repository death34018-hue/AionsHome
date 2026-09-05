'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {renderSystemNoticeContent} = require('./static/system-notice-ui.js');

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

test('recognized schedule notices are collapsed and keep the escaped full text', () => {
  const html = renderSystemNoticeContent(
    '👀 【星<野>】设定了监督：2026-09-02 20:30，内容：查看战况',
    {escapeHtml},
  );
  assert.match(html, /<details class="system-notice-details">/);
  assert.doesNotMatch(html, /<details[^>]*\sopen(?:\s|>)/);
  assert.match(html, /<summary>【星&lt;野&gt;】设定了监督<\/summary>/);
  assert.match(html, /👀 【星&lt;野&gt;】设定了监督：2026-09-02 20:30，内容：查看战况/);
});

test('ordinary system notices remain one escaped text line', () => {
  const html = renderSystemNoticeContent('星<野>翻找了记忆', {escapeHtml});
  assert.equal(html, '<span class="system-notice-text">星&lt;野&gt;翻找了记忆</span>');
});

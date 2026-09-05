(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.SystemNoticeUI = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const SCHEDULE_NOTICE = /^(?:(?:⏰|📅|👀)\uFE0F?\s*)?(【[^】]+】设定了(?:闹铃|日程|监督))\s*[：:]/u;

  function fallbackEscape(value) {
    return String(value ?? '')
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }

  function renderSystemNoticeContent(content, options) {
    const text = String(content ?? '').trim();
    const escapeHtml = typeof options?.escapeHtml === 'function'
      ? options.escapeHtml
      : fallbackEscape;
    const match = text.match(SCHEDULE_NOTICE);
    if (!match) {
      return `<span class="system-notice-text">${escapeHtml(text)}</span>`;
    }
    return `<details class="system-notice-details">
      <summary>${escapeHtml(match[1])}</summary>
      <div class="system-notice-full">${escapeHtml(text)}</div>
    </details>`;
  }

  return {renderSystemNoticeContent};
});

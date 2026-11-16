// 把家庭动态按时间插入聊天消息列表，只显示不入库；范围与模型上下文一致（/api/timeline/chat）。
(function () {
  'use strict';

  const POLL_MS = 60000;
  const REFRESH_GAP_MS = 5000;
  const ROW_CLASS = 'family-dynamic-row';

  function esc(value) {
    return String(value ?? '')
      .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
  }

  // options: container, rowClass, lineClass, getTimestamp(msgId) -> 秒, getQuery() -> 查询串（无聊天时返回 ''）
  function attach(options) {
    const container = options.container;
    if (!container) return null;
    let items = [];
    let loadedQuery = null;
    let lastFetchAt = 0;
    let fetchSeq = 0;
    let trailingTimer = 0;
    let placedIds = '';

    function buildRow(item) {
      const row = document.createElement('div');
      row.className = `${options.rowClass} ${ROW_CLASS}`;
      row.dataset.familyId = item.id;
      row.dataset.familyKey = item.title;
      row.innerHTML = `<div class="${options.lineClass}"><span class="system-notice-marker" aria-hidden="true">&gt;</span><span class="system-notice-text">🏠 ${esc(item.title)}</span></div>`;
      return row;
    }

    function place() {
      const existing = new Map();
      const rows = [];
      for (const node of container.children) {
        if (node.classList.contains(ROW_CLASS)) existing.set(node.dataset.familyId, node);
        else rows.push(node);
      }
      const stamped = [];
      for (const node of rows) {
        const ts = Number(node.dataset.msgId ? options.getTimestamp(node.dataset.msgId) : 0);
        if (ts > 0) stamped.push({ node, ts });
      }

      // 动态排在第一条比它晚的消息之前；比所有消息都晚的排在最后一条消息之后；
      // 比已加载的最早消息还早的不显示。
      const before = new Map();
      let tail = [];
      if (stamped.length && options.getQuery() === loadedQuery) {
        let index = 0;
        while (index < items.length && items[index].timestamp < stamped[0].ts) index++;
        for (const row of stamped) {
          while (index < items.length && items[index].timestamp < row.ts) {
            if (!before.has(row.node)) before.set(row.node, []);
            before.get(row.node).push(items[index++]);
          }
        }
        tail = items.slice(index);
      }
      const lastStamped = stamped.length ? stamped[stamped.length - 1].node : null;
      const desired = [];
      const ids = [];
      const use = item => {
        const old = existing.get(item.id);
        desired.push(old && old.dataset.familyKey === item.title ? old : buildRow(item));
        ids.push(item.id);
      };
      for (const node of rows) {
        (before.get(node) || []).forEach(use);
        desired.push(node);
        if (node === lastStamped) tail.forEach(use);
      }

      const current = Array.from(container.children);
      if (current.length === desired.length && current.every((node, i) => node === desired[i])) return;

      // 页面整体重绘会清掉动态行：原样补回时保持滚动位置；新增动态时保持可见内容不跳动。
      const restoring = existing.size === 0 && ids.length > 0 && ids.join('|') === placedIds;
      const stick = container.scrollHeight - container.scrollTop - container.clientHeight < 60;
      const scrollTop = container.scrollTop;
      const top = container.getBoundingClientRect().top;
      const anchor = stick || restoring ? null : rows.find(node => node.getBoundingClientRect().bottom > top);
      const anchorTop = anchor ? anchor.getBoundingClientRect().top : 0;

      observer.disconnect();
      const keep = new Set(desired);
      for (const node of existing.values()) if (!keep.has(node)) node.remove();
      let next = null;
      for (let i = desired.length - 1; i >= 0; i--) {
        const node = desired[i];
        if (node.classList.contains(ROW_CLASS) && (node.parentNode !== container || node.nextSibling !== next)) {
          container.insertBefore(node, next);
        }
        next = node;
      }
      observer.observe(container, { childList: true });
      placedIds = ids.join('|');

      if (stick) container.scrollTop = container.scrollHeight;
      else if (restoring) container.scrollTop = scrollTop;
      else if (anchor) container.scrollTop += anchor.getBoundingClientRect().top - anchorTop;
    }

    async function refresh(force) {
      const query = options.getQuery();
      if (!query) {
        items = [];
        loadedQuery = query;
        place();
        return;
      }
      const wait = lastFetchAt + REFRESH_GAP_MS - Date.now();
      if (!force && query === loadedQuery && wait > 0) {
        if (!trailingTimer) trailingTimer = setTimeout(() => { trailingTimer = 0; refresh(true); }, wait);
        return;
      }
      lastFetchAt = Date.now();
      const seq = ++fetchSeq;
      try {
        const res = await fetch(`/api/timeline/chat?${query}`, { cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json();
        if (seq !== fetchSeq || query !== options.getQuery()) return;
        items = Array.isArray(data.items) ? data.items : [];
        loadedQuery = query;
        place();
      } catch (_) { /* 网络失败时保留已显示的动态 */ }
    }

    // 同步补位：回调在页面脚本结束后、绘制之前执行，重绘时不闪烁。
    const observer = new MutationObserver(() => {
      place();
      refresh(false);
    });
    observer.observe(container, { childList: true });
    setInterval(() => { if (!document.hidden) refresh(true); }, POLL_MS);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(true); });
    refresh(true);
    return { refresh: () => refresh(true) };
  }

  window.FamilyDynamicsInline = { attach };
})();

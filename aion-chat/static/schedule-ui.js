'use strict';

(function exposeScheduleUI(root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.ScheduleUI = api;
  }
}(typeof window !== 'undefined' ? window : null, function createScheduleUI() {
  const TYPE_PRESENTATIONS = {
    alarm: { icon: '🔔', label: '闹铃', className: 'alarm' },
    reminder: { icon: '📋', label: '日程', className: 'reminder' },
    monitor: { icon: '👁', label: '监督', className: 'monitor' },
  };

  function scheduleTypePresentation(type) {
    return TYPE_PRESENTATIONS[type] || TYPE_PRESENTATIONS.reminder;
  }

  function defaultEscapeHtml(value) {
    return String(value)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }

  function scheduleItemHtml(item, options = {}) {
    const history = Boolean(options.history);
    const escapeHtml = options.escapeHtml || defaultEscapeHtml;
    const type = scheduleTypePresentation(item.type);
    const originName = item.origin_name || '';
    const originHtml = originName
      ? `<span class="sch-origin">【${escapeHtml(originName)}】</span>`
      : '';
    const triggerAt = String(item.trigger_at || '').replace('T', ' ');
    const historyStatus = item.status === 'cancelled' ? 'cancelled' : 'triggered';
    const historyLabel = historyStatus === 'cancelled' ? '已取消' : '已完成';
    const historyHtml = history
      ? `<span class="sch-history-status ${historyStatus}">${historyLabel}</span>`
      : '';
    const encodedId = encodeURIComponent(String(item.id || '')).replaceAll("'", '%27');
    const deleteLabel = escapeHtml(`删除日程：${item.content || ''}`);
    const deleteHtml = history
      ? ''
      : `<button class="sch-del-btn" onclick="deleteSchedule(decodeURIComponent('${encodedId}'))" aria-label="${deleteLabel}" title="删除">✕</button>`;

    return `<div class="sch-item${history ? ' sch-history-item' : ''}">
      <span class="sch-icon">${type.icon}</span>
      <div class="sch-body">
        <div>${originHtml}<span class="sch-content">${escapeHtml(item.content || '')}</span><span class="sch-type ${type.className}">${type.label}</span>${historyHtml}</div>
        <div class="sch-time">${escapeHtml(triggerAt)}</div>
      </div>
      ${deleteHtml}
    </div>`;
  }

  async function loadScheduleLists(request) {
    const [activeResult, historyResult] = await Promise.allSettled([
      request('GET', '/api/schedules?status=active'),
      request('GET', '/api/schedules?status=history'),
    ]);
    const errors = [];
    if (activeResult.status === 'rejected') {
      errors.push({ list: 'active', error: activeResult.reason });
    }
    if (historyResult.status === 'rejected') {
      errors.push({ list: 'history', error: historyResult.reason });
    }
    return {
      active: activeResult.status === 'fulfilled' ? activeResult.value : null,
      history: historyResult.status === 'fulfilled' ? historyResult.value : null,
      errors,
    };
  }

  function shouldReloadSchedules(message) {
    return Boolean(message && message.type === 'schedule_changed');
  }

  function privateMemoItemHtml(item, options = {}) {
    const escapeHtml = options.escapeHtml || defaultEscapeHtml;
    const completed = item.status === 'completed';
    const encodedId = encodeURIComponent(String(item.id || '')).replaceAll("'", '%27');
    const content = escapeHtml(item.content || '');
    const action = completed
      ? `<button class="memo-check completed" onclick="restorePrivateMemo(decodeURIComponent('${encodedId}'))" aria-label="恢复备忘">✓</button>`
      : `<button class="memo-check" onclick="completePrivateMemo(decodeURIComponent('${encodedId}'))" aria-label="完成备忘"></button>`;
    const edit = completed
      ? ''
      : `<button class="memo-action" onclick="editPrivateMemo(decodeURIComponent('${encodedId}'))" aria-label="编辑备忘">✎</button>`;
    return `<div class="private-memo-item${completed ? ' completed' : ''}">
      ${action}<span class="private-memo-content">${content}</span>${edit}
      <button class="memo-action danger" onclick="deletePrivateMemo(decodeURIComponent('${encodedId}'))" aria-label="删除备忘">✕</button>
    </div>`;
  }

  async function loadPrivateMemoLists(request) {
    const [activeResult, completedResult] = await Promise.allSettled([
      request('GET', '/api/private-memos?status=active'),
      request('GET', '/api/private-memos?status=completed'),
    ]);
    return {
      active: activeResult.status === 'fulfilled' ? activeResult.value : null,
      completed: completedResult.status === 'fulfilled' ? completedResult.value : null,
      errors: [
        ...(activeResult.status === 'rejected' ? [activeResult.reason] : []),
        ...(completedResult.status === 'rejected' ? [completedResult.reason] : []),
      ],
    };
  }

  function shouldReloadPrivateMemos(message) {
    return Boolean(message && message.type === 'private_memos_changed');
  }

  function normalizeScheduleTab(tab) {
    return tab === 'memo' || tab === 'history' ? tab : 'active';
  }

  function selectScheduleTab(tab, documentLike = document) {
    const selected = normalizeScheduleTab(tab);
    for (const [name, suffix] of [['active', 'Active'], ['memo', 'Memo'], ['history', 'History']]) {
      const isSelected = selected === name;
      const tabElement = documentLike.getElementById(`schTab${suffix}`);
      const panel = documentLike.getElementById(`schPanel${suffix}`);
      tabElement?.classList.toggle('active', isSelected);
      tabElement?.setAttribute('aria-selected', String(isSelected));
      tabElement?.setAttribute('tabindex', isSelected ? '0' : '-1');
      panel?.classList.toggle('active', isSelected);
      if (panel) panel.hidden = !isSelected;
    }
    const tabs = documentLike.getElementById('schTabs');
    if (tabs) tabs.hidden = selected === 'history';
    const historyButton = documentLike.getElementById('schHistoryButton');
    if (historyButton) historyButton.hidden = selected !== 'active';
    const title = documentLike.getElementById('schPageTitle');
    if (title) title.textContent = selected === 'history' ? '日程历史' : '日程管理';
    documentLike.getElementById('schBackButton')?.setAttribute(
      'aria-label', selected === 'history' ? '返回日程管理' : '返回首页',
    );
    return selected;
  }

  function handleScheduleTabKeydown(event, tab, documentLike = document) {
    const selected = normalizeScheduleTab(tab);
    let next = null;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      next = selected === 'active' ? 'memo' : 'active';
    } else if (event.key === 'Home') {
      next = 'active';
    } else if (event.key === 'End') {
      next = 'memo';
    }
    if (!next) return null;

    event.preventDefault();
    selectScheduleTab(next, documentLike);
    const nextTab = documentLike.getElementById(
      next === 'active' ? 'schTabActive' : 'schTabMemo',
    );
    nextTab?.focus();
    return next;
  }

  return {
    handleScheduleTabKeydown,
    loadScheduleLists,
    loadPrivateMemoLists,
    normalizeScheduleTab,
    scheduleItemHtml,
    privateMemoItemHtml,
    scheduleTypePresentation,
    selectScheduleTab,
    shouldReloadSchedules,
    shouldReloadPrivateMemos,
  };
}));

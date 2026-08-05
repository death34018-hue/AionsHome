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

  function normalizeScheduleTab(tab) {
    return tab === 'history' ? 'history' : 'active';
  }

  function selectScheduleTab(tab, documentLike = document) {
    const selected = normalizeScheduleTab(tab);
    const activeSelected = selected === 'active';
    const activeTab = documentLike.getElementById('schTabActive');
    const historyTab = documentLike.getElementById('schTabHistory');
    const activePanel = documentLike.getElementById('schPanelActive');
    const historyPanel = documentLike.getElementById('schPanelHistory');

    activeTab?.classList.toggle('active', activeSelected);
    historyTab?.classList.toggle('active', !activeSelected);
    activeTab?.setAttribute('aria-selected', String(activeSelected));
    historyTab?.setAttribute('aria-selected', String(!activeSelected));
    activeTab?.setAttribute('tabindex', activeSelected ? '0' : '-1');
    historyTab?.setAttribute('tabindex', activeSelected ? '-1' : '0');
    activePanel?.classList.toggle('active', activeSelected);
    historyPanel?.classList.toggle('active', !activeSelected);
    if (activePanel) activePanel.hidden = !activeSelected;
    if (historyPanel) historyPanel.hidden = activeSelected;
    return selected;
  }

  function handleScheduleTabKeydown(event, tab, documentLike = document) {
    const selected = normalizeScheduleTab(tab);
    let next = null;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      next = selected === 'active' ? 'history' : 'active';
    } else if (event.key === 'Home') {
      next = 'active';
    } else if (event.key === 'End') {
      next = 'history';
    }
    if (!next) return null;

    event.preventDefault();
    selectScheduleTab(next, documentLike);
    const nextTab = documentLike.getElementById(
      next === 'active' ? 'schTabActive' : 'schTabHistory',
    );
    nextTab?.focus();
    return next;
  }

  function scheduleCount(value) {
    const number = Number(value);
    return Number.isFinite(number) ? Math.max(0, Math.floor(number)) : 0;
  }

  function updateScheduleTabCounts(
    activeCount,
    historyCount,
    documentLike = document,
  ) {
    const activeTab = documentLike.getElementById('schTabActive');
    const historyTab = documentLike.getElementById('schTabHistory');
    if (activeTab) activeTab.textContent = `当前 ${scheduleCount(activeCount)}`;
    if (historyTab) {
      historyTab.textContent = `历史 ${scheduleCount(historyCount)}`;
    }
  }

  return {
    handleScheduleTabKeydown,
    loadScheduleLists,
    normalizeScheduleTab,
    scheduleItemHtml,
    scheduleTypePresentation,
    selectScheduleTab,
    shouldReloadSchedules,
    updateScheduleTabCounts,
  };
}));

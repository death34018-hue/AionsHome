(function (root, factory) {
  const ui = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = ui;
  if (root) root.LoungeFriendsUI = ui;
  if (root?.document) {
    root.document.addEventListener('DOMContentLoaded', () => ui.init(root.document));
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const state = {
    actors: [],
    friends: [],
    editingId: null,
    expandedVisitId: null,
    historyActorId: null,
    visitRefreshTimer: null,
    visitingActorIds: new Set(),
    runningVisitActorIds: new Set(),
  };

  function renderActorOptions(select, actors, documentRef = document) {
    const options = (Array.isArray(actors) ? actors : []).map(actor => {
      const option = documentRef.createElement('option');
      option.value = String(actor.id || '');
      option.textContent = String(actor.display_name || 'AI');
      return option;
    });
    select.replaceChildren(...options);
  }

  function friendPayload(elements) {
    return {
      actor_id: elements.actorId.value,
      display_name: elements.displayName.value.trim(),
      lounge_url: elements.loungeUrl.value.trim(),
      visitor_key: elements.visitorKey.value,
      relationship_note: elements.relationshipNote.value.trim(),
      enabled: Boolean(elements.enabled.checked),
      allow_autonomous: Boolean(elements.allowAutonomous.checked),
      cooldown_hours: Number(elements.cooldownHours.value),
      max_turns: Number(elements.maxTurns.value),
    };
  }

  async function saveFriend({ friendId, elements, request }) {
    const saved = await request(
      friendId ? 'PUT' : 'POST',
      friendId ? `/api/lounge-friends/${encodeURIComponent(friendId)}` : '/api/lounge-friends',
      friendPayload(elements),
    );
    elements.visitorKey.value = '';
    return saved;
  }

  async function request(method, url, body, options = {}) {
    if (typeof globalThis.api === 'function') {
      return globalThis.api(method, url, body, options);
    }
    const response = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined || body === null ? undefined : JSON.stringify(body),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(payload?.detail || `请求失败 (${response.status})`);
    }
    return payload;
  }

  async function requestImmediateVisit({ friend, topic, request }) {
    return request(
      'POST',
      `/api/lounge-friends/${encodeURIComponent(friend.id)}/visit`,
      { actor_id: friend.actor_id, topic },
      { timeoutMs: 610000 },
    );
  }

  function immediateVisitButtonState(actorId, busyActorIds) {
    const disabled = busyActorIds.has(actorId);
    return { disabled, label: disabled ? '拜访中…' : '立即拜访' };
  }

  async function withVisitingActor(visitingActorIds, actorId, refresh, operation) {
    visitingActorIds.add(actorId);
    refresh();
    try {
      return await operation();
    } finally {
      visitingActorIds.delete(actorId);
      refresh();
    }
  }

  function canDeleteVisit(visit) {
    return visit?.status !== 'running';
  }

  async function requestDeleteVisit({ visit, actorId, confirmDelete, request }) {
    if (!canDeleteVisit(visit) || !confirmDelete()) return false;
    await request(
      'DELETE',
      `/api/lounge-visits/${encodeURIComponent(visit.id)}?actor_id=${encodeURIComponent(actorId)}`,
    );
    return true;
  }

  function elements(documentRef) {
    return {
      actorId: documentRef.getElementById('actorId'),
      displayName: documentRef.getElementById('displayName'),
      loungeUrl: documentRef.getElementById('loungeUrl'),
      visitorKey: documentRef.getElementById('visitorKey'),
      relationshipNote: documentRef.getElementById('relationshipNote'),
      enabled: documentRef.getElementById('enabled'),
      allowAutonomous: documentRef.getElementById('allowAutonomous'),
      cooldownHours: documentRef.getElementById('cooldownHours'),
      maxTurns: documentRef.getElementById('maxTurns'),
    };
  }

  function status(documentRef, message) {
    documentRef.getElementById('statusLine').textContent = message || '';
  }

  function actorName(actorId) {
    return state.actors.find(actor => actor.id === actorId)?.display_name || 'AI';
  }

  function friendName(friendId) {
    return state.friends.find(friend => friend.id === friendId)?.display_name || '好友';
  }

  function visitStatusText(visit) {
    const turns = Math.max(0, Number(visit?.turn_count) || 0);
    if (visit?.status === 'running') {
      return turns > 0 ? `进行中 · ${turns} 回合` : '正在连接';
    }
    if (visit?.status === 'completed') return `已完成 · ${turns} 回合`;
    if (visit?.status === 'interrupted') return `已中断 · ${turns} 回合`;
    if (visit?.status === 'rejected') return '未能开始';
    return `${visit?.status || '未知状态'}${turns ? ` · ${turns} 回合` : ''}`;
  }

  function nextExpandedVisitId(currentId, clickedId) {
    return currentId === clickedId ? null : clickedId;
  }

  function visitTimeText(value) {
    const timestamp = Number(value);
    if (!Number.isFinite(timestamp) || timestamp <= 0) return '';
    const date = new Date(timestamp * 1000);
    return new Intl.DateTimeFormat('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(date);
  }

  function messageTimeText(value) {
    const timestamp = Number(value);
    if (!Number.isFinite(timestamp) || timestamp <= 0) return '';
    const date = new Date(timestamp * 1000);
    return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
  }

  function textElement(documentRef, tagName, className, text) {
    const element = documentRef.createElement(tagName);
    if (className) element.className = className;
    element.textContent = text;
    return element;
  }

  function button(documentRef, label, className, handler) {
    const control = textElement(documentRef, 'button', className, label);
    control.type = 'button';
    control.addEventListener('click', handler);
    return control;
  }

  function openForm(documentRef, friend = null) {
    const controls = elements(documentRef);
    state.editingId = friend?.id || null;
    documentRef.getElementById('formTitle').textContent = friend ? '编辑好友' : '新增好友';
    controls.actorId.disabled = Boolean(friend);
    controls.actorId.value = friend?.actor_id || state.actors[0]?.id || '';
    controls.displayName.value = friend?.display_name || '';
    controls.loungeUrl.value = friend?.lounge_url || '';
    controls.visitorKey.value = '';
    controls.relationshipNote.value = friend?.relationship_note || '';
    controls.enabled.checked = friend ? Boolean(friend.enabled) : true;
    controls.allowAutonomous.checked = friend ? Boolean(friend.allow_autonomous) : false;
    controls.cooldownHours.value = String(friend?.cooldown_hours || 12);
    controls.maxTurns.value = String(friend?.max_turns || 4);
    documentRef.getElementById('friendDialog').showModal();
  }

  function closeForm(documentRef) {
    elements(documentRef).visitorKey.value = '';
    documentRef.getElementById('friendDialog').close();
  }

  function renderFriendGroups(documentRef) {
    const root = documentRef.getElementById('friendGroups');
    const groups = state.actors.map(actor => {
      const section = documentRef.createElement('section');
      section.className = 'actor-group';
      section.appendChild(textElement(documentRef, 'h4', '', actor.display_name));
      const grid = documentRef.createElement('div');
      grid.className = 'friend-grid';
      const friends = state.friends.filter(friend => friend.actor_id === actor.id);
      if (!friends.length) {
        grid.appendChild(textElement(documentRef, 'p', 'empty-state', '还没有登记好友。'));
      }
      friends.forEach(friend => {
        const card = documentRef.createElement('article');
        card.className = `friend-card${friend.enabled ? '' : ' is-disabled'}`;
        const heading = documentRef.createElement('div');
        heading.className = 'friend-card-heading';
        heading.appendChild(textElement(documentRef, 'h5', '', friend.display_name));
        heading.appendChild(textElement(documentRef, 'span', 'visit-meta', friend.enabled ? '已启用' : '已停用'));
        card.appendChild(heading);
        card.appendChild(textElement(documentRef, 'p', 'friend-note', friend.relationship_note || '暂无关系备注'));
        const keyState = friend.has_key ? `Key：${friend.visitor_key_masked || '已保存'}` : 'Key：未设置';
        card.appendChild(textElement(documentRef, 'p', 'friend-meta', `${keyState} · 最多 ${friend.max_turns} 回合 · 冷却 ${friend.cooldown_hours} 小时`));
        const actions = documentRef.createElement('div');
        actions.className = 'friend-actions';
        const busyActorIds = new Set([
          ...state.visitingActorIds,
          ...state.runningVisitActorIds,
        ]);
        const visitButtonState = immediateVisitButtonState(friend.actor_id, busyActorIds);
        const visitButton = button(
          documentRef,
          visitButtonState.label,
          'card-button',
          () => visitFriend(documentRef, friend),
        );
        visitButton.disabled = visitButtonState.disabled || !friend.enabled;
        actions.append(
          visitButton,
          button(documentRef, '测试连接', 'card-button', () => testFriend(documentRef, friend)),
          button(documentRef, '编辑', 'card-button', () => openForm(documentRef, friend)),
          button(documentRef, '删除', 'card-button danger', () => deleteFriend(documentRef, friend)),
        );
        card.appendChild(actions);
        grid.appendChild(card);
      });
      section.appendChild(grid);
      return section;
    });
    root.replaceChildren(...groups);
  }

  async function loadFriends(documentRef) {
    const payload = await request('GET', '/api/lounge-friends');
    state.actors = Array.isArray(payload?.actors) ? payload.actors : [];
    state.friends = Array.isArray(payload?.friends) ? payload.friends : [];
    const currentFormActor = documentRef.getElementById('actorId').value;
    const currentHistoryActor = documentRef.getElementById('historyActor').value;
    renderActorOptions(documentRef.getElementById('actorId'), state.actors, documentRef);
    renderActorOptions(documentRef.getElementById('historyActor'), state.actors, documentRef);
    if (state.actors.some(actor => actor.id === currentFormActor)) {
      documentRef.getElementById('actorId').value = currentFormActor;
    }
    if (state.actors.some(actor => actor.id === currentHistoryActor)) {
      documentRef.getElementById('historyActor').value = currentHistoryActor;
    }
    renderFriendGroups(documentRef);
    await loadVisits(documentRef);
  }

  async function testFriend(documentRef, friend) {
    status(documentRef, `正在测试“${friend.display_name}”…`);
    try {
      const info = await request('POST', `/api/lounge-friends/${encodeURIComponent(friend.id)}/test`, {
        actor_id: friend.actor_id,
      });
      status(
        documentRef,
        `连接成功：接待者 ${info.host_name || '未提供'}；会客室 ${info.lounge_state || '未知'}；身份${info.identity_claimed ? '已认领' : '未认领'}；输入上限 ${info.max_input_chars || '未知'} 字。`,
      );
    } catch (error) {
      status(documentRef, error.message);
    }
  }

  async function visitFriend(documentRef, friend) {
    const topic = globalThis.prompt?.(`想让${actorName(friend.actor_id)}和“${friend.display_name}”聊什么？`, '聊聊近况');
    if (!topic?.trim()) return;
    status(documentRef, `正在拜访“${friend.display_name}”…`);
    try {
      const result = await withVisitingActor(
        state.visitingActorIds,
        friend.actor_id,
        () => renderFriendGroups(documentRef),
        () => requestImmediateVisit({
          friend,
          topic: topic.trim(),
          request,
        }),
      );
      status(documentRef, `拜访${result.status === 'completed' ? '完成' : '中断'}，共 ${result.turn_count || 0} 回合。`);
      await loadFriends(documentRef);
    } catch (error) {
      status(documentRef, error.message);
    }
  }

  async function deleteFriend(documentRef, friend) {
    if (!globalThis.confirm?.(`确定删除“${friend.display_name}”吗？`)) return;
    try {
      await request(
        'DELETE',
        `/api/lounge-friends/${encodeURIComponent(friend.id)}?actor_id=${encodeURIComponent(friend.actor_id)}`,
      );
      status(documentRef, '好友已删除。');
      await loadFriends(documentRef);
    } catch (error) {
      status(documentRef, error.message);
    }
  }

  async function loadVisits(documentRef) {
    const actorId = documentRef.getElementById('historyActor').value;
    const list = documentRef.getElementById('visitList');
    if (state.visitRefreshTimer !== null) {
      clearTimeout(state.visitRefreshTimer);
      state.visitRefreshTimer = null;
    }
    if (state.historyActorId !== actorId) {
      state.historyActorId = actorId;
      state.expandedVisitId = null;
    }
    if (!actorId) {
      list.replaceChildren(textElement(documentRef, 'p', 'empty-state', '暂无本地 AI。'));
      return;
    }
    try {
      const payload = await request('GET', `/api/lounge-visits?actor_id=${encodeURIComponent(actorId)}&limit=50`);
      const visits = Array.isArray(payload?.visits) ? payload.visits : [];
      if (visits.some(visit => visit.status === 'running')) state.runningVisitActorIds.add(actorId);
      else state.runningVisitActorIds.delete(actorId);
      renderFriendGroups(documentRef);
      const rows = [];
      for (const visit of visits) {
        const entry = documentRef.createElement('article');
        entry.className = 'visit-entry';
        const expanded = state.expandedVisitId === visit.id;
        const summary = button(documentRef, '', 'visit-summary', () => {
          state.expandedVisitId = nextExpandedVisitId(state.expandedVisitId, visit.id);
          void loadVisits(documentRef);
        });
        summary.setAttribute('aria-expanded', String(expanded));

        const main = documentRef.createElement('span');
        main.className = 'visit-summary-main';
        main.append(
          textElement(documentRef, 'strong', '', friendName(visit.friend_id)),
          textElement(documentRef, 'span', 'visit-summary-topic', visit.topic || '未填写主题'),
        );
        const meta = documentRef.createElement('span');
        meta.className = 'visit-summary-meta';
        meta.append(
          textElement(documentRef, 'span', 'visit-status', visitStatusText(visit)),
          textElement(documentRef, 'time', 'visit-started-at', visitTimeText(visit.started_at)),
        );
        summary.append(main, meta);
        const header = documentRef.createElement('div');
        header.className = 'visit-entry-header';
        const deleteButton = button(
          documentRef,
          '删除',
          'visit-delete-button',
          () => deleteVisit(documentRef, actorId, visit),
        );
        deleteButton.disabled = !canDeleteVisit(visit);
        deleteButton.title = deleteButton.disabled ? '拜访结束后可以删除' : '删除这条拜访记录';
        header.append(summary, deleteButton);
        entry.appendChild(header);

        if (expanded) {
          try {
            const detail = await request(
              'GET',
              `/api/lounge-visits/${encodeURIComponent(visit.id)}?actor_id=${encodeURIComponent(actorId)}`,
            );
            entry.appendChild(renderVisitThread(documentRef, actorId, detail));
          } catch (error) {
            const thread = documentRef.createElement('div');
            thread.className = 'visit-thread';
            thread.appendChild(textElement(documentRef, 'p', 'empty-state', error.message));
            entry.appendChild(thread);
          }
        }
        rows.push(entry);
      }
      if (!rows.length) rows.push(textElement(documentRef, 'p', 'empty-state', '还没有拜访记录。'));
      list.replaceChildren(...rows);
      if (visits.some(visit => visit.status === 'running')) {
        state.visitRefreshTimer = setTimeout(() => loadVisits(documentRef), 3000);
      }
    } catch (error) {
      status(documentRef, error.message);
    }
  }

  async function deleteVisit(documentRef, actorId, visit) {
    try {
      const deleted = await requestDeleteVisit({
        visit,
        actorId,
        confirmDelete: () => Boolean(globalThis.confirm?.('确定删除这条拜访记录及完整对话吗？')),
        request,
      });
      if (!deleted) return;
      if (state.expandedVisitId === visit.id) state.expandedVisitId = null;
      status(documentRef, '拜访记录已删除。');
      await loadVisits(documentRef);
    } catch (error) {
      status(documentRef, error.message);
    }
  }

  function renderVisitThread(documentRef, actorId, visit) {
    const thread = documentRef.createElement('div');
    thread.className = 'visit-thread';
    thread.setAttribute('aria-live', 'polite');
    const messages = Array.isArray(visit?.messages) ? visit.messages : [];
    if (!messages.length) {
      thread.appendChild(textElement(documentRef, 'p', 'empty-state', '这次拜访还没有留下对话。'));
      return thread;
    }
    messages.forEach(message => {
      const direction = message.direction === 'inbound' ? 'inbound' : 'outbound';
      const bubble = documentRef.createElement('div');
      bubble.className = `visit-message ${direction}`;
      bubble.append(
        textElement(documentRef, 'span', 'visit-message-sender', direction === 'inbound' ? friendName(visit.friend_id) : actorName(actorId)),
        textElement(documentRef, 'p', 'visit-message-text', message.content || ''),
        textElement(documentRef, 'time', 'visit-message-time', messageTimeText(message.created_at)),
      );
      thread.appendChild(bubble);
    });
    return thread;
  }

  async function init(documentRef) {
    const form = documentRef.getElementById('friendForm');
    documentRef.getElementById('newFriendButton').addEventListener('click', () => openForm(documentRef));
    documentRef.getElementById('refreshButton').addEventListener('click', () => loadFriends(documentRef));
    documentRef.getElementById('closeDialogButton').addEventListener('click', () => closeForm(documentRef));
    documentRef.getElementById('cancelButton').addEventListener('click', () => closeForm(documentRef));
    documentRef.getElementById('historyActor').addEventListener('change', () => loadVisits(documentRef));
    form.addEventListener('submit', async event => {
      event.preventDefault();
      try {
        await saveFriend({
          friendId: state.editingId,
          elements: elements(documentRef),
          request,
        });
        closeForm(documentRef);
        status(documentRef, '好友设置已保存。');
        await loadFriends(documentRef);
      } catch (error) {
        status(documentRef, error.message);
      }
    });
    try {
      await loadFriends(documentRef);
    } catch (error) {
      status(documentRef, error.message);
    }
  }

  return {
    friendPayload,
    init,
    renderActorOptions,
    nextExpandedVisitId,
    canDeleteVisit,
    immediateVisitButtonState,
    requestDeleteVisit,
    requestImmediateVisit,
    saveFriend,
    visitStatusText,
    withVisitingActor,
  };
});

'use strict';

(function (root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.RoutineUI = api;
}(typeof window !== 'undefined' ? window : null, function () {
  const DAYS = ['一', '二', '三', '四', '五', '六', '日'];
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;',
  }[c]));

  function dateKey(value) {
    return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`;
  }

  function shiftDate(value, offset) {
    const day = new Date(value + 'T12:00:00');
    day.setDate(day.getDate() + offset);
    return dateKey(day);
  }

  function weekDates(value) {
    const weekday = (new Date(value + 'T12:00:00').getDay() + 6) % 7;
    return DAYS.map((_, index) => shiftDate(value, index - weekday));
  }

  function minutes(value) {
    const [hours, minute] = value.split(':').map(Number);
    return hours * 60 + minute;
  }

  function activeItemId(items, minute) {
    const active = items.filter((item, index) => {
      const start = minutes(item.start);
      const next = items.slice(index + 1).find(row => row.kind !== 'schedule' && row.start > item.start);
      let end = item.end ? minutes(item.end) : item.kind === 'schedule' ? start + 1 : next ? minutes(next.start) : 1440;
      if (end <= start) end += 1440;
      return minute >= start && minute < end;
    });
    active.sort((a, b) => b.start.localeCompare(a.start)
      || Number(Boolean(b.overridden)) - Number(Boolean(a.overridden))
      || Number(b.kind === 'schedule') - Number(a.kind === 'schedule')
      || a.id.localeCompare(b.id));
    return active[0]?.id || null;
  }

  function recordMeta(item, weekly) {
    const creator = escapeHtml(item.origin_name || '你');
    let status = weekly ? '每周计划' : item.kind === 'schedule' ? '仅记录' : '作息参考';
    if (!weekly && item.reminders?.length) {
      status = item.reminders.map(row => {
        const when = row.trigger_at.slice(0,10) === item.date ? row.trigger_at.slice(11,16) : row.trigger_at;
        const by = row.origin_name && row.origin_name !== item.origin_name ? '（' + escapeHtml(row.origin_name) + '）' : '';
        return `${row.type === 'monitor' ? '到点监督' : '已设提醒'} ${escapeHtml(when)}${by}`;
      }).join('；');
    } else if (!weekly && item.reminder_state === 'triggered') status = '已提醒';
    else if (!weekly && item.reminder_state === 'cancelled') status = '提醒已取消';
    return `<span class="routine-meta">${creator}添加 · ${status}</span>`;
  }

  function renderRows(items, options = {}) {
    if (!items.length) return '<div class="routine-empty">这一天还没有安排，点上方 ＋ 添加</div>';
    return items.map(item => {
      const current = item.id === options.currentId;
      const end = item.end ? `${item.end < item.start ? '次日 ' : ''}${item.end}` : '';
      return `<button type="button" class="routine-item" data-edit-id="${escapeHtml(item.id)}" data-current="${current}" ${current ? 'aria-current="time"' : ''} aria-label="编辑安排：${escapeHtml(item.content)}">
        <span class="routine-axis" aria-hidden="true"><span class="routine-dot"></span></span>
        <span class="routine-time">${escapeHtml(item.start)}${end ? `<span class="routine-end">${escapeHtml(end)}</span>` : ''}${item.kind === 'schedule' && !item.event_time_known ? '<span class="routine-end">提醒时点</span>' : ''}</span>
        <span class="routine-content">${escapeHtml(item.content)}${item.overridden ? '<span class="routine-day-badge">仅当天</span>' : ''}${recordMeta(item, options.weekly)}<span class="routine-now" ${current ? '' : 'hidden'}>${current ? `当前 · ${escapeHtml(options.nowText)}` : ''}</span></span>
      </button>`;
    }).join('');
  }

  function mount(root, { api, toast = () => {} }) {
    const doc = root.ownerDocument;
    const find = id => root.querySelector('#' + id);
    const state = { date: dateKey(new Date()), weekly: false, data: null, requestId: 0, positionCurrent: true };
    const editor = find('routineEditor');
    const form = find('routineForm');
    const copyDialog = find('routineCopyDialog');
    const copyForm = find('routineCopyForm');
    const scroll = find('routineScroll');
    const list = find('routineList');
    const shareControl = doc.getElementById('scheduleShare');
    const reminderEditor = find('routineScheduleEditor');
    const reminderForm = find('routineScheduleForm');
    let reminderEvent = null;
    let sharingBusy = false;
    let editing = null;

    function dayText() {
      return `${Number(state.date.slice(5, 7))}月${Number(state.date.slice(8))}日`;
    }

    function weekday() {
      return (new Date(state.date + 'T12:00:00').getDay() + 6) % 7;
    }

    function errorText(error) {
      const detail = error.payload?.detail;
      if (Array.isArray(detail)) return detail.map(item => item.msg.replace(/^Value error, /, '')).join('；');
      return error.message || '操作失败，请重试';
    }

    function showError(element, error) {
      element.textContent = typeof error === 'string' ? error : errorText(error);
      element.hidden = false;
      if (element.id === 'routineError') {
        element.parentElement.hidden = false;
        find('routineRetry').hidden = false;
      }
    }

    function setBusy(target, busy) {
      target.dataset.busy = String(busy);
      target.querySelectorAll('input, select, button').forEach(input => {
        if (busy) {
          input.dataset.routineWasDisabled = String(input.disabled);
          input.disabled = true;
        } else {
          input.disabled = input.dataset.routineWasDisabled === 'true';
          delete input.dataset.routineWasDisabled;
        }
      });
    }

    function selectedItems() {
      return state.data ? state.weekly ? state.data.weekly_items : state.data.items : [];
    }

    function clockState() {
      const now = new Date();
      const isToday = !state.weekly && state.date === dateKey(now);
      return {
        currentId: isToday ? activeItemId(selectedItems(), now.getHours() * 60 + now.getMinutes()) : null,
        nowText: `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`,
      };
    }

    function renderChrome() {
      const today = dateKey(new Date());
      find('routineDate').value = state.date;
      find('routineWeek').innerHTML = weekDates(state.date).map((date, index) => `<button type="button" class="routine-day" data-date="${date}" data-today="${date === today}" aria-pressed="${date === state.date}" aria-label="${Number(date.slice(5, 7))}月${Number(date.slice(8))}日，周${DAYS[index]}"><span>周${DAYS[index]}</span><strong>${Number(date.slice(8))}</strong></button>`).join('');
      find('routineHeading').textContent = state.weekly ? `每周${DAYS[weekday()]}` : `${dayText()} · ${state.date === today ? '今天' : '周' + DAYS[weekday()]}`;
      find('routineMode').textContent = state.weekly ? '查看当天' : '编辑每周';
      find('routineMode').setAttribute('aria-pressed', String(state.weekly));
      find('routineWeeklyTools').hidden = !state.weekly;
      find('routineAdd').setAttribute('aria-label', state.weekly ? '添加每周安排' : '添加安排');
      find('routineReset').hidden = state.weekly || !state.data?.has_overrides;
      if (shareControl && state.data && !sharingBusy) {
        shareControl.checked = state.data.share_enabled;
        shareControl.disabled = false;
      }
    }

    function positionCurrent() {
      if (!state.positionCurrent || !scroll.clientHeight || !state.data) return;
      const current = list.querySelector('[aria-current="time"]');
      scroll.scrollTop = current ? Math.max(0, current.offsetTop - (scroll.clientHeight - current.offsetHeight) / 2) : 0;
      state.positionCurrent = false;
    }

    function renderList() {
      const items = selectedItems();
      list.dataset.empty = String(!items.length);
      list.innerHTML = renderRows(items, {...clockState(), weekly:state.weekly});
      renderChrome();
      positionCurrent();
    }

    async function reload() {
      const requestId = ++state.requestId;
      const date = state.date;
      find('routineError').hidden = true;
      find('routineError').parentElement.hidden = true;
      find('routineRetry').hidden = true;
      root.setAttribute('aria-busy', 'true');
      list.innerHTML = '<div class="routine-empty">正在读取安排…</div>';
      list.dataset.empty = 'true';
      try {
        const data = await api('GET', `/api/routines/timeline?date=${date}`);
        if (requestId !== state.requestId) return;
        state.data = data;
        renderList();
      } catch (error) {
        if (requestId !== state.requestId) return;
        state.data = null;
        list.innerHTML = '<div class="routine-empty">暂时无法读取安排</div>';
        showError(find('routineError'), error);
        renderChrome();
      } finally {
        if (requestId === state.requestId) root.setAttribute('aria-busy', 'false');
      }
    }

    function selectDate(date) {
      state.date = date;
      state.data = null;
      state.positionCurrent = true;
      renderChrome();
      reload();
    }

    function updateScopeLabel() {
      const weekly = form.elements.scope.value === 'weekly';
      find('routineDelete').textContent = weekly ? `删除每周${DAYS[weekday()]}这项` : '仅删除这一天';
      find('routineRemindControl').hidden = weekly;
      find('routineRemindFields').hidden = weekly || !form.elements.remind.checked;
      form.elements.reminder_at.required = !weekly && form.elements.remind.checked;
    }

    function openEditor(item = null) {
      editing = item;
      form.reset();
      find('routineEditorTitle').textContent = item ? '编辑安排' : '添加安排';
      form.elements.content.value = item?.content || '';
      form.elements.start.value = item?.start || '09:00';
      form.elements.end.value = item?.end || '';
      form.elements.scope.innerHTML = `<option value="weekly">每周${DAYS[weekday()]}重复</option><option value="day">仅 ${dayText()}</option>`;
      form.elements.scope.value = state.weekly ? 'weekly' : 'day';
      const pending = item?.reminders?.find(row => row.type === 'alarm');
      form.elements.remind.checked = Boolean(pending);
      form.elements.reminder_at.value = (pending?.trigger_at || state.date + 'T' + (item?.start || '09:00')).replace(' ', 'T').slice(0,16);
      find('routineDelete').hidden = !item;
      find('routineFormError').hidden = true;
      updateScopeLabel();
      editor.showModal();
      form.elements.content.focus();
    }

    function selectReminderRecord() {
      const record = reminderEvent.schedule_records.find(row => row.id === reminderForm.elements.record.value);
      reminderForm.elements.content.value = record?.content || reminderEvent.content;
      reminderForm.elements.type.value = record?.type || 'alarm';
      reminderForm.elements.trigger_at.value = (record?.trigger_at || `${state.date}T${reminderEvent.start}`).replace(' ', 'T').slice(0,16);
      reminderForm.elements.event_at.value = (record?.event_at || '').replace(' ', 'T').slice(0,16);
      find('routineScheduleDelete').hidden = !record;
    }

    function openReminderEditor(item) {
      reminderEvent = item;
      const active = item.schedule_records.filter(row => row.status === 'active');
      reminderForm.elements.record.innerHTML = active.map(row => `<option value="${escapeHtml(row.id)}">${escapeHtml(row.trigger_at)} · ${escapeHtml(row.origin_name)}</option>`).join('') + '<option value="">新增提醒或记录</option>';
      reminderForm.elements.record.value = active[0]?.id || '';
      find('routineScheduleError').hidden = true;
      selectReminderRecord();
      reminderEditor.showModal();
    }

    async function saveReminder(event) {
      event.preventDefault();
      if (reminderForm.dataset.busy === 'true') return;
      const id = reminderForm.elements.record.value;
      const body = {type:reminderForm.elements.type.value,trigger_at:reminderForm.elements.trigger_at.value,
        content:reminderForm.elements.content.value.trim(),event_at:reminderForm.elements.event_at.value,event_key:reminderEvent.event_key};
      if (!body.content) { showError(find('routineScheduleError'), '请填写内容'); return; }
      setBusy(reminderForm, true);
      try {
        await api(id ? 'PUT' : 'POST', '/api/schedules' + (id ? '/' + encodeURIComponent(id) : ''), body);
        reminderEditor.close();
        await reload();
        toast('已保存日程');
      } catch(error) { showError(find('routineScheduleError'),error); }
      finally { setBusy(reminderForm,false); }
    }

    async function cancelReminder() {
      const id = reminderForm.elements.record.value;
      if (!id || !doc.defaultView.confirm('取消这条提醒或记录？')) return;
      setBusy(reminderForm, true);
      try {
        await api('DELETE','/api/schedules/'+encodeURIComponent(id));
        reminderEditor.close();
        await reload();
        toast('已取消这条提醒');
      } catch(error) { showError(find('routineScheduleError'),error); }
      finally { setBusy(reminderForm,false); }
    }

    async function save(event) {
      event.preventDefault();
      if (form.dataset.busy === 'true') return;
      const content = form.elements.content.value.trim();
      if (!content) { showError(find('routineFormError'), '请填写安排内容'); return; }
      const body = { date: state.date, scope: form.elements.scope.value, apply_to_date: !state.weekly, content, start: form.elements.start.value, end: form.elements.end.value };
      if (body.scope === 'day') {
        body.remind = form.elements.remind.checked;
        body.reminder_at = body.remind ? form.elements.reminder_at.value : '';
      }
      const id = editing?.id;
      find('routineFormError').hidden = true;
      setBusy(form, true);
      try {
        await api(id ? 'PUT' : 'POST', '/api/routines/items' + (id ? '/' + encodeURIComponent(id) : ''), body);
        editor.close();
        await reload();
        toast(body.scope === 'weekly' ? `已保存每周${DAYS[weekday()]}的安排` : '已保存当天调整');
      } catch (error) {
        showError(find('routineFormError'), error);
      } finally {
        setBusy(form, false);
      }
    }

    async function removeItem() {
      const scope = form.elements.scope.value;
      if (!editing || !doc.defaultView.confirm(`确定删除${scope === 'weekly' ? '每周' + DAYS[weekday()] : dayText()}的“${editing.content}”？`)) return;
      setBusy(form, true);
      try {
        await api('DELETE', `/api/routines/items/${encodeURIComponent(editing.id)}?date=${state.date}&scope=${scope}`);
        editor.close();
        await reload();
        toast('已删除安排');
      } catch (error) {
        showError(find('routineFormError'), error);
      } finally {
        setBusy(form, false);
      }
    }

    function openCopy() {
      find('routineCopyTitle').textContent = `把每周${DAYS[weekday()]}复制到`;
      find('routineCopyDays').innerHTML = DAYS.map((day, index) => `<label class="routine-copy-day"><input type="checkbox" name="weekday" value="${index}" ${index === weekday() ? 'disabled' : ''}><span>周${day}</span></label>`).join('');
      find('routineCopyError').hidden = true;
      copyDialog.showModal();
    }

    async function copy(event) {
      event.preventDefault();
      if (copyForm.dataset.busy === 'true') return;
      const targets = Array.from(copyForm.querySelectorAll('input:checked')).map(input => Number(input.value));
      if (!targets.length) { showError(find('routineCopyError'), '请选择要复制到的星期'); return; }
      setBusy(copyForm, true);
      try {
        await api('POST', '/api/routines/copy', { source_weekday: weekday(), target_weekdays: targets });
        copyDialog.close();
        toast('已复制，每个星期仍可单独修改');
        await reload();
      } catch (error) {
        showError(find('routineCopyError'), error);
      } finally {
        setBusy(copyForm, false);
      }
    }

    async function resetDay() {
      if (!doc.defaultView.confirm(`恢复${dayText()}的每周安排？这天的临时修改将清除。`)) return;
      find('routineReset').disabled = true;
      try {
        await api('DELETE', `/api/routines/overrides/${state.date}`);
        await reload();
        toast('已恢复每周安排');
      } catch (error) {
        showError(find('routineError'), error);
      } finally {
        find('routineReset').disabled = false;
      }
    }

    function refreshClock() {
      if (!state.data) return;
      const clock = clockState();
      list.querySelectorAll('[data-edit-id]').forEach(button => {
        const current = button.dataset.editId === clock.currentId;
        button.dataset.current = String(current);
        if (current) button.setAttribute('aria-current', 'time');
        else button.removeAttribute('aria-current');
        const label = button.querySelector('.routine-now');
        label.hidden = !current;
        label.textContent = current ? `当前 · ${clock.nowText}` : '';
      });
    }

    root.addEventListener('click', event => {
      const button = event.target.closest('button');
      if (!button || button.disabled) return;
      if (button.dataset.date) { selectDate(button.dataset.date); return; }
      if (button.dataset.editId) {
        const item = selectedItems().find(row => row.id === button.dataset.editId);
        if (item?.kind === 'schedule') openReminderEditor(item);
        else if (item) openEditor(item);
        return;
      }
      switch (button.dataset.routineAction) {
        case 'previous': selectDate(shiftDate(state.date, -7)); break;
        case 'next': selectDate(shiftDate(state.date, 7)); break;
        case 'today': selectDate(dateKey(new Date())); break;
        case 'mode': state.weekly = !state.weekly; state.positionCurrent = true; if (state.data) renderList(); else renderChrome(); break;
        case 'add': if (state.data) openEditor(); break;
        case 'close-editor': editor.close(); break;
        case 'delete': removeItem(); break;
        case 'copy': openCopy(); break;
        case 'close-copy': copyDialog.close(); break;
        case 'reset': resetDay(); break;
        case 'retry': reload(); break;
        case 'close-schedule': reminderEditor.close(); break;
        case 'cancel-schedule': cancelReminder(); break;
      }
    });
    find('routineDate').addEventListener('change', event => {
      if (event.target.value) selectDate(event.target.value);
      else renderChrome();
    });
    form.elements.scope.addEventListener('change', updateScopeLabel);
    form.elements.remind.addEventListener('change', updateScopeLabel);
    reminderForm.elements.record.addEventListener('change', selectReminderRecord);
    reminderForm.addEventListener('submit', saveReminder);
    reminderEditor.addEventListener('cancel', event => { if (reminderForm.dataset.busy === 'true') event.preventDefault(); });
    if (shareControl) shareControl.addEventListener('change', async () => {
      const enabled = shareControl.checked;
      sharingBusy = true;
      shareControl.disabled = true;
      try {
        await api('PUT','/api/routines/sharing',{enabled});
        toast(enabled ? '已开启默认作息共享' : '已关闭默认作息共享');
        if(state.data) state.data.share_enabled=enabled;
      } catch(error) {
        shareControl.checked=!enabled;
        showError(find('routineError'),error);
        toast(errorText(error));
      } finally {
        sharingBusy=false;
        shareControl.disabled=false;
      }
    });
    form.addEventListener('submit', save);
    copyForm.addEventListener('submit', copy);
    editor.addEventListener('cancel', event => { if (form.dataset.busy === 'true') event.preventDefault(); });
    copyDialog.addEventListener('cancel', event => { if (copyForm.dataset.busy === 'true') event.preventDefault(); });
    const timer = setInterval(() => {
      if (!root.isConnected) { clearInterval(timer); return; }
      refreshClock();
    }, 30000);
    renderChrome();
    return { reload, onVisible() { refreshClock(); positionCurrent(); } };
  }

  return { mount, weekDates, shiftDate, activeItemId, renderRows };
}));

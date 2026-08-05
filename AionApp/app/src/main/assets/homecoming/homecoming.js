(() => {
  "use strict";
  const api = window.HomecomingNative;
  const state = {
    timeline: "group",
    page: "chat",
    requestId: "",
    bootstrap: null,
    enterArmed: false,
    returnArmed: false,
    returnDismissed: false
  };
  const byId = id => document.getElementById(id);
  const uuid = () => "hc-" + Date.now() + "-" + Math.random().toString(16).slice(2);
  const metric = (root, value, label) => {
    const box = document.createElement("div");
    box.className = "metric";
    const strong = document.createElement("strong");
    strong.textContent = String(value || 0);
    const span = document.createElement("span");
    span.textContent = label;
    box.append(strong, span);
    root.appendChild(box);
  };

  function readiness() {
    try { return JSON.parse(api.getReadinessJson()); }
    catch (_) { return { ready: false, warning: "归巢数据暂不可读" }; }
  }

  function fillSelect(select, rows, labelKey, valueKey) {
    select.textContent = "";
    rows.forEach(row => {
      const option = document.createElement("option");
      option.value = row[valueKey];
      option.textContent = row[labelKey];
      select.appendChild(option);
    });
  }

  function renderReadiness() {
    const data = readiness();
    byId("status").textContent = data.ready
      ? `快照时间：${data.snapshotCreatedAt || "—"}` : "尚未准备好可进入的归巢快照";
    byId("warning").textContent = data.warning || "";
    const summary = byId("summary");
    summary.textContent = "";
    metric(summary, data.mainMemoryCount, "伴侣一记忆");
    metric(summary, data.secondMemoryCount, "伴侣二记忆");
    metric(summary, data.mainMessageCount, "主线消息");
    metric(summary, data.secondMessageCount, "伴侣私聊");
    metric(summary, data.groupMessageCount, "群聊消息");
    metric(summary, data.portableRouteCount, "可用云线路");
  }

  function initializeChat() {
    try { state.bootstrap = JSON.parse(api.getBootstrapJson()); }
    catch (_) { state.bootstrap = null; }
    if (!state.bootstrap || !state.bootstrap.identity) return;
    const routes = state.bootstrap.routes || [];
    const responders = [
      { id: "main", label: state.bootstrap.identity.mainName },
      { id: "second", label: state.bootstrap.identity.secondName }
    ];
    fillSelect(byId("memoryOwner"), responders, "label", "id");
    fillSelect(byId("scheduleOwner"), responders, "label", "id");
    fillSelect(byId("settingsOwner"), responders, "label", "id");
    fillSelect(byId("routeSelect"), routes, "label", "routeId");
    byId("ttsEnabled").checked = Boolean(state.bootstrap.ttsEnabled);
    switchTimeline(state.timeline);
    switchPage(state.page);
    applyRoutePreference();
    updateModels();
    loadMessages();
  }

  function applyRoutePreference() {
    const owner = byId("settingsOwner").value || "main";
    const preference = state.bootstrap?.routePreferences?.[owner] || {};
    const routes = state.bootstrap?.routes || [];
    const chosen = routes.some(item => item.routeId === preference.routeId)
      ? preference.routeId
      : routes[0]?.routeId || "";
    byId("routeSelect").value = chosen;
  }

  function updateModels() {
    const route = (state.bootstrap?.routes || [])
      .find(item => item.routeId === byId("routeSelect").value);
    const models = (route?.modelKeys || []).map(key => ({ key, label: key }));
    fillSelect(byId("modelSelect"), models, "label", "key");
    const owner = byId("settingsOwner").value || "main";
    const preferred = state.bootstrap?.routePreferences?.[owner]?.modelId || "";
    if (models.some(item => item.key === preferred)) {
      byId("modelSelect").value = preferred;
    }
  }

  function saveRoutePreference() {
    const owner = byId("settingsOwner").value || "main";
    const routeId = byId("routeSelect").value;
    const modelId = byId("modelSelect").value;
    if (!routeId || !modelId) return;
    api.setRoutePreference(owner, routeId, modelId);
    state.bootstrap.routePreferences ||= {};
    state.bootstrap.routePreferences[owner] = { routeId, modelId };
  }

  function switchTimeline(timeline) {
    state.timeline = timeline;
    const identity = state.bootstrap?.identity || {};
    const labels = {
      main_private: identity.mainName || "主伴侣私聊",
      companion_private: identity.secondName || "第二伴侣私聊",
      group: "三人聊天"
    };
    byId("chatTitle").textContent = labels[timeline] || "当前会话";
    document.querySelectorAll("[data-timeline]").forEach(item =>
      item.classList.toggle("active", item.dataset.timeline === timeline));
    byId("conversationMenu").classList.add("hidden");
    byId("conversationPicker").setAttribute("aria-expanded", "false");
    loadMessages();
  }

  function switchPage(page) {
    state.page = page;
    document.querySelectorAll("[data-page-panel]").forEach(item =>
      item.classList.toggle("active", item.dataset.pagePanel === page));
    document.querySelectorAll("[data-page]").forEach(item =>
      item.classList.toggle("active", item.dataset.page === page));
    if (page === "memory") renderMemories();
    if (page === "schedule") renderSchedules();
  }

  function loadMessages() {
    let rows = [];
    try { rows = JSON.parse(api.getMessagesJson(state.timeline, 0, 160)); }
    catch (_) {}
    const root = byId("messages");
    root.textContent = "";
    rows.forEach(appendMessage);
    root.scrollTop = root.scrollHeight;
  }

  function appendMessage(row) {
    const item = document.createElement("article");
    item.className = "message " + (row.assistant ? "assistant" : "user");
    const text = document.createElement("div");
    text.textContent = row.text || "";
    const meta = document.createElement("small");
    meta.textContent = row.sender || "";
    item.append(text, meta);
    if (row.assistant && row.id) {
      const replay = document.createElement("button");
      replay.textContent = "重听";
      replay.addEventListener("click", () => api.replayTts(row.id));
      item.appendChild(replay);
    }
    byId("messages").appendChild(item);
  }

  function send() {
    const text = byId("composer").value.trim();
    if (!text && !byId("mediaState").textContent) return;
    state.requestId = uuid();
    const owner = state.timeline === "main_private" ? "main"
      : state.timeline === "companion_private" ? "second"
      : (byId("settingsOwner").value || "main");
    appendMessage({ text, sender: state.bootstrap.identity.userName, assistant: false });
    byId("composer").value = "";
    byId("mediaState").textContent = "";
    window.HomecomingNative.sendMessage(state.requestId, state.timeline, owner, text,
      byId("routeSelect").value, byId("modelSelect").value);
  }

  function renderMemories() {
    const owner = byId("memoryOwner").value || "main";
    let rows = [];
    try { rows = JSON.parse(api.getMemoriesJson(owner, "")); } catch (_) {}
    const root = byId("memoryList");
    root.textContent = "";
    rows.forEach(row => {
      const item = document.createElement("article");
      item.className = "memory-item";
      const text = document.createElement("p");
      text.textContent = row.content;
      const actions = document.createElement("div");
      actions.className = "memory-actions";
      const edit = document.createElement("button");
      edit.textContent = "编辑";
      edit.addEventListener("click", () => {
        const changed = window.prompt("编辑记忆", row.content);
        if (changed !== null && changed.trim()) {
          window.HomecomingNative.updateMemory(
            owner, row.id, changed.trim(), row.baseHash || "local");
          renderMemories();
        }
      });
      const remove = document.createElement("button");
      remove.textContent = "删除";
      remove.addEventListener("click", () => {
        if (window.confirm("确认删除这条记忆？")) {
          window.HomecomingNative.deleteMemory(owner, row.id, row.baseHash || "local");
          renderMemories();
        }
      });
      actions.append(edit, remove);
      item.append(text, actions);
      root.appendChild(item);
    });
  }

  function renderSchedules() {
    let rows = [];
    try { rows = JSON.parse(window.HomecomingNative.listSchedules()); } catch (_) {}
    const exactness = state.bootstrap?.scheduleExactness || "unknown";
    byId("scheduleExactness").textContent = exactness === "exact"
      ? "系统允许精确触发"
      : exactness === "inexact"
        ? "系统将尽量准时触发；可在系统设置中允许精确闹铃"
        : "触发精度将在创建日程时确认";
    const names = {
      main: state.bootstrap?.identity?.mainName || "伴侣一",
      second: state.bootstrap?.identity?.secondName || "伴侣二"
    };
    const labels = { alarm: "闹铃", reminder: "提醒", monitor: "定时监控" };
    const root = byId("scheduleList");
    root.textContent = "";
    rows.forEach(row => {
      const item = document.createElement("article");
      item.className = "schedule-item";
      const title = document.createElement("strong");
      title.textContent = `${labels[row.type] || row.type} · ${names[row.ownerId] || row.ownerId}`;
      const content = document.createElement("p");
      content.textContent = row.content || "";
      const time = document.createElement("small");
      time.textContent = `${new Date(row.triggerAt).toLocaleString()} · 待执行`;
      const remove = document.createElement("button");
      remove.textContent = "删除";
      remove.addEventListener("click", () => {
        if (window.confirm("确认删除这个归巢日程？")) {
          window.HomecomingNative.deleteSchedule(row.id);
          renderSchedules();
        }
      });
      item.append(title, content, time, remove);
      root.appendChild(item);
    });
  }

  function renderSupervisionStatus() {
    let data = { enabled: false, readiness: "unavailable", groups: [] };
    try {
      data = JSON.parse(window.HomecomingNative.getSupervisionStatusJson());
    } catch (_) {}
    const readinessLabels = {
      ready: "权限就绪",
      degraded: "部分权限不可用",
      unavailable: "监督状态暂不可用"
    };
    byId("supervisionReadiness").textContent = data.enabled
      ? (readinessLabels[data.readiness] || "状态未知")
      : "手机监督功能未启用";
    const root = byId("supervisionList");
    root.textContent = "";
    (data.groups || []).forEach(group => {
      const item = document.createElement("article");
      item.className = "supervision-item";
      const title = document.createElement("strong");
      title.textContent = group.displayName || group.groupId || "应用组";
      const role = document.createElement("p");
      role.textContent = `负责伴侣：${group.roleLabel || "未配置"}`;
      const usage = document.createElement("p");
      usage.textContent = `本轮使用：${Math.floor((group.roundUsageMs || 0) / 60000)} 分钟`;
      const checkpoints = document.createElement("p");
      checkpoints.textContent = `检查点：${(group.checkpointsMinutes || []).join("、") || "无"} 分钟`;
      const effective = document.createElement("small");
      effective.textContent = `当前状态：${group.effectiveState || "UNKNOWN"}`;
      item.append(title, role, usage, checkpoints, effective);
      root.appendChild(item);
    });
  }

  function returnState() {
    try { return JSON.parse(api.getReturnStateJson()); }
    catch (_) {
      return {
        open: true,
        phase: "failed",
        failure: "local_package_unavailable",
        counts: {},
        canRetry: true,
        canReturnWithoutSync: false
      };
    }
  }

  function renderReturn() {
    const data = returnState();
    const panel = byId("returnPanel");
    panel.classList.toggle("hidden", !data.open || state.returnDismissed);
    if (!data.open || state.returnDismissed) return;

    const order = {
      freezing: 0,
      building: 1,
      frozen: 1,
      uploading: 2,
      returning: 2,
      planning: 3,
      applying: 4,
      verifying: 4,
      confirming: 4,
      complete: 5
    };
    const position = order[data.phase] ?? -1;
    byId("returnPhases").querySelectorAll("li").forEach((item, index) => {
      item.classList.toggle("active", index === position);
      item.classList.toggle("done", index < position);
    });
    const phaseLabels = {
      idle: "等待你手动开始",
      ready: "已准备好冻结并生成回归包",
      freezing: "正在停止归巢运行任务",
      building: "正在生成本机不可变回归包",
      frozen: "本机回归包已保存",
      uploading: "正在上传服务器隔离区",
      returning: "正在回传",
      planning: "服务器正在只读预演",
      applying: "服务器正在小批量导入",
      verifying: "正在核验服务器回执",
      confirming: "正在确认本机操作",
      complete: "回传完成，正在返回正常模式",
      failed: "回传暂未完成"
    };
    byId("returnStatus").textContent = phaseLabels[data.phase] || "等待手动操作";
    const failureLabels = {
      package_build_failed: "回归包生成失败，归巢已恢复，可稍后再试",
      server_retryable: "服务器当前繁忙，请稍后手动重试",
      receipt_mismatch: "服务器回执校验不一致，本机数据尚未确认",
      return_sync_failed: "网络或服务器暂不可用，本机回归包已保留",
      upload_package_mismatch: "服务器返回的包标识不一致，已停止回传",
      apply_incomplete: "本次前台回传未完成，请手动重试",
      no_pending_package: "没有找到可回传的本机数据包",
      local_package_unavailable: "本机回归包暂不可读"
    };
    byId("returnFailure").textContent =
      failureLabels[data.failure] || (data.failure ? "回传暂未完成" : "");
    const counts = byId("returnCounts");
    counts.textContent = "";
    const countLabels = {
      apply: "可写入",
      duplicate: "已存在",
      server_wins: "主线保留",
      skip: "已跳过",
      quarantine: "已隔离",
      invalid: "无效"
    };
    Object.keys(countLabels).forEach(key => {
      if (Object.prototype.hasOwnProperty.call(data.counts || {}, key)) {
        metric(counts, data.counts[key], countLabels[key]);
      }
    });
    const mayStart = !data.inFlight && !data.failure
      && (data.mode === "active" || (data.pendingPackageCount || 0) > 0);
    byId("startReturnSync").classList.toggle("hidden", !mayStart);
    byId("retryReturnSync").classList.toggle("hidden", !data.canRetry);
    byId("returnWithoutSync").classList.toggle(
      "hidden", !data.canReturnWithoutSync);
    byId("closeReturnPanel").classList.toggle(
      "hidden", data.mode !== "active" || data.inFlight);
  }

  function render() {
    const active = Boolean(api.isActive());
    byId("readinessCard").classList.toggle("hidden", active);
    byId("chatShell").classList.toggle("hidden", !active);
    if (active) initializeChat(); else renderReadiness();
    renderReturn();
  }

  function onNativeEvent(event) {
    if (event.type === "complete") {
      appendMessage({ id: event.value, text: event.text, sender: "", assistant: true });
      byId("messages").scrollTop = byId("messages").scrollHeight;
      state.requestId = "";
    } else if (event.type === "failure") {
      byId("mediaState").textContent = "回复失败：" + (event.value || "");
      state.requestId = "";
    } else if (event.type === "group_reply_complete") {
      loadMessages();
    } else if (event.type === "group_reply_failure") {
      const identity = state.bootstrap.identity || {};
      const name = event.ownerId === "second"
        ? (identity.secondName || "AI")
        : (identity.mainName || "AI");
      byId("mediaState").textContent = `${name} 回复失败`;
    } else if (event.type === "group_complete") {
      state.requestId = "";
      byId("mediaState").textContent = "";
      loadMessages();
    } else if (event.type === "media_ready" || event.type === "media_failure") {
      byId("mediaState").textContent = event.value || "";
    } else if (event.type === "summary_all_result") {
      const results = event.results || {};
      const values = [results.main, results.second].filter(Boolean);
      const processed = values.reduce((sum, value) =>
        sum + Number(value.processed || 0), 0);
      const created = values.reduce((sum, value) =>
        sum + Number(value.created || 0), 0);
      const failed = values.some(value => value.status === "summary_failed");
      byId("mediaState").textContent = failed
        ? `记忆总结部分完成：处理 ${processed} 条，新增 ${created} 条记忆`
        : processed > 0
          ? `记忆总结完成：处理 ${processed} 条，新增 ${created} 条记忆`
          : "暂无新增聊天需要总结";
      renderMemories();
    } else if (event.type === "summary_result") {
      byId("mediaState").textContent = event.text === "complete"
        ? `记忆总结完成，新增 ${event.value || "0"} 条`
        : event.text === "minimum_not_met"
          ? "新增聊天还不够一组，暂不总结"
          : "记忆总结暂未完成，稍后可以重试";
      renderMemories();
    } else if (event.type === "schedule_changed") {
      renderSchedules();
    } else if (event.type === "schedule_fired") {
      byId("mediaState").textContent = event.value || "归巢日程已执行";
      renderSchedules();
      loadMessages();
    } else if (event.type === "schedule_failed") {
      byId("mediaState").textContent = event.value || "归巢日程执行失败，可稍后重试";
      renderSchedules();
    }
  }

  byId("enter").addEventListener("click", event => {
    if (!state.enterArmed) {
      state.enterArmed = true;
      event.currentTarget.textContent = "再次确认进入归巢";
      return;
    }
    api.confirmEnter();
  });
  byId("cancel").addEventListener("click", () => api.cancelEnter());
  byId("return").addEventListener("click", event => {
    if (!state.returnArmed) {
      state.returnArmed = true;
      event.currentTarget.textContent = "再次确认返回正常模式";
      return;
    }
    state.returnDismissed = false;
    api.requestFoundationReturn();
  });
  byId("closeReturnPanel").addEventListener("click", () => {
    state.returnDismissed = true;
    renderReturn();
  });
  byId("startReturnSync").addEventListener("click", () => {
    state.returnDismissed = false;
    api.startReturnSync();
  });
  byId("retryReturnSync").addEventListener("click", () => {
    state.returnDismissed = false;
    api.retryReturnSync();
  });
  byId("returnWithoutSync").addEventListener("click", () => {
    if (window.confirm("确认保留本机回归包并直接返回正常模式？")) {
      api.returnWithoutSync();
    }
  });
  document.querySelectorAll("[data-timeline]").forEach(button => {
    button.addEventListener("click", () => switchTimeline(button.dataset.timeline));
  });
  document.querySelectorAll("[data-page]").forEach(button => {
    button.addEventListener("click", () => switchPage(button.dataset.page));
  });
  byId("conversationPicker").addEventListener("click", () => {
    const menu = byId("conversationMenu");
    const opening = menu.classList.contains("hidden");
    menu.classList.toggle("hidden", !opening);
    byId("conversationPicker").setAttribute(
      "aria-expanded", opening ? "true" : "false");
  });
  byId("routeSelect").addEventListener("change", () => {
    updateModels();
    saveRoutePreference();
  });
  byId("modelSelect").addEventListener("change", saveRoutePreference);
  byId("settingsOwner").addEventListener("change", () => {
    applyRoutePreference();
    updateModels();
  });
  byId("send").addEventListener("click", send);
  byId("stopReply").addEventListener("click", () => {
    if (state.requestId) api.stopMessage(state.requestId);
  });
  byId("pickImage").addEventListener("click", () => api.pickImage());
  byId("captureImage").addEventListener("click", () => api.captureImage());
  byId("ttsEnabled").addEventListener("change", event =>
    api.setTtsEnabled(event.currentTarget.checked));
  byId("memoryOwner").addEventListener("change", renderMemories);
  byId("scheduleTab").addEventListener("click", () => {
    byId("scheduleTab").classList.add("active");
    byId("supervisionTab").classList.remove("active");
    byId("schedulePanel").classList.add("active");
    byId("supervisionPanel").classList.remove("active");
    renderSchedules();
  });
  byId("supervisionTab").addEventListener("click", () => {
    byId("supervisionTab").classList.add("active");
    byId("scheduleTab").classList.remove("active");
    byId("supervisionPanel").classList.add("active");
    byId("schedulePanel").classList.remove("active");
    renderSupervisionStatus();
  });
  byId("scheduleOwner").addEventListener("change", event => {
    if (byId("scheduleTimeline").value !== "group") {
      byId("scheduleTimeline").value = event.currentTarget.value === "second"
        ? "companion_private" : "main_private";
    }
  });
  byId("addSchedule").addEventListener("click", () => {
    const content = byId("scheduleContent").value.trim();
    const triggerAt = new Date(byId("scheduleTime").value).getTime();
    if (!content || !Number.isFinite(triggerAt) || triggerAt <= Date.now()) return;
    let created = {};
    try {
      created = JSON.parse(window.HomecomingNative.createSchedule(
        byId("scheduleType").value,
        triggerAt,
        content,
        byId("scheduleOwner").value,
        byId("scheduleTimeline").value));
    } catch (_) {}
    if (created.registrationStatus === "registration_failed") {
      byId("scheduleExactness").textContent =
        "日程已保存，但系统闹铃注册失败；重启应用后会再次恢复";
    }
    byId("scheduleContent").value = "";
    renderSchedules();
  });
  byId("summarizeMemories").addEventListener("click", () =>
    window.HomecomingNative.summarizeAllMemories());
  byId("addMemory").addEventListener("click", () => {
    const content = byId("memoryText").value.trim();
    if (!content) return;
    window.HomecomingNative.createMemory(byId("memoryOwner").value, content, "");
    byId("memoryText").value = "";
    renderMemories();
  });
  window.HomecomingPage = { render, onNativeEvent };
  render();
})();

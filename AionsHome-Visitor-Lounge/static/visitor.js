(() => {
  "use strict";

  const page = document.body.dataset.page;
  const errorNode = document.getElementById("form-error");
  const connectionErrorMessage = "会客室连接暂时中断，请稍后重新发送。";

  async function api(path, options = {}) {
    let response;
    try {
      response = await fetch(path, {
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", ...(options.headers || {}) },
        ...options,
      });
    } catch (_) {
      throw new Error(connectionErrorMessage);
    }
    const data = await response.json().catch(() => null);
    if (data === null) throw new Error(connectionErrorMessage);
    if (!response.ok) {
      const detail = typeof data.detail === "string" ? data.detail : data.detail?.message;
      const error = new Error(detail || connectionErrorMessage);
      error.templateText = data.detail?.template_text || "";
      throw error;
    }
    return data;
  }

  if (page === "login") {
    document.getElementById("login-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      errorNode.textContent = "";
      const input = document.getElementById("visitor-key");
      try {
        await api("/api/login", {
          method: "POST",
          body: JSON.stringify({ key: input.value, device_id: crypto.randomUUID() }),
        });
        input.value = "";
        location.reload();
      } catch (error) {
        errorNode.textContent = error.message;
      }
    });
    return;
  }

  if (page === "claim") {
    document.getElementById("claim-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      errorNode.textContent = "";
      try {
        await api("/api/claim", {
          method: "POST",
          body: JSON.stringify({
            name: document.getElementById("visitor-name").value,
            consent: document.getElementById("recording-consent").checked,
          }),
        });
        location.reload();
      } catch (error) {
        errorNode.textContent = error.message;
      }
    });
    return;
  }

  if (page !== "chat") return;

  const state = window.LOUNGE_STATE;
  const form = document.getElementById("message-form");
  const textarea = document.getElementById("message-text");
  const sendButton = document.getElementById("send-button");
  const count = document.getElementById("character-count");
  const jobStatus = document.getElementById("job-status");
  const messages = document.getElementById("messages");
  const quotaReset = document.getElementById("quota-reset");
  let stream = null;
  let pollTimer = null;
  let pollCount = 0;
  let pending = Boolean(state.job && ["queued", "generating"].includes(state.job.status));
  const messageLimit = 500;
  const messageLimitError = "消息不能超过 500 个字符";

  function messageLength() {
    return Array.from(textarea.value).length;
  }

  function updateComposer() {
    const length = messageLength();
    const overLimit = length > messageLimit;
    const locked = ["suspended", "safety_lock"].includes(state.visitor_status);
    count.textContent = String(length);
    sendButton.disabled = pending || locked || overLimit;
    textarea.disabled = locked;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 120)}px`;
    if (overLimit) errorNode.textContent = messageLimitError;
    else if (errorNode.textContent === messageLimitError) errorNode.textContent = "";
  }

  function setPending(value) {
    pending = value;
    updateComposer();
  }

  function statusText(job) {
    if (!job) return "";
    if (job.status === "queued") return `排队中 · 第 ${job.queue_position || "?"} 位`;
    if (job.status === "generating") return "正在生成…";
    if (job.status === "completed") return "已完成";
    return "本次生成未完成，可稍后重试";
  }

  function visitorStatusText(status) {
    if (status === "active") return "会客中";
    if (status === "suspended") return "已挂起";
    if (status === "safety_lock") return "已锁定";
    if (status === "paused") return "已暂停";
    return status;
  }

  function addConversationDate() {
    const marker = document.createElement("div");
    marker.className = "conversation-date";
    marker.textContent = "今天 · 会客室";
    messages.append(marker);
  }

  function updateMessageMetadata(article, message = {}) {
    if (message.id) article.dataset.messageId = message.id;
    if (message.source) article.dataset.source = message.source;
    if (message.delivery_status) {
      article.dataset.deliveryStatus = message.delivery_status;
    }
    let meta = article.querySelector(".message-meta");
    if (!meta) {
      meta = document.createElement("small");
      meta.className = "message-meta";
      article.querySelector(".message-body")?.append(meta);
    }
    meta.replaceChildren(document.createTextNode(message.source === "mcp" ? "MCP" : "网页"));
    if (message.delivery_status === "failed") {
      const failed = document.createElement("span");
      failed.className = "message-failed";
      failed.textContent = "发送失败 · 未进入后续上下文与记忆";
      meta.append(failed);
    }
  }

  function appendMessage(sender, content, metadata = {}) {
    const article = document.createElement("article");
    article.className = `message message-${sender}`;
    article.dataset.sender = sender;
    let avatar;
    if (sender === "host") {
      avatar = document.createElement("img");
      avatar.src = state.host_avatar;
      avatar.alt = "";
      avatar.className = "message-avatar";
    } else {
      avatar = document.createElement("span");
      avatar.textContent = Array.from(state.visitor_name || "访")[0] || "访";
      avatar.className = "message-avatar visitor-avatar";
      avatar.setAttribute("aria-hidden", "true");
    }
    const body = document.createElement("div");
    body.className = "message-body";
    const label = document.createElement("span");
    label.className = "message-author";
    label.textContent = sender === "visitor" ? state.visitor_name : state.host_name;
    const paragraph = document.createElement("p");
    paragraph.textContent = content;
    body.append(label, paragraph);
    article.append(avatar, body);
    updateMessageMetadata(article, metadata);
    messages.append(article);
    while (messages.querySelectorAll(".message").length > 30) {
      messages.querySelector(".message")?.remove();
    }
    messages.scrollTop = messages.scrollHeight;
    return paragraph;
  }

  function syncMessages(freshMessages) {
    for (const message of freshMessages) {
      let article = Array.from(messages.querySelectorAll(".message")).find(
        (candidate) => candidate.dataset.messageId === message.id
      );
      if (!article) {
        article = Array.from(messages.querySelectorAll(".message")).find(
          (candidate) =>
            !candidate.dataset.messageId &&
            candidate.dataset.sender === message.sender &&
            (message.sender === "host" ||
              candidate.querySelector(".message-body p")?.textContent === message.content)
        );
      }
      if (!article) {
        appendMessage(message.sender, message.content, message);
        continue;
      }
      const paragraph = article.querySelector(".message-body p");
      if (paragraph) paragraph.textContent = message.content;
      updateMessageMetadata(article, message);
    }
    messages.scrollTop = messages.scrollHeight;
  }

  function updateQuota(quota) {
    document.getElementById("quota").textContent = `剩余 ${quota.remaining} / ${quota.limit}`;
    quotaReset.dataset.resetAt = quota.reset_at || "";
    if (!quota.reset_at) {
      quotaReset.textContent = "每小时额度 · 尚未开始";
      return;
    }
    const reset = new Date(quota.reset_at);
    quotaReset.textContent = `额度重置 ${Number.isNaN(reset.valueOf()) ? quota.reset_at : reset.toLocaleString()}`;
  }

  function updateMeta(fresh) {
    state.visitor_status = fresh.visitor_status;
    state.job = fresh.job;
    document.getElementById("visitor-status").textContent = visitorStatusText(fresh.visitor_status);
    document.getElementById("visitor-status").dataset.status = fresh.visitor_status;
    updateQuota(fresh.quota);
  }

  function hydrateState(fresh) {
    updateMeta(fresh);
    if (!messages.querySelector(".conversation-date")) addConversationDate();
    syncMessages(fresh.messages);
    if (
      fresh.job &&
      ["queued", "generating"].includes(fresh.job.status) &&
      fresh.job.visible_text
    ) {
      const existingDraft = Array.from(messages.querySelectorAll(".message-host")).find(
        (candidate) => !candidate.dataset.messageId
      );
      if (existingDraft) return existingDraft.querySelector(".message-body p");
      return appendMessage("host", fresh.job.visible_text);
    }
    return null;
  }

  function stopPolling() {
    if (pollTimer !== null) window.clearInterval(pollTimer);
    pollTimer = null;
  }

  async function refreshQueuedPosition() {
    if (document.hidden || !pending || pollCount >= 80) {
      stopPolling();
      return;
    }
    pollCount += 1;
    try {
      const fresh = await api("/api/state");
      updateMeta(fresh);
      if (!fresh.job || !["queued", "generating"].includes(fresh.job.status)) {
        stopPolling();
        return;
      }
      jobStatus.textContent = statusText(fresh.job);
      if (fresh.job.status !== "queued") stopPolling();
    } catch (_) {
      if (pollCount >= 80) stopPolling();
    }
  }

  function startBoundedPositionPolling() {
    stopPolling();
    pollCount = 0;
    if (document.hidden || !pending) return;
    refreshQueuedPosition();
    pollTimer = window.setInterval(refreshQueuedPosition, 1500);
  }

  async function openStream(jobId, restoreMessages = true) {
    if (stream) stream.close();
    let replyNode = null;
    try {
      const fresh = await api("/api/state");
      if (restoreMessages) {
        replyNode = hydrateState(fresh);
      } else {
        updateMeta(fresh);
        if (fresh.job?.visible_text) {
          replyNode = appendMessage("host", fresh.job.visible_text);
        }
      }
      if (!fresh.job || fresh.job.job_id !== jobId) return;
      if (!["queued", "generating"].includes(fresh.job.status)) {
        setPending(false);
        jobStatus.textContent = statusText(fresh.job);
        return;
      }
    } catch (_) { /* SSE can still recover from the persisted job. */ }
    stream = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events`);
    stream.onerror = () => {
      if (pending) jobStatus.textContent = "连接暂时中断，正在等待恢复…";
    };
    stream.addEventListener("queued", (event) => {
      const payload = JSON.parse(event.data);
      jobStatus.textContent = `排队中 · 第 ${payload.position || "?"} 位`;
      startBoundedPositionPolling();
    });
    stream.addEventListener("started", () => {
      stopPolling();
      jobStatus.textContent = "正在生成…";
    });
    stream.addEventListener("text", (event) => {
      const payload = JSON.parse(event.data);
      if (!replyNode) replyNode = appendMessage("host", "");
      replyNode.textContent = payload.visible_text || replyNode.textContent + payload.text;
    });
    stream.addEventListener("snapshot", (event) => {
      const payload = JSON.parse(event.data);
      if (["queued", "running", "generating"].includes(payload.state) && payload.visible_text) {
        if (!replyNode) replyNode = appendMessage("host", "");
        replyNode.textContent = payload.visible_text;
      }
    });
    const finish = async (event) => {
      const payload = JSON.parse(event.data);
      stopPolling();
      stream.close();
      stream = null;
      setPending(false);
      jobStatus.textContent = payload.state === "completed" ? "已完成" : "本次生成未完成，可稍后重试";
      try {
        const fresh = await api("/api/state");
        updateMeta(fresh);
        if (payload.state !== "completed" && replyNode) {
          replyNode.closest(".message")?.remove();
          replyNode = null;
        }
        syncMessages(fresh.messages);
        if (payload.state === "completed" && fresh.job?.visible_text && replyNode) {
          replyNode.textContent = fresh.job.visible_text;
        }
        setPending(false);
      } catch (_) { /* state will refresh on the next page load */ }
    };
    stream.addEventListener("completed", finish);
    stream.addEventListener("failed", finish);
  }

  textarea.addEventListener("input", updateComposer);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (pending || messageLength() > messageLimit) {
      updateComposer();
      return;
    }
    errorNode.textContent = "";
    const text = textarea.value;
    setPending(true);
    try {
      const ticket = await api("/api/messages", {
        method: "POST",
        body: JSON.stringify({ request_id: crypto.randomUUID(), text }),
      });
      appendMessage("visitor", text, { source: "web", delivery_status: "accepted" });
      textarea.value = "";
      updateComposer();
      jobStatus.textContent = `排队中 · 第 ${ticket.queue_position || "?"} 位`;
      openStream(ticket.job_id, false);
      startBoundedPositionPolling();
    } catch (error) {
      setPending(false);
      if (error.templateText) appendMessage("host", error.templateText);
      else errorNode.textContent = error.message;
    }
  });

  document.getElementById("logout").addEventListener("click", async () => {
    await api("/api/logout", { method: "POST", body: "{}" }).catch(() => undefined);
    location.reload();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopPolling();
    else if (pending) startBoundedPositionPolling();
  });

  setPending(pending);
  updateQuota(state.quota);
  jobStatus.textContent = statusText(state.job);
  if (pending && state.job) {
    openStream(state.job.job_id);
    if (state.job.status === "queued") startBoundedPositionPolling();
  }
})();

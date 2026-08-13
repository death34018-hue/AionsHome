(() => {
  "use strict";

  const page = document.body;

  if (page.dataset.page === "admin-settings") {
    const form = document.getElementById("reception-settings-form");
    const status = document.getElementById("settings-status");
    const payload = () => Object.fromEntries(new FormData(form).entries());
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const body = payload();
      body.lounge_enabled = form.elements.lounge_enabled.checked;
      body.idle_minutes = Number(body.idle_minutes);
      body.hourly_quota_limit = Number(body.hourly_quota_limit);
      const response = await fetch("/admin/api/settings", {
        method: "PUT", headers: {"Content-Type": "application/json"},
        cache: "no-store", body: JSON.stringify(body),
      });
      status.textContent = response.ok ? "设置已保存并立即生效。" : "保存失败，请检查内容。";
    });
    document.getElementById("restore-settings").addEventListener("click", async () => {
      if (!window.confirm("恢复全部默认接待设置？")) return;
      const response = await fetch("/admin/api/settings/restore-defaults", {method: "POST", cache: "no-store"});
      if (response.ok) window.location.reload();
      else status.textContent = "恢复默认失败。";
    });
    return;
  }

  if (page.dataset.page === "admin-dashboard") {
    const panel = document.getElementById("invitation-reveal");
    const keyElement = document.getElementById("invitation-key");
    const status = document.getElementById("invitation-status");
    const copyButton = document.getElementById("copy-invitation-key");
    const kindSelect = document.getElementById("invitation-kind");
    const rows = Array.from(document.querySelectorAll("[data-visitor-row]"));
    const search = document.getElementById("visitor-search");
    const kindFilter = document.getElementById("visitor-kind-filter");
    const selectVisible = document.getElementById("select-visible-visitors");
    const selectionCount = document.getElementById("selection-count");
    const deleteSelected = document.getElementById("delete-selected");
    const deleteDialog = document.getElementById("delete-visitors-dialog");
    const deleteForm = document.getElementById("delete-visitors-form");
    const deleteList = document.getElementById("delete-visitor-list");
    const deleteInput = document.getElementById("delete-confirmation");
    const deleteError = document.getElementById("delete-error");
    const confirmDelete = document.getElementById("confirm-delete");
    let revealVisitorId = null;
    let pendingDeleteIds = [];
    let timer = null;

    async function responsePayload(response) {
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(typeof payload.detail === "string" ? payload.detail : "操作失败");
      }
      return payload;
    }

    function showTemporaryKey(payload, visitorId, message) {
      revealVisitorId = visitorId;
      panel.hidden = false;
      keyElement.textContent = payload.key;
      copyButton.disabled = false;
      status.textContent = message;
      if (timer) window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        keyElement.textContent = `…${payload.key.slice(-4)}`;
      }, payload.hide_after_seconds * 1000);
    }

    document.getElementById("create-invitation").addEventListener("click", async () => {
      const response = await fetch("/admin/api/invitations", {
        method: "POST",
        cache: "no-store",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({visitor_kind: kindSelect.value}),
      });
      if (!response.ok) {
        status.textContent = "邀请创建失败。";
        return;
      }
      const invitation = await response.json();
      const kindLabel = invitation.visitor_kind === "external_ai" ? "外部 AI" : "人类访客";
      showTemporaryKey(invitation, invitation.visitor_id, `${kindLabel} · 首次使用后固定名字。`);
    });

    document.getElementById("copy-invitation-key").addEventListener("click", async () => {
      if (!revealVisitorId) return;
      let disclosedKey = "";
      try {
        const disclosureResponse = await fetch(`/admin/api/visitors/${encodeURIComponent(revealVisitorId)}/key/copy-disclosure`, {
          method: "POST",
          cache: "no-store",
        });
        if (!disclosureResponse.ok) throw new Error("copy disclosure failed");
        const disclosure = await disclosureResponse.json();
        disclosedKey = disclosure.key;
        await navigator.clipboard.writeText(disclosedKey);
        status.textContent = "Key 已为复制而披露并写入剪贴板；系统剪贴板不受页面 30 秒计时器控制。";
      } catch (_error) {
        status.textContent = disclosedKey
          ? "Key 已为复制而披露，但写入剪贴板失败。"
          : "Key 复制披露失败。";
      } finally {
        disclosedKey = "";
      }
    });

    function selectedIds() {
      return rows.filter((row) => row.querySelector(".visitor-select")?.checked).map((row) => row.dataset.visitorId);
    }

    function updateSelection() {
      const count = selectedIds().length;
      if (selectionCount) selectionCount.textContent = String(count);
      if (deleteSelected) deleteSelected.disabled = count === 0;
      if (selectVisible) {
        const visible = rows.filter((row) => !row.hidden);
        selectVisible.checked = visible.length > 0 && visible.every((row) => row.querySelector(".visitor-select")?.checked);
        selectVisible.indeterminate = visible.some((row) => row.querySelector(".visitor-select")?.checked) && !selectVisible.checked;
      }
    }

    function applyFilter() {
      const needle = (search?.value || "").trim().toLocaleLowerCase();
      const kind = kindFilter?.value || "";
      for (const row of rows) {
        row.hidden = Boolean((needle && !row.dataset.search.includes(needle)) || (kind && row.dataset.kind !== kind));
      }
      updateSelection();
    }

    search?.addEventListener("input", applyFilter);
    kindFilter?.addEventListener("change", applyFilter);
    selectVisible?.addEventListener("change", () => {
      for (const row of rows.filter((candidate) => !candidate.hidden)) {
        row.querySelector(".visitor-select").checked = selectVisible.checked;
      }
      updateSelection();
    });
    document.querySelectorAll(".visitor-select").forEach((checkbox) => checkbox.addEventListener("change", updateSelection));

    function openDeleteDialog(ids) {
      pendingDeleteIds = [...new Set(ids)];
      deleteList.replaceChildren();
      for (const id of pendingDeleteIds) {
        const row = rows.find((candidate) => candidate.dataset.visitorId === id);
        if (!row) continue;
        const item = document.createElement("li");
        item.textContent = `${row.dataset.visitorName} · ${row.dataset.kindLabel} · ${row.dataset.messageCount} 条聊天 · ${id}`;
        deleteList.append(item);
      }
      deleteInput.value = "";
      deleteError.textContent = "";
      confirmDelete.disabled = true;
      deleteDialog.showModal();
      deleteInput.focus();
    }

    deleteInput?.addEventListener("input", () => { confirmDelete.disabled = deleteInput.value !== "DELETE"; });
    document.getElementById("cancel-delete")?.addEventListener("click", () => deleteDialog.close());
    deleteSelected?.addEventListener("click", () => openDeleteDialog(selectedIds()));
    deleteForm?.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (deleteInput.value !== "DELETE" || pendingDeleteIds.length === 0) return;
      confirmDelete.disabled = true;
      try {
        await responsePayload(await fetch("/admin/api/visitor-cleanup", {
          method: "POST", cache: "no-store", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({visitor_ids: pendingDeleteIds, confirmation: "DELETE"}),
        }));
        window.location.reload();
      } catch (error) {
        deleteError.textContent = error.message;
        confirmDelete.disabled = false;
      }
    });

    document.addEventListener("click", async (event) => {
      const copyId = event.target.closest("[data-copy-id]");
      if (copyId) {
        await navigator.clipboard.writeText(copyId.dataset.copyId);
        copyId.textContent = "已复制";
        window.setTimeout(() => { copyId.textContent = "复制 ID"; }, 1200);
        return;
      }
      const deleteOne = event.target.closest("[data-delete-one]");
      if (deleteOne) {
        openDeleteDialog([deleteOne.dataset.deleteOne]);
        return;
      }
      const button = event.target.closest("[data-key-command]");
      if (!button) return;
      const visitorId = button.dataset.visitorId;
      const command = button.dataset.keyCommand;
      try {
        if (command === "copy") {
          const disclosure = await responsePayload(await fetch(`/admin/api/visitors/${encodeURIComponent(visitorId)}/key/copy-disclosure`, {method: "POST", cache: "no-store"}));
          await navigator.clipboard.writeText(disclosure.key);
          button.textContent = "已复制";
          window.setTimeout(() => { button.textContent = "复制"; }, 1200);
          return;
        }
        if (command === "rotate" && window.prompt("轮换后旧 Key 立即失效，但身份和记录保留。请输入 ROTATE") !== "ROTATE") return;
        if (command === "revoke" && window.prompt("撤销后无法登录，但身份和记录保留。请输入 REVOKE") !== "REVOKE") return;
        if (command === "create" && !window.confirm("为这个原有访客创建一把新 Key？")) return;
        const path = command === "create" ? "key" : `key/${command}`;
        const response = await fetch(`/admin/api/visitors/${encodeURIComponent(visitorId)}/${path}`, {method: "POST", cache: "no-store"});
        if (command === "revoke") {
          if (!response.ok) await responsePayload(response);
          window.location.reload();
          return;
        }
        const payload = await responsePayload(response);
        showTemporaryKey(payload, visitorId, command === "rotate" ? "Key 已轮换，旧 Key 已失效。" : "已为原访客创建新 Key。");
      } catch (error) {
        status.textContent = error.message;
        panel.hidden = false;
      }
    });
    updateSelection();
    return;
  }

  if (page.dataset.page !== "admin-visitor") return;

  const visitorId = page.dataset.visitorId;
  const base = `/admin/api/visitors/${encodeURIComponent(visitorId)}`;
  const flash = document.getElementById("admin-flash");
  const reveal = document.getElementById("key-reveal");
  const keyValue = document.getElementById("key-value");
  const copyButton = document.getElementById("copy-key");
  let hideTimer = null;
  let currentMask = keyValue.dataset.mask || "";
  let currentRawKey = "";

  function showMessage(message, error = false) {
    flash.textContent = message;
    flash.style.color = error ? "#ffaa9d" : "";
  }

  function hideKey() {
    if (hideTimer) window.clearTimeout(hideTimer);
    hideTimer = null;
    currentRawKey = "";
    keyValue.textContent = currentMask;
    reveal.dataset.visible = currentMask ? "true" : "false";
  }

  function showKey(key, seconds) {
    if (hideTimer) window.clearTimeout(hideTimer);
    currentMask = `…${key.slice(-4)}`;
    currentRawKey = key;
    copyButton.disabled = false;
    keyValue.textContent = key;
    reveal.dataset.visible = "true";
    hideTimer = window.setTimeout(hideKey, seconds * 1000);
  }

  async function request(path, options = {}) {
    const response = await fetch(path ? `${base}/${path}` : base, {
      cache: "no-store",
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(typeof payload.detail === "string" ? payload.detail : "操作失败");
    }
    return response.status === 204 ? null : response.json();
  }

  document.querySelectorAll("[data-key-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      const confirmation = button.dataset.confirm;
      if (confirmation && window.prompt(`请输入 ${confirmation} 确认`) !== confirmation) return;
      try {
        const payload = await request(button.dataset.keyAction, { method: "POST" });
        showKey(payload.key, payload.hide_after_seconds);
        showMessage("Key 仅在当前页面内存中临时显示。");
      } catch (error) {
        showMessage(error.message, true);
      }
    });
  });

  document.querySelectorAll("[data-admin-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      const confirmation = button.dataset.confirm;
      if (confirmation && window.prompt(`请输入 ${confirmation} 确认`) !== confirmation) return;
      try {
        await request(button.dataset.adminAction, { method: "POST" });
        window.location.reload();
      } catch (error) {
        showMessage(error.message, true);
      }
    });
  });

  document.getElementById("identity-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = document.getElementById("identity-name").value;
    const visitorKind = document.getElementById("identity-kind").value;
    try {
      await request("identity", {
        method: "PUT",
        body: JSON.stringify({name, visitor_kind: visitorKind}),
      });
      window.location.reload();
    } catch (error) {
      showMessage(error.message, true);
    }
  });

  copyButton.addEventListener("click", async () => {
    let disclosedKey = "";
    try {
      const disclosure = await request("key/copy-disclosure", { method: "POST" });
      disclosedKey = disclosure.key;
      await navigator.clipboard.writeText(disclosedKey);
      showMessage("Key 已为复制而披露并写入剪贴板；页面会按时隐藏 Key，但系统剪贴板不受页面计时器控制。");
    } catch (_error) {
      showMessage(
        disclosedKey
          ? "Key 已为复制而披露，但写入剪贴板失败。"
          : "Key 复制披露失败。",
        true,
      );
    } finally {
      disclosedKey = "";
    }
  });

  document.getElementById("note-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const note = document.getElementById("note-text").value;
    try {
      await request("notes", { method: "POST", body: JSON.stringify({ note }) });
      window.location.reload();
    } catch (error) {
      showMessage(error.message, true);
    }
  });

  document.getElementById("export-visitor").addEventListener("click", async () => {
    if (window.prompt("请输入 EXPORT 确认导出") !== "EXPORT") return;
    try {
      const payload = await request("export", {
        method: "POST",
        body: JSON.stringify({ confirmation: "EXPORT" }),
      });
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `visitor-${visitorId}.json`;
      link.click();
      URL.revokeObjectURL(url);
      showMessage("导出已生成并记录审计。");
    } catch (error) {
      showMessage(error.message, true);
    }
  });

  document.getElementById("delete-visitor").addEventListener("click", async () => {
    if (window.prompt("消息、摘要、Key、Session、额度、任务等关联数据将永久删除且不可恢复。请输入 DELETE") !== "DELETE") return;
    try {
      await request("", {
        method: "DELETE",
        body: JSON.stringify({ confirmation: "DELETE" }),
      });
      window.location.assign("/admin");
    } catch (error) {
      showMessage(error.message, true);
    }
  });
})();

(() => {
  const $ = (s, root = document) => root.querySelector(s);
  const name = document.body.dataset.visitorName || "访客";
  let activeId = null;
  async function api(path, options = {}) {
    const response = await fetch(path, {credentials: "same-origin", headers: {"Content-Type": "application/json"}, ...options});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "暂时无法连接留言板");
    return data;
  }
  const json = value => JSON.stringify(value);
  function node(tag, className, text) { const el = document.createElement(tag); el.className = className; el.textContent = text; return el; }
  function showError(form, error) { $(".form-error", form).textContent = error.message; }
  async function load() {
    const threads = await api("/api/board/threads");
    const list = $("#thread-list"); list.replaceChildren();
    $("#thread-count").textContent = `${threads.length} 张纸条`;
    if (!threads.length) { list.append(node("p", "empty", "这里还空着。第一张纸条，等你来写。")); return; }
    for (const item of threads) {
      const card = node("button", "thread-card", ""); card.type = "button";
      card.append(node("small", "", item.status === "open" ? "● 正在聊" : "○ 已结束"), node("h3", "", item.title), node("p", "", new Date(item.updated_at).toLocaleString("zh-CN")));
      card.addEventListener("click", () => openThread(item.id)); list.append(card);
    }
  }
  async function openThread(id) {
    const thread = await api(`/api/board/threads/${encodeURIComponent(id)}`); activeId = id;
    $("#detail-title").textContent = thread.title;
    $("#detail-status").textContent = thread.status === "open" ? "这张纸条还在聊" : `话题已由 ${thread.closed_by || "朋友"} 结束`;
    const posts = $("#post-list"); posts.replaceChildren();
    for (const post of thread.posts) {
      const box = node("article", "post", ""); const head = node("header", "", "");
      head.append(node("strong", "", post.author_name), node("span", "", post.addressed_to ? `写给 ${post.addressed_to}` : "写给大家"), node("time", "", new Date(post.created_at).toLocaleString("zh-CN")));
      box.append(head, node("p", "", post.content)); posts.append(box);
    }
    $("#reply-form").hidden = thread.status !== "open"; $("#close-thread").hidden = thread.status !== "open";
    if (!$("#thread-dialog").open) $("#thread-dialog").showModal();
  }
  $("#new-thread").addEventListener("click", () => $("#composer").showModal());
  document.querySelectorAll("[data-close]").forEach(button => button.addEventListener("click", () => button.closest("dialog").close()));
  $("#compose-form").addEventListener("submit", async event => {
    event.preventDefault(); const form = event.currentTarget; $(".form-error", form).textContent = "";
    const fields = Object.fromEntries(new FormData(form));
    try { const request_id = form.dataset.requestId || (form.dataset.requestId = crypto.randomUUID()); const thread = await api("/api/board/threads", {method:"POST", body:json({...fields, request_id})}); delete form.dataset.requestId; $("#composer").close(); form.reset(); form.elements.author_name.value = name; await load(); await openThread(thread.id); }
    catch(error) { showError(form,error); }
  });
  $("#reply-form").addEventListener("submit", async event => {
    event.preventDefault(); const form = event.currentTarget; $(".form-error", form).textContent = "";
    try { const request_id = form.dataset.requestId || (form.dataset.requestId = crypto.randomUUID()); await api(`/api/board/threads/${encodeURIComponent(activeId)}/posts`, {method:"POST", body:json({...Object.fromEntries(new FormData(form)), request_id})}); delete form.dataset.requestId; form.elements.content.value = ""; await openThread(activeId); await load(); }
    catch(error) { showError(form,error); }
  });
  $("#close-thread").addEventListener("click", async () => { if (!confirm("结束后，这张纸条就不能继续回复啦。")) return; await api(`/api/board/threads/${encodeURIComponent(activeId)}/close`, {method:"POST", body:json({author_name:$("#reply-form").elements.author_name.value || name})}); await openThread(activeId); await load(); });
  $("#logout").addEventListener("click", async () => { await api("/api/logout", {method:"POST"}); location.reload(); });
  for (const form of [$("#compose-form"), $("#reply-form")]) form.addEventListener("input", () => delete form.dataset.requestId);
  load().catch(error => { $("#thread-list").append(node("p", "empty", error.message)); });
})();

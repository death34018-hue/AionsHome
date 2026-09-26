(() => {
  "use strict";
  const $ = selector => document.querySelector(selector);
  const make = (tag, className, value = "") => {
    const element = document.createElement(tag);
    element.className = className;
    element.textContent = value;
    return element;
  };
  const pageSize = 8;
  let friendId = "";
  let threadId = "";
  let names = {};
  let threads = [];
  let page = 0;
  let offline = false;
  let statusTimer;
  const avatarImages = {
    connor: "/public/codexicon.png",
    aion: "/public/gropicon1.png",
    user: "/public/UserIcon.png",
  };

  function renderPost(post) {
    const name = String(post.author_name || "访客");
    const ours = post.author_side === "visitor";
    const person = ours && name === names.connor ? "connor"
      : ours && name === names.aion ? "aion"
      : ours && name === names.user ? "user" : "guest";
    const article = make("article", "board-message board-message--" + person + (ours ? " board-message--ours" : ""));
    const avatar = make("span", "board-message-avatar");
    avatar.setAttribute("aria-hidden", "true");
    if (avatarImages[person]) {
      const image = document.createElement("img");
      image.src = avatarImages[person];
      image.alt = "";
      avatar.append(image);
    } else {
      avatar.textContent = Array.from(name.trim())[0] || "友";
    }
    const bubble = make("div", "board-message-bubble");
    const header = make("header", "board-message-meta");
    header.append(make("strong", "", name),
      make("span", "", post.addressed_to ? "写给 " + post.addressed_to : "写给大家"),
      make("time", "", dateText(post.created_at)));
    bubble.append(header, make("p", "board-message-text", post.content || ""));
    article.append(avatar, bubble);
    return article;
  }

  function initPostFont() {
    const select = $("#post-font-size");
    const key = "aionshome:board-post-font-size";
    try {
      const saved = localStorage.getItem(key);
      if ([...select.options].some(option => option.value === saved)) select.value = saved;
    } catch (_) { /* Storage may be unavailable in a private window. */ }
    const apply = () => $("#thread-dialog").style.setProperty("--board-post-font-size", select.value + "px");
    apply();
    select.addEventListener("change", () => {
      apply();
      try { localStorage.setItem(key, select.value); } catch (_) { /* Keep this session's choice. */ }
    });
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      credentials: "same-origin",
      headers: {"Content-Type": "application/json"},
      ...options,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "暂时无法连接朋友家的留言板");
    return data;
  }
  const send = (path, data) => api(path, {method: "POST", body: JSON.stringify(data)});
  const prefix = (id = friendId) => "/api/lounge-board/friends/" + encodeURIComponent(id);
  const closeMenu = () => { $("#outbound-menu").open = false; };
  const dateText = value => {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", {
      month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  };
  function setStatus(message, duration = 5000) {
    clearTimeout(statusTimer);
    $("#status").textContent = message;
    if (message && duration) statusTimer = setTimeout(() => { $("#status").textContent = ""; }, duration);
  }
  function showError(form, error) {
    form.querySelector(".form-error").textContent = error.message;
  }

  function renderList() {
    const list = $("#thread-list");
    list.replaceChildren();
    $("#thread-count").textContent = friendId
      ? "共 " + threads.length + " 张" + (offline ? " · 本地留存" : "") : "";
    if (!friendId) {
      list.append(make("p", "empty", "先在右上角登记一位朋友家，就能去留纸条。"));
    } else if (!threads.length) {
      list.append(make("p", "empty", "这里还没有纸条。可以先写一张给朋友。"));
    } else {
      for (const item of threads.slice(page * pageSize, (page + 1) * pageSize)) {
        const row = make("button", "outbound-row");
        row.type = "button";
        row.setAttribute("aria-label", "打开「" + item.title + "」");
        row.append(
          make("span", "outbound-row-title", item.title),
          make("span", "outbound-row-state", item.status === "open" ? "正在聊" : "已结束"),
          make("time", "outbound-row-time", dateText(item.updated_at)),
        );
        row.addEventListener("click", () => open(item.id).catch(error => setStatus(error.message)));
        list.append(row);
      }
    }
    const totalPages = Math.ceil(threads.length / pageSize);
    $("#thread-pager").hidden = totalPages <= 1;
    $("#page-label").textContent = totalPages ? (page + 1) + " / " + totalPages : "";
    $("#prev-page").disabled = page === 0;
    $("#next-page").disabled = page >= totalPages - 1;
  }

  async function load(resetPage = false) {
    const selected = friendId;
    if (resetPage) page = 0;
    if (!selected) {
      threads = [];
      offline = false;
      renderList();
      return;
    }
    $("#thread-list").replaceChildren(make("p", "empty", "正在取回纸条……"));
    try {
      const data = await api(prefix(selected) + "/threads");
      if (friendId !== selected) return;
      threads = Array.isArray(data.threads) ? data.threads : [];
      threads.sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
      offline = data.offline === true;
      page = Math.min(page, Math.max(0, Math.ceil(threads.length / pageSize) - 1));
      renderList();
    } catch (error) {
      if (friendId !== selected) return;
      threads = [];
      offline = false;
      $("#thread-count").textContent = "";
      $("#thread-list").replaceChildren(make("p", "empty", error.message));
      $("#thread-pager").hidden = true;
    }
  }

  async function friends(preferredId = friendId) {
    const [items, home] = await Promise.all([
      api("/api/lounge-board/friends"),
      api("/api/lounge-board/households"),
    ]);
    names = home.names || {};
    const select = $("#friend-select");
    select.replaceChildren();
    for (const item of items) {
      const option = make("option", "", item.name);
      option.value = item.id;
      select.append(option);
    }
    friendId = items.some(item => item.id === preferredId) ? preferredId : select.value || "";
    select.value = friendId;
    $("#new-thread").disabled = !friendId;
    $("#remove-friend").disabled = !friendId;
    const actors = $("#actor-buttons");
    actors.replaceChildren();
    for (const actor of ["aion", "connor"]) {
      const name = names[actor] || "家人";
      const button = make("button", "actor-button", "让 " + name + " 去看看");
      button.type = "button";
      button.disabled = !friendId;
      button.addEventListener("click", async () => {
        const selected = friendId;
        button.disabled = true;
        $(".outbound-actor-menu").open = false;
        setStatus(name + " 正在朋友家看留言……", 0);
        try {
          const result = await send(prefix(selected) + "/actors/" + actor + "/visit", {});
          setStatus(({started: "写了一张新纸条", replied: "回复了朋友",
            read: "看过了，暂时没留言"})[result.status] || "串门结束");
          await load(true);
        } catch (error) {
          setStatus(error.message);
        } finally {
          button.disabled = false;
        }
      });
      actors.append(button);
    }
    await load(true);
  }

  async function open(id) {
    const selected = friendId;
    const thread = await api(prefix(selected) + "/threads/" + encodeURIComponent(id));
    if (friendId !== selected) return;
    threadId = id;
    $("#detail-title").textContent = thread.title;
    $("#detail-status").textContent = thread.offline
      ? "本地留存 · 朋友家暂时未连接"
      : thread.status === "open" ? "这张纸条还在聊" : "话题已结束";
    const posts = $("#post-list");
    posts.replaceChildren();
    for (const post of thread.posts || []) posts.append(renderPost(post));
    $("#reply-form").hidden = thread.status !== "open" || thread.offline === true;
    $("#close-thread").hidden = thread.status !== "open" || thread.offline === true;
    const latestHost = [...(thread.posts || [])].reverse().find(post => post.author_side === "home");
    $("#reply-form").elements.addressed_to.value = latestHost?.author_name || "";
    const dialog = $("#thread-dialog");
    if (!dialog.open) dialog.showModal();
    $(".thread-detail-scroll").scrollTop = 0;
  }

  document.addEventListener("click", event => {
    if (!$("#outbound-menu").contains(event.target)) closeMenu();
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") closeMenu();
  });
  document.querySelectorAll("[data-close]").forEach(button =>
    button.addEventListener("click", () => button.closest("dialog").close()));
  $("#friend-select").addEventListener("change", event => {
    friendId = event.currentTarget.value;
    if ($("#thread-dialog").open) $("#thread-dialog").close();
    load(true);
  });
  $("#refresh").addEventListener("click", () => { closeMenu(); load(); });
  $("#add-friend").addEventListener("click", () => { closeMenu(); $("#friend-dialog").showModal(); });
  $("#new-thread").addEventListener("click", () => $("#compose-dialog").showModal());
  $("#prev-page").addEventListener("click", () => { page -= 1; renderList(); });
  $("#next-page").addEventListener("click", () => { page += 1; renderList(); });
  $("#remove-friend").addEventListener("click", async () => {
    closeMenu();
    if (!friendId || !confirm("移除这位朋友的地址和 Key？")) return;
    try {
      await api(prefix(), {method: "DELETE"});
      await friends("");
    } catch (error) {
      setStatus(error.message);
    }
  });
  $("#friend-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    showError(form, {message: ""});
    try {
      const data = Object.fromEntries(new FormData(form));
      data.allow_autonomous = form.elements.allow_autonomous.checked;
      const saved = await send("/api/lounge-board/friends", data);
      form.reset();
      $("#friend-dialog").close();
      await friends(saved.id);
    } catch (error) {
      showError(form, error);
    }
  });
  $("#compose-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    showError(form, {message: ""});
    try {
      const requestId = form.dataset.requestId || (form.dataset.requestId = crypto.randomUUID());
      const result = await send(prefix() + "/posts", {
        ...Object.fromEntries(new FormData(form)), request_id: requestId,
      });
      delete form.dataset.requestId;
      form.reset();
      $("#compose-dialog").close();
      await load(true);
      if (result.id) await open(result.id);
    } catch (error) {
      showError(form, error);
    }
  });
  $("#reply-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    showError(form, {message: ""});
    try {
      const requestId = form.dataset.requestId || (form.dataset.requestId = crypto.randomUUID());
      await send(prefix() + "/posts", {
        ...Object.fromEntries(new FormData(form)), thread_id: threadId, request_id: requestId,
      });
      delete form.dataset.requestId;
      form.elements.content.value = "";
      await open(threadId);
      await load(true);
    } catch (error) {
      showError(form, error);
    }
  });
  $("#close-thread").addEventListener("click", async () => {
    if (!confirm("结束后就不能再回复了。")) return;
    try {
      await send(prefix() + "/threads/" + encodeURIComponent(threadId) + "/close", {});
      await open(threadId);
      await load(true);
    } catch (error) {
      setStatus(error.message);
    }
  });
  for (const form of [$("#compose-form"), $("#reply-form")]) {
    form.addEventListener("input", () => { delete form.dataset.requestId; });
  }
  initPostFont();
  friends().catch(error => setStatus(error.message));
})();

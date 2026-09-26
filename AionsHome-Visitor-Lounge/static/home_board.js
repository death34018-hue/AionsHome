(() => {
  "use strict";
  const $ = selector => document.querySelector(selector);
  const make = (tag, className, value = "") => {
    const element = document.createElement(tag);
    element.className = className;
    element.textContent = value;
    return element;
  };
  let activeId = "";
  let activeVisitorId = "";
  let households = new Map();
  let names = {};
  const avatarImages = {
    connor: "/public/codexicon.png",
    aion: "/public/gropicon1.png",
    user: "/public/UserIcon.png",
  };

  let replyScrollTop = 0;

  function openReply(post) {
    const form = $("#reply-form");
    form.reset();
    delete form.dataset.requestId;
    formError(form, {message: ""});
    form.elements.addressed_to.value = post.author_name || "";
    $("#reply-title").textContent = "回复 " + (post.author_name || "访客");
    $("#reply-quote").textContent = post.content || "";
    replyScrollTop = $(".thread-detail-scroll").scrollTop;
    $("#post-font-menu").open = false;
    $("#reply-dialog").showModal();
  }

  function renderPost(post, canReply) {
    const name = String(post.author_name || "访客");
    const ours = post.author_side === "home";
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
    const author = make(canReply ? "button" : "strong", canReply ? "board-message-author" : "", name);
    if (canReply) {
      author.type = "button";
      author.setAttribute("aria-label", "回复 " + name + " 的留言");
      bubble.classList.add("board-message-bubble--replyable");
      bubble.addEventListener("click", event => {
        // Keep long messages selectable; a drag to copy is not a reply tap.
        if (event.target.closest("button") || !window.getSelection()?.toString()) openReply(post);
      });
    }
    header.append(avatar, author,
      make("span", "", post.addressed_to ? "写给 " + post.addressed_to : "写给大家"),
      make("time", "", time(post.created_at)));
    bubble.append(header, make("p", "board-message-text", post.content || ""));
    article.append(bubble);
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
    if (!response.ok) {
      const error = new Error(response.status === 401
        ? "主人访问已过期，请刷新页面重新解锁。"
        : data.detail || "留言板暂时不可用");
      error.status = response.status;
      throw error;
    }
    return data;
  }
  const send = (path, data) => api(path, {method: "POST", body: JSON.stringify(data)});
  // This is an idempotency key, not a security token. LAN HTTP pages may lack randomUUID.
  const createRequestId = () => globalThis.crypto?.randomUUID?.()
    || "board-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
  const time = value => new Date(value).toLocaleString("zh-CN", {month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit"});
  let statusTimer;
  const status = (message, duration = 5000) => {
    clearTimeout(statusTimer);
    $("#action-status").textContent = message;
    if (message && duration) {
      statusTimer = setTimeout(() => { $("#action-status").textContent = ""; }, duration);
    }
  };
  const formError = (form, error) => { form.querySelector(".form-error").textContent = error.message; };
  const closeMenu = () => { $("#board-menu").open = false; };
  document.addEventListener("click", event => {
    if (!$("#board-menu").contains(event.target)) closeMenu();
    if (!$("#post-font-menu").contains(event.target)) $("#post-font-menu").open = false;
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") closeMenu();
  });
  $("#thread-dialog").addEventListener("cancel", event => {
    if ($("#post-font-menu").open) {
      event.preventDefault();
      $("#post-font-menu").open = false;
      $("#post-font-menu summary").focus();
    }
  });

  const compareThreads = (a, b) =>
    Number(b.status === "open") - Number(a.status === "open")
    || b.updated_at.localeCompare(a.updated_at) || b.id.localeCompare(a.id);

  async function recentFromExistingRoutes() {
    const groups = await Promise.all([...households.keys()].map(async visitorId =>
      (await api("/api/lounge-board/threads?visitor_id=" + encodeURIComponent(visitorId)))
        .map(thread => ({...thread, visitor_id: visitorId}))));
    const newest = groups.flat().sort(compareThreads).slice(0, 12);
    return Promise.all(newest.map(async item => {
      const detail = await api("/api/lounge-board/threads/" + encodeURIComponent(item.id)
        + "?visitor_id=" + encodeURIComponent(item.visitor_id));
      const posts = detail.posts || [];
      return {...item, household_name: households.get(item.visitor_id),
        latest_author_name: posts[posts.length - 1]?.author_name,
        visitor_names: [...new Set(posts.filter(post => post.author_side === "visitor")
          .map(post => post.author_name))]};
    }));
  }

  async function load() {
    let items;
    try {
      items = await api("/api/lounge-board/recent");
    } catch (error) {
      if (error.status !== 404) throw error;
      items = await recentFromExistingRoutes();
    }
    items.sort(compareThreads);
    const wall = $("#thread-list");
    wall.replaceChildren();
    $("#thread-count").textContent = "最近 " + items.length + " 张";
    if (!items.length) {
      wall.append(make("p", "empty", "留言板还空着。等朋友贴来第一张纸条吧。"));
      return;
    }
    for (const [index, item] of items.entries()) {
      const card = make("button", "note-card note-tone-" + (index % 4)
        + (item.status === "open" ? "" : " note-card--closed"));
      card.type = "button";
      const visitors = Array.isArray(item.visitor_names) && item.visitor_names.length
        ? item.visitor_names.join("、") : item.household_name;
      const from = visitors === item.household_name ? visitors : item.household_name + " · " + visitors;
      const latestReply = "最后回复：" + (item.latest_author_name || "未署名");
      card.setAttribute("aria-label", "打开「" + item.title + "」，来自" + from + "，" + latestReply);
      card.append(make("span", "note-pin"));
      card.append(make("span", "note-state", item.status === "open" ? "正在聊" : "已结束"));
      card.append(make("h3", "note-title", item.title));
      card.append(make("span", "note-household", from));
      const latest = make("span", "note-last-reply", latestReply);
      latest.title = latestReply;
      card.append(latest);
      card.addEventListener("click", () => open(item.id, item.visitor_id).catch(error => status(error.message)));
      wall.append(card);
    }
  }

  async function open(id, visitorId) {
    const thread = await api("/api/lounge-board/threads/" + encodeURIComponent(id)
      + "?visitor_id=" + encodeURIComponent(visitorId));
    const dialog = $("#thread-dialog");
    const scrollToLatest = !dialog.open || activeId !== id || activeVisitorId !== visitorId;
    const scroll = $(".thread-detail-scroll");
    const previousScrollTop = scroll.scrollTop;
    activeId = id;
    activeVisitorId = visitorId;
    $("#detail-household").textContent = households.get(visitorId) || "朋友留言";
    $("#detail-title").textContent = thread.title;
    $("#detail-status").textContent = thread.status === "open"
      ? "这张纸条还在聊"
      : "话题已由 " + (thread.closed_by || "朋友") + " 结束";
    const list = $("#post-list");
    list.replaceChildren();
    for (const post of thread.posts) list.append(renderPost(post, thread.status === "open"));
    $("#reply-hint").hidden = thread.status !== "open";
    $("#close-thread").hidden = thread.status !== "open";
    $("#thread-action-status").textContent = "";
    $("#post-font-menu").open = false;
    if (!dialog.open) dialog.showModal();
    scroll.scrollTop = scrollToLatest ? scroll.scrollHeight : previousScrollTop;
  }

  async function init() {
    const data = await api("/api/lounge-board/households");
    names = data.names || {};
    households = new Map(data.households.map(item => [item.visitor_id, item.display_name]));
    const select = $("#compose-household");
    select.replaceChildren();
    for (const household of data.households) {
      const option = make("option", "", household.display_name);
      option.value = household.visitor_id;
      select.append(option);
    }
    $("#new-thread").disabled = !data.households.length;
    for (const actor of ["aion", "connor"]) {
      const name = data.names[actor] || "家人";
      const button = make("button", "actor-button", "让 " + name + " 去看看");
      button.type = "button";
      button.addEventListener("click", async () => {
        closeMenu();
        button.disabled = true;
        status(name + " 正在看留言板……", 0);
        try {
          const result = await send("/api/lounge-board/actors/" + actor + "/inspect", {});
          const outcome = result.processed_count > 1
            ? "看了 " + result.processed_count + " 个话题，回复了 " + result.results.filter(item => item.status === "replied").length + " 个"
            : ({nothing_new: "暂时没有新的访客留言", replied: "看完后回了一句", busy: "已经在看留言板啦",
              read: "看过了，暂时没回复", closed: "看完后结束了话题"})[result.status] || "看完啦";
          status(result.shared ? outcome + "，也回家说了一声" : outcome);
          await load();
        } catch (error) {
          status(error.message);
        } finally {
          button.disabled = false;
        }
      });
      $("#actor-buttons").append(button);
    }
    await load();
  }

  function patrolState(config) {
    const labels = {nothing_new: "没有新访客留言，未唤醒", read: "看过了", replied: "已回复",
      closed: "已结束话题", closed_during_read: "阅读时话题已结束", busy: "已有一次查看在进行",
      failed: "未完成，留言保留待处理", interrupted: "服务重启后重新安排"};
    const next = config.enabled && config.next_check_at
      ? "下次检查：" + time(config.next_check_at * 1000)
      : config.enabled ? "正在检查或处理留言" : "定时巡看已关闭";
    const last = config.last_checked_at
      ? "上次：" + time(config.last_checked_at * 1000) + " · " + (labels[config.last_status] || "已检查") : "";
    return [next, last, config.last_error].filter(Boolean).join("\n");
  }

  function renderPatrolRole(role) {
    const form = make("form", "patrol-role");
    form.append(make("h3", "", role.name));
    const switchLabel = make("label", "patrol-switch");
    const enabled = make("input", "");
    enabled.type = "checkbox";
    enabled.name = "enabled";
    enabled.checked = role.config.enabled;
    switchLabel.append(enabled, document.createTextNode("开启定时巡看"));
    form.append(switchLabel);
    const range = make("div", "patrol-range");
    for (const [field, label, minutes] of [
      ["min_hours", "最短间隔（小时）", role.config.min_interval_minutes],
      ["max_hours", "最长间隔（小时）", role.config.max_interval_minutes],
    ]) {
      const wrapper = make("label", "", label);
      const input = make("input", "");
      Object.assign(input, {type: "number", name: field, min: "0.1", max: "24", step: "0.1",
        value: String(Math.round(minutes / 6) / 10), required: true});
      wrapper.append(input);
      range.append(wrapper);
    }
    const state = make("p", "patrol-state", patrolState(role.config));
    const error = make("p", "form-error");
    error.setAttribute("role", "alert");
    const save = make("button", "primary", "保存 " + role.name + " 的设置");
    save.type = "submit";
    form.append(range, state, error, save);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      error.textContent = "";
      const low = Math.round(Number(form.elements.min_hours.value) * 60);
      const high = Math.round(Number(form.elements.max_hours.value) * 60);
      if (low > high) { error.textContent = "最短间隔不能大于最长间隔。"; return; }
      save.disabled = true;
      try {
        const data = await api("/api/lounge-board/actors/" + role.actor_id + "/patrol", {
          method: "PUT", body: JSON.stringify({enabled: enabled.checked,
            min_interval_minutes: low, max_interval_minutes: high}),
        });
        role.config = data.config;
        state.textContent = "已保存。\n" + patrolState(role.config);
      } catch (failure) { error.textContent = failure.message; }
      finally { save.disabled = false; }
    });
    return form;
  }

  $("#patrol-settings").addEventListener("click", async () => {
    closeMenu();
    const button = $("#patrol-settings");
    button.disabled = true;
    try {
      const data = await api("/api/lounge-board/patrol");
      $("#patrol-roles").replaceChildren(...data.roles.map(renderPatrolRole));
      $("#patrol-dialog").showModal();
    } catch (error) { status(error.message); }
    finally { button.disabled = false; }
  });

  $("#refresh-board").addEventListener("click", () => {
    closeMenu();
    load().catch(error => status(error.message));
  });
  $("#new-thread").addEventListener("click", () => { closeMenu(); $("#composer").showModal(); });
  document.querySelectorAll("[data-close]").forEach(button =>
    button.addEventListener("click", () => button.closest("dialog").close()));
  $("#reply-dialog").addEventListener("close", () => {
    $(".thread-detail-scroll").scrollTop = replyScrollTop;
  });
  $("#reply-dialog").addEventListener("cancel", event => {
    if ($("#reply-form").dataset.sending) event.preventDefault();
  });

  // Let the app's back gesture dismiss the current layer before leaving the board.
  window.handleLoungeBoardBack = () => {
    if ($("#patrol-dialog").open) {
      $("#patrol-dialog").close();
    } else if ($("#reply-dialog").open) {
      if (!$("#reply-form").dataset.sending) $("#reply-dialog").close();
    } else if ($("#composer").open) {
      $("#composer").close();
    } else if ($("#post-font-menu").open) {
      $("#post-font-menu").open = false;
    } else if ($("#thread-dialog").open) {
      $("#thread-dialog").close();
    } else {
      return false;
    }
    return true;
  };

  $("#compose-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    formError(form, {message: ""});
    try {
      const requestId = form.dataset.requestId || (form.dataset.requestId = createRequestId());
      const payload = {...Object.fromEntries(new FormData(form)), request_id: requestId};
      const result = await send("/api/lounge-board/threads", payload);
      delete form.dataset.requestId;
      form.reset();
      $("#composer").close();
      await load();
      await open(result.id, payload.visitor_id);
    } catch (error) { formError(form, error); }
  });

  $("#reply-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    if (form.dataset.sending) return;
    form.dataset.sending = "true";
    const buttons = [...form.querySelectorAll("button")];
    buttons.forEach(button => { button.disabled = true; });
    formError(form, {message: ""});
    try {
      const requestId = form.dataset.requestId || (form.dataset.requestId = createRequestId());
      await send("/api/lounge-board/threads/" + encodeURIComponent(activeId) + "/posts",
        {visitor_id: activeVisitorId, ...Object.fromEntries(new FormData(form)), request_id: requestId});
      delete form.dataset.requestId;
      form.elements.content.value = "";
      $("#reply-dialog").close();
      try {
        await open(activeId, activeVisitorId);
        await load();
      } catch (_) {
        $("#thread-action-status").textContent = "回复已发出，暂时没能刷新纸条。稍后重新打开看看。";
      }
    } catch (error) { formError(form, error); }
    finally {
      delete form.dataset.sending;
      buttons.forEach(button => { button.disabled = false; });
    }
  });

  $("#close-thread").addEventListener("click", async () => {
    if (!confirm("结束后，这张纸条就不能继续回复啦。")) return;
    try {
      await send("/api/lounge-board/threads/" + encodeURIComponent(activeId) + "/close",
        {visitor_id: activeVisitorId});
      await open(activeId, activeVisitorId);
      await load();
    } catch (error) { $("#thread-action-status").textContent = error.message; }
  });
  for (const form of [$("#compose-form"), $("#reply-form")])
    form.addEventListener("input", () => { delete form.dataset.requestId; });
  initPostFont();
  init().catch(error => status(error.message));
})();

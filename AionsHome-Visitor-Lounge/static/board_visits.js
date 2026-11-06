(() => {
  "use strict";
  const $ = selector => document.querySelector(selector);
  const make = (tag, className = "", value = "") => {
    const node = document.createElement(tag);
    node.className = className;
    node.textContent = value;
    return node;
  };
  const labels = {read: "只看了看", replied: "留下了回复", closed: "结束了话题", started: "写了新纸条",
    nothing_new: "没有新留言", running: "还在看纸条", reading: "正在阅读", failed: "这次没能完成",
    interrupted: "这次查看中断了", waiting_user: "先回来陪你聊天", incomplete: "读到纸条，尚未完成", unconfirmed: "发送结果未确认",
    closed_during_read: "回复前话题已结束", already_closed: "朋友已先结束话题", archive: "留存便签", sending: "正在发送"};
  const triggers = {scheduled: "定时来看看", manual: "你请家人去看看", autonomous: "自己去转了转", legacy: "以前留下的经历"};
  const date = value => {
    if (!value) return "";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? "" : parsed.toLocaleString("zh-CN", {
      month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit"});
  };
  let names = {}, offset = 0, generation = 0, detailGeneration = 0;
  async function api(path) {
    const response = await fetch(path, {credentials: "same-origin", cache: "no-store"});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "这页手记暂时没能取来，稍后再翻翻。");
    return data;
  }
  const locationText = item => item.source === "outbound" ? "到 " + (item.friend_name || "朋友家") + " 留言板"
    : "在家里的留言板";
  function actionText(topics) {
    const counts = new Map();
    for (const topic of topics) counts.set(topic.status, (counts.get(topic.status) || 0) + 1);
    return [...counts].map(([key, count]) => (labels[key] || "看了纸条") + (count > 1 ? " · " + count + " 张" : "")).join("，");
  }
  function cover(item) {
    const button = make("button", "journal-card");
    button.type = "button";
    const name = names[item.actor_id] || "家人";
    const meta = make("div", "journal-card-meta");
    const stamp = make("time", "", date(item.started_at));
    stamp.dateTime = item.started_at;
    meta.append(make("span", "journal-chip journal-chip--" + item.actor_id, name),
      make("span", "", locationText(item)), stamp);
    const titles = item.topics.map(topic => "《" + (topic.title || "朋友的纸条") + "》");
    const title = titles.length ? titles.slice(0, 2).join("、") + (titles.length > 2 ? " 等 " + titles.length + " 张" : "")
      : item.status === "nothing_new" ? "墙上暂时没有新纸条" : "到朋友家转了转";
    button.append(meta, make("h3", "", title));
    const reason = item.topics.find(topic => topic.reason)?.reason;
    if (reason) button.append(make("p", "journal-card-reason", reason));
    if (item.topics.length) button.append(make("p", "", actionText(item.topics)));
    if (["failed", "interrupted", "running", "waiting_user"].includes(item.status)) button.append(make("p", "", labels[item.status]));
    const foot = make("div", "journal-card-foot");
    foot.append(make("span", "", item.legacy ? "旧经历 · 可翻看留存便签" : triggers[item.trigger] || "一次小拜访"),
      make("span", "", "翻开这一页 ↗"));
    button.append(foot);
    button.addEventListener("click", () => openVisit(item.id));
    return button;
  }
  function renderPost(post, source, contextPosts) {
    const ours = post.author_side === (source === "home" ? "home" : "visitor");
    const article = make("article", "journal-post" + (ours ? " journal-post--ours" : ""));
    const header = make("header");
    const stamp = make("time", "", date(post.created_at));
    if (post.created_at) stamp.dateTime = post.created_at;
    header.append(make("strong", "", post.author_name || "未署名"),
      make("span", "", post.addressed_to ? "写给 " + post.addressed_to : "写给大家"), stamp);
    if (contextPosts?.has(post.id)) header.append(make("span", "journal-chip", "这次读到"));
    article.append(header, make("p", "", post.content || ""));
    return article;
  }
  function renderTopic(topic, visit) {
    const section = make("section", "journal-topic");
    section.append(make("h3", "", "《" + (topic.title || "朋友的纸条") + "》"),
      make("p", "journal-detail-meta", (topic.friend_name || visit.friend_name || "朋友留言")
        + " · " + (labels[topic.status] || "看了纸条")));
    if (topic.reason) section.append(make("p", "journal-reflection", topic.reason));
    if (topic.memory) {
      const memory = make("details");
      memory.append(make("summary", "", "留下的一点见闻"), make("p", "journal-reflection", topic.memory));
      section.append(memory);
    }
    if (topic.content) {
      const heading = topic.status === "started" ? "这次留下的新留言" : "这次发出的回复";
      const action = make("details");
      action.append(make("summary", "", heading), make("p", "journal-reflection", topic.content));
      section.append(action);
    }
    if (topic.pending_content) section.append(make("p", "journal-note", "你重新说话后，这条准备好的回复没有发出："),
      make("p", "journal-reflection", topic.pending_content));
    if (topic.planned_share_message) section.append(make("p", "journal-note", "先陪你聊天，这句分享没有插播：" + topic.planned_share_message));
    if (topic.attempted_content && ["unconfirmed", "sending"].includes(topic.status)) {
      section.append(make("p", "journal-note", "这条回复还没有收到成功回执，暂时无法确认是否送达。"),
        make("p", "journal-reflection", topic.attempted_content));
    }
    const paper = topic.snapshot;
    if (!paper) {
      section.append(make("p", "journal-note", "这份旧经历没有留下便签副本。"));
      return section;
    }
    if (topic.snapshot_kind === "current") section.append(make("p", "journal-note", "下方是这张便签现在的内容，旧记录没有保存当时的副本。"));
    if (topic.snapshot_kind === "cached") section.append(make("p", "journal-note", "下方是最后带回家的便签副本，无法确认它属于当年的哪次拜访。"));
    if (topic.copy_complete === false) section.append(make("p", "journal-note", "回复已收到成功回执。这里保留读到的全文和这次发出的留言，但暂时没能取回操作后的完整便签，可能缺少同时新写来的内容。"));
    if (topic.context_excerpt) section.append(make("p", "journal-note", "整张便签已带回；这次接话主要读了最近的留言。"));
    if (topic.read_post_ids) section.append(make("p", "journal-note", "保留整张便签；本次读到的留言另有标记。"));
    const readIds = topic.read_post_ids ? new Set(topic.read_post_ids) : null;
    for (const post of paper.posts || []) section.append(renderPost(post, visit.source, readIds));
    if (!paper.posts?.length) section.append(make("p", "journal-note", "对方这次没有返回留言正文，手记暂未取得完整副本。"));
    if (paper.status === "closed") section.append(make("p", "journal-note", "这张便签已由 " + (paper.closed_by || "参与者") + " 结束。"));
    if (topic.context_excerpt?.length) {
      const excerpt = make("details");
      excerpt.append(make("summary", "", "看看这次接话时读到的内容"));
      for (const post of topic.context_excerpt) excerpt.append(renderPost(post, visit.source));
      section.append(excerpt);
    }
    if (topic.share_message) section.append(make("p", "journal-note", "回家说了：" + topic.share_message));
    return section;
  }
  async function openVisit(id) {
    const request = ++detailGeneration;
    const dialog = $("#visit-dialog"), detail = $("#visit-detail");
    detail.replaceChildren(make("h2", "", "正在翻开这一页……"));
    detail.firstChild.id = "visit-detail-title";
    if (!dialog.open) dialog.showModal();
    dialog.scrollTop = 0;
    try {
      const visit = await api("/api/lounge-board/visits/" + encodeURIComponent(id));
      if (request !== detailGeneration) return;
      names = visit.names || names;
      const title = make("h2", "", (names[visit.actor_id] || "家人") + "的这次小拜访");
      title.id = "visit-detail-title";
      detail.replaceChildren(make("span", "eyebrow", visit.legacy ? "AN EARLIER MEMORY" : "A LITTLE VISIT"), title,
        make("p", "journal-detail-meta", date(visit.started_at) + " · " + locationText(visit)
          + " · " + (triggers[visit.trigger] || "一次小拜访")));
      if (visit.legacy) detail.append(make("p", "journal-reflection", "这是以前留下的经历，还不是完整的拜访手记。已有的见闻、操作和留存便签都整理在下面，缺少的部分无法补回。"));
      if (visit.reason) detail.append(make("p", "journal-reflection", visit.reason));
      if (visit.memory) detail.append(make("p", "journal-reflection", "留下的见闻：" + visit.memory));
      if (visit.old_notes) for (const note of visit.old_notes) detail.append(make("p", "journal-reflection", note.summary));
      for (const topic of visit.topics) detail.append(renderTopic(topic, visit));
      if (!visit.topics.length) detail.append(make("p", "journal-reflection", visit.status === "nothing_new"
        ? "这次检查没有新的访客留言，没有唤醒家里人。" : "这次没有带回便签。"));
      if (visit.attempted_action && visit.status === "failed" && !visit.topics.some(topic => topic.status === "started")) {
        detail.append(make("p", "journal-note", "原本想写一张新便签，尚未确认送达：《" + visit.attempted_action.title + "》"),
          make("p", "journal-reflection", visit.attempted_action.content));
      }
      if (visit.share_message) detail.append(make("p", "journal-reflection", "回家说了：" + visit.share_message));
      if (visit.pending_action?.content) detail.append(make("p", "journal-note", "你重新说话后，这段准备好的留言没有发出："),
        make("p", "journal-reflection", visit.pending_action.content));
      if (visit.planned_share_message) detail.append(make("p", "journal-note", "先陪你聊天，这句分享没有插播：" + visit.planned_share_message));
      if (visit.status === "waiting_user") detail.append(make("p", "journal-note", "你重新说话了，这次查看先停在这里。已经完成的操作与便签保留在手记里，自动巡看会等聊天安静满 15 分钟再继续。"));
      if (["failed", "interrupted", "running"].includes(visit.status)) detail.append(make("p", "journal-note", labels[visit.status] + "。已经完成的操作和取回的便签仍保留在这一页。"));
    } catch (error) {
      if (request !== detailGeneration) return;
      const heading = make("h2", "", "这一页暂时没能翻开");
      heading.id = "visit-detail-title";
      detail.replaceChildren(heading, make("p", "journal-reflection", error.message));
    }
  }
  async function load(reset = true) {
    const request = ++generation;
    if (reset) { offset = 0; $("#visit-list").replaceChildren(); $("#visit-more").hidden = true; }
    $("#visit-status").textContent = "正在把手记取来……";
    $("#visit-more").disabled = true;
    $("#visit-refresh").disabled = true;
    const query = new URLSearchParams({limit: "20", offset: String(offset)});
    for (const [field, selector] of [["actor_id", "#visit-actor"], ["source", "#visit-source"]]) {
      if ($(selector).value) query.set(field, $(selector).value);
    }
    try {
      const data = await api("/api/lounge-board/visits?" + query);
      if (request !== generation) return;
      names = data.names || names;
      if ($("#visit-actor").options.length === 1) for (const actor of ["aion", "connor"]) {
        const option = make("option", "", names[actor] || "家人");
        option.value = actor;
        $("#visit-actor").append(option);
      }
      $("#visit-list").append(...data.items.map(cover));
      offset += data.items.length;
      $("#visit-status").textContent = data.total ? "已翻开 " + offset + " / " + data.total + " 页" : "这里还没有拜访手记";
      if (!data.total) $("#visit-list").append(make("p", "empty", "等家里人下次去看纸条，\n就会带回一页新的手记。"));
      $("#visit-more").hidden = offset >= data.total;
    } catch (error) {
      if (request === generation) $("#visit-status").textContent = error.message;
    } finally {
      if (request === generation) { $("#visit-more").disabled = false; $("#visit-refresh").disabled = false; }
    }
  }
  $("#visit-close").addEventListener("click", () => { ++detailGeneration; $("#visit-dialog").close(); });
  $("#visit-dialog").addEventListener("cancel", () => { ++detailGeneration; });
  $("#visit-refresh").addEventListener("click", () => load());
  $("#visit-more").addEventListener("click", () => load(false));
  $("#visit-actor").addEventListener("change", () => load());
  $("#visit-source").addEventListener("change", () => load());
  window.handleLoungeBoardBack = () => {
    if (!$("#visit-dialog").open) return false;
    ++detailGeneration; $("#visit-dialog").close(); return true;
  };
  const params = new URLSearchParams(location.search);
  if (["home", "outbound"].includes(params.get("source"))) $("#visit-source").value = params.get("source");
  load().then(() => { if (params.get("visit")) openVisit(params.get("visit")); });
})();

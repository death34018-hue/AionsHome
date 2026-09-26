(() => {
  "use strict";

  const notice = document.getElementById("homeBoardNotice");
  if (!notice) return;
  let checking = false;

  async function check() {
    if (checking || document.hidden) return;
    checking = true;
    try {
      const response = await fetch("/api/lounge-board/notice", {cache: "no-store"});
      if (!response.ok) throw new Error("留言板提醒暂时不可用");
      notice.hidden = !(await response.json()).has_new;
    } catch (_) {
      notice.hidden = true;
    } finally {
      checking = false;
    }
  }

  notice.addEventListener("click", () => openApp("/lounge-board"));
  window.addEventListener("focus", check);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) check(); });
  setInterval(check, 15000);
  check();
})();

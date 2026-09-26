(() => {
  "use strict";
  const root = document.documentElement;
  if (navigator.userAgent.includes("AionChatApp")) root.classList.add("aion-app");
  if (window.parent !== window) root.classList.add("aion-iframe");

  function syncChrome() {
    const color = getComputedStyle(root).getPropertyValue("--board-bg").trim();
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = color;
    if (window.AionStatusBar) window.AionStatusBar.setBarStyle(window.AionTheme.current());
    // The chat shell owns the status-bar inset when this page opens inside it.
    if (window.parent !== window) {
      try { window.parent.syncSubPageChromeFromFrame?.(window.frameElement); }
      catch (_) { /* A standalone page needs no parent chrome. */ }
    }
  }

  window.addEventListener("aion-theme-applied", syncChrome);
  syncChrome();
})();

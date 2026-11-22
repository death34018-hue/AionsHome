// 首页、私聊和群聊共用同一份氛围；页面各自负责渲染。
(() => {
  const source = Math.random().toString(36).slice(2);
  let revision = 0;

  function put(patch) {
    return fetch('/api/chat-ambience', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...patch, source })
    });
  }

  function receive(data) {
    if (!data || data.source === source) return;
    revision++;
    if ((data.theme === 'light' || data.theme === 'dark') && data.theme !== document.body.dataset.theme) {
      const apply = window.applyChatroomTheme || window.applyAionTheme;
      apply(data.theme);
    }
    window.AionChatAmbience?.applyRemote(data);
  }

  async function restore() {
    const start = ++revision;
    try {
      let local;
      try { local = JSON.parse(localStorage.getItem('aion_chat_ambience')); } catch (_) {}
      const response = await put({
        only_missing: true,
        theme: document.body.dataset.theme,
        effect: local?.effect || 'none',
        tint: local?.tint || ''
      });
      if (!response.ok) return;
      const { state } = await response.json();
      if (start === revision) receive(state);
    } catch (_) {}
  }

  function save(patch) {
    revision++;
    return put(patch).catch(() => {});
  }

  window.AionAmbienceSync = { receive, restore, save };
})();

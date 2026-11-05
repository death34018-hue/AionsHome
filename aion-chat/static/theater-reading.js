/* Adapter from stable theater chapter IDs to the shared published reading UI. */
const TheaterReading = (() => {
  let options, activeKey = '', generation = 0, pending = null, loaded = false;
  const state = () => options?.state();
  const keyOf = s => s?.visible && s.chapter?.status === 'ready' && !s.chapter.writing
    ? JSON.stringify([s.book.id, s.chapter.id, s.chapter.revision, s.chapter.title]) : '';
  async function request(url, method='GET') {
    const response = await fetch(url, {method});
    const data = await response.json();
    if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : '共读加载失败，请重试');
    return data;
  }
  function clear() {
    generation++; activeKey = ''; pending = null; loaded = false;
    ReadingCompanion.reset();
    if (options) options.inviteHost.hidden = true;
  }
  function decorate(content) {
    const source = state()?.chapter?.content || '';
    const lines = [...source.matchAll(/[^\r\n]+/g)].filter(m => m[0].trim());
    content.querySelectorAll('p[data-p]').forEach(p => {
      const line = lines[Number(p.dataset.p)];
      if (!line) return;
      const start = line.index + line[0].length - line[0].trimStart().length;
      p.dataset.sourceStart = start;
      p.dataset.sourceEnd = start + line[0].trim().length;
    });
    content.querySelectorAll('.book-img img').forEach(img => {
      img.tabIndex = 0; img.setAttribute('role', 'button');
      img.setAttribute('aria-label', '查看并保存故事插画');
    });
    options.onRender?.();
  }
  async function load(create=false) {
    const requestedKey = keyOf(state());
    if (pending) {
      await pending;
      if (!create) return;
    }
    const s = state(), key = keyOf(s), token = generation;
    if (!key || key !== requestedKey || key !== activeKey || ReadingCompanion.busy) return;
    const run = async () => {
      const link = await request('/api/theater/studio/chapters/'+encodeURIComponent(s.chapter.id)+'/reading', create ? 'POST' : 'GET');
      if (token !== generation || key !== keyOf(state())) return;
      if (!link) {
        if (loaded) { ReadingCompanion.reset(); loaded = false; options.onUnavailable?.(); }
        if (create) throw Error('本章已变化，请重新确认完成后共读');
        return;
      }
      const [detail, data] = await Promise.all([
        request('/api/books/'+link.book_id),
        request('/api/books/'+link.book_id+'/chapters/'+link.chapter_index)
      ]);
      if (token !== generation || key !== keyOf(state())) return;
      if (data.chapter.id !== link.chapter_id) throw Error('本章已更新，请重新打开共读');
      loaded = true;
      s.book.reading_book_id = link.book_id;
      ReadingCompanion.setChapter(detail.book, link.chapter_index, data);
    };
    const task = run(); pending = task;
    try { await task; } finally { if (pending === task) pending = null; }
  }
  async function invite() {
    const key = keyOf(state()), token = generation;
    if (!key) return;
    const button = document.getElementById('inviteBtn');
    button.disabled = true;
    try {
      await load(true);
      if (loaded && token === generation && key === keyOf(state())) await ReadingCompanion.inviteRead();
    } finally { if (token === generation) button.disabled = false; }
  }
  function sync() {
    const key = keyOf(state());
    if (key !== activeKey) clear();
    if (!key) return false;
    options.inviteHost.hidden = false;
    if (!activeKey) {
      activeKey = key;
      load().catch(e => showToast(e.message));
    }
    return loaded;
  }
  function refresh() {
    if (document.hidden || !keyOf(state()) || ReadingCompanion.busy) return;
    return load();
  }
  function mount(settings) {
    options = settings;
    ReadingCompanion.mount({content:settings.content, inviteHost:settings.inviteHost,
      onRender:decorate, onInvite:invite, onRefresh:refresh});
    setInterval(() => refresh()?.catch(() => {}), 10000);
  }
  return {mount, sync, clear, refresh};
})();
window.TheaterReading = TheaterReading;

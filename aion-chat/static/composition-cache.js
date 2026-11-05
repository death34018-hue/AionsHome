(function (root) {
  'use strict';
  const KEY = 'aionshome.music-box.v1';
  const FRESH_MS = 5 * 60 * 1000;

  function createCache(options = {}) {
    let storage = options.storage;
    try { if (!storage) storage = root.localStorage; } catch (_) {}
    const fetcher = options.fetch || root.fetch?.bind(root);
    let state = {items: {}, listIds: null};
    try {
      const saved = JSON.parse(storage?.getItem(KEY) || 'null');
      if (saved?.items && typeof saved.items === 'object') state = saved;
    } catch (_) {}
    const pending = new Map();
    const attempts = new Map();
    let listRequest;
    const valid = piece => piece && typeof piece.id === 'string' && typeof piece.code === 'string';

    function put(piece) {
      const previous = state.items[piece.id]?.piece;
      if (Number(previous?.updated_at || 0) > Number(piece.updated_at || 0)) return previous;
      delete state.items[piece.id];
      state.items[piece.id] = {piece, cachedAt: Date.now()};
      return piece;
    }

    function persist() {
      state.items = Object.fromEntries(Object.entries(state.items).slice(-200));
      if (Array.isArray(state.listIds)) state.listIds = state.listIds.filter(id => state.items[id]);
      try { storage?.setItem(KEY, JSON.stringify(state)); } catch (_) {}
    }

    function rememberPiece(piece) {
      if (!valid(piece)) return;
      const current = put(piece);
      if (Array.isArray(state.listIds) && !state.listIds.includes(piece.id)) state.listIds.unshift(piece.id);
      persist();
      return current;
    }

    async function json(url, headers = {}) {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 8000);
      try {
        const response = await fetcher(url, {cache: 'no-store', signal: controller.signal, headers});
        if (response.status === 304) return {unchanged: true};
        if (!response.ok) throw new Error('暂时连不上小家，请稍后重试');
        return {data: await response.json(), etag: response.headers.get('ETag')};
      } catch (_) {
        throw new Error('暂时连不上小家，请稍后重试');
      } finally { clearTimeout(timeout); }
    }

    function refreshPiece(id) {
      if (pending.has(id)) return pending.get(id);
      attempts.set(id, Date.now());
      const request = json('/api/compositions/' + encodeURIComponent(id)).then(({data: piece}) => {
        if (!valid(piece)) throw new Error('这首小曲暂时无法读取');
        return rememberPiece(piece);
      }).finally(() => pending.delete(id));
      pending.set(id, request);
      return request;
    }

    async function getPiece(id) {
      const cached = state.items[id];
      if (!valid(cached?.piece)) return refreshPiece(id);
      // Playback never waits for a weak connection when this score is already here.
      if (Date.now() - (attempts.get(id) || cached.cachedAt) >= FRESH_MS && root.navigator?.onLine !== false) {
        refreshPiece(id).catch(() => {});
      }
      return cached.piece;
    }

    function getCachedList() {
      const ids = Array.isArray(state.listIds) ? state.listIds : Object.keys(state.items).reverse();
      if (!ids.length && !Array.isArray(state.listIds)) return null;
      return ids.map(id => state.items[id]?.piece).filter(valid);
    }

    function loadList() {
      if (listRequest) return listRequest;
      const headers = state.listEtag ? {'If-None-Match':state.listEtag} : {};
      listRequest = json('/api/compositions', headers).then(({data: result, etag, unchanged}) => {
        if (unchanged) return getCachedList() || [];
        if (!Array.isArray(result.compositions)) throw new Error('音乐盒暂时没有打开，请稍后重试');
        if (result.authors) state.authors = result.authors;
        const pieces = result.compositions.filter(valid);
        for (const piece of [...pieces].reverse()) put(piece);
        const current = pieces.map(piece => state.items[piece.id].piece);
        state.listIds = pieces.map(piece => piece.id);
        // A bounded snapshot cannot reconstruct a larger 304 response.
        state.listEtag = pieces.length <= 200 ? etag : '';
        persist();
        return current;
      }).finally(() => { listRequest = null; });
      return listRequest;
    }

    return {getPiece, getCachedList, loadList, rememberPiece, getAuthors: () => state.authors || {}};
  }

  if (typeof module === 'object' && module.exports) module.exports = {createCache};
  else root.CompositionCache = createCache();
})(typeof globalThis !== 'undefined' ? globalThis : this);

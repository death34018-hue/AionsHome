(() => {
  const escape = text => String(text ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function safeImage(value) {
    try {
      const url = new URL(value);
      return url.protocol === 'https:' && !url.username && (url.hostname === 'alicdn.com' || url.hostname.endsWith('.alicdn.com')) ? url.href : '';
    } catch {return '';}
  }
  function returnUrl(value) {
    try {
      const origin = window.location?.origin || 'http://localhost';
      const url = new URL(value || '/', origin);
      if (url.origin === origin && ['/', '/chat', '/chatroom'].includes(url.pathname)) return url.pathname + url.search;
    } catch {}
    return '/';
  }
  function navigate(event, value) {
    if (event && (event.defaultPrevented || event.button > 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey)) return;
    const target = new URL(value, window.location.origin);
    if (target.origin !== window.location.origin) return;
    event?.preventDefault();
    const path = target.pathname + target.search + target.hash;
    const host = window.parent !== window ? window.parent : window;
    if (target.pathname === '/chat' && typeof host.closeSubPage === 'function') {
      const conv = target.searchParams.get('conv');
      host.closeSubPage(!!conv);
      if (conv && typeof host.selectConv === 'function') host.selectConv(conv);
    } else if (typeof host.openSubPage === 'function') host.openSubPage(path);
    else window.location.href = path;
  }
  function render(attachments, options = {}) {
    const card = (Array.isArray(attachments) ? attachments : []).find(a => a?.type === 'taobao_trip');
    if (!card || !/^[A-Za-z0-9_-]+$/.test(card.trip_id || '') || !Array.isArray(card.products)) return '';
    const products = card.products.slice(0, 3);
    if (!products.length) return '';
    const count = Math.max(products.length, Number(card.count) || 0);
    const image = safeImage(products[0].image);
    const preview = image
      ? `<img src="${escape(image)}" alt="${escape(products[0].title)}" loading="lazy" referrerpolicy="no-referrer">`
      : '<span class="taobao-notice-placeholder" aria-label="暂无商品图片">心愿</span>';
    const titles = products.map(p => p.title || '商品').join(' / ');
    return `<a class="taobao-notice-card" href="/taobao?trip=${encodeURIComponent(card.trip_id)}&amp;return=${encodeURIComponent(returnUrl(options.returnTo))}" aria-label="查看这次逛街的 ${count} 件商品">
      <span class="taobao-notice-preview">${preview}</span>
      <span class="taobao-notice-copy"><span class="taobao-notice-heading">心愿袋 · ${count} 件<span>未购买</span></span>
      <span class="taobao-notice-title" title="${escape(titles)}">${escape(titles)}</span></span>
      <span class="taobao-notice-arrow" aria-hidden="true">›</span>
    </a>`;
  }
  window.TaobaoCards = {render, navigate, returnUrl};
  if (typeof document !== 'undefined') document.addEventListener('click', event => {
    const link = event.target.closest?.('a.taobao-notice-card');
    if (link) navigate(event, link.href);
  });
})();

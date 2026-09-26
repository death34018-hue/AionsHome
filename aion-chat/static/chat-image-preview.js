(function (root) {
  'use strict';
  const escape = value => String(value).replaceAll('&', '&amp;').replaceAll('"', '&quot;')
    .replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll("'", '&#39;');

  function attributes(url, escaped = false) {
    let original = String(url || '');
    if (escaped) {
      const text = document.createElement('textarea');
      text.innerHTML = original;
      original = text.value;
    }
    let source = original;
    try {
      const parsed = new URL(original, root.location.href);
      if (parsed.origin === root.location.origin
          && /^\/(uploads|cr-uploads|public|screenshots)\//.test(parsed.pathname)
          && !parsed.pathname.startsWith('/public/wallpaper/')
          && /\.(jpe?g|png|webp)$/i.test(parsed.pathname)) {
        source = '/api/chat-media/thumbnail?src=' + encodeURIComponent(parsed.pathname);
      }
    } catch (_) {}
    // Reserve space before lazy images load so off-screen history stays off-screen.
    return `src="${escape(source)}" data-original-src="${escape(original)}" loading="lazy" decoding="async" width="240" height="180" style="object-fit:contain;max-width:100%;border-radius:8px;cursor:pointer" onerror="this.onerror=null;this.src=this.dataset.originalSrc"`;
  }

  function original(img) { return img.dataset?.originalSrc || img.src; }
  root.ChatImagePreview = { attributes, original };
})(typeof window !== 'undefined' ? window : globalThis);

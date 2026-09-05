(function () {
  const KEY = 'aion_chat_theme';
  const STYLE_ID = 'aion-theme-css';
  const STYLE_HREF = '/static/theme.css?v=20260905-theater-v2';

  function normalizeTheme(theme) {
    return theme === 'light' ? 'light' : 'dark';
  }

  function ensureThemeStyles() {
    if (document.getElementById(STYLE_ID)) return;
    const link = document.createElement('link');
    link.id = STYLE_ID;
    link.rel = 'stylesheet';
    link.href = STYLE_HREF;
    document.head.appendChild(link);
  }

  function updateThemeChrome(theme) {
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', theme === 'dark' ? '#050923' : '#eef3ff');
    if (window.AionStatusBar) window.AionStatusBar.setBarStyle(theme);
  }

  function applyTheme(theme, options) {
    const next = normalizeTheme(theme);
    document.documentElement.dataset.theme = next;
    if (document.body) document.body.dataset.theme = next;
    if (!options || options.persist !== false) localStorage.setItem(KEY, next);
    updateThemeChrome(next);
    window.dispatchEvent(new CustomEvent('aion-theme-applied', { detail: { theme: next } }));
    return next;
  }

  function initialTheme() {
    return localStorage.getItem(KEY)
      || (document.body && document.body.dataset.theme)
      || document.documentElement.dataset.theme
      || 'light';
  }

  window.AionTheme = Object.assign(window.AionTheme || {}, {
    key: KEY,
    apply: applyTheme,
    ensureStyles: ensureThemeStyles,
    current: () => normalizeTheme((document.body && document.body.dataset.theme) || document.documentElement.dataset.theme || 'light')
  });
  if (!window.applyAionTheme) window.applyAionTheme = applyTheme;

  function initTheme() {
    ensureThemeStyles();
    applyTheme(initialTheme(), { persist: Boolean(localStorage.getItem(KEY)) });
  }

  if (document.body) initTheme();
  else document.addEventListener('DOMContentLoaded', initTheme, { once: true });

  window.addEventListener('storage', event => {
    if (event.key === KEY) applyTheme(event.newValue || initialTheme(), { persist: false });
  });
})();

(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.MessageRowFocus = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {
  function focusRowFromClick(event, row) {
    const interactive = event?.target?.closest?.('textarea, input, select, button, a, [contenteditable="true"]');
    if (interactive) return;
    row.focus();
  }

  return { focusRowFromClick };
});

(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.MonitorCameraSnapshot = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const SAFE_CAMERA_URL =
    /^\/uploads\/monitor_camera_[A-Za-z0-9_-]+(?:_[A-Za-z0-9_-]+)*\.jpe?g$/i;

  function renderMonitorCameraSnapshot(attachments, options) {
    const items = Array.isArray(attachments) ? attachments : [];
    const snapshot = items.find(item =>
      item &&
      typeof item === 'object' &&
      item.type === 'monitor_camera_snapshot' &&
      SAFE_CAMERA_URL.test(String(item.url || ''))
    );
    if (!snapshot) return '';

    const opts = options || {};
    const escapeHtml = typeof opts.escapeHtml === 'function'
      ? opts.escapeHtml
      : value => String(value);
    const imageAttrs = typeof opts.imageAttrs === 'string'
      ? opts.imageAttrs.trim()
      : '';
    const attrs = imageAttrs ? ` ${imageAttrs}` : '';
    const url = escapeHtml(snapshot.url);

    return `<details class="monitor-camera-snapshot">
      <summary>📷 查看本次摄像头画面</summary>
      <div class="monitor-camera-snapshot-frame">
        <img src="${url}" alt="本次摄像头画面" loading="lazy"${attrs} onerror="this.closest('details').hidden=true">
      </div>
    </details>`;
  }

  return {renderMonitorCameraSnapshot};
});

'use strict';

const assert = require('node:assert/strict');
const {
  renderMonitorCameraSnapshot,
} = require('./static/monitor-camera-snapshot.js');

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

const snapshot = {
  type: 'monitor_camera_snapshot',
  url: '/uploads/monitor_camera_phone_123.jpg',
};

{
  const html = renderMonitorCameraSnapshot([snapshot], {
    escapeHtml,
    imageAttrs: 'data-image-interaction="enabled"',
  });
  assert.match(html, /<details class="monitor-camera-snapshot">/);
  assert.doesNotMatch(html, /<details[^>]*\sopen(?:\s|>)/);
  assert.match(html, /查看本次摄像头画面/);
  assert.match(html, /\/uploads\/monitor_camera_phone_123\.jpg/);
  assert.match(html, /data-image-interaction="enabled"/);
  assert.match(html, /loading="lazy"/);
}

{
  const nonCameraAttachments = [
    '/uploads/phone-screen.jpg',
    {type: 'phone_screen', url: '/uploads/phone-screen.jpg'},
    {type: 'monitor_camera_snapshot', url: 'https://example.com/leak.jpg'},
    {type: 'monitor_camera_snapshot', url: '/uploads/../private.jpg'},
  ];
  assert.equal(
    renderMonitorCameraSnapshot(nonCameraAttachments, {escapeHtml}),
    '',
  );
}

{
  const html = renderMonitorCameraSnapshot(
    [{
      type: 'monitor_camera_snapshot',
      url: '/uploads/monitor_camera_safe.jpg?x="<unsafe>',
    }],
    {escapeHtml},
  );
  assert.equal(html, '');
}

console.log('monitor camera snapshot UI tests passed');

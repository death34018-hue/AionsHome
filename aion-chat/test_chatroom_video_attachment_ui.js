'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = __dirname;
const html = fs.readFileSync(path.join(root, 'static', 'chatroom.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'static', 'chatroom.js'), 'utf8');

assert.match(html, /id="fileInput"[^>]+accept="image\/\*,video\/\*"/);
assert.match(html, />上传附件</);

const renderAttachmentsSource = script.slice(
  script.indexOf('function renderAttachments('),
  script.indexOf('async function handleChatroomFileSelect('),
);
const attachmentContext = {
  esc: value => String(value),
  imageInteractionAttrs: () => '',
};
vm.runInNewContext(renderAttachmentsSource, attachmentContext);
const messageHtml = attachmentContext.renderAttachments(['/cr-uploads/2026-08-22/latest.mp4']);
assert.match(messageHtml, /<video[^>]+controls/);
assert.doesNotMatch(messageHtml, /<img/);

const renderPreviewSource = script.slice(
  script.indexOf('function renderPreview('),
  script.indexOf('function removeChatroomAttachment('),
);
const area = {className: '', innerHTML: ''};
const previewContext = {
  pendingAttachments: [{url: '/cr-uploads/2026-08-22/latest.mp4', type: 'video/mp4'}],
  document: {getElementById: () => area},
};
vm.runInNewContext(`${renderPreviewSource}\nrenderPreview();`, previewContext);
assert.match(area.innerHTML, /<video/);
assert.doesNotMatch(area.innerHTML, /<img/);

console.log('chatroom video attachment UI tests passed');

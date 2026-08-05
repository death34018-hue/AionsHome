'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');


function functionBlock(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.notEqual(start, -1, `missing ${startMarker}`);
  assert.notEqual(end, -1, `missing ${endMarker}`);
  return source.slice(start, end);
}


function loadMessageRenderer() {
  const source = fs.readFileSync(
    path.join(__dirname, 'static', 'chatroom.js'),
    'utf8',
  );
  const rendererSource = [
    functionBlock(source, 'function crMsgMenuHtml', 'function crCanRateAiMsg'),
    functionBlock(source, 'function crMsgSenderLineHtml', 'function crEnsureMsgMenu'),
    functionBlock(source, 'function crBubbleUnitHtml', 'function crRenderMessageItems'),
    functionBlock(source, 'function msgHTML', 'let crMsgFeedbackPopover'),
  ].join('\n');

  const context = {
    AVATARS: {
      user: '/avatar-user.png',
      aion: '/avatar-aion.png',
      connor: '/avatar-connor.png',
    },
    crMemoryRecordMsgIds: new Set(),
    crName: sender => ({ user: 'Ithil', aion: 'Aion', connor: 'Connor' })[sender],
    crStripWishFulfillmentMarker: value => value,
    crWithWishFallbackAttachments: message => message.attachments || [],
    crMessageContentItems: () => [],
    crRenderMessageItems: () => '',
    crBandVibrationNoteHtml: () => '',
    crMsgFeedbackHtml: () => '',
    esc: value => String(value ?? ''),
    escWithTransfer: value => String(value ?? ''),
    escWithImages: value => String(value ?? ''),
    renderToyAttachments: () => '',
    renderAttachments: attachments => attachments.map(item => (
      item.type === 'voice'
        ? `<div class="msg-media"><div class="voice-bubble">${item.transcript}</div></div>`
        : ''
    )).join(''),
    timeStr: () => '00:00',
  };
  vm.createContext(context);
  vm.runInContext(`${rendererSource}\nthis.renderMessage = msgHTML;`, context);
  return context.renderMessage;
}


test('pure user voice message renders avatar and delete-only menu once', () => {
  const msgHTML = loadMessageRenderer();
  const html = msgHTML({
    id: 'voice-1',
    sender: 'user',
    content: '测试语音',
    created_at: 1,
    attachments: [{
      type: 'voice',
      url: '/uploads/voice.webm',
      duration: 3,
      transcript: '测试语音',
    }],
  });

  assert.match(html, /class="avatar"/);
  assert.match(html, /src="\/avatar-user\.png"/);
  assert.equal((html.match(/class="voice-bubble"/g) || []).length, 1);
  assert.match(html, /deleteMsg\('voice-1'/);
  assert.doesNotMatch(html, /editChatroomMsg\('voice-1'/);
});

'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const SystemNoticeUI = require('./static/system-notice-ui.js');
const MonitorCameraSnapshot = require('./static/monitor-camera-snapshot.js');

const ROOT = __dirname;

function functionBlock(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.notEqual(start, -1, `missing ${startMarker}`);
  assert.notEqual(end, -1, `missing ${endMarker}`);
  return source.slice(start, end);
}

function privateSystemRenderer() {
  const source = fs.readFileSync(path.join(ROOT, 'static', 'chat.js'), 'utf8');
  const block = [
    functionBlock(source, 'function formatPrivateSystemNoticeContent', '// ── 渲染 ──'),
    functionBlock(source, 'function privateSystemMessageHTML', 'function renderMessages'),
  ].join('\n');
  const context = {
    currentConvId: 'conv-1',
    worldBook: { ai_name: '星野' },
    escHtml: value => String(value ?? ''),
    imageInteractionAttrs: () => '',
    systemNoticeAfterMsgId: () => '',
    systemNoticeBeforeMsgId: () => '',
    window: { LoungeVisitUI: null, MonitorCameraSnapshot, SystemNoticeUI, TaobaoCards: null },
  };
  vm.createContext(context);
  vm.runInContext(`${block}\nthis.renderSystem = privateSystemMessageHTML;`, context);
  return context.renderSystem;
}

function chatroomSystemRenderer() {
  const source = fs.readFileSync(path.join(ROOT, 'static', 'chatroom.js'), 'utf8');
  const blocks = [
    functionBlock(source, 'function crMsgMenuHtml', 'function crCanRateAiMsg'),
    functionBlock(source, 'function msgHTML', 'let crMsgFeedbackPopover'),
  ].join('\n');
  const context = {
    AVATARS: {},
    crMemoryRecordMsgIds: new Set(),
    crName: value => value,
    crMsgSenderLineHtml: () => '',
    crWithWishFallbackAttachments: message => message.attachments || [],
    crMessageContentItems: () => [],
    crRenderMessageItems: () => '',
    crBandVibrationNoteHtml: () => '',
    crMsgFeedbackHtml: () => '',
    crSystemNoticeAfterMsgId: () => '',
    crSystemNoticeBeforeMsgId: () => '',
    esc: value => String(value ?? ''),
    escWithTransfer: value => String(value ?? ''),
    escWithImages: value => String(value ?? ''),
    renderToyAttachments: () => '',
    renderAttachments: () => '',
    timeStr: () => '',
    currentRoom: { id: 'room-1' },
    imageInteractionAttrs: () => '',
    window: { LoungeVisitUI: null, MonitorCameraSnapshot, SystemNoticeUI, TaobaoCards: null },
  };
  vm.createContext(context);
  vm.runInContext(`${blocks}\nthis.renderSystem = msgHTML;`, context);
  return context.renderSystem;
}

test('private and chatroom system messages render one visible system marker', () => {
  const privateHtml = privateSystemRenderer()({
    id: 'sys-1', conv_id: 'conv-1', role: 'system', content: '星野查看了监控', attachments: [],
  });
  const chatroomHtml = chatroomSystemRenderer()({
    id: 'sys-2', room_id: 'room-1', sender: 'system', content: '星野查看了监控', attachments: [],
  });

  for (const html of [privateHtml, chatroomHtml]) {
    assert.equal((html.match(/system-notice-marker/g) || []).length, 1);
    assert.match(html, /aria-hidden="true">&gt;<\/span>/);
  }
});

test('system message markers start on the shared content rail on both chat surfaces', () => {
  const privateCss = fs.readFileSync(path.join(ROOT, 'static', 'chat.css'), 'utf8');
  const chatroomCss = fs.readFileSync(path.join(ROOT, 'static', 'chatroom.css'), 'utf8');

  assert.match(privateCss, /\.msg-row\.system\s*\{[^}]*margin-left:\s*0;/s);
  assert.match(chatroomCss, /\.system-event-msg\s*\{[^}]*margin-left:\s*0;/s);
  assert.match(privateCss, /\.system-notice-marker\s*\{[^}]*position:\s*static;/s);
  assert.match(chatroomCss, /\.system-notice-marker\s*\{[^}]*position:\s*static;/s);
});

test('system notices use the compact spacing of their shared content rail', () => {
  const privateCss = fs.readFileSync(path.join(ROOT, 'static', 'chat.css'), 'utf8');
  const chatroomCss = fs.readFileSync(path.join(ROOT, 'static', 'chatroom.css'), 'utf8');

  assert.match(privateCss, /\.msg-row\.system\s*\{[^}]*margin:\s*0 0 4px;/s);
  assert.match(chatroomCss, /\.system-event-msg\s*\{[^}]*margin:\s*8px 0 0;/s);
});

test('system messages are plain text rows without a capsule background', () => {
  const privateCss = fs.readFileSync(path.join(ROOT, 'static', 'chat.css'), 'utf8');
  assert.match(privateCss, /\.msg-row\.system \.system-notice\s*\{[^}]*background:\s*transparent;[^}]*border:\s*0;/s);
});

test('schedule alarm and supervision notices are collapsed to a short summary', () => {
  const messages = [
    '【星野】设定了闹铃：2026-09-02 07:30，内容：起床',
    '📅 【星野】设定了日程：2026-09-02 12:00，内容：午饭',
    '👀 【星野】设定了监督：2026-09-02 20:30，内容：看看战况',
  ];
  const renderers = [privateSystemRenderer(), chatroomSystemRenderer()];

  for (const render of renderers) {
    messages.forEach((content, index) => {
      const html = render({
        id: `schedule-${index}`, conv_id: 'conv-1', room_id: 'room-1',
        role: 'system', sender: 'system', content, attachments: [],
      });
      assert.match(html, /<details class="system-notice-details">/);
      assert.doesNotMatch(html, /<details[^>]*\sopen(?:\s|>)/);
      assert.match(html, /<summary>【星野】设定了(?:闹铃|日程|监督)<\/summary>/);
      assert.match(html, /class="system-notice-full"/);
    });
  }
});

test('camera snapshot control shares the monitor system line', () => {
  const attachment = {
    type: 'monitor_camera_snapshot',
    url: '/screenshots/monitor_camera_phone_123.jpg',
  };
  const renderers = [privateSystemRenderer(), chatroomSystemRenderer()];

  for (const render of renderers) {
    const html = render({
      id: 'monitor-1', conv_id: 'conv-1', room_id: 'room-1',
      role: 'system', sender: 'system', content: '📷 星野查看了监控',
      attachments: [attachment],
    });
    assert.match(html, /<summary>[^<]*📷 (?:【星野】|星野)查看了监控[^<]*· 查看画面/);
    assert.equal((html.match(/星野】?查看了监控/g) || []).length, 1);
    assert.doesNotMatch(html, /查看本次摄像头画面/);
  }
});

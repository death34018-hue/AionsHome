'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const SystemNoticeUI = require('./static/system-notice-ui.js');
const MonitorCameraSnapshot = require('./static/monitor-camera-snapshot.js');

const ROOT = __dirname;

test('ANKNI notices expand into per-segment seconds and user mode names in both chats', () => {
  const raw='[ANKNI:LOOP:2000,2;3000,5;1000,0;2500,10;200,9]';
  const attachments=[{type:'ankni_command_notice',raw,format:'duration_modes_v1'}];
  for(const render of [privateSystemRenderer(),chatroomSystemRenderer()]) {
    const html=render({id:'ankni',role:'system',sender:'system',content:'ANKNI编排 · 5 段循环',attachments});
    assert.match(html,/<summary>💗趴趴猫 控制 · 5段心动<\/summary>/);
    assert.ok(html.includes('2秒-渐入佳境；3秒-蛮牛冲撞；1秒-停止；2.5秒-冲刺；0.2秒-快速震弹'));
    assert.doesNotMatch(html,/<details[^>]*\sopen|ANKNI:LOOP/);
  }
  assert.equal(attachments[0].raw,raw);
  const old='[ANKNI:LOOP:2000,30,0]';
  const html=SystemNoticeUI.renderSystemNoticeContent('ANKNI编排 · 1 段循环',{
    attachments:[{type:'ankni_command_notice',raw:old+'<script>bad</script>'}],
  });
  assert.ok(html.includes(old));
  assert.match(html,/&lt;script&gt;/);
  assert.doesNotMatch(html,/<script>/);
  const oldCumulative='[ANKNI:LOOP:2000,5;3000,6]';
  const historical=SystemNoticeUI.renderSystemNoticeContent('ANKNI编排 · 2 段循环',{
    attachments:[{type:'ankni_command_notice',raw:oldCumulative}],
  });
  assert.ok(historical.includes(oldCumulative),'historical end-time commands must not be reinterpreted as durations');
});

test('new toy command notices remain collapsed and escape text around translated commands in both chats', () => {
  const attachments = [{type: 'svakom_command_notice', raw: '[SVAKOM:STOP]<script>bad</script>', status: '已广播，执行未确认'}];
  const html = SystemNoticeUI.renderSystemNoticeContent('新玩具编排 · 停止', {attachments});
  assert.match(html, /<details class="system-notice-details">/);
  assert.doesNotMatch(html, /<details[^>]*\sopen|<script>/);
  assert.match(html, /\[SVAKOM:全部停止\]&lt;script&gt;/);
  assert.match(html, /<summary>💗谜语时刻·停止<\/summary>/);
  assert.doesNotMatch(html, /已广播|执行未确认|<br>/);
  const loop = SystemNoticeUI.renderSystemNoticeContent('新玩具编排 · 3 段循环', {attachments});
  assert.match(loop, /<summary>💗谜语时刻·3段循环<\/summary>/);
  for (const render of [privateSystemRenderer(), chatroomSystemRenderer()]) {
    const result = render({id: 'notice', role: 'system', sender: 'system', content: '新玩具 · 停止', attachments});
    assert.match(result, /<details class="system-notice-details">/);
    assert.match(result, /SVAKOM:全部停止/);
    assert.doesNotMatch(result, /已广播|执行未确认/);
  }
});

test('toy loop notices translate exact channel values with heart separators without changing saved commands', () => {
  const raw = '[SVAKOM:LOOP:4,1,2,2,0;4,3,0,0,2;2,0,0,0,0]';
  const expected = '[SVAKOM:循环:4秒,慢速旋转伸缩,轻柔细振波,力度2,豆豆拍打0💗4秒,三短一长,震动关闭,力度0,豆豆拍打2💗2秒,主体关闭,震动关闭,力度0,豆豆拍打0]';
  const attachments = [{type:'svakom_command_notice', raw}];
  for (const render of [privateSystemRenderer(), chatroomSystemRenderer()]) {
    const html = render({id:'translated',role:'system',sender:'system',content:'新玩具编排 · 3 段循环',attachments});
    assert.ok(html.includes(expected));
    assert.ok(!html.includes('LOOP:'));
  }
  assert.equal(attachments[0].raw, raw);
});

test('toy translation covers all factory names and leaves unrecognized rows inspectable', () => {
  const { STRETCH_MODES, VIBRATE_MODES } = require('./static/toy-svakom.js');
  for (let i = 1; i <= 10; i++) {
    const stretch = Math.min(i, 7);
    const html = SystemNoticeUI.renderSystemNoticeContent('新玩具编排 · 1 段循环', {
      attachments:[{type:'svakom_command_notice',raw:`[SVAKOM:LOOP:1,${stretch},${i},10,7]`}],
    });
    assert.ok(html.includes(`1秒,${STRETCH_MODES[stretch-1][0]},${VIBRATE_MODES[i-1][0]},力度10,豆豆拍打7`));
  }
  for (const raw of ['[SVAKOM:LOOP:4,9,2,2,0]', '[SVAKOM:LOOP:4,1,0,2,0]', '[SVAKOM:LOOP:broken]', '[SVAKOM:LOOP:4,1,2']) {
    assert.ok(SystemNoticeUI.renderSystemNoticeContent('未下发', {attachments:[{type:'svakom_command_notice',raw}]}).includes(raw));
  }
});

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
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'static', 'repair-result-card.js'), 'utf8'), context);
  vm.runInContext(`${blocks}\nthis.renderSystem = msgHTML;`, context);
  return context.renderSystem;
}

test('repair results render one summary card without repeating the system text', () => {
  const summary='已完成文档写入及图片交付，文件内容读回一致。';
  const html=chatroomSystemRenderer()({id:'repair-1',sender:'system',content:'🛠️ 维修室 · 测试\n用户已验收\n'+summary,
    attachments:[{type:'repair_result',task_id:'a'.repeat(32),summary}]});
  assert.equal(html.split(summary).length-1,1);
  assert.doesNotMatch(html,/system-notice-marker|system-event-text/);
  assert.match(html,/repair-result-card/);
  const escaped=chatroomSystemRenderer()({id:'repair-2',sender:'system',content:'标题\n状态',
    attachments:[{type:'repair_result',task_id:'b'.repeat(32),summary:'<script>bad</script>'}]});
  assert.doesNotMatch(escaped,/<script>/);
});

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

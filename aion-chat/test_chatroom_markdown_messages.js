'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const ROOT = __dirname;

function functionBlock(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.notEqual(start, -1, `missing ${startMarker}`);
  assert.notEqual(end, -1, `missing ${endMarker}`);
  return source.slice(start, end);
}

function loadMessageRenderer() {
  const source = fs.readFileSync(path.join(ROOT, 'static', 'chatroom.js'), 'utf8');
  const markdown = require('./static/chatroom-markdown.js');
  const rendererSource = [
    functionBlock(source, 'function crMsgMenuHtml', 'function crCanRateAiMsg'),
    functionBlock(source, 'function crMsgSenderLineHtml', 'function crEnsureMsgMenu'),
    functionBlock(source, 'function crBubbleParts', 'let crProactiveCompanionshipStatus'),
    functionBlock(source, 'function crBubbleUnitHtml', 'function crBandVibrationNoteHtml'),
    functionBlock(source, 'function crPrivateWhisperNoteHtml', 'let crMsgFeedbackPopover'),
  ].join('\n');

  const context = {
    document: {readyState:'loading',addEventListener() {}},
    AVATARS: {
      user: '/avatar-user.png',
      aion: '/avatar-aion.png',
      connor: '/avatar-connor.png',
    },
    crMemoryRecordMsgIds: new Set(),
    crName: sender => ({ user: 'Test User', aion: 'Primary AI', connor: 'Second AI' })[sender],
    crStripWishFulfillmentMarker: value => value,
    crWithWishFallbackAttachments: message => message.attachments || [],
    crBandVibrationNoteHtml: () => '',
    crMsgFeedbackHtml: () => '',
    esc: markdown.escapeHtml,
    escWithTransfer: markdown.escapeHtml,
    escWithImages: markdown.escapeHtml,
    renderToyAttachments: () => '',
    renderAttachments: () => '',
    timeStr: () => '16:42',
    window: { LoungeVisitUI: null, ChatroomMarkdown: markdown, SystemNoticeUI: require('./static/system-notice-ui.js') },
  };
  context.window.AionPat = require('./static/pat.js');
  context.window.addEventListener = () => {};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(ROOT,'static','kitchen-cards.js'),'utf8'),context);
  vm.runInContext(`${rendererSource}\nthis.renderMessage = msgHTML;`, context);
  return context.renderMessage;
}

test('each actor keeps the fridge lookup between the preamble and the returned reply', () => {
  const before = '我去翻冰箱😊。';
  for (const [sender,name] of [['aion','Primary AI'],['connor','Second AI']]) {
    const message = {id:'fridge',sender,content:before+'\n\n看好啦，**先吃饭**。',attachments:[
      {type:'kitchen_lookup',actor:sender,actor_name:'旧名字',offset:before.length,before,status:'done'},
    ]};
    const render = loadMessageRenderer(), html = render(message);
    assert.ok(html.indexOf(before)<html.indexOf('class="kc-lookup"'));
    assert.ok(html.indexOf('class="kc-lookup"')<html.indexOf('看好啦'));
    assert.match(html,new RegExp('「'+name+'」查看了冰箱库存'));
    assert.match(html,/<strong>先吃饭<\/strong>/);
    assert.doesNotMatch(render({...message,attachments:[]}),/查看了冰箱库存/);
  }
});

test('fridge lookup follows its preamble after saved paragraph whitespace is compressed', () => {
  const before = '前面的话😊。\n\n\n\n先去翻一下冰箱😼。';
  const html = loadMessageRenderer()({
    id:'compressed-fridge',sender:'connor',
    content:before.replace(/\n+/g,'\n')+'\n家底看完了，嚯。',
    attachments:[{type:'kitchen_lookup',actor:'connor',offset:before.length,before,status:'done'}],
  });
  assert.ok(html.indexOf('class="kc-lookup"')<html.indexOf('家底看完了'));
  assert.match(html,/家底看完了，嚯。/);
});

test('private whisper receipt is shown below the reply, including an empty public reply', () => {
  for (const content of ['公开正文', '']) {
    const message = { id: 'whisper', sender: 'aion', content, attachments: [{ type: 'private_whisper_sent', count: 1 }] };
    const html = loadMessageRenderer()(message);
    assert.match(html, /已给你发了私聊/);
    assert.ok(html.indexOf('private-whisper-note') > html.indexOf('message-stack'));
    assert.doesNotMatch(loadMessageRenderer()({ ...message, attachments: [] }), /已给你发了私聊/);
  }
});

test('private whisper links use the original destination and preserve old receipts', () => {
  for (const [sender, target_type, target_id, url] of [
    ['aion', 'conversation', 'conv-old', '/chat?conv=conv-old'],
    ['connor', 'room', 'room-old', '/chatroom?room=room-old'],
  ]) {
    const html = loadMessageRenderer()({ id: 'source', sender, content: '正文',
      attachments: [{ type: 'private_whisper_sent', count: 1, target_type, target_id }] });
    assert.ok(html.includes(`href="${url}"`));
    assert.match(html, /<a class="private-whisper-note"/);
  }
  const old = loadMessageRenderer()({ id: 'old', sender: 'aion', content: '',
    attachments: [{ type: 'private_whisper_sent', count: 1 }] });
  assert.match(old, /已给你发了私聊/);
  assert.doesNotMatch(old, /<a class="private-whisper-note"/);
});

test('private whisper navigation switches rooms repeatedly without reloading the chatroom page', async () => {
  const source = fs.readFileSync(path.join(ROOT, 'static', 'chatroom.js'), 'utf8');
  const calls = [];
  const context = {
    window: { parent: {
      closeSubPage: skipReload => calls.push(['close', skipReload]),
      selectConv: async id => calls.push(['conversation', id]),
      openSubPage: url => calls.push(['page', url]),
    } },
    rooms: [{ id: 'room-old', type: 'connor_1v1' }],
    currentRoom: { id: 'group', type: 'group' },
    async selectRoom(id) {
      context.currentRoom = context.rooms.find(room => room.id === id);
      calls.push(['room', id]);
    },
    async api(path) {
      assert.equal(path, '/rooms');
      return [{ id: 'room-old', type: 'connor_1v1' }];
    },
    toast: message => assert.fail(message),
  };
  vm.createContext(context);
  vm.runInContext(functionBlock(source, 'async function crOpenPrivateWhisper', 'function msgHTML'), context);
  const event = { stopPropagation() {}, preventDefault() { calls.push(['prevent']); } };
  await context.crOpenPrivateWhisper(event, { dataset: { targetType: 'conversation', targetId: 'conv-old' } });
  assert.deepEqual(calls, [['prevent'], ['close', true], ['conversation', 'conv-old']]);
  calls.length = 0;
  const roomLink = { dataset: { targetType: 'room', targetId: 'room-old' }, getAttribute: () => '/chatroom?room=room-old' };
  for (let click = 0; click < 2; click++) {
    context.currentRoom = { id: 'group', type: 'group' };
    await context.crOpenPrivateWhisper(event, roomLink);
    assert.equal(context.currentRoom.id, 'room-old');
    assert.deepEqual(calls, [['prevent'], ['room', 'room-old']]);
    calls.length = 0;
  }
  context.window.parent = context.window;
  context.rooms = []; // A newly created private room may not have arrived over WS yet.
  await context.crOpenPrivateWhisper(event, roomLink);
  assert.equal(context.currentRoom.id, 'room-old');
  assert.deepEqual(calls, [['prevent'], ['room', 'room-old']]);
  calls.length = 0;
  await context.crOpenPrivateWhisper(event, { dataset: { targetType: 'conversation', targetId: 'conv-old' } });
  assert.deepEqual(calls, []);
});

test('old background toy tags in either AI reply become folded display-only notices', () => {
  for (const sender of ['aion','connor']) {
    const html = loadMessageRenderer()({id:'old-toy',sender,created_at:1,attachments:[],content:'[SVAKOM:LOOP:4,1,2,2,0]查岗正文'});
    assert.doesNotMatch(html, /SVAKOM:LOOP/);
    assert.match(html, /<details class="system-notice-details">/);
    assert.match(html, /查岗正文/);
    assert.match(html, /SVAKOM:循环:4秒,慢速旋转伸缩/);
  }
});

function renderStreamingMessage(sender = 'aion') {
  const source = fs.readFileSync(path.join(ROOT, 'static', 'chatroom.js'), 'utf8');
  const rendererSource = [
    functionBlock(source, 'function crMessageUnitHtml', 'const CR_MESSAGE_SPECIAL_TOKEN_RE'),
    functionBlock(source, 'function startStreamingBubble', 'function crStripScheduleCommands'),
  ].join('\n');
  let appended = null;
  const messagesEl = {
    querySelector: () => null,
    appendChild: row => { appended = row; },
  };
  const context = {
    AVATARS: { user: '/avatar-user.png', aion: '/avatar-aion.png', connor: '/avatar-connor.png' },
    crName: actor => actor === 'connor' ? 'Second AI' : 'Primary AI',
    crMsgSenderLineHtml: actor => `<div class="sender-line"><span>${actor}</span></div>`,
    document: {
      createElement: () => ({
        className: '',
        id: '',
        innerHTML: '',
        querySelector: () => ({}),
      }),
    },
    esc: value => String(value),
    messagesEl,
    scrollToBottom: () => {},
  };
  vm.createContext(context);
  vm.runInContext(`${rendererSource}\nstartStreamingBubble('${sender}', 'stream-1');`, context);
  return appended?.innerHTML || '';
}

test('markdown renders expressive text without executing raw HTML or remote images', () => {
  const markdown = require('./static/chatroom-markdown.js');
  const html = markdown.render([
    '# 大字报',
    '',
    '**粗体**、*斜体*和~~啦啦啦~~',
    '',
    '> 先喝几口水。',
    '',
    '- 第一件事',
    '- 第二件事',
    '',
    '<script>alert(1)</script>',
    '![追踪图](https://tracker.example/pixel.png)',
    '[危险链接](javascript:alert(1))',
  ].join('\n'));

  assert.match(html, /<h1>大字报<\/h1>/);
  assert.match(html, /<strong>粗体<\/strong>/);
  assert.match(html, /<em>斜体<\/em>/);
  assert.match(html, /~~啦啦啦~~/);
  assert.doesNotMatch(html, /<s>/);
  assert.match(html, /<blockquote>/);
  assert.match(html, /<ul>/);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script|<img|javascript:/i);
  assert.match(html, /追踪图/);
  assert.match(html, /危险链接/);
});

test('user markdown gives plain lines and a heading separate bubbles under one avatar', () => {
  const html = loadMessageRenderer()({
    id: 'user-1', sender: 'user', created_at: 1, attachments: [],
    content: '先听我说。\n# 我的大字报\n**今天也要开心。**',
  });

  assert.equal((html.match(/class="avatar"/g) || []).length, 1);
  assert.equal((html.match(/class="bubble markdown-body"/g) || []).length, 3);
  assert.match(html, /<p>先听我说。<\/p>/);
  assert.match(html, /<h1>我的大字报<\/h1>/);
  assert.match(html, /<strong>今天也要开心。<\/strong>/);
});

test('AI bold closes after Chinese punctuation before the next sentence', () => {
  const html = loadMessageRenderer()({
    id: 'ai-cjk-bold', sender: 'connor', created_at: 1, attachments: [],
    content: '看见了。**那不是“脾气好”，是把所有关系责任都打包退回给她。**什么都依着、不主动找。',
  });
  assert.match(html, /看见了。<strong>那不是“脾气好”，是把所有关系责任都打包退回给她。<\/strong>什么都依着/);
});

test('Chinese bold supports adjacent quotes and nested inline formatting', () => {
  const markdown = require('./static/chatroom-markdown.js');
  assert.equal(markdown.render('他说**“先喝水。”**然后起身。'), '<p>他说<strong>“先喝水。”</strong>然后起身。</p>\n');
  assert.equal(markdown.render('**先*喝水*。**然后**起身。**走走。'), '<p><strong>先<em>喝水</em>。</strong>然后<strong>起身。</strong>走走。</p>\n');
});

test('Chinese bold compatibility preserves literal code, escapes, and unmatched markers', () => {
  const markdown = require('./static/chatroom-markdown.js');
  const literal = '**先喝水。**然后起身。';
  assert.equal(markdown.render('`' + literal + '`'), '<p><code>' + literal + '</code></p>\n');
  assert.equal(markdown.render('```\n' + literal + '\n```'), '<pre><code>' + literal + '\n</code></pre>\n');
  assert.equal(markdown.render(String.raw`\*\*先喝水。\*\*然后起身。`), '<p>' + literal + '</p>\n');
  assert.equal(markdown.render('先喝水。**然后起身。'), '<p>先喝水。**然后起身。</p>\n');
});

test('user bubble splitting keeps markdown lists, quotes, and fenced code intact', () => {
  const markdown = require('./static/chatroom-markdown.js');
  const parts = markdown.splitUserBubbleParts([
    '开场。',
    '- 第一项',
    '- 第二项',
    '> 第一行引用',
    '> 第二行引用',
    '```js',
    'const answer = 42;',
    '```',
  ].join('\n'));

  assert.deepEqual(parts, [
    '开场。',
    '- 第一项\n- 第二项',
    '> 第一行引用\n> 第二行引用',
    '```js\nconst answer = 42;\n```',
  ]);
});

test('user identity header sits above the right-aligned bubble', () => {
  const html = loadMessageRenderer()({
    id: 'user-layout-1', sender: 'user', created_at: 1, attachments: [],
    content: '气泡贴着右侧安全线。',
  });

  const unit = html.indexOf('class="message-unit user user-message-unit"');
  const header = html.indexOf('class="message-header user-message-header"');
  const avatar = html.indexOf('class="avatar"');
  const content = html.indexOf('class="unit-content"');
  const bubble = html.indexOf('class="bubble markdown-body"');

  assert.ok(unit >= 0, 'user message should expose its right-aligned layout');
  assert.ok(unit < header && header < avatar && avatar < content, 'avatar should sit in the user header above the content');
  assert.ok(content < bubble, 'user bubble should begin below the identity header');
});

test('AI markdown is one wide text flow without a bubble or repeated avatar', () => {
  const html = loadMessageRenderer()({
    id: 'ai-1', sender: 'aion', created_at: 1, attachments: [],
    content: '## 醒得正好。\n\n先喝水，再慢慢起身。\n\n*别着急。*',
  });

  assert.equal((html.match(/class="avatar"/g) || []).length, 1);
  assert.doesNotMatch(html, /class="bubble/);
  assert.equal((html.match(/class="ai-message-content markdown-body"/g) || []).length, 1);
  assert.match(html, /<h2>醒得正好。<\/h2>/);
  assert.match(html, /<em>别着急。<\/em>/);
});

test('AI identity header sits above the full-width reply body', () => {
  const html = loadMessageRenderer()({
    id: 'ai-layout-1', sender: 'connor', created_at: 1, attachments: [],
    content: '正文从头像下方顶格开始。',
  });

  const headerStart = html.indexOf('class="message-header"');
  const avatar = html.indexOf('class="avatar"');
  const senderLine = html.indexOf('class="sender-line"');
  const headerEnd = html.indexOf('class="unit-content"');
  const body = html.indexOf('class="message-text-flow connor"');

  assert.ok(headerStart >= 0, 'AI message should expose a dedicated header row');
  assert.ok(headerStart < avatar && avatar < headerEnd, 'avatar should stay inside the header row');
  assert.ok(headerStart < senderLine && senderLine < headerEnd, 'name and actions should stay inside the header row');
  assert.ok(headerEnd < body, 'reply body should begin after the header row');
});

test('streaming AI reply uses the same identity header above its live body', () => {
  const html = renderStreamingMessage('aion');

  assert.match(html, /class="message-unit aion ai-message-unit"/);
  assert.match(html, /class="message-header"/);
  assert.ok(html.indexOf('class="message-header"') < html.indexOf('class="message-live-content'));
});

test('AI single line breaks stay within a paragraph and blank lines start another', () => {
  const html = loadMessageRenderer()({
    id: 'ai-2', sender: 'connor', created_at: 1, attachments: [],
    content: '第一行。\n第二行。\n\n第二段。',
  });

  assert.equal((html.match(/class="avatar"/g) || []).length, 1);
  assert.equal((html.match(/class="ai-message-content markdown-body"/g) || []).length, 1);
  assert.equal((html.match(/<p>/g) || []).length, 2);
  assert.match(html, /第一行。<br>\n<span class="line-indent" aria-hidden="true"><\/span>第二行。/);
});

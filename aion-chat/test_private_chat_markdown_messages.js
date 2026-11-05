'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const ROOT = __dirname;

test('private AI preserves single versus blank line spacing, including after a heading', () => {
  const html = loadPrivateMessageRenderer()({
    id: 'line-breaks', role: 'assistant', created_at: 1, attachments: [],
    content: '# 标题\n第一行。\n**第二行。**\n\n第二段。',
  });
  assert.equal((html.match(/<p>/g) || []).length, 2);
  assert.match(html, /第一行。<br>\n<span class="line-indent" aria-hidden="true"><\/span><strong>第二行。<\/strong>/);
});

function functionBlock(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.notEqual(start, -1, `missing ${startMarker}`);
  assert.notEqual(end, -1, `missing ${endMarker}`);
  return source.slice(start, end);
}

function loadPrivateMessageRenderer() {
  const source = fs.readFileSync(path.join(ROOT, 'static', 'chat.js'), 'utf8');
  const markdown = require('./static/chatroom-markdown.js');
  const rendererSource = functionBlock(
    source,
    'const PRIVATE_MESSAGE_SPECIAL_TOKEN_RE',
    'function renderMessages',
  );
  const context = {
    document: {readyState:'loading',addEventListener() {}},
    _memoryRecordMsgIds: new Set(),
    escHtml: markdown.escapeHtml,
    fmtTime: () => '16:42',
    formatMsg: markdown.escapeHtml,
    renderAttachments: () => '',
    renderBandVibrationNote: () => '',
    stripWishFulfillmentMarker: value => value,
    withWishFallbackAttachments: message => message.attachments || [],
    worldBook: { user_name: 'Test User', ai_name: 'Primary AI' },
    window: { ChatroomMarkdown: markdown, SystemNoticeUI: require('./static/system-notice-ui.js') },
  };
  context.window.AionPat = require('./static/pat.js');
  context.window.addEventListener = () => {};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(ROOT,'static','kitchen-cards.js'),'utf8'),context);
  vm.runInContext(`${rendererSource}\nthis.renderPrivateMessage = renderPrivateMessageHTML;`, context);
  return context.renderPrivateMessage;
}

test('private fridge lookup survives history rendering and displays pending or failed state truthfully', () => {
  const render = loadPrivateMessageRenderer(), before = '我去翻冰箱😊。';
  const message = {id:'fridge',role:'assistant',content:before+'\n\n库存看好啦。',attachments:[
    {type:'kitchen_lookup',actor:'aion',actor_name:'旧名字',offset:before.length,before,status:'done'},
  ]};
  const html = render(message);
  assert.ok(html.indexOf(before)<html.indexOf('class="kc-lookup"'));
  assert.ok(html.indexOf('class="kc-lookup"')<html.indexOf('库存看好啦'));
  assert.match(html,/「Primary AI」查看了冰箱库存/);
  for (const [status,text] of [['pending','正在查看冰箱'],['failed','暂时没能查看冰箱库存']]) {
    const notice = render({...message,content:'',attachments:[{...message.attachments[0],offset:0,before:'',status}]});
    assert.ok(notice.includes(text));
    assert.doesNotMatch(notice,/」查看了冰箱库存/);
  }
});

test('private lookup also keeps the continuation intact after paragraph normalization', () => {
  const before = '前面的话😊。\n\n\n\n先去翻一下冰箱😼。';
  const html = loadPrivateMessageRenderer()({
    id:'compressed-fridge',role:'assistant',
    content:before.replace(/\n+/g,'\n')+'\n家底看完了，嚯。',
    attachments:[{type:'kitchen_lookup',actor:'aion',offset:before.length,before,status:'done'}],
  });
  assert.ok(html.indexOf('class="kc-lookup"')<html.indexOf('家底看完了'));
  assert.match(html,/家底看完了，嚯。/);
});

test('old leaked AI toy commands become display-only folded notices, not ordinary chat text', () => {
  const render = loadPrivateMessageRenderer();
  const content = '[SVAKOM:LOOP:4,1,2,2,0]查岗正文';
  const message = {id:'old-toy',role:'assistant',created_at:1,attachments:[],content};
  const html = render(message);
  assert.doesNotMatch(html, /SVAKOM:LOOP/);
  assert.match(html, /<details class="system-notice-details">/);
  assert.match(html, /查岗正文/);
  assert.match(html, /SVAKOM:循环:4秒,慢速旋转伸缩/);
  assert.equal(message.content, content);
  assert.match(render({...message,role:'user'}), /SVAKOM:LOOP/, 'user discussion of syntax is not a command');
});

test('private AI markdown renders without a bubble below one identity header', () => {
  const html = loadPrivateMessageRenderer()({
    id: 'private-ai-1', role: 'assistant', created_at: 1, attachments: [],
    content: '## 醒得正好。\n\n**先喝水。**\n\n*慢慢起身。*',
  });

  assert.equal((html.match(/class="msg-avatar"/g) || []).length, 1);
  assert.doesNotMatch(html, /class="msg-bubble/);
  assert.match(html, /class="private-message-header"/);
  assert.match(html, /class="private-ai-message-content markdown-body"/);
  assert.match(html, /<h2>醒得正好。<\/h2>/);
  assert.match(html, /<strong>先喝水。<\/strong>/);
  assert.ok(html.indexOf('class="private-message-header"') < html.indexOf('class="private-ai-message-content'));
});

test('private user lines and heading become separate right-aligned bubbles below one header', () => {
  const html = loadPrivateMessageRenderer()({
    id: 'private-user-1', role: 'user', created_at: 1, attachments: [],
    content: '先听我说。\n# 我的大字报\n今天也开心。',
  });

  assert.equal((html.match(/class="msg-avatar"/g) || []).length, 1);
  assert.equal((html.match(/class="msg-bubble markdown-body"/g) || []).length, 3);
  assert.match(html, /class="private-message-unit user-message-unit"/);
  assert.match(html, /class="private-message-header user-message-header"/);
  assert.match(html, /<p>先听我说。<\/p>/);
  assert.match(html, /<h1>我的大字报<\/h1>/);
  assert.ok(html.indexOf('class="private-message-header') < html.indexOf('class="msg-bubble markdown-body'));
});

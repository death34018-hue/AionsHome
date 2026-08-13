const assert = require('assert');
const fs = require('fs');
const path = require('path');

const root = __dirname;
const chatroom = fs.readFileSync(path.join(root, 'static', 'chatroom.js'), 'utf8');
const chat = fs.readFileSync(path.join(root, 'static', 'chat.js'), 'utf8');

assert.match(
  chatroom,
  /hasLoungeReportAtt[\s\S]{0,900}crBubbleUnitHtml\(\{[\s\S]{0,500}showHeader:\s*true[\s\S]{0,300}deleteOnly:\s*true[\s\S]{0,300}preBubbleHtml:\s*attHtml/,
  'group lounge reports should use the standard avatar/name/menu message unit',
);
assert.ok(
  chatroom.includes('renderAttachments(messageAttachments, { actorName: name })'),
  'group report title should receive the configured sender name',
);
assert.match(
  chatroom,
  /actorName.*刚刚去.*partner.*那里串门回来了。/s,
  'outbound group report title should identify who visited which friend',
);
assert.match(
  chatroom,
  /actorName.*刚刚接待了访客.*partner/,
  'inbound group report title should identify host and visitor',
);
assert.match(
  chat,
  /actorName.*刚刚去.*partner.*那里串门回来了。/s,
  'private report title should identify who visited which friend',
);
assert.ok(
  !chatroom.includes('<strong>${partner}</strong>')
    && !chat.includes('<strong>${partner}</strong>'),
  'report cards should not repeat the friend name as a bold standalone line',
);
assert.ok(
  chatroom.includes('<small>共聊了 ${turns} 回合</small>')
    && chat.includes('<small>共聊了 ${turns} 回合</small>'),
  'report cards should describe the total turn count clearly',
);

const chatroomCss = fs.readFileSync(path.join(root, 'static', 'chatroom.css'), 'utf8');
const chatCss = fs.readFileSync(path.join(root, 'static', 'chat.css'), 'utf8');
assert.match(
  chatroomCss,
  /\.msg-media:has\(\.lounge-report-card\)\s*\{[^}]*margin-left:\s*0/s,
  'group lounge cards should cancel the generic media indent',
);
assert.match(chatroomCss, /\.lounge-report-card small\s*\{[^}]*font-size:\s*10px/s);
assert.match(chatCss, /\.lounge-report-card small\s*\{[^}]*font-size:\s*10px/s);

console.log('lounge report card UI contract passed');

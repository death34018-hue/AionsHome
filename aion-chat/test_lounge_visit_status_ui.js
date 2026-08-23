const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const ROOT = __dirname;
const ui = require('./static/lounge-visit-ui.js');

test('temporary visit attachment is recognized as a small status message', () => {
  const message = {
    role: 'system',
    attachments: [
      { type: 'lounge_visit_status', state: 'active', status_id: 'status-1' },
      { type: 'system_notice_order', after_msg_id: 'reply-1' },
    ],
  };

  assert.equal(ui.isStatusMessage(message), true);
  assert.equal(ui.isStatusMessage({ role: 'system', attachments: [] }), false);
});

test('outbound report copy distinguishes completion interruption and rejection', () => {
  assert.equal(
    ui.reportTitle('Connor', 'YUI 的朋友', 'outbound', 'completed'),
    'Connor 刚刚去 YUI 的朋友那里串门回来了。',
  );
  assert.equal(
    ui.reportTitle('Connor', 'YUI 的朋友', 'outbound', 'interrupted'),
    'Connor 去 YUI 的朋友那里串门时中断了。',
  );
  assert.equal(
    ui.reportTitle('Connor', 'YUI 的朋友', 'outbound', 'rejected'),
    'Connor 这次没能前往拜访 YUI 的朋友。',
  );
  assert.equal(ui.reportMeta('completed', 3), '共聊了 3 回合');
  assert.equal(
    ui.reportMeta('interrupted', 2, 'request_timeout'),
    '中断前聊了 2 回合 · 连接会客室超时，本次会面已结束。',
  );
  assert.equal(
    ui.reportMeta('rejected', 0, 'lounge_closed'),
    '未能开始拜访 · 对方会客室已关闭。',
  );
});

test('private and chatroom pages wire the shared status renderer and subdued class', () => {
  for (const surface of ['chat', 'chatroom']) {
    const html = fs.readFileSync(path.join(ROOT, 'static', `${surface}.html`), 'utf8');
    const script = fs.readFileSync(path.join(ROOT, 'static', `${surface}.js`), 'utf8');
    const css = fs.readFileSync(path.join(ROOT, 'static', `${surface}.css`), 'utf8');
    assert.match(html, /lounge-visit-ui\.js/);
    assert.match(script, /LoungeVisitUI\.isStatusMessage/);
    assert.match(script, /lounge-visit-status-line/);
    assert.match(script, /LoungeVisitUI\.reportTitle/);
    assert.match(script, /LoungeVisitUI\.reportMeta/);
    assert.match(css, /\.lounge-visit-status-line/);
  }
});

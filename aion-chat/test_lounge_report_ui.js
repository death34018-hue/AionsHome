const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');


const ROOT = __dirname;


test('report metadata includes the Chinese interruption reason', () => {
  const ui = require(path.join(ROOT, 'static', 'lounge-visit-ui.js'));

  assert.equal(
    ui.reportMeta('interrupted', 1, 'network_reconnect_failed'),
    '中断前聊了 1 回合 · 网络连接中断，自动重连后仍未恢复。',
  );
});


test('private and chatroom surfaces render lounge visit report attachments as cards', () => {
  for (const name of ['chat', 'chatroom']) {
    const script = fs.readFileSync(path.join(ROOT, 'static', `${name}.js`), 'utf8');
    const css = fs.readFileSync(path.join(ROOT, 'static', `${name}.css`), 'utf8');

    assert.match(script, /lounge_visit_report/);
    assert.match(script, /lounge-report-card/);
    assert.match(script, /partner_name/);
    assert.match(css, /\.lounge-report-card/);
    assert.match(css, /var\(--surface/);
    assert.match(css, /body\[data-theme=["']light["']\][^{]*\.lounge-report-card/);
  }
});

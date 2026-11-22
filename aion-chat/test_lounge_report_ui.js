const assert = require('node:assert/strict');
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

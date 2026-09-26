const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

test('Home shows one reminder for new board posts and hides it after the board is read', async () => {
  let hasNew = false;
  let opened = '';
  let poll;
  const handlers = {};
  const notice = {
    hidden: true,
    addEventListener(type, handler) { handlers[type] = handler; },
  };
  const context = {
    document: {hidden: false, getElementById: () => notice, addEventListener() {}},
    window: {addEventListener() {}},
    fetch: async () => ({ok: true, json: async () => ({has_new: hasNew})}),
    setInterval(handler) { poll = handler; },
    openApp(url) { opened = url; },
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/home_board_notice.js'), 'utf8'), context);
  await new Promise(setImmediate);
  assert.equal(notice.hidden, true);

  hasNew = true;
  await poll();
  assert.equal(notice.hidden, false);
  handlers.click();
  assert.equal(opened, '/lounge-board');

  hasNew = false;
  await poll();
  assert.equal(notice.hidden, true);
});

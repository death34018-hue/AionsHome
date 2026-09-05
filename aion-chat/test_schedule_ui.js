'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ui = require('./static/schedule-ui.js');

const tests = [];

function test(name, fn) {
  tests.push({ name, fn });
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

test('all existing schedule types keep their visible labels', () => {
  assert.deepEqual(ui.scheduleTypePresentation('alarm'), {
    icon: '🔔',
    label: '闹铃',
    className: 'alarm',
  });
  assert.deepEqual(ui.scheduleTypePresentation('reminder'), {
    icon: '📋',
    label: '日程',
    className: 'reminder',
  });
  assert.deepEqual(ui.scheduleTypePresentation('monitor'), {
    icon: '👁',
    label: '监督',
    className: 'monitor',
  });
});

test('active row displays configured creator and keeps its delete action', () => {
  const html = ui.scheduleItemHtml(
    {
      id: 'active-1',
      type: 'alarm',
      trigger_at: '2026-07-27T08:00',
      content: '<wake up>',
      origin_name: 'Configured Main',
      status: 'active',
    },
    { history: false, escapeHtml },
  );

  assert.match(html, /【Configured Main】/);
  assert.match(html, /&lt;wake up&gt;/);
  assert.match(html, /2026-07-27 08:00/);
  assert.match(html, /deleteSchedule\(/);
  assert.match(html, /active-1/);
  assert.match(html, /aria-label="删除日程：&lt;wake up&gt;"/);
  assert.doesNotMatch(html, /已完成|已取消/);
});

test('triggered history row is completed and has no delete action', () => {
  const html = ui.scheduleItemHtml(
    {
      id: 'done-1',
      type: 'reminder',
      trigger_at: '2026-07-26 09:00',
      content: 'plan',
      origin_name: 'Configured User',
      status: 'triggered',
    },
    { history: true, escapeHtml },
  );

  assert.match(html, /【Configured User】/);
  assert.match(html, /已完成/);
  assert.doesNotMatch(html, /deleteSchedule|sch-del-btn/);
});

test('cancelled history row is cancelled and preserves monitor type', () => {
  const html = ui.scheduleItemHtml(
    {
      id: 'stopped-1',
      type: 'monitor',
      trigger_at: '2026-07-26 10:00',
      content: 'check',
      origin_name: 'Configured Companion',
      status: 'cancelled',
    },
    { history: true, escapeHtml },
  );

  assert.match(html, /监督/);
  assert.match(html, /已取消/);
  assert.doesNotMatch(html, /deleteSchedule|sch-del-btn/);
});

test('default renderer escapes untrusted record fields without a caller helper', () => {
  const html = ui.scheduleItemHtml(
    {
      id: 'unsafe',
      type: 'alarm',
      trigger_at: '<time>',
      content: '<img src=x onerror=alert(1)>',
      origin_name: '<script>alert(2)</script>',
      status: 'cancelled<script>',
    },
    { history: true },
  );

  assert.doesNotMatch(html, /<img|<script>/);
  assert.match(html, /&lt;img/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /&lt;time&gt;/);
});

test('schedule data loader requests active and history together', async () => {
  const calls = [];
  const result = await ui.loadScheduleLists(async (method, path) => {
    calls.push([method, path]);
    return path.endsWith('active') ? [{ id: 'active' }] : [{ id: 'history' }];
  });

  assert.deepEqual(calls, [
    ['GET', '/api/schedules?status=active'],
    ['GET', '/api/schedules?status=history'],
  ]);
  assert.deepEqual(result, {
    active: [{ id: 'active' }],
    history: [{ id: 'history' }],
    errors: [],
  });
});

test('only schedule change messages request both-list refresh', () => {
  assert.equal(ui.shouldReloadSchedules({ type: 'schedule_changed' }), true);
  assert.equal(ui.shouldReloadSchedules({ type: 'message_created' }), false);
  assert.equal(ui.shouldReloadSchedules(null), false);
});

test('memo rows escape content and expose the correct status action', () => {
  const active = ui.privateMemoItemHtml({
    id: "memo'1",
    content: '<img src=x onerror=alert(1)>',
    status: 'active',
  });
  const completed = ui.privateMemoItemHtml({
    id: 'memo-2', content: '完成内容', status: 'completed',
  });

  assert.doesNotMatch(active, /<img/);
  assert.match(active, /&lt;img/);
  assert.match(active, /completePrivateMemo/);
  assert.match(active, /editPrivateMemo/);
  assert.match(completed, /restorePrivateMemo/);
  assert.doesNotMatch(completed, /editPrivateMemo/);
});

test('memo loader requests active and completed lists together', async () => {
  const calls = [];
  const result = await ui.loadPrivateMemoLists(async (method, path) => {
    calls.push([method, path]);
    return path.endsWith('active') ? [{ id: 'a' }] : [{ id: 'c' }];
  });
  assert.deepEqual(calls, [
    ['GET', '/api/private-memos?status=active'],
    ['GET', '/api/private-memos?status=completed'],
  ]);
  assert.equal(result.active.length, 1);
  assert.equal(result.completed.length, 1);
});

test('private memo change messages request a memo refresh', () => {
  assert.equal(ui.shouldReloadPrivateMemos({ type: 'private_memos_changed' }), true);
  assert.equal(ui.shouldReloadPrivateMemos({ type: 'schedule_changed' }), false);
});

function createTabDocument() {
  const elements = {};
  for (const id of [
    'schTabActive',
    'schTabMemo',
    'schPanelActive',
    'schPanelHistory',
    'schPanelMemo',
    'schTabs',
    'schPageTitle',
    'schBackButton',
    'schHistoryButton',
  ]) {
    const classes = new Set();
    elements[id] = {
      attributes: {},
      focusCalls: 0,
      classList: {
        contains(value) {
          return classes.has(value);
        },
        toggle(value, enabled) {
          if (enabled) classes.add(value);
          else classes.delete(value);
        },
      },
      hidden: false,
      focus() {
        this.focusCalls += 1;
      },
      setAttribute(name, value) {
        this.attributes[name] = String(value);
      },
      textContent: '',
    };
  }
  return {
    elements,
    document: {
      getElementById(id) {
        return elements[id] || null;
      },
    },
  };
}

test('unknown tab defaults to current with matching accessible state', () => {
  const fixture = createTabDocument();
  const selected = ui.selectScheduleTab('unknown', fixture.document);

  assert.equal(selected, 'active');
  assert.equal(fixture.elements.schPanelActive.hidden, false);
  assert.equal(fixture.elements.schPanelHistory.hidden, true);
  assert.equal(
    fixture.elements.schTabActive.attributes['aria-selected'],
    'true',
  );
  assert.equal(
    fixture.elements.schTabMemo.attributes['aria-selected'],
    'false',
  );
  assert.equal(fixture.elements.schTabActive.attributes.tabindex, '0');
  assert.equal(fixture.elements.schTabMemo.attributes.tabindex, '-1');
  assert.equal(fixture.elements.schTabActive.classList.contains('active'), true);
});

test('memo tab gets its own exclusive panel', () => {
  const fixture = createTabDocument();
  const selected = ui.selectScheduleTab('memo', fixture.document);

  assert.equal(selected, 'memo');
  assert.equal(fixture.elements.schPanelActive.hidden, true);
  assert.equal(fixture.elements.schPanelMemo.hidden, false);
  assert.equal(
    fixture.elements.schTabMemo.attributes['aria-selected'],
    'true',
  );
  assert.equal(fixture.elements.schTabMemo.classList.contains('active'), true);
  assert.equal(
    fixture.elements.schPanelMemo.classList.contains('active'),
    true,
  );
  assert.equal(
    fixture.elements.schPanelActive.classList.contains('active'),
    false,
  );
  assert.equal(fixture.elements.schTabActive.attributes.tabindex, '-1');
  assert.equal(fixture.elements.schTabMemo.attributes.tabindex, '0');
});

test('tab keyboard navigation selects and focuses the expected tab', () => {
  const fixture = createTabDocument();
  let prevented = 0;
  const event = {
    key: 'ArrowRight',
    preventDefault() {
      prevented += 1;
    },
  };

  assert.equal(
    ui.handleScheduleTabKeydown(event, 'active', fixture.document),
    'memo',
  );
  assert.equal(prevented, 1);
  assert.equal(fixture.elements.schPanelMemo.hidden, false);
  assert.equal(fixture.elements.schTabMemo.focusCalls, 1);

  event.key = 'Home';
  assert.equal(
    ui.handleScheduleTabKeydown(event, 'memo', fixture.document),
    'active',
  );
  assert.equal(fixture.elements.schTabActive.focusCalls, 1);

  event.key = 'Enter';
  assert.equal(
    ui.handleScheduleTabKeydown(event, 'active', fixture.document),
    null,
  );
});

test('history is a separate view and returning preserves the active panel', () => {
  const fixture = createTabDocument();
  ui.selectScheduleTab('history', fixture.document);
  assert.equal(fixture.elements.schTabs.hidden, true);
  assert.equal(fixture.elements.schHistoryButton.hidden, true);
  assert.equal(fixture.elements.schPanelHistory.hidden, false);
  assert.equal(fixture.elements.schPanelActive.hidden, true);
  assert.equal(fixture.elements.schPanelMemo.hidden, true);
  assert.equal(fixture.elements.schBackButton.attributes['aria-label'], '返回日程管理');
  ui.selectScheduleTab('active', fixture.document);
  assert.equal(fixture.elements.schTabs.hidden, false);
  assert.equal(fixture.elements.schHistoryButton.hidden, false);
  assert.equal(fixture.elements.schPanelHistory.hidden, true);
  assert.equal(fixture.elements.schPanelActive.hidden, false);
});

test('embedded schedule keeps its own back action after common navigation initializes', () => {
  const common = fs.readFileSync(`${__dirname}/static/common.js`, 'utf8');
  const html = fs.readFileSync(`${__dirname}/static/schedule.html`, 'utf8');
  const callbacks = {};
  const fixture = createTabDocument();
  const button = fixture.elements.schBackButton;
  const markup = html.match(/<button[^>]*id="schBackButton"[^>]*>/)[0];
  button.hasAttribute = name => markup.includes(name);
  const context = {
    window: { parent: { openSubPage: () => { throw new Error('Unexpected exit to home'); } }, addEventListener: (name, fn) => { callbacks[name] = fn; } },
    document: { querySelector: () => button, addEventListener: (name, fn) => { callbacks[name] = fn; } },
    location: { hash: '', pathname: '/static/schedule.html', search: '' },
    history: {
      state: null,
      pushState(state, title, hash) { this.state = state; context.location.hash = hash; },
      back() { context.location.hash = ''; callbacks.popstate(); },
    },
    $: id => fixture.elements[id],
    switchScheduleTab: tab => ui.selectScheduleTab(tab, fixture.document),
  };
  vm.createContext(context);
  vm.runInContext(common.slice(common.indexOf('function getSubPageReturnUrl'), common.indexOf('async function api')), context);
  vm.runInContext(html.slice(html.indexOf('function openScheduleHistory'), html.indexOf('function renderScheduleList')), context);
  button.onclick = context.leaveScheduleView;
  callbacks.DOMContentLoaded();
  context.openScheduleHistory();
  button.onclick();
  assert.equal(fixture.elements.schPanelActive.hidden, false);
  assert.equal(fixture.elements.schPanelHistory.hidden, true);
  button.hasAttribute = () => false;
  callbacks.DOMContentLoaded();
  assert.equal(button.onclick, context.navigateSubPageBack, 'ordinary subpages retain shared navigation');
});

test('typed memo saves trimmed content and preserves input when saving fails', async () => {
  const html = fs.readFileSync(`${__dirname}/static/schedule.html`, 'utf8');
  const elements = { privateMemoInput: { value: '  记一件小事  ' }, privateMemoAddButton: { disabled: false }, privateMemoList: { parentElement: { scrollTop: 60 } } };
  const calls = [];
  const context = {
    $: id => elements[id],
    api: async (...args) => calls.push(args),
    notifyNativeMemoChanged: () => calls.push('widget'),
    loadPrivateMemos: async () => calls.push('reload'),
    showToast: () => {}, alert: () => {},
  };
  vm.createContext(context);
  vm.runInContext(html.slice(html.indexOf('async function addPrivateMemoManual'), html.indexOf('async function completePrivateMemo')), context);
  await context.addPrivateMemoManual();
  assert.equal(calls[0][0], 'POST');
  assert.equal(calls[0][1], '/api/private-memos');
  assert.equal(calls[0][2].content, '记一件小事');
  assert.deepEqual(calls.slice(1), ['widget', 'reload']);
  assert.equal(elements.privateMemoInput.value, '');
  elements.privateMemoInput.value = '保留草稿';
  context.api = async () => { throw new Error('offline'); };
  await context.addPrivateMemoManual();
  assert.equal(elements.privateMemoInput.value, '保留草稿');
  assert.equal(elements.privateMemoAddButton.disabled, false);
});

test('schedule panels constrain long lists to internal scrolling', () => {
  const html = fs.readFileSync(
    `${__dirname}/static/schedule.html`,
    'utf8',
  );

  assert.match(
    html,
    /\.schedule-page\s*\{[^}]*min-height:\s*0[^}]*overflow:\s*hidden/s,
  );
  assert.match(
    html,
    /\.sch-tab-panel\.active\s*\{[^}]*display:\s*flex[^}]*min-height:\s*0/s,
  );
  assert.match(
    html,
    /\.sch-list\s*\{[^}]*min-height:\s*0[^}]*overflow-y:\s*auto/s,
  );
  assert.match(
    html,
    /\.sch-add-section\s*\{[^}]*flex-shrink:\s*0/s,
  );
  assert.doesNotMatch(html, /max-height:\s*calc\(100dvh/);
});

test('page keeps browser zoom available for compact history text', () => {
  const html = fs.readFileSync(
    `${__dirname}/static/schedule.html`,
    'utf8',
  );

  assert.match(
    html,
    /<meta name="viewport" content="width=device-width, initial-scale=1\.0">/,
  );
  assert.doesNotMatch(html, /user-scalable\s*=\s*no|maximum-scale\s*=\s*1/i);
});

test('phone layout gives the add button its own large full-width row', () => {
  const html = fs.readFileSync(
    `${__dirname}/static/schedule.html`,
    'utf8',
  );
  const mobileStart = html.indexOf('@media (max-width:420px)');
  const reducedMotionStart = html.indexOf(
    '@media (prefers-reduced-motion:reduce)',
  );
  assert.notEqual(mobileStart, -1);
  assert.notEqual(reducedMotionStart, -1);
  const mobileCss = html.slice(mobileStart, reducedMotionStart);

  assert.match(
    mobileCss,
    /\.sch-add-row2 button\s*\{[^}]*flex-basis:\s*100%[^}]*width:\s*100%[^}]*min-height:\s*44px[^}]*font-size:\s*13px/s,
  );
  assert.match(
    html,
    /\.sch-add-row2 button\s*\{[^}]*background:\s*var\(--sch-add-bg\)[^}]*color:\s*var\(--sch-add-fg\)/s,
  );
  assert.match(html, /--sch-add-bg:\s*#f47a33/);
});

(async function run() {
  let failures = 0;
  for (const { name, fn } of tests) {
    try {
      await fn();
      console.log(`ok - ${name}`);
    } catch (error) {
      failures += 1;
      console.error(`not ok - ${name}`);
      console.error(error.stack || error);
    }
  }
  if (failures) {
    throw new Error(`ScheduleUI: ${failures} test(s) failed`);
  }
  console.log(`ScheduleUI: ${tests.length} tests passed`);
}());

'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
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

function createTabDocument() {
  const elements = {};
  for (const id of [
    'schTabActive',
    'schTabHistory',
    'schPanelActive',
    'schPanelHistory',
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
    fixture.elements.schTabHistory.attributes['aria-selected'],
    'false',
  );
  assert.equal(fixture.elements.schTabActive.attributes.tabindex, '0');
  assert.equal(fixture.elements.schTabHistory.attributes.tabindex, '-1');
  assert.equal(fixture.elements.schTabActive.classList.contains('active'), true);
});

test('history tab hides current panel and exposes history panel', () => {
  const fixture = createTabDocument();
  const selected = ui.selectScheduleTab('history', fixture.document);

  assert.equal(selected, 'history');
  assert.equal(fixture.elements.schPanelActive.hidden, true);
  assert.equal(fixture.elements.schPanelHistory.hidden, false);
  assert.equal(
    fixture.elements.schTabHistory.attributes['aria-selected'],
    'true',
  );
  assert.equal(fixture.elements.schTabHistory.classList.contains('active'), true);
  assert.equal(
    fixture.elements.schPanelHistory.classList.contains('active'),
    true,
  );
  assert.equal(
    fixture.elements.schPanelActive.classList.contains('active'),
    false,
  );
  assert.equal(fixture.elements.schTabActive.attributes.tabindex, '-1');
  assert.equal(fixture.elements.schTabHistory.attributes.tabindex, '0');
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
    'history',
  );
  assert.equal(prevented, 1);
  assert.equal(fixture.elements.schPanelHistory.hidden, false);
  assert.equal(fixture.elements.schTabHistory.focusCalls, 1);

  event.key = 'Home';
  assert.equal(
    ui.handleScheduleTabKeydown(event, 'history', fixture.document),
    'active',
  );
  assert.equal(fixture.elements.schTabActive.focusCalls, 1);

  event.key = 'Enter';
  assert.equal(
    ui.handleScheduleTabKeydown(event, 'active', fixture.document),
    null,
  );
});

test('tab counts use non-negative whole record totals', () => {
  const fixture = createTabDocument();
  ui.updateScheduleTabCounts(5.9, -3, fixture.document);

  assert.equal(fixture.elements.schTabActive.textContent, '当前 5');
  assert.equal(fixture.elements.schTabHistory.textContent, '历史 0');
});

test('page separates current and history into exclusive tab panels', () => {
  const html = fs.readFileSync(
    `${__dirname}/static/schedule.html`,
    'utf8',
  );

  assert.match(html, /id="schPanelActive"[^>]*role="tabpanel"/);
  assert.match(html, /id="schPanelHistory"[^>]*role="tabpanel"[^>]*hidden/);
  assert.match(html, /id="schTabActive"[^>]*aria-selected="true"/);
  assert.match(html, /id="schTabHistory"[^>]*aria-selected="false"/);
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

test('history typography is compact and page colors stay theme-derived', () => {
  const html = fs.readFileSync(
    `${__dirname}/static/schedule.html`,
    'utf8',
  );

  assert.match(
    html,
    /\.sch-history-item \.sch-content\s*\{[^}]*font-size:\s*12px/s,
  );
  assert.match(
    html,
    /\.sch-history-item \.sch-origin\s*\{[^}]*font-size:\s*10\.5px/s,
  );
  assert.match(
    html,
    /\.sch-history-item \.sch-time\s*\{[^}]*font-size:\s*10px/s,
  );
  assert.doesNotMatch(html, /\.sch-type\.alarm\s*\{\s*background:\s*#/);
  assert.match(html, /color-mix\(in srgb,\s*var\(--accent\)/);
  assert.match(html, /--sch-muted:\s*#67584f/);
  assert.match(html, /body\[data-theme="dark"\]\s+\.schedule-page/);
  assert.match(html, /\.sch-history-item \.sch-time\s*\{[^}]*color:\s*var\(--sch-subtle\)/s);
  assert.doesNotMatch(html, /\.sch-add-row2 button\s*\{[^}]*color:\s*#fff/s);
  assert.match(
    html,
    /\.sch-del-btn\s*\{[^}]*color:\s*var\(--danger\)[^}]*opacity:\s*1/s,
  );
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
    /\.sch-add-row2 button\s*\{[^}]*flex-basis:\s*100%[^}]*width:\s*100%[^}]*min-height:\s*46px[^}]*font-size:\s*15px/s,
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

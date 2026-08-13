const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');


const ROOT = __dirname;
const SCRIPT_PATH = path.join(ROOT, 'static', 'lounge-friends.js');
const HTML_PATH = path.join(ROOT, 'static', 'lounge-friends.html');
const COMMON_PATH = path.join(ROOT, 'static', 'common.js');
const STYLE_PATH = path.join(ROOT, 'static', 'lounge-friends.css');


test('actor options use display names returned by the API', () => {
  const ui = require(SCRIPT_PATH);
  const select = {
    children: [],
    replaceChildren(...children) {
      this.children = children;
    },
  };
  const documentRef = {
    createElement(tagName) {
      return { tagName, value: '', textContent: '' };
    },
  };

  ui.renderActorOptions(select, [
    { id: 'actor-primary', display_name: 'Configured Primary' },
    { id: 'actor-companion', display_name: 'Configured Companion' },
  ], documentRef);

  assert.deepEqual(
    select.children.map(option => [option.value, option.textContent]),
    [
      ['actor-primary', 'Configured Primary'],
      ['actor-companion', 'Configured Companion'],
    ],
  );
});


test('successful save clears the password field without using localStorage', async () => {
  const ui = require(SCRIPT_PATH);
  const originalStorage = global.localStorage;
  global.localStorage = new Proxy({}, {
    get() {
      throw new Error('friend form must not access localStorage');
    },
  });
  const elements = {
    actorId: { value: 'actor-primary' },
    displayName: { value: 'Remote Friend' },
    loungeUrl: { value: 'https://friend.example/mcp' },
    visitorKey: { value: 'private-key' },
    relationshipNote: { value: '' },
    enabled: { checked: true },
    allowAutonomous: { checked: false },
    cooldownHours: { value: '12' },
    maxTurns: { value: '4' },
  };
  let sent;
  try {
    const result = await ui.saveFriend({
      friendId: null,
      elements,
      request: async (method, url, body) => {
        sent = { method, url, body };
        return { id: 'friend-id', has_key: true };
      },
    });

    assert.equal(sent.method, 'POST');
    assert.equal(sent.url, '/api/lounge-friends');
    assert.equal(sent.body.visitor_key, 'private-key');
    assert.equal(elements.visitorKey.value, '');
    assert.deepEqual(result, { id: 'friend-id', has_key: true });
  } finally {
    if (originalStorage === undefined) delete global.localStorage;
    else global.localStorage = originalStorage;
  }
});


test('immediate visit request allows the full coordinator lifecycle', async () => {
  const ui = require(SCRIPT_PATH);
  let sent;

  const result = await ui.requestImmediateVisit({
    friend: { id: 'friend-id', actor_id: 'actor-primary' },
    topic: 'ordinary topic',
    request: async (method, url, body, options) => {
      sent = { method, url, body, options };
      return { status: 'completed' };
    },
  });

  assert.deepEqual(sent, {
    method: 'POST',
    url: '/api/lounge-friends/friend-id/visit',
    body: { actor_id: 'actor-primary', topic: 'ordinary topic' },
    options: { timeoutMs: 610000 },
  });
  assert.deepEqual(result, { status: 'completed' });
});


test('visitor key is entered through a password control and common navigation links the page', () => {
  const html = fs.readFileSync(HTML_PATH, 'utf8');
  const common = fs.readFileSync(COMMON_PATH, 'utf8');

  assert.match(
    html,
    /<input[^>]+id=["']visitorKey["'][^>]+type=["']password["']/i,
  );
  assert.match(common, /\/lounge-friends/);
});


test('lounge page uses theme surfaces and provides its own vertical scroll area', () => {
  const css = fs.readFileSync(STYLE_PATH, 'utf8');

  assert.match(css, /--lounge-surface:\s*color-mix\([^;]*var\(--surface,/);
  assert.doesNotMatch(css, /var\(--card,/);
  assert.doesNotMatch(css, /var\(--text-secondary,/);
  assert.match(css, /\.lounge-content\s*\{[^}]*flex:\s*1;[^}]*min-height:\s*0;[^}]*overflow-y:\s*auto;/s);
});


test('visit history is a collapsible chat thread with friendly running labels', () => {
  const ui = require(SCRIPT_PATH);
  const html = fs.readFileSync(HTML_PATH, 'utf8');
  const css = fs.readFileSync(STYLE_PATH, 'utf8');
  const script = fs.readFileSync(SCRIPT_PATH, 'utf8');

  assert.equal(ui.visitStatusText({ status: 'running', turn_count: 0 }), '正在连接');
  assert.equal(ui.visitStatusText({ status: 'running', turn_count: 2 }), '进行中 · 2 回合');
  assert.equal(ui.nextExpandedVisitId('visit-1', 'visit-1'), null);
  assert.equal(ui.nextExpandedVisitId('visit-1', 'visit-2'), 'visit-2');
  assert.doesNotMatch(html, /id=["']visitDetail["']/);
  assert.match(script, /className = 'visit-thread'/);
  assert.match(script, /aria-expanded/);
  assert.match(script, /setTimeout\([^;]*3000/s);
  assert.match(css, /\.visit-message\.outbound/);
  assert.match(css, /\.visit-message-text/);
  assert.match(css, /font-size:\s*13px/);
});


test('single visit deletion is blocked while running and stays actor scoped', async () => {
  const ui = require(SCRIPT_PATH);
  const calls = [];

  assert.equal(ui.canDeleteVisit({ status: 'running' }), false);
  assert.equal(ui.canDeleteVisit({ status: 'completed' }), true);
  const deleted = await ui.requestDeleteVisit({
    visit: { id: 'visit-1', status: 'completed' },
    actorId: 'actor-primary',
    confirmDelete: () => true,
    request: async (method, url) => calls.push([method, url]),
  });

  assert.equal(deleted, true);
  assert.deepEqual(calls, [[
    'DELETE',
    '/api/lounge-visits/visit-1?actor_id=actor-primary',
  ]]);
});


test('same actor is busy only while its immediate visit request is pending', async () => {
  const ui = require(SCRIPT_PATH);
  const visitingActorIds = new Set();
  let release;
  const pendingOperation = new Promise(resolve => { release = resolve; });
  const operation = ui.withVisitingActor(
    visitingActorIds,
    'actor-primary',
    () => {},
    () => pendingOperation,
  );

  assert.deepEqual(ui.immediateVisitButtonState('actor-primary', visitingActorIds), {
    disabled: true,
    label: '拜访中…',
  });
  assert.deepEqual(ui.immediateVisitButtonState('actor-companion', visitingActorIds), {
    disabled: false,
    label: '立即拜访',
  });
  release('done');
  assert.equal(await operation, 'done');
  assert.equal(visitingActorIds.has('actor-primary'), false);
});

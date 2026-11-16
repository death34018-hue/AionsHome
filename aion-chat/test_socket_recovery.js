const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture() {
  const source = fs.readFileSync(__dirname + '/static/socket-recovery.js', 'utf8').replace(/\r\n/g, '\n');
  const helper = source.match(/function createSocketRecovery\([^]*?\n\}/);
  assert.ok(helper, 'chat pages need a connection health/retry controller');
  let now = 0, nextId = 0, current, recovery;
  const timers = new Map(), sockets = [], disconnected = [];
  const context = {
    WebSocket: { OPEN: 1, CONNECTING: 0 },
    setTimeout(fn, delay) { const id = ++nextId; timers.set(id, { fn, at: now + delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    Math: Object.assign(Object.create(Math), { random: () => 0 }),
  };
  vm.runInNewContext(helper[0] + ';this.create = createSocketRecovery;', context);
  function connect() {
    current = { readyState: 0, sent: [], closed: 0,
      send(value) { this.sent.push(JSON.parse(value)); },
      close() { this.closed++; this.readyState = 2; },
    };
    sockets.push(current);
    recovery.connecting(current);
  }
  recovery = context.create({ getSocket: () => current, connect, onDisconnect: socket => disconnected.push(socket) });
  function tick(ms) {
    const until = now + ms;
    for (;;) {
      const next = [...timers].filter(([, timer]) => timer.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      timers.delete(next[0]); now = next[1].at; next[1].fn();
    }
    now = until;
  }
  function open() { current.readyState = 1; recovery.opened(current); }
  return { recovery, connect, open, tick, sockets, disconnected, get current() { return current; } };
}

test('a half-open connection is replaced even when close never calls back', () => {
  const f = fixture(); f.connect(); f.open();
  const abandoned = f.current;
  f.tick(20000);
  assert.deepEqual(f.current.sent, [{ type: 'ping' }]);
  f.tick(15000);
  assert.equal(f.current.closed, 1);
  assert.equal(f.recovery.received(abandoned), false, 'late data cannot re-open the finished queue');
  f.tick(1000);
  assert.equal(f.sockets.length, 2);
  f.recovery.closed(abandoned);
  assert.deepEqual(f.disconnected, [abandoned], 'finish the old speech queue exactly once even without onclose');
});

test('traffic confirms liveness and repeated probes/late old callbacks do not duplicate sockets', () => {
  const f = fixture(); f.connect(); f.open();
  const old = f.current;
  f.recovery.check(); f.recovery.check();
  assert.equal(old.sent.length, 1);
  f.tick(10000); f.recovery.received(old); f.tick(10000);
  assert.equal(old.closed, 0);
  f.recovery.check(); f.tick(15000); f.tick(1000); f.open();
  f.recovery.closed(old); f.recovery.received(old); f.recovery.check();
  f.tick(1000);
  assert.equal(f.sockets.length, 2);
});

test('failed connections use bounded backoff and a stalled handshake is retried', () => {
  const f = fixture(); f.connect();
  for (const delay of [1000, 2000, 4000, 8000, 16000, 30000, 30000]) {
    const count = f.sockets.length;
    f.current.readyState = 3; f.recovery.closed(f.current);
    f.tick(delay - 1); assert.equal(f.sockets.length, count);
    f.tick(1); assert.equal(f.sockets.length, count + 1);
  }
  f.tick(20000); assert.equal(f.current.closed, 1);
  f.tick(30000); assert.equal(f.sockets.length, 9);
});

test('foreground resume actively checks the page and embedded chat even when readyState is OPEN', () => {
  const source = fs.readFileSync(__dirname + '/../AionApp/app/src/main/java/com/aion/chat/ForegroundResumeSyncScript.java', 'utf8');
  const body = source.slice(source.indexOf('static String build()'));
  const script = [...body.matchAll(/"(?:[^"\\]|\\.)*"/g)].map(match => JSON.parse(match[0])).join('');
  let pageChecks = 0, frameChecks = 0;
  const window = { aionCheckSocket() { pageChecks++; } };
  const frame = { contentWindow: { aionCheckSocket() { frameChecks++; } } };
  vm.runInNewContext(script, { window, document: { querySelectorAll: () => [frame] },
    ws: { readyState: 1 }, setTimeout() {}, console });
  assert.equal(pageChecks, 1);
  assert.equal(frameChecks, 1);
});

test('both standalone chat pages load recovery before their connection code', () => {
  for (const page of ['chat', 'chatroom']) {
    const html = fs.readFileSync(__dirname + `/static/${page}.html`, 'utf8');
    const scripts = [...html.matchAll(/<script[^>]*src="([^"]+)"/g)].map(match => match[1].split('?')[0]);
    const beforeChat = scripts.slice(0, scripts.indexOf(`/static/${page}.js`));
    assert.ok(beforeChat.some(path => {
      if (!path.startsWith('/static/')) return false;
      return /function createSocketRecovery\(/.test(fs.readFileSync(__dirname + path, 'utf8'));
    }), `${page} must load the recovery controller before opening its socket`);
  }
});

test('private replay retries HTTP failure without letting live traffic skip the missing messages', async () => {
  const source = fs.readFileSync(__dirname + '/static/chat.js', 'utf8');
  const timers = new Map(), stored = new Map([['sync', '100']]), received = [], requests = [];
  let id = 0, fail = true;
  const context = {
    privateSyncReplayPromise: null, privateSyncRetryTimer: null, privateSyncRetryDelay: 1000,
    PRIVATE_SYNC_SEQ_KEY: 'sync', ws: null, WebSocket: class {
      static OPEN = 1; static CONNECTING = 0;
      readyState = 1;
    },
    location: { protocol: 'https:', host: 'test' },
    privateSocketRecovery: { connecting() {}, received() {} },
    setTimeout(fn, delay) { timers.set(++id, { fn, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    localStorage: { getItem: k => stored.get(k), setItem: (k, v) => stored.set(k, v) },
    handleSync: e => received.push(e.sync_seq), console: { warn() {} },
    api: async (_method, url) => {
      requests.push(url);
      if (fail) throw new Error('offline');
      return { events: [101, 102, 103].map(sync_seq => ({ type: 'msg_created', sync_seq })), has_more: false };
    },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('// ── WebSocket 同步'), source.indexOf('const privateSocketRecovery =')), context);
  await context.reconcilePrivateSync();
  context.connectWS();
  context.ws.onmessage({ data: JSON.stringify({ type: 'msg_created', sync_seq: 103 }) });
  assert.equal(stored.get('sync'), '100', 'live events must not skip an unsuccessful replay');
  assert.equal(timers.size, 1, 'retry must not depend on another socket open');
  fail = false;
  const retry = [...timers.values()][0]; timers.clear();
  assert.equal(retry.delay, 1000);
  await retry.fn();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(stored.get('sync'), '103');
  assert.ok(received.includes(101) && received.includes(102));
  assert.equal(requests[1], '/api/sync/changes?after=100&limit=200');
  assert.equal(timers.size, 0);
});

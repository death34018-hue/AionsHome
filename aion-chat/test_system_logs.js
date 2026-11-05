const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync(`${__dirname}/static/chat.js`, 'utf8');

function page(api = async () => [], storage = new Map()) {
  const context = { systemLogs: [], Date, Math, console, api,
    localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
    $: () => null, document: { getElementById: () => null } };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('// ── 系统日志 ──'),
    source.indexOf('// ── 星标消息 ──')), context);
  return context;
}

test('keeps the latest 30 entries and merges the same call from two channels', () => {
  const p = page();
  for (let i = 0; i < 35; i++) p.addSystemLog({ log_id: `call${i}`, model: 'test', started_at: i + 1 });
  assert.equal(p.systemLogs.length, 30);
  assert.equal(p.systemLogs[0].log_id, 'call34');
  p.addSystemLog({ log_id: 'call34', msg_id: 'message1', recall_topic: 'test' });
  assert.equal(p.systemLogs.length, 30);
  assert.equal(p.systemLogs[0].recall_topic, 'test');
});

test('fresh page restores saved logs and keeps a call completed during loading', async () => {
  let respond;
  const p = page(() => new Promise(resolve => { respond = resolve; }));
  const loading = p.loadSystemLogs();
  p.addSystemLog({ log_id: 'wake', started_at: 2, status: 'success' });
  respond([{ log_id: 'wake', started_at: 2, status: 'running' },
    { log_id: 'supervision', started_at: 1, status: 'success' }]);
  await loading;
  assert.equal(p.systemLogs.length, 2);
  assert.equal(p.systemLogs[0].status, 'success');
  assert.equal(p.systemLogs[1].log_id, 'supervision');
});

test('clearing persists on the server and invalidates an older snapshot', async () => {
  let respond;
  const calls = [];
  const p = page((method, url) => {
    calls.push([method, url]);
    if (method === 'GET') return new Promise(resolve => { respond = resolve; });
    return Promise.resolve({ ok: true });
  });
  const loading = p.loadSystemLogs();
  await p.clearSystemLog();
  respond([{ log_id: 'old', started_at: 1 }]);
  await loading;
  assert.equal(p.systemLogs.length, 0);
  assert.deepEqual(calls[1], ['DELETE', '/api/system-logs']);
});

test('network errors survive a page refresh even though they cannot reach the server', async () => {
  const storage = new Map();
  const first = page(async () => [], storage);
  first.addErrorToSystemLog('connection lost', 'test');
  const refreshed = page(async () => [], storage);
  await refreshed.loadSystemLogs();
  assert.equal(refreshed.systemLogs.length, 1);
  assert.equal(refreshed.systemLogs[0].error_text, 'connection lost');
  await refreshed.clearSystemLog();
  const cleared = page(async () => [], storage);
  await cleared.loadSystemLogs();
  assert.equal(cleared.systemLogs.length, 0);
});

test('group log copies separate calls for one reply and ignores unrelated rooms', async () => {
  const roomSource = fs.readFileSync(`${__dirname}/static/chatroom.js`, 'utf8');
  const list = {innerHTML:'',querySelectorAll:()=>[]}, count = {}, modal = {classList:{add(){},contains:()=>true}};
  const calls = [
    {log_id:'one',msg_id:'reply',room_id:'group',actor:'aion',call_number:1,started_at:1,model:'test',
      prompt_messages:[{role:'system',content:'厨房工具 [KITCHEN:查看]'}]},
    {log_id:'two',msg_id:'reply',room_id:'group',actor:'aion',call_number:2,started_at:2,model:'test',
      prompt_messages:[{role:'system',content:'[小厨房本次查询] 样例库存'}]},
  ];
  const p = {crSystemLogs:[],crMsgDebugData:{},crLogLoadVersion:0,crSysLogHasUnreadError:false,
    currentRoom:{id:'group'},crName:()=> '当餐厨师',esc:String,Date,Math,console,
    document:{getElementById:id=>({crSysLogList:list,crSysLogCount:count,crSysLogModal:modal})[id]},
    fetch:async url=>{assert.equal(url,'/api/system-logs?room_id=group');return {ok:true,json:async()=>calls}},
    navigator:{clipboard:{writeText:async text=>{p.copied=text}}},toast() {}};
  vm.createContext(p);
  vm.runInContext(roomSource.slice(roomSource.indexOf('function crHandleDebug('),roomSource.indexOf('async function fetchChatroomModels()')),p);
  await p.crOpenSystemLog();
  p.crHandleDebug({...calls[1],type:'debug',recall_topic:'测试'});
  p.crAddSystemLog({...calls[0],room_id:'another',log_id:'hidden'});
  assert.equal(p.crSystemLogs.length,2);
  assert.match(list.innerHTML,/当餐厨师 · test/);
  assert.doesNotMatch(list.innerHTML,/第\s*\d+\s*次调用/);
  assert.match(list.innerHTML,/\[小厨房本次查询\]/);
  await p.crCopyAllSystemLogs();
  assert.doesNotMatch(p.copied,/第\s*\d+\s*次调用/);
  assert.match(p.copied,/厨房工具 \[KITCHEN:查看\]/);
  assert.match(p.copied,/\[小厨房本次查询\] 样例库存/);
});

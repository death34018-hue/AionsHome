const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync(path.join(__dirname, 'static/qq-social.js'), 'utf8');

function page(stopped = false) {
  const nodes = new Map(), timers = new Map(), requests = [];
  let timerId = 0;
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {textContent:'', innerHTML:'', disabled:false,
      dataset:{}, classList:{toggle(){}}, querySelectorAll:()=>[], querySelector:()=>({}),
      addEventListener(){}, setAttribute(){}});
    return nodes.get(id);
  };
  const context = vm.createContext({document:{hidden:false, getElementById:node},
    setInterval(fn) { const id = ++timerId; timers.set(id, fn); return id; },
    clearInterval(id) { timers.delete(id); },
    async fetch(url) {
      requests.push(url);
      if (url.endsWith('/credentials')) return {ok:true, json:async()=>({actors:{}})};
      if (url.endsWith('/connect')) return {ok:true, json:async()=>({ok:true, connection:'connecting'})};
      return {ok:false, json:async()=>({detail:'服务不可用', retry_stopped:stopped})};
    }, console});
  vm.runInContext(source, context);
  return {context, nodes, timers, requests};
}
const settle = () => new Promise(resolve => setImmediate(resolve));

test('unavailable service stops page requests after three failures until manual connect', async () => {
  const p = page();
  await settle();
  for (let n=0; n<5; n++) {
    for (const tick of [...p.timers.values()]) { tick(); await settle(); }
  }
  assert.equal(p.requests.filter(url=>url.endsWith('/status')).length, 3);
  assert.equal(p.timers.size, 0);
  assert.equal(p.nodes.get('power').textContent.includes('连接中'), false);
  const click = p.nodes.get('connect').onclick();
  await settle();
  await click;
  assert.equal(p.requests.filter(url=>url.endsWith('/connect')).length, 1);
  assert.equal(p.requests.filter(url=>url.endsWith('/status')).length, 4);
  assert.equal(p.timers.size, 1);
});

test('saved stopped status does not start another page retry cycle', async () => {
  const p = page(true);
  await settle();
  assert.equal(p.timers.size, 0);
  assert.equal(p.requests.filter(url=>url.endsWith('/status')).length, 1);
});

test('QQ model dropdown saves each actor independently and restores the saved value on failure', async t => {
  const { chromium } = require('playwright');
  const browser = await chromium.launch({ headless:true, channel:'chrome' });
  t.after(() => browser.close());
  const tab = await browser.newPage({ viewport:{width:390,height:844} });
  const errors = [], writes = [];
  tab.on('pageerror', error => errors.push(error.message));
  let rejectSave = false, holdActivity = false, releaseActivity, activityStarted;
  const activityWaiting = new Promise(resolve => { activityStarted=resolve; });
  const settings = {enabled:true, mode:'natural', interval_seconds:120, context_messages:35,
    image_wait_seconds:60, hourly_limit:6, total_hourly_limit:60, model:''};
  const status = {settings:{enabled:true,bring_home:true}, groups:[], models:['Home','Cheap & Fast'], actors:{
    aion:{name:'角色甲',state:'在线',model:'Home',home_model:'Home',settings:{...settings}},
    connor:{name:'角色乙',state:'在线',model:'Home',home_model:'Home',settings:{...settings}},
  }};
  await tab.route('http://qq.test/**', async route => {
    const request = route.request(), url = new URL(request.url());
    const staticFiles = {'/':'qq-social.html','/static/qq-social.js':'qq-social.js','/static/qq-social.css':'qq-social.css'};
    if (staticFiles[url.pathname]) return route.fulfill({path:path.join(__dirname,'static',staticFiles[url.pathname])});
    if (url.pathname.endsWith('/credentials')) return route.fulfill({json:{actors:{}}});
    if (url.pathname.endsWith('/status')) return route.fulfill({json:status});
    if (url.pathname.endsWith('/activity')) {
      if(holdActivity)await new Promise(resolve => { releaseActivity=resolve;activityStarted(); });
      return route.fulfill({json:{enabled:false}});
    }
    const actor = url.pathname.match(/\/actors\/(aion|connor)\/settings$/)?.[1];
    if (actor && request.method()==='PATCH') {
      writes.push({actor,...request.postDataJSON()});
      if (rejectSave) return route.fulfill({status:400,json:{detail:'模型已不可用'}});
      Object.assign(status.actors[actor].settings, request.postDataJSON());
      status.actors[actor].model = status.actors[actor].settings.model || 'Home';
      return route.fulfill({json:status.actors[actor].settings});
    }
    return route.fulfill({status:404,json:{detail:'Not found'}});
  });
  await tab.goto('http://qq.test/');
  const aion = tab.locator('[data-actor-model="aion"]'), connor = tab.locator('[data-actor-model="connor"]');
  await aion.waitFor({timeout:2000});
  assert.equal(await aion.inputValue(), '');
  await aion.selectOption('Cheap & Fast');
  await tab.waitForFunction(() => document.getElementById('notice').textContent==='已保存');
  assert.deepEqual(writes, [{actor:'aion',model:'Cheap & Fast'}]);
  await tab.reload();
  await aion.waitFor();
  assert.equal(await aion.inputValue(), 'Cheap & Fast');
  assert.equal(await connor.inputValue(), '');
  holdActivity = true;
  await tab.evaluate(() => { refresh(); });
  await activityWaiting;
  rejectSave = true;
  await aion.selectOption('Home');
  await tab.waitForFunction(() => document.getElementById('notice').textContent==='模型已不可用');
  holdActivity = false;
  releaseActivity();
  await tab.waitForFunction(() => document.querySelector('[data-actor-model="aion"]').value==='Cheap & Fast' && !document.querySelector('[data-actor-model="aion"]').disabled, null, {timeout:2000});
  rejectSave = false;
  await aion.selectOption('');
  await tab.waitForFunction(() => document.getElementById('notice').textContent==='已保存' && !document.querySelector('[data-actor-model="aion"]').disabled);
  assert.equal(status.actors.aion.model, 'Home');
  assert.equal(await tab.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.deepEqual(errors, []);
});

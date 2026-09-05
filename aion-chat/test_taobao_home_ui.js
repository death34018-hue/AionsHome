const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// Exercise the actual refresh path without making requests or starting an outing.
const source = fs.readFileSync('static/taobao.js', 'utf8');
const refresh = source.slice(source.indexOf('  async function refresh()'), source.indexOf("  $('searchForm').addEventListener"));
async function check(last_run, busy = false) {
  const messages = [], timers = [];
  const context = {state:null, tripsHaveMore:false, linkedTrip:null, pollTimer:null,
    api:async()=>({names:{connor:'测试角色'},trips:[],last_run,busy}),
    render(){}, notice:(...args)=>messages.push(args), clearTimeout(){},
    setTimeout:(fn,delay)=>timers.push(delay)};
  vm.createContext(context); vm.runInContext(refresh,context);
  await context.refresh(); return {messages,timers};
}
(async()=>{
  const success = await check({actor:'connor',ok:true,message:'真实搜索了某商品，保存了 1 件'});
  assert.equal(success.messages[0][0], '', 'successful outings must not repeat the search summary above the feed');
  const failed = await check({actor:'connor',ok:false,message:'需要重新登录'});
  assert.equal(failed.messages[0][0], '测试角色：需要重新登录');
  assert.equal(failed.messages[0][1], true, 'connection failures must remain visible');
  const active = await check(null,true);
  assert.ok(active.messages[0][0].includes('正在逛淘宝'));
  assert.deepEqual(active.timers,[4000]);
  console.log('shopping home: quiet success, visible errors and active progress OK');
})().catch(error=>{console.error(error);process.exitCode=1;});

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname,'static/theater-studio.js'),'utf8');
const c = vm.createContext({});
vm.runInContext(source.slice(source.indexOf('  function writeProgressText('),source.indexOf('  function renderWriteStatus()')),c);
const text = (p,ahead) => ({...c.writeProgressText(p,ahead)});

test('preparing and waiting show elapsed seconds and that no data has arrived',()=>{
  assert.deepEqual(text({elapsed:12.4,waited:null,idle:null,chars:0,thinking:false}),
    {clock:'已用时 12 秒',detail:'正在整理前文，还没有向模型发出本章请求。',stalled:false});
  assert.deepEqual(text({elapsed:40,waited:37.9,idle:null,chars:0,thinking:false}),
    {clock:'已等待 37 秒',detail:'请求已发出，尚未收到任何数据。',stalled:false});
});
test('a minute without any data suggests stopping, counting time since the last poll',()=>{
  const view=text({elapsed:60,waited:58.6,idle:null,chars:0,thinking:false},1.5);
  assert.equal(view.clock,'已等待 1 分 0 秒');
  assert.equal(view.stalled,true);
  assert.match(view.detail,/尚未收到任何数据。已超过 1 分钟，可能卡住了，可以终止后重试。/);
});
test('received data reports thinking, text length and how recent it is',()=>{
  assert.deepEqual(text({elapsed:41,waited:40,idle:3.2,chars:0,thinking:true}),
    {clock:'已用时 40 秒',detail:'模型正在思考，尚无正文，最近一次数据在 3 秒前。',stalled:false});
  assert.deepEqual(text({elapsed:66,waited:65,idle:2,chars:1234,thinking:false}),
    {clock:'已用时 1 分 5 秒',detail:'已收到 1,234 字，最近一次数据在 2 秒前。',stalled:false});
  const stalled=text({elapsed:200,waited:190,idle:75,chars:1234,thinking:false});
  assert.equal(stalled.stalled,true);
  assert.match(stalled.detail,/最近一次数据在 1 分 15 秒前。数据已中断超过 1 分钟/);
});

const assert = require('node:assert/strict');
const fs = require('node:fs');
assert.ok(fs.existsSync(`${__dirname}/static/toy-ankni-protocol.js`), 'ANKNI protocol must exist');
const p = require('./static/toy-ankni-protocol.js');
assert.equal(p.intensity(5, 0), 'AA08030500641E');
assert.equal(p.intensity(5, 0, true), 'AA08030005641E');
assert.deepEqual(p.stop('0F'), ['AA0803000000B5', 'AA0F020000BB']);
assert.equal(p.classic(1, '0F'), 'AA0F020101BD');
assert.deepEqual(p.parse('LOOP:2000,5;3000,0;4000,6').phases, [{ms:2000,mode:5},{ms:3000,mode:0},{ms:4000,mode:6}]);
assert.equal(p.parse('LOOP:2000,5;3000,0;4000,6').duration,9000);
assert.equal(p.parse('LOOP:2000,5;1000,6;1000,0').duration,4000);
for(const bad of ['LOOP:2000,5;0,6','LOOP:2000,5;199,6','LOOP:200,11','LOOP:600001,1','LOOP:400000,1;400000,2','LOOP:200,5,0']) assert.throws(()=>p.parse(bad));
assert.throws(() => p.parse('LOOP:200,101,0'));
const { createAnkniController } = require('./static/toy-ankni-controller.js');
(async () => {
  const sent = [], delays=[], timers = new Map(); let id = 0;
  const controller = createAnkniController({
    transport: { sendHex: async hex => sent.push(hex), isConnected: () => true },
    scheduler: {setTimeout: (fn,ms) => { delays.push(ms); timers.set(++id, fn); return id; }, clearTimeout: key => timers.delete(key)},
  });
  await controller.executeText('LOOP:2000,5;3000,6');
  assert.equal(sent.at(-1), 'AA0F020505C5');
  assert.equal(delays.at(-1),2000);
  const step=timers.get(id);timers.delete(id);await step();
  assert.equal(sent.at(-1),'AA0F020606C7');
  assert.equal(delays.at(-1),3000);
  const old = [...timers.values()][0];
  await controller.stopAll();
  const count = sent.length;
  await old();
  assert.equal(sent.length, count, 'STOP cancels an already captured timer');
  assert.equal(timers.size, 0);
  await controller.executeText('LOOP:2000,5;3000,6');
  const beforePending=sent.length;
  await controller.executeText('LOOP:800,2');
  await controller.executeText('LOOP:1000,3;2000,0');
  assert.equal(sent.length,beforePending,'new plans wait for the first full cycle');
  let next=timers.get(id);timers.delete(id);await next();
  assert.equal(sent.at(-1),'AA0F020606C7','old second segment is not skipped');
  next=timers.get(id);timers.delete(id);await next();
  assert.equal(sent.at(-1),'AA0F020303C1','latest waiting plan starts at the cycle boundary');
  next=timers.get(id);timers.delete(id);await next();
  assert.deepEqual(sent.slice(-2),['AA0803000000B5','AA0F020000BB'],'mode zero stops both channels');
  next=timers.get(id);timers.delete(id);await next();
  assert.equal(sent.at(-1),'AA0F020303C1','a timed rest resumes with the next cycle');
  await controller.executeText('LOOP:1000,4');
  assert.equal(sent.at(-1),'AA0F020404C3','after one full cycle replacement is immediate');
  await controller.executeText('LOOP:1000,8');
  const cancelled=timers.get(id);
  await controller.stopAll();
  const stoppedCount=sent.length;
  await cancelled();
  assert.equal(sent.length,stoppedCount,'STOP also discards the waiting plan');
  const {createAnkniAiControl} = require('./static/toy-ankni-ai.js');
  let permission = {enabled:true,epoch:'ankni-1'}, connected=true;
  const actions=[];
  const ai=createAnkniAiControl({
    controller:{getState:()=>({connected,protocolConfirmed:connected}),executeText:async raw=>actions.push(raw),stopAll:async()=>actions.push('STOP')},
    request:async()=>({ok:true,json:async()=>({...permission})}),
  });
  const event={type:'ankni_command',data:{event_id:'one',epoch:'ankni-1',command:'LOOP:2000,5'}};
  await ai.refresh();await ai.receive(event);await ai.receive(event);
  assert.deepEqual(actions,['LOOP:2000,5']);
  await ai.receive({type:'svakom_command',data:{...event.data,event_id:'wrong-profile'}});
  assert.equal(actions.length,1);
  permission={enabled:false,epoch:'ankni-2'};
  await ai.receive({type:'toy_profile_changed'});
  assert.equal(actions.at(-1),'STOP');
  permission={enabled:true,epoch:'ankni-3'};connected=false;
  const offline={...event,data:{...event.data,event_id:'offline',epoch:'ankni-3'}};
  await ai.receive(offline);connected=true;await ai.receive(offline);
  await ai.receive({...event,data:{...event.data,event_id:'stale'}});
  assert.equal(actions.length,2,'offline, duplicate and stale commands never restart');
  console.log('ANKNI packets, validation and stop cancellation passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

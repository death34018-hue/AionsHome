(function(root, factory) {
  const api = factory(typeof module === 'object' && module.exports ? require('./toy-ankni-protocol.js') : root.AnkniProtocol);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) Object.assign(root, api);
})(typeof window === 'undefined' ? globalThis : window, function(p) {
  'use strict';
  function createAnkniController({transport, scheduler = globalThis, onState = () => {}, onError = () => {}}) {
    let generation = 0, timer = null, queue = Promise.resolve(), swap = false, prefix = '0F', activeMode = -1;
    let timelineRunning = false, firstCycleComplete = false, pendingPlan = null;
    let current = {v:0,s:0,running:false};
    const getState = () => ({...current, connected:transport.isConnected(), protocolConfirmed:transport.isConnected()});
    function cancel() {
      generation++; activeMode=-1; timelineRunning=false; firstCycleComplete=false; pendingPlan=null;
      if (timer !== null) scheduler.clearTimeout(timer); timer = null; current.running = false;
    }
    function send(hex, token) {
      const task = queue.then(() => { if (token === generation) return transport.sendHex(hex); });
      queue = task.catch(() => {}); return task;
    }
    async function stopAll() {
      cancel(); activeMode = 0; const token = generation;
      if (transport.isConnected()) {
        if (transport.execute) await transport.execute('STOP');
        else for (const hex of p.stop(prefix)) await send(hex, token);
      }
      current = {v:0,s:0,running:false}; onState(getState());
    }
    async function executeText(text) {
      const action = p.parse(text);
      if (action.kind === 'stop') return stopAll();
      if (!transport.isConnected()) throw new Error('请先连接 ANKNI MX');
      // The phone owns waiting when a native transport is available.
      if (!transport.execute && action.kind === 'timeline' && timelineRunning && !firstCycleComplete) {
        pendingPlan=action; return;
      }
      cancel(); const token = generation;
      if (transport.execute) { await transport.execute(action.raw); current.running = true; onState(getState()); return; }
      async function write(v,s) { activeMode=-1; await send(p.intensity(v,s,swap),token); if(token===generation) {current={v,s,running:true}; onState(getState());} }
      async function mode(number) {
        if(activeMode !== number) {
          const packets=number===0 ? p.stop(prefix) : [p.classic(number,prefix)];
          for(const hex of packets) await send(hex,token);
        }
        if(token===generation) {activeMode=number; current={v:0,s:0,mode:number,running:true};onState(getState());}
      }
      if (action.kind === 'set') return write(action.v,action.s);
      if (action.kind === 'mode') return mode(action.mode);
      timelineRunning=true;
      async function step(index) {
        if (token !== generation) return;
        if (index === action.phases.length) {
          firstCycleComplete=true;
          if(pendingPlan) return executeText(pendingPlan.raw);
          if (!action.loop) return stopAll(); index=0;
        }
        const phase = action.phases[index];
        await mode(phase.mode);
        if(token===generation) timer=scheduler.setTimeout(() => step(index+1).catch(async e => {await stopAll().catch(()=>{});onError(e);}),phase.ms);
      }
      return step(0);
    }
    async function configure(nextSwap,nextPrefix) {
      p.stop(nextPrefix); await stopAll(); swap=Boolean(nextSwap); prefix=nextPrefix;
      await transport.configure?.(swap,prefix);
    }
    function disconnected() { cancel(); activeMode=0; current={v:0,s:0,running:false}; onState(getState()); }
    return {getState,executeText,stopAll,configure,disconnected};
  }
  return {createAnkniController};
});

(function(root, doc) {
  'use strict';
  const $ = id => doc.getElementById(id);
  const bridge = root.AionBle;
  const p = root.AnkniProtocol;
  let connecting = false, pending = null, nativeAction = '';
  function connected() { return Boolean(bridge?.isConnected() && bridge.getProfile() === 'ankni'); }
  function log(value) {
    const row=doc.createElement('p'); row.textContent=new Date().toLocaleTimeString()+' '+value;
    $('toyLog').appendChild(row); while($('toyLog').children.length>40) $('toyLog').firstElementChild.remove();
  }
  function error(e) { const message=e.message || String(e); log(message); $('ankniDetail').textContent=message; }
  function update() {
    const ready=connected();
    $('ankniStatus').textContent=connecting?'正在连接':ready?'已连接':'未连接';
    $('ankniConnect').textContent=ready?'断开':'连接'; $('ankniConnect').disabled=connecting;
    doc.querySelectorAll('[data-ankni-control]').forEach(button=>button.disabled=!ready);
    $('ankniLive').textContent=ready && nativeAction.startsWith('SET:') ? '震动 '+nativeAction.slice(4).split(',')[0]+' · 吮吸 '+nativeAction.slice(4).split(',')[1] : ready && nativeAction.startsWith('MODE:') ? p.MODES[Number(nativeAction.slice(5))] : '已停止';
  }
  const transport={
    isConnected:connected,
    execute:async raw=>{if(raw !== 'STOP' && Number(bridge.getAnkniControlVersion?.() || 0)<3) throw new Error('模式等待与定时停止需要小家 App 1.28，请更新手机 App');if(!bridge.executeAnkniCommand(raw)) throw new Error('手机未接受 ANKNI 指令，请检查连接');},
    configure:async(swap,prefix)=>{if(!bridge.configureAnkni(swap,prefix)) throw new Error('连接参数设置失败');},
  };
  const controller=root.createAnkniController({transport,onState:update,onError:error});
  const ai=root.createAnkniAiControl({controller,onLog:log,onState:value=>{
    $('ankniAi').checked=value.enabled; $('ankniAi').disabled=value.blocked;
    $('ankniAiStatus').textContent=value.blocked?'正在同步控制权…':value.active?'AI 编排运行中':value.enabled?'已开启 · 连接后执行':'已关闭 · 手动控制仍可用';
  }});
  async function selected() {
    const value=await root.ToyNavigation.readSelection();
    if(value.active!=='ankni') throw new Error('请先在密语时刻选用 ANKNI MX');
  }
  async function manual(raw) {
    await selected(); await ai.takeover();
    if(raw !== 'STOP') await controller.stopAll();
    await controller.executeText(raw);
    log('已提交 '+raw);
  }
  function targets() {
    const rows=JSON.parse(bridge.getAnkniWriteTargets()); $('ankniTarget').innerHTML='';
    rows.forEach((text,i)=>{const option=doc.createElement('option');option.value=i;option.textContent=text;$('ankniTarget').appendChild(option);});
  }
  root.toyNativeBle={
    async onConnected(profile,name) {
      if(profile!=='ankni') return;
      try {
        await selected();
        await controller.configure($('ankniSwap').checked,$('ankniPrefix').value);
        targets(); $('ankniDetail').textContent=name; pending?.resolve();
      } catch(e){bridge.disconnect();pending?.reject(e);error(e);}
      pending=null;connecting=false;update();
    },
    onDisconnected(){pending?.reject(new Error('连接已断开'));pending=null;connecting=false;nativeAction='';controller.disconnected();ai.disconnected();update();},
    onError(message){pending?.reject(new Error(message));pending=null;connecting=false;error(new Error(message));update();},
    onLog:log,
    onNativeAction(action){nativeAction=action;update();},
  };
  root.stopAndDisconnectToy=async()=>{
    if(!connected() && !connecting) return;
    await ai.takeover(); await controller.stopAll();
    bridge.disconnect(); controller.disconnected(); nativeAction='';update();
  };
  $('ankniConnect').onclick=async()=>{
    try {
      if(connected()) {await root.stopAndDisconnectToy();return;}
      await selected();
      if(!bridge?.getAnkniControlVersion || bridge.getAnkniControlVersion()<3) throw new Error('请在小家手机 App 1.28 或更新版内连接');
      bridge.selectProfile('ankni');connecting=true;update();
      await new Promise((resolve,reject)=>{
        const timeout=root.setTimeout(()=>{pending=null;bridge.disconnect();reject(new Error('连接超时，请确认玩具已开机'));},15000);
        pending={resolve:()=>{root.clearTimeout(timeout);resolve();},reject:e=>{root.clearTimeout(timeout);reject(e);}};
        bridge.connect();
      });
    }catch(e){error(e);}finally{connecting=false;update();}
  };
  for(const channel of ['V','S']) $('ankni'+channel).oninput=e=>$('ankni'+channel+'Value').value=e.target.value;
  $('ankniApply').onclick=()=>manual('SET:'+$('ankniV').value+','+$('ankniS').value).catch(error);
  p.MODES.forEach((name,i)=>{const button=doc.createElement('button');button.type='button';button.dataset.ankniControl='';button.textContent=i+' · '+name;button.onclick=()=>manual(i===0?'STOP':'MODE:'+i).catch(error);$('ankniModes').appendChild(button);});
  doc.querySelectorAll('[data-command]').forEach(button=>button.onclick=()=>manual(button.dataset.command).catch(error));
  for(const [id,kind] of [['ankniOnce','SEQ'],['ankniLoop','LOOP']]) $(id).onclick=()=>manual(kind+':'+$('ankniPhases').value.trim().split(/\n+/).join(';')).catch(error);
  $('ankniStop').onclick=async()=>{try{await ai.takeover();await controller.stopAll();log('已提交全部停止');}catch(e){error(e);}};
  $('ankniAi').onchange=()=>ai.toggle($('ankniAi').checked).catch(error);
  for(const id of ['ankniSwap','ankniPrefix']) $(id).onchange=async()=>{
    try{await ai.takeover();await controller.configure($('ankniSwap').checked,$('ankniPrefix').value);root.localStorage.setItem('ankni_connection_options',JSON.stringify({swap:$('ankniSwap').checked,prefix:$('ankniPrefix').value}));}catch(e){error(e);}
  };
  $('ankniTarget').onchange=async()=>{try{await ai.takeover();await controller.stopAll();if(!bridge.selectAnkniWriteTarget(Number($('ankniTarget').value)))throw new Error('写入特征切换失败');}catch(e){error(e);}};
  try {const saved=JSON.parse(root.localStorage.getItem('ankni_connection_options')||'{}');$('ankniSwap').checked=saved.swap===true;if(['0A','0F'].includes(saved.prefix))$('ankniPrefix').value=saved.prefix;}catch(e){}
  function socket() {
    const ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws');
    ws.onopen=()=>ai.refresh().catch(error);
    ws.onmessage=event=>{try{ai.receive(JSON.parse(event.data)).catch(error);}catch(e){error(e);}};
    ws.onclose=()=>root.setTimeout(socket,2000);ws.onerror=()=>ws.close();
  }
  socket();update();ai.refresh().catch(error);
  doc.addEventListener('visibilitychange',()=>{if(!doc.hidden){update();ai.refresh().catch(error);}});
})(window,document);

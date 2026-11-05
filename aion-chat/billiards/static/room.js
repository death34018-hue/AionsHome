/* Saved state drives the room; the original game only renders and collects input. */
(() => {
  const $ = id => document.getElementById(id);
  const makeId=()=>globalThis.crypto?.randomUUID?.()||`pool-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  const client = makeId();
  let game=null, mounted=false, busy=false, animating=false, startingAnimation=false;
  let polling=false, visible=!document.hidden, heartbeatAt=0, revision=-1, rack=0, eventId='';
  let pageVisible=true, appForeground=true;
  let labels={user:'你',aion:'AI',connor:'另一位伴侣'}, chatConfig={}, messagesSeen=new Set(), audioQueue=Promise.resolve();
  const bubbleTimers=new Map();
  let statusKey='',messagesKey='',pottedKey='',controlEnabled=null,chatBusy=false,voiceRecording=false,activeSpeech=null;
  let selectedOpponent='aion',historyOffset=0,historyLoading=false,lobbyData=null;
  const error=text=>{$('roomError').textContent=text||'';$('lobbyError').textContent=text||'';};
  async function request(path, data, keepalive=false) {
    const response=await fetch(path,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json'}:{},body:data?JSON.stringify(data):undefined,cache:'no-store',keepalive});
    const result=await response.json();
    if(!response.ok)throw Error(result.detail||result.error||'台球室暂时连不上');
    return result;
  }
  const endpoint=part=>`/api/billiards/${game.id}/${part}`;
  const canControl=()=>!!(game&&game.status==='running'&&game.controlled_here&&game.players[0]==='user'&&!busy&&!animating&&game.busy_ms<=0&&visible);
  const label=seat=>labels[game.players[seat]];
  function status() {
    if(!game)return;
    if(!animating)renderPottedBalls();
    const key=JSON.stringify([game.match,game.status,game.controlled_here,game.score,game.rack,game.players,labels,busy,animating,game.error,game.chat_error,game.state.phase,game.state.turn,game.state.result,chatBusy]);
    if(statusKey===key)return;statusKey=key;
    $('seriesScore').textContent=`${game.score[0]} : ${game.score[1]}`;
    $('seriesScore').setAttribute('aria-label',`${label(0)} ${game.score[0]} 比 ${game.score[1]} ${label(1)}`);
    const match=game.match||{format:'legacy',status:'legacy'},single=match.format==='single',series=match.format==='best_of_3',completed=match.status==='completed',ended=match.status!=='active';
    $('rackBadge').textContent=single?'单局制 · 一局定胜负':series?`历史三局赛 · 第 ${game.rack} / 3 局`:`旧版自由对局 · 第 ${game.rack} 局`;
    $('seriesStage').textContent=completed?'本场胜负已定':single?'打完一局即可收杆':match.deciding?'决胜局':match.match_point?'赛点':'';
    $('winMarks').textContent=series?game.score.map(n=>'●'.repeat(n)+'○'.repeat(Math.max(0,2-n))).join(' : '):'';
    $('winMarks').setAttribute('aria-label',`${label(0)}已赢${game.score[0]}局，${label(1)}已赢${game.score[1]}局`);
    const watching=!game.players.includes('user')||!game.controlled_here;
    const aiOnly=!game.players.includes('user');
    $('matchStatus').textContent=completed?'整场结束':match.status==='abandoned'?'本场已中止':game.status==='finished'?(aiOnly?'本局结束 · 可继续下一局':'本局已结束'):game.status==='paused'?'已暂停':watching?'正在旁观':'正在打球';
    if(aiOnly&&!ended&&game.status==='finished')$('seriesStage').textContent='比分保留 · 可继续下一局';
    $('saveState').textContent=busy?'正在保存…':game.error||'已保存到小家 · 随时回来继续';
    $('pauseBtn').textContent=game.status==='running'&&(game.controlled_here||!game.players.includes('user'))?'暂停':'继续';
    $('pauseBtn').setAttribute('aria-label',$('pauseBtn').textContent==='暂停'?'暂停球局':'继续球局');
    $('pauseBtn').hidden=game.status==='finished';
    $('pauseBtn').disabled=busy;
    $('roomCover').hidden=game.status!=='paused'||animating;
    $('roomCoverText').textContent=game.error||'球局已暂停 · 点击继续';
    $('humanCard').querySelector('strong').textContent=label(0);
    $('aiCard').querySelector('strong').textContent=label(1);
    $('leftSeatAvatar').src=game.players[0]==='user'?'/public/UserIcon.png':avatar(game.players[0]);
    $('rightSeatAvatar').src=avatar(game.players[1]);
    $('opponentAvatar').src=avatar(game.players[1]);
    $('opponentAvatar').alt=label(1);
    document.querySelector('.tray-label').firstChild.textContent=`${label(0)}的目标 `;
    if(game.players[0]!=='user')$('turnTitle').textContent=game.state.phase==='over'?`${label(game.state.result.winner)}赢了`:`${label(game.state.turn)}的回合`;
    if(game.players[0]!=='user'||!game.controlled_here){$('placeBtn').disabled=true;$('shootBtn').disabled=true;$('aimLeft').disabled=$('aimRight').disabled=true;}
    $('endDialog').dataset.series=match.status;
    $('seriesFinal').hidden=!completed;
    $('endDialog').querySelector('.dialog-close').hidden=ended;
    $('endKicker').textContent=completed?(single?'MATCH COMPLETE · 单局结束':'MATCH COMPLETE · 比赛结束'):'THE FINAL POCKET';
    $('endLobbyBtn').className=ended?'primary':'quiet';
    if(game.state.result){
      $('endTitle').textContent=completed?(game.players[match.winner]==='user'?'这一轮，你是冠军！':'本场冠军揭晓'):`${label(game.state.result.winner)}赢了这局`;
      if(completed){
        $('championAvatar').src=avatar(game.players[match.winner]);
        $('championName').textContent=label(match.winner);
        $('finalLeftName').textContent=label(0);$('finalRightName').textContent=label(1);
        $('finalScore').textContent=game.score.join(' : ');
        $('endReason').textContent=single?game.state.result.reason:game.score[1-match.winner]===0?'直落两局，拿下整轮胜利。':'决胜局落定，这一轮分出胜负。';
        const reports=game.result_reports||[];
        $('finalRacks').replaceChildren();
        for(const report of reports){const row=element('li');row.append(element('span','',`第 ${report.rack} 局`),element('strong','',`${label(report.winner)}胜`));$('finalRacks').append(row);}
        $('endStats').textContent=`本场 ${game.rack} 局 · 共 ${reports.reduce((total,r)=>total+r.shots,0)} 杆 · 战绩已保存`;
      }else $('endStats').textContent=`${single?'单局制':series?'历史三局赛':'旧版对局'} · ${label(0)} ${game.score[0]} : ${game.score[1]} ${label(1)} · 本局 ${game.state.shotCount} 杆`;
    }
    $('playAgainBtn').textContent=aiOnly?'让他们打下一局':'进入下一局';
    $('playAgainBtn').hidden=ended;
    $('playAgainBtn').disabled=busy;
    $('restartBtn').textContent=game.state.phase==='over'?'进入下一局':'重开本局';
    $('restartBtn').hidden=ended||(aiOnly&&game.state.phase!=='over');
    $('restartBtn').disabled=ended||busy||(aiOnly&&game.state.phase!=='over');
    $('stalemateBtn').hidden=ended;
    $('finishMatch').disabled=ended;
    $('chatState').textContent=chatBusy?'正在接话…':game.chat_error||'';
  }
  const avatar=actor=>actor==='user'?'/public/UserIcon.png':actor==='connor'?'/public/codexicon.png':'/public/gropicon1.png';
  function renderPottedBalls(){
    const potted=game.potted_balls||[[],[]],key=JSON.stringify([game.id,game.rack,potted,labels]);
    if(pottedKey===key)return;pottedKey=key;
    for(let seat=0;seat<2;seat++){
      const list=$(seat===0?'leftPottedBalls':'rightPottedBalls');list.replaceChildren();
      list.setAttribute('aria-label',`${label(seat)}本局进球，按时间顺序`);
      for(const id of potted[seat]){
        const ball=document.createElement('li');ball.className='seat-ball'+(id>8?' striped':'');
        ball.style.setProperty('--ball-color',Billiards.Renderer.color(id));
        ball.title=`${label(seat)}打进 ${id} 号球`;ball.setAttribute('aria-label',`${id}号球`);
        const number=document.createElement('b');number.textContent=id;ball.append(number);list.append(ball);
      }
    }
  }
  function renderMessages(initial=false) {
    const key=JSON.stringify([game.id,labels,game.messages]);if(key===messagesKey)return;messagesKey=key;
    const list=$('tableMessages'),nearBottom=list.scrollHeight-list.scrollTop-list.clientHeight<70;
    list.replaceChildren();
    for(const message of game.messages){
      const row=document.createElement('div');row.className='table-message';
      const author=document.createElement('strong');author.textContent=labels[message.sender]||message.sender;
      const time=document.createElement('small');time.textContent=`第${message.rack}局 · 第${message.shot}杆`;
      const text=document.createElement('p');text.textContent=message.text;row.append(author,time,text);
      list.append(row);
      if(!initial&&!messagesSeen.has(message.id)&&message.sender!=='user'&&visible){
        showBubble(message);
        if($('tableVoice').checked)speak(message);
      }
      messagesSeen.add(message.id);
    }
    if(initial||nearBottom)list.scrollTop=list.scrollHeight;
    if(!game.messages.length)list.textContent='可以聊球，也可以随便说点什么。';
  }
  function showBubble(message){
    const seat=game.players.indexOf(message.sender);
    if(seat<0||message.sender==='user'||message.rack!==game.rack)return;
    const card=$(seat===0?'humanCard':'aiCard');
    let bubble=card.querySelector('.table-bubble');
    if(!bubble){
      bubble=document.createElement('button');bubble.className='table-bubble';bubble.type='button';
      bubble.setAttribute('aria-live','polite');bubble.onclick=()=>toggleTableChat(true);card.append(bubble);
    }
    const copy=document.createElement('span');copy.className='bubble-copy';
    copy.textContent=`${labels[message.sender]}：${message.text}`;bubble.replaceChildren(copy);
    bubble.setAttribute('aria-label',`${labels[message.sender]}说：${message.text}，点击打开桌边聊天`);
    bubble.hidden=false;
    if(portraitRoom())for(const other of document.querySelectorAll('.table-bubble'))if(other!==bubble)other.hidden=true;
    bubble.dataset.seat=String(seat);
    layoutBubbles();
    clearTimeout(bubbleTimers.get(message.sender));
    const readingTime=Math.min(90000,Math.max(20000,message.text.length*120));
    bubbleTimers.set(message.sender,setTimeout(()=>{bubble.hidden=true;layoutBubbles();},readingTime));
    if(!$('roomChat').classList.contains('chat-open'))$('tableChatToggle').classList.add('unread');
  }
  function layoutBubbles(){
    const bar=document.querySelector('.room-bar');
    const padding=parseFloat(getComputedStyle(bar).paddingBottom),barBox=bar.getBoundingClientRect();
    for(const bubble of document.querySelectorAll('.table-bubble:not([hidden])')){
      const card=bubble.parentElement,box=card.getBoundingClientRect(),avatar=card.querySelector('.seat-avatar').getBoundingClientRect();
      const seat=Number(bubble.dataset.seat);
      const available=avatar.width?(seat===0?innerWidth-avatar.right-20:avatar.left-20):innerWidth-24;
      const width=Math.min(portraitRoom()?320:380,Math.max(100,available),innerWidth-24);
      const beside=seat===0?avatar.right+8:avatar.left-width-8;
      const left=Math.max(12,Math.min(innerWidth-width-12,beside));
      bubble.style.width=width+'px';bubble.style.left=left-box.left+'px';
      const top=Math.max(0,avatar.top-box.top);
      bubble.style.top=top+'px';
      const style=getComputedStyle(bubble);
      const inset=parseFloat(style.paddingTop)+parseFloat(style.paddingBottom)+2;
      // Scroll long speech inside the existing header; never resize the table.
      bubble.querySelector('.bubble-copy').style.maxHeight=Math.max(16,barBox.bottom-padding-box.top-top-inset)+'px';
    }
  }
  function speak(message){
    const voice=chatConfig[message.sender==='connor'?'tts_connor_voice':'tts_aion_voice'];
    if(!voice){error('先在小家的语音设置里选择这位伴侣的音色');return;}
    audioQueue=audioQueue.then(async()=>{
      if(!visible||voiceRecording)return;
      const response=await fetch('/api/tts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:message.text,voice,msg_id:message.id})});
      if(!response.ok)throw Error('这句语音没能播放，文字和球局都已保存');
      const url=URL.createObjectURL(await response.blob()),audio=new Audio(url);
      if(!visible||voiceRecording){URL.revokeObjectURL(url);return;}
      showBubble(message);
      await new Promise(resolve=>{activeSpeech=()=>{audio.pause();resolve();};audio.onended=audio.onerror=resolve;audio.play().catch(resolve);});activeSpeech=null;URL.revokeObjectURL(url);
    }).catch(e=>error(e.message));
  }
  function apply(data, animate=false, initial=false){
    const changingRack=game&&(game.rack!==data.rack||game.id!==data.id);
    if(game&&game.id!==data.id){revision=-1;eventId='';messagesSeen.clear();messagesKey='';statusKey='';}
    if(changingRack){for(const bubble of document.querySelectorAll('.table-bubble'))bubble.hidden=true;layoutBubbles();}
    game=data;labels=data.names||labels;
    if(changingRack&&$('endDialog').open)$('endDialog').close();
    if(!mounted){
      document.body.classList.add('in-match');
      $('lobby').hidden=true;$('gameApp').hidden=false;
      $('difficulty').value=data.difficulty;$('accuracy').value=data.accuracy;$('rulesMode').value=data.mode;
      Billiards.Game.mount({state:data.state,playerName:label,isAI:seat=>game.players[seat]!=='user',canControl,canPresent:()=>visible,action:submit,restart,onState:s=>{
        if(animating&&!startingAnimation&&!['motion','presenting'].includes(s.phase))animating=false;
        statusKey='';status();
      }});mounted=true;
      eventId=data.last_event?.id||'';
    } else if(!animating&&(data.revision!==revision||data.rack!==rack)){
      if(animate&&data.last_event&&data.last_event.id!==eventId&&data.busy_ms>0){
        startingAnimation=true;animating=true;eventId=data.last_event.id;
        Billiards.session.play(data.last_event.before,data.last_event.action);startingAnimation=false;
      }else if(!data.ai_preview||data.ai_preview.id!==Billiards.session.controller.getState().aiCue?.id)Billiards.session.load(data.state);
    }
    if(!animating){Billiards.session.preview(data.status==='running'?data.ai_preview:null);revision=data.revision;rack=data.rack;}
    status();renderMessages(initial);
    const enabled=canControl();if(enabled!==controlEnabled){controlEnabled=enabled;Billiards.session.refresh();statusKey='';status();}
  }
  async function loadGame(id){
    error('');const data=await request(`/api/billiards/${id}?client=${encodeURIComponent(client)}`);
    history.replaceState(null,'',`/billiards?game=${id}`);apply(data,false,true);
  }
  async function lobby(){
    const data=await request('/api/billiards');labels=data.names;
    lobbyData=data;
    const current=data.games.find(g=>g.players.includes('user'));
    if(current)selectedOpponent=current.players.find(p=>p!=='user');
    $('aionChoiceName').textContent=labels.aion;$('connorChoiceName').textContent=labels.connor;
    updateChallenge();
    $('historyCount').textContent=data.history_count;
    renderStandings(data.standings);
    if(!data.available)error('台球室暂时不可用，其他玩法可以正常使用');
    const list=$('savedGames');list.replaceChildren();
    for(const saved of data.games){
      const card=document.createElement('div');card.className='saved-game';
      const title=document.createElement('strong');title.textContent=saved.players.map(p=>labels[p]).join(' & ');
      const aiOnly=!saved.players.includes('user');
      const stage=saved.status==='finished'?'等待下一局':saved.status==='paused'?'已暂停':'正在打球';
      const description=document.createElement('p');description.textContent=`${stage} · ${saved.score.join(' : ')} · 第${saved.rack}${saved.match.format==='best_of_3'?' / 3':''}局${aiOnly?' · 不影响你开桌':''}`;
      const button=document.createElement('button');button.className='quiet';button.textContent=saved.players.includes('user')?'继续比赛':'旁观比赛';button.onclick=()=>loadGame(saved.id).catch(e=>error(e.message));
      const finish=element('button','quiet end-saved-match','中止本场');
      const continueAI=aiOnly&&saved.status!=='running'?element('button','quiet continue-ai-match',saved.status==='finished'?'让他们打下一局 ↗':'让他们继续打 ↗'):null;
      if(continueAI){
        continueAI.disabled=!data.available||current?.status==='running';
        continueAI.title=current?.status==='running'?'先暂停你正在打的比赛，再让两位伴侣上桌。':'';
        continueAI.onclick=async()=>{
          button.disabled=finish.disabled=continueAI.disabled=true;error('');
          try{
            await request(`/api/billiards/${saved.id}/${saved.status==='finished'?'next':'resume'}`,{client,breaker:saved.rack%2});
            await loadGame(saved.id);
          }catch(e){error(e.message);button.disabled=finish.disabled=false;continueAI.disabled=!data.available||current?.status==='running';}
        };
      }
      finish.onclick=async()=>{
        if(!confirm(`中止 ${saved.players.map(p=>labels[p]).join(' 与 ')} 的这场比赛？已完成的胜局和聊天会保留在历史里，本场不计整场胜负。`))return;
        button.disabled=finish.disabled=true;if(continueAI)continueAI.disabled=true;error('');
        try{await request(`/api/billiards/${saved.id}/finish`,{client});await lobby();}
        catch(e){error(e.message);button.disabled=finish.disabled=false;if(continueAI)continueAI.disabled=!data.available||current?.status==='running';}
      };
      const copy=element('div','saved-game-copy'),actions=element('div','saved-game-actions');
      copy.append(title,description);actions.append(button,finish);card.append(copy);
      if(continueAI)card.append(continueAI);
      card.append(actions);list.append(card);
    }
    $('activeMatches').hidden=!data.games.length;
  }
  function updateChallenge(){
    for(const button of document.querySelectorAll('[data-opponent]')){
      button.disabled=!lobbyData?.available;
      button.setAttribute('aria-pressed',String(button.dataset.opponent===selectedOpponent));
    }
    const current=lobbyData?.games.find(g=>g.players.includes('user'));
    const continuing=current?.players.includes(selectedOpponent);
    $('startChallenge').disabled=!lobbyData?.available||!!(current&&!continuing);
    $('startChallenge').textContent=continuing?`继续与 ${labels[selectedOpponent]} 的比赛`:'开始挑战 ↗';
    $('challengerName').textContent=current?`与 ${labels[current.players.find(p=>p!=='user')]} 的比赛尚未结束`:lobbyData?.games.some(g=>!g.players.includes('user')&&g.status==='running')?'你开桌时，他们会暂停让位':`${labels.user}出战`;
    const aiMatch=lobbyData?.games.find(g=>!g.players.includes('user'));
    const occupied=current?.status==='running';
    $('spectatorPanel').hidden=!!aiMatch;
    $('watchCompanions').disabled=!lobbyData?.available||occupied;
    $('spectatorHint').textContent=occupied?'先暂停你正在打的比赛，再让两位伴侣上桌。':`${labels.aion} 与 ${labels.connor}对打，你坐在桌边看。`;
  }
  function element(tag,className,text){const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node;}
  function renderStandings(stats){
    const list=$('standings');list.replaceChildren();
    for(const stat of stats){
      const rank=1+stats.filter(s=>s.wins>stat.wins).length;
      const card=element('article','contender-card'+(stat.leader?' leader':''));
      card.append(element('span','contender-rank',stat.leader?'♛ 胜局榜首':`NO. 0${rank}`));
      const portrait=element('img','contender-portrait');portrait.src=avatar(stat.actor);portrait.alt='';card.append(portrait);
      card.append(element('h3','contender-name',labels[stat.actor]));
      const record=element('div','contender-record'),wins=element('span','record-side'),losses=element('span','record-side');
      wins.append(element('b','',stat.wins),element('span','','胜'));losses.append(element('b','',stat.losses),element('span','','负'));record.append(wins,element('i','','/'),losses);
      const meta=element('div','contender-meta'),rate=element('span'),matches=element('span');
      rate.append('胜率 ',element('b','',stat.win_rate===null?'—':`${stat.win_rate}%`));
      matches.append('整场 ',element('b','',`${stat.match_wins}胜 ${stat.match_losses}负`));
      meta.append(rate,matches);card.append(record,meta);list.append(card);
    }
  }
  const dateLabel=timestamp=>new Date(timestamp*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
  async function loadHistory(reset=false){
    if(historyLoading)return;historyLoading=true;
    if(reset){historyOffset=0;$('historyList').replaceChildren();}
    $('historyMore').disabled=true;$('historyState').textContent='正在读取战绩…';
    try{
      const data=await request(`/api/billiards/history?offset=${historyOffset}&limit=10`);
      for(const saved of data.games){
        const entry=element('details','history-entry'),head=element('summary');
        head.append(element('strong','history-match-title',saved.players.map(p=>labels[p]).join('  vs  ')),element('span','history-score',saved.score.join(' : ')));
        const match=saved.match;
        const result=match.status==='completed'?`${labels[saved.players[match.winner]]}夺冠`:match.status==='abandoned'?'本场中止':match.status==='active'?(saved.status==='running'?'整场进行中':'整场待续'):'旧版自由对局';
        head.append(element('span','history-caption',`${dateLabel(saved.updated_at)} · ${result}`));
        const detail=element('div','history-detail');entry.append(head,detail);
        entry.addEventListener('toggle',async()=>{
          if(!entry.open||entry.dataset.loaded)return;entry.dataset.loaded='loading';detail.textContent='正在读取比赛详情…';
          try{
            const full=await request(`/api/billiards/${saved.id}`);detail.replaceChildren();
            for(const report of full.result_reports||[])detail.append(element('div','history-rack',`第${report.rack}局 · ${labels[full.players[report.winner]]}获胜 · ${report.shots}杆 · ${report.reason}`));
            if(!(full.result_reports||[]).length)detail.append(element('p','','这场没有已结算的单局战报。'));
            detail.append(element('h3','','桌边聊天'));
            const chat=element('div','history-chat');
            for(const message of full.messages){const line=element('p');line.append(element('strong','',labels[message.sender]||message.sender),element('small','',`第${message.rack}局`),element('span','',`\n${message.text}`));chat.append(line);}
            if(!full.messages.length)chat.textContent='没有桌边聊天记录。';detail.append(chat);entry.dataset.loaded='yes';
          }catch(e){delete entry.dataset.loaded;detail.textContent=e.message+'，收起后重试。';}
        });
        $('historyList').append(entry);
      }
      historyOffset+=data.games.length;$('historyMore').hidden=!data.has_more;
      $('historyState').textContent=data.total?`已显示 ${historyOffset} / ${data.total} 场`:'还没有历史战绩，第一场交锋等你来。';
    }catch(e){$('historyState').textContent=e.message;$('historyMore').hidden=false;}
    finally{historyLoading=false;$('historyMore').disabled=false;}
  }
  $('historyBtn').onclick=()=>{$('historyDialog').showModal();loadHistory(true);};
  $('historyClose').onclick=()=>$('historyDialog').close();
  $('historyMore').onclick=()=>loadHistory();
  async function submit(action,local){
    if(!canControl())return;
    busy=true;status();error('');
    if(action.type==='confirmPlacement'){const cue=local.balls.find(b=>b.id===0);action={...action,position:{x:cue.x,y:cue.y}};}
    try{
      const data=await request(endpoint('action'),{client,revision:game.revision,action_id:makeId(),action});
      busy=false;apply(data,action.type==='shot');
    }catch(e){busy=false;error(e.message);await poll();Billiards.session.load(game.state);status();}
  }
  async function pause(manual=false){
    if(!game||game.status!=='running'||(game.players.includes('user')?!game.controlled_here:!manual))return;
    Billiards.session.cancelCharge();
    try{const data=await request(endpoint('pause'),{client},true);apply(data);}catch(e){error(e.message);}
  }
  async function poll(){
    if(!game||polling||!visible)return;
    const pollingGame=game.id;
    polling=true;
    try{
      let data;
      if(game.controlled_here&&game.status==='running'&&Date.now()-heartbeatAt>6000){data=await request(endpoint('heartbeat'),{client});heartbeatAt=Date.now();}
      else data=await request(`/api/billiards/${game.id}?client=${encodeURIComponent(client)}`);
      if(!busy&&game.id===pollingGame)apply(data,true);
    }catch(e){error(e.message);game.controlled_here=false;status();}
    finally{polling=false;}
  }
  async function restart(){
    if(busy)return;
    const aiOnly=!game.players.includes('user');
    if(aiOnly&&game.state.phase!=='over'){error('这是伴侣们的球局，可以继续旁观');return;}
    if(game.match?.status!=='active')return;
    const over=game.state.phase==='over';
    if(!over&&!confirm('重新摆当前这一局？局号、比分和聊天保留，本局当前进度不计胜负。'))return;
    busy=true;status();
    try{
      animating=false;eventId='';
      const options=aiOnly?{client,breaker:game.rack%2}:{client,difficulty:$('difficulty').value,accuracy:+$('accuracy').value,mode:$('rulesMode').value,breaker:+$('breaker').value};
      const data=await request(endpoint(over?'next':'restart'),options);
      busy=false;apply(data);Billiards.session.load(data.state);
    }catch(e){busy=false;error(e.message);status();}
  }
  function setVisible(){
    const next=pageVisible&&appForeground&&!document.hidden;
    if(next===visible)return;
    visible=next;
    if(!visible){tableRecorder.cancel();activeSpeech?.();pause();}
    else{animating=false;if(game){Billiards.session.load(game.state);poll();}}
  }
  window.onAionSubPageVisibilityChanged=value=>{pageVisible=!!value;setVisible();};
  window.onAionAppForegroundChanged=value=>{appForeground=!!value;setVisible();};
  document.addEventListener('visibilitychange',setVisible);
  window.addEventListener('pagehide',()=>{if(game?.controlled_here&&game.status==='running')navigator.sendBeacon(endpoint('pause'),new Blob([JSON.stringify({client})],{type:'application/json'}));});
  $('pauseBtn').onclick=async()=>{
    try{if(game.status==='running'&&(game.controlled_here||!game.players.includes('user')))await pause(true);else apply(await request(endpoint('resume'),{client}));}catch(e){error(e.message);}
  };
  $('roomCoverText').onclick=()=>{if(game?.status==='paused')$('pauseBtn').click();};
  $('finishMatch').onclick=async()=>{
    if(!confirm('结束这场球局并离开？已完成的胜负和聊天记录会保留。'))return;
    try{await request(endpoint('finish'),{client});location.href='/billiards';}catch(e){error(e.message);}
  };
  $('roomBack').onclick=async()=>{await pause();location.href='/billiards';};
  $('endLobbyBtn').onclick=()=>location.href='/billiards';
  $('endDialog').addEventListener('cancel',e=>{if(game?.match?.status!=='active')e.preventDefault();});
  $('lobbyBack').onclick=()=>location.href='/playground';
  const portraitRoom=()=>matchMedia('(max-width:760px) and (orientation:portrait)').matches;
  function toggleTableChat(open){
    $('roomChat').classList.toggle('mobile-open',open);
    $('roomChat').classList.toggle('chat-open',open);
    $('gameApp').classList.toggle('chat-open',open);
    $('chatDismiss').hidden=!open;
    $('tableChatToggle').setAttribute('aria-expanded',String(open));
    if(open)$('tableChatToggle').classList.remove('unread');
    layoutBubbles();
  }
  window.addEventListener('resize',layoutBubbles);
  $('tableChatToggle').onclick=()=>toggleTableChat(!$('roomChat').classList.contains('mobile-open'));
  $('chatDismiss').onclick=()=>toggleTableChat(false);
  matchMedia('(max-width:760px) and (orientation:portrait)').addEventListener('change',()=>toggleTableChat(false));
  async function sendTableText(text,destination={id:game?.id,target:''}){
    if(!text||!destination.id)return;
    chatBusy=true;$('sendChat').disabled=true;status();
    try{await request(`/api/billiards/${destination.id}/chat`,{text,target:destination.target});await poll();}
    finally{chatBusy=false;$('sendChat').disabled=false;status();}
  }
  $('chatForm').onsubmit=async e=>{
    e.preventDefault();const text=$('tableChatInput').value.trim();if(!text||!game||chatBusy)return;
    $('tableChatInput').value='';
    try{await sendTableText(text);}catch(err){error(err.message);$('tableChatInput').value=text;}
  };
  const tableRecorder=Billiards.Voice.bind({button:$('tableVoiceHold'),hint:$('tableVoiceHint'),
    destination:()=>visible&&game?.players.includes('user')&&!chatBusy?{id:game.id,target:game.players.find(p=>p!=='user')}:null,
    send:async(text,target)=>{try{await sendTableText(text,target);}catch(e){$('tableChatInput').value=text;toggleTableChat(true);throw e;}},onRecording:active=>{voiceRecording=active;if(active){activeSpeech?.();Billiards.session?.cancelCharge();}},
    onError:text=>error(text)});
  window.addEventListener('pagehide',()=>{tableRecorder.cancel();activeSpeech?.();});
  $('tableChatInput').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();$('chatForm').requestSubmit();}};
  for(const button of document.querySelectorAll('[data-opponent]'))button.onclick=()=>{
    selectedOpponent=button.dataset.opponent;
    updateChallenge();
  };
  $('startChallenge').onclick=async()=>{
    $('startChallenge').disabled=true;
    try{
      const current=lobbyData?.games.find(g=>g.players.includes('user')&&g.players.includes(selectedOpponent));
      if(current){await loadGame(current.id);return;}
      const data=await request('/api/billiards',{opponent:selectedOpponent,difficulty:$('lobbyDifficulty').value});await loadGame(data.id);apply(await request(endpoint('resume'),{client}));
    }
    catch(e){error(e.message);if(!game)$('startChallenge').disabled=false;}
  };
  $('watchCompanions').onclick=async()=>{
    $('watchCompanions').disabled=true;error('');
    try{
      const data=await request('/api/billiards/watch',{difficulty:$('lobbyDifficulty').value});
      await loadGame(data.id);
    }catch(e){error(e.message);updateChallenge();}
  };
  request('/api/chatroom/config').then(config=>chatConfig=config).catch(()=>{});
  const id=new URLSearchParams(location.search).get('game');
  (id?loadGame(id):lobby()).catch(e=>error(e.message));
  setInterval(poll,1000);
})();

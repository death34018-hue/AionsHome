const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, 'static/theater-studio.js'), 'utf8');
const follow = require('./static/theater-follow.js');
function section(start, end) { return source.slice(source.indexOf(start), source.indexOf(end)); }

test('reading cues prefer saved source positions, support old character counts and skip unindexed legacy audio', () => {
  const player={seq:1,segments:[{chars:320},{chars:360}]};
  assert.deepEqual(follow.segmentRange(player),{start:320,end:680});
  player.segments[1]={chars:360,start:342,end:702};
  assert.equal(follow.segmentRange(player),player.segments[1]);
  player.seq=2;player.segments.push({chars:310});
  assert.deepEqual(follow.segmentRange(player),{start:702,end:1012});
  player.legacy=true;assert.equal(follow.segmentRange(player),null);
  assert.equal(follow.segmentRange({seq:1,segments:[{chars:0},{chars:300}]}),null);
});

test('paragraph anchors retain blank lines, Unicode and image-slice source offsets', () => {
  const text='🌙第一段\n\n  \n第二段\n继续';
  const html=follow.paragraphs(text,42,s=>s);
  const matches=[...html.matchAll(/data-source-start="(\d+)" data-source-end="(\d+)">([\s\S]*?)<\/p>/g)];
  assert.equal(matches.length,2);
  for(const [,start,end,body] of matches)assert.equal(text.slice(Number(start)-42,Number(end)-42),body);
  assert.equal(matches[1][3],'第二段\n继续');
});

test('one audio segment scrolls proportionally through its text and keeps the reading position centered', () => {
  const reader={scrollTop:0,scrollHeight:5000,clientHeight:600,before(){},addEventListener(){},
    getBoundingClientRect:()=>({top:100,height:600}),scrollTo({top}){this.scrollTop=top;}};
  const node={nodeType:3,length:360,isConnected:true};
  const range={startContainer:node,setStart(){},setEnd(){},getClientRects:()=>[
    {top:1000-reader.scrollTop,width:200,height:20},
    {top:1800-reader.scrollTop,width:200,height:20}
  ]};
  const state={root:{querySelectorAll:()=>[]},content:'文'.repeat(360),visible:true,currentTime:0,duration:30,
    player:{id:'audio',seq:0,paused:false,segments:[{chars:360}]}};
  const c=vm.createContext({module:{exports:{}},NodeFilter:{SHOW_TEXT:4,SHOW_ELEMENT:1},
    localStorage:{getItem:()=>null},clearTimeout(){},matchMedia:()=>({matches:true}),
    document:{createElement:()=>({append(){},setAttribute(){},addEventListener(){},dataset:{}}),
      addEventListener(){},createRange:()=>range,createTreeWalker(){let read=false;return {nextNode(){if(read)return null;read=true;return node;}};}}});
  vm.runInContext(fs.readFileSync(require('node:path').join(__dirname,'static/theater-follow.js'),'utf8'),c);
  const controller=c.module.exports.mount(reader,()=>state);
  controller.update();assert.equal(reader.scrollTop,610,'first line starts in the center');
  state.currentTime=15;controller.update();assert.equal(reader.scrollTop,1010,'half the audio reaches half the text height');
  state.currentTime=30;controller.update();assert.equal(reader.scrollTop,1410,'last line reaches the center');
  state.player.paused=true;state.currentTime=5;controller.update();assert.equal(reader.scrollTop,1410,'pause prevents automatic movement');
  controller.resume();assert(Math.abs(reader.scrollTop-(610+800/6))<1,'explicit seeking uses the requested time while paused');
});

test('starting a chapter collapses the cleared multiline input, failure keeps the draft',async()=>{
  const input={value:'本章想法\n补充场景\n更多细节',style:{height:'120px'},get scrollHeight(){return this.value?120:42;}};
  const c=vm.createContext({planning:()=>false,chapter:()=>({id:'chapter'}),player:null,
    $:()=>input,request:async()=>({}),refresh:async()=>{}});
  const shell=fs.readFileSync(require('node:path').join(__dirname,'static/theater.html'),'utf8');
  vm.runInContext(shell.slice(shell.indexOf('function autoResize('),shell.indexOf('/* ── 模型切换')),c);
  vm.runInContext(section('  async function write(', '  async function editChapter('),c);
  await c.write();
  assert.equal(input.value,'');assert.equal(input.style.height,'42px');
  input.value='保留这段想法';input.style.height='120px';
  c.request=async()=>{throw Error('网络失败');};
  await assert.rejects(c.write(),/网络失败/);
  assert.equal(input.value,'保留这段想法');assert.equal(input.style.height,'120px');
});

test('continuation requires confirmation and cancellation keeps the input without a request',async()=>{
  const input={value:'只补充最后一幕'};let calls=0,confirmations=0,accepted=false;
  const current={id:'chapter',content:'已有正文',status:'draft'};
  const c=vm.createContext({planning:()=>false,chapter:()=>current,player:null,
    confirm:()=>false,confirmAction:async()=>{confirmations++;return accepted;},$:()=>input,
    request:async()=>{calls++;},autoResize(){},refresh:async()=>{}});
  vm.runInContext(section('  async function write(', '  async function editChapter('),c);
  await c.write();assert.equal(confirmations,1);assert.equal(calls,0);assert.equal(input.value,'只补充最后一幕');
  accepted=true;await c.write();assert.equal(confirmations,2);assert.equal(calls,1);assert.equal(input.value,'');
  current.writing=true;await c.write();assert.equal(calls,1);assert.equal(confirmations,2);
});

test('rewrite waits for the page dialog even when native confirms are suppressed; dismissing preserves the chapter',async()=>{
  const dialogs=[],calls=[],input={value:'保留这段想法'};
  const c=vm.createContext({planning:()=>false,chapter:()=>({id:'chapter',content:'旧正文'}),
    player:{source:'chapter'},confirm:()=>false,escHtml:s=>s,$:()=>input,
    stopPlayer:()=>calls.push('stop'),request:async(...args)=>calls.push(args),autoResize(){},refresh:async()=>{},
    document:{body:{append:d=>dialogs.push(d)},createElement:()=>{
      const d=new EventTarget(),yes={};
      Object.assign(d,{returnValue:'',setAttribute(){},querySelector:()=>yes,
        showModal(){this.open=true;},remove(){this.removed=true;},
        close(value){if(value!==undefined)this.returnValue=value;this.open=false;this.dispatchEvent(new Event('close'));}});
      return d;
    }}});
  vm.runInContext(section('  function confirmAction(', '  function updateReaderComposer('),c);
  vm.runInContext(section('  async function write(', '  async function editChapter('),c);
  for(const choice of ['cancel',undefined,'confirm']) {
    const pending=c.write(true),dialog=dialogs.at(-1);
    assert.equal(dialog?.open,true,'an in-page confirmation must be visible');
    assert.equal(calls.length,0,'no rewrite or playback stop before confirmation');
    if(choice==='confirm')dialog.querySelector('[data-confirm]').onclick();
    else dialog.close(choice);
    await pending;
    assert.equal(dialog.removed,true);
    if(choice!=='confirm')assert.equal(input.value,'保留这段想法');
  }
  assert.equal(calls[0],'stop');
  assert.equal(calls[1][0],'/chapters/chapter/write');
  assert.equal(calls[1][2].rewrite,true);
  assert.equal(calls.length,2);
  assert.equal(input.value,'');
});

test('discussion messages use configured names and current persona with separate alignment',()=>{
  const c=vm.createContext({discussionNames:{user_name:'读者甲',ai_name:'默认角色'},
    personas:[{id:'chosen',name:'角色乙'}],currentPersonaId:'chosen',book:{writer_name:'旧角色'},
    discussion:{status:'idle'},escHtml:s=>String(s).replaceAll('<','&lt;').replaceAll('>','&gt;')});
  vm.runInContext(section('  function discussionMessage(', '  function renderNovel()'),c);
  const user=c.discussionMessage({id:'u',role:'user',content:'<想法>'});
  assert.match(user,/studio-talk user/);assert.match(user,/读者甲/);assert.match(user,/&lt;想法&gt;/);
  assert.match(user,/Studio.talkAction\('edit','u'\)/);assert.match(user,/Studio.talkAction\('delete','u'\)/);
  assert.doesNotMatch(user,/重新生成/);
  const ai=c.discussionMessage({id:'a',role:'assistant',content:'继续聊'});
  assert.match(ai,/studio-talk assistant/);assert.match(ai,/角色乙/);assert.doesNotMatch(ai,/旧角色/);
  assert.match(ai,/Studio.talkAction\('regenerate','a'\)/);assert.match(ai,/Studio.talkAction\('copy','a'\)/);
});

test('discussion actions save edited text, regenerate and reject edits while generating',async()=>{
  const calls=[];let submit;
  const c=vm.createContext({discussion:{messages:[{id:'u',role:'user',content:'原文'},{id:'a',role:'assistant',content:'回复'}],status:'idle'},
    book:{id:'book'},currentConvId:'book',outlineBusy:false,talkBusy:false,chapters:[],escHtml:s=>s,
    document:{querySelectorAll:()=>[]},modeUI(){},refresh:async()=>{},showToast:s=>calls.push(s),
    request:async(...args)=>calls.push(args),modal:(title,html,label,fn)=>{submit=fn;},$:()=>({value:' 修改后 '})});
  vm.runInContext(section('  async function talkAction(', '  async function discussionMode()'),c);
  await c.talkAction('edit','u');await submit();
  assert.equal(calls[0][0],'/books/book/discussion/messages/u');
  assert.equal(calls[0][1],'PUT');assert.equal(calls[0][2].content,'修改后');
  await c.talkAction('regenerate','a');
  assert.equal(calls[1][0],'/books/book/discussion/messages/a/regenerate');
  c.discussion.status='running';await c.talkAction('delete','u');
  assert.match(calls[2],/先等/);assert.equal(calls.length,3);
});

test('illustrations save through the parent App bridge or a browser download',async()=>{
  const calls=[];
  const parent={AionImageSaver:{save:(...args)=>calls.push(args)}};
  const c=vm.createContext({window:{parent,top:{}},location:{href:'https://home.test/theater'},
    fetch:async()=>({ok:true,blob:async()=>({})}),
    FileReader:class{readAsDataURL(){this.result='data:image/png;base64,IMAGE';this.onload();}},
    URL:class extends URL{static createObjectURL(){return 'blob:picture';}static revokeObjectURL(){}},
    document:{body:{append(){}},createElement(){return {click(){calls.push([this.href,this.download]);},remove(){}};}},
    setTimeout(){},showToast(){}});
  vm.runInContext(section('  async function saveIllustration(', '  function button('),c);
  await c.saveIllustration('/uploads/album/story.png');
  assert.deepEqual(calls[0],['IMAGE','story.png']);
  delete parent.AionImageSaver;
  await c.saveIllustration('/uploads/album/story.png');
  assert.deepEqual(calls[1],['blob:picture','story.png']);
  c.fetch=async()=>({ok:false});
  await assert.rejects(c.saveIllustration('/missing.png'),/图片下载失败/);
  assert.equal(calls.length,2);
});

test('reader controls follow the visible chapter even when the book has pending discussion', () => {
  const elements=new Map();
  const c=vm.createContext({book:{mode:'novel',phase:'discussion'},chapters:[{}],planningView:false,
    outlineBusy:false,talkBusy:false,discussion:{status:'idle'},isStreaming:false,readingFollow:null,
    document:{body:{classList:{toggle(){}}}},novel:()=>true,chapter:()=>({status:'ready'}),
    updateReaderComposer(){},renderDirectory(){},toolbar(){},renderOutlineStatus(){},$:id=>{if(!elements.has(id))elements.set(id,{setAttribute(){}});return elements.get(id);}});
  vm.runInContext(section('  function modeUI()', '  function renderOutlineStatus()'),c);
  c.modeUI();assert.equal(elements.get('sendBtn').textContent,'下一章');
  c.chapter=()=>({status:'planned'});c.modeUI();assert.equal(elements.get('sendBtn').textContent,'开始写');
  c.chapter=()=>({status:'draft'});c.modeUI();assert.equal(elements.get('sendBtn').textContent,'续写本章');
  c.planningView=true;c.modeUI();assert.equal(elements.get('sendBtn').textContent,'➤');
  assert.equal(elements.get('input').placeholder,'聊聊剧情…');
});

test('outline feedback appears immediately, survives rendering, and reports success or failure',async()=>{
  for(const fails of [false,true]) {
    let finish,calls=0;
    const status={hidden:true,dataset:{},innerHTML:''},input={value:''},messages={scrollHeight:1000};
    const pending=new Promise((resolve,reject)=>{finish=fails?()=>reject(Error('模型连接失败')):()=>resolve({book:{id:'book',phase:'writing'},chapters:[],outline_draft:{plans:[]}});});
    const c=vm.createContext({book:{id:'book',premise:'故事'},currentConvId:'book',outlineBusy:false,outlineFeedback:null,outlineDraft:null,
      talkBusy:false,discussion:{status:'idle',messages:[{content:'本次脑洞'}]},chapters:[],planningView:true,outlineEditor(){},
      novel:()=>true,escHtml:s=>s,$:id=>id==='studioOutlineStatus'?status:id==='input'?input:messages,
      request:()=>{calls++;return pending;},showToast(){},renderPlanning(){},
      setPlanningView(value){c.planningView=value;},modeUI(){c.renderOutlineStatus();}});
    vm.runInContext(section('  function renderOutlineStatus()', '  function renderDirectory()')+
                    section('  async function outline(', '  async function confirmOutline()'),c);
    const task=c.outline();
    assert.equal(status.hidden,false);assert.equal(status.dataset.state,'running');assert.match(status.innerHTML,/正在生成大纲/);
    await c.outline();assert.equal(calls,1);
    c.renderOutlineStatus();assert.equal(status.dataset.state,'running');
    c.currentConvId='another';c.renderOutlineStatus();assert.equal(status.hidden,true);
    c.currentConvId='book';c.renderOutlineStatus();assert.equal(status.hidden,false);
    finish();
    if(fails)await assert.rejects(task,/模型连接失败/);else await task;
    assert.equal(c.outlineBusy,false);assert.equal(status.dataset.state,fails?'error':'done');
    assert.match(status.innerHTML,fails?/模型连接失败/:/大纲已拟好/);
    c.planningView=false;c.renderOutlineStatus();assert.equal(status.hidden,true);
  }
});

test('next chapter remains navigation while the story has unconfirmed discussion', () => {
  let next=0,talk=0;
  const c=vm.createContext({currentConvId:'story',novel:()=>true,planningView:false,
    chapter:()=>({status:'ready'}),action:fn=>fn(),next(){next++;},discuss(){talk++;},
    write(){throw Error('Next chapter must not trigger writing or outline approval');}});
  vm.runInContext(section('  send=function() {','  async function settings()'),c);
  c.send();assert.equal(next,1);
  c.planningView=true;c.send();assert.equal(talk,1);
});

test('chapter completion controls preserve drafts and allow reopening finished chapters',async()=>{
  const elements=new Map();let current={id:'chapter',status:'draft',content:'故事正文',writing:false};
  const calls=[];
  const c=vm.createContext({book:{id:'story'},currentConvId:'story',planningView:false,outlineBusy:false,
    chapter:()=>current,novel:()=>true,request:async(...args)=>calls.push(args),refresh:async()=>calls.push('refresh'),
    $:id=>{if(!elements.has(id))elements.set(id,{});return elements.get(id);},
    button:(label,handler)=>`<button onclick="${handler}">${label}</button>`});
  vm.runInContext(section('  function toolbar()', '  function renderPlanning()'),c);
  vm.runInContext(section('  async function completeChapter(', '  async function editChapter('),c);
  c.toolbar();assert.match(elements.get('studioToolbar').innerHTML,/本章已写完/);
  await c.completeChapter(true);
  assert.equal(calls[0][0],'/chapters/chapter/completion');assert.equal(calls[0][2].completed,true);
  current={...current,status:'ready'};c.toolbar();
  assert.match(elements.get('studioToolbar').innerHTML,/本章还没写完/);
  current.writing=true;c.toolbar();
  assert.doesNotMatch(elements.get('studioToolbar').innerHTML,/本章还没写完|本章已写完/);
  await c.completeChapter(false);assert.equal(calls.length,2);
});

test('deleting a playing message or novel stops the shared player without affecting other stories',()=>{
  const calls=[];
  const c=vm.createContext({player:{source:'message',cid:'story'},
    stopPlayer(){calls.push('stop');c.player=null;},oldDiscardMessageTTS:(...args)=>calls.push(args),
    oldDiscardConversationTTS:id=>calls.push(id)});
  vm.runInContext(section('  discardMessageTTS = function(', '  handleSync = function('),c);
  c.discardMessageTTS('other',true);assert.equal(c.player.source,'message');
  c.discardMessageTTS('message',true);assert.equal(c.player,null);
  c.player={source:'chapter',cid:'novel'};
  c.discardConversationTTS('other-story');assert.equal(c.player.source,'chapter');
  c.discardConversationTTS('novel');assert.equal(c.player,null);
  assert.equal(calls.filter(x=>x==='stop').length,2);
});

test('native chapter playback failure falls back to the same cached segment and keeps its position',async()=>{
  const {installAionTtsAudio}=require('./static/native-tts-audio.js');
  const browserPlayers=[],nativeCalls=[],notices=[];
  class BrowserAudio {
    constructor(url){this.src=url;this.currentTime=0;this.duration=60;browserPlayers.push(this);}
    play(){this.playing=true;return Promise.resolve();}
    pause(){this.playing=false;}
  }
  const root={Audio:BrowserAudio,AionTtsAudio:{
    prepareAudio(id,url){nativeCalls.push({id,url});return true;},stop(){},pauseAudio(){},resumeAudio(){},seekAudio(){},
  }};
  installAionTtsAudio(root);
  const c=vm.createContext({window:root,Audio:BrowserAudio,audio:null,playbackToken:0,
    player:{id:'recording',status:'ready',seq:1,at:12,paused:false,segments:[{url:'/zero.mp3'},{url:'/one.mp3'},{url:'/two.mp3'}]},
    updateProgress(){},saveListen(){},showToast:text=>notices.push(text),request(){throw Error('Playback must not request synthesis');}});
  vm.runInContext(section('  function releaseAudio()', '  function stopPlayer()')+
    section('  function playSegment()', '  async function pollAudio()'),c);
  c.playSegment();
  assert.equal(nativeCalls.length,1);assert.equal(nativeCalls[0].url,'/one.mp3');
  root.onAionNativeTtsEvent({playerId:nativeCalls[0].id,type:'error'});
  assert.equal(browserPlayers.length,1);assert.equal(browserPlayers[0].src,'/one.mp3');
  browserPlayers[0].onloadedmetadata();assert.equal(browserPlayers[0].currentTime,12);
  assert.equal(c.player.seq,1);assert.equal(c.player.paused,false);assert.deepEqual(notices,[]);
  root.onAionNativeTtsEvent({playerId:nativeCalls[0].id,type:'ended'});
  assert.equal(c.player.seq,1,'late native events must not skip the browser segment');
  browserPlayers[0].onended();
  assert.equal(nativeCalls.length,1);assert.equal(browserPlayers[1].src,'/two.mp3');
  browserPlayers[1].onerror();
  assert.equal(c.player.paused,true);assert.equal(browserPlayers.length,2);assert.equal(notices.length,1);
});

test('a completed chapter with an old review flag still reads as completed', () => {
  const elements=new Map([['messages',{querySelector:()=>({})}]]);
  const current={id:'chapter-six',number:6,title:'第六章',content:'读完的正文',status:'ready',needs_review:true,images:[]};
  const c=vm.createContext({planningView:false,book:{},chapter:()=>current,chapters:[current],
    conversations:[{id:'book',title:'故事'}],currentConvId:'book',selected:current.id,statusNames:{ready:'已完成'},
    readingFollow:null,TheaterFollow:{paragraphs:text=>`<p>${text}</p>`},escHtml:text=>text,
    button:(label,handler)=>`<button onclick="${handler}">${label}</button>`,
    $:id=>{if(!elements.has(id))elements.set(id,{innerHTML:'',hidden:false});return elements.get(id);}});
  vm.runInContext(section('  function renderDirectory()', '  function toolbar()'),c);
  vm.runInContext(section('  function renderNovel()', '  function backgroundPoll()'),c);
  c.renderDirectory();
  c.renderNovel();
  assert.match(elements.get('studioDirectory').innerHTML,/已完成/);
  assert.doesNotMatch(elements.get('studioDirectory').innerHTML,/待确认/);
  assert.doesNotMatch(elements.get('novelNotice').innerHTML,/确认衔接无误/);
  assert.equal(elements.get('novelCompletion').innerHTML,'');
});

test('dialogue replay checks existing recordings without requiring a selected voice',async()=>{
  const calls=[];
  const c=vm.createContext({ttsVoice:'',novel:()=>false,player:null,chapters:[],conversations:[],currentConvId:'story',
    request:async(...args)=>{calls.push(args);return {id:'legacy_msg',status:'ready',segments:[{seq:0,url:'/existing.mp3'}]};},
    stopPlayer(){},localStorage:{getItem:()=>null},$:()=>({classList:{add(){}}}),
    document:{querySelector:()=>({classList:{add(){}}})},pollAudio:async()=>calls.push('play'),showToast:s=>calls.push(s)});
  vm.runInContext(section('  async function speak(', '  replayTTS='),c);
  await c.speak('tm_old');
  assert.equal(calls[0][0],'/speech/tm_old');assert.equal(calls[0][2].prefer_cached,true);
  assert.equal(calls[1],'play');assert.equal(c.player.id,'legacy_msg');
  c.novel=()=>true;c.player=null;
  await c.speak('chapter');
  assert.equal(calls[2][0],'/speech/chapter');assert.equal(calls[3],'play');
});

test('chapter without audio only synthesizes after confirmation; cancel keeps current playback',async()=>{
  const calls=[];let accepted=false,confirmations=0;
  const previous={id:'previous'};
  const c=vm.createContext({ttsVoice:'new',novel:()=>true,player:previous,chapters:[],conversations:[],currentConvId:'story',
    confirm:()=>false,confirmAction:async(title,message)=>{assert.match(message,/本章节没有语音/);confirmations++;return accepted;},
    request:async(...args)=>{calls.push(args);return args[2].allow_generation
      ? {id:'new',status:'running',segments:[]} : {needs_confirmation:true};},
    stopPlayer(){c.player=null;},localStorage:{getItem:()=>null},$:()=>({classList:{add(){}}}),
    document:{querySelector:()=>({classList:{add(){}}})},pollAudio:async()=>{},showToast(){}});
  vm.runInContext(section('  async function speak(', '  replayTTS='),c);
  await c.speak('chapter');
  assert.equal(confirmations,1);assert.equal(calls.length,1);assert.equal(calls[0][2].allow_generation,false);
  assert.equal(c.player,previous);
  accepted=true;await c.speak('chapter');
  assert.equal(confirmations,2);assert.equal(calls.length,3);assert.equal(calls[2][2].allow_generation,true);
  assert.equal(c.player.id,'new');
});

test('first outline confirms in the current story; existing outlines clearly offer a new version',async()=>{
  for(const existing of [false,true]) {
    const saved = new Map([['studio_view_story','discussion']]);
    const calls=[],fields={consensus:'共识',outline:'走向',title:'新版本',chapterTitle:'初遇',plan:'雨夜',min:'5000',max:'8000'};
    const plan={title:'初遇',plan:'雨夜',min_chars:5000,max_chars:8000};
    const draft={revision:'draft',consensus:'共识',outline:'走向',settings:{target_chars:6500},plans:[plan]};
    const chapterPanel={querySelector:selector=>({value:fields[selector.match(/data-field="(.*?)"/)[1]]})};
    const panel={...chapterPanel,querySelectorAll:selector=>selector==='.outline-plan'?[chapterPanel]:[]};
    const controls=new Map();
    const dialog={innerHTML:'',querySelector:selector=>{
      if(selector.startsWith('[data-outline-page='))return panel;
      if(!controls.has(selector))controls.set(selector,{});
      return controls.get(selector);
    },querySelectorAll:()=>[],showModal(){},close(){}};
    const c=vm.createContext({book:{id:'story',outline:existing?'旧大纲':'',target_chars:6500},
      chapters:existing?[{id:'chapter',...plan}]:[],selected:'chapter',outlineDraft:draft,planningView:true,currentConvId:'story',
      conversations:[{id:'story',title:'原名'}],modes:new Map(),$:()=>dialog,escHtml:s=>s,
      localStorage:{setItem:(key,value)=>saved.set(key,value)},
      request:async(path,method,body)=>{calls.push({path,method,body});return method==='PUT'?draft:{conversation:{id:existing?'copy':'story',title:existing?'新版本':'原名'}};},
      selectConv:async id=>calls.push(id),showToast:message=>calls.push(message)});
    vm.runInContext(section('  function outlineEditor(', '  function renderPlanningIfVisible('),c);
    c.outlineEditor(true);
    assert.match(dialog.innerHTML,existing?/确认并创建新剧场/:/确认并开始写作/);
    if(!existing)assert.doesNotMatch(dialog.innerHTML,/新剧场名字|重写版|确认后新建剧场/);
    await controls.get('[data-confirm]').onclick();
    assert.equal(calls[1].path,'/books/story/confirm-outline');
    assert.equal(calls[1].body.title,existing?'新版本':'');
    assert.equal(calls[2],existing?'copy':'story');
    assert.equal(c.conversations.length,existing?2:1);
    assert.equal(saved.get('studio_view_'+(existing?'copy':'story')),'reader');
    assert.match(calls[3],existing?/新剧场已创建/:/大纲已确认/);
  }
});

test('copy outline sends the chosen model and opens the new story',async()=>{
  const elements={copyStoryTitle:{value:'另一种写法'},copyStoryModel:{},modelSelect:{value:'old-model',innerHTML:'<option>old-model</option><option>new-model</option>'}};
  const calls=[];let submit;
  const c=vm.createContext({book:{id:'source',outline:'大纲'},chapters:[{}],conversations:[{id:'source',title:'原书',model:'old-model'}],
    modes:new Map(),escHtml:s=>s,$:id=>elements[id],modal:(title,html,label,fn)=>{submit=fn;},
    request:async(...args)=>{calls.push(args);return {id:'copy'};},selectConv:async id=>calls.push(id),showToast(){}});
  vm.runInContext(section('  function copyOutline()', '  async function settings()'),c);
  c.copyOutline();assert.equal(elements.copyStoryModel.value,'old-model');
  elements.copyStoryModel.value='new-model';await submit();
  assert.equal(calls[0][0],'/books/source/copy-outline');assert.equal(calls[0][2].model,'new-model');
  assert.equal(calls[0][2].title,'另一种写法');assert.equal(calls[1],'copy');
  assert.equal(c.conversations[0].id,'copy');assert.equal(c.modes.get('copy'),'novel');
});

test('opening discussion leaves the confirmed story phase unchanged and remembers the view', async () => {
  const saved = new Map();
  const c = vm.createContext({book:{id:'story',phase:'writing'},currentConvId:'story',outlineBusy:false,
    player:null,planningView:false,localStorage:{setItem:(k,v)=>saved.set(k,v)},
    modeUI(){},renderPlanning(){},$(){return {focus(){}};},
    request(){throw Error('Navigation must not mutate the story');}});
  vm.runInContext(section('  function setPlanningView(', '  function readerSize(') +
    section('  async function discussionMode()', '  async function outline('), c);
  await c.discussionMode();
  assert.equal(c.planningView,true);
  assert.equal(c.book.phase,'writing');
  assert.equal(saved.get('studio_view_story'),'discussion');
});

test('returning to a chapter during discussion restores reader, position and controls', () => {
  const saved = new Map([['studio_read_one','420']]);
  let updates=0,closed=0;
  const messages={scrollTop:0};
  const c=vm.createContext({currentConvId:'story',planningView:true,selected:null,chapters:[{id:'one'}],
    localStorage:{setItem:(k,v)=>saved.set(k,v),getItem:k=>saved.get(k)},
    modeUI(){updates++;},renderNovel(){},$:id=>id==='messages'?messages:{close(){closed++;}}});
  vm.runInContext(section('  function setPlanningView(', '  function readerSize(')+
    section('  function selectChapter(', '  function next()'),c);
  c.selectChapter('one');
  assert.equal(c.planningView,false);
  assert.equal(saved.get('studio_view_story'),'reader');
  assert.equal(saved.get('studio_chapter_story'),'one');
  assert.equal(messages.scrollTop,420);
  assert.equal(updates,1);
  assert.equal(closed,1);
});

test('font preference applies immediately and persists within the supported range', () => {
  const saved=new Map();let applied;
  const c=vm.createContext({document:{body:{style:{setProperty:(k,v)=>applied=v}}},
    localStorage:{setItem:(k,v)=>saved.set(k,v)},$(){return null;}});
  vm.runInContext(section('  function readerSize(', '  function readingSettings('),c);
  c.readerSize(null);assert.equal(applied,'16px');
  c.readerSize(14);assert.equal(applied,'14px');assert.equal(saved.get('studio_reader_size'),'14');
  c.readerSize(99);assert.equal(applied,'24px');
});

const assert = require('node:assert/strict');
const { ReadingSpeechPlayer } = require('./static/reading-speech.js');

async function test() {
  const audio = [];
  const states = [];
  const player = new ReadingSpeechPlayer(() => {
    const a = { src: '', paused: true, time: 0,
      play() { this.paused = false; return Promise.resolve(); },
      pause() { this.paused = true; } };
    audio.push(a);
    return a;
  }, state => states.push(state));
  await player.start([{ url: 'body' }, { url: 'annotation' }, { url: 'next' }]);
  assert.equal(audio[0].src, 'body');
  player.pause();
  audio[0].time = 12;
  await player.resume();
  assert.equal(audio[0].time, 12);
  assert.equal(audio.length, 1);
  const staleEnd = audio[0].onended;
  audio[0].onended();
  assert.equal(audio[1].src, 'annotation');
  player.stop();
  staleEnd();
  assert.equal(audio.length, 2, 'stopped/stale clips must not advance the queue');
  assert.equal(audio[1].src, '');
  await player.start([{ url: 'fresh' }]);
  audio[2].onended();
  assert.equal(states.at(-1), 'ended');
  let resolvePlay;
  const slow = new ReadingSpeechPlayer(() => ({src:'', pause() {}, play() {return new Promise(r => resolvePlay=r);}}), () => {});
  const starting = slow.start([{url:'slow'}]);
  slow.pause(); resolvePlay(); await starting;
  assert.equal(slow.state, 'paused', 'A delayed play result must not undo pause');
  const clips = [{url:'first',ready:true}, {url:'second',ready:false}];
  await player.start(clips, 'running');
  assert.equal(player.state,'playing','The first ready clip must play before generation finishes');
  player.setDuration('first',10);
  audio.at(-1).currentTime=3;
  assert.equal(player.progress().current,3);
  audio.at(-1).onended();
  assert.equal(player.state,'waiting','Missing next audio must not mean finished');
  player.pause();
  player.update([{...clips[0]}, {...clips[1],ready:true}], 'done');
  assert.equal(player.state,'paused','Polling must not undo pause');
  await player.resume();
  assert.equal(audio.at(-1).src,'second');
  player.setDuration('second',20);
  assert.equal(player.progress().total,30);
  await player.seek(5);
  audio.at(-1).onloadedmetadata();
  assert.equal(audio.at(-1).src,'first');
  assert.equal(audio.at(-1).currentTime,5);
  player.update(clips,'error');
  audio.at(-1).onended();
  assert.equal(player.state,'blocked','Synthesis failure must never be shown as complete');
  const html = require('node:fs').readFileSync(require('node:path').join(__dirname,'static/reading-companion.js'),'utf8');
  const invitation = html.slice(html.indexOf('function inviteRead()'),html.indexOf('async function startAnnotateAll'));
  let requests=0, confirmations=0, approved=false;
  const ctx={currentBook:{},chapterData:{chapter:{segments_meta:[{status:'not_sent'}]}},isAnnotating:false,
    showToast(){}, startAnnotateAll(){requests++;}, confirm(message){assert.match(message,/当前已经共读过一次/);confirmations++;return approved;}};
  require('node:vm').runInNewContext(invitation+';inviteRead()',ctx);
  assert.equal(requests,1);assert.equal(confirmations,0);
  for(const field of ['annotations','connor_annotations','summaries','connor_summaries']) {
    ctx.chapterData[field]=field.includes('summaries')?[{summary:'已有读后感'}]:{0:{text:'已有批注'}};
    ctx.inviteRead();
    assert.equal(requests,1,'Cancelling repeat co-reading must not start regeneration');
    delete ctx.chapterData[field];
  }
  ctx.chapterData.chapter.segments_meta[0].status='done';approved=true;ctx.inviteRead();
  assert.equal(requests,2);assert.equal(confirmations,5);
  console.log('Reading speech queue: order, pause/resume and stale callbacks passed');
}

async function browserTest() {
  const fs = require('node:fs');
  const path = require('node:path');
  const {chromium} = require(process.env.PLAYWRIGHT_PATH || 'C:/Users/32816/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage({viewport:{width:390,height:844}});
    const errors = [], posts = [], confirmations = [];
    const cached = new Set();
    let synthesizing = false, readyCount = 0;
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      window.testAudios = [];
      window.Audio = class {
        constructor(src='') { this.src=src; this.paused=true; this.duration=10; this.currentTime=0; }
        set src(value) { this._src=value; if(value) queueMicrotask(() => this.onloadedmetadata?.()); }
        get src() { return this._src; }
        load() { queueMicrotask(() => this.onloadedmetadata?.()); }
        play() { this.paused=false; if(!window.testAudios.includes(this)) window.testAudios.push(this); return Promise.resolve(); }
        pause() { this.paused=true; }
      };
    });
    const book = {book_id:'test', title:'夜里的书', author:'测试作者',total_chapters:2,current_chapter:0};
    const chapter = {title:'第一章',char_count:12,paragraphs:['第一段正文。','第二段正文。'],segments_meta:[{status:'done'}],html_content:'<p data-p="0">第一段正文。</p><p data-p="1">第二段正文。</p>'};
    const data = {chapter, ai_name:'伴侣甲',connor_name:'伴侣乙',annotations:{0:{text:'第一位批注'}},connor_annotations:{0:{text:'第二位批注'}}};
    function plan(narrator, running=false) {
      const count = cached.has(narrator) ? 4 : synthesizing ? readyCount : 0;
      const clips = [narrator,'aion','connor',narrator].map((speaker,index) => ({key:String(index),paragraph:index===3?1:0,speaker,name:speaker==='aion'?'伴侣甲':'伴侣乙',kind:index===3?'summary':index===0?'body':'annotation',url:`/audio/${speaker}/${index}`,ready:index<count}));
      return {narrator,clips,signature:narrator,body_chars:12,annotation_chars:10,summary_chars:7,missing_count:4-count,missing_chars:count===4?0:29,missing_voices:[],job:running||synthesizing&&count<4?{state:'running',completed:count,total:4}:null};
    }
    await page.route('http://reading.test/**', async route => {
      const url = new URL(route.request().url()), p = url.pathname;
      let json = {};
      if (p === '/') return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(__dirname,'static/reading.html'),'utf8')});
      if (p === '/static/common.js') return route.fulfill({contentType:'application/javascript',body:`function $(id){return document.getElementById(id)};function connectCommonWS(){};function showToast(){};function escHtml(t){const e=document.createElement('div');e.textContent=t;return e.innerHTML};async function api(method,url,body){const r=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});if(!r.ok)throw Error('request failed');return r.json()}`});
      if (p === '/static/theme.js') return route.fulfill({contentType:'application/javascript',body:''});
      if (p.startsWith('/static/')) {
        const file=path.join(__dirname,p.slice(1));
        if(fs.existsSync(file)) return route.fulfill({path:file});
      }
      if (p.startsWith('/public/')) {
        const file=path.join(__dirname,'..',p.slice(1));
        if(fs.existsSync(file)) return route.fulfill({path:file});
      }
      if(p==='/api/books') json={books:[book]};
      else if(p==='/api/books/test') json={book,chapters:[{...chapter,chapter_index:0},{...chapter,chapter_index:1}]};
      else if(p.endsWith('/speech')) {
        if(route.request().method()==='POST') {
          const body=route.request().postDataJSON();posts.push(body);synthesizing=true;readyCount=1;json=plan(body.narrator,true);
        } else json=plan(url.searchParams.get('narrator'));
      } else if(p.includes('/chapters/')) json=data;
      return route.fulfill({json});
    });
    await page.goto('http://reading.test/');
    await page.evaluate(() => openBook('test'));
    await page.click('#readingSpeechSettings');
    await page.waitForFunction(() => !document.getElementById('readingSpeechStart').disabled);
    assert.equal(posts.length,0);
    await page.selectOption('#readingNarrator','connor');
    await page.waitForFunction(() => !document.getElementById('readingSpeechStart').disabled);
    async function answerConfirmation(accept) {
      const handled = new Promise(resolve => page.once('dialog', async dialog => {
        confirmations.push(dialog.message());
        await (accept ? dialog.accept() : dialog.dismiss());
        resolve();
      }));
      await page.click('#readingSpeechStart');
      await handled;
      await page.waitForFunction(() => !document.getElementById('readingSpeechStart').disabled);
    }
    await answerConfirmation(false);
    assert.equal(posts.length,0);
    await answerConfirmation(true);
    await page.waitForFunction(() => window.testAudios.length===1 && !window.testAudios[0].paused);
    assert.match(confirmations[0],/12 字/);
    assert.match(confirmations[0],/29 字/);
    assert.match(confirmations[0],/读后感 7 字/);
    assert.equal(posts[0].narrator,'connor');
    assert.equal(readyCount,1,'Only the first clip is ready when playback starts');
    await page.click('#readingSpeechPause');
    assert.equal(await page.getAttribute('#readingSpeechPause','aria-label'),'继续');
    await page.click('#readingSpeechPause');
    await page.evaluate(() => window.testAudios.at(-1).onended());
    await page.waitForFunction(() => document.getElementById('readingSpeechNow').textContent.includes('等待下一段'));
    assert.equal(await page.evaluate(() => window.testAudios.length),1);
    await page.click('#readingSpeechPause');
    readyCount=2;
    await page.waitForFunction(() => document.getElementById('readingSpeechSynthesis').textContent.includes('2/4'));
    assert.equal(await page.evaluate(() => window.testAudios.length),1,'Polling does not resume a paused listener');
    await page.click('#readingSpeechPause');
    await page.waitForFunction(() => window.testAudios.length===2);
    assert.equal(await page.evaluate(() => window.testAudios.at(-1).src),'/audio/aion/1');
    readyCount=4;cached.add('connor');
    await page.waitForFunction(() => document.getElementById('readingSpeechSynthesis').textContent.includes('4/4'));
    await page.evaluate(() => window.testAudios.at(-1).onended());
    assert.equal(await page.evaluate(() => window.testAudios.at(-1).src),'/audio/connor/2');
    await page.evaluate(() => window.testAudios.at(-1).onended());
    assert.match(await page.textContent('#readingSpeechNow'),/伴侣乙 · 读后感/);
    await page.waitForFunction(() => Number(document.getElementById('readingSpeechSeek').max)===40);
    await page.evaluate(() => {
      const seek=document.getElementById('readingSpeechSeek');seek.value=15;seek.dispatchEvent(new Event('input'));seek.dispatchEvent(new Event('change'));
    });
    await page.waitForFunction(() => window.testAudios.at(-1).currentTime===5);
    assert.equal(await page.evaluate(() => window.testAudios.at(-1).src),'/audio/aion/1');
    await page.click('#readingSpeechSettings');
    await page.waitForFunction(() => !document.getElementById('readingSpeechStart').disabled);
    await page.click('#readingSpeechStart');
    await page.waitForFunction(() => document.getElementById('readingSpeechPanel').hidden);
    assert.equal(await page.getAttribute('#segmentStatus','title'),'1/1 段已批注');
    for (const width of [320,390]) {
      await page.setViewportSize({width,height:844});
      const layout=await page.evaluate(() => {
        const bar=document.getElementById('readingSpeechBar').getBoundingClientRect();
        return {height:bar.height,overflow:document.documentElement.scrollWidth>innerWidth};
      });
      assert.ok(layout.height<=68,'The player should use only two compact rows');
      assert.equal(layout.overflow,false);
    }
    await page.screenshot({path:process.env.READING_SCREENSHOT || path.join(require('node:os').tmpdir(),'reading-speech.png')});
    assert.equal(confirmations.length,2,'Cached playback must not ask to synthesize');
    assert.equal(posts.length,1);
    await page.click('#nextChBtn');
    await page.waitForFunction(() => document.getElementById('readingSpeechBar').hidden);
    assert.ok(await page.evaluate(() => window.testAudios.every(audio => audio.paused)));
    assert.deepEqual(errors,[]);
    console.log('Mobile reading UI: confirmation/cancel, voice selection, cached replay, annotations and chapter navigation passed');
  } finally { await browser.close(); }
}
test().then(() => process.argv.includes('--browser') ? browserTest() : null).catch(error => { console.error(error); process.exitCode = 1; });

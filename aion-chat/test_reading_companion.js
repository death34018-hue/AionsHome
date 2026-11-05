// Real pages, isolated HTTP fixtures: no live conversations, model calls or user data.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_PATH || 'C:/Users/32816/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const context = await browser.newContext({viewport:{width:390,height:844}});
    await context.addInitScript(() => { window.WebSocket = class { static OPEN=1; readyState=1; send(){} close(){} addEventListener(){} }; });
    const errors=[], posts=[];
    let linked=false, partialNext=false;
    const book={book_id:'theater_shared',title:'月下故事',author:'故事角色',total_chapters:1,current_chapter:0};
    const source={id:'chapter',number:1,title:'第一章',status:'ready',revision:1,content:'  🌙月光落在窗前。\n\n我们一起读故事。',images:[],versions:[]};
    const data={chapter:{id:101,title:'第一章',char_count:20,paragraphs:['🌙月光落在窗前。','我们一起读故事。'],html_content:'<p data-p="0">🌙月光落在窗前。</p><p data-p="1">我们一起读故事。</p>',segments_meta:[{start_p:0,end_p:1,status:'not_sent'}]},ai_name:'伴侣甲',connor_name:'伴侣乙',user_name:'读者',annotations:{},connor_annotations:{},summaries:[],connor_summaries:[],highlights:[]};
    const sse=events=>events.map(e=>'data: '+JSON.stringify(e)+'\n\n').join('');
    await context.route('http://coreading.test/**', async route => {
      const req=route.request(), url=new URL(req.url()), p=url.pathname, method=req.method();
      if(method==='POST')posts.push({path:p,body:req.postDataJSON()});
      if(p==='/theater' || p==='/reading')return route.fulfill({path:path.join(__dirname,'static',p==='/theater'?'theater.html':'reading.html'),contentType:'text/html'});
      if(p.startsWith('/static/') || p.startsWith('/public/')) {
        const file=path.join(p.startsWith('/public/')?path.join(__dirname,'..'):__dirname,p.slice(1));
        return fs.existsSync(file)?route.fulfill({path:file}):route.fulfill({status:404,body:''});
      }
      let json={};
      if(p==='/api/models')json=[{key:'test'}];
      else if(p==='/api/worldbook')json={ai_name:'伴侣甲',user_name:'读者'};
      else if(p==='/api/tts/voices')json={voices:[]};
      else if(p==='/api/theater/personas')json=[{id:'writer',name:'故事角色',persona:'作者'}];
      else if(p==='/api/theater/conversations')json=[{id:'story',title:book.title,model:'test',persona_id:'writer'}];
      else if(p==='/api/theater/studio/books')json=[{id:'story',mode:'novel'}];
      else if(p==='/api/theater/studio/books/story/export') {
        assert.equal(url.searchParams.get('format'),'txt');
        assert.equal(url.searchParams.get('include_drafts'),'false');
        return route.fulfill({contentType:'text/plain;charset=utf-8',headers:{'Content-Disposition':"attachment; filename*=UTF-8''"+encodeURIComponent('月下故事.txt')},body:'月下故事\n\n第一章\n\n测试小说正文'});
      }
      else if(p==='/api/theater/studio/books/story')json={book:{id:'story',mode:'novel',phase:'writing',outline:'大纲',writer_ready:true,writer_name:'故事角色',reading_book_id:linked?book.book_id:null},chapters:[source],discussion:{messages:[],status:'idle'}};
      else if(p==='/api/theater/studio/chapters/chapter/reading') {
        if(method==='POST')linked=true;
        json=linked&&source.status==='ready'?{book_id:book.book_id,chapter_index:0,chapter_id:data.chapter.id}:null;
      } else if(p==='/api/theater/studio/chapters/chapter' && method==='PATCH') {
        source.content=req.postDataJSON().content;source.revision++;source.status='draft';json=source;
      } else if(p==='/api/books')json={books:linked?[book]:[]};
      else if(p==='/api/books/theater_shared')json={book,chapters:[{...data.chapter,chapter_index:0}]};
      else if(p==='/api/books/theater_shared/chapters/0')json=data;
      else if(p.endsWith('/annotate-all')) {
        data.annotations={0:{type:'感想',text:'第一位的批注'}};
        data.connor_annotations={1:{type:'感想',text:'第二位的批注'}};
        data.summaries=[{segment_index:0,summary:'第一位的读后感'}];
        data.connor_summaries=[{segment_index:0,summary:'第二位的读后感'}];
        data.chapter.segments_meta[0].status='done';
        if(partialNext) {
          partialNext=false;
          data.connor_annotations={};data.connor_summaries=[];
          data.chapter.segments_meta[0].status='partial';
          return route.fulfill({contentType:'text/event-stream',body:sse([
            {type:'companion_done',segment_index:0,who:'aion',annotations:[{p:0,...data.annotations[0]}],summary:'第一位的读后感'},
            {type:'segment_error',segment_index:0,who:'connor',message:'测试暂时失败'},
            {type:'segment_done',segment_index:0,status:'partial',aion_annotations:[{p:0,...data.annotations[0]}],aion_summary:'第一位的读后感'},
            {type:'done'}])});
        }
        return route.fulfill({contentType:'text/event-stream',body:sse([{type:'segment_done',segment_index:0,aion_annotations:[{p:0,...data.annotations[0]}],connor_annotations:[{p:1,...data.connor_annotations[1]}],aion_summary:'第一位的读后感',connor_summary:'第二位的读后感'},{type:'done'}])});
      } else if(p==='/api/conversations')json=[{id:'private',model:'test'}];
      else if(p==='/api/chatroom/rooms')json=[{id:'group',type:'group'},{id:'second',type:'connor_1v1'}];
      else if(p.includes('/messages'))json=[];
      else if(p==='/api/chatroom/rooms/group/send')return route.fulfill({contentType:'text/event-stream',body:sse([{type:'aion_chunk',content:'我喜欢这段月光。'},{type:'connor_chunk',content:'我也想和你一起看。'},{type:'done'}])});
      else if(p.endsWith('/highlights') && method==='POST') {json={id:7};data.highlights.push({...req.postDataJSON(),id:7});}
      return route.fulfill({json});
    });
    const theater=await context.newPage();theater.on('pageerror',e=>errors.push(e.message));
    await theater.goto('http://coreading.test/theater');
    await theater.waitForSelector('#novelBody');
    assert.equal(posts.length,0,'Opening a finished chapter must not call AI or create a second book');
    await theater.locator('#studioMenu summary').click();
    await theater.getByRole('button',{name:'导出小说',exact:true}).click();
    assert.equal(await theater.locator('#novelExportFormat').inputValue(),'docx');
    assert.equal(await theater.locator('#novelExportDrafts').isChecked(),false);
    await theater.locator('#novelExportFormat').selectOption('txt');
    const exporting=theater.waitForEvent('download');
    await theater.locator('#studioSave').click();
    const downloaded=await exporting;
    assert.equal(downloaded.suggestedFilename(),'月下故事.txt');
    assert.equal(await downloaded.failure(),null);
    await theater.getByRole('button',{name:'邀请共读',exact:true}).click({timeout:3000});
    await theater.waitForSelector('#novelBody .anno-bubble');
    assert.equal(await theater.locator('#novelBody .anno-bubble').count(),2);
    assert.match(await theater.locator('#novelBody').textContent(),/伴侣乙读后感/);
    const first=await theater.locator('#novelBody [data-p="0"]').getAttribute('data-source-start');
    assert.equal(first,'2','Published paragraphs map back to original UTF-16 offsets for narration');
    await theater.evaluate(()=>{
      const text=document.querySelector('#novelBody [data-p="0"]').firstChild;
      const range=document.createRange();range.setStart(text,0);range.setEnd(text,text.length);
      const sel=getSelection();sel.removeAllRanges();sel.addRange(range);document.dispatchEvent(new Event('selectionchange'));
    });
    await theater.locator('#selAskGroupBtn').click();
    await theater.locator('#askInput').fill('你们喜欢这段吗？');
    await theater.locator('#askPopup .ask-send').click();
    await theater.evaluate(()=>ReadingCompanion.submitAskToChat());
    await theater.waitForSelector('#novelBody .user-hl');
    assert.equal(data.highlights[0].chapter_id,101);
    assert.equal(data.highlights[0].annotator,'group');
    assert.equal(data.highlights[0].connor_answer,'我也想和你一起看。');
    assert.equal(posts.filter(p=>p.path.endsWith('/annotate-all')).length,1);
    assert.equal(posts.filter(p=>p.path==='/api/chatroom/rooms/group/send').length,1,'A second submit during context loading must not send the question twice');
    const reading=await context.newPage();reading.on('pageerror',e=>errors.push(e.message));
    await reading.goto('http://coreading.test/reading?book=theater_shared');
    await reading.waitForSelector('#readerContent .user-hl');
    assert.equal(await reading.locator('#readerContent .anno-bubble').count(),2);
    await reading.locator('#readerContent .user-hl').first().click();
    assert.match(await reading.locator('#hlPopupAnswer').textContent(),/我也想和你一起看/);
    await reading.evaluate(()=>ReadingCompanion.closeHlPopup());
    let confirmations=0;
    reading.on('dialog',async dialog=>{confirmations++;await dialog.accept();});
    partialNext=true;
    await reading.evaluate(()=>ReadingCompanion.inviteRead());
    assert.equal(confirmations,1,'Completed chapters still offer an explicit reread');
    assert.equal(posts.filter(p=>p.path.endsWith('/annotate-all')).at(-1).body.reread,true);
    assert.match(await reading.locator('#segmentStatus').getAttribute('aria-label'),/0\/1/,'A partial failure must not be reported as complete');
    await reading.evaluate(()=>ReadingCompanion.inviteRead());
    assert.equal(confirmations,1,'Incomplete chapters resume without asking to regenerate saved notes');
    assert.equal(posts.filter(p=>p.path.endsWith('/annotate-all')).at(-1).body.reread,false);
    assert.match(await reading.locator('#segmentStatus').getAttribute('aria-label'),/1\/1/);
    data.chapter.html_content += '<div class="book-img"><img src="/public/AIIcon.png" alt="故事插画"></div>';
    await reading.evaluate(()=>refreshPublishedBook());
    await reading.waitForSelector('#readerContent .book-img img');
    assert.equal(await reading.locator('#readerContent .user-hl').count(),1,'Late images must keep the saved selection');
    await theater.evaluate(()=>TheaterReading.refresh());
    await theater.waitForSelector('#novelBody .book-img img');
    for (const width of [320,390]) {
      await theater.setViewportSize({width,height:844});
      assert.equal(await theater.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      assert.equal(await theater.locator('#inviteBtn').isVisible(),true);
    }
    await theater.screenshot({path:process.env.COREADING_SCREENSHOT || path.join(require('node:os').tmpdir(),'theater-coreading.png')});
    await theater.evaluate(()=>{ReadingCompanion.closeReplyPanel();Studio.editChapter(true);});
    await theater.locator('#chapterContent').fill('未确认的新稿');
    await theater.locator('#studioSave').click();
    await theater.waitForFunction(()=>document.getElementById('novelBody').textContent==='未确认的新稿');
    assert.equal(await theater.locator('#novelBody .anno-bubble').count(),0);
    assert.equal(await theater.getByRole('button',{name:'邀请共读',exact:true}).count(),0);
    assert.equal(data.highlights.length,1,'Draft edits must preserve the last published version in companion reading');
    assert.equal(await theater.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    assert.deepEqual(errors,[]);
    console.log('Shared reading: finished gate, two companions, selection group chat, persisted shared notes and draft isolation passed');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

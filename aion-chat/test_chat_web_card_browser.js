const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {chromium} = require('playwright');

test('inline play, replay, and one request for repeated runtime errors', async () => {
  const browser = await chromium.launch({channel:'chrome', headless:true});
  try {
    const page = await browser.newPage({viewport:{width:390, height:740}});
    await page.route('http://chat.test/', route => route.fulfill({contentType:'text/html',
      body:'<!doctype html><meta charset=utf-8><div class="msg-row" data-msg-id="m1"></div>'}));
    await page.goto('http://chat.test/');
    await page.addStyleTag({path:path.join(__dirname, 'static/chat-web-cards.css')});
    await page.addScriptTag({path:path.join(__dirname, 'static/chat-web-cards.js')});
    const card = {type:'web_card', id:'a'.repeat(32), title:'星星', revision:1,
      html:'<button onclick="this.textContent=\'亮了\'">点星星</button>'};
    const render = item => page.evaluate(item => {
      document.querySelector('.msg-row').innerHTML = ChatWebCards.render([item]);
    }, item);
    await render(card);
    const stage = page.frameLocator('.web-card-stage iframe');
    const star = stage.getByRole('button',{name:'点星星'});
    // The artwork appears once fitted; a fresh cross-process frame then needs
    // a moment before Chrome routes input to it.
    await page.waitForFunction(() => document.querySelector('.web-card-stage iframe')?.style.opacity === '1');
    await page.waitForTimeout(600);
    await star.click();
    await stage.getByRole('button',{name:'亮了'}).waitFor({timeout:3000});
    assert.equal(await page.locator('iframe').getAttribute('sandbox'), 'allow-scripts');
    await page.getByRole('button',{name:'收起'}).click();
    await page.getByRole('button',{name:'打开'}).click();
    await stage.getByText('点星星').waitFor();
    let requests = 0;
    await page.route('**/api/web-cards/private/m1/*', route => {
      requests++;
      assert.match(route.request().postDataJSON().error, /broken/);
      return route.fulfill({json:{...card, revision:3, auto_repaired:true}});
    });
    await render({...card, revision:2, html:'<script>setTimeout(()=>{throw Error("broken")});throw Error("broken")</script>'});
    await stage.getByText('点星星').click();
    assert.equal(requests, 1);

    // A page locked to 100vh with more fixed content than the card's aspect
    // gets a taller virtual screen and is scaled down, never cut.
    await render({...card, id:'e'.repeat(32), html:'<style>body{height:100vh;overflow:hidden;display:flex;flex-direction:column;justify-content:space-between;padding:14px}' +
      '.cup{height:420px;flex:none}</style><h1>茶饮台</h1><div class="cup"></div><button>加满</button>'});
    const fill = stage.getByRole('button',{name:'加满'});
    await fill.waitFor();
    assert.equal(await fill.evaluate(el => innerWidth), 360, 'every phone gets the same virtual screen width');
    assert.ok(await fill.evaluate(el => el.getBoundingClientRect().bottom <= innerHeight &&
      getComputedStyle(document.documentElement).overflowY !== 'auto'), 'the bottom control fits without scrolling');
    const shown = await page.locator('.web-card-stage').evaluate(el => {
      const stage = el.getBoundingClientRect(), frame = el.querySelector('iframe').getBoundingClientRect();
      return stage.height <= 520 && frame.bottom <= stage.bottom + 1;
    });
    assert.ok(shown, 'the scaled artwork stays inside the card');
    await page.setViewportSize({width:320,height:568});
    await page.waitForFunction(() => document.querySelector('.web-card-stage').clientWidth < 320);
    await fill.click();
    assert.equal(await fill.evaluate(() => innerWidth), 360, 'a narrow phone scales the same layout');
    await page.setViewportSize({width:390,height:740});

    await render({...card, id:'c'.repeat(32), html:'<style>body{margin:0;overflow:hidden}main{height:100vh;min-height:760px;display:grid;align-items:end}</style><main><button>底部互动</button></main>'});
    const lastButton = stage.getByRole('button',{name:'底部互动'});
    await lastButton.waitFor();
    await lastButton.click({timeout:3000});
    const mobileHeight = await page.locator('.web-card-stage').evaluate(el => el.clientHeight);
    assert.ok(mobileHeight <= 520, 'the artwork must leave room for the chat on a phone');
    assert.ok(await lastButton.evaluate(el => el.getBoundingClientRect().bottom <= innerHeight + 1),
      'a portrait page with a minimum height should expose its bottom control');
    await page.setViewportSize({width:1000,height:800});
    await render({...card,id:'f'.repeat(32),html:'<style>*{margin:0}body{height:100vh}</style><button>弹性页面</button>'});
    await stage.getByText('弹性页面').waitFor();
    const stageHeight = () => page.locator('.web-card-stage').evaluate(el => el.clientHeight);
    assert.ok(await stageHeight() <= 430,'a flexible desktop artwork should use a compact stage');
    await render({...card,id:'c'.repeat(32),revision:2,html:'<style>body{margin:0}main{min-height:520px;height:100vh}</style><main><button>竖版页面</button></main>'});
    await stage.getByText('竖版页面').waitFor();
    await stage.getByRole('button',{name:'竖版页面'}).click();
    await page.setViewportSize({width:390,height:740});

    // The app uses a separate player; its slot follows the visible slice of
    // the message when the user scrolls the upper half out of the chat.
    await page.evaluate(() => {
      window.positions = [];
      window.AionWebCard = {open(key, doc){ window.opened = {key, doc}; }, close(){}, place(...args){ positions.push(args); }};
      const row = document.querySelector('.msg-row');
      Object.assign(row.style, {height:'160px', overflow:'auto'});
      row.style.padding = '0';
    });
    await render({...card, id:'b'.repeat(32)});
    await page.waitForFunction(() => window.positions.some(p => p[3] > 0));
    await page.evaluate(() => { document.querySelector('.msg-row').scrollTop = 90; });
    await page.waitForFunction(() => window.positions.some(p => p[3] > 0 && p[6] < 0));
    assert.equal(await page.locator('iframe').count(), 0);

    // A fully visible card has rounded bottom corners. In the actual chat,
    // a floating header and composer also overlap the scrolling message list.
    await page.evaluate(() => {
      const row = document.querySelector('.msg-row');
      row.id = 'messages';
      Object.assign(row.style, {height:'700px', paddingTop:'100px'});
      row.scrollTop = 0;
      for (const [kind, y] of [['chat-header',45], ['composer',370]]) {
        const bar = document.createElement('div');
        bar.className = kind;
        Object.assign(bar.style,{position:'fixed',left:'4px',right:'4px',top:y+'px',height:'45px',zIndex:5});
        document.body.append(bar);
      }
    });
    await render({...card,id:'d'.repeat(32)});
    await page.waitForFunction(() => positions.at(-1)?.[0].startsWith('dddd'));
    const placement = () => page.evaluate(async () => {
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const rect = document.querySelector('.web-card-stage').getBoundingClientRect();
      return {actual:positions.at(-1).slice(2,5), expectedTop:Math.max(rect.top,90),
              expectedHeight:Math.min(rect.bottom,370)-Math.max(rect.top,90)};
    });
    for (const padding of [100,0,260]) {
      await page.locator('#messages').evaluate((el,n) => { el.style.paddingTop = n+'px'; },padding);
      const {actual:[top,width,height],expectedTop,expectedHeight} = await placement();
      assert.ok(width > 0, 'rounded corners and bars must leave the exposed artwork visible');
      assert.ok(Math.abs(top-expectedTop) <= 1 && Math.abs(height-expectedHeight) <= 1,
        'clip between bars: '+JSON.stringify({top,height,expectedTop,expectedHeight}));
    }
    await page.evaluate(() => {
      const overlay = document.createElement('div');
      Object.assign(overlay.style,{position:'fixed',inset:0,zIndex:10});
      document.body.append(overlay);
    });
    assert.equal((await placement()).actual[1],0,'a dialog covers the native player');

    // The native player loads a shell that fits the artwork the same way and
    // reports its errors through the console.
    await render({...card, id:'9'.repeat(32), html:'<!-- 注释 --><h1>报错</h1><script>setTimeout(()=>{throw Error("broken")})</script>'});
    await page.evaluate(() => document.querySelector('[data-web-action=play]').click());
    const {key, doc} = await page.evaluate(() => window.opened);
    const player = await browser.newPage({viewport:{width:334, height:416}});
    const logged = [];
    player.on('console', message => logged.push(message.text()));
    await player.route('http://player.test/', route => route.fulfill({contentType:'text/html', body:doc}));
    await player.goto('http://player.test/');
    const heading = player.frameLocator('iframe').getByRole('heading', {name:'报错'});
    await heading.waitFor();
    assert.equal(await heading.evaluate(() => innerWidth), 360);
    await player.waitForFunction(() => document.querySelector('iframe').style.opacity === '1');
    await player.waitForFunction(() => document.querySelector('iframe').getBoundingClientRect().width > 330);
    assert.ok(logged.some(text => text.startsWith('web-card:' + key + ':Uncaught Error: broken')), logged.join('\n'));
  } finally { await browser.close(); }
});

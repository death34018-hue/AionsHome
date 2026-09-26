// Isolated browser UI check with a fake phone bridge. Never contacts a toy or production service.
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_PATH || 'C:/Users/32816/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async()=>{
  const browser=await chromium.launch({headless:true});
  try {
    const page=await browser.newPage({viewport:{width:390,height:844},deviceScaleFactor:1});
    let profile='ankni', epoch='one', enabled=false;
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.route('http://toy.test/**',async route=>{
      const url=new URL(route.request().url());
      if(url.pathname==='/api/toys/selection') {
        if(route.request().method()==='PUT'){profile=route.request().postDataJSON().profile;epoch+='x';}
        return route.fulfill({json:{profile,active:profile,epoch}});
      }
      if(url.pathname==='/api/capabilities/ankni'){enabled=route.request().postDataJSON().enabled;return route.fulfill({json:{ok:true}});}
      if(url.pathname.startsWith('/api/ankni-ai')) return route.fulfill({json:{enabled,epoch}});
      if(url.pathname==='/static/common.js') return route.fulfill({contentType:'text/javascript',body:''});
      const documents={'/toys':'whisper.html','/toys/ankni':'toy-ankni.html','/toys/sosexy':'toy-sosexy.html'};
      const name=documents[url.pathname] || (url.pathname.startsWith('/static/') ? path.basename(url.pathname) : '');
      const file=path.join(__dirname,'static',name);
      if(!name || !fs.existsSync(file))return route.fulfill({status:404,body:''});
      return route.fulfill({body:fs.readFileSync(file),contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html'});
    });
    await page.addInitScript(()=>{
      window.WebSocket=class{constructor(){setTimeout(()=>this.onopen?.(),0);}close(){}};
      let connected=false,profile='ankni';window.phoneActions=[];
      window.AionBle={getAnkniControlVersion:()=>3,getProfile:()=>profile,isConnected:()=>connected,
        selectProfile:value=>{profile=value;},connect:()=>{connected=true;window.toyNativeBle.onConnected(profile,'ANKNI MX');},
        disconnect:()=>{connected=false;window.toyNativeBle.onDisconnected();},getAnkniWriteTargets:()=>JSON.stringify(['dddd / ddd1']),
        configureAnkni:()=>true,selectAnkniWriteTarget:()=>true,
        executeAnkniCommand:raw=>{window.phoneActions.push(raw);window.toyNativeBle.onNativeAction(raw);return true;},
      };
    });
    await page.goto('http://toy.test/toys');
    await page.waitForURL('**/toys/ankni');
    await page.getByRole('button',{name:'连接',exact:true}).click();
    await page.getByText('已连接',{exact:true}).waitFor();
    await page.getByRole('button',{name:'5 · 蛮牛冲撞',exact:true}).click();
    await page.waitForFunction(()=>phoneActions.at(-1)==='MODE:5');
    assert.equal(await page.locator('#ankniLive').textContent(),'蛮牛冲撞');
    await page.getByRole('button',{name:'0 · 停止',exact:true}).click();
    await page.waitForFunction(()=>phoneActions.at(-1)==='STOP');
    await page.getByRole('button',{name:'试震动 · 5'}).click();
    await page.waitForFunction(()=>phoneActions.includes('SET:5,0'));
    await page.locator('#ankniTimeline > summary').click();
    await page.getByRole('button',{name:'循环播放',exact:true}).click();
    await page.waitForFunction(()=>phoneActions.includes('LOOP:2000,5;3000,0;4000,6'));
    await page.locator('#ankniPhases').fill('2000,5\n1000,0\n1000,6');
    await page.getByRole('button',{name:'循环播放',exact:true}).click();
    await page.waitForFunction(()=>phoneActions.at(-1)==='LOOP:2000,5;1000,0;1000,6');
    assert.equal(await page.evaluate(()=>phoneActions.at(-2)),'STOP','manual timelines take over immediately even without AI ownership');
    await page.getByRole('button',{name:'全部停止'}).click();
    await page.waitForFunction(()=>phoneActions.at(-1)==='STOP');
    await page.locator('#ankniTimeline > summary').click();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth),true,'no phone horizontal overflow');
    await page.evaluate(()=>scrollTo(0,0));
    await page.screenshot({path:path.resolve(__dirname,'../.codex-tmp/ankni-mobile.png'),fullPage:true});
    await page.getByRole('button',{name:'更换玩具'}).click();
    await page.waitForURL('**/toys?choose=1');
    await page.getByRole('button',{name:/SOSEXY/}).click();
    await page.waitForURL('**/toys/sosexy');
    assert.equal(profile,'sosexy');assert.deepEqual(errors,[]);
    console.log('phone UI: persisted entry, native connect/control/stop and explicit switch passed');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});

'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

test('slow navigation never exposes the previous page; retained pages survive back and retry', async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    const html = fs.readFileSync(path.join(__dirname, 'static/chat.html'), 'utf8');
    const js = fs.readFileSync(path.join(__dirname, 'static/chat.js'), 'utf8');
    const loading = html.match(/<script id="subpage-navigation">([\s\S]*?)<\/script>/)?.[1] || '';
    const markup = html.slice(html.indexOf('<div id="subPageOverlay">'), html.indexOf('<script id="subpage-navigation">') < 0 ? html.indexOf('<script id="initial-desktop">') : html.indexOf('<script id="subpage-navigation">'));
    const navigation = js.slice(js.indexOf('const persistentSubPageFrames ='), js.indexOf('function rememberToyReturnPage('))
      + js.slice(js.indexOf('function openSubPage(url)'), js.indexOf('// Android 原生返回键回调'));
    const held = new Map();
    const counts = new Map();
    await page.route('http://navigation.test/**', async route => {
      const pathname = new URL(route.request().url()).pathname;
      counts.set(pathname, (counts.get(pathname) || 0) + 1);
      if (pathname === '/chat') return route.fulfill({ contentType: 'text/html', body: `<style>.sub-page-frame{position:absolute;inset:0;width:100%;height:100%;display:none}#subPageOverlay:not(.show){display:none}[hidden]{display:none!important}</style>${markup}<script>${loading}</script><script>
        const $=id=>document.getElementById(id); let currentSubPage=null,currentConvId=null;
        const rememberToyReturnPage=()=>{},closeSidebar=()=>{},syncSubPageMode=()=>{},syncSubPageChromeFromFrame=()=>{},applyAionTheme=()=>{};
        ${navigation}</script>` });
      if (pathname === '/slow' || pathname === '/failed') {
        held.set(pathname, route);
        return;
      }
      return route.fulfill({ contentType: 'text/html', body: `<h1>${pathname}</h1><input id="draft"><script>window.onAionSubPageVisibilityChanged=v=>document.body.dataset.visible=v</script>` });
    });
    const open = url => page.evaluate(url => openSubPage(url), url);
    const visiblePaths = () => page.evaluate(() => [...document.querySelectorAll('.sub-page-frame')].filter(f => getComputedStyle(f).display !== 'none' && getComputedStyle(f).visibility !== 'hidden').map(f => f.contentDocument?.querySelector('h1')?.textContent).filter(Boolean));
    await page.goto('http://navigation.test/chat');
    await open('/other');
    await page.waitForFunction(() => [...document.querySelectorAll('iframe')].some(f => f.contentDocument?.querySelector('h1')?.textContent === '/other'));
    await open('/');
    await page.waitForFunction(() => [...document.querySelectorAll('iframe')].some(f => f.contentDocument?.querySelector('h1')?.textContent === '/'));
    assert.equal(await page.evaluate(() => [...document.querySelectorAll('iframe')].some(f => f.contentDocument?.querySelector('h1')?.textContent === '/other')), false,
      'returning home must release the previous transient document before another app opens');
    assert.equal(await page.evaluate(() => [...document.querySelectorAll('iframe')].some(f => f.contentDocument?.querySelector('h1')?.textContent === '/other')), false,
      'returning home must release the previous transient document before another app opens');
    await open('/slow');
    assert.deepEqual(await visiblePaths(), [], 'old transient document must disappear immediately');
    await open('/memory');
    await page.waitForFunction(() => [...document.querySelectorAll('iframe')].some(f => f.contentDocument?.querySelector('h1')?.textContent === '/memory'));
    const memory = page.frames().find(f => new URL(f.url()).pathname === '/memory');
    await memory.locator('#draft').fill('unsaved memory');
    await open('/');
    assert.equal(await memory.locator('body').getAttribute('data-visible'), 'false');
    await open('/family-dynamics');
    await page.waitForFunction(() => [...document.querySelectorAll('iframe')].some(f => f.contentDocument?.querySelector('h1')?.textContent === '/family-dynamics'));
    await open('/memory');
    assert.equal(await memory.locator('#draft').inputValue(), 'unsaved memory');
    assert.equal(counts.get('/memory'), 1);
    assert.equal(await memory.locator('body').getAttribute('data-visible'), 'true');
    if (held.has('/slow')) await held.get('/slow').fulfill({ body: '<h1>late obsolete page</h1>' }).catch(() => {});
    assert.deepEqual(await visiblePaths(), ['/memory']);
    await open('/failed');
    await page.waitForFunction(() => document.querySelector('#subPageLoading')?.hidden === false);
    // Use the browser clock so the timeout recovery is tested without waiting 15 seconds.
    await page.clock.install();
    await open('/failed');
    await page.clock.fastForward(16000);
    assert.equal(await page.locator('#subPageLoadingRetry').isVisible(), true);
    const retryRequest = page.waitForRequest('http://navigation.test/failed');
    await page.locator('#subPageLoadingRetry').click();
    await retryRequest;
    await held.get('/failed').abort();
    await page.waitForFunction(() => document.querySelector('#subPageLoadingRetry').hidden === false);
    assert.equal(await page.locator('#subPageLoading').isVisible(), true);
    await page.locator('#subPageLoadingHome').click();
    assert.deepEqual(await visiblePaths(), ['/']);
    assert.equal(await page.locator('#subPageLoading').isVisible(), false);
  } finally { await browser.close(); }
});

test('the full shell adopts an early pending page, and DOM readiness does not wait for images', async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    const html = fs.readFileSync(path.join(__dirname, 'static/chat.html'), 'utf8');
    const js = fs.readFileSync(path.join(__dirname, 'static/chat.js'), 'utf8');
    const shell = html.slice(html.indexOf('<div id="subPageOverlay">'), html.indexOf('<script src="/static/hug_pillow_ai.js'));
    const navigation = js.slice(js.indexOf('const persistentSubPageFrames ='), js.indexOf('function rememberToyReturnPage('))
      + js.slice(js.indexOf('function openSubPage(url)'), js.indexOf('// Android 原生返回键回调'));
    let memoryRequest, imageRequest, memoryCount = 0;
    await page.route('http://early.test/**', async route => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === '/chat') return route.fulfill({ contentType: 'text/html', body: `<style>[hidden]{display:none!important}</style>${shell}` });
      if (pathname === '/memory') { memoryCount++; memoryRequest = route; return; }
      if (pathname === '/slow-image') { imageRequest = route; return; }
      return route.fulfill({ contentType: 'text/html', body: '<h1>Home</h1>' });
    });
    await page.goto('http://early.test/chat?page=/memory', { waitUntil: 'domcontentloaded' });
    await page.evaluate(source => {
      window.$ = id => document.getElementById(id);
      window.currentSubPage = null;
      window.closeSidebar = window.rememberToyReturnPage = window.syncSubPageMode = window.syncSubPageChromeFromFrame = () => {};
      window.eval(source);
    }, navigation);
    await page.evaluate(() => openSubPage('/memory'));
    assert.equal(memoryCount, 1, 'adoption must reuse an in-flight request');
    await memoryRequest.fulfill({ contentType: 'text/html', body: `<h1>Memory</h1><img src="/slow-image"><script>
      addEventListener('DOMContentLoaded',()=>parent.AionSubPageNavigation.ready(frameElement));
      window.onAionSubPageVisibilityChanged=v=>document.body.dataset.visible=v;
      </script>` });
    await page.waitForFunction(() => document.querySelector('#subPageLoading').hidden);
    const memory = page.frames().find(f => new URL(f.url()).pathname === '/memory');
    assert.equal(await memory.evaluate(() => document.readyState), 'interactive', 'large image is still pending');
    await page.evaluate(() => openSubPage('/'));
    await page.waitForFunction(() => document.querySelector('#subPageLoading').hidden);
    await imageRequest.abort();
    await memory.waitForLoadState('load');
    assert.equal(await memory.locator('body').getAttribute('data-visible'), 'false');
    await page.evaluate(() => openSubPage('/memory'));
    assert.equal(await memory.locator('body').getAttribute('data-visible'), 'true');
    assert.equal(memoryCount, 1);
  } finally { await browser.close(); }
});

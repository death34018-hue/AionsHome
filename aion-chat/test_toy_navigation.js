const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function loadNavigation(options = {}) {
  const values = new Map();
  const events = [];
  const returnLink = { addEventListener(type, fn) { this.click = fn; } };
  const document = {
    querySelectorAll(selector) { return selector === '[data-toy-return]' ? [returnLink] : []; },
    addEventListener() {},
  };
  const location = { href: '/toys', search: options.search || '' };
  let selected = options.selected || null;
  if(options.chooser) document.body={classList:{contains:name=>['toy-page','toy-chooser-page'].includes(name)}};
  const window = {
    parent: options.parent,
    location,
    localStorage: {
      getItem(key) { return values.get(key) || null; },
      setItem(key, value) { values.set(key, value); events.push(`stored:${value}`); },
    },
    fetch: async (url, options) => {
      if(options?.method === 'PUT') {selected=JSON.parse(options.body).profile;events.push('saved:'+selected);}
      return {ok:true,json:async()=>({profile:selected,active:selected || 'sosexy',epoch:'test'})};
    },
    console: { warn(message) { events.push(`warn:${message}`); } },
    stopAndDisconnectToy: options.stopAndDisconnectToy,
  };
  const context = { window, document, console: window.console, Promise, URLSearchParams };
  vm.createContext(context);
  const path = `${__dirname}/static/toy-navigation.js`;
  const source = fs.existsSync(path) ? fs.readFileSync(path, 'utf8') : '';
  vm.runInContext(source, context);
  return { navigation: window.ToyNavigation, location, values, events, returnLink, ready:window.ToySelectionReady };
}

async function main() {
  {
    const source=fs.readFileSync(`${__dirname}/static/chat.js`,'utf8');
    const handler=source.slice(source.indexOf('const legacyToyEvents ='),source.indexOf('window.stopAllToyControllers ='));
    const actions=[];
    let dedicated=false;
    const context={toyConnected:true,window:{hasDedicatedToyController:()=>dedicated},
      fetch:async()=>({ok:true,json:async()=>({active:'sosexy',epoch:'new'})}),toyExecCmd:c=>actions.push(c),console};
    vm.createContext(context);vm.runInContext(handler,context);
    await context.handleLegacyToyEvent({msg_id:'old',epoch:'old',commands:['9']});
    assert.deepEqual(actions,[],'legacy chat also rejects commands from the previous selection');
    const fresh={msg_id:'new',epoch:'new',commands:['1']};
    await context.handleLegacyToyEvent(fresh);await context.handleLegacyToyEvent(fresh);
    assert.deepEqual(actions,['1']);
    dedicated=true;
    await context.handleLegacyToyEvent({msg_id:'page',epoch:'new',commands:['2']});
    assert.deepEqual(actions,['1'],'dedicated SOSEXY page owns delivery, avoiding duplicate native writes');
  }
  {
    for (const page of ['chat', 'chatroom']) {
      const html = fs.readFileSync(`${__dirname}/static/${page}.html`, 'utf8');
      const handler = html.match(/<button\b[^>]*onclick="([^"]+)"[^>]*>💗 密语时刻<\/button>/)[1];
      const source = fs.readFileSync(`${__dirname}/static/${page}.js`, 'utf8');
      const helper = source.match(/function crOpenToyControls\(\) \{[\s\S]*?\n\}/)?.[0] || '';
      for (const embedded of [true, false]) {
        const opened = [];
        const window = { location: { href: '' } };
        window.parent = embedded ? { openSubPage: url => opened.push(url) } : window;
        const context = { window, currentRoom: {id: 'room-42'}, closeSidebar() {}, openSubPage: url => opened.push(url),
          openWhisper() { opened.push('legacy'); }, crOpenWhisper() { opened.push('legacy'); } };
        vm.runInNewContext(`${helper}\n${handler}`, context);
        if (page === 'chat' || embedded) assert.deepEqual(opened, ['/toys'], `${page} sidebar opens the shared toy chooser`);
        else assert.equal(window.location.href, '/chat?page=%2Ftoys&toyReturn=%2Fchatroom%3Froom%3Droom-42', 'standalone group chat preserves its exact room when opening the persistent app shell');
      }
    }
  }
  {
    let returned = 0;
    const { returnLink } = loadNavigation({ parent: { returnFromToyControls() { returned++; } },
      stopAndDisconnectToy() { throw new Error('return must preserve connection'); } });
    returnLink.click({ preventDefault() {} });
    assert.equal(returned, 1, 'toy back button returns to its chat origin without disconnecting');
  }
  {
    const source = fs.readFileSync(`${__dirname}/static/chat.js`, 'utf8');
    const helpers = source.slice(source.indexOf('function rememberToyReturnPage('), source.indexOf('function openSubPage(url)'));
    const opened = [];
    let closed = 0;
    const context = {window: {}, location: {origin:'http://localhost', search:''}, URL, URLSearchParams,
      currentSubPage: '/chatroom', subPagePath: url => new URL(url, 'http://localhost').pathname,
      openSubPage: url => opened.push(url), closeSubPage: () => closed++};
    vm.createContext(context);
    vm.runInContext(helpers, context);
    vm.runInContext("rememberToyReturnPage('/toys'); currentSubPage='/toys'; rememberToyReturnPage('/toys/svakom'); returnFromToyControls();", context);
    assert.deepEqual(opened, ['/chatroom']);
    const headerAction = fs.readFileSync(`${__dirname}/static/chat.html`, 'utf8').match(/id="subPageBar">\s*<button onclick="([^"]+)"/)[1];
    vm.runInContext(headerAction, context);
    assert.deepEqual(opened, ['/chatroom', '/chatroom'], 'shell close button also returns to the group, not private chat');
    vm.runInContext("currentSubPage=null; rememberToyReturnPage('/toys'); returnFromToyControls();", context);
    assert.equal(closed, 1, 'private chat return closes the overlay');
    context.window.__toyReturnPage = null;
    context.location.search = '?toyReturn=%2Fchatroom%3Froom%3Droom-42';
    vm.runInContext('returnFromToyControls();', context);
    assert.equal(opened.at(-1), '/chatroom?room=room-42');
    vm.runInContext("rememberToyReturnPage('/toys'); currentSubPage=null; rememberToyReturnPage('/toys'); returnFromToyControls();", context);
    assert.equal(closed, 2, 'a later private-chat entry must not reuse the standalone launch room');
    context.window.__toyReturnPage = 'https://example.com/';
    vm.runInContext('returnFromToyControls();', context);
    assert.equal(opened.at(-1), '/', 'return destinations must stay in the local app');
  }
  {
    const opened = [];
    const { navigation, location } = loadNavigation({ parent: { openSubPage: url => opened.push(url) } });
    navigation.open('svakom');
    navigation.open('unknown');
    assert.deepEqual(opened, ['/toys/svakom', '/toys'], 'shell navigation must retain the toy iframe');
    assert.equal(location.href, '/toys', 'do not replace the connected document');
  }
  {
    const { navigation, location, values } = loadNavigation();
    assert.ok(navigation, 'missing ToyNavigation');
    navigation.open('svakom');
    assert.equal(location.href, '/toys/svakom');
    assert.equal(values.get('aion_toy_last_profile'), 'svakom');
    navigation.open('unknown');
    assert.equal(location.href, '/toys');
  }

  {
    let release;
    const order = [];
    const cleanup = () => new Promise(resolve => {
      order.push('cleanup-start');
      release = () => { order.push('cleanup-end'); resolve(); };
    });
    const { navigation, location } = loadNavigation({ stopAndDisconnectToy: cleanup });
    const switching = navigation.switchTo('sosexy');
    await Promise.resolve();
    assert.deepEqual(order, ['cleanup-start']);
    assert.equal(location.href, '/toys');
    release();
    await switching;
    assert.deepEqual(order, ['cleanup-start', 'cleanup-end']);
    assert.equal(location.href, '/toys/sosexy');
  }

  {
    const { navigation, location, events } = loadNavigation({
      stopAndDisconnectToy: async () => { throw new Error('stop failed'); },
    });
    await assert.rejects(navigation.switchTo('svakom'), /stop failed/);
    assert.equal(location.href, '/toys');
    assert.ok(!events.some(event => event.startsWith('saved:')));
  }

  {
    const first=loadNavigation();
    await first.navigation.select('ankni');
    assert.equal(first.location.href,'/toys/ankni');
    assert.ok(first.events.includes('saved:ankni'));
    const reopened=loadNavigation({chooser:true,selected:'ankni'});
    await reopened.ready;
    assert.equal(reopened.location.href,'/toys/ankni','saved selection skips chooser');
    const change=loadNavigation({chooser:true,selected:'ankni',search:'?choose=1'});
    await change.ready;
    assert.equal(change.location.href,'/toys','explicit change always shows chooser');
  }
  console.log('toy navigation: allowlist, persistence and stop-before-switch passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });

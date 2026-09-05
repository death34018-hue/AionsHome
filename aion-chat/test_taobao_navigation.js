const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const opened = [];
const win = {location:{origin:'http://localhost:8080',href:'http://localhost:8080/chatroom'},
  parent:{openSubPage:url=>opened.push(url),closeSubPage:()=>opened.push('close')}};
const context = {window:win,URL};
vm.runInNewContext(fs.readFileSync('static/taobao-card.js','utf8'),context);
const ui = win.TaobaoCards;
assert.equal(typeof ui.navigate, 'function', 'card must use host navigation instead of replacing room iframe');
const click = {button:0,preventDefault(){this.prevented=true;}};
ui.navigate(click,'/taobao?trip=abc&return=%2Fchatroom');
assert.equal(opened[0],'/taobao?trip=abc&return=%2Fchatroom');
assert.equal(win.location.href,'http://localhost:8080/chatroom');
assert.equal(click.prevented,true);
ui.navigate({button:0,preventDefault(){}},'/chatroom?room=group-7');
assert.equal(opened[1],'/chatroom?room=group-7');
ui.navigate({button:0,preventDefault(){}},'/chat');
assert.equal(opened[2],'close');
assert.equal(ui.returnUrl('https://evil.test/chatroom'),'/');
assert.equal(ui.returnUrl('/chatroom?room=group-7'),'/chatroom?room=group-7');
assert.equal(ui.returnUrl('/taobao?trip=loop'),'/');
ui.navigate({button:0,ctrlKey:true,preventDefault(){throw Error('modified click should remain native');}},'/taobao');
assert.equal(opened.length,3);
win.parent = win;
ui.navigate({button:0,preventDefault(){}},'/chatroom?room=standalone');
assert.equal(win.location.href,'/chatroom?room=standalone');

// Exercise the production cache decision, not a duplicated implementation.
const source=fs.readFileSync('static/chat.js','utf8');
const decision=source.slice(source.indexOf('function shouldNavigatePersistentSubPage('),source.indexOf('function isPersistentSubPage('));
const host={URL,location:{origin:'http://localhost:8080'}};
vm.createContext(host);vm.runInContext(decision,host);
const frame={src:'/chatroom',contentWindow:{location:{href:'http://localhost:8080/taobao?trip=abc'}}};
assert.equal(host.shouldNavigatePersistentSubPage(frame,'/chatroom'),true,'recover iframe that navigated away');
frame.contentWindow.location.href='http://localhost:8080/chatroom?room=group-7';
assert.equal(host.shouldNavigatePersistentSubPage(frame,'/chatroom'),false,'keep healthy cached room');
assert.equal(host.shouldNavigatePersistentSubPage(frame,'/chatroom?room=group-8'),true,'honor explicit target');
console.log('shopping navigation: host, standalone, safe return, cache recovery OK');

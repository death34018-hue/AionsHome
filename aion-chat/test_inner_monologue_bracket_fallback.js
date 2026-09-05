'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = __dirname;
const mixedBracketTexts = [
  '笑死了。[心里嘀咕：睡饱了才有力气窝进怀里。】',
  '笑死了。【心里嘀咕：睡饱了才有力气窝进怀里。]',
];
const expected = [
  {type: 'bubble', text: '笑死了。'},
  {type: 'monologue', text: '睡饱了才有力气窝进怀里。'},
];

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

const chatScript = fs.readFileSync(path.join(root, 'static', 'chat.js'), 'utf8');
const chatSource = chatScript.slice(
  chatScript.indexOf('function splitInnerMonologueParts('),
  chatScript.indexOf('function hasInnerMonologue('),
);
const chatContext = {};
vm.runInNewContext(chatSource, chatContext);
for (const text of mixedBracketTexts) {
  assert.deepStrictEqual(plain(chatContext.splitInnerMonologueParts(text)), expected);
}

const chatroomScript = fs.readFileSync(path.join(root, 'static', 'chatroom.js'), 'utf8');
const chatroomSource = chatroomScript.slice(
  chatroomScript.indexOf('const CR_STRUCTURED_LINE_RE'),
  chatroomScript.indexOf('let crProactiveCompanionshipStatus'),
);
const chatroomContext = {};
vm.runInNewContext(chatroomSource, chatroomContext);
for (const text of mixedBracketTexts) {
  assert.deepStrictEqual(plain(chatroomContext.crMessageContentItems(text)), expected);
}

const wallpaperScript = fs.readFileSync(path.join(root, 'static', 'wallpaper.html'), 'utf8');
const wallpaperSource = wallpaperScript.slice(
  wallpaperScript.indexOf('const WALLPAPER_STRUCTURED_LINE_RE'),
  wallpaperScript.indexOf('function _addOneBubble('),
);
const wallpaperContext = {};
vm.runInNewContext(wallpaperSource, wallpaperContext);
for (const text of mixedBracketTexts) {
  assert.deepStrictEqual(plain(wallpaperContext.wallpaperMessageItems(text)), expected);
}

console.log('inner monologue mixed-bracket fallback tests passed');

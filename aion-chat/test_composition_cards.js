'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const cards = require('./static/composition-cards.js');

test('saved composition cards offer inline playback and escape score metadata', () => {
  const html = cards.render([{type:'composition',id:'piece"x',title:'<img onerror=bad()>',
    actor_name:'小熊',bpm:76,cycles:4,favorite:true}]);
  assert.ok(html.includes('data-compose-action="play"'));
  assert.ok(html.includes('data-compose-action="restart"'));
  assert.ok(html.includes('data-compose-action="volume"'));
  assert.ok(html.includes('composition-visual'));
  assert.ok(html.includes('aria-label="取消收藏小曲"'));
  assert.ok(html.includes('&lt;img onerror=bad()&gt;'));
  assert.ok(html.includes('data-composition-id="piece&quot;x"'));
  assert.ok(!html.includes('<img'));
  assert.ok(!html.includes('<iframe'));
});

test('incomplete and complete score commands stay out of visible reply text', () => {
  assert.equal(cards.stripCommands('听听。[COMPOSE]{"code":"note(1)"}[/COMPOSE]喜欢吗？'), '听听。喜欢吗？');
  assert.equal(cards.stripCommands('听听。[COMPOSE]{"code":"unfinished'), '听听。');
  assert.equal(cards.render([{type:'music',id:1}]), '');
});

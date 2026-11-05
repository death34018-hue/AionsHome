'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {renderSystemNoticeContent, openMiniGame} = require('./static/system-notice-ui.js');
const {render: renderGameCard} = require('./billiards/static/result-card.js');

test('game scoreboard preserves multiple winners, amounts and escaped names', () => {
  const html = renderGameCard([{type:'doudizhu_result', outcome:'农民方获胜', balanceLabel:'剩余手牌', rows:[
    {name:'<地主>', delta:-6, net:'-6 元', balance:'6 张', won:false},
    {name:'农民甲', delta:3, net:'+3 元', balance:'0 张', won:true},
    {name:'农民乙', delta:3, net:'+3 元', balance:'2 张', won:true},
  ]}]);
  assert.match(html, /&lt;地主&gt;/);
  assert.doesNotMatch(html, /<地主>/);
  assert.equal((html.match(/data-winner="true"/g)||[]).length, 2);
  assert.match(html, /-6 元/);
  assert.match(html, /剩余手牌/);
});

test('historical table reports keep recorded balances without inventing missing values', () => {
  const html = renderGameCard([{type:'table_game_result', title:'德州扑克', outcome:'本场结束', lines:[
    '玩家甲：本场 +20.00 元 · 收桌余额 120.00 元',
    '玩家乙：本场 -20.00 元 · 收桌余额 80.00 元',
    '旧局：剩余 350 筹码（旧局未记录起始筹码）',
  ], note:'原结算说明'}]);
  assert.match(html, /120.00 元/);
  assert.match(html, /80.00 元/);
  assert.match(html, /旧局未记录起始筹码/);
  assert.match(html, /原结算说明/);
  assert.equal((html.match(/data-winner="true"/g)||[]).length, 1);
});

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

test('table game report shows money and escapes names in compact cards', () => {
  const html = renderSystemNoticeContent('', {attachments: [{type:'table_game_result', title:'德州扑克', round:2,
    outcome:'<同伴>赢下本手', lines:['<用户>：本手 -0.50 元 · 累计 -1.00 元'], note:'金额仍在桌上'}]});
  assert.match(html, /billiards-result-card/);
  assert.match(html, /&lt;同伴&gt;赢下本手/);
  assert.match(html, /本手 -0.50 元/);
  assert.doesNotMatch(html, /<用户>/);
});

test('recognized schedule notices are collapsed and keep the escaped full text', () => {
  const html = renderSystemNoticeContent(
    '👀 【星<野>】设定了监督：2026-09-02 20:30，内容：查看战况',
    {escapeHtml},
  );
  assert.match(html, /<details class="system-notice-details">/);
  assert.doesNotMatch(html, /<details[^>]*\sopen(?:\s|>)/);
  assert.match(html, /<summary>【星&lt;野&gt;】设定了监督<\/summary>/);
  assert.match(html, /👀 【星&lt;野&gt;】设定了监督：2026-09-02 20:30，内容：查看战况/);
});

test('ordinary system notices remain one escaped text line', () => {
  const html = renderSystemNoticeContent('星<野>翻找了记忆', {escapeHtml});
  assert.equal(html, '<span class="system-notice-text">星&lt;野&gt;翻找了记忆</span>');
});

test('mini game invites link only to supported game lobbies and escape actor names', () => {
  for (const [game, url] of [['桌球', '/billiards'], ['斗地主', '/doudizhu'],
    ['德州扑克', '/texas-holdem'], ['吹牛骰子', '/liars-dice']]) {
    const html = renderSystemNoticeContent('游戏邀请', {attachments: [
      {type: 'mini_game_invite', game, actor_name: '星<野>'},
    ]});
    assert.match(html, /class="mini-game-invite"/);
    assert.ok(html.includes(`href="${url}"`));
    assert.ok(html.includes(`星&lt;野&gt;邀请你玩${game}`));
    assert.doesNotMatch(html, /星<野>/);
  }
  const unknown = renderSystemNoticeContent('游戏邀请', {attachments: [
    {type: 'mini_game_invite', game: 'javascript:alert(1)'},
  ]});
  assert.doesNotMatch(unknown, /href=/);
});

test('mini game buttons use the retained app page or the standalone game lobby', () => {
  const original = globalThis.window;
  const opened = [];
  const event = {preventDefault() {}, stopPropagation() {}};
  try {
    globalThis.window = {openSubPage: url => opened.push(url)};
    openMiniGame(event, '桌球');
    openMiniGame(event, '德州扑克');
    globalThis.window = {parent: {openSubPage: url => opened.push(url)}};
    openMiniGame(event, '斗地主');
    openMiniGame(event, '吹牛骰子');
    globalThis.window = {location: {}, parent: null};
    globalThis.window.parent = globalThis.window;
    openMiniGame(event, '桌球');
    assert.equal(window.location.href, '/billiards');
    assert.deepEqual(opened, ['/billiards', '/texas-holdem', '/doudizhu', '/liars-dice']);
    openMiniGame(event, 'unknown');
    assert.equal(window.location.href, '/billiards');
  } finally {
    if (original === undefined) delete globalThis.window;
    else globalThis.window = original;
  }
});

test('doudizhu result renders a compact card with escaped names and settlement', () => {
  const html = renderSystemNoticeContent('完整战报', {attachments: [{
    type: 'doudizhu_result', outcome: '农民方获胜 · 星<野>、同伴',
    winnerName: '星<野>', landlordName: '用户',
    remaining: '用户 6 张 · 同伴 2 张',
    walletLines: ['用户 -6 元', '星<野> +3 元', '同伴 +3 元'],
  }]});
  assert.match(html, /class="billiards-result-card doudizhu-result-card"/);
  assert.match(html, /斗地主战报/);
  assert.match(html, /农民方获胜 · 星&lt;野&gt;、同伴/);
  assert.match(html, /先出完：星&lt;野&gt; · 地主：用户/);
  assert.match(html, /用户 6 张 · 同伴 2 张/);
  assert.match(html, /用户 -6 元/);
  assert.doesNotMatch(html, /星<野>/);
});

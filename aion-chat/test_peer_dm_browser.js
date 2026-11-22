'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { chromium } = require('playwright');

test('their conversation opens in one click, stays read-only and only reads private updates on screen', async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const width of [390, 1280]) {
      const peer = {id: 'cr_peer_dm', type: 'peer_dm', title: '他们俩'};
      const group = {id: 'group', type: 'group', title: '群聊窗口'};
      let transcript = [], peerReads = 0;
      const page = await browser.newPage({viewport: {width, height: 840}, timezoneId: 'Asia/Shanghai'});
      await page.clock.setFixedTime(new Date('2026-11-16T00:05:00+08:00'));
      await page.route('**/*', async route => {
        const {pathname} = new URL(route.request().url());
        if (pathname === '/chatroom') return route.fulfill({path: path.join(__dirname, 'static/chatroom.html')});
        if (pathname.startsWith('/static/')) return route.fulfill({path: path.join(__dirname, pathname.slice(1))});
        let json = {};
        if (pathname === '/api/chatroom/config') json = {ai_name: '星星', connor_name: '月亮'};
        if (pathname === '/api/chatroom/rooms') json = [peer, group];
        if (pathname === '/api/chatroom/rooms/cr_peer_dm/messages') { ++peerReads; json = transcript; }
        if (pathname === '/api/chatroom/rooms/group/messages') json = [];
        if (pathname === '/api/system-logs') json = [{
          room_id: peer.id, log_id: 'turn1', started_at: 1, actor: 'connor', model: 'test',
          prompt_source: 'dispatch', prompt_messages: [{role: 'user', content: '发送时带上的私聊原文'}],
          response_text: '模型收到后的原始回复',
        }];
        if (pathname === '/api/timeline/chat') json = {items: []};
        if (!pathname.startsWith('/api/')) return route.fulfill({status: 204});
        return route.fulfill({json});
      });
      await page.addInitScript(() => {
        window.WebSocket = class {
          static OPEN = 1;
          readyState = 1;
          constructor() { window.testSocket = this; setTimeout(() => this.onopen?.(), 0); }
          send() {}
          close() {}
        };
      });
      const broadcast = () => page.evaluate(() => window.testSocket.onmessage({
        data: JSON.stringify({type:'peer_dm_updated', data:{room_id:'cr_peer_dm'}}),
      }));
      await page.goto('http://room.test/chatroom');
      await page.waitForFunction(() => window.testSocket?.onmessage && document.querySelector('#roomTitle').textContent === '群聊窗口');
      await broadcast();
      assert.equal(peerReads, 0);
      assert.equal(await page.locator('#peerRoomTab.has-unread').count(), 1);
      if (width === 390) await page.locator('#menuBtn').click();
      await page.locator('#peerRoomTab').click();
      await page.getByText('他们俩还没有聊过。').waitFor();
      assert.equal(await page.locator('#roomTitle').textContent(), '星星 和 月亮 的私聊');
      assert.equal(await page.locator('#roomTitle').isDisabled(), true);
      assert.equal(await page.locator('#peerRoomTab.has-unread').count(), 0);
      for (const id of ['composer', 'headerActions', 'crVoiceModeRow']) assert.equal(await page.locator('#' + id).isVisible(), false);
      transcript = [
        {id:'a', sender:'aion', room_id:peer.id, created_at:Date.parse('2026-11-15T23:59:00+08:00') / 1000, content:'昨晚的留言'},
        {id:'m', sender:'connor', room_id:peer.id, created_at:Date.parse('2026-11-16T00:03:00+08:00') / 1000, content:'私下的问候'},
      ];
      await broadcast();
      await page.getByText('私下的问候').waitFor();
      assert.deepEqual(await page.locator('#messages .message-time:visible').allTextContents(),
        ['2026-11-15 23:59', '2026-11-16 00:03']);
      assert.equal(await page.locator('#messages button,[data-pat-target]').count(), 0);
      assert.equal(await page.evaluate(() => localStorage.getItem('chatroom_messages_snapshot_v1_cr_peer_dm')), null);
      assert.equal(await page.evaluate(() => localStorage.getItem('chatroom_peer_dm_read_at')), String(transcript[1].created_at));
      if (width === 390) await page.locator('#menuBtn').click();
      await page.locator('#crSysLogBtn').click();
      await page.locator('#crSysLogList').getByText('详情 ▾').click();
      await page.getByText('发送时带上的私聊原文').waitFor();
      await page.getByText('模型收到后的原始回复').waitFor();
      await page.locator('#crSysLogModal').getByText('关闭').click();
      await page.evaluate(() => openChatroomRoute('/chatroom'));
      await page.waitForFunction(() => document.querySelector('#roomTitle').textContent === '群聊窗口');
      assert.equal(await page.locator('#composer').isVisible(), true);
      const reads = peerReads;
      await broadcast();
      await page.waitForTimeout(60);
      assert.equal(peerReads, reads);
      assert.equal(await page.getByText('私下的问候').count(), 0);
      await page.close();
    }
  } finally { await browser.close(); }
});

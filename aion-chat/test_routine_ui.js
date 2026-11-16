'use strict';

const assert = require('node:assert/strict');
const ui = require('./static/routine-ui.js');

assert.deepEqual(ui.weekDates('2026-11-01'), [
  '2026-10-26', '2026-10-27', '2026-10-28', '2026-10-29',
  '2026-10-30', '2026-10-31', '2026-11-01',
]);
assert.equal(ui.shiftDate('2026-12-29', 7), '2027-01-05');

const items = [
  { id: 'gym', start: '15:30', end: '17:00', content: '练胸' },
  { id: 'walk', start: '17:00', end: '18:30', content: '遛狗' },
  { id: 'sleep', start: '23:30', end: '', content: '入睡' },
];
assert.equal(ui.activeItemId(items, 16 * 60), 'gym');
assert.equal(ui.activeItemId(items, 17 * 60), 'walk');
assert.equal(ui.activeItemId(items, 20 * 60), null);
assert.equal(ui.activeItemId(items, 23 * 60 + 59), 'sleep');

const overlapping = [
  { id:'free', start:'19:30', end:'21:30' },
  { id:'date', start:'20:00', end:'20:30', overridden:true },
];
assert.equal(ui.activeItemId(overlapping, 20 * 60 + 15), 'date');
assert.equal(ui.activeItemId(overlapping, 20 * 60 + 30), 'free');
assert.equal(ui.activeItemId([
  { id:'gym', start:'15:30', end:'17:00' },
  { id:'once', start:'15:30', end:'16:00', overridden:true },
], 15 * 60 + 45), 'once');

const html = ui.renderRows([
  { id: 'unsafe"', start: '09:00', end: '10:00', content: '<img src=x onerror=alert(1)>', overridden: true },
], { currentId: 'unsafe"', nowText: '09:30' });
assert(!html.includes('<img'));
assert(html.includes('&lt;img'));
assert(html.includes('aria-current="time"'));
assert(html.includes('仅当天'));
assert(!ui.renderRows(items, {}).includes('aria-current="time"'));

const linked = ui.renderRows([{id:'gym',kind:'routine',start:'15:30',end:'17:00',content:'练胸',origin_name:'<用户>',
  reminders:[{id:'alarm-1',type:'alarm',trigger_at:'2026-11-09 15:20',origin_name:'Configured Companion'}],reminder_state:'pending'}]);
assert(linked.includes('&lt;用户&gt;'));
assert(linked.includes('15:20'));
assert(linked.includes('Configured Companion'));
assert.equal(ui.activeItemId([{id:'event',kind:'schedule',start:'14:00',end:''}],14*60+2),null);

console.log('PASS: calendar navigation, current interval boundaries, escaped user content, and current-row rendering.');

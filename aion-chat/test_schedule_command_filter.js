'use strict';

const assert = require('node:assert/strict');
const { stripScheduleCommands } = require('./static/schedule-command-filter.js');

assert.equal(
  stripScheduleCommands(
    '前文［ alarm ： 2026-08-05 18:00 ｜ 喝水 ］中间[ SCHEDULE_DEL ： sch_123 ]结尾',
  ),
  '前文中间结尾',
);

assert.equal(
  stripScheduleCommands('已经取消。[ SCHEDULE_DEL ： sch_'),
  '已经取消。',
);

assert.equal(stripScheduleCommands('普通聊天[随便写写]'), '普通聊天[随便写写]');

console.log('Schedule command filter: ok');

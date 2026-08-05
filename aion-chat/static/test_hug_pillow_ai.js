'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { createController } = require('./hug_pillow_ai.js');


function fakeRuntime() {
  let now = 1000;
  const scheduled = [];
  const sent = [];
  return {
    sent,
    scheduled,
    controller: createController({
      now: () => now,
      setTimer: (callback, delay) => {
        scheduled.push({ callback, delay });
        return scheduled.length;
      },
      transmit: command => sent.push({ command, at: now }),
    }),
    advanceAndRunNext(milliseconds) {
      now += milliseconds;
      const task = scheduled.shift();
      assert.ok(task, 'expected a scheduled task');
      task.callback();
    },
  };
}


test('first command transmits immediately and later commands keep a two-second gap', () => {
  const runtime = fakeRuntime();

  runtime.controller.handleCommandEvent({
    commands: ['PAT_START_STOP', 'SPEED_DOWN', 'SPEED_UP'],
  });

  assert.deepEqual(runtime.sent, [{ command: 'PAT_START_STOP', at: 1000 }]);
  assert.equal(runtime.scheduled[0].delay, 2000);

  runtime.advanceAndRunNext(2000);
  assert.deepEqual(runtime.sent[1], { command: 'SPEED_DOWN', at: 3000 });
  assert.equal(runtime.scheduled[0].delay, 2000);

  runtime.advanceAndRunNext(2000);
  assert.deepEqual(runtime.sent[2], { command: 'SPEED_UP', at: 5000 });
  assert.equal(runtime.scheduled.length, 0);
});


test('duplicate commands remain in the FIFO and each transmits once', () => {
  const runtime = fakeRuntime();

  runtime.controller.handleCommandEvent({
    commands: ['SPEED_DOWN', 'SPEED_DOWN'],
  });
  runtime.advanceAndRunNext(2000);

  assert.deepEqual(
    runtime.sent.map(item => item.command),
    ['SPEED_DOWN', 'SPEED_DOWN'],
  );
});


test('unknown commands and a missing Android bridge are harmless', () => {
  const controller = createController({
    now: () => 0,
    setTimer: () => {
      throw new Error('no timer should be scheduled');
    },
    transmit: null,
  });

  assert.doesNotThrow(() => {
    controller.handleCommandEvent({ commands: ['POWER', 'SPEED_UP'] });
    controller.handleCommandEvent(null);
  });
});


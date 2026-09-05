'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

const { focusRowFromClick } = require('./static/message-row-focus.js');

function clickTarget(isInteractive) {
  return {
    closest(selector) {
      assert.equal(selector, 'textarea, input, select, button, a, [contenteditable="true"]');
      return isInteractive ? {} : null;
    },
  };
}

test('message row click keeps focus inside editors and other interactive controls', () => {
  let focusCalls = 0;
  const row = { focus() { focusCalls += 1; } };

  focusRowFromClick({ target: clickTarget(true) }, row);

  assert.equal(focusCalls, 0);
});

test('message row click focuses the row from ordinary message content', () => {
  let focusCalls = 0;
  const row = { focus() { focusCalls += 1; } };

  focusRowFromClick({ target: clickTarget(false) }, row);

  assert.equal(focusCalls, 1);
});

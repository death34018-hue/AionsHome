const assert = require('node:assert/strict');
const test = require('node:test');

const policy = require('./static/voice-call-policy.js');

test('short utterances finish after 800ms and longer utterances get 1200ms', () => {
  assert.equal(policy.silenceTimeoutMs(900), 800);
  assert.equal(policy.silenceTimeoutMs(2500), 1200);
});

test('tap-to-interrupt only stops TTS while the active call is speaking', () => {
  let stopped = 0;
  let stoppedMsgId = '';
  const adapter = { interruptTTS(msgId) { stopped += 1; stoppedMsgId = msgId; } };

  assert.equal(policy.requestInterruption({ active: true, speaking: true, adapter, msgId: 'msg_1' }), true);
  assert.equal(stopped, 1);
  assert.equal(stoppedMsgId, 'msg_1');
  assert.equal(policy.requestInterruption({ active: true, speaking: false, adapter }), false);
  assert.equal(stopped, 1);
});

const test = require('node:test');
const assert = require('node:assert/strict');

const queueApi = require('./static/theater_tts_queue.js');

test('waits for zero and plays observed Codex arrivals exactly once in order', () => {
  const queue = queueApi.createQueue();
  const arrivals = [14, 11, 12, 4, 9, 8, 13, 6, 10, 2, 0, 3, 5, 7, 1];
  const played = [];

  for (const seq of arrivals) {
    queueApi.upsert(queue, seq, `/s${seq}`);
    let item;
    while ((item = queueApi.peekNext(queue))) {
      played.push(item.seq);
      queueApi.advance(queue);
    }
  }

  queueApi.markFinished(queue);
  let item;
  while ((item = queueApi.peekNext(queue))) {
    played.push(item.seq);
    queueApi.advance(queue);
  }

  assert.deepEqual(played, Array.from({ length: 15 }, (_, index) => index));
  assert.equal(queueApi.isDrained(queue), true);
});

test('deduplicates a repeated sequence', () => {
  const queue = queueApi.createQueue();

  queueApi.upsert(queue, 0, '/old');
  queueApi.upsert(queue, 0, '/new');

  assert.equal(queue.chunks.size, 1);
  assert.equal(queueApi.peekNext(queue).url, '/new');
});

test('ignores a duplicate that arrives after its sequence already played', () => {
  const queue = queueApi.createQueue();

  queueApi.upsert(queue, 0, '/s0');
  queueApi.advance(queue);
  queueApi.upsert(queue, 0, '/s0-again');
  queueApi.markFinished(queue);

  assert.equal(queue.chunks.size, 0);
  assert.equal(queueApi.isDrained(queue), true);
});

test('skips a missing sequence only after the queue is finished', () => {
  const queue = queueApi.createQueue();

  queueApi.upsert(queue, 1, '/s1');
  assert.equal(queueApi.peekNext(queue), null);

  queueApi.markFinished(queue);
  assert.equal(queueApi.peekNext(queue).seq, 1);
});

test('formats long playback times without wrapping after one hour', () => {
  assert.equal(queueApi.formatTime(0), '00:00');
  assert.equal(queueApi.formatTime(65), '01:05');
  assert.equal(queueApi.formatTime(3661), '1:01:01');
  assert.equal(queueApi.formatTime(Number.NaN), '--:--');
});

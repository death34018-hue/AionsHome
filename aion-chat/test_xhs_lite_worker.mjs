import assert from 'node:assert/strict';
import test from 'node:test';
import worker from './xhs_lite_worker.js';

async function profileWith(posted) {
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    const path = new URL(url).pathname;
    if (path.endsWith('/user/otherinfo')) {
      return Response.json({ success: true, data: { basic_info: { user_id: 'target' } } });
    }
    assert.ok(path.endsWith('/user_posted'));
    if (posted instanceof Error) throw posted;
    return Response.json(posted);
  };
  try {
    const response = await worker.fetch(new Request('http://xhs-lite.local/api/user-profile', {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-xhs-cookie': 'a1=offline' },
      body: JSON.stringify({ user_id: 'target' }),
    }), {}, {});
    return await response.json();
  } finally {
    globalThis.fetch = realFetch;
  }
}

test('profile preserves the posted-list failure even when account info succeeds', async () => {
  const result = await profileWith({ success: false, code: -100, msg: '登录已过期' });
  assert.equal(result.success, false);
  assert.equal(result.raw_error.code, -100);
});

test('profile does not turn a network failure into an empty account', async () => {
  const result = await profileWith(new Error('offline request failed'));
  assert.match(result.error, /offline request failed/);
});

test('profile distinguishes a genuinely empty posted list', async () => {
  const result = await profileWith({ success: true, data: { notes: [] } });
  assert.equal(result.success, true);
  assert.deepEqual(result.notes, []);
  assert.equal(result.raw_error, undefined);
});

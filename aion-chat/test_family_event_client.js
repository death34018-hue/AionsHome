const assert = require('node:assert/strict');
const test = require('node:test');

const { reportFamilyEvent } = require('./static/family-event-client.js');

test('reports a configured pet-care event to the family timeline API', async () => {
  let request = null;
  const fetchImpl = async (url, options) => {
    request = { url, options };
    return { ok: true, json: async () => ({ ok: true }) };
  };

  await reportFamilyEvent('seeky_feed', 'Configured Pet', fetchImpl);

  assert.equal(request.url, '/api/family-events');
  assert.equal(request.options.method, 'POST');
  assert.deepEqual(JSON.parse(request.options.body), {
    kind: 'seeky_feed',
    subject_name: 'Configured Pet',
  });
});

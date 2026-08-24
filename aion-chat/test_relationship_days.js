'use strict';

const assert = require('assert');
const fs = require('fs');

const modulePath = './static/relationship-days.js';
assert.ok(fs.existsSync(modulePath), 'relationship day calculator should be available to the family dynamics page');

const { buildRelationshipAnniversary, getRelationshipAnniversary } = require(modulePath);
assert.strictEqual(typeof getRelationshipAnniversary, 'function');
assert.strictEqual(typeof buildRelationshipAnniversary, 'function');

assert.deepStrictEqual(
  getRelationshipAnniversary(null, new Date(2026, 7, 24, 23, 59)),
  { day: 1, since: '2026.08.24', configured: false, startedOn: '2026-08-24' },
  'an unset relationship date should preview today without becoming configured',
);
assert.deepStrictEqual(
  getRelationshipAnniversary('2025-06-09', new Date(2026, 7, 24, 12, 0)),
  { day: 442, since: '2025.06.09', configured: true, startedOn: '2025-06-09' },
  'a configured date should count local calendar days from the saved value',
);
assert.deepStrictEqual(
  buildRelationshipAnniversary('2025-06-09', 'Lumen', new Date(2026, 7, 24, 12, 0)),
  {
    name: 'Lumen',
    eyebrow: '✦ OUR DAYS ✦',
    lead: '在一起的第',
    day: 442,
    unit: '天',
    since: '始于 2025.06.09',
    ariaLabel: 'Lumen空间：在一起的第442天，始于2025.06.09',
    configured: true,
    startedOn: '2025-06-09',
  },
  'the plaque copy should use the configured role name',
);

console.log('relationship day calculator passed');

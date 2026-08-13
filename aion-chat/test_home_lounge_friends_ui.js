const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');


const ROOT = __dirname;
const HOME_PATH = path.join(ROOT, 'static', 'home.html');
const ICON_PATH = path.join(ROOT, '..', 'public', 'funIcon_0030_好友串门.png');


test('home app grid links to lounge friends with the supplied icon', () => {
  const html = fs.readFileSync(HOME_PATH, 'utf8');

  assert.match(
    html,
    /\{\s*id:\s*['"]lounge-friends['"],\s*name:\s*['"]好友串门['"],\s*icon:\s*['"]\/public\/funIcon_0030_好友串门\.png['"],\s*url:\s*['"]\/lounge-friends['"]\s*\}/,
  );
  assert.equal(fs.existsSync(ICON_PATH), true);
});

'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('supervision reveals its own shell before its external script loads and returns through the host', () => {
  const html = fs.readFileSync(path.join(__dirname, 'static/app-supervision.html'), 'utf8');
  const match = html.match(/<script id="supervision-navigation">([\s\S]*?)<\/script>/);
  assert.ok(match, 'supervision needs the same early readiness and host navigation as other pages');
  assert.ok(html.indexOf(match[0]) < html.indexOf('<script src='));
  const frame = {}, calls = [];
  let click;
  const parent = { AionSubPageNavigation: { ready: f => calls.push(f) }, openSubPage: url => calls.push(url) };
  const window = { parent, frameElement: frame };
  vm.runInNewContext(match[1], { window, document: { querySelector: () => ({ addEventListener: (_, fn) => click = fn }) } });
  assert.equal(calls[0], frame);
  click({ preventDefault: () => calls.push('prevented') });
  assert.deepEqual(calls, [frame, 'prevented', '/']);
  window.parent = window;
  vm.runInNewContext(match[1], { window, document: { querySelector: () => ({ addEventListener: (_, fn) => click = fn }) } });
  click({ preventDefault() { throw Error('standalone back must remain a normal link'); } });
});

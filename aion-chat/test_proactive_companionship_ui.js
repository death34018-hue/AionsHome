'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');

const capabilities = fs.readFileSync(`${__dirname}/static/capabilities.html`, 'utf8');
const chatHtml = fs.readFileSync(`${__dirname}/static/chat.html`, 'utf8');
const chatroomHtml = fs.readFileSync(`${__dirname}/static/chatroom.html`, 'utf8');
const chat = fs.readFileSync(`${__dirname}/static/chat.js`, 'utf8');
const chatroom = fs.readFileSync(`${__dirname}/static/chatroom.js`, 'utf8');
const common = fs.readFileSync(`${__dirname}/static/common.css`, 'utf8');
const chatCss = fs.readFileSync(`${__dirname}/static/chat.css`, 'utf8');
const chatroomCss = fs.readFileSync(`${__dirname}/static/chatroom.css`, 'utf8');
const orbitCss = fs.readFileSync(`${__dirname}/static/proactive-orbit.css`, 'utf8');
const { applyStatus } = require('./static/proactive-orbit.js');

assert.match(capabilities, /主动陪伴/);
assert.match(capabilities, /proactiveCompanionshipCard/);
assert.match(capabilities, /\/api\/proactive-companionship\/\$\{encodeURIComponent\(actor\)\}/);
assert.match(capabilities, /remaining_minutes/);

assert.match(chat, /proactive_companionship_changed/);
assert.match(chatroom, /proactive_companionship_changed/);
assert.match(chatroom, /function crStripScheduleCommands/);
assert.match(chatroom, /data\.message\.content = crStripScheduleCommands\(data\.message\.content\)/);

assert.match(chatHtml, /data-proactive-orb="aion"/);
assert.match(chatroomHtml, /data-proactive-orb="aion"/);
assert.match(chatroomHtml, /data-proactive-orb="connor"/);
assert.doesNotMatch(chatHtml, /proactive-orb-glow/);
assert.doesNotMatch(chatroomHtml, /proactive-orb-glow/);
assert.equal((chatHtml.match(/class="proactive-flow-svg"/g) || []).length, 1);
assert.equal((chatHtml.match(/class="proactive-flow-runner"/g) || []).length, 1);
assert.equal((chatHtml.match(/class="proactive-flow-step"/g) || []).length, 16);
assert.equal((chatroomHtml.match(/class="proactive-flow-svg"/g) || []).length, 2);
assert.equal((chatroomHtml.match(/class="proactive-flow-runner"/g) || []).length, 2);
assert.equal((chatroomHtml.match(/class="proactive-flow-step"/g) || []).length, 32);
assert.match(chatHtml, /pathLength="100"/);
assert.match(chatroomHtml, /pathLength="100"/);
assert.match(orbitCss, /proactiveOrbitClockwise/);
assert.match(orbitCss, /prefers-reduced-motion:\s*reduce/);
assert.doesNotMatch(orbitCss, /(?:^|\n)\.proactive-orbit-host\s*\{[^}]*position\s*:/s);
assert.doesNotMatch(orbitCss, /\.proactive-orbit-host\s*>\s*:not\(\.proactive-orbit\)/);
assert.match(orbitCss, /\.input-row\.proactive-orbit-host\s*\{[^}]*position:\s*relative/s);
assert.match(orbitCss, /\.proactive-orbit\s*\{[^}]*inset:\s*-4px/s);
assert.match(orbitCss, /\.proactive-flow-step\s*\{[^}]*width:\s*calc\(100% - 6px\)[^}]*height:\s*calc\(100% - 6px\)[^}]*stroke-linecap:\s*butt/s);
assert.match(orbitCss, /\.proactive-flow-step:nth-child\(1\)\s*\{[^}]*stroke-width:\s*4\.5[^}]*stroke-dasharray:\s*0 80 20 0[^}]*opacity:\s*\.025/s);
assert.match(orbitCss, /\.proactive-flow-step:nth-child\(16\)\s*\{[^}]*stroke-width:\s*\.9[^}]*stroke-dasharray:\s*0 98\.75 1\.25 0[^}]*opacity:\s*\.42[^}]*stroke-linecap:\s*round/s);
assert.match(orbitCss, /\.proactive-orb-aion\s+\.proactive-flow-runner\s*\{[^}]*stroke:\s*#26f087[^}]*animation:\s*proactiveOrbitClockwise\s+18s/s);
assert.match(orbitCss, /\.proactive-orb-connor\s+\.proactive-flow-runner\s*\{[^}]*stroke:\s*#238fff[^}]*transform:\s*scaleX\(-1\)[^}]*transform-box:\s*fill-box[^}]*transform-origin:\s*center[^}]*animation:\s*proactiveOrbitClockwise\s+18s/s);
assert.match(orbitCss, /\.proactive-orb-aion\s+\.proactive-flow-step:nth-child\(16\)\s*\{\s*stroke:\s*#ecfff3/);
assert.match(orbitCss, /\.proactive-orb-connor\s+\.proactive-flow-step:nth-child\(16\)\s*\{\s*stroke:\s*#eef7ff/);
assert.match(orbitCss, /@keyframes proactiveOrbitClockwise\s*\{[^}]*stroke-dashoffset:\s*0[^}]*\}[^}]*stroke-dashoffset:\s*-100/s);
assert.doesNotMatch(orbitCss, /proactiveOrbitCounterclockwise/);
assert.doesNotMatch(orbitCss, /--trail-offset/);
assert.doesNotMatch(orbitCss, /mask-composite|offset-path|proactiveOrbBreath|conic-gradient|proactive-flow-glow|proactive-flow-edge|proactive-flow-spin|proactive-orbit-cover|proactive-flow-aura|proactive-flow-tail|proactive-flow-core/);
assert.doesNotMatch(orbitCss, /filter\s*:/);
assert.doesNotMatch(orbitCss, /box-shadow\s*:/);
assert.doesNotMatch(common, /proactive-halo/);
assert.doesNotMatch(chatCss, /proactive-halo/);
assert.doesNotMatch(chatroomCss, /proactive-halo/);
assert.doesNotMatch(chat, /proactive-halo/);
assert.doesNotMatch(chatroom, /proactive-halo/);
assert.match(chatHtml, /proactive-orbit\.css[^"']*20260817/);
assert.match(chatroomHtml, /proactive-orbit\.css[^"']*20260817/);
assert.match(chatroomHtml, /schedule-command-filter\.js[^"']*next-chat-20260816/);
assert.match(chatroomHtml, /chatroom\.js[^"']*next-chat-finish-20260816/);

const elements = {
  aion: { hidden: true },
  connor: { hidden: true },
};
const root = {
  querySelector(selector) {
    const actor = selector.match(/"(aion|connor)"/)?.[1];
    return actor ? elements[actor] : null;
  },
};

applyStatus({ aion: true, connor: true }, ['aion', 'connor'], root);
assert.equal(elements.aion.hidden, false);
assert.equal(elements.connor.hidden, false);

applyStatus({ aion: true, connor: true }, ['connor'], root);
assert.equal(elements.aion.hidden, true);
assert.equal(elements.connor.hidden, false);

applyStatus({ aion: false, connor: false }, ['aion', 'connor'], root);
assert.equal(elements.aion.hidden, true);
assert.equal(elements.connor.hidden, true);

console.log('proactive companionship UI tests passed');

'use strict';

const assert = require('assert');
const fs = require('fs');

const family = fs.readFileSync('static/family-dynamics.html', 'utf8');
const activity = fs.readFileSync('static/activity-logs.html', 'utf8');
const home = fs.readFileSync('static/home.html', 'utf8');
const privateSpaceAssets = [
  '../public/private-space/board.webp',
  '../public/private-space/aion-door.webp',
  '../public/private-space/connor-door.webp',
  '../public/private-space/day-frame.webp',
  '../public/private-space/night-frame.webp',
];

for (const asset of privateSpaceAssets) {
  assert.ok(fs.existsSync(asset), `${asset} should be packaged with the private-space UI`);
  assert.ok(fs.statSync(asset).size < 350 * 1024, `${asset} should stay mobile-friendly`);
}

assert.match(family, /id="timelinePane"/);
assert.match(family, /id="autonomyControls"/);
assert.doesNotMatch(family, /id="autonomyPane"/);
assert.match(family, /id="privateSpaceTabs"/);
assert.match(family, /id="privateSpacePane"/);
assert.match(family, /id="privateSpaceScene"/);
assert.match(family, /src="\/static\/relationship-days\.js"/);
assert.match(family, /class="relationship-plaque"/);
assert.match(family, /onclick="openRelationshipDateEditor\(\)"/);
assert.match(family, /id="relationshipDateEditor"/);
assert.match(family, /id="relationshipDateInput" type="date"/);
assert.match(family, /function openRelationshipDateEditor\(\)/);
assert.match(family, /async function saveRelationshipDate\(\)/);
assert.match(family, /\/relationship-date`/);
assert.match(family, /buildRelationshipAnniversary\(role\.config\?\.relationship_started_on,name\)/);
assert.match(family, /class="space-layer space-board"/);
assert.match(family, /class="space-cards"/);
assert.match(family, /class="space-door"/);
assert.match(family, /class="space-layer space-frame day-frame"/);
assert.match(family, /class="space-layer space-frame night-frame"/);
assert.match(family, /\/public\/private-space\/board\.webp/);
assert.match(family, /\/public\/private-space\/\$\{doorAsset\}/);
assert.match(family, /body\[data-theme="dark"\] \.day-frame/);
assert.match(family, /body\[data-theme="dark"\] \.night-frame/);
assert.match(family, /#privateSpacePane\{--paper:#f7f0e5;/);
assert.match(family, /body\[data-theme="dark"\] \.postcard-sheet\{[^}]*#121a35/);
assert.match(family, /body\[data-theme="dark"\] \.detail-text\{color:#e8ebff/);
assert.match(family, /function renderPrivateSpaceTabs\(\)/);
assert.match(family, /function openPrivateSpace\(\)/);
assert.match(family, /classList\.add\('open'\)/);
assert.match(family, /classList\.remove\('open'\)/);
assert.match(family, /function nicheSlot\(index\)/);
assert.match(family, /style="\$\{nicheSlot\(index\)\}"/);
assert.match(family, /const NICHE_PAGE_SIZE=12/);
assert.match(family, /function changeNichePage\(delta\)/);
assert.match(family, /class="niche-pager"/);
assert.match(family, /class="postcard-sheet"/);
assert.match(family, /class="postcard-menu"/);
assert.match(family, /id="postcardMentionToggle"/);
assert.match(family, /async function toggleNicheMention\(\)/);
assert.match(family, /api\('PATCH',`\/api\/idle-autonomy\/niches\/\$\{encodeURIComponent\(card\.id\)\}\?actor=/);
assert.match(family, /card\.mentioned\?'设为未提及':'设为已提及'/);
assert.match(family, /\.postcard-sheet::before\{content:none\}/);
assert.doesNotMatch(family, /OUR LITTLE SECRET/);
assert.doesNotMatch(family, /\.postcard-sheet::after\{content:'✦'/);
assert.match(family, /async function deleteNicheCard\(\)/);
assert.match(family, /api\('DELETE',`\/api\/idle-autonomy\/niches\/\$\{encodeURIComponent\(card\.id\)\}\?actor=/);
assert.match(family, /私人空间/);
assert.match(family, /\/api\/idle-autonomy\/niches\?actor=/);
assert.match(family, /class="niche-sources"/);
assert.match(family, /class="niche-state \$\{card\.mentioned\?'mentioned':'fresh'\}"/);
assert.match(family, /card\.mentioned\?'已提及':'未提及'/);
assert.doesNotMatch(family, /<details class="niche-sources"\s+open/);
assert.match(family, /confirm\(/);
assert.match(family, /\/api\/idle-autonomy\/\$\{actor\}\/config/);
assert.match(family, /class="role-test" onclick="testWake\('\$\{role\.actor\}'\)"/);
assert.match(family, /<details class="settings">/);
assert.doesNotMatch(family, /<details class="settings"\s+open/);
assert.match(family, /<summary>配置与操作<\/summary>/);
assert.match(family, /下次随机唤醒/);
assert.match(family, /无新消息后随机间隔/);
assert.doesNotMatch(family, /<div class="packet">/);
assert.doesNotMatch(family, /状态包历史/);
assert.doesNotMatch(family, /醒来目的/);
assert.doesNotMatch(family, /打扰策略/);
assert.doesNotMatch(activity, /id="tabTimeline"/);
assert.match(home, /funIcon_0031_家庭动态\.png/);
assert.match(home, /url: '\/family-dynamics'/);

console.log('family dynamics UI contract passed');

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {
  parseLrc,
  mergeLyrics,
  playbackBounds,
  nextTrackIndex,
  setMusicVolume,
  trackRowAction,
  afterTracksDeleted,
} = require('./static/music-station.js');

const lines = parseLrc('[00:01.25]第一句\n[01:02.50]第二句\n没有时间');
assert.deepEqual(lines, [
  { time: 1.25, text: '第一句' },
  { time: 62.5, text: '第二句' },
]);

assert.deepEqual(
  mergeLyrics('[00:01.00]你好', '[00:01.00]Hello'),
  [{ time: 1, text: '你好', translation: 'Hello' }],
);

assert.deepEqual(
  playbackBounds({ trim_start_ms: 1500, trim_end_ms: 8000 }, 10),
  { start: 1.5, end: 8 },
);
assert.deepEqual(
  playbackBounds({ trim_start_ms: 0, trim_end_ms: 0 }, 10),
  { start: 0, end: 10 },
);

assert.equal(nextTrackIndex(1, 3, 'list'), 2);
assert.equal(nextTrackIndex(2, 3, 'list'), 0);
assert.equal(nextTrackIndex(1, 3, 'one'), 1);
assert.equal(nextTrackIndex(2, 3, 'none'), -1);

const musicAudio = { volume: 0 };
assert.equal(typeof setMusicVolume, 'function');
assert.equal(setMusicVolume(musicAudio, 73), 73);
assert.equal(musicAudio.volume, 0.73);
assert.equal(setMusicVolume(musicAudio, 140), 100);
assert.equal(musicAudio.volume, 1);
assert.equal(typeof trackRowAction, 'function');
assert.equal(trackRowAction(false), 'play');
assert.equal(trackRowAction(true), 'select');
assert.deepEqual(afterTracksDeleted('t1', ['t1']), { currentTrackId: '', shouldStop: true });
assert.deepEqual(afterTracksDeleted('t1', ['t2']), { currentTrackId: 't1', shouldStop: false });

const chatJs = fs.readFileSync(path.join(__dirname, 'static', 'chat.js'), 'utf8');
const persistentFunctionSource = chatJs.match(/function isPersistentSubPage\(url\)\s*\{[\s\S]*?\n\}/);
assert.ok(persistentFunctionSource, 'isPersistentSubPage should exist');
const isPersistentSubPage = Function(
  'subPagePath',
  `${persistentFunctionSource[0]}; return isPersistentSubPage;`,
)(value => value);
assert.equal(isPersistentSubPage('/music-station'), true);

const homeHtml = fs.readFileSync(path.join(__dirname, 'static', 'home.html'), 'utf8');
assert.match(homeHtml, /id:\s*'music-station'[\s\S]*?name:\s*'点歌台'[\s\S]*?icon:\s*'\/public\/funIcon_0032_点歌台\.png'[\s\S]*?url:\s*'\/music-station'/);
assert.ok(fs.existsSync(path.join(__dirname, '..', 'public', 'funIcon_0032_点歌台.png')));

const stationHtml = fs.readFileSync(path.join(__dirname, 'static', 'music-station.html'), 'utf8');
const stationCss = fs.readFileSync(path.join(__dirname, 'static', 'music-station.css'), 'utf8');
assert.match(stationHtml, /id="uploadButton"[^>]*aria-label="上传本地歌曲"[^>]*>＋<\/button>/);
assert.match(stationHtml, /id="libraryViewButton"[^>]*data-view="library"/);
assert.match(stationHtml, /id="lyricsViewButton"[^>]*data-view="lyrics"/);
assert.match(stationHtml, /id="volumeButton"[^>]*aria-label="歌曲音量"/);
assert.match(stationHtml, /id="volumePanel"[^>]*hidden/);
for (const id of ['playlistBar', 'manageButton', 'manageActions', 'playlistDialog', 'deleteTracksDialog']) {
  assert.match(stationHtml, new RegExp(`id="${id}"`));
}
assert.match(stationCss, /\.track-card\s*\{[^}]*grid-template-columns:\s*32px minmax\(0, 1fr\);/);
assert.match(stationCss, /\.track-list\s*\{[^}]*flex:\s*1/);
assert.match(stationCss, /\.empty-state\[hidden\]\s*\{\s*display:\s*none/);
assert.match(stationCss, /@media \(max-width:\s*850px\)[\s\S]*body\.view-lyrics \.library-panel\s*\{\s*display:\s*none/);
assert.match(stationCss, /@media \(max-width:\s*850px\)[\s\S]*body\.view-library \.side-panel\s*\{\s*display:\s*none/);

console.log('music station UI behavior tests passed');

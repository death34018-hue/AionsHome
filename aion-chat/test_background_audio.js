const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const read = file => fs.readFileSync(path.join(__dirname, 'static', file), 'utf8');

test('monitor cues use native playback while the App WebView is hidden', () => {
  for (const file of ['chat.js', 'common.js']) {
    const source = read(file);
    // Exercise each cue's actual playback block, without booting the whole chat UI.
    const blocks = [...source.matchAll(/(?:const audio = new Audio\('\/public\/AionMonitoralart\.mp3'\);\s*audio\.play\(\)\.catch\(\(\) => \{\}\);|if \(window\.AionTtsAudio[\s\S]*?audio\.play\(\)\.catch\(\(\) => \{\}\);\s*\})/g)];
    assert.equal(blocks.length, file === 'chat.js' ? 2 : 1);
    for (const [block] of blocks) {
      const played = [];
      const context = {
        window: { AionTtsAudio: { play: (id, url) => { played.push(url); return true; } } },
        Audio: class { play() { throw new Error('hidden HTML audio cannot start'); } },
      };
      vm.runInNewContext(block, context);
      assert.deepEqual(played, ['/public/AionMonitoralart.mp3']);
      let fallbackUrl;
      vm.runInNewContext(block, {
        window: {},
        Audio: class { constructor(url) { fallbackUrl = url; } play() { return Promise.resolve(); } },
      });
      assert.equal(fallbackUrl, '/public/AionMonitoralart.mp3');
    }
  }
});

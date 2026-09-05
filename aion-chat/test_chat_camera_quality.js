'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

for (const room of [false, true]) {
  const label = room ? 'chatroom' : 'chat';
  const source = fs.readFileSync(path.join(__dirname, 'static', `${label}.js`), 'utf8');
  const start = room ? '_crStartCam' : 'startCam';
  const capture = room ? 'crCapturePhoto' : 'capturePhoto';
  const begin = source.indexOf(room ? 'let _crCamOverlay' : 'let _camOverlay');
  const end = source.indexOf(room ? 'let _crVoiceMode' : 'let _voiceAudio', begin);

  function setup(getUserMedia, bridge) {
    const calls = { uploads: [], attachments: [] };
    const video = { videoWidth: 1280, videoHeight: 960, style: {}, play: async () => {} };
    const image = { style: {} };
    const canvas = {
      getContext: () => ({ drawImage: (...args) => { calls.draw = args; } }),
      toDataURL: (type, quality) => {
        calls.encoding = [type, quality, canvas.width, canvas.height];
        return 'data:image/jpeg;base64,photo';
      },
    };
    const context = {
      navigator: { mediaDevices: { getUserMedia } },
      document: {
        getElementById: id => id.endsWith('Video') ? video : image,
        createElement: () => canvas,
      },
      window: { AionCamera: bridge },
      _getNativeBridge: () => bridge,
      requestAnimationFrame: () => 1,
      cancelAnimationFrame: () => {},
      console: { warn: () => {} },
      alert: message => assert.fail(message),
      toast: message => assert.fail(message),
      API: '/api/chatroom',
      pendingAttachments: calls.attachments,
      renderPreview: () => {},
      FormData,
      fetch: async (url, options) => {
        if (url.startsWith('data:')) return { blob: async () => new Blob(['photo']) };
        calls.uploads.push([url, options]);
        return { json: async () => ({ url: '/uploads/photo.jpg', type: 'image/jpeg' }) };
      },
    };
    vm.runInNewContext(source.slice(begin, end), context);
    return { context, calls };
  }

  test(`${label}: browser photo requests more detail and uploads quality 90 JPEG`, async () => {
    let constraints;
    const { context, calls } = setup(async value => {
      constraints = value;
      return { getTracks: () => [{ stop() {} }] };
    });
    await context[start]();
    assert.equal(constraints.video.width.ideal, 1280);
    assert.equal(constraints.video.height.ideal, 960);
    await context[capture]();
    assert.deepEqual(calls.encoding, ['image/jpeg', 0.9, 1280, 960]);
    assert.equal(calls.uploads.length, 1);
    assert.equal(calls.attachments[0].url, '/uploads/photo.jpg');
  });

  test(`${label}: native photo opts into high resolution`, async () => {
    const starts = [];
    const bridge = {
      start: facing => { starts.push(['legacy', facing]); return true; },
      startPhoto: facing => { starts.push(['photo', facing]); return true; },
      getFrame: () => null,
      capture: () => 'photo',
      stop() {},
    };
    const { context, calls } = setup(async () => { throw new Error('HTTP camera fallback'); }, bridge);
    await context[start]();
    assert.deepEqual(starts, [['photo', 'environment']]);
    await context[capture]();
    assert.equal(calls.attachments.length, 1);
  });

  test(`${label}: older APK can still open the camera`, async () => {
    let facing;
    const { context } = setup(async () => { throw new Error('HTTP camera fallback'); }, {
      start: value => { facing = value; return true; },
      getFrame: () => null,
    });
    await context[start]();
    assert.equal(facing, 'environment');
  });
}

'use strict';

const assert = require('node:assert/strict');
const {createSecurityAlertUI} = require('./static/security-alert.js');


class FakeClassList {
  constructor() { this.values = new Set(); }
  add(...names) { names.forEach(name => this.values.add(name)); }
  remove(...names) { names.forEach(name => this.values.delete(name)); }
  contains(name) { return this.values.has(name); }
}


class FakeElement {
  constructor(tagName, ownerDocument) {
    this.tagName = tagName.toUpperCase();
    this.ownerDocument = ownerDocument;
    this.children = [];
    this.classList = new FakeClassList();
    this.className = '';
    this.textContent = '';
    this.type = '';
    this.onclick = null;
    this._id = '';
  }

  set id(value) {
    this._id = value;
    this.ownerDocument.elements.set(value, this);
  }

  get id() { return this._id; }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  replaceChildren(...children) {
    this.children = [...children];
    this.textContent = '';
  }
}


class FakeDocument {
  constructor() {
    this.elements = new Map();
    this.listeners = new Map();
    this.head = new FakeElement('head', this);
    this.body = new FakeElement('body', this);
  }

  createElement(tagName) { return new FakeElement(tagName, this); }
  getElementById(id) { return this.elements.get(id) || null; }
  addEventListener(type, handler) { this.listeners.set(type, handler); }
  removeEventListener(type, handler) {
    if (this.listeners.get(type) === handler) this.listeners.delete(type);
  }
  async dispatch(type) {
    const handler = this.listeners.get(type);
    if (handler) await handler();
  }
}


function createStorage() {
  const values = new Map();
  return {
    getItem: key => values.has(key) ? values.get(key) : null,
    setItem: (key, value) => values.set(key, String(value)),
  };
}


function createHarness({pending = [], rejectAudio = false, storage = createStorage()} = {}) {
  const document = new FakeDocument();
  const requests = [];
  const audioSources = [];
  const audioState = {reject: rejectAudio};
  class FakeAudio {
    constructor(src) { audioSources.push(src); }
    play() {
      return audioState.reject
        ? Promise.reject(new Error('autoplay blocked'))
        : Promise.resolve();
    }
  }
  const fetch = async (url, options = {}) => {
    requests.push({url, options});
    return {
      ok: true,
      async json() {
        return url.endsWith('/alerts/pending') ? {alerts: pending} : {trusted: true};
      },
    };
  };
  const ui = createSecurityAlertUI({document, storage, fetch, Audio: FakeAudio});
  return {ui, document, requests, audioSources, audioState};
}


const warning = {
  alert_id: 'warning-1',
  level: 'warning',
  reason: 'unknown_device',
  ip: '8.8.8.8',
  source: 'public',
  timestamp: '2026-08-06T20:00:00+08:00',
  location: 'United States · California · Mountain View · Google LLC',
  location_kind: 'overseas',
  location_notice: '境外访问，请提高警惕',
};


(async () => {
  {
    const {ui, document, audioSources} = createHarness();
    await ui.handleMessage({type: 'security_alert', data: warning});
    await ui.handleMessage({type: 'security_alert', data: warning});

    assert.equal(audioSources.length, 1);
    assert.equal(audioSources[0], '/public/strangealert.mp3');
    assert.equal(document.getElementById('securityAlertTitle').textContent, '发现陌生设备访问');
    const rows = document.getElementById('securityAlertMeta').children;
    assert.equal(rows.length, 4);
    assert.deepEqual(rows.map(row => row.textContent), [
      'IP：8.8.8.8',
      '归属地：United States · California · Mountain View · Google LLC',
      '来源：public',
      '时间：2026-08-06T20:00:00+08:00',
    ]);
    assert.equal(document.getElementById('securityAlertLocationNotice').textContent, '境外访问，请提高警惕');
    assert.equal(document.getElementById('securityAlertOverlay').classList.contains('show'), true);
  }

  {
    const sharedStorage = createStorage();
    const firstPage = createHarness({storage: sharedStorage});
    await firstPage.ui.handleMessage({type: 'security_alert', data: warning});
    const reopenedPage = createHarness({storage: sharedStorage});
    await reopenedPage.ui.handleMessage({type: 'security_alert', data: warning});

    assert.equal(firstPage.audioSources.length, 1);
    assert.equal(reopenedPage.audioSources.length, 1);
  }

  {
    const {ui, document, audioSources, audioState} = createHarness({rejectAudio: true});
    await assert.doesNotReject(() => ui.handleMessage({
      type: 'security_alert',
      data: {...warning, alert_id: 'serious-1', level: 'serious', reason: 'single_ip_rate'},
    }));
    assert.equal(document.getElementById('securityAlertTitle').textContent, '严重安全事件');
    assert.equal(document.getElementById('securityAlertOverlay').classList.contains('serious'), true);
    audioState.reject = false;
    await document.dispatch('click');
    assert.equal(audioSources.length, 2);
  }

  {
    const {ui, document, requests} = createHarness();
    await ui.handleMessage({type: 'security_alert', data: warning});
    await document.getElementById('securityAlertTrust').onclick();

    assert.equal(requests[0].url, '/api/security-access/alerts/warning-1/trust-source');
    assert.equal(requests[0].options.method, 'POST');
    assert.equal(requests.length, 1);
    assert.equal(document.getElementById('securityAlertOverlay').classList.contains('show'), false);
  }

  {
    const {ui, document} = createHarness();
    await ui.handleMessage({
      type: 'security_alert',
      data: {
        ...warning,
        alert_id: 'lan-1',
        ip: '192.168.1.178',
        source: 'lan',
        location: undefined,
        location_kind: undefined,
        location_notice: undefined,
      },
    });

    const rows = document.getElementById('securityAlertMeta').children;
    assert.equal(rows[1].textContent, '归属地：局域网设备（本地网络）');
    assert.equal(document.getElementById('securityAlertLocationNotice').textContent, '');
  }

  {
    const {ui, document} = createHarness();
    await ui.handleMessage({
      type: 'security_alert',
      data: {...warning, alert_id: 'unknown-location-1', location: '', location_notice: ''},
    });
    assert.equal(
      document.getElementById('securityAlertMeta').children[1].textContent,
      '归属地：暂时无法确定',
    );
  }

  {
    const recovered = {...warning, alert_id: 'recovered-1'};
    const {ui, document, requests} = createHarness({pending: [recovered]});
    await ui.init();

    assert.equal(requests[0].url, '/api/security-access/alerts/pending');
    assert.equal(document.getElementById('securityAlertMeta').children[0].textContent, 'IP：8.8.8.8');
  }

  console.log('security access UI tests passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});

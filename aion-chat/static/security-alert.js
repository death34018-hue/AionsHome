(function (root, factory) {
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.AionSecurityAlerts = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';

  function createSecurityAlertUI(overrides = {}) {
    const documentRef = overrides.document || root.document;
    const fetchRef = overrides.fetch || (root.fetch && root.fetch.bind(root));
    const AudioRef = overrides.Audio || root.Audio;
    let initialized = false;
    let activeAlert = null;
    const pendingAudio = new Set();
    const activeAudios = new Set();
    const interactionRetryHandlers = new Set();
    const interactionEvents = ['pointerdown', 'touchend', 'click'];

    const seenAlertIds = new Set();

    function rememberSeen(alertId) {
      if (!alertId || seenAlertIds.has(String(alertId))) return false;
      seenAlertIds.add(String(alertId));
      return true;
    }

    function ensureDom() {
      if (!documentRef) return null;
      let overlay = documentRef.getElementById('securityAlertOverlay');
      if (overlay) return overlay;

      if (!documentRef.getElementById('aionSecurityAlertStyles')) {
        const style = documentRef.createElement('style');
        style.id = 'aionSecurityAlertStyles';
        style.textContent = `
#securityAlertOverlay{position:fixed;inset:0;z-index:2147483000;display:none;align-items:center;justify-content:center;padding:20px;background:rgba(5,8,20,.58);backdrop-filter:blur(7px)}
#securityAlertOverlay.show{display:flex}#securityAlertOverlay.flash-red{animation:aion-security-flash-red .42s ease-in-out 3}@keyframes aion-security-flash-red{0%,100%{background:rgba(5,8,20,.58);box-shadow:inset 0 0 0 rgba(239,68,68,0)}50%{background:rgba(130,0,12,.78);box-shadow:inset 0 0 80px rgba(255,30,30,.95)}}#securityAlertOverlay .security-alert-card{width:min(430px,100%);border:1px solid rgba(251,146,60,.48);border-radius:22px;padding:22px;color:#fff;background:linear-gradient(155deg,rgba(48,25,21,.98),rgba(20,15,31,.98));box-shadow:0 24px 80px rgba(0,0,0,.5)}
#securityAlertOverlay.serious .security-alert-card{border-color:rgba(248,113,113,.8);background:linear-gradient(155deg,rgba(76,16,24,.98),rgba(26,10,23,.98));box-shadow:0 24px 90px rgba(220,38,38,.32)}
#securityAlertTitle{margin:0 0 10px;font-size:22px}#securityAlertBody{margin:0 0 12px;color:rgba(255,255,255,.86);line-height:1.6}#securityAlertMeta{margin:0;padding:11px 13px;border-radius:13px;background:rgba(255,255,255,.07);color:rgba(255,255,255,.72);font-size:13px;line-height:1.55;overflow-wrap:anywhere}.security-alert-meta-row{display:block;margin:2px 0}#securityAlertLocationNotice{display:none;margin:8px 0 18px;padding:7px 10px;border-radius:10px;font-size:12px;font-weight:700}#securityAlertLocationNotice.has-notice{display:block}#securityAlertLocationNotice.location-beijing{color:#fed7aa;background:rgba(251,146,60,.13)}#securityAlertLocationNotice.location-domestic{color:#fdba74;background:rgba(234,88,12,.16)}#securityAlertLocationNotice.location-overseas{color:#fecaca;background:rgba(220,38,38,.2)}
.security-alert-actions{display:flex;flex-wrap:wrap;gap:10px}.security-alert-actions button{flex:1 1 120px;border:0;border-radius:999px;padding:11px 14px;font-weight:700;cursor:pointer}.security-alert-trust{color:#25130b;background:#fdba74}.security-alert-block{color:#fff;background:#b91c1c}.security-alert-dismiss{color:#fff;background:rgba(255,255,255,.13)}@media(prefers-reduced-motion:reduce){#securityAlertOverlay.flash-red{animation:none;box-shadow:inset 0 0 45px rgba(239,68,68,.72)}}
`;
        documentRef.head.appendChild(style);
      }

      overlay = documentRef.createElement('div');
      overlay.id = 'securityAlertOverlay';
      const card = documentRef.createElement('section');
      card.classList.add('security-alert-card');
      const title = documentRef.createElement('h2');
      title.id = 'securityAlertTitle';
      const body = documentRef.createElement('p');
      body.id = 'securityAlertBody';
      const meta = documentRef.createElement('div');
      meta.id = 'securityAlertMeta';
      const locationNotice = documentRef.createElement('div');
      locationNotice.id = 'securityAlertLocationNotice';
      const actions = documentRef.createElement('div');
      actions.classList.add('security-alert-actions');
      const trust = documentRef.createElement('button');
      trust.id = 'securityAlertTrust';
      trust.type = 'button';
      trust.classList.add('security-alert-trust');
      trust.textContent = '这是我的设备';
      const block = documentRef.createElement('button');
      block.id = 'securityAlertBlock';
      block.type = 'button';
      block.classList.add('security-alert-block');
      block.textContent = '封锁此 IP 24 小时';
      const dismiss = documentRef.createElement('button');
      dismiss.id = 'securityAlertDismiss';
      dismiss.type = 'button';
      dismiss.classList.add('security-alert-dismiss');
      dismiss.textContent = '知道了';
      trust.onclick = () => trustCurrentDevice();
      block.onclick = () => blockCurrentIp();
      dismiss.onclick = () => dismissCurrentAlert();
      actions.appendChild(trust);
      actions.appendChild(block);
      actions.appendChild(dismiss);
      card.appendChild(title);
      card.appendChild(body);
      card.appendChild(meta);
      card.appendChild(locationNotice);
      card.appendChild(actions);
      overlay.appendChild(card);
      documentRef.body.appendChild(overlay);
      return overlay;
    }

    async function playAlert(alertId) {
      const id = String(alertId || '');
      if (!id || seenAlertIds.has(id) || pendingAudio.has(id) || !AudioRef) return;
      pendingAudio.add(id);
      const attempt = async (allowInteractionRetry) => {
        let audio = null;
        try {
          audio = new AudioRef('/public/strangealert.mp3');
          activeAudios.add(audio);
          if (audio.addEventListener) {
            audio.addEventListener('ended', () => activeAudios.delete(audio), {once: true});
          }
          const playback = audio.play();
          if (playback && typeof playback.then === 'function') await playback;
          rememberSeen(id);
          pendingAudio.delete(id);
        } catch (_) {
          if (audio) activeAudios.delete(audio);
          if (allowInteractionRetry && documentRef && documentRef.addEventListener) {
            const retry = () => {
              for (const eventName of interactionEvents) {
                if (documentRef.removeEventListener) {
                  documentRef.removeEventListener(eventName, retry);
                }
              }
              interactionRetryHandlers.delete(retry);
              return attempt(false);
            };
            interactionRetryHandlers.add(retry);
            for (const eventName of interactionEvents) {
              documentRef.addEventListener(eventName, retry, {once: true});
            }
          } else {
            pendingAudio.delete(id);
          }
        }
      };
      await attempt(true);
    }

    function stopAlertAudio() {
      for (const retry of interactionRetryHandlers) {
        for (const eventName of interactionEvents) {
          if (documentRef && documentRef.removeEventListener) {
            documentRef.removeEventListener(eventName, retry);
          }
        }
      }
      interactionRetryHandlers.clear();
      for (const audio of activeAudios) {
        try {
          if (audio.pause) audio.pause();
          audio.currentTime = 0;
        } catch (_) {}
      }
      activeAudios.clear();
      pendingAudio.clear();
    }

    function describeReason(alert) {
      if (alert.reason === 'single_ip_rate') return '同一陌生 IP 在短时间内频繁访问。';
      if (alert.reason === 'distinct_ip_rate') return '短时间内出现了多个不同的陌生 IP。';
      return '发现尚未确认的设备或浏览器正在访问 AionsHome。';
    }

    function fallbackLocation(alert) {
      if (alert.source === 'lan') return '局域网设备（本地网络）';
      if (alert.source === 'localhost') return '服务器本机';
      if (alert.source === 'tailscale') return 'Tailscale 私有网络';
      return '暂时无法确定';
    }

    function isBlockableIp(alert) {
      if (!alert || !['public', 'cloudflare'].includes(alert.source)) return false;
      const ip = String(alert.ip || '').trim().toLowerCase();
      const parts = ip.split('.');
      if (parts.length === 4 && parts.every(part => /^\d{1,3}$/.test(part))) {
        const numbers = parts.map(Number);
        if (numbers.some(number => number < 0 || number > 255)) return false;
        const [a, b] = numbers;
        return !(
          a === 0 || a === 10 || a === 127 || a >= 224 ||
          a === 169 && b === 254 ||
          a === 172 && b >= 16 && b <= 31 ||
          a === 192 && b === 168 ||
          a === 100 && b >= 64 && b <= 127
        );
      }
      if (ip.includes(':')) {
        return !(ip === '::1' || ip.startsWith('fc') || ip.startsWith('fd') || ip.startsWith('fe80:'));
      }
      return false;
    }

    function renderMeta(alert) {
      const meta = documentRef.getElementById('securityAlertMeta');
      if (!meta) return;
      meta.replaceChildren();
      const values = [
        ['IP', alert.ip || '未知'],
        ['归属地', alert.location || fallbackLocation(alert)],
        ['来源', alert.source || '未知'],
        ['时间', alert.timestamp || '未知'],
      ];
      for (const [label, value] of values) {
        const row = documentRef.createElement('div');
        row.classList.add('security-alert-meta-row');
        row.textContent = `${label}：${value}`;
        meta.appendChild(row);
      }

      const notice = documentRef.getElementById('securityAlertLocationNotice');
      if (!notice) return;
      notice.className = '';
      notice.classList.remove('has-notice', 'location-beijing', 'location-domestic', 'location-overseas');
      notice.textContent = alert.location_notice || '';
      if (notice.textContent) {
        notice.classList.add('has-notice');
        if (['beijing', 'domestic', 'overseas'].includes(alert.location_kind)) {
          notice.classList.add(`location-${alert.location_kind}`);
        }
      }
    }

    async function showAlert(alert) {
      if (!alert || !alert.alert_id) return;
      const overlay = ensureDom();
      if (!overlay) return;
      if (activeAlert && activeAlert.alert_id !== alert.alert_id) stopAlertAudio();
      activeAlert = alert;
      const serious = alert.level === 'serious';
      overlay.classList.remove('serious');
      if (serious) overlay.classList.add('serious');
      documentRef.getElementById('securityAlertTitle').textContent = serious
        ? '严重安全事件'
        : '发现陌生设备访问';
      documentRef.getElementById('securityAlertBody').textContent = describeReason(alert);
      renderMeta(alert);
      const blockButton = documentRef.getElementById('securityAlertBlock');
      if (blockButton) blockButton.hidden = !isBlockableIp(alert);
      overlay.classList.remove('flash-red');
      void overlay.offsetWidth;
      overlay.classList.add('flash-red');
      overlay.classList.add('show');
      await playAlert(alert.alert_id);
    }

    async function post(url, body) {
      if (!fetchRef) return null;
      try {
        return await fetchRef(url, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body || {}),
        });
      } catch (_) {
        return null;
      }
    }

    async function acknowledge(alert) {
      if (!alert || !alert.alert_id) return;
      await post(`/api/security-access/alerts/${encodeURIComponent(alert.alert_id)}/ack`);
    }

    async function trustCurrentDevice() {
      const alert = activeAlert;
      if (!alert || !alert.alert_id) return;
      stopAlertAudio();
      const response = await post(
        `/api/security-access/alerts/${encodeURIComponent(alert.alert_id)}/trust-source`,
        {label: ''},
      );
      if (!response || response.ok === false) return;
      const overlay = ensureDom();
      if (overlay) overlay.classList.remove('show');
      activeAlert = null;
    }

    async function dismissCurrentAlert() {
      const alert = activeAlert;
      stopAlertAudio();
      await acknowledge(alert);
      const overlay = ensureDom();
      if (overlay) overlay.classList.remove('show');
      activeAlert = null;
    }

    async function blockCurrentIp() {
      const alert = activeAlert;
      if (!alert || !alert.alert_id) return;
      stopAlertAudio();
      const response = await post(
        `/api/security-access/alerts/${encodeURIComponent(alert.alert_id)}/block-24h`,
        {},
      );
      if (!response || response.ok === false) return;
      const overlay = ensureDom();
      if (overlay) overlay.classList.remove('show');
      activeAlert = null;
    }

    async function handleMessage(message) {
      try {
        if (!message || message.type !== 'security_alert') return;
        await showAlert(message.data || {});
      } catch (_) {}
    }

    async function init() {
      if (initialized) return;
      initialized = true;
      ensureDom();
      if (!fetchRef) return;
      try {
        const response = await fetchRef('/api/security-access/alerts/pending');
        if (!response || response.ok === false) return;
        const payload = await response.json();
        const alerts = Array.isArray(payload && payload.alerts) ? payload.alerts : [];
        for (const alert of alerts) await showAlert(alert);
      } catch (_) {}
    }

    return {init, handleMessage, trustCurrentDevice, dismissCurrentAlert};
  }

  const singleton = createSecurityAlertUI();
  return {
    createSecurityAlertUI,
    init: singleton.init,
    handleMessage: singleton.handleMessage,
  };
});

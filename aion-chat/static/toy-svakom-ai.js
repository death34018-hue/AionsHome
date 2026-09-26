(function (root, factory) {
  const protocol = typeof module === 'object' && module.exports ? require('./toy-svakom-protocol.js') : root.SvakomProtocol;
  const api = factory(protocol);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) Object.assign(root, api);
})(typeof window === 'undefined' ? globalThis : window, function (protocol) {
  'use strict';
  function createSvakomStateReporter({ request = fetch, source, onError = () => {} }) {
    let sequence = 0;
    let everConnected = false;
    let pending = null;
    let sending = null;
    let failed = false;
    async function drain() {
      while (pending) {
        const body = pending;
        pending = null;
        try {
          const response = await request('/api/svakom-ai/state', { method: 'POST',
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
          if (!response.ok) throw new Error('参考强度上报失败，AI 将在状态过期后看到“未知”');
          failed = false;
        } catch (error) {
          if (!failed) onError(error);
          failed = true;
        }
      }
    }
    function report(snapshot, enabled) {
      if (snapshot.connected) everConnected = true;
      if (!enabled) { pending = null; return Promise.resolve(); }
      if (!everConnected) return Promise.resolve();
      pending = { source, sequence: ++sequence,
        connected: Boolean(snapshot.connected && snapshot.protocolConfirmed && snapshot.strengthKnown && !snapshot.busy && !snapshot.lastError),
        flap: snapshot.flap || 0, vibrate: snapshot.vibrate || 0,
        vibrate_level: snapshot.vibrate ? (snapshot.vibrateLevel || 0) : 0,
      };
      if (!sending) sending = drain().finally(() => { sending = null; });
      return sending;
    }
    return { report };
  }
  function createSvakomAiControl({ controller, request = fetch, onState = () => {}, onLog = () => {} }) {
    let permission = { enabled: false, epoch: null };
    let active = false;
    let blocked = false;
    let revision = 0;
    let delivery = 0;
    const seen = new Set();
    const publish = () => onState({ ...permission, active, blocked });
    async function stopAi() {
      if (!active) return;
      if (controller.getState().connected) await controller.stopAll();
      active = false;
      publish();
    }
    async function read(url, options) {
      const response = await request(url, { cache: 'no-store', ...options });
      if (!response.ok) throw new Error('AI 控制开关同步失败');
      return response.json();
    }
    async function applyPermission(next) {
      if (typeof next?.enabled !== 'boolean' || typeof next?.epoch !== 'string') throw new Error('AI 控制状态无效，请更新服务端');
      const changed = permission.epoch !== next.epoch;
      permission = next;
      if (changed || !next.enabled) {
        revision++;
        await stopAi();
      }
      publish();
      return { ...permission };
    }
    async function refresh() {
      const token = revision;
      const next = await read('/api/svakom-ai');
      if (token === revision) await applyPermission(next);
      return { ...permission };
    }
    async function toggle(enabled) {
      blocked = true;
      revision++;
      publish();
      try {
        if (!enabled) await stopAi();
        await read('/api/capabilities/svakom', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled }) });
        await refresh();
      } finally { blocked = false; publish(); }
    }
    async function takeover() {
      revision++;
      blocked = true;
      publish();
      // Local cancellation completes before manual writes, even if the input is invalid.
      // Server sync must not delay local manual controls while the network is unavailable.
      await stopAi();
      read('/api/svakom-ai/takeover', { method: 'POST' }).then(async next => {
        await applyPermission(next);
        blocked = false;
        publish();
      }).catch(() => onLog('手动接管已生效；权限同步失败，暂不接受 AI 指令，请刷新后重试', 'error'));
    }
    async function receive(message) {
      if (message.type === 'toy_profile_changed') { revision++; return refresh(); }
      if (message.type === 'capability_config_changed' && message.data?.key === 'svakom') return refresh();
      if (message.type === 'svakom_revoked') {
        revision++;
        return applyPermission(message.data);
      }
      if (message.type !== 'svakom_command') return;
      const data = message.data || {};
      if (!data.event_id || seen.has(data.event_id)) return;
      seen.add(data.event_id);
      if (seen.size > 512) seen.delete(seen.values().next().value);
      if (blocked || !controller.getState().connected) { onLog('AI 编排未执行：设备未连接或正在手动接管', 'system'); return; }
      const token = revision;
      const order = ++delivery;
      const current = await read('/api/svakom-ai');
      if (order !== delivery || token !== revision || blocked || !current.enabled || current.epoch !== data.epoch) return;
      const parsed = protocol.parse(data.command);
      if (!parsed.ok || !(data.command === 'STOP' || data.command.startsWith('LOOP:'))) {
        onLog('AI 编排无效，未执行', 'error'); return;
      }
      if (!controller.getState().connected || !controller.getState().protocolConfirmed) return;
      permission = current;
      if (data.command === 'STOP') {
        await controller.stopAll();
        active = false;
        onLog('AI 已提交停止', 'system');
      } else {
        active = true;
        try {
          await controller.executeText(data.command);
          if (token === revision) onLog(`AI 循环 · ${parsed.command.events.length} 段 · 每轮 ${parsed.command.cycleSeconds} 秒`, 'success');
        } catch (error) {
          // Keep ownership until stopping succeeds, so disabling can retry a failed stop.
          if (order === delivery && token === revision) await stopAi();
          throw error;
        }
      }
      publish();
    }
    function disconnected() { revision++; active = false; publish(); }
    return { refresh, toggle, takeover, receive, disconnected };
  }
  return { createSvakomAiControl, createSvakomStateReporter };
});

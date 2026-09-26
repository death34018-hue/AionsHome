(function (root, factory) {
  const protocol = typeof module === 'object' && module.exports ? require('./toy-ankni-protocol.js') : root.AnkniProtocol;
  const api = factory(protocol);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) Object.assign(root, api);
})(typeof window === 'undefined' ? globalThis : window, function (protocol) {
  'use strict';
  function createAnkniAiControl({ controller, request = fetch, onState = () => {}, onLog = () => {} }) {
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
      const next = await read('/api/ankni-ai');
      if (token === revision) await applyPermission(next);
      return { ...permission };
    }
    async function toggle(enabled) {
      blocked = true;
      revision++;
      publish();
      try {
        if (!enabled) await stopAi();
        await read('/api/capabilities/ankni', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled }) });
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
      read('/api/ankni-ai/takeover', { method: 'POST' }).then(async next => {
        await applyPermission(next);
        blocked = false;
        publish();
      }).catch(() => onLog('手动接管已生效；权限同步失败，暂不接受 AI 指令，请刷新后重试', 'error'));
    }
    async function receive(message) {
      if (message.type === 'toy_profile_changed') { revision++; return refresh(); }
      if (message.type === 'capability_config_changed' && message.data?.key === 'ankni') return refresh();
      if (message.type === 'ankni_revoked') {
        revision++;
        return applyPermission(message.data);
      }
      if (message.type !== 'ankni_command') return;
      const data = message.data || {};
      if (!data.event_id || seen.has(data.event_id)) return;
      seen.add(data.event_id);
      if (seen.size > 512) seen.delete(seen.values().next().value);
      if (blocked || !controller.getState().connected) { onLog('AI 编排未执行：设备未连接或正在手动接管', 'system'); return; }
      const token = revision;
      const order = ++delivery;
      const current = await read('/api/ankni-ai');
      if (order !== delivery || token !== revision || blocked || !current.enabled || current.epoch !== data.epoch) return;
      let parsed;
      try { parsed = protocol.parse(data.command); } catch(error) {onLog(error.message, "error");return;}
      if (!(data.command === 'STOP' || data.command.startsWith('LOOP:'))) {
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
          if (token === revision) onLog(`AI 循环 · ${parsed.phases.length} 段 · 每轮 ${parsed.duration / 1000} 秒`, 'success');
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
  return { createAnkniAiControl };
});

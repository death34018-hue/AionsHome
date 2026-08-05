(function (root) {
  'use strict';

  const BRIDGE_UNAVAILABLE = {
    ok: false,
    available: false,
    error: 'BRIDGE_UNAVAILABLE',
    message: '请在支持红外的 AionsHome 手机 App 中使用'
  };
  const INVALID_RESPONSE = {
    ok: false,
    available: true,
    error: 'INVALID_RESPONSE',
    message: '红外模块返回了无效结果'
  };
  const COMMAND_LABELS = Object.freeze({
    POWER: '总开关',
    PAT_START_STOP: '拍打开关',
    SPEED_DOWN: '拍拍调慢',
    SPEED_UP: '拍拍调快',
    BLUETOOTH: '蓝牙',
    TIMER: '拍打定时',
    PREVIOUS: '上一首',
    NEXT: '下一首',
    RECORD_PLAY: '录音播放'
  });

  function commandLabel(command) {
    return COMMAND_LABELS[command] || '抱枕';
  }

  function parseBridgeResult(raw) {
    try {
      const parsed = typeof raw === 'string' ? JSON.parse(raw) : raw;
      if (!parsed || typeof parsed.ok !== 'boolean') {
        return { ...INVALID_RESPONSE };
      }
      return parsed;
    } catch (error) {
      return { ...INVALID_RESPONSE };
    }
  }

  function sendCommand(bridge, command) {
    if (!bridge || typeof bridge.transmit !== 'function') {
      return { ...BRIDGE_UNAVAILABLE };
    }
    try {
      return parseBridgeResult(bridge.transmit(command));
    } catch (error) {
      return {
        ok: false,
        available: true,
        error: 'TRANSMIT_FAILED',
        message: '红外发射失败'
      };
    }
  }

  function getStatus(bridge) {
    if (!bridge || typeof bridge.getStatus !== 'function') {
      return { ...BRIDGE_UNAVAILABLE };
    }
    try {
      return parseBridgeResult(bridge.getStatus());
    } catch (error) {
      return {
        ok: false,
        available: false,
        error: 'STATUS_FAILED',
        message: '无法检测手机红外'
      };
    }
  }

  function init() {
    const statusElement = document.getElementById('irStatus');
    const buttons = Array.from(document.querySelectorAll('[data-command]'));
    const bridge = root.AionInfrared || null;
    const status = getStatus(bridge);

    function showStatus(result, fallbackMessage) {
      statusElement.textContent = result.message || fallbackMessage;
      statusElement.classList.toggle('error', !result.ok);
    }

    showStatus(status, '手机红外已就绪');
    buttons.forEach(button => {
      button.disabled = !status.ok;
      button.addEventListener('click', () => {
        if (button.dataset.busy === '1') {
          return;
        }
        button.dataset.busy = '1';
        button.disabled = true;
        try {
          const command = button.dataset.command;
          const result = sendCommand(bridge, command);
          if (result.ok) {
            showStatus(
              { ...result, message: `指令已发送：${commandLabel(command)}` },
              '指令已发送'
            );
            button.classList.add('sent');
            setTimeout(() => button.classList.remove('sent'), 180);
          } else {
            showStatus(result, '红外发射失败');
          }
        } finally {
          button.dataset.busy = '0';
          button.disabled = !status.ok;
        }
      });
    });
  }

  const api = { parseBridgeResult, sendCommand, getStatus, commandLabel };
  root.HugRemote = api;

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', init, { once: true });
    } else {
      init();
    }
  }
})(typeof window !== 'undefined' ? window : globalThis);

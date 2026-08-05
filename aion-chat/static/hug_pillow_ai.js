(function (root, factory) {
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.HugPillowAI = api.createController();
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';

  const SUPPORTED_COMMANDS = new Set([
    'PAT_START_STOP',
    'SPEED_DOWN',
    'SPEED_UP',
  ]);
  const COMMAND_GAP_MS = 2000;

  function createController(options) {
    const opts = options || {};
    const now = opts.now || (() => Date.now());
    const setTimer = opts.setTimer || ((callback, delay) => setTimeout(callback, delay));
    const resolveTransmit = Object.prototype.hasOwnProperty.call(opts, 'transmit')
      ? () => opts.transmit
      : () => {
          const bridge = root && root.AionInfrared;
          return bridge && typeof bridge.transmit === 'function'
            ? command => bridge.transmit(command)
            : null;
        };

    const queue = [];
    let busy = false;
    let lastTransmissionAt = null;

    function drain() {
      if (busy || !queue.length) return;
      const transmit = resolveTransmit();
      if (typeof transmit !== 'function') {
        queue.length = 0;
        return;
      }

      const elapsed = lastTransmissionAt == null
        ? COMMAND_GAP_MS
        : now() - lastTransmissionAt;
      const delay = Math.max(0, COMMAND_GAP_MS - elapsed);
      busy = true;

      const run = () => {
        const command = queue.shift();
        try {
          transmit(command);
        } catch (error) {
          if (root && root.console) root.console.warn('[HugPillowAI] IR transmit failed', error);
        }
        lastTransmissionAt = now();
        busy = false;
        drain();
      };

      if (delay === 0) run();
      else setTimer(run, delay);
    }

    function handleCommandEvent(data) {
      const commands = Array.isArray(data && data.commands) ? data.commands : [];
      for (const command of commands) {
        if (SUPPORTED_COMMANDS.has(command)) queue.push(command);
      }
      drain();
    }

    return { handleCommandEvent };
  }

  return { createController };
});


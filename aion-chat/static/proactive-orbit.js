'use strict';

(function (global) {
  function applyStatus(status, visibleActors, root) {
    const scope = root || global.document;
    const visible = new Set(visibleActors || []);
    for (const actor of ['aion', 'connor']) {
      const orb = scope?.querySelector(`[data-proactive-orb="${actor}"]`);
      if (orb) orb.hidden = !(visible.has(actor) && !!status?.[actor]);
    }
  }

  const api = { applyStatus };
  global.ProactiveOrbit = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);

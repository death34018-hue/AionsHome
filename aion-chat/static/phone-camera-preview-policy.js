(function (root) {
  "use strict";

  function shouldPreview(state) {
    return !!state
      && state.pageVisible === true
      && state.appForeground === true
      && state.source === "phone"
      && state.eventCaptureActive !== true;
  }

  function chooseConfig(capabilities, savedConfig, serverStatus) {
    const available = capabilities || {};
    const saved = savedConfig || {};
    const server = serverStatus || {};
    const preferred = server.armed ? server : saved;
    let facing = preferred.facing;
    if (!available[facing]) {
      facing = available.back ? "back" : (available.front ? "front" : "back");
    }

    const capability = available[facing] || {};
    let presets = Array.isArray(capability.presets)
      ? capability.presets.map(Number).filter(Number.isFinite)
      : [];
    if (!presets.length) {
      const min = Number(capability.minZoom || 1);
      const max = Number(capability.maxZoom || Math.max(1, min));
      presets = [min, 1, 2, max]
        .filter((value, index, values) =>
          value >= min && value <= max && values.indexOf(value) === index);
    }
    if (!presets.length) presets = [1];

    const requested = Number(preferred.zoom);
    const target = Number.isFinite(requested) ? requested : presets[0];
    let zoom = presets[0];
    for (const value of presets) {
      if (Math.abs(value - target) < Math.abs(zoom - target)) zoom = value;
    }
    return { facing, zoom };
  }

  function isStale(state) {
    if (!state || state.running !== true) return false;
    const lastFrameAt = Number(state.lastFrameAt || 0);
    const startedAt = Number(state.startedAt || 0);
    const reference = Math.max(lastFrameAt, startedAt);
    const graceMs = Math.max(0, Number(state.graceMs || 0));
    return reference > 0 && Number(state.now) - reference > graceMs;
  }

  root.PhoneCameraPreviewPolicy = {
    shouldPreview,
    chooseConfig,
    isStale,
  };
})(globalThis);

(function (root) {
  'use strict';

  function normalized(value) {
    return String(value == null ? '' : value).trim().toLocaleLowerCase();
  }

  function uniquePackages(values) {
    const seen = new Set();
    const result = [];
    (values || []).forEach(value => {
      const packageName = String(value == null ? '' : value).trim();
      if (!packageName || seen.has(packageName)) return;
      seen.add(packageName);
      result.push(packageName);
    });
    return result;
  }

  function filterApps(apps, query) {
    const needle = normalized(query);
    return (apps || [])
      .filter(app => !needle
        || normalized(app && app.label).includes(needle)
        || normalized(app && app.packageName).includes(needle))
      .slice()
      .sort((left, right) => String(left.label || '').localeCompare(String(right.label || '')));
  }

  function splitKnownPackages(packageNames, apps) {
    const known = new Set((apps || []).map(app => String(app.packageName || '').trim()));
    const selected = [];
    const aliases = [];
    uniquePackages(packageNames).forEach(packageName => {
      (known.has(packageName) ? selected : aliases).push(packageName);
    });
    return { selected, aliases };
  }

  function mergePackages(selected, aliasesText) {
    const aliases = String(aliasesText == null ? '' : aliasesText).split(/\r?\n/);
    return uniquePackages([...(selected || []), ...aliases]);
  }

  function createGroupId(cryptoObject) {
    if (cryptoObject && typeof cryptoObject.randomUUID === 'function') {
      return cryptoObject.randomUUID();
    }
    const bytes = new Uint8Array(16);
    if (cryptoObject && typeof cryptoObject.getRandomValues === 'function') {
      cryptoObject.getRandomValues(bytes);
    } else {
      for (let index = 0; index < bytes.length; index++) {
        bytes[index] = Math.floor(Math.random() * 256);
      }
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  function createEmergencyHoldController(options) {
    let activePointerId = null;
    let timer = null;

    function clearTimer() {
      if (timer == null) return;
      options.cancelSchedule(timer);
      timer = null;
    }

    function finish() {
      clearTimer();
      activePointerId = null;
      options.setActive(false);
    }

    function fail(error) {
      finish();
      if (typeof options.onError === 'function') options.onError(error);
    }

    function render(gate) {
      options.render(gate);
      return gate;
    }

    function tick() {
      if (activePointerId == null) return;
      try {
        const gate = render(options.sample(true));
        if (!gate || gate.phase !== 'HOLDING') finish();
      } catch (error) {
        fail(error);
      }
    }

    function start(pointerId) {
      if (activePointerId != null) return false;
      activePointerId = pointerId;
      options.setActive(true);
      try {
        const gate = render(options.begin());
        if (gate && gate.phase === 'HOLDING') {
          timer = options.schedule(tick, 200);
        } else {
          finish();
        }
        return true;
      } catch (error) {
        fail(error);
        return false;
      }
    }

    function release(pointerId) {
      if (pointerId !== activePointerId) return false;
      clearTimer();
      try {
        const finalGate = render(options.sample(true));
        if (finalGate && finalGate.phase === 'HOLDING') {
          render(options.sample(false));
        }
        finish();
        return true;
      } catch (error) {
        fail(error);
        return false;
      }
    }

    function cancel(pointerId) {
      if (pointerId !== activePointerId) return false;
      clearTimer();
      try {
        render(options.sample(false));
        finish();
        return true;
      } catch (error) {
        fail(error);
        return false;
      }
    }

    return {
      start,
      release,
      cancel,
      isActive() { return activePointerId != null; },
    };
  }

  root.AppSupervisionUi = {
    filterApps,
    splitKnownPackages,
    mergePackages,
    createGroupId,
    createEmergencyHoldController,
  };
})(typeof globalThis === 'undefined' ? this : globalThis);

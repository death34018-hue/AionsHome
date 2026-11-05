/* The Android push socket already received these messages. No network round trip. */
(function(root) {
  root.NativeMessageSync = {
    create({ read, target, field, apply }) {
      const cursors = new Map(), versions = new Map();
      const key = event => `${event.data?.[field] || ''}:${event.data?.id || ''}`;
      const observe = event => {
        if (!event.data?.[field] || !event.data?.id) return true;
        const seq = Number(event.sync_seq || 0);
        if (seq && seq <= (versions.get(key(event)) || 0)) return false;
        if (seq) versions.set(key(event), Math.max(seq, versions.get(key(event)) || 0));
        if (versions.size > 500) versions.delete(versions.keys().next().value);
        return true;
      };
      return { observe, reset() {
        cursors.delete(target());
        for (const entry of versions.keys()) if (entry.startsWith(`${target()}:`)) versions.delete(entry);
      }, drain(after = 0) {
        const id = target();
        if (!id) return;
        try {
          const events = JSON.parse(read() || '[]');
          let cursor = Math.max(cursors.get(id) || 0, after);
          for (const event of events) {
            if (event.native_seq <= cursor) continue;
            cursor = event.native_seq;
            if (event.data?.[field] !== id) continue;
            const seq = Number(event.sync_seq || 0);
            if (seq && seq <= (versions.get(key(event)) || 0)) continue;
            apply(event);
            observe(event);
          }
          cursors.set(id, cursor);
        } catch (error) { console.warn('[native messages] sync failed', error); }
      } };
    },
    read() {
      const bridge = root.AppSharedData || root.top?.AppSharedData;
      return bridge?.messageEvents?.() || '[]';
    },
  };
})(window);

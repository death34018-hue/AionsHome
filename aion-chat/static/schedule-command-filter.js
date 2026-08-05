'use strict';

(function exposeScheduleCommandFilter(root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.ScheduleCommandFilter = api;
  }
}(typeof window !== 'undefined' ? window : null, function createScheduleCommandFilter() {
  const COMPLETE_COMMAND = /[\[［]\s*(?:(?:ALARM|REMINDER|MONITOR)\s*[:：]\s*[^\]］]*?\s*[|｜]\s*[^\]］]*?|SCHEDULE_DEL\s*[:：]\s*[^\]］]*?|SCHEDULE_LIST)\s*[\]］]/gi;
  const COMMAND_NAMES = [
    'ALARM',
    'REMINDER',
    'MONITOR',
    'SCHEDULE_DEL',
    'SCHEDULE_LIST',
  ];

  function stripScheduleCommands(value) {
    const text = String(value || '').replace(COMPLETE_COMMAND, '');
    const openingIndex = Math.max(text.lastIndexOf('['), text.lastIndexOf('［'));
    if (openingIndex < 0) return text;

    const tail = text.slice(openingIndex);
    const match = tail.match(/^[\[［]\s*([A-Z_]*)/i);
    const partialName = match?.[1]?.toUpperCase() || '';
    if (partialName && COMMAND_NAMES.some(name => name.startsWith(partialName))) {
      return text.slice(0, openingIndex);
    }
    return text;
  }

  return { stripScheduleCommands };
}));

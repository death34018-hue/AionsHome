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
  const COMPLETE_COMMAND = /[\[［]\s*(?:(?:ALARM|REMINDER|MONITOR)\s*[:：]\s*[^\]］]*?\s*[|｜]\s*[^\]］]*?|SCHEDULE_DEL\s*[:：]\s*[^\]］]*?|SCHEDULE_LIST|NEXT_CHAT\s*[:：]\s*[^\]］]*?)\s*[\]］]/gi;
  const COMMAND_NAMES = [
    'ALARM',
    'REMINDER',
    'MONITOR',
    'NEXT_CHAT',
    'SCHEDULE_DEL',
    'SCHEDULE_LIST',
  ];

  function stripScheduleCommands(value) {
    let text = String(value || '')
      .replace(COMPLETE_COMMAND, '')
      .replace(/\s*<autonomy_state>[\s\S]*?<\/autonomy_state>\s*/gi, '');
    const autonomyOpening = text.toLowerCase().lastIndexOf('<autonomy_state');
    if (autonomyOpening >= 0) text = text.slice(0, autonomyOpening);
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

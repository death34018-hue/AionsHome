(function (root, factory) {
  const api = factory(typeof module === 'object' && module.exports ? require('./toy-ankni-protocol.js') : root.AnkniProtocol);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.SystemNoticeUI = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (ankniProtocol) {
  'use strict';

  const SCHEDULE_NOTICE = /^(?:(?:⏰|📅|👀)\uFE0F?\s*)?(【[^】]+】设定了(?:闹铃|日程|监督))\s*[：:]/u;

  // Display-only labels, matching the controller's factory mode numbering.
  const TOY_STRETCH_NAMES = ['主体关闭', '慢速旋转伸缩', '中速旋转伸缩', '三短一长', '混合变速', '单次停顿脉冲', '快速停顿脉冲', '连续短脉冲'];
  const TOY_VIBRATE_NAMES = ['震动关闭', '高频连续波', '轻柔细振波', '渐强波', '慢速起伏波', '间歇脉冲', '快速锯齿波', '中速锯齿波', '阶梯脉冲', '高速方波脉冲', '低速方波脉冲'];

  function translateToyNotice(raw) {
    return String(raw ?? '').replace(/\[SVAKOM:([^\]]*)\]/gi, (original, body) => {
      const command = body.replace(/\s+/g, '').toUpperCase();
      if (command === 'STOP') return '[SVAKOM:全部停止]';
      if (!command.startsWith('LOOP:')) return original;
      const rows = command.slice(5).split(';');
      if (!rows.length || rows.length > 63) return original;
      let total = 0;
      const translated = [];
      for (const row of rows) {
        // Unknown/malformed commands stay visible verbatim, never guessed into valid modes.
        if (!/^[0-9]{1,4}(?:,[0-9]{1,4}){4}$/.test(row)) return original;
        const [seconds, stretch, vibrate, level, flap] = row.split(',').map(Number);
        total += seconds;
        if (seconds < 1 || total > 3600 || stretch > 7 || vibrate > 10 || flap > 7
            || (vibrate === 0 ? level !== 0 : level < 1 || level > 10)) return original;
        translated.push(`${seconds}秒,${TOY_STRETCH_NAMES[stretch]},${TOY_VIBRATE_NAMES[vibrate]},力度${level},豆豆拍打${flap}`);
      }
      return `[SVAKOM:循环:${translated.join('💗')}]`;
    });
  }

  function fallbackEscape(value) {
    return String(value ?? '')
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }

  function translateAnkniNotice(raw) {
    return String(raw ?? '').replace(/\[ANKNI:([^\]]*)\]/gi, (original, body) => {
      try {
        const action=ankniProtocol.parse(body);
        if(action.kind==='stop') return '全部停止（结束编排）';
        if(action.kind!=='timeline') return original;
        return action.phases.map(phase=>`${phase.ms/1000}秒-${ankniProtocol.MODES[phase.mode]}`).join('；')
          + (action.loop ? '（循环）' : '（播放一次）');
      } catch (_) { return original; }
    });
  }

  function renderSystemNoticeContent(content, options) {
    const text = String(content ?? '').trim();
    const escapeHtml = typeof options?.escapeHtml === 'function'
      ? options.escapeHtml
      : fallbackEscape;
    const toyNotice = (options?.attachments || []).find(item => ['svakom_command_notice','ankni_command_notice'].includes(item?.type));
    if (toyNotice) {
      // Restyle existing saved notices too, without rewriting conversation history.
      const ankni=toyNotice.type==='ankni_command_notice';
      const title = ankni
        ? text.replace(/^ANKNI(?:编排)?\s*·\s*/i, '💗趴趴猫 控制 · ').replace(/(\d+)\s*段循环$/, '$1段心动')
        : text.replace(/^新玩具(?:编排)?\s*·\s*/, '💗谜语时刻·').replace(/(\d+)\s+段循环$/, '$1段循环');
      const detail = ankni
        ? (toyNotice.format==='duration_modes_v1' ? translateAnkniNotice(toyNotice.raw) : toyNotice.raw)
        : translateToyNotice(toyNotice.raw);
      return `<details class="system-notice-details">
        <summary>${escapeHtml(title)}</summary>
        <div class="system-notice-full">${escapeHtml(detail)}</div>
      </details>`;
    }
    const match = text.match(SCHEDULE_NOTICE);
    if (!match) {
      return `<span class="system-notice-text">${escapeHtml(text)}</span>`;
    }
    return `<details class="system-notice-details">
      <summary>${escapeHtml(match[1])}</summary>
      <div class="system-notice-full">${escapeHtml(text)}</div>
    </details>`;
  }

  function splitInlineToyCommands(value) {
    const original = String(value ?? '');
    const raw = [];
    const content = original.replace(/\[(?:SVAKOM|ANKNI)\b[^\]]*(?:\]|$)/gi, tag => { raw.push(tag); return ''; });
    if (!raw.length) return {content: original, noticeHtml: ''};
    // Historical leaked tags are display-only: no dispatch and no execution claim.
    const noticeHtml = renderSystemNoticeContent('💗谜语时刻·历史指令', {
      attachments: [{type:'svakom_command_notice',raw:raw.join('\n')}],
    });
    return {content: content.trim(), noticeHtml};
  }

  return {renderSystemNoticeContent, splitInlineToyCommands};
});

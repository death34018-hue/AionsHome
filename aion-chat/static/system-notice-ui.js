(function (root, factory) {
  const api = factory(typeof module === 'object' && module.exports ? require('./toy-ankni-protocol.js') : root.AnkniProtocol);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.SystemNoticeUI = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (ankniProtocol) {
  'use strict';

  const SCHEDULE_NOTICE = /^(?:(?:⏰|📅|👀)\uFE0F?\s*)?(【[^】]+】设定了(?:闹铃|日程|监督))\s*[：:]/u;
  const GAME_LOBBIES = new Map([
    ['桌球', '/billiards'], ['斗地主', '/doudizhu'],
    ['德州扑克', '/texas-holdem'], ['吹牛骰子', '/liars-dice'],
  ]);

  function openMiniGame(event, game) {
    const url = GAME_LOBBIES.get(game);
    if (!url) return false;
    event.preventDefault();
    event.stopPropagation();
    if (typeof window.openSubPage === 'function') window.openSubPage(url);
    else if (window.parent !== window && typeof window.parent.openSubPage === 'function') window.parent.openSubPage(url);
    else window.location.href = url;
    return false;
  }

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
    const composition = globalThis.CompositionCards?.render(options?.attachments);
    if (composition) return composition;
    const billiardsCard=globalThis.BilliardsResultCard?.render(options?.attachments);
    if(billiardsCard)return billiardsCard;
    const text = String(content ?? '').trim();
    const escapeHtml = typeof options?.escapeHtml === 'function'
      ? options.escapeHtml
      : fallbackEscape;
    const gameInvite = (options?.attachments || []).find(item => item?.type === 'mini_game_invite');
    if (gameInvite && GAME_LOBBIES.has(gameInvite.game)) {
      return `<a class="mini-game-invite" href="${GAME_LOBBIES.get(gameInvite.game)}" onclick="return SystemNoticeUI.openMiniGame(event, '${gameInvite.game}')"><span aria-hidden="true">${gameInvite.game === '桌球' ? '🎱' : gameInvite.game === '吹牛骰子' ? '🎲' : '🃏'}</span><span>${escapeHtml(gameInvite.actor_name || 'AI')}邀请你玩${escapeHtml(gameInvite.game)}</span><span class="mini-game-invite-go">进入游戏 →</span></a>`;
    }
    const tableGame = (options?.attachments || []).find(item => item?.type === 'table_game_result');
    if (tableGame) {
      return `<div class="billiards-result-card"><div class="billiards-result-heading"><span class="billiards-result-badge" aria-hidden="true">♧</span><strong>${escapeHtml(tableGame.title)}战报</strong><span>${tableGame.scope === 'table' ? '本场结算' : `第 ${escapeHtml(tableGame.round)} 轮`}</span></div><p class="billiards-result-outcome">${escapeHtml(tableGame.outcome)}</p>${(tableGame.lines || []).map(line => `<p class="billiards-result-detail">${escapeHtml(line)}</p>`).join('')}<p class="billiards-result-score">${escapeHtml(tableGame.note)}</p></div>`;
    }
    const doudizhu = (options?.attachments || []).find(item => item?.type === 'doudizhu_result');
    if (doudizhu) {
      const wallet = (doudizhu.walletLines || []).map(line => escapeHtml(line)).join(' · ');
      return `<div class="billiards-result-card doudizhu-result-card">
        <div class="billiards-result-heading"><span class="billiards-result-badge" aria-hidden="true">♠</span><strong>斗地主战报</strong><span>本局结算</span></div>
        <p class="billiards-result-outcome">${escapeHtml(doudizhu.outcome)}</p>
        <p class="billiards-result-detail">先出完：${escapeHtml(doudizhu.winnerName)} · 地主：${escapeHtml(doudizhu.landlordName)}</p>
        <p class="billiards-result-detail">剩余手牌：${escapeHtml(doudizhu.remaining)}</p>
        ${wallet ? `<p class="billiards-result-score">零钱结算：${wallet}</p>` : ''}
      </div>`;
    }
    const rpsResult = (options?.attachments || []).find(item => item?.type === 'rps_result');
    if (rpsResult) {
      // Keep the full agreement in saved history and model context; shorten its display.
      const prefix = `猜拳结果：本局约定「${rpsResult.topic}」。`;
      const symbols = {'石头': '✊', '剪刀': '✌️', '布': '🖐️'};
      const summary = (text.startsWith(prefix) ? text.slice(prefix.length) : text.replace(/^猜拳结果：/, ''))
        .replace(/出了(石头|剪刀|布)/g, (_, hand) => '出了' + symbols[hand]);
      return `<span class="system-notice-text">${escapeHtml(summary)}</span>`;
    }
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

  return {renderSystemNoticeContent, splitInlineToyCommands, openMiniGame};
});

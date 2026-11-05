(function(root,factory){const api=factory();if(typeof module==='object')module.exports=api;else root.BilliardsResultCard=api;})(globalThis,function(){
  'use strict';
  const esc=value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;');
  const svg=(body,cls='')=>`<svg class="${cls}" viewBox="0 0 32 32" fill="none" aria-hidden="true">${body}</svg>`;
  const crown=()=>svg('<path d="m4 10 7 5 5-10 5 10 7-5-3 14H7Z" fill="currentColor"/><path d="M8 28h16" stroke="currentColor" stroke-width="3" stroke-linecap="round"/><circle cx="4" cy="9" r="2" fill="currentColor"/><circle cx="16" cy="4" r="2" fill="currentColor"/><circle cx="28" cy="9" r="2" fill="currentColor"/>','game-report-crown');
  const check=()=>svg('<circle cx="16" cy="16" r="13" stroke="currentColor" stroke-width="2"/><path d="m9 16 5 5 9-10" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>');
  const info=()=>svg('<circle cx="16" cy="16" r="13" stroke="currentColor" stroke-width="2"/><path d="M16 15v8M16 9v1" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/>');
  const wallet=()=>svg('<path d="M26 11V7a3 3 0 0 0-4-3L6 9v16a3 3 0 0 0 3 3h17V11H6" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><path d="M26 17h-6a3 3 0 0 0 0 6h6" stroke="currentColor" stroke-width="2"/><circle cx="21" cy="20" r="1" fill="currentColor"/>');
  function heading(title,symbol,status){
    return `<header class="game-report-header"><span class="game-report-emblem ${symbol==='8'?'is-ball':''}" aria-hidden="true"><span>${symbol}</span></span><strong class="game-report-title">${esc(title)}</strong><span class="game-report-status">${esc(status)}</span></header>`;
  }
  function shell(content,kind){return `<article class="billiards-result-card game-report game-report-${kind}">${content}</article>`;}
  function billiards(card){
    if(card.players?.length!==2||![0,1].includes(card.winner))return '';
    const player=(seat,won)=>`<div class="game-report-contender ${won?'is-winner':'is-loser'}">${won?crown():'<span class="game-report-crown-spacer" aria-hidden="true"></span>'}<strong class="game-report-name">${esc(card.players[seat].name)}</strong><span class="game-report-result">${won?'获胜':'落败'}</span></div>`;
    return shell(heading('台球战报','8','对局结束')+`<div class="game-report-duel">${player(card.winner,true)}<span class="game-report-versus" aria-label="对阵">VS</span>${player(1-card.winner,false)}</div><div class="game-report-finish"><div class="game-report-shots"><strong>${esc(card.shots)}</strong><span>杆</span></div><div class="game-report-reason"><span class="game-report-reason-icon">${String(card.reason||'').startsWith('合法')?check():info()}</span><span>${esc(card.reason)}</span></div></div>`,'billiards');
  }
  // Older saved messages contain display lines only. Parse known formats and
  // retain unrecognised lines verbatim rather than guessing missing amounts.
  function legacyRows(card){
    const rows=[],extra=[];
    for(const line of card.lines||card.walletLines||[]){
      const match=String(line).match(/^(.*)：本场 ([+−-]?\d+(?:\.\d+)?) (元|分|筹码)(?: · (?:收桌余额|剩余) (.*))?$/)
        ||String(line).match(/^(.*) ([+−-]?\d+(?:\.\d+)?) (元)$/);
      if(match)rows.push({name:match[1],delta:Number(match[2].replace('−','-')),net:`${match[2]} ${match[3]}`,balance:match[4]||'—'});
      else extra.push(line);
    }
    return {rows,extra};
  }
  function scoreboard(card){
    const ddz=card.type==='doudizhu_result',legacy=legacyRows(card);
    const rows=(Array.isArray(card.rows)?card.rows:legacy.rows).map(row=>({...row}));
    const deltas=rows.filter(row=>Number.isFinite(row.delta)).map(row=>row.delta);
    const best=deltas.length?Math.max(...deltas):0;
    for(const row of rows)if(typeof row.won!=='boolean')row.won=Number.isFinite(row.delta)&&row.delta>0&&(ddz||row.delta===best);
    rows.sort((a,b)=>Number(b.won)-Number(a.won));
    const hasBalance=rows.some(row=>row.balance!=null&&row.balance!=='—');
    const table=rows.length?`<div class="game-report-table-wrap"><table class="game-report-table"><colgroup><col class="game-report-player-col"><col>${hasBalance?'<col>':''}</colgroup><thead><tr><th scope="col">玩家</th><th scope="col">${esc(card.netLabel||'本场盈亏')}</th>${hasBalance?`<th scope="col">${esc(card.balanceLabel||(ddz?'剩余手牌':'收桌余额'))}</th>`:''}</tr></thead><tbody>${rows.map(row=>{
      const tone=row.delta>0?'positive':row.delta<0?'negative':'neutral';
      return `<tr data-winner="${row.won}" class="${row.won?'is-winner':''}"><th scope="row"><div class="game-report-player">${row.won?crown():''}<span class="game-report-player-name">${esc(row.name)}</span>${row.won?'<span class="game-report-win-tag">获胜</span>':''}</div></th><td class="game-report-number ${tone}">${esc(row.net??'—')}</td>${hasBalance?`<td class="game-report-number">${esc(row.balance??'—')}</td>`:''}</tr>`;
    }).join('')}</tbody></table></div>`:'';
    const extra=Array.isArray(card.rows)?[]:legacy.extra;
    if(ddz&&!Array.isArray(card.rows)){
      if(card.winnerName||card.landlordName)extra.push(`先出完：${card.winnerName||'—'} · 地主：${card.landlordName||'—'}`);
      if(card.remaining)extra.push(`剩余手牌：${card.remaining}`);
    }
    const title=ddz?'斗地主':card.title||'游戏',symbol=ddz?'♠':title==='吹牛骰子'?'⚄':'♧';
    const note=card.note||(ddz?'按本局阵营结算 · 用户金额仅记本桌':'');
    return shell(heading(`${title}战报`,symbol,ddz?'本局结算':'本场结算')+`<p class="game-report-summary">${esc(card.outcome)}</p>`+table+extra.map(line=>`<p class="game-report-legacy-line">${esc(line)}</p>`).join('')+(note?`<footer class="game-report-footer">${wallet()}<span>${esc(note)}</span></footer>`:''),'table');
  }
  function render(attachments){
    const card=(attachments||[]).find(item=>['billiards_result','table_game_result','doudizhu_result'].includes(item?.type));
    return card?(card.type==='billiards_result'?billiards(card):scoreboard(card)):'';
  }
  return{render};
});

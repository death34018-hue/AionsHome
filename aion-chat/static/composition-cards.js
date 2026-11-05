(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.CompositionCards = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  function escape(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  }

  function stripCommands(value) {
    return String(value || '').replace(/\[COMPOSE\][\s\S]*?(?:\[\/COMPOSE\]|$)/gi, '').trim();
  }

  function render(attachments) {
    const piece = (attachments || []).find(item => item?.type === 'composition');
    if (!piece) return '';
    const id = escape(piece.id);
    return `<section class="composition-card" data-composition-id="${id}" aria-label="${escape(piece.title)} 小曲播放器">
      <header class="composition-heading"><div><strong>${escape(piece.title || '小曲')}</strong><span class="composition-byline">${escape(piece.actor_name || 'AI')}</span></div><button data-compose-action="favorite" aria-label="${piece.favorite ? '取消收藏小曲' : '收藏小曲'}" aria-pressed="${Boolean(piece.favorite)}">${piece.favorite ? '♥' : '♡'}</button></header>
      <div class="composition-inline-controls"><button class="composition-play" data-compose-action="play" aria-label="播放小曲">▶</button><button data-compose-action="restart" aria-label="从头播放">↤</button><button data-compose-action="loop" aria-label="循环播放" aria-pressed="false">↻</button><span class="composition-time">00:00</span><label class="composition-volume">音量<input data-compose-action="volume" type="range" min="0" max="100" value="55" aria-label="小曲音量"></label></div>
      <div class="composition-progress"><i></i></div><p class="composition-status" role="status"></p>
      <details class="composition-visual"><summary>动态谱面</summary><canvas aria-label="随实际演奏滚动的音符"></canvas><div class="composition-legend"><span>音符条越长，持续越久</span><span class="composition-bar">第 1 小节</span></div></details>
      <footer><details class="composition-source"><summary>文字谱</summary><pre>展开后读取乐谱</pre></details><a href="/music-box?piece=${encodeURIComponent(piece.id)}" data-compose-action="library">音乐盒 ↗</a></footer>
    </section>`;
  }

  if (typeof document === 'undefined') return {render, stripCommands};

  const pieces = new Map();
  const requests = new Map();
  const sampleRequests = new Map();
  let frame;
  let frameReady;
  let readyResolve;
  let samples;
  let activeId = '';
  let playing = false;
  let loading = false;
  let cycle = 0;
  let endPhase = '';
  let endAt = null;
  let cps = 1 / 3;
  let notes = [];
  let looping = false;
  let volume = Number(localStorage.getItem('compositionVolume') || 55) / 100;
  let playRequest = 0;
  let errorText = '';
  const channel = typeof BroadcastChannel === 'function' ? new BroadcastChannel('aionshome-composition-player') : null;

  function cards(id) {
    return [...document.querySelectorAll('.composition-card')].filter(card => !id || card.dataset.compositionId === id);
  }

  function post(type, data = {}) {
    frame?.contentWindow?.postMessage({channel: 'aionshome-composition', type, ...data}, '*');
  }

  async function getPiece(id) {
    if (requests.has(id)) return requests.get(id);
    const request = CompositionCache.getPiece(id).then(piece => {
      pieces.set(id, piece);
      return piece;
    }).finally(() => requests.delete(id));
    requests.set(id, request);
    return request;
  }

  async function ensureFrame() {
    if (!frame) {
      frameReady = new Promise((resolve, reject) => {
        const timeout = setTimeout(() => reject(new Error('播放器加载超时，请重试')), 15000);
        readyResolve = () => { clearTimeout(timeout); resolve(); };
      }).catch(error => {
        frame?.remove();
        frame = null;
        throw error;
      });
      frame = document.createElement('iframe');
      frame.src = '/static/composition-player.html';
      frame.title = '小曲发声引擎';
      frame.setAttribute('sandbox', 'allow-scripts');
      frame.setAttribute('allow', 'autoplay');
      frame.className = 'composition-engine';
      frame.setAttribute('aria-hidden', 'true');
      document.body.appendChild(frame);
    }
    await frameReady;
    if (!samples) {
      const response = await fetch('/static/vendor/strudel-1.3.0/samples.json', {cache: 'force-cache'});
      if (!response.ok) throw new Error('音色目录加载失败，请重试');
      samples = await response.json();
      samples._base = new URL(samples._base, location.origin).href;
    }
  }

  async function play(id, restart = false) {
    if (id === activeId && !restart && !loading && !errorText && endPhase !== 'ended' && (playing || cycle > 0)) {
      post(playing ? 'pause' : 'resume');
      return;
    }
    if (errorText && frame) {
      frame.remove();
      frame = null;
      frameReady = null;
    }
    const request = ++playRequest;
    if (activeId) post('stop');
    activeId = id;
    loading = true;
    playing = false;
    errorText = '';
    endPhase = '';
    endAt = null;
    cycle = 0;
    notes = [];
    channel?.postMessage('stop');
    refresh();
    try {
      const piece = await getPiece(id);
      await ensureFrame();
      if (request !== playRequest) return;
      cps = piece.bpm / 240;
      post('play', {piece, samples, volume, loop: looping});
      for (const card of cards(id)) card.querySelector('.composition-visual').open = true;
    } catch (error) {
      if (request !== playRequest) return;
      loading = false;
      errorText = error.message;
      refresh();
    }
  }

  function stop() {
    playRequest++;
    post('stop');
    endPhase = '';
    endAt = null;
    notes = [];
    loading = false;
    playing = false;
    cycle = 0;
    refresh();
  }

  function clock(seconds) {
    return `${String(Math.floor(seconds / 60)).padStart(2,'0')}:${String(Math.floor(seconds % 60)).padStart(2,'0')}`;
  }

  function draw(card) {
    const canvas = card.querySelector('canvas');
    if (!canvas || !card.querySelector('.composition-visual').open || !canvas.clientWidth) return;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    const scale = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(width * scale) || canvas.height !== Math.round(height * scale)) {
      canvas.width = Math.round(width * scale);
      canvas.height = height * scale;
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.strokeStyle = '#ffffff09';
    for (let y = 16; y < height; y += 20) { ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(width,y); ctx.stroke(); }
    const currentNotes = card.dataset.compositionId === activeId ? notes : [];
    const position = card.dataset.compositionId === activeId ? cycle : 0;
    const span = Math.min(3, (card.dataset.compositionId === activeId && endAt) || 3);
    // Near a finite ending, move the cursor across the last bars instead of
    // scrolling the notes away to make room for a nonexistent next bar.
    let viewStart = position - span * .3;
    if (card.dataset.compositionId === activeId && endAt !== null) {
      viewStart = Math.min(viewStart, endAt - span * .96);
    }
    const cursorX = (position - viewStart) / span * width;
    const pitches = currentNotes.map(note => note.midi);
    const low = Math.min(48, ...pitches) - 3;
    const high = Math.max(76, ...pitches) + 3;
    for (const note of currentNotes) {
      const x = (note.start - viewStart) / span * width;
      const length = Math.max(5, (note.end - note.start) / span * width - 2);
      const y = 14 + (high - note.midi) / (high - low) * (height - 30);
      const active = playing && position >= note.start && position < note.end;
      ctx.fillStyle = active ? '#ffe5a4' : note.midi < 65 ? '#79b7ac' : '#dfb77f';
      ctx.globalAlpha = active ? 1 : .72;
      ctx.beginPath(); ctx.roundRect(x,y,length,6,3); ctx.fill();
    }
    ctx.globalAlpha = 1;
    ctx.strokeStyle = '#e2e7df77';
    ctx.beginPath(); ctx.moveTo(cursorX,6); ctx.lineTo(cursorX,height - 6); ctx.stroke();
  }

  function refresh() {
    for (const card of cards()) {
      const id = card.dataset.compositionId;
      const active = id === activeId;
      const piece = pieces.get(id);
      const button = card.querySelector('[data-compose-action="play"]');
      button.textContent = active && loading ? '…' : active && playing ? 'Ⅱ' : '▶';
      button.setAttribute('aria-label', active && playing ? '暂停小曲' : '播放小曲');
      button.disabled = active && loading;
      card.querySelector('.composition-status').textContent = active && errorText ? errorText : active && loading ? '正在准备音色…' : active && playing && endPhase === 'tail' ? '余音…' : '';
      card.querySelector('.composition-time').textContent = clock(active ? cycle / cps : 0);
      const barPosition = active && piece ? (endPhase ? piece.cycles : cycle % piece.cycles) : 0;
      card.querySelector('.composition-progress i').style.width = active && piece ? `${barPosition / piece.cycles * 100}%` : '0%';
      card.querySelector('.composition-bar').textContent = `第 ${Math.min(Math.floor(barPosition) + 1, piece?.cycles || 1)} 小节`;
      const repeat = card.querySelector('[data-compose-action="loop"]');
      repeat.setAttribute('aria-pressed', String(looping));
      card.querySelector('[data-compose-action="volume"]').value = String(Math.round(volume * 100));
      if (piece) {
        const favorite = card.querySelector('[data-compose-action="favorite"]');
        favorite.textContent = piece.favorite ? '♥' : '♡';
        favorite.setAttribute('aria-pressed', String(piece.favorite));
        favorite.setAttribute('aria-label', piece.favorite ? '取消收藏小曲' : '收藏小曲');
      }
      draw(card);
    }
  }

  async function sendSample(message) {
    const target = frame?.contentWindow;
    try {
      const base = new URL('/static/vendor/strudel-1.3.0/samples/', location.origin);
      const url = new URL(message.url, location.origin);
      if (url.origin !== base.origin || !url.pathname.startsWith(base.pathname)) throw new Error('音色地址无效');
      if (!sampleRequests.has(url.href)) {
        const request = fetch(url, {credentials: 'same-origin', cache: 'force-cache', signal: AbortSignal.timeout(25000)}).then(async response => {
          if (!response.ok) throw new Error('音色文件加载失败，请重试');
          return response.arrayBuffer();
        }).catch(error => { sampleRequests.delete(url.href); throw error; });
        sampleRequests.set(url.href, request);
      }
      const buffer = (await sampleRequests.get(url.href)).slice(0);
      target?.postMessage({channel: 'aionshome-composition', type: 'sample-response', requestId: message.requestId, buffer}, '*', [buffer]);
    } catch (error) {
      target?.postMessage({channel: 'aionshome-composition', type: 'sample-response', requestId: message.requestId, error: error.message}, '*');
    }
  }

  window.addEventListener('message', event => {
    if (event.source !== frame?.contentWindow || event.data?.channel !== 'aionshome-composition') return;
    const data = event.data;
    if (data.type === 'ready') { readyResolve(); return; }
    if (data.type === 'sample-request') { sendSample(data); return; }
    if (data.id !== activeId) return;
    if (data.type === 'loading') { loading = true; playing = false; }
    if (data.type === 'error') { loading = false; playing = false; errorText = data.message; }
    if (data.type === 'state' || data.type === 'tick') {
      loading = false;
      playing = data.playing;
      cycle = data.cycle || 0;
      endPhase = data.phase || '';
      if ('endAt' in data) endAt = data.endAt;
      cps = data.cps || cps;
      notes = data.notes || (cycle ? notes : []);
    }
    refresh();
  });

  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-compose-action]');
    const card = button?.closest('.composition-card');
    if (!card) return;
    const id = card.dataset.compositionId;
    const action = button.dataset.composeAction;
    if (action === 'library') return;
    event.stopPropagation();
    if (action === 'play') { await play(id); return; }
    if (action === 'restart') { await play(id, true); return; }
    if (action === 'loop') {
      looping = !looping;
      if (looping && endPhase === 'tail' && playing) { await play(activeId, true); return; }
      post('loop', {loop: looping}); refresh(); return;
    }
    if (action !== 'favorite') return;
    try {
      const piece = pieces.get(id) || await getPiece(id);
      const response = await fetch('/api/compositions/' + encodeURIComponent(id), {
        method: 'PATCH', headers: {'Content-Type':'application/json'}, body: JSON.stringify({favorite: !piece.favorite}),
      });
      if (!response.ok) throw new Error('收藏暂时没有保存，请重试');
      const saved = await response.json();
      pieces.set(id, saved);
      CompositionCache.rememberPiece(saved);
      refresh();
      document.dispatchEvent(new CustomEvent('composition-favorite-changed', {detail: {piece: saved}}));
    } catch (error) { card.querySelector('.composition-status').textContent = error.message; }
  });

  document.addEventListener('input', event => {
    if (event.target.dataset.composeAction !== 'volume') return;
    volume = Number(event.target.value) / 100;
    localStorage.setItem('compositionVolume', String(event.target.value));
    post('volume', {volume});
    refresh();
  });

  document.addEventListener('toggle', async event => {
    const details = event.target;
    if (!details.open || !details.closest('.composition-card')) return;
    if (details.classList.contains('composition-visual')) { draw(details.closest('.composition-card')); return; }
    if (!details.classList.contains('composition-source')) return;
    try {
      const piece = await getPiece(details.closest('.composition-card').dataset.compositionId);
      details.querySelector('pre').textContent = piece.code;
    } catch (error) { details.querySelector('pre').textContent = error.message; }
  }, true);

  new MutationObserver(records => {
    if (records.some(record => [...record.addedNodes].some(node => node.nodeType === 1 &&
        (node.matches('.composition-card') || node.querySelector('.composition-card'))))) refresh();
  }).observe(document.documentElement, {childList: true, subtree: true});
  window.addEventListener('resize', refresh);
  if (channel) channel.onmessage = () => stop();
  window.addEventListener('pagehide', () => post('stop'));

  return {render, stripCommands, play, stop, refresh, getPiece};
});

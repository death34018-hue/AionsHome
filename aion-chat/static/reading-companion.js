/* Shared co-reading UI and data, used by the library and finished theater chapters. */
const ReadingCompanion = (() => {
  let options = {}, mounted = false, epoch = 0, annotationAbort = null, replyAbort = null;
  let currentBook = null, currentChIdx = 0, chapterData = null;
  let aiName = 'AI', userName = '你', connorName = '另一位伴侣', askTarget = 'aion';
  const $ = id => id === 'readerContent' ? options.content?.() : document.getElementById(id);
  async function api(method, url, body) {
    const response = await fetch(url, {method, headers:{'Content-Type':'application/json'},
      ...(body ? {body:JSON.stringify(body)} : {})});
    const data = await response.json();
    if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : '操作失败，请重试');
    return data;
  }
  function refreshPublishedBook() { return Promise.resolve(options.onRefresh?.()); }
  function mount(settings) {
    options = settings;
    if (!mounted) {
      const root = document.createElement('div');
      root.className = 'reading-companion-panels';
      root.innerHTML = `<div class="hl-popup" id="hlPopup" onclick="if(event.target===this)ReadingCompanion.closeHlPopup()">
  <div class="hl-popup-card">
    <div class="hl-quote" id="hlPopupQuote"></div>
    <div class="hl-question" id="hlPopupQuestion"></div>
    <div class="hl-answer" id="hlPopupAnswer"></div>
    <div class="hl-actions">
      <button class="hl-delete-btn" id="hlDeleteBtn" onclick="ReadingCompanion.deleteCurrentHighlight()">🗑 删除标注</button>
      <button class="hl-close-btn" onclick="ReadingCompanion.closeHlPopup()">关闭</button>
    </div>
  </div>
</div>

<!-- 批注弹窗 -->
<div class="anno-popup" id="annoPopup" onclick="ReadingCompanion.closeAnnoPopup(event)">
  <div class="anno-popup-card" id="annoPopupCard">
    <div class="anno-type" id="annoPopupType"></div>
    <div class="anno-text" id="annoPopupText"></div>
    <div class="anno-close"><button onclick="ReadingCompanion.closeAnnoPopup()">关闭</button></div>
  </div>
</div>

<!-- 选中文字浮动工具栏 -->
<div class="sel-toolbar" id="selToolbar">
  <button id="selAskAionBtn" onclick="ReadingCompanion.askAIAboutSelection('aion')">💬 问AI</button>
  <button id="selAskConnorBtn" class="connor-btn" onclick="ReadingCompanion.askAIAboutSelection('connor')">💬 问第二位AI</button>
  <button id="selAskGroupBtn" class="group-btn" onclick="ReadingCompanion.askAIAboutSelection('group')">💬 群里聊</button>
</div>

<!-- 提问弹窗 -->
<div class="ask-popup" id="askPopup" onclick="if(event.target===this)ReadingCompanion.closeAskPopup()">
  <div class="ask-popup-card">
    <div class="ask-quote" id="askQuote"></div>
    <div class="ask-input-row">
      <input type="text" id="askInput" placeholder="想问点什么？（可留空）" onkeydown="if(event.key==='Enter')ReadingCompanion.submitAskToChat()">
      <button class="ask-send" onclick="ReadingCompanion.submitAskToChat()">↑</button>
    </div>
    <div class="ask-hint">消息会发送到对应聊天窗口</div>
  </div>
</div>

<!-- 阅读内嵌聊天面板 -->
<div class="reader-reply" id="readerReply">
  <div class="reader-reply-header">
    <span class="reply-title" id="replyTitle">💬 聊天</span>
    <button class="reply-close" onclick="ReadingCompanion.closeReplyPanel()">✕</button>
  </div>
  <div class="reader-reply-msgs" id="replyMsgs"></div>
  <div class="reader-reply-input">
    <input type="text" id="replyInput" placeholder="继续聊..." onkeydown="if(event.key==='Enter')ReadingCompanion.sendReplyMsg()">
    <button class="rc-send" id="replySendBtn" onclick="ReadingCompanion.sendReplyMsg()">↑</button>
  </div>
</div>

`;
      document.body.append(root);
      document.addEventListener('selectionchange', onSelectionChange);
      mounted = true;
    }
    settings.inviteHost.classList.add('reading-invite');
    settings.inviteHost.innerHTML = '<div class="invite-control"><button type="button" class="invite-btn" id="inviteBtn">邀请共读</button><button type="button" class="segment-status" id="segmentStatus" hidden aria-label="批注进度"></button></div>';
    $('inviteBtn').onclick = () => Promise.resolve((options.onInvite || inviteRead)()).catch(e => showToast(e.message));
    $('segmentStatus').onclick = () => showToast($('segmentStatus').title);
  }
  function reset() {
    epoch++;
    annotationAbort?.abort(); replyAbort?.abort();
    annotationAbort = replyAbort = null;
    currentBook = chapterData = null;
    isAnnotating = replySending = replyPreparing = false;
    selectedText = ''; pendingHighlight = null;
    if (!mounted) return;
    closeHlPopup(); closeAskPopup(); closeReplyPanel(); closeAnnoPopup();
    $('selToolbar').style.display = 'none';
    $('inviteBtn').disabled = false;
    $('inviteBtn').textContent = '邀请共读';
    $('replySendBtn').disabled = false;
    $('segmentStatus').hidden = true;
  }
  function setChapter(book, index, data) {
    const changed = currentBook?.book_id !== book.book_id || currentChIdx !== index || chapterData?.chapter.id !== data.chapter.id;
    if (changed) reset();
    if (!changed && (isAnnotating || replySending || replyPreparing)) return;
    const same = !changed && JSON.stringify(chapterData) === JSON.stringify(data);
    currentBook = book; currentChIdx = index; chapterData = data;
    aiName = data.ai_name || 'AI'; userName = data.user_name || '你'; connorName = data.connor_name || '另一位伴侣';
    renderSegmentStatus(data.chapter.segments_meta);
    if (!same) renderChapter(data);
  }
function renderSegmentStatus(segments) {
  const bar = $('segmentStatus');
  bar.hidden = !segments?.length;
  if (!segments || segments.length === 0) {
    return;
  }
  const doneCount = segments.filter(s => s.status === 'done').length;
  const working = segments.some(s => s.status === 'annotating');
  const partial = doneCount > 0 || segments.some(s => s.status === 'partial');
  bar.dataset.state = working ? 'annotating' : doneCount === segments.length ? 'done' : partial ? 'partial' : 'empty';
  bar.textContent = working ? '…' : doneCount === segments.length ? '✓' : partial ? '◐' : '·';
  bar.title = `${doneCount}/${segments.length} 段已批注${working ? '，正在共读…' : ''}`;
  bar.setAttribute('aria-label', bar.title);
}

function renderChapter(data) {
  const content = $('readerContent');
  const ch = data.chapter;
  const annotations = data.annotations || {};
  const connorAnnotations = data.connor_annotations || {};
  const summaries = data.summaries || [];
  const connorSummaries = data.connor_summaries || [];

  // 直接使用后端生成的 HTML（已含 data-p 标记和图片）
  let html = ch.html_content || '';

  // 为有批注的段落添加气泡（Aion + Connor）
  const allAnnotatedPs = new Set([...Object.keys(annotations), ...Object.keys(connorAnnotations)]);
  for (const pIdx of allAnnotatedPs) {
    const marker = `data-p="${pIdx}"`;
    let bubbles = '';
    if (annotations[pIdx]) {
      bubbles += `<span class="anno-bubble" onclick="ReadingCompanion.showAnnotation(${pIdx},'aion')" title="${escHtml(aiName)}的批注"><img src="/public/AIIcon.png" alt=""></span>`;
    }
    if (connorAnnotations[pIdx]) {
      bubbles += `<span class="anno-bubble connor" onclick="ReadingCompanion.showAnnotation(${pIdx},'connor')" title="${escHtml(connorName)}的批注"><img src="/public/codexicon.png" alt=""></span>`;
    }
    if (bubbles) {
      html = html.replace(
        new RegExp(`(<p\\s+${marker}>)(.*?)(</p>)`, 's'),
        `$1$2${bubbles}$3`
      );
    }
  }

  // 在每段（segment）末尾插入读后感按钮
  const segmentsMeta = ch.segments_meta || [];
  for (const s of summaries) {
    const segMeta = segmentsMeta[s.segment_index];
    if (!segMeta) continue;
    const endP = segMeta.end_p;
    const endMarker = `data-p="${endP}"`;
    const summaryBtn = `<div style="text-align:center; margin:16px 0;"><button class="summary-btn" onclick="ReadingCompanion.showSummary(${s.segment_index},'aion')">📋 ${escHtml(aiName)}读后感</button></div>`;
    html = html.replace(
      new RegExp(`(<p\\s+${endMarker}>.*?</p>)`, 's'),
      `$1${summaryBtn}`
    );
  }
  for (const s of connorSummaries) {
    const segMeta = segmentsMeta[s.segment_index];
    if (!segMeta) continue;
    const endP = segMeta.end_p;
    const endMarker = `data-p="${endP}"`;
    const summaryBtn = `<div style="text-align:center; margin:16px 0;"><button class="summary-btn" style="border-color:#5b8af7;color:#5b8af7;" onclick="ReadingCompanion.showSummary(${s.segment_index},'connor')">📋 ${escHtml(connorName)}读后感</button></div>`;
    // 插在已有内容后面（data-p 可能已被替换添加了 Aion 的按钮，追加到 p 后）
    const existingPattern = new RegExp(`(<p\\s+${endMarker}>.*?</p>(?:\\s*<div[^>]*>.*?</div>)*)`, 's');
    html = html.replace(existingPattern, `$1${summaryBtn}`);
  }

  content.innerHTML = html;
  applyHighlights(data.highlights || []);
  options.onRender?.(content, data);
}

// ── 批注弹窗 ──
function showAnnotation(pIdx, who) {
  if (!chapterData) return;
  const annoMap = who === 'connor' ? (chapterData.connor_annotations || {}) : (chapterData.annotations || {});
  const anno = annoMap[pIdx];
  if (!anno) return;
  const name = who === 'connor' ? connorName : aiName;
  const typeLabel = `【${name}】${anno.type || '批注'}`;
  $('annoPopupType').textContent = typeLabel;

  // 解析 [MUSIC:xxx]
  const musicRe = /\[MUSIC:(.+?)\]/g;
  let text = anno.text || '';
  let musicMatches = [];
  let m;
  while ((m = musicRe.exec(text)) !== null) {
    musicMatches.push(m[1]);
  }
  // 替换文本中的 [MUSIC:xxx]
  let cleanText = text.replace(musicRe, '').trim();
  $('annoPopupText').innerHTML = escHtml(cleanText);

  // 添加音乐卡片
  for (const keyword of musicMatches) {
    const card = document.createElement('div');
    card.className = 'anno-music-card';
    card.innerHTML = `<span class="music-icon">🎵</span><span class="music-text">${escHtml(name)}点播了歌曲：${escHtml(keyword)}</span><span class="music-play">▶ 播放</span>`;
    card.onclick = () => playMusicByKeyword(keyword);
    $('annoPopupText').appendChild(card);
  }

  $('annoPopup').classList.add('show');
}

async function playMusicByKeyword(keyword) {
  try {
    const res = await api('GET', `/api/music/search?q=${encodeURIComponent(keyword)}&limit=1`);
    if (res.songs && res.songs.length > 0) {
      playMusicOnline(res.songs[0].id);
    } else {
      showToast('没找到这首歌，试试其他关键词吧');
    }
  } catch(e) {
    showToast('音乐搜索失败');
  }
}

function playMusicOnline(songId) {
  let wrap = document.getElementById('globalMusicWrap');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.id = 'globalMusicWrap';
    wrap.style.cssText = 'position:fixed;top:max(34px, env(safe-area-inset-top, 0px));left:0;right:0;z-index:999;display:none;align-items:center;gap:8px;background:var(--surface);padding:0 12px;height:36px;box-shadow:0 2px 8px rgba(0,0,0,0.15);border-bottom:1px solid var(--border);';

    const playBtn = document.createElement('button');
    playBtn.id = 'globalMusicPlayBtn';
    playBtn.textContent = '⏸';
    playBtn.style.cssText = 'background:none;border:none;font-size:16px;cursor:pointer;color:var(--text);padding:0 4px;line-height:1;flex-shrink:0;';

    const bar = document.createElement('input');
    bar.id = 'globalMusicBar';
    bar.type = 'range'; bar.min = 0; bar.max = 1000; bar.value = 0;
    bar.style.cssText = 'flex:1;height:4px;accent-color:var(--accent);cursor:pointer;';

    const audio = document.createElement('audio');
    audio.id = 'globalMusicAudio';

    playBtn.onclick = () => { if (audio.paused) { audio.play(); playBtn.textContent = '⏸'; } else { audio.pause(); playBtn.textContent = '▶'; } };
    audio.ontimeupdate = () => { if (audio.duration) bar.value = (audio.currentTime / audio.duration) * 1000; };
    bar.oninput = () => { if (audio.duration) audio.currentTime = (bar.value / 1000) * audio.duration; };
    audio.onended = () => { wrap.style.display = 'none'; playBtn.textContent = '▶'; };
    audio.onplay = () => { playBtn.textContent = '⏸'; };
    audio.onpause = () => { if (!audio.ended) playBtn.textContent = '▶'; };

    const closeBtn = document.createElement('button');
    closeBtn.textContent = '✕';
    closeBtn.style.cssText = 'background:none;border:none;font-size:14px;cursor:pointer;color:var(--text3);padding:0 4px;line-height:1;flex-shrink:0;';
    closeBtn.onclick = () => { audio.pause(); audio.src = ''; wrap.style.display = 'none'; bar.value = 0; };

    wrap.appendChild(playBtn);
    wrap.appendChild(bar);
    wrap.appendChild(audio);
    wrap.appendChild(closeBtn);
    document.body.appendChild(wrap);
  }
  const audio = document.getElementById('globalMusicAudio');
  audio.src = '/api/music/stream/' + songId;
  wrap.style.display = 'flex';
  document.getElementById('globalMusicBar').value = 0;
  document.getElementById('globalMusicPlayBtn').textContent = '⏸';
  audio.play().catch(() => {});
}

function showSummary(segIdx, who) {
  if (!chapterData) return;
  const summaryList = who === 'connor' ? (chapterData.connor_summaries || []) : (chapterData.summaries || []);
  const entry = summaryList.find(s => s.segment_index === segIdx);
  if (!entry) return;
  const name = who === 'connor' ? connorName : aiName;
  $('annoPopupType').textContent = `📋 ${name}读后感`;
  $('annoPopupText').textContent = entry.summary;
  $('annoPopup').classList.add('show');
}

function closeAnnoPopup(e) {
  if (e && e.target.id !== 'annoPopup') return;
  $('annoPopup').classList.remove('show');
}

// ── 邀请共读 — AI 批注 ──────────────────────
let isAnnotating = false;

function inviteRead() {
  if (!currentBook || !chapterData) return;
  if (isAnnotating) { showToast('正在批注中，请稍候...'); return; }

  const segments = chapterData.chapter.segments_meta || [];
  if (segments.length === 0) { showToast('当前章节无可批注内容'); return; }

  const reread = segments.every(segment => segment.status === 'done');
  if (reread && !confirm('当前已经共读过一次了，是否要重新共读？\n已有语音会保留，本次朗读继续使用原来的批注。')) return;

  // 开始批注所有段
  return startAnnotateAll(segments, reread);
}

async function startAnnotateAll(segments, reread = false) {
  const token=epoch;
  annotationAbort = new AbortController();
  const sourceBook=currentBook.book_id, sourceIndex=currentChIdx, sourceId=chapterData.chapter.id;
  isAnnotating = true;
  const btn = $('inviteBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> AI 正在读...';

  const total = segments.length;
  let doneCount = 0;
  let finished = false;

  try {
    // 获取主聊天当前使用的模型，批注跟主聊天同步
    let modelKey = null;
    try {
      const convs = await api('GET', '/api/conversations');
      if (convs && convs.length > 0) modelKey = convs[0].model || null;
    } catch (e) { console.warn('获取聊天模型失败，使用默认模型', e); }

    if (token !== epoch) return;
    const resp = await fetch(`/api/books/${sourceBook}/chapters/${sourceIndex}/annotate-all`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: annotationAbort.signal,
      body: JSON.stringify({ model_key: modelKey, chapter_id: sourceId, reread })
    });
    if(!resp.ok)throw Error((await resp.json()).detail || '批注请求失败');

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if(token!==epoch || currentBook?.book_id!==sourceBook || currentChIdx!==sourceIndex || chapterData?.chapter.id!==sourceId){await reader.cancel();return;}

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop(); // 保留不完整的行

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const raw = line.slice(6).trim();
        if (!raw) continue;

        let msg;
        try { msg = JSON.parse(raw); } catch { continue; }

        if(msg.type==='error')throw Error(msg.message);
        if (msg.type === 'segment_start') {
          btn.innerHTML = `<span class="spinner"></span> ${msg.segment_index + 1}/${total} 段...`;
          // 更新状态条中对应段为 annotating
          updateSegBlockStatus(msg.segment_index, 'annotating');
        }
        else if (msg.type === 'companion_done') {
          if (msg.who === 'connor') mergeConnorAnnotations(msg.annotations, msg.summary, msg.segment_index);
          else mergeAnnotations(msg.annotations, msg.summary, msg.segment_index);
          renderChapter(chapterData);
        }
        else if (msg.type === 'segment_done') {
          if (!msg.status || msg.status === 'done') doneCount++;
          // 合并 Aion 批注
          mergeAnnotations(msg.aion_annotations, msg.aion_summary, msg.segment_index);
          // 合并 Connor 批注
          if (msg.connor_annotations?.length || msg.connor_summary) {
            mergeConnorAnnotations(msg.connor_annotations, msg.connor_summary, msg.segment_index);
          }
          updateSegBlockStatus(msg.segment_index, msg.status || 'done');
          // 实时刷新阅读器内容
          renderChapter(chapterData);
        }
        else if (msg.type === 'segment_error') {
          showToast(`第 ${msg.segment_index + 1} 段${msg.who ? '(' + msg.who + ')' : ''}批注失败: ${msg.message}`);
          updateSegBlockStatus(msg.segment_index, 'partial');
        }
        else if (msg.type === 'start' && msg.connor_available === false) {
          showToast(`${connorName} 未接入，仅 ${aiName} 批注`);
        }
        else if (msg.type === 'done') {
          finished = true;
          break;
        }
      }
    }

    if (!finished) {
      showToast('进度连接已断开，共读会在后台继续；再次邀请可查看进度或补读。');
    } else if (doneCount === total) {
      showToast(`批注完成！共 ${doneCount} 段`);
    } else {
      showToast(`已完成 ${doneCount}/${total} 段，再次邀请会补读未完成的部分。`);
    }

  } catch (e) {
    if (token === epoch && e.name !== 'AbortError') showToast('共读进度中断: ' + e.message + '；再次邀请会接续未完成的部分。');
  } finally {
    if (token !== epoch) return;
    annotationAbort = null;
    isAnnotating = false;
    btn.disabled = false;
    btn.textContent = '邀请共读';
    // 刷新状态条
    if(chapterData)renderSegmentStatus(chapterData.chapter.segments_meta);
    refreshPublishedBook().catch(()=>{});
  }
}

function mergeAnnotations(newAnnos, summary, segIdx) {
  if (!chapterData) return;
  // 合并到 annotations map
  for (const a of (newAnnos || [])) {
    chapterData.annotations[a.p] = a;
  }
  // 合并摘要
  if (summary) {
    const existing = chapterData.summaries.findIndex(s => s.segment_index === segIdx);
    if (existing >= 0) {
      chapterData.summaries[existing].summary = summary;
    } else {
      chapterData.summaries.push({ segment_index: segIdx, summary });
    }
  }
}

function mergeConnorAnnotations(newAnnos, summary, segIdx) {
  if (!chapterData) return;
  if (!chapterData.connor_annotations) chapterData.connor_annotations = {};
  if (!chapterData.connor_summaries) chapterData.connor_summaries = [];
  for (const a of (newAnnos || [])) {
    chapterData.connor_annotations[a.p] = a;
  }
  if (summary) {
    const existing = chapterData.connor_summaries.findIndex(s => s.segment_index === segIdx);
    if (existing >= 0) {
      chapterData.connor_summaries[existing].summary = summary;
    } else {
      chapterData.connor_summaries.push({ segment_index: segIdx, summary });
    }
  }
}

function updateSegBlockStatus(segIdx, status) {
  const segments = chapterData?.chapter.segments_meta || [];
  if (segments[segIdx]) segments[segIdx].status = status;
  renderSegmentStatus(segments);
}

// ── 选中文字 → 跳转聊天窗口提问 ────────────────────
let selectedText = '';
let pendingHighlight = null;  // 待保存的选区信息

// 监听文字选择
function onSelectionChange() {
  if (!chapterData || !$('readerContent')) return;
  const sel = window.getSelection();
  const toolbar = $('selToolbar');
  if (!sel || sel.isCollapsed || !sel.toString().trim()) {
    setTimeout(() => {
      if (!window.getSelection()?.toString().trim()) {
        toolbar.style.display = 'none';
      }
    }, 200);
    return;
  }

  const text = sel.toString().trim();
  if (text.length < 2) return;

  const readerContent = $('readerContent');
  const anchor = sel.anchorNode;
  if (!readerContent.contains(anchor) || !readerContent.contains(sel.focusNode)) return;
  pendingHighlight = null;

  const range = sel.getRangeAt(0);
  const rect = range.getBoundingClientRect();
  const contentRect = readerContent.getBoundingClientRect();
  toolbar.style.display = 'block';
  toolbar.style.left = Math.min(
    Math.max(rect.left + rect.width / 2 - toolbar.offsetWidth / 2, contentRect.left),
    contentRect.right - toolbar.offsetWidth
  ) + 'px';
  toolbar.style.top = Math.max(8, rect.top - toolbar.offsetHeight - 8) + 'px';

  $('selAskAionBtn').textContent = `💬 问${aiName}`;
  $('selAskConnorBtn').textContent = `💬 问${connorName}`;
  selectedText = text;

  // 捕获选区位置信息，用于后续持久化高亮
  try {
    const startNode = range.startContainer;
    const endNode = range.endContainer;
    const startPEl = startNode.nodeType === 3
      ? startNode.parentElement.closest('[data-p]')
      : startNode.closest('[data-p]');
    const endPEl = endNode.nodeType === 3
      ? endNode.parentElement.closest('[data-p]')
      : endNode.closest('[data-p]');
    if (startPEl && endPEl) {
      pendingHighlight = {
        start_p: parseInt(startPEl.dataset.p),
        start_offset: calcTextOffset(startPEl, range.startContainer, range.startOffset),
        end_p: parseInt(endPEl.dataset.p),
        end_offset: calcTextOffset(endPEl, range.endContainer, range.endOffset),
        text: text
      };
    }
  } catch(e) { pendingHighlight = null; }
}

// 计算相对于段落元素的纯文本字符偏移
function calcTextOffset(pEl, container, offset) {
  const range = document.createRange();
  range.setStart(pEl, 0); range.setEnd(container, offset);
  return range.toString().length;
}

// 获取段落元素中所有文本节点的拼接内容
function getParagraphTextFromDOM(pEl) {
  const walker = document.createTreeWalker(pEl, NodeFilter.SHOW_TEXT, null);
  let text = '', node;
  while ((node = walker.nextNode())) text += node.textContent;
  return text;
}

// 在 DOM 段落中查找选中文字的位置（兜底方案，偏移量与 applyHighlights 一致）
function findHighlightInDOM(text) {
  if (!text) return null;
  const content = $('readerContent');
  if (!content) return null;
  const pEls = [...content.querySelectorAll('[data-p]')];
  if (!pEls.length) return null;

  // 单段落匹配
  for (const pEl of pEls) {
    const pText = getParagraphTextFromDOM(pEl);
    const idx = pText.indexOf(text);
    if (idx >= 0) {
      const pi = parseInt(pEl.dataset.p);
      return { start_p: pi, start_offset: idx, end_p: pi, end_offset: idx + text.length, text };
    }
  }

  // 跨段落匹配：拼接相邻段落文本
  for (let i = 0; i < pEls.length - 1; i++) {
    let combined = '';
    const pTexts = [];
    for (let j = i; j < Math.min(i + 6, pEls.length); j++) {
      const t = getParagraphTextFromDOM(pEls[j]);
      pTexts.push({ pi: parseInt(pEls[j].dataset.p), text: t, start: combined.length });
      combined += t;
      const idx = combined.indexOf(text);
      if (idx >= 0) {
        const endIdx = idx + text.length;
        let startP, startOff, endP, endOff;
        for (const pt of pTexts) {
          const ptEnd = pt.start + pt.text.length;
          if (idx >= pt.start && idx < ptEnd) {
            startP = pt.pi; startOff = idx - pt.start;
          }
          if (endIdx > pt.start && endIdx <= ptEnd) {
            endP = pt.pi; endOff = endIdx - pt.start;
          }
        }
        if (startP !== undefined && endP !== undefined) {
          return { start_p: startP, start_offset: startOff, end_p: endP, end_offset: endOff, text };
        }
      }
    }
  }
  return null;
}

function askAIAboutSelection(target) {
  if (!selectedText || !currentBook || replySending || replyPreparing) return;
  askTarget = target || 'aion';

  // 如果 selectionchange 没捕获到位置信息，尝试从当前选区补捕
  if (!pendingHighlight) {
    try {
      const sel = window.getSelection();
      if (sel && !sel.isCollapsed && sel.rangeCount > 0) {
        const range = sel.getRangeAt(0);
        const sn = range.startContainer, en = range.endContainer;
        const sp = sn.nodeType === 3 ? sn.parentElement?.closest('[data-p]') : sn.closest?.('[data-p]');
        const ep = en.nodeType === 3 ? en.parentElement?.closest('[data-p]') : en.closest?.('[data-p]');
        if (sp && ep) {
          pendingHighlight = {
            start_p: parseInt(sp.dataset.p),
            start_offset: calcTextOffset(sp, range.startContainer, range.startOffset),
            end_p: parseInt(ep.dataset.p),
            end_offset: calcTextOffset(ep, range.endContainer, range.endOffset),
            text: selectedText
          };
        }
      }
    } catch(e) {}
  }

  // 兜底：在 DOM 中搜索选中文本
  if (!pendingHighlight) {
    pendingHighlight = findHighlightInDOM(selectedText);
  }

  $('selToolbar').style.display = 'none';
  // 显示提问弹窗
  const quote = selectedText.length > 200 ? selectedText.slice(0, 200) + '...' : selectedText;
  $('askQuote').textContent = quote;
  $('askInput').value = '';
  const targetLabel = askTarget === 'connor' ? connorName : askTarget === 'group' ? '群聊' : aiName;
  $('askPopup').classList.add('show');
  $('askInput').placeholder = `问${targetLabel}点什么？（可留空）`;
  $('askInput').focus();
}

function closeAskPopup() {
  $('askPopup').classList.remove('show');
}

function closeReplyPanel() {
  $('readerReply').classList.remove('show');
}

let replyConvId = null;
let replyConnorRoomId = null;
let replyGroupRoomId = null;
let replySending = false;
let replyPreparing = false;
let replyAionModel = null;   // Aion 使用的模型，从主聊天同步

// 渲染一条聊天气泡
function renderChatBubble(role, content, who) {
  const isUser = role === 'user';
  let name, avatar, rowClass;
  if (isUser) {
    name = userName;
    avatar = '/public/UserIcon.png';
    rowClass = 'user';
  } else if (who === 'connor') {
    name = connorName;
    avatar = '/public/codexicon.png';
    rowClass = 'connor';
  } else {
    name = aiName;
    avatar = '/public/AIIcon.png';
    rowClass = 'assistant';
  }
  // 把文本按双换行分段，多段各自一个气泡
  const parts = content.split(/\n{2,}/).filter(p => p.trim());
  const bubbles = parts.length > 1
    ? parts.map(p => `<div class="rc-bubble">${escHtml(p).replace(/\n/g, '<br>')}</div>`).join('')
    : `<div class="rc-bubble">${escHtml(content).replace(/\n/g, '<br>')}</div>`;
  return `<div class="rc-row ${rowClass}">
    <img class="rc-avatar" src="${avatar}" alt="">
    <div class="rc-body">
      <div class="rc-name">${escHtml(name)}</div>
      ${bubbles}
    </div>
  </div>`;
}

// 加载最近聊天上下文并打开面板
async function openReplyPanel() {
  const token = epoch;
  const targetLabel = askTarget === 'connor' ? connorName : askTarget === 'group' ? '群聊' : aiName;
  $('replyTitle').textContent = `💬 ${targetLabel}`;

  // 根据目标获取 room/conv ID
  // 获取主聊天当前使用的模型
  if (!replyAionModel) {
    try {
      const convs = await api('GET', '/api/conversations');
      if (convs && convs.length > 0) {
        replyAionModel = convs[0].model || null;
        if (!replyConvId) replyConvId = convs[0].id;
      }
    } catch(e) {}
  }

  if (askTarget === 'aion') {
    if (!replyConvId) {
      showToast('没有可用的对话，请先在聊天页创建一个');
      return false;
    }
  } else {
    // 获取 chatroom rooms
    try {
      const rooms = await api('GET', '/api/chatroom/rooms');
      if (rooms) {
        for (const r of rooms) {
          if (r.type === 'connor_1v1' && !replyConnorRoomId) replyConnorRoomId = r.id;
          if (r.type === 'group' && !replyGroupRoomId) replyGroupRoomId = r.id;
        }
      }
    } catch(e) {}

    if (askTarget === 'connor' && !replyConnorRoomId) {
      showToast(`${connorName} 未接入，无法使用此功能`);
      return false;
    }
    if (askTarget === 'group' && !replyGroupRoomId) {
      showToast('群聊房间不存在，请先在聊天室创建');
      return false;
    }
  }

  if (token !== epoch) return false;
  // 加载最近消息作为上下文
  const msgs = $('replyMsgs');
  msgs.innerHTML = '';
  try {
    let history = [];
    if (askTarget === 'aion') {
      history = await api('GET', `/api/conversations/${replyConvId}/messages?limit=6`);
      if (token === epoch && history && history.length > 0) {
        const contextHtml = history
          .filter(m => m.role === 'user' || m.role === 'assistant')
          .map(m => renderChatBubble(m.role, m.role === 'assistant' ? cleanAiDisplay(m.content) : m.content))
          .join('');
        msgs.innerHTML = contextHtml + '<div class="rc-divider">── 以上为最近聊天 ──</div>';
      }
    } else {
      const roomId = askTarget === 'connor' ? replyConnorRoomId : replyGroupRoomId;
      const roomMsgs = await api('GET', `/api/chatroom/rooms/${roomId}/messages?limit=6`);
      if (token === epoch && roomMsgs && roomMsgs.length > 0) {
        const contextHtml = roomMsgs.map(m => {
          const role = m.sender === 'user' ? 'user' : m.sender;
          return renderChatBubble(role === 'user' ? 'user' : 'assistant', cleanAiDisplay(m.content), m.sender === 'connor' ? 'connor' : undefined);
        }).join('');
        msgs.innerHTML = contextHtml + '<div class="rc-divider">── 以上为最近聊天 ──</div>';
      }
    }
  } catch(e) {}

  if (token !== epoch) return false;
  $('replyInput').value = '';
  $('readerReply').classList.add('show');
  // 滚动到底部
  setTimeout(() => { msgs.scrollTop = msgs.scrollHeight; }, 50);
  return true;
}

async function submitAskToChat() {
  if (!currentBook || !chapterData || replySending || replyPreparing) return;
  const token = epoch;
  replyPreparing = true;
  try {
  const source={bookId:currentBook.book_id,index:currentChIdx,chapterId:chapterData.chapter.id};
  const question = $('askInput').value.trim();
  closeAskPopup();

  // 拼装消息内容
  const bookTitle = currentBook.title || '未知';
  const chTitle = chapterData?.chapter?.title || `第 ${currentChIdx + 1} 章`;
  const sel = selectedText.length > 500 ? selectedText.slice(0, 500) + '...' : selectedText;

  // 保存当前提问信息，用于AI回复后保存高亮
  const askQuestion = question || '你觉得这段怎么样？';
  // 三重保险获取高亮位置信息
  let askHighlightInfo = pendingHighlight ? { ...pendingHighlight } : findHighlightInDOM(selectedText);
  if (!askHighlightInfo) {
    console.warn('[highlight] 无法定位选中文字位置, selectedText:', selectedText?.slice(0, 50));
  }

  let msgContent = `我在读《${bookTitle}》「${chTitle}」，看到这段话：\n「${sel}」`;
  if (question) {
    msgContent += `\n${question}`;
  } else {
    msgContent += `\n你觉得这段怎么样？`;
  }

  // 先打开面板、加载上下文，再发送
  const ok = await openReplyPanel();
  if (!ok || token !== epoch) return;

  // 预览用户即将发送的内容（带分割线）
  const msgs = $('replyMsgs');
  msgs.insertAdjacentHTML('beforeend', renderChatBubble('user', msgContent));
  msgs.scrollTop = msgs.scrollHeight;

  // 弹出面板让用户先看上下文 + 自己的消息
  await new Promise(r => setTimeout(r, 100));

  // 发送消息
  if (token !== epoch) return;
  await doSendInPanel(msgContent, true, { highlightInfo: askHighlightInfo, question: askQuestion, selectedText: sel, source });
  } finally {
    if (token === epoch) replyPreparing = false;
  }
}

// 面板内继续追问
async function sendReplyMsg() {
  const input = $('replyInput');
  const text = input.value.trim();
  if (!text || !chapterData || replySending || replyPreparing) return;
  // 检查目标是否有效
  if (askTarget === 'aion' && !replyConvId) return;
  if (askTarget === 'connor' && !replyConnorRoomId) return;
  if (askTarget === 'group' && !replyGroupRoomId) return;
  input.value = '';
  await doSendInPanel(text);
}

// 清理 AI 回复中的指令标签（只保留可读文字）
function cleanAiDisplay(text) {
  return text.replace(/\[MUSIC:[^\]]*\]/g, '').replace(/\[CAM_CHECK\]/g, '').replace(/\[ALARM:[^\]]*\]/g, '').replace(/\[REMINDER:[^\]]*\]/g, '').trim();
}

// 把 AI 回复渲染成多气泡（按双换行分段）
function renderAiBubbles(aiBody, displayText) {
  const parts = displayText.split(/\n{2,}/).filter(p => p.trim());
  // 清除旧气泡
  aiBody.querySelectorAll('.rc-bubble').forEach(e => e.remove());
  if (parts.length > 1) {
    parts.forEach(p => {
      const b = document.createElement('div');
      b.className = 'rc-bubble';
      b.innerHTML = escHtml(p).replace(/\n/g, '<br>');
      aiBody.appendChild(b);
    });
  } else {
    const b = document.createElement('div');
    b.className = 'rc-bubble';
    b.innerHTML = escHtml(displayText).replace(/\n/g, '<br>');
    aiBody.appendChild(b);
  }
}

// 实际调用 send API 并流式渲染
// skipUserBubble: 若为 true 则不再重复添加用户气泡（已由调用方添加）
async function doSendInPanel(msgContent, skipUserBubble, highlightMeta) {
  const token = epoch, target = askTarget;
  replyAbort = new AbortController();
  replySending = true;
  $('replySendBtn').disabled = true;

  const msgs = $('replyMsgs');

  // 添加用户气泡
  if (!skipUserBubble) {
    msgs.insertAdjacentHTML('beforeend', renderChatBubble('user', msgContent));
  }

  let fullReply = '';
  let connorReply = '';

  if (askTarget === 'aion') {
    // ── Aion 私聊模式 ──
    const aiRow = document.createElement('div');
    aiRow.className = 'rc-row assistant';
    aiRow.innerHTML = `
      <img class="rc-avatar" src="/public/AIIcon.png" alt="">
      <div class="rc-body">
        <div class="rc-name">${escHtml(aiName)}</div>
        <div class="rc-bubble thinking">正在思考...</div>
      </div>`;
    msgs.appendChild(aiRow);
    msgs.scrollTop = msgs.scrollHeight;
    const aiBody = aiRow.querySelector('.rc-body');

    try {
      const resp = await fetch(`/api/conversations/${replyConvId}/send`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: replyAbort.signal,
        body: JSON.stringify({ content: msgContent, context_limit: 30 })
      });
      fullReply = await _streamToBody(resp, aiBody, msgs);
    } catch(e) {
      if (token === epoch && e.name !== 'AbortError') _showError(aiBody, e.message);
    }

  } else if (askTarget === 'connor') {
    // ── Connor 1v1 模式 ──
    const cRow = document.createElement('div');
    cRow.className = 'rc-row connor';
    cRow.innerHTML = `
      <img class="rc-avatar" src="/public/codexicon.png" alt="">
      <div class="rc-body">
        <div class="rc-name">${escHtml(connorName)}</div>
        <div class="rc-bubble thinking">正在思考...</div>
      </div>`;
    msgs.appendChild(cRow);
    msgs.scrollTop = msgs.scrollHeight;
    const cBody = cRow.querySelector('.rc-body');

    try {
      const connorPayload = { content: msgContent };
      if (replyAionModel) connorPayload.model = replyAionModel;
      const resp = await fetch(`/api/chatroom/rooms/${replyConnorRoomId}/send`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: replyAbort.signal,
        body: JSON.stringify(connorPayload)
      });
      fullReply = await _streamChatroomToBody(resp, null, cBody, msgs);
    } catch(e) {
      if (token === epoch && e.name !== 'AbortError') _showError(cBody, e.message);
    }

  } else {
    // ── 群聊模式：两个 AI 都会回复 ──
    const aRow = document.createElement('div');
    aRow.className = 'rc-row assistant';
    aRow.innerHTML = `
      <img class="rc-avatar" src="/public/AIIcon.png" alt="">
      <div class="rc-body">
        <div class="rc-name">${escHtml(aiName)}</div>
        <div class="rc-bubble thinking">正在思考...</div>
      </div>`;
    msgs.appendChild(aRow);
    const aBody = aRow.querySelector('.rc-body');

    const cRow = document.createElement('div');
    cRow.className = 'rc-row connor';
    cRow.innerHTML = `
      <img class="rc-avatar" src="/public/codexicon.png" alt="">
      <div class="rc-body">
        <div class="rc-name">${escHtml(connorName)}</div>
        <div class="rc-bubble thinking">等待中...</div>
      </div>`;
    msgs.appendChild(cRow);
    const cBody = cRow.querySelector('.rc-body');
    msgs.scrollTop = msgs.scrollHeight;

    try {
      const groupPayload = { content: msgContent };
      if (replyAionModel) groupPayload.model = replyAionModel;
      const resp = await fetch(`/api/chatroom/rooms/${replyGroupRoomId}/send`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: replyAbort.signal,
        body: JSON.stringify(groupPayload)
      });
      const results = await _streamChatroomToBody(resp, aBody, cBody, msgs);
      fullReply = results.aion || '';
      connorReply = results.connor || '';
    } catch(e) {
      if (token === epoch && e.name !== 'AbortError') _showError(aBody, e.message);
    }
  }

  if (token !== epoch) return;
  // AI 回复完成后，保存高亮
  if (highlightMeta && highlightMeta.highlightInfo && (fullReply || connorReply) && currentBook?.book_id===highlightMeta.source.bookId && currentChIdx===highlightMeta.source.index && chapterData?.chapter.id===highlightMeta.source.chapterId) {
    try {
      const hl = highlightMeta.highlightInfo;
      const res = await api('POST', `/api/books/${currentBook.book_id}/chapters/${currentChIdx}/highlights`, {
        chapter_id: highlightMeta.source.chapterId,
        selected_text: highlightMeta.selectedText,
        start_p: hl.start_p,
        start_offset: hl.start_offset,
        end_p: hl.end_p,
        end_offset: hl.end_offset,
        question: highlightMeta.question,
        answer: fullReply,
        annotator: target,
        connor_answer: connorReply
      });
      if (token === epoch && res && res.id) {
        // 将新高亮加入当前数据并立即应用
        const newHl = {
          id: res.id,
          selected_text: highlightMeta.selectedText,
          start_p: hl.start_p, start_offset: hl.start_offset,
          end_p: hl.end_p, end_offset: hl.end_offset,
          question: highlightMeta.question,
          answer: fullReply,
          annotator: target,
          connor_answer: connorReply,
          created_at: Date.now() / 1000
        };
        if (!chapterData.highlights) chapterData.highlights = [];
        chapterData.highlights.push(newHl);
        applyHighlights([newHl]);
      }
    } catch(e) { if (token === epoch) showToast('保存标注失败: ' + e.message); }
    if (token === epoch) pendingHighlight = null;
  }
  if (token !== epoch) return;
  replyAbort = null;
  replySending = false;
  $('replySendBtn').disabled = false;
  $('replyInput').focus();
}

// 流式读取 Aion 私聊 SSE 并渲染到 body
async function _streamToBody(resp, body, msgs) {
  if (!resp.ok) throw Error((await resp.json()).detail || '请求失败');
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let fullReply = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop();

    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      const raw = line.slice(6).trim();
      if (!raw) continue;
      let msg;
      try { msg = JSON.parse(raw); } catch { continue; }

      if (msg.type === 'chunk') {
        fullReply += msg.content;
        const display = cleanAiDisplay(fullReply);
        renderAiBubbles(body, display);
        msgs.scrollTop = msgs.scrollHeight;
      } else if (msg.type === 'music') {
        if (msg.cards && msg.cards.length) {
          playMusicOnline(msg.cards[0].id);
        }
      } else if (msg.type === 'error') {
        _showError(body, msg.message);
      }
    }
  }
  return fullReply;
}

// 流式读取 chatroom SSE 并渲染到对应 body
// aionBody 可为 null（Connor 1v1 时）
async function _streamChatroomToBody(resp, aionBody, connorBody, msgs) {
  if (!resp.ok) throw Error((await resp.json()).detail || '请求失败');
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let aionReply = '';
  let connorReply = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop();

    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      const raw = line.slice(6).trim();
      if (!raw) continue;
      let msg;
      try { msg = JSON.parse(raw); } catch { continue; }

      if (msg.type === 'aion_chunk' && aionBody) {
        aionReply += msg.content;
        renderAiBubbles(aionBody, cleanAiDisplay(aionReply));
        msgs.scrollTop = msgs.scrollHeight;
      } else if (msg.type === 'connor_chunk' && connorBody) {
        connorReply += msg.content;
        renderAiBubbles(connorBody, cleanAiDisplay(connorReply));
        msgs.scrollTop = msgs.scrollHeight;
      } else if (msg.type === 'connor_start' && connorBody) {
        // Connor 开始回复，更新提示
        const thinkBubble = connorBody.querySelector('.rc-bubble.thinking');
        if (thinkBubble) thinkBubble.textContent = '正在思考...';
      } else if (msg.type === 'aion_start' && aionBody) {
        const thinkBubble = aionBody.querySelector('.rc-bubble.thinking');
        if (thinkBubble) thinkBubble.textContent = '正在思考...';
      } else if (msg.type === 'error') {
        if (aionBody) _showError(aionBody, msg.message);
        else if (connorBody) _showError(connorBody, msg.message);
      }
    }
  }

  // Connor 1v1 模式只返回 connorReply
  if (!aionBody) return connorReply;
  // 群聊返回两个
  return { aion: aionReply, connor: connorReply };
}

function _showError(body, message) {
  body.querySelectorAll('.rc-bubble').forEach(e => e.remove());
  const errB = document.createElement('div');
  errB.className = 'rc-bubble';
  errB.textContent = '出错了: ' + message;
  body.appendChild(errB);
}

// ── 高亮持久化：应用橙色下划线 + 点击查看 ──────────────
function applyHighlights(highlights) {
  for (const hl of highlights) {
    try {
      const content = $('readerContent');
      // 找到所有涉及的段落
      for (let pi = hl.start_p; pi <= hl.end_p; pi++) {
        const pEl = content.querySelector(`[data-p="${pi}"]`);
        if (!pEl) continue;

        const walker = document.createTreeWalker(pEl, NodeFilter.SHOW_TEXT, null);
        let node, charPos = 0, ranges = [];

        // 确定本段落内的起止偏移
        const localStart = (pi === hl.start_p) ? hl.start_offset : 0;
        const localEnd = (pi === hl.end_p) ? hl.end_offset : Infinity;

        while ((node = walker.nextNode())) {
          const nodeLen = node.textContent.length;
          const nodeStart = charPos;
          const nodeEnd = charPos + nodeLen;

          // 检查是否与目标区间重叠
          if (nodeEnd > localStart && nodeStart < localEnd) {
            const rangeStart = Math.max(0, localStart - nodeStart);
            const rangeEnd = Math.min(nodeLen, localEnd - nodeStart);
            ranges.push({ node, start: rangeStart, end: rangeEnd });
          }
          charPos += nodeLen;
        }

        // 包裹文本节点（从后往前以避免偏移变化）
        for (let i = ranges.length - 1; i >= 0; i--) {
          const r = ranges[i];
          const range = document.createRange();
          range.setStart(r.node, r.start);
          range.setEnd(r.node, r.end);

          // surroundContents 在跨元素边界时会失败，用手动方式替代
          const span = document.createElement('span');
          span.className = 'user-hl';
          span.dataset.hlId = hl.id;
          span.onclick = function(e) {
            e.stopPropagation();
            showHighlightDetail(hl.id);
          };
          try {
            range.surroundContents(span);
          } catch(_) {
            // 降级：提取内容再包裹
            span.appendChild(range.extractContents());
            range.insertNode(span);
          }
        }
      }
    } catch(e) {
      console.warn('应用高亮失败', hl.id, e);
    }
  }
}

let currentHlId = null;
function showHighlightDetail(hlId) {
  if (!chapterData || !chapterData.highlights) return;
  const hl = chapterData.highlights.find(h => h.id === hlId);
  if (!hl) return;
  currentHlId = hlId;
  $('hlPopupQuote').textContent = hl.selected_text.length > 200
    ? hl.selected_text.slice(0, 200) + '...'
    : hl.selected_text;
  $('hlPopupQuestion').textContent = '💬 ' + hl.question;

  // 根据 annotator 类型显示回复
  let answerHtml = '';
  const annotator = hl.annotator || 'aion';
  if (annotator === 'group') {
    if (hl.answer) answerHtml += `${aiName}：\n${hl.answer}\n\n`;
    if (hl.connor_answer) answerHtml += `${connorName}：\n${hl.connor_answer}`;
  } else if (annotator === 'connor') {
    answerHtml = hl.answer || hl.connor_answer || '';
  } else {
    answerHtml = hl.answer || '';
  }
  $('hlPopupAnswer').innerHTML = escHtml(answerHtml).replace(/\n/g, '<br>');
  $('hlPopup').classList.add('show');
}

function closeHlPopup() {
  $('hlPopup').classList.remove('show');
  currentHlId = null;
}

async function deleteCurrentHighlight() {
  if (!currentHlId || !currentBook) return;
  if (!confirm('确认删除这条标注？')) return;
  const token = epoch, id = currentHlId;
  try {
    await api('DELETE', `/api/books/${currentBook.book_id}/highlights/${id}`);
    if (token !== epoch) return;
    // 从数据中移除
    if (chapterData.highlights) {
      chapterData.highlights = chapterData.highlights.filter(h => h.id !== id);
    }
    // 移除DOM中的高亮标记
    $('readerContent').querySelectorAll(`[data-hl-id="${id}"]`).forEach(el => {
      const parent = el.parentNode;
      while (el.firstChild) parent.insertBefore(el.firstChild, el);
      parent.removeChild(el);
      parent.normalize();
    });
    closeHlPopup();
    showToast('已删除');
  } catch(e) {
    showToast('删除失败');
  }
}

  return {mount, setChapter, reset,
    get busy() { return isAnnotating || replySending || replyPreparing; },
    get data() { return chapterData; },
    inviteRead, showAnnotation, showSummary, closeAnnoPopup, closeHlPopup, deleteCurrentHighlight, askAIAboutSelection, closeAskPopup, closeReplyPanel, submitAskToChat, sendReplyMsg};
})();
window.ReadingCompanion = ReadingCompanion;

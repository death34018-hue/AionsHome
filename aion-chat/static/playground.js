/* ── 娱乐室前端逻辑 ── */

// 主题
(function() {
  const saved = localStorage.getItem('aion_chat_theme') || 'dark';
  document.body.dataset.theme = saved;
})();

// 状态
let currentServer = '';
let isConnected = false;
let isRunning = false;
let currentEventSource = null;

// DOM
const serverSelect = document.getElementById('serverSelect');
const personaSelect = document.getElementById('personaSelect');
const statusIcon = document.getElementById('statusIcon');
const statusName = document.getElementById('statusName');
const statusDetail = document.getElementById('statusDetail');
const connectBtn = document.getElementById('connectBtn');
const toolsPanel = document.getElementById('toolsPanel');
const toolsList = document.getElementById('toolsList');
const toolCount = document.getElementById('toolCount');
const toolsArrow = document.getElementById('toolsArrow');
const logArea = document.getElementById('logArea');
const logPlaceholder = document.getElementById('logPlaceholder');
const instructionInput = document.getElementById('instructionInput');
const sendBtn = document.getElementById('sendBtn');
const modelSelect = document.getElementById('modelSelect');

// ── 加载可用模型列表 ──
async function loadModels() {
  try {
    const resp = await fetch('/api/models');
    const models = await resp.json();  // 返回数组 [{key, provider, ...}]

    modelSelect.innerHTML = '<option value="">🤖 自动</option>';
    (models || []).forEach(m => {
      const opt = document.createElement('option');
      opt.value = m.key;
      opt.textContent = m.key;
      modelSelect.appendChild(opt);
    });

    const savedModel = localStorage.getItem('playground_model') || '';
    if (modelSelect.querySelector(`option[value="${savedModel}"]`)) {
      modelSelect.value = savedModel;
    }
  } catch (e) {
    console.error('加载模型列表失败:', e);
  }
}

modelSelect.addEventListener('change', () => {
  localStorage.setItem('playground_model', modelSelect.value);
});

// ── 初始化：加载服务器列表 ──
async function loadServers() {
  try {
    const resp = await fetch('/api/playground/servers');
    const data = await resp.json();
    const servers = data.servers || [];

    serverSelect.innerHTML = '<option value="">选择服务器...</option>';
    servers.forEach(s => {
      const opt = document.createElement('option');
      opt.value = s.name;
      opt.textContent = s.name + (s.connected ? ' ✅' : '');
      serverSelect.appendChild(opt);

      // 如果已连接，自动选中
      if (s.connected) {
        serverSelect.value = s.name;
        currentServer = s.name;
        isConnected = true;
        updateStatusCard(true, s.name, s.tool_count);
      }
    });
  } catch (e) {
    console.error('加载服务器列表失败:', e);
  }
}

// ── 加载 AI 人格列表 ──
async function loadPersonas() {
  try {
    const resp = await fetch('/api/playground/personas');
    const data = await resp.json();
    const personas = data.personas || [];

    personaSelect.innerHTML = '';
    personas.forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.key;
      opt.textContent = p.emoji + ' ' + p.name;
      personaSelect.appendChild(opt);
    });

    // 恢复上次选择的人格
    const savedPersona = localStorage.getItem('playground_persona') || 'aion';
    if (personaSelect.querySelector(`option[value="${savedPersona}"]`)) {
      personaSelect.value = savedPersona;
    }
  } catch (e) {
    console.error('加载 AI 人格失败:', e);
  }
}

// 保存人格选择
personaSelect.addEventListener('change', () => {
  localStorage.setItem('playground_persona', personaSelect.value);
});

// 选择服务器
serverSelect.addEventListener('change', () => {
  const name = serverSelect.value;
  if (!name) return;
  currentServer = name;
  // 如果选了新的服务器，重置连接状态
  if (isConnected) {
    updateStatusCard(false, name, 0);
    isConnected = false;
    toolsPanel.style.display = 'none';
  }
  updateStatusCard(false, name, 0);
});

// ── 连接/断开 ──
async function toggleConnect() {
  if (!currentServer) {
    currentServer = serverSelect.value;
    if (!currentServer) return;
  }

  if (isConnected) {
    // 断开
    try {
      await fetch('/api/playground/disconnect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ server: currentServer }),
      });
    } catch (e) { /* ignore */ }
    isConnected = false;
    updateStatusCard(false, currentServer, 0);
    toolsPanel.style.display = 'none';
    return;
  }

  // 连接
  connectBtn.textContent = '连接中...';
  connectBtn.disabled = true;

  try {
    const resp = await fetch('/api/playground/connect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ server: currentServer }),
    });
    const data = await resp.json();

    if (data.ok) {
      isConnected = true;
      updateStatusCard(true, currentServer, data.count);
      renderTools(data.tools || []);
    } else {
      updateStatusCard(false, currentServer, 0);
      addLog('error', '连接失败: ' + (data.error || '未知错误'));
    }
  } catch (e) {
    updateStatusCard(false, currentServer, 0);
    addLog('error', '连接失败: ' + e.message);
  }

  connectBtn.disabled = false;
}

function updateStatusCard(connected, name, tools) {
  if (connected) {
    statusIcon.textContent = '🟢';
    statusName.textContent = name + ' 已连接';
    statusDetail.textContent = `可用工具: ${tools} 个`;
    connectBtn.textContent = '断开';
    connectBtn.classList.add('connected');
  } else {
    statusIcon.textContent = '⚪';
    statusName.textContent = name || '未连接';
    statusDetail.textContent = name ? '点击连接按钮' : '请选择一个服务器';
    connectBtn.textContent = '连接';
    connectBtn.classList.remove('connected');
  }
}

// ── 工具面板 ──
function renderTools(tools) {
  toolsList.innerHTML = '';
  toolCount.textContent = tools.length;

  tools.forEach(t => {
    const tag = document.createElement('span');
    tag.className = 'pg-tool-tag';
    tag.textContent = t.name;
    tag.title = t.description || t.name;
    toolsList.appendChild(tag);
  });

  toolsPanel.style.display = '';
  toolsList.classList.remove('collapsed');
  toolsArrow.classList.remove('collapsed');
}

function toggleToolsPanel() {
  toolsList.classList.toggle('collapsed');
  toolsArrow.classList.toggle('collapsed');
}

// ── 获取当前对话 ID ──
function getCurrentConvId() {
  try {
    return localStorage.getItem('aion_last_conv') || '';
  } catch {
    return '';
  }
}

// ── 发送指令 ──
async function sendInstruction() {
  if (isRunning) {
    stopTask();
    return;
  }

  const instruction = instructionInput.value.trim();
  if (!instruction) return;
  if (!isConnected || !currentServer) {
    addLog('error', '请先连接一个服务器');
    return;
  }

  isRunning = true;
  instructionInput.disabled = true;
  sendBtn.textContent = '停止';
  sendBtn.classList.add('stop-btn');

  // 切换到实时日志视图
  showLiveLog();
  addLog('status', `📤 指令: ${instruction}`);

  const convId = getCurrentConvId();

  try {
    const resp = await fetch('/api/playground/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        server: currentServer,
        instruction: instruction,
        conv_id: convId,
        persona: personaSelect.value || 'aion',
        model: modelSelect.value || '',
      }),
    });

    if (!resp.ok) {
      addLog('error', `请求失败: ${resp.status}`);
      finishRun();
      return;
    }

    // 读取 SSE 流
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      let currentEvent = '';
      for (const line of lines) {
        if (line.startsWith('event: ')) {
          currentEvent = line.slice(7).trim();
        } else if (line.startsWith('data: ')) {
          const dataStr = line.slice(6);
          try {
            const data = JSON.parse(dataStr);
            handleSSE(currentEvent, data);
          } catch { /* ignore parse errors */ }
        }
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') {
      addLog('error', '请求异常: ' + e.message);
    }
  }

  finishRun();
}

function finishRun() {
  isRunning = false;
  instructionInput.disabled = false;
  instructionInput.value = '';
  instructionInput.focus();
  sendBtn.textContent = '出发';
  sendBtn.classList.remove('stop-btn');
  // 运行结束后重新加载历史（新记录会出现）
  loadHistory();
}

async function stopTask() {
  try {
    await fetch('/api/playground/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ server: currentServer }),
    });
  } catch { /* ignore */ }
  addLog('status', '⏹ 正在停止...');
}

// ── SSE 事件处理 ──
function handleSSE(event, data) {
  switch (event) {
    case 'status':
      addLog('status', '📋 ' + data);
      break;
    case 'thinking':
      addLog('thinking', '🤔 ' + data);
      break;
    case 'tool_call':
      addToolCallLog(data);
      break;
    case 'tool_result':
      addToolResultLog(data);
      break;
    case 'text':
      addLog('text', '💬 ' + data);
      break;
    case 'error':
      addLog('error', '❌ ' + data);
      break;
    case 'done':
      addLog('done', '✨ ' + data);
      break;
  }
}

// ── 日志渲染 ──
const historyPanel = document.getElementById('historyPanel');
const historyList = document.getElementById('historyList');
const liveLog = document.getElementById('liveLog');

function showLiveLog() {
  historyPanel.style.display = 'none';
  liveLog.style.display = 'flex';
  liveLog.innerHTML = '';
}

function showHistory() {
  liveLog.style.display = 'none';
  historyPanel.style.display = 'flex';
}

function addLog(type, content) {
  const el = document.createElement('div');
  el.className = `pg-log-item ${type}`;
  el.textContent = content;
  liveLog.appendChild(el);
  logArea.scrollTop = logArea.scrollHeight;
}

function addToolCallLog(data) {
  const el = document.createElement('div');
  el.className = 'pg-log-item tool-call';

  let html = `<span class="pg-log-label">🔧 调用工具:</span> ${escapeHtml(data.name)}`;
  if (data.args && Object.keys(data.args).length > 0) {
    html += `<div class="pg-tool-args">${escapeHtml(JSON.stringify(data.args, null, 2))}</div>`;
  }
  el.innerHTML = html;

  liveLog.appendChild(el);
  logArea.scrollTop = logArea.scrollHeight;
}

function addToolResultLog(data) {
  const el = document.createElement('div');
  el.className = 'pg-log-item tool-result';

  let result = data.result || '';
  if (result.length > 1000) {
    result = result.substring(0, 1000) + '...';
  }

  el.innerHTML = `<span class="pg-log-label">📋 ${escapeHtml(data.name)} 结果:</span><br>${escapeHtml(result)}`;
  liveLog.appendChild(el);
  logArea.scrollTop = logArea.scrollHeight;
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// ── 历史记录 ──
async function loadHistory() {
  try {
    const resp = await fetch('/api/playground/logs?limit=50');
    const data = await resp.json();
    const logs = data.logs || [];

    const placeholder = document.getElementById('logPlaceholder');
    if (logs.length === 0) {
      if (placeholder) placeholder.style.display = '';
      historyList.innerHTML = '';
      return;
    }

    if (placeholder) placeholder.style.display = 'none';
    historyList.innerHTML = '';

    logs.forEach(log => {
      const card = document.createElement('div');
      card.className = 'pg-history-card';
      card.dataset.id = log.id;

      const time = new Date(log.created_at * 1000);
      const timeStr = `${time.getMonth()+1}/${time.getDate()} ${time.getHours().toString().padStart(2,'0')}:${time.getMinutes().toString().padStart(2,'0')}`;

      const summaryHtml = log.summary ? `<div class="pg-history-summary">${escapeHtml(log.summary)}</div>` : '';

      card.innerHTML = `
        <div class="pg-history-head">
          <span class="pg-history-icon">🗺️</span>
          <div class="pg-history-info">
            <div class="pg-history-instruction">${escapeHtml(log.instruction)}</div>
            <div class="pg-history-meta">${escapeHtml(log.server)} · ${timeStr}</div>
          </div>
          <button class="pg-history-del" title="删除" onclick="event.stopPropagation();deleteLog('${log.id}')">✕</button>
        </div>
        ${summaryHtml}
        <div class="pg-history-events"></div>
      `;

      const eventsContainer = card.querySelector('.pg-history-events');
      renderEventsToContainer(log.events, eventsContainer);

      card.addEventListener('click', () => {
        card.classList.toggle('expanded');
      });

      historyList.appendChild(card);
    });
  } catch (e) {
    console.error('加载历史失败:', e);
  }
}

function renderEventsToContainer(events, container) {
  events.forEach(ev => {
    const el = document.createElement('div');
    const event = ev.event;
    const data = ev.data;

    if (event === 'tool_call') {
      el.className = 'pg-log-item tool-call';
      let html = `<span class="pg-log-label">🔧 调用工具:</span> ${escapeHtml(data.name)}`;
      if (data.args && Object.keys(data.args).length > 0) {
        html += `<div class="pg-tool-args">${escapeHtml(JSON.stringify(data.args, null, 2))}</div>`;
      }
      el.innerHTML = html;
    } else if (event === 'tool_result') {
      el.className = 'pg-log-item tool-result';
      let result = data.result || '';
      if (result.length > 1000) result = result.substring(0, 1000) + '...';
      el.innerHTML = `<span class="pg-log-label">📋 ${escapeHtml(data.name)} 结果:</span><br>${escapeHtml(result)}`;
    } else if (event === 'text') {
      el.className = 'pg-log-item text';
      el.textContent = '💬 ' + data;
    } else if (event === 'thinking') {
      el.className = 'pg-log-item thinking';
      el.textContent = '🤔 ' + data;
    } else if (event === 'status') {
      el.className = 'pg-log-item status';
      el.textContent = '📋 ' + data;
    } else if (event === 'error') {
      el.className = 'pg-log-item error';
      el.textContent = '❌ ' + data;
    } else if (event === 'done') {
      el.className = 'pg-log-item done';
      el.textContent = '✨ ' + data;
    } else {
      return; // 跳过未知事件
    }

    container.appendChild(el);
  });
}

async function deleteLog(logId) {
  try {
    await fetch(`/api/playground/logs/${logId}`, { method: 'DELETE' });
    const card = document.querySelector(`.pg-history-card[data-id="${logId}"]`);
    if (card) {
      card.style.transition = 'opacity 0.3s';
      card.style.opacity = '0';
      setTimeout(() => card.remove(), 300);
    }
  } catch (e) {
    console.error('删除失败:', e);
  }
}

// ── 启动 ──
loadServers();
loadPersonas();
loadModels();
loadHistory();

// ── 服务器管理 ──
function toggleManagePanel() {
  const overlay = document.getElementById('manageOverlay');
  if (overlay.style.display === 'none') {
    overlay.style.display = 'flex';
    renderManageList();
  } else {
    overlay.style.display = 'none';
  }
}

function renderManageList() {
  const list = document.getElementById('manageList');
  list.innerHTML = '<div style="opacity:0.5;font-size:12px;padding:8px;">加载中...</div>';
  fetch('/api/playground/servers').then(r => r.json()).then(data => {
    const servers = data.servers || [];
    if (servers.length === 0) {
      list.innerHTML = '<div style="opacity:0.5;font-size:13px;padding:8px;">暂无服务器，在下方添加</div>';
      return;
    }
    list.innerHTML = '';
    servers.forEach(s => {
      const row = document.createElement('div');
      row.className = 'pg-manage-row';
      row.innerHTML = `
        <span class="pg-manage-row-icon">${s.connected ? '🟢' : '⚪'}</span>
        <div class="pg-manage-row-info">
          <div class="pg-manage-row-name">${escapeHtml(s.name)}</div>
          <div class="pg-manage-row-detail">${escapeHtml(s.type)} · ${s.connected ? '已连接' : '未连接'}</div>
        </div>
        <button class="pg-manage-row-del" onclick="removeServer('${escapeHtml(s.name)}')" title="删除">✕</button>
      `;
      list.appendChild(row);
    });
  });
}

async function addServer() {
  const name = document.getElementById('addName').value.trim();
  const url = document.getElementById('addUrl').value.trim();
  const type = document.getElementById('addType').value;
  if (!name || !url) return;

  try {
    const resp = await fetch('/api/playground/servers/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, type, url }),
    });
    const data = await resp.json();
    if (data.ok) {
      document.getElementById('addName').value = '';
      document.getElementById('addUrl').value = '';
      renderManageList();
      loadServers();
    } else {
      alert('添加失败: ' + (data.error || '未知错误'));
    }
  } catch (e) {
    alert('添加失败: ' + e.message);
  }
}

async function removeServer(name) {
  if (!confirm(`确认删除「${name}」？`)) return;
  try {
    const resp = await fetch('/api/playground/servers/remove', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ server: name }),
    });
    const data = await resp.json();
    if (data.ok) {
      renderManageList();
      loadServers();
      if (currentServer === name) {
        currentServer = '';
        isConnected = false;
        updateStatusCard(false, '', 0);
        toolsPanel.style.display = 'none';
      }
    } else {
      alert('删除失败: ' + (data.error || '未知错误'));
    }
  } catch (e) {
    alert('删除失败: ' + e.message);
  }
}

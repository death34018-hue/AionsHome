const $ = id => document.getElementById(id);

let seekyConfig = {
  name: 'Seeky',
  persona: '',
  model: 'Gemini-3.1-lite',
  context_limit: 40,
};
let models = [];
let messages = [];
let memoryReview = null;
let reviewBusy = false;
let sending = false;
let toastTimer = null;
let reviewPollTimer = null;
let reviewMode = 'compress';
let petCare = null;
let livePetMessage = null;
let feeding = false;
let petCareTimer = null;
let petSpeechTimer = null;

const PET_CARE_KEY = 'seeky_pet_care_v1';
const HOUR_MS = 60 * 60 * 1000;

function esc(text) {
  const div = document.createElement('div');
  div.textContent = text == null ? '' : String(text);
  return div.innerHTML;
}

async function api(method, url, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(url, opts);
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const data = await res.json();
      msg = data.detail || data.error || msg;
    } catch {}
    throw new Error(msg);
  }
  return res.json();
}

function showToast(text) {
  const toast = $('toast');
  toast.textContent = text;
  toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('show'), 1800);
}

function clamp(value, min = 0, max = 100) {
  return Math.max(min, Math.min(max, Number(value) || 0));
}

function rand(min, max) {
  return Math.random() * (max - min) + min;
}

function randInt(min, max) {
  return Math.floor(rand(min, max + 1));
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function clockText(ts = Date.now()) {
  const d = new Date(ts);
  const pad = n => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function nextWasteTime(base = Date.now(), soon = false) {
  const min = soon ? 0.5 * HOUR_MS : 2.5 * HOUR_MS;
  const max = soon ? 4 * HOUR_MS : 7 * HOUR_MS;
  return base + rand(min, max);
}

function newWaste(createdAt = Date.now()) {
  return {
    id: `waste_${createdAt}_${Math.random().toString(36).slice(2, 8)}`,
    x: rand(17, 84),
    bottom: rand(8, 22),
    created_at: createdAt,
  };
}

function defaultPetCare() {
  const now = Date.now();
  return {
    fullness: 72,
    cleanliness: 92,
    happiness: 76,
    last_seen_at: now,
    next_waste_at: nextWasteTime(now),
    wastes: [],
    events: [],
    seeky_x: 8,
    seeky_y: 38,
  };
}

function normalizePetCare(raw) {
  const base = defaultPetCare();
  const state = raw && typeof raw === 'object' ? { ...base, ...raw } : base;
  state.fullness = clamp(state.fullness);
  state.cleanliness = clamp(state.cleanliness);
  state.happiness = clamp(state.happiness);
  state.last_seen_at = Number(state.last_seen_at) || Date.now();
  state.next_waste_at = Number(state.next_waste_at) || nextWasteTime(Date.now());
  state.wastes = Array.isArray(state.wastes) ? state.wastes.slice(0, 6) : [];
  state.events = Array.isArray(state.events) ? state.events.slice(-24) : [];
  state.seeky_x = clamp(state.seeky_x, 3, 72);
  state.seeky_y = clamp(state.seeky_y, 18, 64);
  return state;
}

function loadPetCareState() {
  try {
    petCare = normalizePetCare(JSON.parse(localStorage.getItem(PET_CARE_KEY) || 'null'));
  } catch {
    petCare = defaultPetCare();
  }
  applyPetCareTime(Date.now(), true);
  savePetCareState();
  renderPetCare();
}

function savePetCareState() {
  if (!petCare) return;
  petCare.last_seen_at = Date.now();
  localStorage.setItem(PET_CARE_KEY, JSON.stringify(petCare));
}

function applyPetCareTime(now = Date.now(), includeOfflineWaste = false) {
  if (!petCare) return;
  const lastSeen = Number(petCare.last_seen_at) || now;
  const elapsedHours = clamp((now - lastSeen) / HOUR_MS, 0, 24 * 14);
  if (elapsedHours > 0) {
    petCare.fullness = clamp(petCare.fullness - elapsedHours * 3.2);
    petCare.cleanliness = clamp(petCare.cleanliness - elapsedHours * 0.7 - petCare.wastes.length * 0.35);
    const hungryPenalty = petCare.fullness < 35 ? elapsedHours * 1.2 : 0;
    const dirtyPenalty = petCare.cleanliness < 45 ? elapsedHours * 0.9 : 0;
    petCare.happiness = clamp(petCare.happiness - elapsedHours * 0.55 - hungryPenalty - dirtyPenalty);
  }

  let spawned = 0;
  while (petCare.next_waste_at <= now && petCare.wastes.length < 6) {
    if (!includeOfflineWaste && spawned >= 1) break;
    petCare.wastes.push(newWaste(petCare.next_waste_at));
    petCare.cleanliness = clamp(petCare.cleanliness - 10);
    petCare.happiness = clamp(petCare.happiness - 3);
    petCare.next_waste_at = nextWasteTime(petCare.next_waste_at);
    spawned += 1;
  }
  if (petCare.next_waste_at <= now) {
    petCare.next_waste_at = nextWasteTime(now);
  }
  petCare.last_seen_at = now;
}

function setMeter(id, value) {
  const el = $(id);
  if (!el) return;
  const next = clamp(value);
  el.style.setProperty('--value', `${Math.round(next)}%`);
  el.classList.toggle('low', next < 34);
}

function petCareStatusText() {
  if (feeding) return 'FEEDING';
  if (!petCare) return 'ONLINE';
  if (petCare.cleanliness < 34 || petCare.wastes.length >= 3) return 'CLEAN ME';
  if (petCare.fullness < 28) return 'HUNGRY';
  if (petCare.happiness > 78) return 'HAPPY';
  return 'ONLINE';
}

function refreshPetStatus() {
  const status = $('petStatus');
  if (status) status.textContent = petCareStatusText();
}

function renderPetCare() {
  if (!petCare) return;
  setMeter('fullnessStat', petCare.fullness);
  setMeter('cleanStat', petCare.cleanliness);
  setMeter('happyStat', petCare.happiness);

  const aquarium = document.querySelector('.aquarium');
  if (aquarium) aquarium.classList.toggle('is-murky', petCare.cleanliness < 52 || petCare.wastes.length >= 2);

  const orbit = document.querySelector('.pet-orbit');
  if (orbit) {
    orbit.classList.toggle('hungry', petCare.fullness < 34);
    orbit.classList.toggle('dirty', petCare.cleanliness < 48);
    orbit.classList.toggle('happy', petCare.happiness > 74);
  }

  const wasteLayer = $('wasteLayer');
  if (wasteLayer) {
    const signature = petCare.wastes.map(item => `${item.id}:${item.x.toFixed(1)}:${item.bottom.toFixed(1)}`).join('|');
    if (wasteLayer.dataset.signature !== signature) {
      wasteLayer.dataset.signature = signature;
      wasteLayer.innerHTML = petCare.wastes.map(item => `
        <button class="waste-clump" type="button" data-waste-id="${esc(item.id)}"
          title="清理一下"
          style="--x:${item.x.toFixed(2)}%;--bottom:${item.bottom.toFixed(2)}%"></button>
      `).join('');
    }
  }
  refreshPetStatus();
}

function showTemporaryPetBubble(text, tone = 'normal', ms = 2600) {
  const pop = $('petPopBubble');
  if (!pop) return;
  const aquarium = document.querySelector('.aquarium');
  const avatar = $('petAvatar');
  if (aquarium && avatar) {
    const tankRect = aquarium.getBoundingClientRect();
    const avatarRect = avatar.getBoundingClientRect();
    const x = clamp(((avatarRect.left + avatarRect.width * 0.48 - tankRect.left) / tankRect.width) * 100, 18, 82);
    const y = clamp(((avatarRect.top + avatarRect.height * 0.08 - tankRect.top) / tankRect.height) * 100, 8, 72);
    pop.style.setProperty('--pop-x', `${x.toFixed(2)}%`);
    pop.style.setProperty('--pop-y', `${y.toFixed(2)}%`);
  }
  pop.textContent = (text || '').replace(/\s+/g, ' ').trim();
  pop.dataset.tone = tone;
  pop.classList.toggle('show', Boolean(pop.textContent));
  clearTimeout(petSpeechTimer);
  petSpeechTimer = setTimeout(() => {
    pop.classList.remove('show');
    setTimeout(() => {
      if (!pop.classList.contains('show')) {
        pop.textContent = '';
        delete pop.dataset.tone;
      }
    }, 180);
  }, ms);
}

function addPetEventMessage(text, createdAt = Date.now()) {
  if (!petCare || !text) return;
  petCare.events.push({
    id: `event_${createdAt}_${Math.random().toString(36).slice(2, 7)}`,
    role: 'event',
    content: text,
    created_at: createdAt,
  });
  petCare.events = petCare.events.slice(-24);
  savePetCareState();
  renderMainChat();
}

function sprinkleFoodPellets() {
  const layer = $('foodLayer');
  const aquarium = document.querySelector('.aquarium');
  if (!layer || !aquarium) return [];
  layer.innerHTML = '';
  const rect = aquarium.getBoundingClientRect();
  return Array.from({ length: randInt(3, 6) }, (_, index) => {
    const x = rand(22, 78);
    const y = rand(30, 68);
    const el = document.createElement('span');
    el.className = 'food-pellet';
    el.style.setProperty('--x', `${x.toFixed(2)}%`);
    el.style.setProperty('--fall', `${Math.max(90, rect.height * ((y + 8) / 100)).toFixed(1)}px`);
    el.style.setProperty('--drift', `${rand(-18, 18).toFixed(1)}px`);
    el.style.setProperty('--drop-time', `${(1 + index * 0.08 + rand(0, 0.28)).toFixed(2)}s`);
    layer.appendChild(el);
    return { x, y, el };
  });
}

async function moveSeekyToPoint(x, y) {
  const orbit = document.querySelector('.pet-orbit');
  if (!orbit || !petCare) return;
  const nextX = clamp(x - 13, 3, 72);
  const nextY = clamp(y - 10, 18, 64);
  const face = nextX >= petCare.seeky_x ? 'scaleX(-1)' : 'scaleX(1)';
  petCare.seeky_x = nextX;
  petCare.seeky_y = nextY;
  orbit.style.setProperty('--seeky-x', `${nextX.toFixed(2)}%`);
  orbit.style.setProperty('--seeky-y', `${nextY.toFixed(2)}%`);
  orbit.style.setProperty('--seeky-face', face);
  orbit.style.setProperty('--seeky-tilt', `${rand(-5, 5).toFixed(1)}deg`);
  orbit.classList.add('seeking');
  await sleep(820);
}

function releaseSeeky() {
  const orbit = document.querySelector('.pet-orbit');
  if (!orbit) return;
  orbit.classList.remove('seeking');
  orbit.style.removeProperty('--seeky-x');
  orbit.style.removeProperty('--seeky-y');
  orbit.style.removeProperty('--seeky-face');
  orbit.style.removeProperty('--seeky-tilt');
}

function addWasteSoon() {
  if (!petCare || petCare.wastes.length >= 6) return;
  petCare.wastes.push(newWaste());
  petCare.cleanliness = clamp(petCare.cleanliness - 12);
  petCare.happiness = clamp(petCare.happiness - 4);
  petCare.next_waste_at = Math.max(Date.now() + HOUR_MS, nextWasteTime(Date.now()));
  savePetCareState();
  renderPetCare();
  showTemporaryPetBubble('＞﹏＜', 'pet', 3200);
}

async function feedSeeky() {
  if (!petCare || feeding) return;
  if (petCare.fullness > 94) {
    showTemporaryPetBubble('我已经圆滚滚啦，等一会儿再吃。', 'pet');
    showToast('Seeky 已经很饱');
    return;
  }

  feeding = true;
  $('feedBtn').disabled = true;
  refreshPetStatus();
  showTemporaryPetBubble('开饭！我游过去吃。', 'pet', 3600);

  const pellets = sprinkleFoodPellets();
  await sleep(760);
  for (const pellet of pellets) {
    await moveSeekyToPoint(pellet.x, pellet.y);
    pellet.el.classList.add('eaten');
    petCare.fullness = clamp(petCare.fullness + 7);
    petCare.happiness = clamp(petCare.happiness + 4);
    renderPetCare();
    await sleep(250);
    pellet.el.remove();
  }

  releaseSeeky();
  petCare.next_waste_at = Math.min(petCare.next_waste_at || Infinity, nextWasteTime(Date.now(), true));
  savePetCareState();
  renderPetCare();

  if (Math.random() < 0.38) {
    setTimeout(addWasteSoon, rand(2600, 7200));
  }
  globalThis.FamilyEventClient?.reportFamilyEvent?.('seeky_feed', seekyConfig.name).catch(() => {});
  showToast('Seeky 吃饱了一点');
  showTemporaryPetBubble('ヾ(≧▽≦*)o', 'pet');
  feeding = false;
  $('feedBtn').disabled = false;
  refreshPetStatus();
}

function cleanWaste(id, button) {
  if (!petCare || !id) return;
  const target = petCare.wastes.find(item => item.id === id);
  if (!target) return;
  petCare.wastes = petCare.wastes.filter(item => item.id !== id);
  petCare.cleanliness = clamp(petCare.cleanliness + 16);
  petCare.happiness = clamp(petCare.happiness + 3);
  savePetCareState();

  if (button) {
    button.classList.add('cleaning');
    const pop = document.createElement('span');
    pop.className = 'waste-pop';
    pop.style.setProperty('--x', `${target.x.toFixed(2)}%`);
    pop.style.setProperty('--bottom', `${target.bottom.toFixed(2)}%`);
    $('wasteLayer')?.appendChild(pop);
    setTimeout(() => {
      pop.remove();
      renderPetCare();
    }, 520);
  } else {
    renderPetCare();
  }
  globalThis.FamilyEventClient?.reportFamilyEvent?.('seeky_clean', seekyConfig.name).catch(() => {});
  showToast('清理好了');
  addPetEventMessage(`${clockText()} 你给 Seeky 清理了水族缸`);
  showTemporaryPetBubble(petCare.wastes.length ? '主人真好！' : '(❁´◡`❁)', 'pet');
}

function petSeeky() {
  if (!petCare) return;
  const orbit = document.querySelector('.pet-orbit');
  orbit?.classList.remove('petted');
  void orbit?.offsetWidth;
  orbit?.classList.add('petted');
  setTimeout(() => orbit?.classList.remove('petted'), 640);
  petCare.happiness = clamp(petCare.happiness + 8);
  petCare.fullness = clamp(petCare.fullness - 0.4);
  savePetCareState();
  renderPetCare();
  const lines = ['我贴过来啦~', '收到摸摸信号！', '我绕个圈给你看！', ' .｡. o(≧▽≦)o .｡.:*☆', '被摸摸了！', '再摸一下!~'];
  showTemporaryPetBubble(lines[randInt(0, lines.length - 1)], 'pet');
}

function startPetCareClock() {
  if (petCareTimer) clearInterval(petCareTimer);
  petCareTimer = setInterval(() => {
    applyPetCareTime(Date.now(), false);
    savePetCareState();
    renderPetCare();
  }, 60 * 1000);
  window.addEventListener('beforeunload', savePetCareState);
}

function goHome() {
  if (window.parent !== window) {
    try {
      if (typeof window.parent.navigateToHome === 'function') {
        window.parent.navigateToHome();
        return;
      }
      if (typeof window.parent.openSubPage === 'function') {
        window.parent.openSubPage('/');
        return;
      }
    } catch {}
  }
  window.location.href = '/';
}

function fillModelSelect(select, value) {
  select.innerHTML = models.map(model => {
    const label = `${model.key} · ${model.provider}`;
    return `<option value="${esc(model.key)}">${esc(label)}</option>`;
  }).join('');
  if (models.some(model => model.key === value)) select.value = value;
}

function applyConfigToUi() {
  const name = seekyConfig.name || 'Seeky';
  $('brandName').textContent = name;
  $('petName').textContent = name;
  $('historyHint').textContent = `${name} 的独立长期窗口`;
  $('nameInput').value = name;
  $('personaInput').value = seekyConfig.persona || '';
  $('contextInput').value = seekyConfig.context_limit || 40;
  fillModelSelect($('configModelSelect'), seekyConfig.model);
  renderHistory();
  updatePetBubbleFromHistory();
}

function petName() {
  return seekyConfig.name || 'Seeky';
}

function seekyMessageName(role) {
  if (role === 'user') return '你';
  if (role === 'aion') return 'Aion';
  if (role === 'connor') return 'Connor';
  return petName();
}

function normalizeSeekyMessageRole(msg) {
  const role = msg?.role || 'assistant';
  if (role !== 'user') return role;
  const content = (msg?.content || '').trim();
  if (/^\[(aion|aions?|ai\s*on|AIon)[^\]]*\]/i.test(content)) return 'aion';
  if (/^\[(connor)[^\]]*\]/i.test(content)) return 'connor';
  return role;
}

function seekyMessageSide(role) {
  return role === 'user' ? 'user' : 'seeky';
}

function mainChatItems() {
  const historyItems = messages
    .filter(msg => (msg.content || '').trim())
    .map(msg => ({
      id: msg.id,
      role: normalizeSeekyMessageRole(msg),
      content: msg.content,
      created_at: Number(msg.created_at || 0) * 1000,
    }));
  const eventItems = (petCare?.events || []).map(item => ({
    id: item.id,
    role: 'event',
    content: item.content,
    created_at: Number(item.created_at || 0),
  }));
  const items = [...historyItems, ...eventItems].sort((a, b) => a.created_at - b.created_at);
  const latest = items.slice(-4);
  if (livePetMessage?.content) {
    const last = latest[latest.length - 1];
    if (!last || last.role !== livePetMessage.role || last.content !== livePetMessage.content) {
      latest.push(livePetMessage);
    }
  }
  if (!latest.length) {
    latest.push({
      id: 'welcome',
      role: 'assistant',
      content: '我在这里，等你叫我。',
      created_at: Date.now(),
    });
  }
  return latest.slice(-5);
}

function renderMainChat() {
  const bubble = $('speechBubble');
  if (!bubble) return;
  bubble.innerHTML = `
    <div class="pet-chat-log">
      ${mainChatItems().map(renderMainChatItem).join('')}
    </div>`;
  requestAnimationFrame(() => {
    bubble.scrollTop = bubble.scrollHeight;
  });
}

function renderMainChatItem(item) {
  if (item.role === 'event') {
    return `<div class="pet-chat-row event"><div class="pet-event-chip">${esc(item.content)}</div></div>`;
  }
  const isUser = item.role === 'user';
  const name = isUser ? '你' : petName();
  return `
    <div class="pet-chat-row ${isUser ? 'user' : 'seeky'}">
      <div class="pet-chat-speaker">${esc(name)}</div>
      <div class="pet-chat-bubble">${esc(item.content)}</div>
    </div>`;
}

function showPetBubble(text, tone = 'normal') {
  const raw = (text || `我在这里，等你叫我。`).replace(/\s+/g, ' ').trim();
  const role = tone === 'user' ? 'user' : (tone === 'event' ? 'event' : 'assistant');
  livePetMessage = {
    id: `live_${Date.now()}`,
    role,
    content: raw || `我在这里，等你叫我。`,
    created_at: Date.now() + 1,
  };
  const bubble = $('speechBubble');
  if (bubble) bubble.dataset.tone = tone;
  renderMainChat();
}

function updatePetBubbleFromHistory() {
  const lastAssistant = [...messages].reverse().find(msg => msg.role === 'assistant' && msg.content.trim());
  if (lastAssistant) {
    livePetMessage = null;
  } else {
    livePetMessage = null;
  }
  renderMainChat();
}

function renderHistory() {
  const box = $('historyList');
  if (!messages.length) {
    box.innerHTML = `<div class="history-empty">还没有聊天记录。</div>`;
    return;
  }
  box.innerHTML = messages.map(renderHistoryMessage).join('');
  scrollHistoryToBottom();
}

function renderHistoryMessage(message) {
  const isUser = message.role === 'user';
  const name = isUser ? '你' : petName();
  return `
    <div class="history-msg ${isUser ? 'user' : 'assistant'}" data-msg-id="${esc(message.id)}">
      <div class="history-name">${esc(name)}</div>
      <div class="history-bubble">${esc(message.content)}</div>
    </div>`;
}

function renderMainChatItem(item) {
  if (item.role === 'event') {
    return `<div class="pet-chat-row event"><div class="pet-event-chip">${esc(item.content)}</div></div>`;
  }
  const isUser = item.role === 'user';
  const isActor = item.role === 'aion' || item.role === 'connor';
  const name = seekyMessageName(item.role);
  const side = seekyMessageSide(item.role);
  return `
    <div class="pet-chat-row ${side} ${isActor ? 'actor' : ''}">
      <div class="pet-chat-speaker">${esc(name)}</div>
      <div class="pet-chat-bubble">${esc(item.content)}</div>
    </div>`;
}

function renderHistoryMessage(message) {
  const role = normalizeSeekyMessageRole(message);
  const isUser = role === 'user';
  const name = seekyMessageName(role);
  const actorCls = isUser ? 'user' : `assistant ${role === 'aion' || role === 'connor' ? 'actor' : ''}`;
  return `
    <div class="history-msg ${actorCls}" data-msg-id="${esc(message.id)}">
      <div class="history-name">${esc(name)}</div>
      <div class="history-bubble">${esc(message.content)}</div>
    </div>`;
}

function scrollHistoryToBottom() {
  const box = $('historyList');
  requestAnimationFrame(() => {
    box.scrollTop = box.scrollHeight;
  });
}

function appendMessage(message) {
  messages.push(message);
  renderHistory();
  renderMainChat();
}

function updateMessageContent(id, content) {
  const target = messages.find(msg => msg.id === id);
  if (target) target.content = content;
  const row = document.querySelector(`[data-msg-id="${CSS.escape(id)}"]`);
  if (row) {
    const bubble = row.querySelector('.history-bubble');
    if (bubble) bubble.textContent = content;
  }
  scrollHistoryToBottom();
  renderMainChat();
}

function setStatus(text) {
  if (text) {
    $('petStatus').textContent = 'THINKING';
  } else {
    refreshPetStatus();
  }
}

function setSending(value) {
  sending = value;
  $('sendBtn').disabled = value;
  $('messageInput').disabled = value;
}

function setReviewBusy(value, label = '') {
  reviewBusy = value;
  $('createReviewBtn').disabled = value;
  $('saveReviewBtn').disabled = value;
  $('discardReviewBtn').disabled = value;
  $('applyReviewBtn').disabled = value;
  if (label) $('reviewStatus').textContent = label;
}

async function loadInitialData() {
  const [modelData, config, msgData] = await Promise.all([
    api('GET', '/api/models'),
    api('GET', '/api/seeky/config'),
    api('GET', '/api/seeky/messages?limit=300'),
  ]);
  models = modelData || [];
  seekyConfig = config;
  messages = msgData.messages || [];
  applyConfigToUi();
}

async function saveConfig(showSaved = true) {
  const next = {
    name: $('nameInput').value.trim() || 'Seeky',
    persona: $('personaInput').value.trim(),
    model: $('configModelSelect').value || seekyConfig.model,
    context_limit: Number($('contextInput').value || 40),
  };
  const saved = await api('PUT', '/api/seeky/config', next);
  seekyConfig = saved;
  applyConfigToUi();
  if (showSaved) showToast('Seeky 设置已保存');
}

async function syncModelFromConfig() {
  seekyConfig.model = $('configModelSelect').value;
  try {
    await saveConfig(false);
    showToast('模型已保存');
  } catch (err) {
    showToast(`保存失败：${err.message}`);
  }
}

function autoSizeInput() {
  const input = $('messageInput');
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 112)}px`;
}

async function sendMessage(event) {
  event.preventDefault();
  if (sending) return;
  const input = $('messageInput');
  const text = input.value.trim();
  if (!text) return;

  input.value = '';
  autoSizeInput();
  setSending(true);
  setStatus(`${petName()} 正在接收信号...`);

  appendMessage({
    id: `local_${Date.now()}_u`,
    role: 'user',
    content: text,
    created_at: Date.now() / 1000,
  });

  let assistantId = '';
  let assistantText = '';
  try {
    const res = await fetch('/api/seeky/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: text }),
    });
    if (!res.ok || !res.body) throw new Error(`发送失败：${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split('\n\n');
      buffer = parts.pop() || '';
      for (const part of parts) {
        const line = part.split('\n').find(item => item.startsWith('data: '));
        if (!line) continue;
        handleStreamEvent(JSON.parse(line.slice(6)), {
          get assistantId() { return assistantId; },
          set assistantId(value) { assistantId = value; },
          get assistantText() { return assistantText; },
          set assistantText(value) { assistantText = value; },
        });
      }
    }
  } catch (err) {
    showToast(err.message || '发送失败');
    showPetBubble(`信号好像断了一下。`);
    setStatus('');
  } finally {
    setSending(false);
    setStatus('');
    input.focus();
  }
}

function handleStreamEvent(data, streamState) {
  if (data.type === 'assistant_start') {
    streamState.assistantId = data.id;
    streamState.assistantText = '';
    appendMessage({
      id: data.id,
      role: 'assistant',
      content: '',
      created_at: Date.now() / 1000,
    });
    showPetBubble(`${petName()} 正在想...`);
    setStatus(`${petName()} 正在思考...`);
    return;
  }
  if (data.type === 'status') {
    showPetBubble(data.text || '模型处理中...');
    setStatus(data.text || '模型处理中...');
    return;
  }
  if (data.type === 'chunk') {
    streamState.assistantText += data.content || '';
    updateMessageContent(streamState.assistantId, streamState.assistantText);
    showPetBubble(streamState.assistantText);
    return;
  }
  if (data.type === 'assistant_done') {
    const index = messages.findIndex(msg => msg.id === streamState.assistantId);
    if (index >= 0) messages[index] = data.message;
    const row = document.querySelector(`[data-msg-id="${CSS.escape(streamState.assistantId)}"]`);
    if (row) row.dataset.msgId = data.message.id;
    updateMessageContent(data.message.id, data.message.content);
    showPetBubble(data.message.content);
    setStatus('');
    return;
  }
  if (data.type === 'error') {
    showToast(data.content || '模型回复失败');
    showPetBubble(`我刚刚没接稳信号。`);
    setStatus('');
  }
}

async function clearMessages() {
  if (!confirm('清空 Seeky 的独立聊天记录？')) return;
  try {
    await api('POST', '/api/seeky/clear');
    messages = [];
    renderHistory();
    showPetBubble(`我在这里，等你叫我。`);
    showToast('已清空');
  } catch (err) {
    showToast(`清空失败：${err.message}`);
  }
}

function actionLabel(action) {
  return { keep: '不动', edit: '改写', delete: '删除', create: '写入', discard: '丢弃' }[action] || '不动';
}

function isSourceReview() {
  return memoryReview?.mode === 'source_day';
}

function isCreateStyleReview() {
  return ['source_day', 'memory_compress'].includes(memoryReview?.mode);
}

function reviewModeName(mode) {
  return mode === 'memory_compress' ? '长期压缩' : '原文整理';
}

function reviewModeFor(review) {
  return review?.mode === 'memory_compress' ? 'compress' : 'source';
}

function activeMemoryReview() {
  if (!memoryReview) return null;
  return reviewModeFor(memoryReview) === reviewMode ? memoryReview : null;
}

function parseJsonList(text) {
  if (!text) return [];
  if (Array.isArray(text)) return text;
  try {
    const parsed = JSON.parse(text);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return String(text).split(/[,，、\n]/).map(s => s.trim()).filter(Boolean);
  }
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(Number(ts) * 1000);
  if (Number.isNaN(d.getTime())) return '';
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function defaultReviewDate() {
  const d = new Date(Date.now() - 8 * 24 * 60 * 60 * 1000);
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function setDateRange(startId, endId, daysAgoEnd = 8, lengthDays = 1) {
  const end = new Date(Date.now() - daysAgoEnd * 24 * 60 * 60 * 1000);
  const start = new Date(end.getTime() - (lengthDays - 1) * 24 * 60 * 60 * 1000);
  const pad = n => String(n).padStart(2, '0');
  const fmt = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  $(startId).value = fmt(start);
  $(endId).value = fmt(end);
}

function setReviewMode(mode) {
  reviewMode = 'compress';
  $('compressFields')?.classList.remove('hidden');
  document.querySelectorAll('.review-mode-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.mode === reviewMode);
  });
  $('createReviewBtn').textContent = '生成压缩草案';
  renderMemoryReview();
}
function renderMemoryReview() {
  const list = $('reviewList');
  if (!activeMemoryReview()) {
    $('reviewStatus').textContent = '还没有长期压缩草案。生成草案不会修改主记忆库。';
    list.innerHTML = `<div class="review-empty">点“生成压缩草案”，Seeky 会先从旧摘要里提炼长期重点，等你确认后才会应用。</div>`;
    $('saveReviewBtn').disabled = true;
    $('discardReviewBtn').disabled = true;
    $('applyReviewBtn').disabled = true;
    return;
  }

  const items = memoryReview.items || [];
  const replaceItems = memoryReview.replace_items || [];
  const counts = items.reduce((acc, item) => {
    const action = item.final_action || (isCreateStyleReview() ? 'create' : 'keep');
    acc[action] = (acc[action] || 0) + 1;
    return acc;
  }, {});
  const statusLabel = {
    draft: '草案，可修改',
    processing: '生成中',
    failed: '生成失败',
    applied: '已应用',
    discarded: '已废弃',
  }[memoryReview.status] || memoryReview.status;
  const errorText = memoryReview.error ? `；部分批次提示：${memoryReview.error}` : '';
  if (isCreateStyleReview()) {
    const labelName = memoryReview.mode === 'memory_compress' ? '压缩窗' : '原文窗';
    const label = memoryReview.source_label ? `${labelName}：${memoryReview.source_label}。` : '';
    const oldCount = replaceItems.length || memoryReview.delete_count || 0;
    const replaceText = memoryReview.mode === 'memory_compress' ? `应用时会替换该时间窗内旧摘要 ${oldCount} 条` : `应用时会替换该时间窗内旧记忆 ${oldCount} 条`;
    $('reviewStatus').textContent =
      `${statusLabel}。${label}${reviewModeName(memoryReview.mode)}候选 ${items.length} 条：写入 ${counts.create || 0}，丢弃 ${counts.discard || 0}；${replaceText}${errorText}`;
  } else {
    $('reviewStatus').textContent =
      `${statusLabel}。共 ${items.length} 条：不动 ${counts.keep || 0}，改写 ${counts.edit || 0}，删除 ${counts.delete || 0}${errorText}`;
  }

  if (!items.length && !replaceItems.length) {
    if (memoryReview.status === 'processing') {
      list.innerHTML = `<div class="review-empty">Seeky 正在压缩旧摘要，这里完成后会自动刷新。</div>`;
    } else if (memoryReview.status === 'failed') {
      list.innerHTML = `<div class="review-empty">这次生成失败了，错误信息在上面的状态里。</div>`;
    } else {
      list.innerHTML = `<div class="review-empty">这个时间窗没有生成可写入的记忆。</div>`;
    }
  } else {
    const createItems = items.filter(item => (item.final_action || item.suggested_action || 'create') === 'create');
    const discardItems = items.filter(item => (item.final_action || item.suggested_action || 'create') === 'discard');
    list.innerHTML = `
      <div class="review-section">
        <div class="review-section-head">
          <span>将写入的新长期记忆</span>
          <b>${createItems.length}</b>
        </div>
        ${createItems.length ? createItems.map(renderMemoryReviewItem).join('') : '<div class="review-empty small">没有新记忆会写入。</div>'}
      </div>
      <div class="review-section">
        <div class="review-section-head danger">
          <span>确认后会替换/删除的旧摘要</span>
          <b>${replaceItems.length}</b>
        </div>
        ${replaceItems.length ? replaceItems.map(renderReplaceMemoryItem).join('') : '<div class="review-empty small">没有旧摘要会被替换。</div>'}
      </div>
      ${discardItems.length ? `
        <div class="review-section">
          <div class="review-section-head muted">
            <span>草案中丢弃的新候选</span>
            <b>${discardItems.length}</b>
          </div>
          ${discardItems.map(renderMemoryReviewItem).join('')}
        </div>` : ''}`;
  }

  const editable = memoryReview.status === 'draft' && items.length > 0 && !reviewBusy;
  const discardable = ['draft', 'processing', 'failed'].includes(memoryReview.status) && !reviewBusy;
  $('saveReviewBtn').disabled = !editable;
  $('discardReviewBtn').disabled = !discardable;
  $('applyReviewBtn').disabled = !editable;
}

function renderReplaceMemoryItem(item, index) {
  let keywords = [];
  try {
    keywords = item.keywords ? JSON.parse(item.keywords) : [];
  } catch {
    keywords = String(item.keywords || '').split(/[,，、\n]/).map(s => s.trim()).filter(Boolean);
  }
  const kw = keywords.length ? `<div class="review-old-keywords">${keywords.map(k => `<span>${esc(k)}</span>`).join('')}</div>` : '';
  const importance = item.importance == null ? '' : `<span>重要度 ${esc(Number(item.importance).toFixed(2))}</span>`;
  return `
    <div class="review-item review-old-item">
      <div class="review-seq">
        <span>D${String(index + 1).padStart(3, '0')}</span>
        <em>将删除</em>
      </div>
      <div class="review-content">
        <div class="review-original">${esc(item.content)}</div>
        <div class="review-source-meta">
          <span>${esc(fmtTime(item.source_start_ts || item.created_at))}</span>
          <span>${esc(item.type || 'memory')}</span>
          ${importance}
        </div>
        ${kw}
      </div>
    </div>`;
}

function renderMemoryReviewItem(item) {
  const sourceMode = isCreateStyleReview();
  const action = item.final_action || item.suggested_action || (sourceMode ? 'create' : 'keep');
  const editable = memoryReview?.status === 'draft';
  const editContent = item.final_content || item.suggested_content || item.original_content || '';
  const suggested = item.suggested_action || (sourceMode ? 'create' : 'keep');
  const textareaStyle = (action === 'edit' || action === 'create') ? '' : ' style="display:none"';
  const sourceQuotes = parseJsonList(item.source_quotes);
  const sourceIds = parseJsonList(item.source_message_ids);
  const sourceMeta = sourceMode ? `
        <div class="review-source-meta">
          <span>${esc(fmtTime(item.memory_time || item.source_start_ts))}</span>
          <span>${esc(sourceIds.length)} 条来源证据</span>
        </div>
        ${sourceQuotes.length ? `<div class="review-quotes">${sourceQuotes.map(q => `<div>${esc(q)}</div>`).join('')}</div>` : ''}` : '';
  const options = sourceMode
    ? `<option value="create"${action === 'create' ? ' selected' : ''}>写入</option>
       <option value="discard"${action === 'discard' ? ' selected' : ''}>丢弃</option>`
    : `<option value="keep"${action === 'keep' ? ' selected' : ''}>不动</option>
       <option value="edit"${action === 'edit' ? ' selected' : ''}>改写</option>
       <option value="delete"${action === 'delete' ? ' selected' : ''}>删除</option>`;
  return `
    <div class="review-item" data-seq="${esc(item.seq)}" data-action="${esc(action)}">
      <div class="review-seq">
        <span>${esc(item.seq)}</span>
        <select class="review-action" ${editable ? '' : 'disabled'}>
          ${options}
        </select>
      </div>
      <div class="review-content">
        <div class="review-original">${esc(item.original_content)}</div>
        ${sourceMeta}
        <div class="review-reason">
          <span class="review-badge">Seeky 建议：${esc(actionLabel(suggested))}</span>
          ${esc(item.reason || '无理由')}
        </div>
        <textarea class="review-edit" ${editable ? '' : 'disabled'}${textareaStyle}>${esc(editContent)}</textarea>
      </div>
    </div>`;
}

function syncReviewFromDom() {
  if (!memoryReview) return;
  const bySeq = new Map(memoryReview.items.map(item => [item.seq, item]));
  document.querySelectorAll('.review-item').forEach(row => {
    const seq = row.dataset.seq;
    const item = bySeq.get(seq);
    if (!item) return;
    const action = row.querySelector('.review-action')?.value || 'keep';
    const textarea = row.querySelector('.review-edit');
    item.final_action = action;
    if (action === 'edit' || action === 'create') {
      item.final_content = textarea?.value.trim() || item.original_content;
    } else if (action === 'keep') {
      item.final_content = item.original_content;
    } else {
      item.final_content = '';
    }
  });
}

async function loadLatestMemoryReview() {
  try {
    const data = await api('GET', '/api/seeky/memory-review/latest');
    memoryReview = data.review || null;
    renderMemoryReview();
    maybePollMemoryReview();
  } catch (err) {
    $('reviewStatus').textContent = `读取草案失败：${err.message}`;
  }
}

function stopReviewPoll() {
  if (reviewPollTimer) {
    clearTimeout(reviewPollTimer);
    reviewPollTimer = null;
  }
}

function maybePollMemoryReview() {
  stopReviewPoll();
  if (!memoryReview || memoryReview.status !== 'processing') return;
  reviewPollTimer = setTimeout(async () => {
    try {
      const data = await api('GET', `/api/seeky/memory-review/${memoryReview.id}`);
      memoryReview = data || memoryReview;
      renderMemoryReview();
      if (memoryReview.status === 'processing') {
        maybePollMemoryReview();
      } else if (memoryReview.status === 'draft') {
        showToast('整理草案已生成');
        showPetBubble('草案整理好了，你先检查。');
      } else if (memoryReview.status === 'failed') {
        showToast('草案生成失败');
        showPetBubble('这次整理没跑完，错误信息在草案状态里。');
      }
    } catch (err) {
      $('reviewStatus').textContent = `刷新草案失败：${err.message}`;
      maybePollMemoryReview();
    }
  }, 2500);
}

async function createMemoryReviewDraft() {
  if (reviewBusy) return;
  const compressStart = $('compressStartDateInput').value || defaultReviewDate();
  const compressEnd = $('compressEndDateInput').value || compressStart;
  const payload = {
    mode: 'compress',
    start_date: compressStart,
    end_date: compressEnd,
    compress_source: 'summary',
    compress_strength: $('compressStrengthSelect').value,
  };
  const label = `${compressStart} 到 ${compressEnd} 的长期压缩草案`;
  if (!confirm(`生成 ${label}？这一步只写入 Seeky 草案，不会修改主记忆库。`)) return;
  setReviewBusy(true, 'Seeky 正在压缩旧摘要...');
  showPetBubble('我去压缩旧摘要，只做草案。');
  try {
    memoryReview = await api('POST', '/api/seeky/memory-review/draft', payload);
    renderMemoryReview();
    maybePollMemoryReview();
    showToast('已开始生成草案');
    showPetBubble('我开始压缩这段旧记忆了，完成后会显示在这里。');
  } catch (err) {
    $('reviewStatus').textContent = `生成失败：${err.message}`;
    showToast('草案生成失败');
  } finally {
    setReviewBusy(false);
    renderMemoryReview();
  }
}

async function saveReviewEdits(silent = false) {
  if (!memoryReview || memoryReview.status !== 'draft') return memoryReview;
  syncReviewFromDom();
  const payload = {
    items: memoryReview.items.map(item => ({
      seq: item.seq,
      final_action: item.final_action || 'keep',
      final_content: item.final_content || '',
    })),
  };
  memoryReview = await api('PUT', `/api/seeky/memory-review/${memoryReview.id}`, payload);
  renderMemoryReview();
  if (!silent) showToast('草案修改已保存');
  return memoryReview;
}

async function discardMemoryReview() {
  if (!memoryReview) return;
  if (!['draft', 'processing', 'failed'].includes(memoryReview.status)) {
    showToast('这份草案已经应用或废弃，不能再废弃');
    return;
  }
  if (!confirm('废弃这次整理草案？主记忆库不会有任何变化。')) return;
  try {
    stopReviewPoll();
    memoryReview = await api('POST', `/api/seeky/memory-review/${memoryReview.id}/discard`);
    renderMemoryReview();
    showToast('草案已废弃');
  } catch (err) {
    showToast(`废弃失败：${err.message}`);
  }
}

async function applyMemoryReview() {
  if (!activeMemoryReview() || memoryReview.status !== 'draft') {
    showToast('褰撳墠娌℃湁鍙簲鐢ㄧ殑鑽夋');
    renderMemoryReview();
    return;
  }
  syncReviewFromDom();
  let warning = '';
  if (isSourceReview()) {
    const createCount = memoryReview.items.filter(item => item.final_action === 'create').length;
    const discardCount = memoryReview.items.filter(item => item.final_action === 'discard').length;
    const oldCount = memoryReview.delete_count || 0;
    warning = `确认应用这次整理？这一步会删除该原文时间窗内旧记忆 ${oldCount} 条，并写入你保留的 ${createCount} 条新记忆，丢弃 ${discardCount} 条候选。`;
  } else if (memoryReview.mode === 'memory_compress') {
    const createCount = memoryReview.items.filter(item => item.final_action === 'create').length;
    const discardCount = memoryReview.items.filter(item => item.final_action === 'discard').length;
    const oldCount = memoryReview.delete_count || 0;
    warning = `确认应用这次压缩？这一步会删除该时间窗内旧摘要 ${oldCount} 条，并写入你保留的 ${createCount} 条压缩记忆，丢弃 ${discardCount} 条候选。`;
  } else {
    const deleteCount = memoryReview.items.filter(item => item.final_action === 'delete').length;
    const editCount = memoryReview.items.filter(item => item.final_action === 'edit').length;
    warning = `确认应用这次整理？这一步会真正改写 ${editCount} 条、删除 ${deleteCount} 条主记忆。Seeky 会保留应用日志，但请先确认你已经检查过。`;
  }
  if (!confirm(warning)) return;
  setReviewBusy(true, '正在先保存你的修改，然后应用到主记忆库...');
  try {
    await saveReviewEdits(true);
    memoryReview = await api('POST', `/api/seeky/memory-review/${memoryReview.id}/apply`);
    renderMemoryReview();
    const result = memoryReview.apply_result || {};
    const skipped = result.skipped?.length ? `，跳过 ${result.skipped.length} 条` : '';
    showToast(`已应用：改写 ${result.changed || 0}，删除 ${result.deleted || 0}${skipped}`);
    showPetBubble('我已经按你确认的草案整理好了。');
  } catch (err) {
    showToast(`应用失败：${err.message}`);
    $('reviewStatus').textContent = `应用失败：${err.message}`;
  } finally {
    setReviewBusy(false);
    renderMemoryReview();
  }
}

function closeDrawers() {
  $('sideDrawer').classList.remove('open');
  $('historyPanel').classList.remove('open');
  $('configPanel').classList.remove('open');
  $('memoryReviewPanel').classList.remove('open');
  $('drawerBackdrop').classList.remove('show');
}

function openDrawer(id) {
  closeDrawers();
  $(id).classList.add('open');
  $('drawerBackdrop').classList.add('show');
  if (id === 'historyPanel') renderHistory();
  if (id === 'memoryReviewPanel') loadLatestMemoryReview();
}

function bindEvents() {
  $('menuBtn').addEventListener('click', () => openDrawer('sideDrawer'));
  $('closeMenuBtn').addEventListener('click', closeDrawers);
  $('historyBtn').addEventListener('click', () => openDrawer('historyPanel'));
  $('closeHistoryBtn').addEventListener('click', closeDrawers);
  $('settingsBtn').addEventListener('click', () => openDrawer('configPanel'));
  $('drawerSettingsBtn').addEventListener('click', () => openDrawer('configPanel'));
  $('closeSettingsBtn').addEventListener('click', closeDrawers);
  $('drawerBackdrop').addEventListener('click', closeDrawers);
  $('homeBtn').addEventListener('click', goHome);
  $('saveConfigBtn').addEventListener('click', () => saveConfig(true).catch(err => showToast(`保存失败：${err.message}`)));
  $('clearBtn').addEventListener('click', clearMessages);
  $('closeMemoryReviewBtn').addEventListener('click', closeDrawers);
  $('createReviewBtn').addEventListener('click', createMemoryReviewDraft);
  $('saveReviewBtn').addEventListener('click', () => saveReviewEdits(false).catch(err => showToast(`保存失败：${err.message}`)));
  $('discardReviewBtn').addEventListener('click', discardMemoryReview);
  $('applyReviewBtn').addEventListener('click', applyMemoryReview);
  $('feedBtn').addEventListener('click', feedSeeky);
  $('petAvatar').addEventListener('click', petSeeky);
  $('wasteLayer').addEventListener('click', event => {
    const button = event.target.closest('.waste-clump');
    if (button) cleanWaste(button.dataset.wasteId, button);
  });
  $('reviewList').addEventListener('change', event => {
    if (!event.target.classList.contains('review-action')) return;
    const row = event.target.closest('.review-item');
    if (!row) return;
    const action = event.target.value;
    row.dataset.action = action;
    const textarea = row.querySelector('.review-edit');
    if (textarea) textarea.style.display = (action === 'edit' || action === 'create') ? '' : 'none';
    syncReviewFromDom();
    renderMemoryReview();
  });
  $('reviewList').addEventListener('input', event => {
    if (event.target.classList.contains('review-edit')) syncReviewFromDom();
  });
  $('composer').addEventListener('submit', sendMessage);
  $('messageInput').addEventListener('input', autoSizeInput);
  $('messageInput').addEventListener('keydown', event => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) sendMessage(event);
  });
  $('configModelSelect').addEventListener('change', syncModelFromConfig);
  document.querySelectorAll('.skill-btn').forEach(button => {
    button.addEventListener('click', () => {
      if (button.disabled || button.classList.contains('archived')) {
        showToast(`${button.dataset.skill} 暂时封存`);
        return;
      }
      if (button.dataset.skill === '记忆整理') {
        openDrawer('memoryReviewPanel');
      } else {
        showToast(`${button.dataset.skill} 之后接上`);
      }
    });
  });
}

bindEvents();
setDateRange('compressStartDateInput', 'compressEndDateInput', 190, 30);
setReviewMode('compress');
loadPetCareState();
startPetCareClock();
loadInitialData().then(() => {
  renderPetCare();
}).catch(err => {
  showToast(`Seeky 启动失败：${err.message}`);
  console.error(err);
});

(() => {
  const buttons = Array.from(document.querySelectorAll('.switcher button'));
  const preference = 'aion_usage_source';
  let source = localStorage.getItem(preference) === 'codex' ? 'codex' : 'agy';
  let report = {};
  let readingCache = true;
  const querying = new Set();
  const localTime = new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
  });
  const groupNames = { 'Gemini Models': 'Gemini', 'Claude and GPT models': 'Claude 和 GPT' };

  function element(tag, className, text) {
    const node = document.createElement(tag);
    node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function recovery(bucket) {
    const line = element('div', 'recovery');
    const reset = Date.parse(bucket.reset_at);
    if (bucket.remaining === 100 || !Number.isFinite(reset)) {
      line.textContent = bucket.remaining === 100 ? '额度可用' : '暂未提供恢复时间';
    } else {
      const minutes = Math.ceil((reset - Date.now()) / 60000);
      const hours = Math.floor(minutes / 60);
      const time = element('time', '', localTime.format(reset) + ' 恢复');
      time.dateTime = bucket.reset_at;
      line.append(time, element('span', '', minutes <= 0 ? '等待下次查询' :
        '还剩 ' + (hours ? hours + ' 小时 ' : '') + (minutes % 60) + ' 分钟'));
    }
    return line;
  }

  function quotaRow(bucket) {
    const label = ({ weekly: '每周额度', '5h': '5 小时额度' })[bucket.window] || bucket.window;
    const value = source === 'agy' ? bucket.remaining.toFixed(2) : String(bucket.remaining);
    const row = element('div', 'quota');
    const readout = element('div', 'readout');
    const number = element('strong', 'number', value);
    number.append(element('small', '', '%'));
    readout.append(element('span', '', label + '剩余'), number);
    const bar = element('div', 'meter');
    bar.setAttribute('aria-hidden', 'true');
    const fill = element('i', '');
    fill.style.width = bucket.remaining + '%';
    bar.append(fill);
    row.append(readout, bar, recovery(bucket));
    return row;
  }

  function render() {
    for (const btn of buttons) {
      const key = btn.dataset.source;
      const name = report?.[key]?.display_name;
      if (name) btn.textContent = name;
      btn.setAttribute('aria-pressed', String(key === source));
    }
    const snapshot = report?.[source];
    const groups = snapshot?.groups || [];
    const isAgy = source === 'agy';
    const selected = isAgy ? groups.find(g => /\bgemini\s*4\s+argon\b/i.test(g.name)) : groups[0];
    $('quotaTitle').textContent = isAgy ? 'Gemini 4 Argon' : 'Codex CLI';
    $('quotas').replaceChildren(...(selected?.buckets || []).map(quotaRow));
    const others = isAgy ? groups.filter(g => g !== selected) : [];
    $('more').hidden = !others.length;
    $('otherGroups').replaceChildren(...others.map(group => {
      const section = document.createElement('section');
      section.append(element('h3', '', groupNames[group.name] || group.name), ...group.buckets.map(quotaRow));
      return section;
    }));
    $('updatedAt').textContent = snapshot?.updated_at ? '上次更新 ' + localTime.format(new Date(snapshot.updated_at)) : '';
    const busy = querying.has(source);
    $('refreshUsage').disabled = readingCache || busy;
    $('refreshUsage').textContent = busy ? '查询中…' : '查询用量';
    let status = '';
    if (readingCache) status = '正在读取上次结果…';
    else if (busy) status = '正在查询额度…';
    else if (snapshot?.error) status = snapshot.error + (snapshot.updated_at ? '，显示上次成功结果。' : '');
    else if (!snapshot?.updated_at) status = '尚未查询';
    else if (!selected) status = '暂未返回此额度';
    if ($('usageStatus').textContent !== status) $('usageStatus').textContent = status;
  }

  async function readCache() {
    try {
      report = await api('GET', '/api/usage');
    } catch {
      for (const btn of buttons) report[btn.dataset.source] = { error: '暂时连不上小家' };
    } finally {
      readingCache = false;
      render();
    }
  }

  async function refresh() {
    const target = source;
    if (readingCache || querying.has(target)) return;
    querying.add(target);
    render();
    try {
      const snapshot = await api('POST', '/api/usage/' + target + '/refresh', null, { timeoutMs: 60000 });
      report[target] = { ...report[target], ...snapshot };
    } catch {
      report[target] = { ...report[target], error: '用量查询未完成，请稍后再试' };
    } finally {
      querying.delete(target);
      render();
    }
  }
  for (const btn of buttons) {
    btn.addEventListener('click', () => {
      source = btn.dataset.source;
      localStorage.setItem(preference, source);
      render();
    });
  }
  $('refreshUsage').addEventListener('click', refresh);
  function updateCountdowns() {
    if (!document.hidden && report[source]?.groups?.length) render();
  }
  document.addEventListener('visibilitychange', updateCountdowns);
  setInterval(updateCountdowns, 60000);
  render();
  readCache();
})();

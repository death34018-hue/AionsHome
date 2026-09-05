(() => {
  let dialog;
  let options;
  let saving = false;
  let initialValues = '';
  let viewportTargets = [];
  const field = name => dialog.querySelector(`[name="${name}"]`);

  function values() {
    return { content: field('content').value, kind: field('kind').value, keywords: field('keywords').value, importance: field('importance').value };
  }

  function syncViewport() {
    const viewport = window.visualViewport;
    let top = viewport?.offsetTop || 0;
    let bottom = top + (viewport?.height || window.innerHeight);
    // The home screen embeds subpages; its keyboard can shrink the parent only.
    try {
      if (window.parent !== window && window.frameElement) {
        const parentViewport = window.parent.visualViewport;
        const frameTop = window.frameElement.getBoundingClientRect().top;
        const parentTop = parentViewport?.offsetTop || 0;
        top = Math.max(top, parentTop - frameTop);
        bottom = Math.min(bottom, parentTop + (parentViewport?.height || window.parent.innerHeight) - frameTop);
      }
    } catch (_) { /* Cross-origin embeddings use their own viewport. */ }
    dialog.style.setProperty('--memory-editor-top', `${top}px`);
    dialog.style.setProperty('--memory-editor-height', `${Math.max(0, bottom - top)}px`);
  }

  function cancel() {
    if (saving) return;
    if (JSON.stringify(values()) !== initialValues && !confirm('修改还没有保存，确定放弃修改吗？')) return;
    dialog.close();
  }

  function setSaving(value) {
    saving = value;
    dialog.setAttribute('aria-busy', String(value));
    dialog.querySelectorAll('input, textarea, select, button').forEach(el => { el.disabled = value; });
    dialog.querySelector('.memory-editor-save').textContent = value ? '正在保存…' : (options.saveLabel || '保存修改');
  }

  async function save(event) {
    event.preventDefault();
    if (saving) return;
    const error = dialog.querySelector('.memory-editor-error');
    error.hidden = true;
    const data = values();
    data.content = data.content.trim();
    data.keywords = data.keywords.trim();
    const importance = Number(data.importance);
    if (!data.content || !data.importance.trim() || !Number.isFinite(importance) || importance < 0 || importance > 1) {
      error.textContent = !data.content ? '请先填写记忆内容。' : '重要度请填写 0 到 1 之间的数字。';
      error.hidden = false;
      if (!data.content) field('content').focus();
      else { dialog.querySelector('details').open = true; field('importance').focus(); }
      return;
    }
    data.importance = importance;
    setSaving(true);
    let result;
    try {
      result = await options.onSave(data);
    } catch (err) {
      error.textContent = `保存失败，修改仍保留在这里。${err.message || '请稍后重试。'}`;
      error.hidden = false;
      setSaving(false);
      return;
    }
    setSaving(false);
    dialog.close();
    options.onSaved?.(result, data);
  }

  function create() {
    dialog = document.createElement('dialog');
    dialog.className = 'memory-editor';
    dialog.setAttribute('aria-labelledby', 'memoryEditorTitle');
    dialog.innerHTML = `
      <header class="memory-editor-header">
        <h2 class="memory-editor-title" id="memoryEditorTitle" tabindex="-1" autofocus>编辑记忆</h2>
        <p class="memory-editor-subtitle">修改完成后，点击底部保存。</p>
      </header>
      <form class="memory-editor-form" novalidate>
        <div class="memory-editor-body">
          <label class="memory-editor-field memory-editor-content-field">
            <span class="memory-editor-label">记忆内容</span>
            <textarea name="content" placeholder="写下想记住的事情…" required></textarea>
          </label>
          <label class="memory-editor-field">
            <span class="memory-editor-label">记忆标签</span>
            <select name="kind"><option value="long_term">长期重要</option><option value="daily">日常</option></select>
          </label>
          <details class="memory-editor-details">
            <summary>更多设置 · 关键词、重要度</summary>
            <div class="memory-editor-detail-fields">
              <label class="memory-editor-field">
                <span class="memory-editor-label">关键词</span>
                <input name="keywords" type="text" placeholder="例如：散步，周末">
                <span class="memory-editor-hint">用逗号分隔，方便以后找到这条记忆。</span>
              </label>
              <label class="memory-editor-field">
                <span class="memory-editor-label">重要度</span>
                <input name="importance" type="number" min="0" max="1" step="0.1" inputmode="decimal">
                <span class="memory-editor-hint">0–1，越大越重要。</span>
              </label>
            </div>
          </details>
        </div>
        <footer class="memory-editor-footer">
          <p class="memory-editor-error" role="alert" hidden></p>
          <div class="memory-editor-actions">
            <button class="memory-editor-cancel" type="button">取消</button>
            <button class="memory-editor-save" type="submit">保存修改</button>
          </div>
        </footer>
      </form>`;
    document.body.appendChild(dialog);
    dialog.querySelector('form').addEventListener('submit', save);
    dialog.querySelector('.memory-editor-cancel').addEventListener('click', cancel);
    dialog.addEventListener('cancel', event => { event.preventDefault(); cancel(); });
    dialog.addEventListener('close', () => {
      viewportTargets.forEach(target => { target.removeEventListener('resize', syncViewport); target.removeEventListener('scroll', syncViewport); });
      viewportTargets = [];
      options.onClose?.();
    });
  }

  function open(config) {
    if (!dialog) create();
    if (dialog.open) return;
    options = config;
    field('content').value = config.content || '';
    field('kind').value = config.kind === 'daily' ? 'daily' : 'long_term';
    field('keywords').value = config.keywords || '';
    field('importance').value = config.importance ?? 0.5;
    dialog.querySelector('.memory-editor-title').textContent = config.title || '编辑记忆';
    dialog.querySelector('details').open = false;
    dialog.querySelector('.memory-editor-error').hidden = true;
    initialValues = JSON.stringify(values());
    setSaving(false);
    viewportTargets = [window];
    if (window.visualViewport) viewportTargets.push(window.visualViewport);
    try {
      if (window.parent !== window) {
        viewportTargets.push(window.parent);
        if (window.parent.visualViewport) viewportTargets.push(window.parent.visualViewport);
      }
    } catch (_) { /* No parent access needed outside the home screen. */ }
    viewportTargets.forEach(target => { target.addEventListener('resize', syncViewport); target.addEventListener('scroll', syncViewport); });
    syncViewport();
    dialog.showModal();
    dialog.querySelector('.memory-editor-body').scrollTop = 0;
    field('content').scrollTop = 0;
    dialog.querySelector('.memory-editor-title').focus({ preventScroll: true });
  }

  window.MemoryEditor = { open };
})();

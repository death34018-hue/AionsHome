/* One portrait editor for settings and the theater. */
window.ImageAnchors = (() => {
  const base = '/api/theater/studio';
  const escape = value => String(value || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let dialog = null, opening = false;

  async function request(path, options = {}) {
    const response = await fetch(base + path, options);
    const data = await response.json();
    if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : '人物保存失败，请重试');
    return data;
  }

  async function open() {
    if (opening || dialog?.open) return;
    opening = true;
    try {
      const [saved, names] = await Promise.all([request('/anchors'), request('/anchor-identities')]);
      const anchors = [...saved];
      while (anchors.length < 3) anchors.push({id: 'anchor_' + (window.crypto?.randomUUID?.() || Date.now().toString(36) + Math.random().toString(36).slice(2)), name: ''});
      const node = document.createElement('dialog');
      dialog = node;
      node.className = 'image-anchors-dialog';
      node.setAttribute('aria-labelledby', 'imageAnchorsTitle');
      node.innerHTML = `<form class="image-anchors-form">
        <header><h2 id="imageAnchorsTitle">熟悉的人 · 人物锚点</h2><button type="button" data-close aria-label="关闭人物锚点">×</button></header>
        <div class="image-anchors-body"><p class="image-anchors-hint">私聊、群聊与小剧场共用这些照片。只给实际出场的人带参考照；长相跟随照片，服饰、动作和场景由本次创作决定。</p>
        <div class="image-anchors-grid">${anchors.map((a, i) => `<section class="image-anchor-card" data-id="${escape(a.id)}" data-image="${escape(a.image)}">
          <div class="image-anchor-preview"><img ${a.image?.startsWith('/image-anchors/') || a.image?.startsWith('/uploads/') ? `src="${escape(a.image)}"` : 'hidden'} alt="人物 ${i + 1} 的锚点照片"><span ${a.image ? 'hidden' : ''}>人物 ${i + 1} · 添加照片</span></div>
          <label>名字<input data-field="name" maxlength="80" value="${escape(a.name)}" placeholder="人物名字"></label>
          <label>别名<input data-field="aliases" value="${escape(a.aliases)}" placeholder="用逗号分隔"></label>
          <label>对应聊天身份<select data-field="identity"><option value="">按名字或别名识别</option>${Object.entries(names).map(([id, name]) => `<option value="${escape(id)}" ${a.identity === id ? 'selected' : ''}>${escape(name)}${id === 'user' ? '（我）' : ''}</option>`).join('')}</select></label>
          <label>固定外貌<textarea data-field="appearance" rows="2" placeholder="发色、瞳色等固定特征">${escape(a.appearance)}</textarea></label>
          <label>锚点照片<input data-field="photo" type="file" accept="image/jpeg,image/png,image/webp"></label>
        </section>`).join('')}</div>
        <p class="image-anchors-hint">名字或别名与聊天名字一致时可自动识别；改名或使用故事名时，可手动绑定聊天身份。小剧场仍在创作设置中选择本故事的出场人物。</p></div>
        <footer><span class="image-anchors-status" role="status" aria-live="polite"></span><button type="button" data-close>取消</button><button type="submit" class="image-anchors-save">保存人物</button></footer>
      </form>`;
      const previews = new Map();
      let busy = false;
      node.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => node.close()));
      node.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
      node.addEventListener('close', () => {
        previews.forEach(url => URL.revokeObjectURL(url));
        node.remove();
        if (dialog === node) dialog = null;
      });
      node.addEventListener('change', event => {
        if (event.target.dataset.field !== 'photo') return;
        const card = event.target.closest('.image-anchor-card');
        const file = event.target.files[0];
        if (previews.has(card)) URL.revokeObjectURL(previews.get(card));
        previews.delete(card);
        const url = file ? URL.createObjectURL(file) : card.dataset.image;
        if (file) previews.set(card, url);
        const img = card.querySelector('img');
        img.hidden = !url;
        if (url) img.src = url; else img.removeAttribute('src');
        card.querySelector('.image-anchor-preview span').hidden = Boolean(url);
      });
      node.querySelector('form').addEventListener('submit', async event => {
        event.preventDefault();
        if (busy) return;
        const status = node.querySelector('.image-anchors-status');
        const cards = [...node.querySelectorAll('.image-anchor-card')];
        const value = (card, field) => card.querySelector(`[data-field="${field}"]`).value.trim();
        try {
          const identities = new Set();
          for (const card of cards) {
            if (!value(card, 'name')) {
              if (card.dataset.image || card.querySelector('[data-field="photo"]').files.length || value(card, 'aliases') || value(card, 'appearance') || value(card, 'identity')) throw Error('请给已填写的人物补上名字');
              continue;
            }
            const identity = value(card, 'identity');
            if (identity && identities.has(identity)) throw Error('同一个聊天身份只能绑定一个人物');
            if (identity) identities.add(identity);
          }
          busy = true;
          node.querySelectorAll('button, input, select, textarea').forEach(el => { el.disabled = true; });
          status.textContent = '正在保存…';
          for (const card of cards) {
            const name = value(card, 'name');
            if (!name) continue;
            const photo = card.querySelector('[data-field="photo"]');
            const path = '/anchors/' + encodeURIComponent(card.dataset.id.replace(/^anchor_/, ''));
            const fields = JSON.stringify({name, aliases: value(card, 'aliases'), appearance: value(card, 'appearance'), identity: value(card, 'identity'), image: card.dataset.image || ''});
            let saved;
            if (photo.files[0]) {
              const body = new FormData();
              body.append('fields', fields);
              body.append('file', photo.files[0]);
              saved = await request(path + '/photo', {method: 'PUT', body});
              photo.value = '';
            } else {
              saved = await request(path, {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: fields});
            }
            card.dataset.image = saved.image || '';
          }
          node.close();
          window.showToast?.('人物锚点已保存，聊天与小剧场共用');
        } catch (error) {
          status.textContent = error.message || '保存失败，请重试；已填写的内容仍保留';
        } finally {
          busy = false;
          node.querySelectorAll('button, input, select, textarea').forEach(el => { el.disabled = false; });
        }
      });
      document.body.appendChild(node);
      node.showModal();
    } catch (error) {
      window.showToast?.(error.message || '人物锚点加载失败');
    } finally {
      opening = false;
    }
  }
  return {open};
})();

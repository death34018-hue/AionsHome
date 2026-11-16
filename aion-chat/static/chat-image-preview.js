(function (root) {
  'use strict';
  const escape = value => String(value).replaceAll('&', '&amp;').replaceAll('"', '&quot;')
    .replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll("'", '&#39;');

  function attributes(url, escaped = false) {
    let original = String(url || '');
    if (escaped) {
      const text = document.createElement('textarea');
      text.innerHTML = original;
      original = text.value;
    }
    let source = original;
    try {
      const parsed = new URL(original, root.location.href);
      if (parsed.origin === root.location.origin
          && /^\/(uploads|cr-uploads|public|screenshots)\//.test(parsed.pathname)
          && !parsed.pathname.startsWith('/public/wallpaper/')
          && /\.(jpe?g|png|webp)$/i.test(parsed.pathname)) {
        source = '/api/chat-media/thumbnail?src=' + encodeURIComponent(parsed.pathname);
      }
    } catch (_) {}
    // Reserve space before lazy images load so off-screen history stays off-screen.
    return `src="${escape(source)}" data-original-src="${escape(original)}" loading="lazy" decoding="async" width="120" height="90" style="object-fit:contain;max-width:100%;border-radius:8px;cursor:pointer" onerror="this.onerror=null;this.src=this.dataset.originalSrc"`;
  }

  function original(img) { return img.dataset?.originalSrc || img.src; }
  function group(images) {
    if (images.length < 2) return images.join('');
    return `<div class="chat-image-group" data-image-gallery><button type="button" class="chat-image-toggle" aria-expanded="false" onclick="ChatImagePreview.toggle(this)">展开 ${images.length}</button><div class="chat-image-cards">${images.join('')}</div></div>`;
  }

  function toggle(button) {
    const group = button.closest('.chat-image-group');
    const top = group.getBoundingClientRect().top;
    const expanded = group.classList.toggle('expanded');
    button.setAttribute('aria-expanded', String(expanded));
    button.textContent = `${expanded ? '收起' : '展开'} ${group.querySelectorAll('img').length}`;
    // Keep the control under the finger if scroll anchoring moves the message.
    let scroll = group.parentElement;
    while (scroll && !/auto|scroll/.test(getComputedStyle(scroll).overflowY)) scroll = scroll.parentElement;
    if (scroll) scroll.scrollTop += group.getBoundingClientRect().top - top;
  }
  async function clipboardPng(url) {
    const image = new Image();
    image.crossOrigin = 'anonymous';
    image.src = url;
    await image.decode();
    const canvas = document.createElement('canvas');
    canvas.width = image.naturalWidth;
    canvas.height = image.naturalHeight;
    canvas.getContext('2d').drawImage(image, 0, 0);
    return new Promise((resolve, reject) => canvas.toBlob(
      blob => blob ? resolve(blob) : reject(new Error('无法复制图片')), 'image/png'));
  }

  function showSaveMenu(url, openViewer) {
    if (!url) return;
    document.querySelector('.image-save-menu-overlay')?.remove();
    const overlay = document.createElement('div');
    overlay.className = 'image-save-menu-overlay';
    const sheet = document.createElement('div');
    sheet.className = 'image-save-menu';
    const close = () => overlay.remove();
    function button(label, onClick) {
      const element = document.createElement('button');
      element.type = 'button';
      element.textContent = label;
      element.onclick = onClick;
      sheet.append(element);
      return element;
    }
    const save = button('保存图片', async () => {
      save.disabled = true;
      try {
        await root.AionImageViewer.saveOriginal(url);
        close();
      } catch (_) {
        save.textContent = '保存失败，点此重试';
        save.disabled = false;
      }
    });
    save.className = 'primary';
    if (matchMedia('(any-pointer: fine)').matches) {
      const copy = button('复制图片', async () => {
        copy.disabled = true;
        try {
          await navigator.clipboard.write([new ClipboardItem({ 'image/png': clipboardPng(url) })]);
          copy.textContent = '已复制图片';
          setTimeout(close, 650);
        } catch (_) {
          close();
          root.AionImageViewer.open([url], 0, { hint: '右键点击原图，选择「复制图片」' });
        }
      });
    }
    button('查看大图', () => { close(); openViewer(url); });
    sheet.append(root.ChatImageAlbum.createButton(url));
    button('取消', close);
    overlay.append(sheet);
    overlay.onclick = event => { if (event.target === overlay) close(); };
    document.body.append(overlay);
    requestAnimationFrame(() => overlay.classList.add('active'));
  }

  root.ChatImagePreview = { attributes, original, group, toggle, showSaveMenu };
})(typeof window !== 'undefined' ? window : globalThis);

// Compress the sending copy on the device; never upload the original first.
(function () {
  'use strict';
  const TARGET_BYTES = 400 * 1024;
  const MAX_EDGE = 1600;

  async function compress(file) {
    if (!file.type.startsWith('image/') || file.type === 'image/gif' || file.size <= TARGET_BYTES) return file;
    const sourceUrl = URL.createObjectURL(file);
    const image = new Image();
    const canvas = document.createElement('canvas');
    try {
      await new Promise((resolve, reject) => {
        image.onload = resolve;
        image.onerror = () => reject(new Error('这张图片无法在设备上压缩，请换成 JPG 或 PNG 后重试'));
        image.src = sourceUrl;
      });
      const context = canvas.getContext('2d');
      if (!context) throw new Error('设备暂时无法处理图片，请重试');
      let edge = Math.min(MAX_EDGE, Math.max(image.naturalWidth, image.naturalHeight));
      while (edge >= 320) {
        const scale = Math.min(1, edge / Math.max(image.naturalWidth, image.naturalHeight));
        canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
        canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
        // JPEG has no alpha channel; a white background keeps transparent text readable.
        context.fillStyle = '#fff';
        context.fillRect(0, 0, canvas.width, canvas.height);
        context.drawImage(image, 0, 0, canvas.width, canvas.height);
        for (const quality of [0.85, 0.72, 0.6]) {
          const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', quality));
          if (!blob) throw new Error('图片压缩失败，请重新选择图片');
          if (blob.size <= TARGET_BYTES) {
            const name = (file.name || 'photo').replace(/\.[^.]+$/, '') + '.jpg';
            return new File([blob], name, { type: 'image/jpeg', lastModified: file.lastModified || Date.now() });
          }
        }
        edge = Math.floor(edge * 0.8);
      }
      throw new Error('图片仍然过大，请裁剪后重试');
    } finally {
      URL.revokeObjectURL(sourceUrl);
      image.src = '';
      canvas.width = canvas.height = 1;
    }
  }

  function release(item) {
    if (item?.previewUrl) URL.revokeObjectURL(item.previewUrl);
    if (item) delete item.previewUrl;
    item?.controller?.abort();
  }

  async function add(endpoint, file, attachments, render) {
    const item = {
      type: file.type, name: file.name, uploading: true,
      status: '处理中…', previewUrl: URL.createObjectURL(file),
      controller: new AbortController(),
    };
    attachments.push(item);
    render();
    let timer;
    try {
      const prepared = await compress(file);
      if (!attachments.includes(item)) return;
      if (prepared !== file) {
        URL.revokeObjectURL(item.previewUrl);
        item.previewUrl = URL.createObjectURL(prepared);
      }
      item.type = prepared.type;
      item.status = '上传中…';
      render();
      const form = new FormData();
      form.append('file', prepared, prepared.name || file.name || 'attachment');
      timer = setTimeout(() => item.controller.abort(), 120000);
      const response = await fetch(endpoint, { method: 'POST', body: form, signal: item.controller.signal });
      if (!response.ok) throw new Error(`上传失败（HTTP ${response.status}），请重试`);
      let data;
      try { data = await response.json(); }
      catch (_) { throw new Error('上传未完成，请检查网络或重新登录后重试'); }
      if (data.error || !data.url) throw new Error(data.error || '上传未完成，请重试');
      if (!attachments.includes(item)) return;
      Object.assign(item, data, { uploading: false, status: '' });
      delete item.controller;
    } catch (error) {
      if (!attachments.includes(item)) return; // User removed/cancelled this attachment.
      attachments.splice(attachments.indexOf(item), 1);
      release(item);
      throw new Error(error.name === 'AbortError' ? '上传超时，请检查网络后重试' : error.message);
    } finally {
      clearTimeout(timer);
      render();
    }
  }

  window.ChatImageUpload = { compress, add, release };
})();

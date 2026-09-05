// 聊天图片只有手动选择时才入册，复用相册上传接口，不移动聊天原图。
(() => {
  'use strict';

  function createButton(url) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = '添加到相册';
    button.setAttribute('aria-live', 'polite');
    button.onclick = async () => {
      if (button.disabled) return;
      button.disabled = true;
      button.textContent = '正在添加…';
      try {
        const image = await fetch(url);
        if (!image.ok) throw new Error('无法读取原图');
        const blob = await image.blob();
        if (blob.size > 40 * 1024 * 1024) throw new Error('图片超过 40 MB');
        const filename = new URL(url, location.href).pathname.split('/').pop() || 'chat-image';
        const data = new FormData();
        data.append('file', blob, filename);
        const response = await fetch('/api/album/upload', { method: 'POST', body: data });
        const result = await response.json().catch(() => null);
        if (!response.ok) throw new Error(typeof result?.detail === 'string' ? result.detail : `请求失败（${response.status}）`);
        button.textContent = '已添加到相册';
      } catch (error) {
        button.disabled = false;
        button.textContent = `添加失败：${error.message || '请检查连接'}（点击重试）`;
      }
    };
    return button;
  }

  window.ChatImageAlbum = { createButton };
})();

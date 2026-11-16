/* Image-only zoom: works even in WebViews that disable page zoom. */
(() => {
  'use strict';
  const clamp = (v, min, max) => Math.max(min, Math.min(max, v));

  function bind(stage, img, options = {}) {
    const pointers = new Map();
    let scale = 1, x = 0, y = 0, start = null, pinch = null;
    let moved = false, multi = false, hold = null, suppressClick = 0;
    const abort = new AbortController();
    const listen = (el, type, fn, extra = {}) => el.addEventListener(type, fn, { ...extra, signal: abort.signal });
    stage.style.touchAction = 'none';
    img.draggable = false;
    img.style.transformOrigin = 'center';
    function paint() {
      // object-fit images can have a viewport-sized box: clamp using the actual fitted pixels.
      const fit = Math.min(img.clientWidth / (img.naturalWidth || 1), img.clientHeight / (img.naturalHeight || 1));
      const width = img.naturalWidth ? img.naturalWidth * fit : img.clientWidth;
      const height = img.naturalHeight ? img.naturalHeight * fit : img.clientHeight;
      const maxX = Math.max(0, (width * scale - stage.clientWidth) / 2);
      const maxY = Math.max(0, (height * scale - stage.clientHeight) / 2);
      x = clamp(x, -maxX, maxX); y = clamp(y, -maxY, maxY);
      img.style.transform = `translate(${x}px, ${y}px) scale(${scale})`;
    }
    function cancelHold() { clearTimeout(hold); hold = null; }
    function reset() { scale = 1; x = y = 0; paint(); }
    function center(a, b) {
      const rect = stage.getBoundingClientRect();
      return { x: (a.x + b.x) / 2 - rect.left - rect.width / 2, y: (a.y + b.y) / 2 - rect.top - rect.height / 2 };
    }
    function beginPinch() {
      const [a,b] = [...pointers.values()];
      pinch = { distance: Math.max(1, Math.hypot(a.x-b.x,a.y-b.y)), center: center(a,b), scale, x, y };
    }
    listen(stage, 'pointerdown', event => {
      if (event.pointerType === 'mouse' && event.button !== 0) return;
      cancelHold();
      pointers.set(event.pointerId, { x:event.clientX, y:event.clientY });
      stage.setPointerCapture(event.pointerId);
      if (pointers.size === 1) {
        moved = multi = false;
        start = { x:event.clientX, y:event.clientY, tx:x, ty:y, scale };
        if (options.onLongPress) hold = setTimeout(() => {
          moved = true; suppressClick = Date.now() + 700;
          options.onLongPress();
        }, 560);
      } else {
        multi = true; moved = true;
        beginPinch();
      }
    });
    listen(stage, 'pointermove', event => {
      if (!pointers.has(event.pointerId)) return;
      pointers.set(event.pointerId, { x:event.clientX, y:event.clientY });
      if (pointers.size >= 2) {
        cancelHold();
        const [a,b] = [...pointers.values()], mid = center(a,b);
        scale = clamp(pinch.scale * Math.hypot(a.x-b.x,a.y-b.y) / pinch.distance, 1, 6);
        const ratio = scale / pinch.scale;
        x = mid.x - (pinch.center.x - pinch.x) * ratio;
        y = mid.y - (pinch.center.y - pinch.y) * ratio;
        paint();
      } else if (start) {
        const dx = event.clientX-start.x, dy = event.clientY-start.y;
        if (Math.hypot(dx,dy) > 10) { moved = true; cancelHold(); }
        if (scale > 1) { x = start.tx+dx; y = start.ty+dy; paint(); }
      }
    });
    function finish(event) {
      if (!pointers.has(event.pointerId)) return;
      cancelHold();
      pointers.delete(event.pointerId);
      if (moved || multi || event.type !== 'pointerup') suppressClick = Date.now()+450;
      if (pointers.size) {
        if (pointers.size >= 2) beginPinch();
        else { const p = [...pointers.values()][0]; start = { ...p, tx:x, ty:y, scale }; }
        return;
      }
      if (event.type === 'pointerup' && start && !multi && start.scale <= 1.01 && scale <= 1.01) {
        const dx = event.clientX-start.x, dy = event.clientY-start.y;
        if (Math.abs(dx)>55 && Math.abs(dx)>Math.abs(dy)*1.2) options.onSwipe?.(dx<0 ? 1 : -1);
        else if (dy>90 && dy>Math.abs(dx)*1.2) options.onDismiss?.();
      }
      start = pinch = null;
    }
    listen(stage, 'pointerup', finish);
    listen(stage, 'pointercancel', finish);
    listen(stage, 'lostpointercapture', finish);
    listen(stage, 'click', event => {
      event.stopPropagation();
      if (Date.now()<suppressClick) { event.preventDefault(); return; }
      options.onTap?.();
    });
    listen(stage, 'dblclick', event => { event.preventDefault(); scale = scale>1 ? 1 : 2.5; x=y=0; paint(); });
    listen(stage, 'wheel', event => {
      event.preventDefault(); cancelHold();
      const rect = stage.getBoundingClientRect(), previous = scale;
      scale = clamp(scale * Math.exp(-event.deltaY*.002), 1, 6);
      const cx = event.clientX-rect.left-rect.width/2, cy = event.clientY-rect.top-rect.height/2;
      x = cx-(cx-x)*scale/previous; y = cy-(cy-y)*scale/previous; paint();
    }, { passive:false });
    listen(stage, 'contextmenu', event => {
      if (!options.onLongPress) return;
      if (event.target === img && event.pointerType !== 'touch' && matchMedia('(any-pointer: fine)').matches) return;
      event.preventDefault(); cancelHold(); suppressClick = Date.now()+700; options.onLongPress();
    });
    listen(img, 'load', reset);
    listen(window, 'resize', paint);
    reset();
    return { reset, destroy() { cancelHold(); abort.abort(); pointers.clear(); } };
  }

  let active = null;
  function close() { if (!active) return false; active(); return true; }
  function open(sources, index = 0, options = {}) {
    const urls = (Array.isArray(sources) ? sources : [sources]).filter(Boolean);
    if (!urls.length) return;
    close();
    let current = clamp(index, 0, urls.length-1);
    const previousFocus = document.activeElement, overflow = document.body.style.overflow;
    const overlay = document.createElement('div');
    overlay.className = 'aion-image-viewer';
    overlay.setAttribute('role','dialog'); overlay.setAttribute('aria-modal','true'); overlay.setAttribute('aria-label','图片预览');
    overlay.innerHTML = `<div class="aion-image-stage"><img alt="原图"><p class="aion-viewer-error" hidden>图片加载失败，可重新打开或保存原图后查看。</p></div><div class="aion-viewer-controls"><div class="aion-viewer-top"><span aria-live="polite"></span><button type="button" aria-label="关闭大图">✕</button></div><button type="button" class="aion-viewer-prev" aria-label="上一张">‹</button><button type="button" class="aion-viewer-next" aria-label="下一张">›</button><div class="aion-viewer-bottom"><button type="button" data-reset aria-label="重置缩放">重置缩放</button><button type="button" data-save>保存图片</button><p class="aion-viewer-hint">双指缩放 · 放大后拖动 · 左右滑动切图</p></div></div>`;
    const img = overlay.querySelector('img'), stage = overlay.querySelector('.aion-image-stage');
    const prev = overlay.querySelector('.aion-viewer-prev'), next = overlay.querySelector('.aion-viewer-next');
    const count = overlay.querySelector('[aria-live]'), error = overlay.querySelector('.aion-viewer-error');
    const closeButton = overlay.querySelector('[aria-label="关闭大图"]');
    if (options.hint) overlay.querySelector('.aion-viewer-hint').textContent = options.hint;
    const gestures = bind(stage,img,{
      onSwipe: step, onDismiss: close,
      onTap: () => overlay.classList.toggle('controls-hidden'),
      onLongPress: options.onLongPress ? () => options.onLongPress(urls[current]) : undefined,
    });
    function show() {
      gestures.reset(); error.hidden = true; img.style.visibility = ''; img.src = urls[current];
      count.textContent = `${current+1} / ${urls.length}`;
      prev.hidden = next.hidden = urls.length<2; prev.disabled = current===0; next.disabled = current===urls.length-1;
    }
    function step(direction) { const i = current+direction; if(i>=0 && i<urls.length) { current=i; show(); } }
    img.onerror = () => { error.hidden = false; img.style.visibility = 'hidden'; };
    closeButton.onclick = close;
    prev.onclick = () => step(-1); next.onclick = () => step(1);
    overlay.querySelector('[data-reset]').onclick = gestures.reset;
    const save = overlay.querySelector('[data-save]');
    save.onclick = async () => {
      save.disabled = true;
      try { await (options.onSave || saveOriginal)(urls[current]); }
      catch (_) { save.textContent = '保存失败，点此重试'; }
      finally { save.disabled = false; }
    };
    function keydown(event) {
      // A save menu above the viewer owns its own interactions.
      if (document.querySelector('.image-save-menu-overlay')) return;
      if(event.key==='Escape') { event.preventDefault(); close(); }
      else if(event.key==='ArrowLeft') { event.preventDefault(); step(-1); }
      else if(event.key==='ArrowRight') { event.preventDefault(); step(1); }
      else if(event.key==='Tab') {
        overlay.classList.remove('controls-hidden');
        const buttons = [...overlay.querySelectorAll('button')].filter(b => !b.hidden && !b.disabled);
        const first = buttons[0], last = buttons[buttons.length-1];
        if(event.shiftKey && document.activeElement===first) { event.preventDefault(); last.focus(); }
        else if(!event.shiftKey && document.activeElement===last) { event.preventDefault(); first.focus(); }
      }
    }
    active = () => {
      gestures.destroy(); document.removeEventListener('keydown',keydown); overlay.remove();
      document.body.style.overflow = overflow; active=null;
      if(previousFocus?.isConnected) previousFocus.focus({preventScroll:true});
    };
    document.body.style.overflow = 'hidden'; document.body.append(overlay);
    document.addEventListener('keydown',keydown); show(); closeButton.focus({preventScroll:true});
  }

  async function saveOriginal(url) {
    const response = await fetch(url);
    if (!response.ok) throw Error('下载失败');
    const blob = await response.blob();
    const source = new URL(url, location.href);
    const name = /^https?:$/.test(source.protocol) && source.pathname.split('/').pop();
    let filename = name || 'image.' + (blob.type.split('/')[1] || 'png');
    try { filename = decodeURIComponent(filename); } catch (_) {}
    let saver;
    for (const frame of [window,window.parent,window.top]) {
      try { if(typeof frame.AionImageSaver?.save === 'function') { saver=frame.AionImageSaver; break; } } catch (_) {}
    }
    if(saver) {
      const data = await new Promise((resolve,reject) => {
        const reader = new FileReader(); reader.onload = () => resolve(reader.result.split(',')[1]); reader.onerror=reject; reader.readAsDataURL(blob);
      });
      saver.save(data,filename);
    } else {
      const href = URL.createObjectURL(blob), link = document.createElement('a');
      link.href=href; link.download=filename; document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(href),60000);
    }
  }
  function openFromElement(img) {
    const group = img.closest('[data-image-gallery],.moment-images,.timeline-images,.detail-images,.msg-attachments');
    const images = group ? [...group.querySelectorAll('img')] : [img];
    open(images.map(image => image.dataset.originalSrc || image.src), images.indexOf(img));
  }
  window.AionImageViewer = { open, close, bind, openFromElement, saveOriginal };
})();

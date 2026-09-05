(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const state = { photos: [], total: 0, filter: 'all', album: '', index: -1, viewPhoto: null, scrollTop: 0, loading: false, uploading: false, request: null, version: 0, dirty: false, detailBusy: false };
  state.selecting = false;
  state.selected = new Set();
  state.moving = false;
  const kindNames = { selfie: '自拍', draw: '绘画', gift: '礼物' };
  const titleOf = photo => photo.title || (photo.source === 'upload' ? '上传的照片' : kindNames[photo.kind] || '生成的照片');
  const current = () => state.viewPhoto;
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  async function api(path, options = {}) {
    const response = await fetch('/api/album' + path, options);
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : `请求失败（${response.status}）`);
    return result;
  }
  const jsonOptions = data => ({ method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
  const notice = text => { $('notice').textContent = text; };

  function renderAlbums(albums) {
    const tabs = $('albumTabs');
    tabs.replaceChildren();
    [{ id: '', name: '全部照片' }, ...albums].forEach(album => {
      const button = el('button', state.album === album.id ? 'active' : '', album.name);
      button.type = 'button';
      button.setAttribute('aria-pressed', String(state.album === album.id));
      button.onclick = () => { state.album = album.id; loadPhotos(); };
      tabs.append(button);
    });
    ['photoAlbum', 'uploadAlbum', 'moveAlbum'].forEach(id => {
      const select = $(id);
      const selected = select.value || 'family';
      select.replaceChildren(...albums.map(album => {
        const option = el('option', '', album.name);
        option.value = album.id;
        return option;
      }));
      select.value = selected;
    });
  }

  function updateSelection() {
    $('selectButton').textContent = state.selecting ? '取消' : '选择';
    $('selectButton').setAttribute('aria-pressed', String(state.selecting));
    $('selectionBar').hidden = !state.selecting;
    document.body.classList.toggle('selecting-photos', state.selecting);
    $('selectionCount').textContent = `已选 ${state.selected.size} 张`;
    $('moveButton').disabled = !state.selected.size || state.loading || state.moving;
    $('photoList').querySelectorAll('.photo-card').forEach(button => {
      const checked = state.selected.has(button.dataset.photoId);
      button.classList.toggle('selected', checked);
      if (state.selecting) button.setAttribute('aria-pressed', String(checked));
      else button.removeAttribute('aria-pressed');
      button.setAttribute('aria-label', `${state.selecting ? '选择' : '查看'}${button.dataset.photoLabel}`);
      button.querySelector('.selection-mark').textContent = checked ? '✓' : '';
    });
  }
  function setSelection(enabled) {
    state.selecting = enabled;
    state.selected.clear();
    updateSelection();
  }
  $('selectButton').onclick = () => setSelection(!state.selecting);
  $('moveButton').onclick = () => {
    if (!state.selected.size || state.loading || state.moving) return;
    $('moveSummary').textContent = `将选中的 ${state.selected.size} 张照片放入：`;
    const destination = Array.from($('moveAlbum').options).find(option => option.value !== state.album);
    if (destination) $('moveAlbum').value = destination.value;
    $('moveMessage').textContent = '';
    $('moveDialog').showModal();
  };
  function closeMove() {
    if (!state.moving) $('moveDialog').close();
  }
  $('cancelMoveButton').onclick = closeMove;
  $('moveDialog').addEventListener('cancel', event => { event.preventDefault(); closeMove(); });
  $('moveForm').onsubmit = async event => {
    event.preventDefault();
    if (state.moving || !state.selected.size) return;
    const albumId = $('moveAlbum').value;
    const albumName = $('moveAlbum').selectedOptions[0].textContent;
    state.moving = true;
    ['moveAlbum', 'cancelMoveButton', 'confirmMoveButton'].forEach(id => { $(id).disabled = true; });
    $('moveMessage').textContent = '正在移动…';
    try {
      const result = await api('/photos/move', {
        ...jsonOptions({ photo_ids: Array.from(state.selected), album_id: albumId }), method: 'POST'
      });
      $('moveDialog').close();
      setSelection(false);
      const loaded = await loadPhotos(false, true);
      notice(`已将 ${result.moved} 张照片放入「${albumName}」${result.skipped ? `，另有 ${result.skipped} 张已不在相册中，已跳过` : ''}。${loaded ? '' : '列表刷新失败，请点击刷新重试。'}`);
    } catch (error) {
      $('moveMessage').textContent = '移动失败：' + error.message + '。勾选已保留，可以重试。';
    } finally {
      state.moving = false;
      ['moveAlbum', 'cancelMoveButton', 'confirmMoveButton'].forEach(id => { $(id).disabled = false; });
      updateSelection();
    }
  };

  function render(appendFrom = 0) {
    const list = $('photoList');
    if (!appendFrom) list.replaceChildren();
    state.photos.slice(appendFrom).forEach((photo, offset) => {
        const index = appendFrom + offset;
        const button = el('button', 'photo-card');
        button.type = 'button';
        button.dataset.photoId = photo.id;
        button.dataset.photoLabel = `${titleOf(photo)}，${photo.taken_on}`;
        button.setAttribute('aria-label', `查看${titleOf(photo)}，${photo.taken_on}`);
        const frame = el('div', 'photo-image');
        const image = el('img');
        image.src = photo.thumbnail_url;
        image.alt = titleOf(photo);
        image.loading = 'lazy';
        image.decoding = 'async';
        frame.append(image);
        if (photo.favorite) frame.append(el('span', 'favorite-mark', '♥'));
        const check = el('span', 'selection-mark');
        check.setAttribute('aria-hidden', 'true');
        frame.append(check);
        button.append(frame);
        button.onclick = () => {
          if (!state.selecting) { openPhoto(index); return; }
          if (state.loading || state.moving) return;
          if (state.selected.has(photo.id)) state.selected.delete(photo.id);
          else if (state.selected.size < 1000) state.selected.add(photo.id);
          else notice('一次最多选择 1000 张照片，请先移动这一批。');
          updateSelection();
        };
        list.append(button);
    });
    $('photoCount').textContent = `${state.total} 张照片 · 从新到旧`;
    $('emptyState').hidden = state.photos.length > 0;
    const filtered = state.album || state.filter !== 'all' || $('searchInput').value.trim();
    $('emptyTitle').textContent = filtered ? '这里暂时还没有照片' : '相册的第一页，留给下一次心动';
    $('emptyHint').textContent = filtered ? '试试其他分类或关键词，或者收藏一张喜欢的照片。' : 'AI 新生成的照片会自动收进来，也可以上传你想留下的照片。聊天里发送的图片不会自动收录。';
    $('emptyUploadButton').hidden = Boolean(filtered);
    $('loadMoreButton').hidden = state.photos.length >= state.total;
    updateSelection();
  }

  async function loadPhotos(append = false, preserve = false) {
    if ($('moveDialog').open) return;
    if ($('photoDialog').open && !append && !preserve) return;
    if (append && state.loading) return;
    if (!append && !preserve) setSelection(state.selecting);
    if (state.request) state.request.abort();
    const request = new AbortController();
    state.request = request;
    const version = ++state.version;
    state.loading = true;
    updateSelection();
    moreObserver.unobserve($('loadMoreButton'));
    let loaded = false;
    $('photoList').setAttribute('aria-busy', 'true');
    $('loadMoreButton').disabled = true;
    notice('');
    const previousCount = state.photos.length;
    const previousScroll = $('photoScroll').scrollTop;
    const targetCount = preserve ? Math.max(60, previousCount) : 60;
    const params = new URLSearchParams({ offset: append ? previousCount : 0, limit: 60, query: $('searchInput').value.trim() });
    if (state.album) params.set('album_id', state.album);
    if (state.filter === 'favorite') params.set('favorite', 'true');
    else if (state.filter !== 'all') params.set('source', state.filter);
    try {
      let result = await api('/photos?' + params, { signal: request.signal });
      const photos = result.photos.slice();
      // 修改日期/筛选归属后重读已加载的范围，避免分页偏移导致漏图或重复。
      while (!append && photos.length < Math.min(targetCount, result.total) && result.photos.length) {
        params.set('offset', photos.length);
        result = await api('/photos?' + params, { signal: request.signal });
        photos.push(...result.photos);
      }
      if (version !== state.version) return;
      state.photos = append ? state.photos.concat(photos) : photos;
      state.total = result.total;
      if (result.albums) renderAlbums(result.albums);
      render(append ? previousCount : 0);
      if (!append) $('photoScroll').scrollTop = preserve ? previousScroll : 0;
      if ($('photoDialog').open) {
        state.index = state.photos.findIndex(photo => photo.id === current().id);
        updateNavigation();
      }
      loaded = true;
      return true;
    } catch (error) {
      if (error.name !== 'AbortError') {
        notice('相册加载失败：' + error.message + '。可以点击刷新重试。');
        if (!state.photos.length) $('photoList').replaceChildren();
      }
    } finally {
      if (version === state.version) {
        state.loading = false;
        updateSelection();
        $('photoList').setAttribute('aria-busy', 'false');
        $('loadMoreButton').disabled = false;
        if (loaded && state.photos.length < state.total) moreObserver.observe($('loadMoreButton'));
      }
    }
  }

  function openPhoto(index) {
    if (state.detailBusy) return;
    if (index < 0 || index >= state.photos.length) return;
    state.index = index;
    state.viewPhoto = state.photos[index];
    state.dirty = false;
    const photo = current();
    $('viewerMessage').textContent = '';
    $('fullImage').hidden = false;
    $('missingImage').hidden = true;
    $('fullImage').alt = titleOf(photo);
    $('fullImage').src = photo.url;
    $('downloadLink').href = `/api/album/photos/${photo.id}/download`;
    updateNavigation();
    updateFavorite();
    setControls(false);
    if (!$('photoDialog').open) {
      suppressClickUntil = 0;
      state.scrollTop = $('photoScroll').scrollTop;
      document.documentElement.classList.add('viewing-photo');
      setParentImmersive(true);
      $('photoDialog').showModal();
    }
    $('imageStage').focus({ preventScroll: true });
  }

  function updateNavigation() {
    $('photoPosition').textContent = state.index < 0 ? '照片' : `${state.index + 1} / ${state.total}`;
    $('previousButton').disabled = state.index <= 0;
    $('nextButton').disabled = state.index < 0 || state.index + 1 >= state.total;
  }
  function updateFavorite() {
    const photo = current();
    $('favoriteButton').textContent = photo.favorite ? '♥' : '♡';
    $('favoriteButton').setAttribute('aria-pressed', String(photo.favorite));
    $('favoriteButton').setAttribute('aria-label', photo.favorite ? '取消收藏' : '收藏照片');
  }
  function setParentImmersive(open) {
    if (window.parent === window) return;
    try {
      window.parent.document.getElementById('subPageOverlay')?.classList.toggle('album-viewing', open);
    } catch (_) { /* 独立页面也能正常查看。 */ }
  }
  function setControls(visible) {
    $('photoDialog').classList.toggle('controls-visible', visible);
    $('viewerControls').inert = !visible;
    $('imageStage').setAttribute('aria-label', visible ? '轻点隐藏操作栏' : '轻点显示操作栏');
  }
  async function stepPhoto(direction) {
    if (state.detailBusy || state.loading || state.index < 0) return;
    const photoId = current().id;
    const index = state.index + direction;
    if (index >= state.photos.length && index < state.total) {
      $('viewerMessage').textContent = '正在加载…';
      const loaded = await loadPhotos(true);
      if (!$('photoDialog').open || current().id !== photoId) return;
      if (!loaded) { setControls(true); $('viewerMessage').textContent = '加载失败，请再试一次。'; return; }
    }
    openPhoto(index);
  }

  function closePhoto() {
    if (state.detailBusy) return;
    if ($('detailsDialog').open && !closeDetails()) return;
    $('photoDialog').close();
    $('fullImage').removeAttribute('src');
    setParentImmersive(false);
    document.documentElement.classList.remove('viewing-photo');
    $('photoScroll').scrollTop = state.scrollTop;
    // 重新观察，关闭大图后继续预加载，浏览位置和已加载页面保持不变。
    moreObserver.unobserve($('loadMoreButton'));
    if (state.photos.length < state.total) moreObserver.observe($('loadMoreButton'));
  }
  $('closePhotoButton').onclick = closePhoto;
  $('photoDialog').addEventListener('cancel', event => { event.preventDefault(); closePhoto(); });
  $('fullImage').onerror = () => { $('fullImage').hidden = true; $('missingImage').hidden = false; };
  $('previousButton').onclick = () => stepPhoto(-1);
  $('nextButton').onclick = () => stepPhoto(1);
  let detailViewRequest = 0;
  async function loadPhotoViews(photoId) {
    const request = ++detailViewRequest;
    $('photoViewStatus').textContent = '正在读取…';
    try {
      const photo = await api(`/photos/${photoId}`);
      if (request !== detailViewRequest || !$('detailsDialog').open || current().id !== photoId) return;
      if (!Array.isArray(photo.viewed_by)) throw new Error('回看状态尚不可用，请确认后端已更新');
      const names = photo.viewed_by.map(view => view.name).filter(Boolean);
      $('photoViewStatus').textContent = names.length ? `已回看 · ${names.join('、')}` : '尚未回看';
    } catch (error) {
      if (request === detailViewRequest && $('detailsDialog').open && current().id === photoId) {
        $('photoViewStatus').textContent = '回看状态暂不可用';
      }
    }
  }
  $('detailsButton').onclick = () => {
    $('photoTitle').value = current().title || '';
    $('photoDate').value = current().taken_on;
    $('photoAlbum').value = current().album_id || 'family';
    const generated = current().source === 'generated';
    $('promptSection').hidden = !generated;
    $('promptText').textContent = generated ? (current().prompt || '（无）') : '';
    $('detailMessage').textContent = '';
    state.dirty = false;
    $('detailsDialog').showModal();
    loadPhotoViews(current().id);
  };
  function closeDetails() {
    if (state.detailBusy) return false;
    if (state.dirty && !confirm('照片信息尚未保存，放弃本次修改吗？')) return false;
    state.dirty = false;
    $('detailsDialog').close();
    return true;
  }
  $('cancelDetailsButton').onclick = closeDetails;
  $('detailsDialog').addEventListener('cancel', event => { event.preventDefault(); closeDetails(); });
  // 只有从面板外开始、也在面板外结束的点击才关闭，避免编辑时拖动误触。
  let detailsBackdropDown = false;
  function outsideDetails(event) {
    const dialog = $('detailsDialog');
    if (event.target !== dialog) return false;
    const bounds = dialog.getBoundingClientRect();
    return event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom;
  }
  $('detailsDialog').addEventListener('pointerdown', event => { detailsBackdropDown = outsideDetails(event); });
  $('detailsDialog').addEventListener('pointercancel', () => { detailsBackdropDown = false; });
  $('detailsDialog').addEventListener('click', event => {
    const close = detailsBackdropDown && outsideDetails(event);
    detailsBackdropDown = false;
    if (close) { event.preventDefault(); event.stopPropagation(); closeDetails(); }
  });
  $('metadataForm').oninput = () => { state.dirty = true; };
  $('metadataForm').onsubmit = async event => {
    event.preventDefault();
    const photo = current();
    if (state.detailBusy) return;
    state.detailBusy = true;
    $('savePhotoButton').disabled = true;
    try {
      const updated = await api(`/photos/${photo.id}`, jsonOptions({ title: $('photoTitle').value.trim(), taken_on: $('photoDate').value, album_id: $('photoAlbum').value }));
      state.viewPhoto = updated;
      state.dirty = false;
      $('fullImage').alt = titleOf(updated);
      $('detailsDialog').close();
      await loadPhotos(false, true);
      $('viewerMessage').textContent = '已保存';
      if (state.index < 0) {
        state.detailBusy = false;
        closePhoto();
        notice('已保存，照片已按新的分类、标题和日期重新排列。');
      }
    } catch (error) { $('detailMessage').textContent = error.message; }
    finally { state.detailBusy = false; $('savePhotoButton').disabled = false; }
  };
  $('favoriteButton').onclick = async () => {
    if (state.detailBusy) return;
    state.detailBusy = true;
    const photo = current();
    $('favoriteButton').disabled = true;
    try {
      const updated = await api(`/photos/${photo.id}`, jsonOptions({ favorite: !photo.favorite }));
      photo.favorite = updated.favorite;
      updateFavorite();
      await loadPhotos(false, true);
      if (state.index < 0) {
        state.detailBusy = false;
        closePhoto();
      }
    } catch (error) { $('viewerMessage').textContent = error.message; }
    finally { state.detailBusy = false; $('favoriteButton').disabled = false; }
  };
  $('removeButton').onclick = () => { if (!state.detailBusy) $('removeDialog').showModal(); };
  $('cancelRemoveButton').onclick = () => { if (!state.detailBusy) $('removeDialog').close(); };
  $('removeDialog').addEventListener('cancel', event => { if (state.detailBusy) event.preventDefault(); });
  $('confirmRemoveButton').onclick = async () => {
    if (state.detailBusy) return;
    state.detailBusy = true;
    $('confirmRemoveButton').disabled = true;
    try {
      await api(`/photos/${current().id}`, { method: 'DELETE' });
      $('removeDialog').close();
      await loadPhotos(false, true);
      state.detailBusy = false;
      closePhoto();
    } catch (error) { $('removeDialog').close(); $('viewerMessage').textContent = error.message; }
    finally { state.detailBusy = false; $('confirmRemoveButton').disabled = false; }
  };

  // 单指左右翻页、下拉关闭；双指交给浏览器缩放，不误触关闭。
  let gesture = null;
  let suppressClickUntil = 0;
  const stage = $('imageStage');
  const resetGesture = () => {
    gesture = null;
    stage.style.transform = '';
    stage.style.opacity = '';
  };
  stage.addEventListener('pointerdown', event => {
    if (!event.isPrimary || event.button !== 0 || (window.visualViewport?.scale || 1) > 1.05) {
      resetGesture();
      return;
    }
    gesture = { id: event.pointerId, x: event.clientX, y: event.clientY };
    stage.setPointerCapture(event.pointerId);
  });
  stage.addEventListener('pointermove', event => {
    if (!gesture || gesture.id !== event.pointerId) return;
    const dx = event.clientX - gesture.x;
    const dy = event.clientY - gesture.y;
    if (dy > 12 && dy > Math.abs(dx) * 1.2) {
      stage.style.transform = `translateY(${dy}px) scale(${Math.max(.8, 1 - dy / 1800)})`;
      stage.style.opacity = String(Math.max(.3, 1 - dy / 700));
    }
  });
  stage.addEventListener('pointerup', event => {
    if (!gesture || gesture.id !== event.pointerId) return;
    const dx = event.clientX - gesture.x;
    const dy = event.clientY - gesture.y;
    resetGesture();
    if (Math.max(Math.abs(dx), Math.abs(dy)) > 12) suppressClickUntil = Date.now() + 400;
    if (dy > 80 && dy > Math.abs(dx) * 1.2) closePhoto();
    else if (Math.abs(dx) > 55 && Math.abs(dx) > Math.abs(dy) * 1.2) stepPhoto(dx < 0 ? 1 : -1);
  });
  stage.addEventListener('pointercancel', resetGesture);
  stage.addEventListener('lostpointercapture', resetGesture);
  stage.onclick = () => {
    if (Date.now() >= suppressClickUntil) setControls(!$('photoDialog').classList.contains('controls-visible'));
  };

  window.handleAlbumBack = () => {
    if (state.moving) return true;
    if ($('moveDialog').open) closeMove();
    else if ($('removeDialog').open) { if (!state.detailBusy) $('removeDialog').close(); }
    else if ($('detailsDialog').open) closeDetails();
    else if ($('photoDialog').open) closePhoto();
    else if ($('uploadDialog').open) { if (!state.uploading) $('uploadDialog').close(); }
    else if (state.selecting) setSelection(false);
    else return false;
    return true;
  };
  window.addEventListener('pagehide', () => setParentImmersive(false));

  function openUpload() {
    setSelection(false);
    $('uploadForm').reset();
    $('uploadStatus').textContent = '';
    $('selectedFiles').textContent = '可一次选择多张照片';
    $('uploadDialog').showModal();
  }
  $('uploadButton').onclick = openUpload;
  $('emptyUploadButton').onclick = openUpload;
  $('cancelUploadButton').onclick = () => { if (!state.uploading) $('uploadDialog').close(); };
  $('uploadDialog').addEventListener('cancel', event => { if (state.uploading) event.preventDefault(); });
  $('fileInput').onchange = () => {
    $('selectedFiles').textContent = Array.from($('fileInput').files).map(file => `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MB`).join('\n') || '可一次选择多张照片';
  };
  $('uploadForm').onsubmit = async event => {
    event.preventDefault();
    if (state.uploading) return;
    const files = Array.from($('fileInput').files);
    if (!files.length) return;
    const takenOn = $('uploadDate').value;
    const albumId = $('uploadAlbum').value;
    state.uploading = true;
    ['confirmUploadButton', 'cancelUploadButton', 'fileInput', 'uploadDate', 'uploadAlbum'].forEach(id => { $(id).disabled = true; });
    let saved = 0;
    const failures = [];
    for (let i = 0; i < files.length; i++) {
      const file = files[i];
      $('uploadStatus').textContent = `正在保存 ${i + 1} / ${files.length}：${file.name}`;
      try {
        if (file.size > 40 * 1024 * 1024) throw new Error('超过 40 MB');
        const data = new FormData();
        data.append('file', file);
        data.append('taken_on', takenOn);
        data.append('album_id', albumId);
        await api('/upload', { method: 'POST', body: data });
        saved++;
      } catch (error) { failures.push(`${file.name}：${error.message}`); }
    }
    state.uploading = false;
    ['confirmUploadButton', 'cancelUploadButton', 'fileInput', 'uploadDate', 'uploadAlbum'].forEach(id => { $(id).disabled = false; });
    $('fileInput').value = '';
    $('selectedFiles').textContent = '可继续选择照片；已成功上传的无需重复选择。';
    if (!failures.length) $('uploadDialog').close();
    else $('uploadStatus').textContent = `成功 ${saved} 张。${failures.join('；')}`;
    await loadPhotos();
    notice(`已收录 ${saved} 张照片${failures.length ? `，${failures.length} 张未成功` : '，原图已保存在本机'}。`);
  };
  document.querySelectorAll('[data-filter]').forEach(button => {
    button.onclick = () => {
      state.filter = button.dataset.filter;
      document.querySelectorAll('[data-filter]').forEach(item => {
        item.classList.toggle('active', item === button);
        item.setAttribute('aria-pressed', String(item === button));
      });
      loadPhotos();
    };
  });
  let searchTimer;
  $('searchInput').oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => loadPhotos(), 250); };
  $('refreshButton').onclick = () => loadPhotos();
  $('loadMoreButton').onclick = () => loadPhotos(true);
  $('backButton').onclick = () => {
    if (window.handleAlbumBack()) return;
    if (window.parent !== window && typeof window.parent.navigateToHome === 'function') window.parent.navigateToHome();
    else location.href = '/';
  };
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && state.selecting && !document.querySelector('dialog[open]')) {
      event.preventDefault(); setSelection(false); return;
    }
    if (!$('photoDialog').open || $('removeDialog').open || $('detailsDialog').open) return;
    if (event.key === 'Tab') setControls(true);
    if ((event.key === 'Enter' || event.key === ' ') && event.target === stage) {
      event.preventDefault(); setControls(!$('photoDialog').classList.contains('controls-visible'));
    }
    if (event.key === 'ArrowLeft') { event.preventDefault(); stepPhoto(-1); }
    if (event.key === 'ArrowRight') { event.preventDefault(); stepPhoto(1); }
  });
  const moreObserver = new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting) && !state.loading && !$('photoDialog').open && !$('moveDialog').open && !state.moving && !state.uploading && state.photos.length < state.total) loadPhotos(true);
  }, { root: $('photoScroll'), rootMargin: '300px' });
  document.addEventListener('visibilitychange', () => {
    // 返回应用时不清空已浏览的页数，新增照片可通过刷新查看。
    if (!document.hidden && !state.photos.length && !$('photoDialog').open && !state.uploading) loadPhotos();
  });
  for (let i = 0; i < 24; i++) $('photoList').append(el('div', 'skeleton'));
  loadPhotos();
})();

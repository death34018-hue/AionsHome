(function (root) {
  'use strict';

  function parseLrc(raw) {
    const result = [];
    const pattern = /\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]/g;
    String(raw || '').split(/\r?\n/).forEach(line => {
      const text = line.replace(pattern, '').trim();
      let match;
      pattern.lastIndex = 0;
      while ((match = pattern.exec(line))) {
        const fraction = match[3] ? Number(`0.${match[3].padEnd(3, '0')}`) : 0;
        result.push({ time: Number(match[1]) * 60 + Number(match[2]) + fraction, text });
      }
    });
    return result.filter(line => line.text).sort((a, b) => a.time - b.time);
  }

  function mergeLyrics(original, translated) {
    const primary = parseLrc(original);
    const translations = parseLrc(translated);
    return primary.map(line => {
      const match = translations.find(item => Math.abs(item.time - line.time) < 0.08);
      return { ...line, translation: match ? match.text : '' };
    });
  }

  function playbackBounds(track, durationSeconds) {
    const duration = Number.isFinite(Number(durationSeconds)) ? Math.max(0, Number(durationSeconds)) : 0;
    const start = Math.max(0, Number(track && track.trim_start_ms || 0) / 1000);
    const savedEnd = Math.max(0, Number(track && track.trim_end_ms || 0) / 1000);
    return { start: Math.min(start, duration || start), end: savedEnd || duration };
  }

  function nextTrackIndex(current, count, repeatMode) {
    if (count <= 0 || current < 0) return -1;
    if (repeatMode === 'one') return current;
    if (current + 1 < count) return current + 1;
    return repeatMode === 'list' ? 0 : -1;
  }

  function setMusicVolume(audioElement, percent) {
    const value = Math.max(0, Math.min(100, Number(percent) || 0));
    audioElement.volume = value / 100;
    return value;
  }

  function trackRowAction(managing) {
    return managing ? 'select' : 'play';
  }

  function afterTracksDeleted(currentTrackId, deletedTrackIds) {
    const shouldStop = Boolean(currentTrackId && deletedTrackIds.includes(currentTrackId));
    return { currentTrackId: shouldStop ? '' : currentTrackId, shouldStop };
  }

  const utils = {
    parseLrc, mergeLyrics, playbackBounds, nextTrackIndex, setMusicVolume,
    trackRowAction, afterTracksDeleted,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = utils;
  root.MusicStationUtils = utils;
  if (typeof document === 'undefined') return;

  const $ = id => document.getElementById(id);
  const state = {
    tracks: [],
    filtered: [],
    currentTrackId: '',
    currentTrackData: null,
    repeatMode: localStorage.getItem('musicStationRepeat') || 'list',
    seeking: false,
    lyricLines: [],
    activeLyric: -1,
    playlists: [],
    activePlaylistId: 'all',
    managing: false,
    selectedTrackIds: new Set(),
  };
  const audio = $('stationAudio');

  async function api(url, options) {
    const response = await fetch(url, options);
    let data = null;
    try { data = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error((data && (data.detail || data.error)) || `请求失败 (${response.status})`);
    return data;
  }

  function notice(message, timeout = 3200) {
    $('notice').textContent = message || '';
    if (message && timeout) setTimeout(() => {
      if ($('notice').textContent === message) $('notice').textContent = '';
    }, timeout);
  }

  function formatTime(seconds) {
    const value = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
    const mins = Math.floor(value / 60);
    return `${mins}:${String(Math.floor(value % 60)).padStart(2, '0')}`;
  }

  function requestNames(track) {
    const names = [];
    (track.requests || []).forEach(item => {
      const name = item.requester_name || item.requester_name_snapshot || 'AI';
      if (!names.includes(name)) names.push(name);
    });
    return names;
  }

  function currentTrack() {
    return state.tracks.find(track => track.id === state.currentTrackId)
      || state.currentTrackData || null;
  }

  function cacheLabel(track) {
    if (track.cache_status === 'cached') return '已缓存';
    if (track.source_type === 'local') return '本地';
    if (track.cache_status === 'failed') return '可在线播放';
    return '缓存中';
  }

  function createTrackCard(track) {
    const card = document.createElement('article');
    card.className = `track-card${track.id === state.currentTrackId ? ' current' : ''}`;
    card.dataset.trackId = track.id;
    card.classList.toggle('managing', state.managing);
    card.classList.toggle('selected', state.selectedTrackIds.has(track.id));
    card.tabIndex = 0;
    card.setAttribute('role', 'button');
    card.setAttribute('aria-label', `播放 ${track.title || '歌曲'}`);

    const cover = track.cover_url ? document.createElement('img') : document.createElement('div');
    cover.className = 'track-cover';
    if (track.cover_url) { cover.src = track.cover_url; cover.alt = ''; cover.loading = 'lazy'; }
    else cover.textContent = '♫';

    const copy = document.createElement('div');
    copy.className = 'track-copy';
    const title = document.createElement('div');
    title.className = 'track-title';
    title.textContent = track.title || '未知歌曲';
    const meta = document.createElement('div');
    meta.className = 'track-meta';
    meta.textContent = [track.artist, track.album].filter(Boolean).join(' · ') || '未知歌手';
    const requesterRow = document.createElement('div');
    requesterRow.className = 'requesters';
    const names = requestNames(track);
    if (names.length) {
      const chip = document.createElement('span');
      chip.className = 'request-chip';
      chip.textContent = `${names.slice(0, 2).join('、')}${names.length > 2 ? ` 等 ${names.length} 位` : ''}点过`;
      requesterRow.appendChild(chip);
    }
    const badge = document.createElement('span');
    badge.className = 'cache-badge';
    badge.textContent = cacheLabel(track);
    requesterRow.appendChild(badge);
    copy.append(title, meta, requesterRow);

    card.append(cover, copy);
    card.onclick = () => {
      if (trackRowAction(state.managing) === 'select') toggleTrackSelection(track.id);
      else selectAndPlay(track.id);
    };
    card.onkeydown = event => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      if (trackRowAction(state.managing) === 'select') toggleTrackSelection(track.id);
      else selectAndPlay(track.id);
    };
    return card;
  }

  function renderTracks() {
    const list = $('trackList');
    list.replaceChildren(...state.filtered.map(createTrackCard));
    $('emptyState').hidden = state.filtered.length !== 0;
    const requestTotal = state.tracks.reduce((sum, track) => sum + Number(track.request_count || 0), 0);
    $('trackSummary').textContent = `${state.tracks.length} 首歌 · ${requestTotal} 次点歌记录`;
  }

  function renderSide(track) {
    const lyricsPanel = $('lyricsPanel');
    state.lyricLines = mergeLyrics(track && track.lyrics_lrc, track && track.translated_lrc);
    state.activeLyric = -1;
    lyricsPanel.replaceChildren();
    if (!track) {
      lyricsPanel.textContent = '选择一首歌查看歌词';
      $('historyPanel').textContent = '选择一首歌查看点歌记录';
      return;
    }
    if (state.lyricLines.length) {
      state.lyricLines.forEach((line, index) => {
        const div = document.createElement('div');
        div.className = 'lyric-line';
        div.dataset.index = String(index);
        div.textContent = line.text;
        if (line.translation) {
          const small = document.createElement('small');
          small.textContent = line.translation;
          div.appendChild(small);
        }
        div.onclick = () => { audio.currentTime = line.time; };
        lyricsPanel.appendChild(div);
      });
    } else {
      const plain = String(track.lyrics_lrc || '').trim();
      lyricsPanel.textContent = plain || '暂无歌词，可以在“编辑”里粘贴 LRC 歌词。';
    }
    const history = $('historyPanel');
    history.replaceChildren();
    if (!(track.requests || []).length) history.textContent = '这是自己上传的歌曲。';
    (track.requests || []).forEach(item => {
      const div = document.createElement('div');
      div.className = 'history-entry';
      const strong = document.createElement('strong');
      strong.textContent = item.requester_name || item.requester_name_snapshot || 'AI';
      const source = document.createElement('span');
      source.textContent = item.source_type === 'private' ? ' 在私聊里点了这首歌' : ' 在聊天室点了这首歌';
      const time = document.createElement('time');
      time.textContent = new Date(item.requested_at * 1000).toLocaleString();
      div.append(strong, source, time);
      history.appendChild(div);
    });
    $('editTitle').value = track.title || '';
    $('editArtist').value = track.artist || '';
    $('editAlbum').value = track.album || '';
    $('editLyrics').value = track.lyrics_lrc || '';
    $('refreshLyricsButton').hidden = track.source_type !== 'netease';
  }

  function updateNowPlaying(track) {
    $('nowTitle').textContent = track ? track.title || '未知歌曲' : '尚未播放';
    $('nowArtist').textContent = track ? track.artist || '未知歌手' : '从歌单里选一首吧';
    const cover = $('nowCover');
    cover.style.backgroundImage = track && track.cover_url ? `url("${String(track.cover_url).replace(/["\\]/g, '')}")` : '';
    cover.textContent = track && track.cover_url ? '' : '♫';
  }

  function selectTrack(trackId, autoplay) {
    const track = state.tracks.find(item => item.id === trackId);
    if (!track) return;
    const changed = state.currentTrackId !== trackId;
    state.currentTrackId = trackId;
    state.currentTrackData = track;
    renderTracks();
    renderSide(track);
    updateNowPlaying(track);
    if (changed) {
      audio.src = `/api/music-station/tracks/${encodeURIComponent(track.id)}/audio`;
      audio.load();
      $('seekBar').value = 0;
      $('currentTime').textContent = '0:00';
      if (track.source_type === 'netease' && track.cache_status !== 'cached') {
        api(`/api/music-station/tracks/${encodeURIComponent(track.id)}/cache`, { method: 'POST' })
          .then(loadTracks).catch(() => {});
      }
      if (track.source_type === 'netease' && !String(track.lyrics_lrc || '').trim()) {
        api(`/api/music-station/tracks/${encodeURIComponent(track.id)}/lyrics/refresh`, { method: 'POST' })
          .then(loadTracks).catch(() => {});
      }
    }
    if (autoplay) audio.play().catch(() => notice('浏览器暂时阻止了自动播放，请再点一次播放。'));
  }

  function selectAndPlay(trackId) {
    if (state.currentTrackId === trackId && !audio.paused) audio.pause();
    else selectTrack(trackId, true);
  }

  function moveTrack(direction) {
    if (!state.filtered.length) return;
    const current = state.filtered.findIndex(track => track.id === state.currentTrackId);
    let next = current + direction;
    if (next < 0) next = state.filtered.length - 1;
    if (next >= state.filtered.length) next = 0;
    selectTrack(state.filtered[next].id, true);
  }

  function finishTrack() {
    const index = state.filtered.findIndex(track => track.id === state.currentTrackId);
    const next = nextTrackIndex(index, state.filtered.length, state.repeatMode);
    if (next >= 0) selectTrack(state.filtered[next].id, true);
    else { audio.pause(); audio.currentTime = playbackBounds(currentTrack(), audio.duration).start; }
  }

  function updateProgress() {
    const track = currentTrack();
    if (!track || !Number.isFinite(audio.duration)) return;
    const bounds = playbackBounds(track, audio.duration);
    if (audio.currentTime < bounds.start - .2) audio.currentTime = bounds.start;
    if (bounds.end && audio.currentTime >= bounds.end - .04) { finishTrack(); return; }
    if (!state.seeking) $('seekBar').value = String((audio.currentTime / audio.duration) * 1000 || 0);
    $('currentTime').textContent = formatTime(audio.currentTime);
    $('durationTime').textContent = formatTime(bounds.end || audio.duration);
    let active = -1;
    state.lyricLines.forEach((line, index) => { if (line.time <= audio.currentTime + .03) active = index; });
    if (active !== state.activeLyric) {
      state.activeLyric = active;
      document.querySelectorAll('.lyric-line').forEach(line => line.classList.remove('active'));
      const element = document.querySelector(`.lyric-line[data-index="${active}"]`);
      if (element) { element.classList.add('active'); element.scrollIntoView({ block: 'center', behavior: 'smooth' }); }
    }
  }

  async function loadTracks() {
    try {
      const selected = state.currentTrackId;
      const query = state.activePlaylistId === 'all'
        ? '' : `?playlist_id=${encodeURIComponent(state.activePlaylistId)}`;
      const data = await api(`/api/music-station/tracks${query}`);
      state.tracks = Array.isArray(data.tracks) ? data.tracks : [];
      applyFilter();
      if (selected && state.tracks.some(track => track.id === selected)) {
        state.currentTrackId = selected;
        state.currentTrackData = state.tracks.find(track => track.id === selected) || state.currentTrackData;
        renderSide(currentTrack());
      }
    } catch (error) {
      notice(error.message, 0);
    }
  }

  function applyFilter() {
    const query = $('searchInput').value.trim().toLocaleLowerCase();
    state.filtered = state.tracks.filter(track => {
      const haystack = [track.title, track.artist, track.album, ...requestNames(track)].join(' ').toLocaleLowerCase();
      return !query || haystack.includes(query);
    });
    renderTracks();
  }

  async function uploadFiles(files) {
    for (const file of Array.from(files || [])) {
      const form = new FormData();
      form.append('file', file);
      notice(`正在导入 ${file.name}…`, 0);
      try { await api('/api/music-station/upload', { method: 'POST', body: form }); }
      catch (error) { notice(`${file.name}：${error.message}`, 0); }
    }
    await Promise.all([loadTracks(), loadPlaylists()]);
    notice('本地歌曲已导入');
  }

  function showSidePanel(panelId) {
    document.querySelectorAll('.side-tab').forEach(button => button.classList.toggle('active', button.dataset.panel === panelId));
    document.querySelectorAll('.side-content').forEach(panel => { panel.hidden = panel.id !== panelId; });
    if (window.innerWidth < 851) setMobileView('lyrics');
  }

  function setMobileView(view) {
    const next = view === 'lyrics' ? 'lyrics' : 'library';
    document.body.classList.toggle('view-library', next === 'library');
    document.body.classList.toggle('view-lyrics', next === 'lyrics');
    document.querySelectorAll('.mobile-view-switch button').forEach(button => {
      const active = button.dataset.view === next;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
  }

  function activePlaylist() {
    return state.playlists.find(item => item.id === state.activePlaylistId) || null;
  }

  function renderPlaylists() {
    const tabs = $('playlistTabs');
    const entries = [{ id: 'all', name: '全部歌曲', track_count: null }, ...state.playlists];
    tabs.replaceChildren(...entries.map(item => {
      const button = document.createElement('button');
      button.type = 'button';
      button.classList.toggle('active', item.id === state.activePlaylistId);
      button.textContent = item.track_count == null ? item.name : `${item.name} ${item.track_count}`;
      button.onclick = () => selectPlaylist(item.id);
      return button;
    }));
    $('playlistSettingsButton').hidden = state.activePlaylistId === 'all';
    $('removeFromPlaylistButton').hidden = state.activePlaylistId === 'all';
    const select = $('playlistTargetSelect');
    const previous = select.value;
    select.replaceChildren(...state.playlists.map(item => {
      const option = document.createElement('option');
      option.value = item.id;
      option.textContent = item.name;
      return option;
    }));
    if (state.playlists.some(item => item.id === previous)) select.value = previous;
    $('addToPlaylistButton').disabled = state.playlists.length === 0;
  }

  async function loadPlaylists() {
    try {
      const data = await api('/api/music-station/playlists');
      state.playlists = Array.isArray(data.playlists) ? data.playlists : [];
      if (state.activePlaylistId !== 'all'
          && !state.playlists.some(item => item.id === state.activePlaylistId)) {
        state.activePlaylistId = 'all';
      }
      renderPlaylists();
    } catch (error) { notice(error.message, 0); }
  }

  async function selectPlaylist(playlistId) {
    state.activePlaylistId = playlistId || 'all';
    state.selectedTrackIds.clear();
    renderPlaylists();
    updateManageUi();
    await loadTracks();
  }

  function setManaging(enabled) {
    state.managing = Boolean(enabled);
    state.selectedTrackIds.clear();
    $('manageButton').textContent = state.managing ? '完成' : '管理';
    $('manageActions').hidden = !state.managing;
    updateManageUi();
    renderTracks();
  }

  function toggleTrackSelection(trackId) {
    if (state.selectedTrackIds.has(trackId)) state.selectedTrackIds.delete(trackId);
    else state.selectedTrackIds.add(trackId);
    updateManageUi();
    renderTracks();
  }

  function updateManageUi() {
    const count = state.selectedTrackIds.size;
    $('selectedCount').textContent = `已选 ${count} 首`;
    ['addToPlaylistButton', 'removeFromPlaylistButton', 'deleteSelectedButton'].forEach(id => {
      $(id).disabled = count === 0 || (id === 'addToPlaylistButton' && state.playlists.length === 0);
    });
  }

  function openPlaylistDialog(mode) {
    const playlist = activePlaylist();
    $('playlistDialog').dataset.mode = mode;
    $('playlistDialogTitle').textContent = mode === 'rename' ? '重命名歌单' : '新建歌单';
    $('playlistNameInput').value = mode === 'rename' && playlist ? playlist.name : '';
    $('playlistDialog').showModal();
    setTimeout(() => $('playlistNameInput').focus(), 0);
  }

  async function savePlaylist() {
    const name = $('playlistNameInput').value.trim();
    const mode = $('playlistDialog').dataset.mode;
    const playlist = activePlaylist();
    try {
      if (mode === 'rename' && playlist) {
        await api(`/api/music-station/playlists/${encodeURIComponent(playlist.id)}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name }),
        });
      } else {
        const created = await api('/api/music-station/playlists', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name }),
        });
        state.activePlaylistId = created.id;
      }
      $('playlistDialog').close();
      await loadPlaylists();
      await loadTracks();
    } catch (error) { notice(error.message, 0); }
  }

  async function deleteActivePlaylist() {
    const playlist = activePlaylist();
    if (!playlist || !window.confirm(`删除歌单“${playlist.name}”？歌曲会保留在全部歌曲中。`)) return;
    try {
      await api(`/api/music-station/playlists/${encodeURIComponent(playlist.id)}`, { method: 'DELETE' });
      state.activePlaylistId = 'all';
      $('playlistMenu').hidden = true;
      await loadPlaylists();
      await loadTracks();
    } catch (error) { notice(error.message, 0); }
  }

  function selectedTrackIds() {
    return Array.from(state.selectedTrackIds);
  }

  async function addSelectedToPlaylist() {
    const playlistId = $('playlistTargetSelect').value;
    if (!playlistId) return notice('请先新建一个歌单');
    try {
      await api(`/api/music-station/playlists/${encodeURIComponent(playlistId)}/tracks`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ track_ids: selectedTrackIds() }),
      });
      state.selectedTrackIds.clear();
      updateManageUi(); renderTracks(); await loadPlaylists(); notice('已加入歌单');
    } catch (error) { notice(error.message, 0); }
  }

  async function removeSelectedFromPlaylist() {
    if (state.activePlaylistId === 'all') return;
    try {
      await api(`/api/music-station/playlists/${encodeURIComponent(state.activePlaylistId)}/tracks`, {
        method: 'DELETE', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ track_ids: selectedTrackIds() }),
      });
      state.selectedTrackIds.clear();
      await loadPlaylists(); await loadTracks(); updateManageUi(); notice('已移出歌单');
    } catch (error) { notice(error.message, 0); }
  }

  function clearCurrentPlayer() {
    audio.pause();
    audio.removeAttribute('src');
    audio.load();
    state.currentTrackData = null;
    updateNowPlaying(null);
    renderSide(null);
    $('seekBar').value = '0';
    $('currentTime').textContent = '0:00';
    $('durationTime').textContent = '0:00';
  }

  async function deleteSelectedTracks() {
    const ids = selectedTrackIds();
    try {
      const result = await api('/api/music-station/tracks', {
        method: 'DELETE', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ track_ids: ids }),
      });
      const next = afterTracksDeleted(state.currentTrackId, ids);
      state.currentTrackId = next.currentTrackId;
      if (next.shouldStop) clearCurrentPlayer();
      state.selectedTrackIds.clear();
      $('deleteTracksDialog').close();
      await loadPlaylists(); await loadTracks(); updateManageUi();
      notice(result.warnings && result.warnings.length ? '歌曲已删除，个别文件清理失败' : '歌曲已删除');
    } catch (error) { notice(error.message, 0); }
  }

  function bindEvents() {
    $('backButton').onclick = () => {
      if (window.parent !== window && typeof window.parent.navigateToHome === 'function') window.parent.navigateToHome();
      else location.href = '/';
    };
    $('uploadButton').onclick = () => $('audioInput').click();
    $('audioInput').onchange = event => uploadFiles(event.target.files);
    $('refreshButton').onclick = () => Promise.all([loadTracks(), loadPlaylists()]);
    $('manageButton').onclick = () => setManaging(!state.managing);
    $('newPlaylistButton').onclick = () => openPlaylistDialog('create');
    $('playlistSettingsButton').onclick = () => {
      $('playlistMenu').hidden = !$('playlistMenu').hidden;
    };
    $('renamePlaylistButton').onclick = () => { $('playlistMenu').hidden = true; openPlaylistDialog('rename'); };
    $('deletePlaylistButton').onclick = deleteActivePlaylist;
    $('savePlaylistButton').onclick = savePlaylist;
    $('playlistNameInput').onkeydown = event => {
      if (event.key === 'Enter') { event.preventDefault(); savePlaylist(); }
    };
    $('addToPlaylistButton').onclick = addSelectedToPlaylist;
    $('removeFromPlaylistButton').onclick = removeSelectedFromPlaylist;
    $('deleteSelectedButton').onclick = () => {
      const count = state.selectedTrackIds.size;
      if (!count) return;
      $('deleteTracksMessage').textContent = `确定删除选中的 ${count} 首歌吗？歌曲文件和全部点歌记录也会一并删除。`;
      $('deleteTracksDialog').showModal();
    };
    $('confirmDeleteTracksButton').onclick = deleteSelectedTracks;
    $('searchInput').oninput = applyFilter;
    $('libraryViewButton').onclick = () => setMobileView('library');
    $('lyricsViewButton').onclick = () => { showSidePanel('lyricsPanel'); setMobileView('lyrics'); };
    const dropZone = $('dropZone');
    ['dragenter', 'dragover'].forEach(type => dropZone.addEventListener(type, event => { event.preventDefault(); dropZone.classList.add('dragging'); }));
    ['dragleave', 'drop'].forEach(type => dropZone.addEventListener(type, event => { event.preventDefault(); dropZone.classList.remove('dragging'); }));
    dropZone.addEventListener('drop', event => uploadFiles(event.dataTransfer.files));
    $('playButton').onclick = () => {
      if (!currentTrack() && state.filtered.length) selectTrack(state.filtered[0].id, true);
      else if (audio.paused) audio.play().catch(() => notice('无法播放这首歌'));
      else audio.pause();
    };
    $('previousButton').onclick = () => moveTrack(-1);
    $('nextButton').onclick = () => moveTrack(1);
    $('repeatButton').onclick = () => {
      const modes = ['list', 'one', 'none'];
      state.repeatMode = modes[(modes.indexOf(state.repeatMode) + 1) % modes.length];
      localStorage.setItem('musicStationRepeat', state.repeatMode);
      updateRepeatButton();
    };
    const volume = setMusicVolume(audio, localStorage.getItem('musicStationVolume') || 50);
    $('volumeBar').value = String(volume);
    $('volumeButton').onclick = () => {
      const panel = $('volumePanel');
      panel.hidden = !panel.hidden;
      $('volumeButton').setAttribute('aria-expanded', String(!panel.hidden));
    };
    $('volumeBar').oninput = event => {
      const next = setMusicVolume(audio, event.target.value);
      localStorage.setItem('musicStationVolume', String(next));
      $('volumeButton').textContent = next === 0 ? '🔇' : next < 50 ? '🔈' : '🔉';
    };
    $('seekBar').onpointerdown = () => { state.seeking = true; };
    $('seekBar').oninput = event => {
      if (Number.isFinite(audio.duration)) $('currentTime').textContent = formatTime(Number(event.target.value) / 1000 * audio.duration);
    };
    $('seekBar').onchange = event => {
      if (Number.isFinite(audio.duration)) {
        const bounds = playbackBounds(currentTrack(), audio.duration);
        const wanted = Number(event.target.value) / 1000 * audio.duration;
        audio.currentTime = Math.max(bounds.start, Math.min(bounds.end || audio.duration, wanted));
      }
      state.seeking = false;
    };
    audio.onloadedmetadata = () => {
      const bounds = playbackBounds(currentTrack(), audio.duration);
      if (audio.currentTime < bounds.start) audio.currentTime = bounds.start;
      $('durationTime').textContent = formatTime(bounds.end || audio.duration);
    };
    audio.ontimeupdate = updateProgress;
    audio.onended = finishTrack;
    audio.onplay = () => { $('playButton').textContent = 'Ⅱ'; renderTracks(); };
    audio.onpause = () => { $('playButton').textContent = '▶'; renderTracks(); };
    audio.onerror = () => notice('这首歌暂时无法播放，可以稍后重试。', 0);
    document.querySelectorAll('.side-tab').forEach(button => { button.onclick = () => showSidePanel(button.dataset.panel); });
    $('lyricsButton').onclick = () => showSidePanel('lyricsPanel');
    $('trimButton').onclick = () => {
      const track = currentTrack(); if (!track) return notice('请先选择一首歌');
      $('trimStart').value = String(Number(track.trim_start_ms || 0) / 1000);
      $('trimEnd').value = String(Number(track.trim_end_ms || 0) / 1000);
      $('trimDialog').showModal();
    };
    $('saveTrimButton').onclick = saveTrim;
    $('saveMetadataButton').onclick = saveMetadata;
    $('refreshLyricsButton').onclick = refreshLyrics;
  }

  function updateRepeatButton() {
    const labels = { list: ['↻', '列表循环'], one: ['↻¹', '单曲循环'], none: ['→', '播完停止'] };
    const [text, title] = labels[state.repeatMode] || labels.list;
    $('repeatButton').textContent = text; $('repeatButton').title = title;
  }

  async function saveTrim() {
    const track = currentTrack(); if (!track) return;
    try {
      await api(`/api/music-station/tracks/${encodeURIComponent(track.id)}/trim`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start_ms: Math.round(Number($('trimStart').value || 0) * 1000), end_ms: Math.round(Number($('trimEnd').value || 0) * 1000) }),
      });
      $('trimDialog').close(); await loadTracks(); notice('播放片段已保存');
    } catch (error) { notice(error.message, 0); }
  }

  async function saveMetadata() {
    const track = currentTrack(); if (!track) return;
    try {
      await api(`/api/music-station/tracks/${encodeURIComponent(track.id)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: $('editTitle').value, artist: $('editArtist').value, album: $('editAlbum').value, lyrics_lrc: $('editLyrics').value }),
      });
      await loadTracks(); notice('歌曲信息已保存');
    } catch (error) { notice(error.message, 0); }
  }

  async function refreshLyrics() {
    const track = currentTrack(); if (!track) return;
    try { await api(`/api/music-station/tracks/${encodeURIComponent(track.id)}/lyrics/refresh`, { method: 'POST' }); await loadTracks(); notice('歌词已刷新'); }
    catch (error) { notice(error.message, 0); }
  }

  bindEvents();
  updateRepeatButton();
  renderSide(null);
  updateManageUi();
  loadPlaylists().then(loadTracks);
})(typeof globalThis !== 'undefined' ? globalThis : this);

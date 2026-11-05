(() => {
  'use strict';

  let pieces = [];
  let actor = 'connor';
  let actorChosen = false;
  let favoritesOnly = false;
  let openedLinkedScore = false;
  const tabs = [...document.querySelectorAll('[data-actor]')];
  const favorites = document.getElementById('musicBoxFavorites');
  const list = document.getElementById('musicBoxList');
  const notice = document.getElementById('musicBoxNotice');
  const empty = document.getElementById('musicBoxEmpty');
  const selected = new URLSearchParams(location.search).get('piece');

  function render() {
    if (!actorChosen) {
      actor = pieces.find(piece => piece.id === selected)?.actor || pieces[0]?.actor || actor;
    }
    const names = CompositionCache.getAuthors?.() || {};
    tabs.forEach(tab => {
      const role = tab.dataset.actor;
      const name = names[role] || pieces.find(piece => piece.actor === role)?.actor_name || (role === 'aion' ? 'AI' : '另一位 AI');
      tab.textContent = name;
      tab.title = name;
      tab.tabIndex = role === actor ? 0 : -1;
      tab.setAttribute('aria-selected', String(role === actor));
    });
    document.getElementById('musicBoxPanel').setAttribute('aria-labelledby', 'tab-' + actor);
    const visible = pieces.filter(piece => piece.actor === actor && (!favoritesOnly || piece.favorite));
    const expanded = new Set([...list.querySelectorAll('.composition-visual[open]')].map(details => details.closest('.composition-card').dataset.compositionId));
    list.innerHTML = visible.map(piece => CompositionCards.render([{...piece, type: 'composition'}])).join('');
    document.getElementById('musicBoxCount').textContent = `${visible.length} 首`;
    empty.hidden = visible.length > 0;
    empty.querySelector('a').hidden = favoritesOnly || pieces.length > 0;
    if (favoritesOnly && !visible.length) {
      empty.querySelector('h2').textContent = '把喜欢的小曲留下来';
      empty.querySelector('p').textContent = '点作品右上角的爱心，就会出现在这里。';
    } else if (!visible.length) {
      empty.querySelector('h2').textContent = '等一首写给你的小曲';
      empty.querySelector('p').textContent = '在聊天里点一首曲子，写好后就会收在这里。';
    }
    for (const card of list.querySelectorAll('.composition-card')) {
      const linked = !openedLinkedScore && card.dataset.compositionId === selected;
      if (linked || expanded.has(card.dataset.compositionId)) card.querySelector('.composition-visual').open = true;
      if (linked) openedLinkedScore = true;
    }
    CompositionCards.refresh();
  }

  async function load() {
    const cached = CompositionCache.getCachedList();
    if (cached) {
      pieces = cached;
      notice.textContent = '';
      render();
    }
    try {
      pieces = await CompositionCache.loadList();
      notice.textContent = '';
      render();
    } catch (error) {
      notice.textContent = cached?.length ? '网络不太稳定，先听已缓存的小曲。' : error.message;
    }
  }

  tabs.forEach((tab, index) => {
    tab.onclick = () => {
      actor = tab.dataset.actor;
      actorChosen = true;
      render();
    };
    tab.onkeydown = event => {
      if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs.at(-1) : tabs[1-index];
      next.focus();
      next.click();
    };
  });
  favorites.onclick = () => {
    favoritesOnly = !favoritesOnly;
    favorites.setAttribute('aria-pressed', String(favoritesOnly));
    favorites.querySelector('span').textContent = favoritesOnly ? '♥' : '♡';
    render();
  };
  const guide = document.getElementById('soundGuide');
  document.getElementById('musicBoxGuide').onclick = () => guide.showModal();
  guide.onclick = event => {
    if (event.target !== guide) return;
    const rect = guide.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) guide.close();
  };
  document.getElementById('musicBoxBack').onclick = () => {
    if (typeof window.closeSubPage === 'function') window.closeSubPage();
    else if (window.parent !== window && typeof window.parent.closeSubPage === 'function') window.parent.closeSubPage();
    else location.href = '/';
  };
  document.addEventListener('composition-favorite-changed', event => {
    pieces = pieces.map(piece => piece.id === event.detail.piece.id ? event.detail.piece : piece);
    render();
  });
  window.addEventListener('pageshow', load);
})();

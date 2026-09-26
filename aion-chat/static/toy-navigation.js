(function (root) {
  'use strict';

  const STORAGE_KEY = 'aion_toy_last_profile';
  const DESTINATIONS = Object.freeze({
    sosexy: '/toys/sosexy',
    svakom: '/toys/svakom',
    ankni: '/toys/ankni',
  });

  function destination(profile) {
    return DESTINATIONS[profile] || '/toys';
  }

  function remember(profile) {
    if (!DESTINATIONS[profile]) return;
    try { root.localStorage.setItem(STORAGE_KEY, profile); }
    catch (error) { root.console?.warn?.(`无法记住玩具选择: ${error.message}`); }
  }

  function navigate(profile) {
    if (DESTINATIONS[profile]) remember(profile);
    try {
      if (root.parent && root.parent && root.parent !== root && typeof root.parent.openSubPage === 'function') {
        root.parent.openSubPage(destination(profile));
        return;
      }
    } catch (error) { root.console?.warn?.(`无法使用小家导航: ${error.message}`); }
    root.location.href = destination(profile);
  }

  async function readSelection() {
    const response = await root.fetch('/api/toys/selection', {cache:'no-store'});
    if (!response.ok) throw new Error('玩具选择读取失败');
    return response.json();
  }
  async function select(profile) {
    if (!DESTINATIONS[profile]) throw new Error('请选择玩具');
    if (root.parent && root.parent !== root && typeof root.parent.stopAllToyControllers === 'function') await root.parent.stopAllToyControllers();
    else if (typeof root.stopAndDisconnectToy === 'function') await root.stopAndDisconnectToy();
    const response = await root.fetch('/api/toys/selection', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({profile})});
    if (!response.ok) throw new Error('玩具选择保存失败，请重试');
    const saved = await response.json();
    if (saved.profile !== profile) throw new Error('玩具选择未保存');
    navigate(profile);
  }
  function showError(error) {
    let notice = document.getElementById('toySelectionError');
    if (!notice) {notice=document.createElement('p');notice.id='toySelectionError';notice.setAttribute('role','alert');document.querySelector('main')?.prepend(notice);}
    notice.textContent=error.message;
  }
  function chooser() {
    if (root.parent && root.parent !== root && typeof root.parent.openSubPage === 'function') root.parent.openSubPage('/toys?choose=1');
    else root.location.href='/toys?choose=1';
  }
  async function switchTo(profile) {
    if (DESTINATIONS[profile]) return select(profile);
    if (typeof root.stopAndDisconnectToy === 'function') await root.stopAndDisconnectToy();
    chooser();
  }

  function returnToChat() {
    try {
      if (root.parent && root.parent && root.parent !== root && typeof root.parent.returnFromToyControls === 'function') {
        root.parent.returnFromToyControls();
        return;
      }
    } catch (error) { root.console?.warn?.(`无法返回聊天: ${error.message}`); }
    navigate('');
  }

  function bindPage() {
    document.querySelectorAll('[data-toy-profile]').forEach(button => {
      button.addEventListener('click', async () => {button.disabled=true;try {await select(button.dataset.toyProfile);} catch(error){showError(error);} finally{button.disabled=false;}});
    });
    document.querySelectorAll('[data-switch-profile]').forEach(button => {
      button.addEventListener('click', async () => {
        button.disabled = true;
        try { await switchTo(button.dataset.switchProfile); }
        catch(error) { showError(error); }
        finally { button.disabled = false; }
      });
    });
    document.querySelectorAll('[data-toy-return]').forEach(link => {
      link.addEventListener('click', event => { event.preventDefault(); returnToChat(); });
    });
  }

  root.ToyNavigation = Object.freeze({
    key: STORAGE_KEY,
    open: navigate,
    switchTo,
    destination, select, readSelection,
  });

  async function initializeSelection() {
    if (!document.body?.classList.contains('toy-page') || !root.fetch) return;
    const profile = Object.keys(DESTINATIONS).find(key => document.body.classList.contains('toy-' + key + '-page'));
    const current = await readSelection();
    if (document.body.classList.contains('toy-chooser-page')) {
      if (current.profile && !new URLSearchParams(root.location.search).has('choose')) navigate(current.profile);
    } else if (profile && current.profile && current.profile !== profile) {
      await root.stopAndDisconnectToy?.(); navigate(current.profile);
    }
    return current;
  }
  root.ToySelectionReady = initializeSelection().catch(error => {showError(error);return null;});
  // Retained control pages must stop even when selection changes on another client.
  if (root.WebSocket && document.body?.classList.contains('toy-control-page')) {
    const profile = Object.keys(DESTINATIONS).find(key => document.body.classList.contains('toy-' + key + '-page'));
    function listen() {
      const socket = new root.WebSocket((root.location.protocol === 'https:' ? 'wss://' : 'ws://') + root.location.host + '/ws');
      async function check(value) {
        if (profile && value.active !== profile) await root.stopAndDisconnectToy?.();
      }
      socket.onopen=()=>readSelection().then(check).catch(showError);
      socket.onmessage=event=>{try {const message=JSON.parse(event.data);if(message.type==='toy_profile_changed') check(message.data).catch(showError);}catch(error){showError(error);}};
      socket.onclose=()=>root.setTimeout(listen,2000);
    }
    listen();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bindPage, { once: true });
  } else {
    bindPage();
  }
})(window);

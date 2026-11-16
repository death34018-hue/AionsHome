// Private/group chat share connection health checks without duplicating sockets.
function createSocketRecovery({ getSocket, connect, onDisconnect }) {
  let retryTimer = null, healthTimer = null, probeTimer = null;
  let retryDelay = 1000;
  let trackedSocket = null;
  function finishConnection() {
    if (!trackedSocket) return;
    const old = trackedSocket;
    trackedSocket = null;
    onDisconnect?.(old);
  }
  const current = socket => socket && socket === getSocket();
  function clearHealth() {
    clearTimeout(healthTimer); clearTimeout(probeTimer);
    healthTimer = probeTimer = null;
  }
  function retry() {
    if (retryTimer !== null) return;
    const delay = retryDelay;
    retryDelay = Math.min(retryDelay * 2, 30000);
    retryTimer = setTimeout(() => {
      retryTimer = null;
      const socket = getSocket();
      if (!socket || socket.readyState > WebSocket.OPEN) connect();
    }, delay);
  }
  function replace(socket) {
    if (!current(socket)) return;
    clearHealth();
    finishConnection();
    try { socket.close(); } catch (_) {}
    // A dead transport may never deliver onclose, so do not wait for it.
    retry();
  }
  function check() {
    const socket = getSocket();
    if (!socket || socket.readyState > WebSocket.OPEN) {
      clearTimeout(retryTimer); retryTimer = null;
      connect();
    } else if (socket.readyState === WebSocket.OPEN && probeTimer === null) {
      clearTimeout(healthTimer); healthTimer = null;
      probeTimer = setTimeout(() => replace(socket), 15000);
      try { socket.send(JSON.stringify({ type: 'ping' })); }
      catch (_) { replace(socket); }
    }
  }
  function received(socket) {
    if (!current(socket) || trackedSocket !== socket) return false;
    clearHealth();
    healthTimer = setTimeout(check, 20000);
    return true;
  }
  return {
    connecting(socket) {
      finishConnection();
      trackedSocket = socket;
      clearTimeout(retryTimer); retryTimer = null;
      clearHealth();
      healthTimer = setTimeout(() => replace(socket), 20000);
    },
    opened(socket) {
      if (!current(socket)) return;
      retryDelay = 1000;
      received(socket);
    },
    received,
    closed(socket) {
      if (!current(socket)) return;
      finishConnection();
      clearHealth(); retry();
    },
    check,
  };
}


(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.LoungeVisitUI = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {
  function isStatusMessage(message) {
    return Array.isArray(message && message.attachments)
      && message.attachments.some(item => item && item.type === 'lounge_visit_status');
  }

  function reportTitle(actorName, partnerName, direction, status) {
    const actor = String(actorName || 'AI');
    const partner = String(partnerName || '朋友');
    if (direction === 'inbound') return `${actor} 刚刚接待了访客 ${partner}。`;
    if (status === 'interrupted') return `${actor} 去 ${partner}那里串门时中断了。`;
    if (status === 'rejected') return `${actor} 这次没能前往拜访 ${partner}。`;
    return `${actor} 刚刚去 ${partner}那里串门回来了。`;
  }

  const TERMINAL_REASON_TEXT = {
    network_reconnect_failed: '网络连接中断，自动重连后仍未恢复。',
    request_timeout: '连接会客室超时，本次会面已结束。',
    generation_failed_after_retries: '对方连续三次未能生成回复，本次会面已中断。',
    prompt_budget_exceeded: '本次回复所需上下文超过会客室容量限制。',
    response_too_long: '对方生成的回复超过长度限制，无法正常送达。',
    lounge_closed: '对方会客室已关闭。',
    quota_exhausted: '对方本时段的接待额度已用完。',
    user_cancelled: '本次拜访已由用户取消。',
    service_restarted: '服务重启导致本次会面提前结束。',
    repository_failed: '会面记录保存失败，本次会面已安全结束。',
    remote_protocol_error: '双方通信协议出现异常，本次会面已结束。',
    unexpected_failure: '会客室发生未知异常；详细诊断信息已保留在本机日志中。',
    visitor_locked: '对方当前处于安全锁定状态，本次会面未能继续。',
    visitor_paused: '对方当前暂停接待，本次会面未能继续。',
    visitor_busy: '对方正在处理另一场会面，本次会面已中断。',
    service_busy: '对方会客室当前繁忙，本次会面已中断。',
    request_conflict: '本次消息与另一项请求发生冲突，会面已中断。',
    friend_not_found: '没有找到这位好友，本次拜访未能开始。',
    local_state_failed: '本地好友状态读取失败，本次会面已结束。',
    invalid_trigger_source: '本次拜访的发起方式无效，未能开始。',
    invalid_topic: '本次拜访的话题格式无效，未能开始。',
    friend_disabled: '这位好友当前已停用，无法开始拜访。',
    identity_name_unavailable: '未能取得访客名称，本次拜访未能开始。',
    unsupported_server: '对方会客室版本不兼容，本次会面已结束。',
    invalid_message: '本次生成的消息为空或格式无效，无法继续会面。',
    message_too_long: '本次消息超过会客室长度限制，无法正常送达。',
    identity_unclaimed: '访客身份尚未确认，本次拜访未能开始。',
    consent_required: '尚未同意对方的来访记录说明，本次拜访未能开始。',
    invalid_name: '访客名称不符合对方会客室要求，本次拜访未能开始。',
    credential_rejected: '本次消息包含敏感凭据，已被对方会客室拒绝。',
    invalid_request_id: '本次会面请求标识无效，无法继续。',
    connection_failed: '网络连接中断，自动重连后仍未恢复。',
    visit_timeout: '连接会客室超时，本次会面已结束。',
    cancelled: '本次拜访已由用户取消。',
    restart_recovery: '服务重启导致本次会面提前结束。',
  };

  function interruptionReasonText(reason) {
    const raw = String(reason || '');
    const code = Object.keys(TERMINAL_REASON_TEXT).find(key => raw.includes(key));
    return code ? TERMINAL_REASON_TEXT[code] : TERMINAL_REASON_TEXT.unexpected_failure;
  }

  function reportMeta(status, turnCount, reason) {
    const turns = Math.max(0, Number(turnCount) || 0);
    const reasonText = interruptionReasonText(reason);
    if (status === 'rejected') return `未能开始拜访 · ${reasonText}`;
    if (status === 'interrupted') return `中断前聊了 ${turns} 回合 · ${reasonText}`;
    return `共聊了 ${turns} 回合`;
  }

  return { interruptionReasonText, isStatusMessage, reportTitle, reportMeta };
});

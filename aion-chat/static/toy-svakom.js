(function (root, factory) {
  const protocol = typeof module === 'object' && module.exports
    ? require('./toy-svakom-protocol.js')
    : root.SvakomProtocol;
  const api = factory(protocol, root, typeof document !== 'undefined' ? document : null);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) Object.assign(root, api);
})(typeof window !== 'undefined' ? window : globalThis, function (protocol, root, doc) {
  'use strict';

  // Descriptive labels from the user's reference; not measured frequency values.
  const STRETCH_MODES = Object.freeze([
    ['慢速旋转伸缩', '慢速连续'],
    ['中速旋转伸缩', '中速连续'],
    ['三短一长', '短短短 · 长'],
    ['混合变速', '快慢交替'],
    ['单次停顿脉冲', '动作与停顿交替'],
    ['快速停顿脉冲', '快速断续'],
    ['连续短脉冲', '连续短促'],
  ]);
  const VIBRATE_MODES = Object.freeze([
    ['高频连续波', '连续而规律'],
    ['轻柔细振波', '细腻均匀'],
    ['渐强波', '由弱至强'],
    ['慢速起伏波', '缓慢周期变化'],
    ['间歇脉冲', '强弱交替停顿'],
    ['快速锯齿波', '密集短促'],
    ['中速锯齿波', '幅度更明显'],
    ['阶梯脉冲', '分段递进'],
    ['高速方波脉冲', '原厂模式 9'],
    ['低速方波脉冲', '原厂模式 10'],
  ]);
  const FLAP_MODES = Object.freeze(Array.from({ length: 7 }, (_, index) =>
    [`${index + 1} 档`, index === 0 ? '最弱 · 建议先试' : index === 6 ? '最强' : '逐档增强']));

  function withDuration(command, seconds) {
    const raw = String(command || '').trim().toUpperCase();
    const duration = Number(seconds);
    if (!Number.isInteger(duration) || duration < 1 || duration > 3600) return raw;
    if (/^(?:STRETCH:[1-7]|VIBRATE:(?:10|[1-9])(?::(?:10|[1-9]))?|FLAP:[1-7]|HEAT:ON)$/.test(raw)) return `${raw}|${duration}`;
    return raw;
  }

  function buildTimeline(phases) {
    if (!Array.isArray(phases) || !phases.length || phases.length > 63) throw new Error('请添加 1～63 段');
    let seconds = 0;
    const events = phases.map(phase => {
      const duration = Number(phase.seconds);
      if (!Number.isInteger(duration) || duration < 1 || seconds + duration > 3600) throw new Error('每段至少 1 秒，总时长最多 3600 秒');
      const actions = [];
      for (const channel of ['stretch', 'vibrate', 'flap']) {
        const value = String(phase[channel] ?? 'KEEP').toUpperCase();
        if (value === 'KEEP') continue;
        const suffix = channel === 'vibrate' && value !== 'OFF' ? `:${phase.level ?? 2}` : '';
        actions.push(`${channel.toUpperCase()}:${value}${suffix}`);
      }
      if (!actions.length) throw new Error('每段至少设置一路动作，其余可选择保持');
      const action = actions.join('+');
      const entry = `${seconds}=${action}`;
      seconds += duration;
      return entry;
    });
    const command = `SEQ:${events.join(';')};${seconds}=STOP`;
    const result = protocol.parse(command);
    if (!result.ok) throw new Error(result.error);
    return { command, seconds };
  }

  // Limit drag traffic and retain only the latest value while a write is in flight.
  function bindVibrationSlider(slider, output, controller, onError, scheduler = root) {
    let timer = null;
    let writing = false;
    let pending = false;
    async function flush() {
      if (timer !== null) scheduler.clearTimeout(timer);
      timer = null;
      if (writing || !pending) return;
      pending = false;
      writing = true;
      try { await controller.updateVibrationLevel(Number(slider.value)); }
      catch (error) { onError(error); }
      finally {
        writing = false;
        if (pending && timer === null) timer = scheduler.setTimeout(flush, 120);
      }
    }
    slider.addEventListener('input', () => {
      output.textContent = `${slider.value} / 10`;
      pending = true;
      if (!writing && timer === null) timer = scheduler.setTimeout(flush, 120);
    });
    slider.addEventListener('change', flush);
  }

  function createSvakomController(options) {
    if (!protocol) throw new Error('SvakomProtocol 未加载');
    const transport = options?.transport;
    if (!transport) throw new Error('缺少 SAVKOM 传输层');
    const onState = typeof options.onState === 'function' ? options.onState : function () {};
    const onLog = typeof options.onLog === 'function' ? options.onLog : function () {};
    const scheduler = options?.scheduler || {
      now: () => Date.now(),
      setTimeout: (fn, delay) => root.setTimeout(fn, delay),
      clearTimeout: id => root.clearTimeout(id),
    };
    const channelTimers = new Map();
    const holds = new Map();
    let stopGeneration = 0;
    let timelineGeneration = 0;
    let timeline = null;
    const desired = new Map();
    let vibrationReady = false;
    let vibrationRevision = 0;
    const knownStrengthChannels = new Set();
    let state = {
      connected: false,
      connecting: false,
      busy: false,
      deviceName: '',
      protocolConfirmed: false,
      transportKind: '',
      stretch: 0,
      vibrate: 0,
      vibrateLevel: 0,
      flap: 0,
      heat: false,
      lastError: '',
    };

    function snapshot() { return { ...state, strengthKnown: knownStrengthChannels.size === 2 }; }
    function publish(patch) {
      state = { ...state, ...patch };
      onState(snapshot());
      return snapshot();
    }

    function channelFor(action) {
      return ['stretch', 'vibrate', 'flap', 'heat'].includes(action.kind) ? action.kind : '';
    }
    function offAction(channel) {
      if (channel === 'stretch') return { kind: 'stretch', mode: 0, duration: 0, raw: 'STRETCH:OFF' };
      if (channel === 'vibrate') return { kind: 'vibrate', mode: 0, duration: 0, raw: 'VIBRATE:OFF' };
      if (channel === 'flap') return { kind: 'flap', mode: 0, duration: 0, raw: 'FLAP:OFF' };
      return { kind: 'heat', enabled: false, duration: 0, raw: 'HEAT:OFF' };
    }
    function desiredAction(channel) {
      if (desired.has(channel) && state[channel]) return { ...desired.get(channel), duration: 0 };
      if (channel === 'stretch' && state.stretch) return { kind: 'stretch', mode: state.stretch, duration: 0, raw: `STRETCH:${state.stretch}` };
      if (channel === 'vibrate' && state.vibrate) return { kind: 'vibrate', mode: state.vibrate, duration: 0, raw: `VIBRATE:${state.vibrate}` };
      if (channel === 'heat' && state.heat) return { kind: 'heat', enabled: true, duration: 0, raw: 'HEAT:ON' };
      return null;
    }
    function clearChannelTimer(channel) {
      const timer = channelTimers.get(channel);
      if (timer) scheduler.clearTimeout(timer.id);
      channelTimers.delete(channel);
    }
    function clearAllTimers() {
      channelTimers.forEach(timer => scheduler.clearTimeout(timer.id));
      channelTimers.clear();
    }
    function clearHold(key) {
      const hold = holds.get(key);
      if (hold) scheduler.clearTimeout(hold.id);
      holds.delete(key);
    }
    function clearHoldsFor(channel) {
      clearHold(channel);
      const all = holds.get('all');
      if (all) all.restores = all.restores.filter(action => channelFor(action) !== channel);
    }
    function clearAllHolds() {
      holds.forEach(hold => scheduler.clearTimeout(hold.id));
      holds.clear();
    }
    function cancelTimeline(reason) {
      if (!timeline) return;
      if (timeline.taskId) scheduler.clearTimeout(timeline.taskId);
      timeline = null;
      timelineGeneration += 1;
      if (reason) onLog(reason, 'system');
    }

    async function connect() {
      if (state.connected || state.connecting) return snapshot();
      knownStrengthChannels.clear();
      publish({ connecting: true, lastError: '' });
      onLog('正在寻找 SL278H…', 'system');
      try {
        const info = await transport.connect();
        const details = { ...(transport.describe?.() || {}), ...(info || {}) };
        const confirmed = details.protocolConfirmed !== false;
        publish({
          connected: true,
          connecting: false,
          deviceName: details.deviceName || 'SL278',
          protocolConfirmed: confirmed,
          transportKind: details.kind || '',
          lastError: confirmed ? '' : (details.error || '协议待确认'),
        });
        onLog(confirmed ? `已连接 ${details.deviceName || 'SL278'}` : '已连接，但协议待确认', confirmed ? 'success' : 'error');
        return snapshot();
      } catch (error) {
        publish({ connected: false, connecting: false, protocolConfirmed: false, lastError: error?.message || String(error) });
        onLog(`连接失败：${error?.message || error}`, 'error');
        throw error;
      }
    }

    function requireReady() {
      if (!state.connected || !transport.isConnected?.()) throw new Error('玩具尚未连接');
      if (!state.protocolConfirmed) throw new Error('设备协议尚未确认，已阻止发送');
    }

    function patchForAction(action) {
      if (action.kind === 'vibrate' || action.kind === 'flap') knownStrengthChannels.add(action.kind);
      if (action.kind === 'stretch') return { stretch: action.mode };
      if (action.kind === 'vibrate') return { vibrate: action.mode, vibrateLevel: action.mode ? action.vibrateLevel : 0 };
      if (action.kind === 'flap') return { flap: action.mode };
      if (action.kind === 'heat') return { heat: Boolean(action.enabled) };
      return {};
    }

    function scheduleDuration(action) {
      const channel = channelFor(action);
      if (!channel || !action.duration) return;
      clearChannelTimer(channel);
      const generation = stopGeneration;
      const record = { id: null };
      record.id = scheduler.setTimeout(async () => {
        if (generation !== stopGeneration || channelTimers.get(channel) !== record) return;
        channelTimers.delete(channel);
        clearHoldsFor(channel);
        try {
          await execute(action.kind === 'heat'
            ? { kind: 'heat', enabled: false, duration: 0, raw: 'HEAT:OFF' }
            : { kind: action.kind, mode: 0, duration: 0, raw: `${action.kind.toUpperCase()}:OFF` }, { source: 'timer' });
          onLog(`${channel.toUpperCase()} 定时结束`, 'system');
        } catch (error) { onLog(`定时停止失败：${error.message}`, 'error'); }
      }, action.duration * 1000);
      channelTimers.set(channel, record);
    }

    async function execute(action, executionOptions = {}) {
      requireReady();
      const source = executionOptions.source || 'manual';
      const channel = channelFor(action);
      if (source === 'manual') {
        cancelTimeline('时间轴已被手动控制替换');
      }
      if (source === 'manual' || source === 'timeline') {
        if (channel) {
          clearChannelTimer(channel);
          clearHoldsFor(channel);
        }
      }
      const hex = protocol.encode(action);
      const generation = stopGeneration;
      const revision = channel === 'vibrate' ? ++vibrationRevision : vibrationRevision;
      if (channel === 'vibrate') vibrationReady = false;
      publish({ busy: true, lastError: '' });
      try {
        await transport.sendHex(hex);
        if (generation !== stopGeneration) return snapshot();
        if (channel === 'vibrate' && revision === vibrationRevision) vibrationReady = action.mode > 0;
        if (channel) desired.set(channel, action);
        publish({ ...patchForAction(action), busy: false, lastError: '' });
        if (source === 'manual' && action.duration) scheduleDuration(action);
        onLog(`发送 ${action.raw || action.kind}`, 'send');
        return snapshot();
      } catch (error) {
        knownStrengthChannels.clear();
        publish({ busy: false, lastError: error?.message || String(error) });
        onLog(`写入失败：${error?.message || error}`, 'error');
        throw error;
      }
    }

    async function executeText(raw) {
      const parsed = protocol.parse(raw);
      if (!parsed.ok) throw new Error(parsed.error);
      const command = parsed.command;
      if (typeof transport.executeText === 'function') {
        requireReady();
        publish({ busy: true, lastError: '' });
        try {
          await transport.executeText(command.raw);
          // Bridge return only acknowledges submission; native callbacks own channel state.
          onLog(`交给手机本地执行 ${command.raw}`, 'send');
          return snapshot();
        } catch (error) {
          applyNativeError(error?.message || String(error));
          throw error;
        }
      }
      if (command.kind === 'stop') return stopAll();
      if (command.kind === 'hold') return hold(command.scope, command.seconds, { source: 'manual' });
      if (command.kind === 'sequence') return startSequence(command);
      return execute(command);
    }

    async function updateVibrationLevel(level) {
      if (!Number.isInteger(level) || level < 1 || level > 10) throw new RangeError('震动力度必须为 1～10');
      // Stopped, HOLD and disconnected states only update the UI preset.
      if (!state.connected || !state.vibrate) return snapshot();
      requireReady();
      if (typeof transport.executeText === 'function') {
        try { await transport.updateVibrationLevel(level); }
        catch (error) { applyNativeError(error?.message || String(error)); throw error; }
        return snapshot();
      }
      if (!vibrationReady) return snapshot();
      const previous = desired.get('vibrate');
      const action = { kind: 'vibrate', mode: state.vibrate, vibrateLevel: level,
        duration: 0, raw: `VIBRATE:${state.vibrate}:${level}` };
      const generation = stopGeneration;
      try { await transport.sendHex(protocol.encode(action)); }
      catch (error) { applyNativeError(error?.message || String(error)); throw error; }
      // An OFF/timer/new mode during the write must win. Never republish a running state.
      if (generation === stopGeneration && state.vibrate === action.mode && desired.get('vibrate') === previous) {
        desired.set('vibrate', action);
        publish({ vibrateLevel: level });
        onLog(`震动力度 ${level} / 10`, 'send');
      }
      return snapshot();
    }

    function applyNativeAction(raw) {
      const parsed = protocol.parse(raw);
      if (!parsed.ok) {
        onLog(`手机状态回传无法识别：${raw}`, 'error');
        return snapshot();
      }
      const action = parsed.command;
      if (action.kind === 'stop') {
        knownStrengthChannels.add('vibrate');
        knownStrengthChannels.add('flap');
        publish({ stretch: 0, vibrate: 0, vibrateLevel: 0, flap: 0, heat: false, busy: false, lastError: '' });
      } else {
        const patch = patchForAction(action);
        publish({ ...patch, busy: false, lastError: knownStrengthChannels.size === 2 ? '' : state.lastError });
      }
      return snapshot();
    }

    function applyNativeError(message) {
      knownStrengthChannels.clear();
      return publish({ busy: false, lastError: String(message) });
    }

    async function hold(scope, seconds, holdOptions = {}) {
      requireReady();
      const key = scope.toLowerCase();
      const channels = key === 'all' ? ['stretch', 'vibrate', 'flap', 'heat'] : [key];
      if (!channels.every(channel => ['stretch', 'vibrate', 'flap', 'heat'].includes(channel))) throw new Error('无效 HOLD 范围');
      clearHold(key);
      const restoreActions = channels.map(desiredAction).filter(Boolean);
      let pausedTimeline = null;
      if (key === 'all' && holdOptions.source === 'manual' && timeline?.taskId) {
        pausedTimeline = {
          generation: timeline.generation,
          remainingMs: Math.max(0, timeline.nextDueAt - scheduler.now()),
        };
        scheduler.clearTimeout(timeline.taskId);
        timeline.taskId = null;
      }
      for (const channel of channels) await execute(offAction(channel), { source: 'hold' });
      const generation = stopGeneration;
      const record = { id: null, restores: restoreActions };
      record.id = scheduler.setTimeout(async () => {
        if (generation !== stopGeneration || holds.get(key) !== record) return;
        holds.delete(key);
        try {
          for (const action of record.restores) {
            if (generation !== stopGeneration) return;
            await execute(action, { source: 'hold' });
          }
          onLog(`HOLD ${scope.toUpperCase()} 已恢复`, 'system');
          if (pausedTimeline && timeline?.generation === pausedTimeline.generation) {
            scheduleTimelineIndex(timeline.index + 1, pausedTimeline.remainingMs);
          }
        } catch (error) { onLog(`HOLD 恢复失败：${error.message}`, 'error'); }
      }, seconds * 1000);
      holds.set(key, record);
      onLog(`HOLD ${scope.toUpperCase()} ${seconds}秒`, 'system');
      return snapshot();
    }

    function scheduleTimelineIndex(index, delayMs) {
      if (!timeline) return;
      const active = timeline;
      active.index = index - 1;
      active.nextDueAt = scheduler.now() + delayMs;
      active.taskId = scheduler.setTimeout(() => runTimelineIndex(index, active.generation), delayMs);
    }

    async function runTimelineIndex(index, generation) {
      if (!timeline || timeline.generation !== generation || generation !== timelineGeneration) return;
      timeline.taskId = null;
      timeline.index = index;
      const event = timeline.events[index];
      if (!event) { cancelTimeline('时间轴已完成'); return; }
      try {
        const action = event.actions[0];
        let pauseMs = 0;
        if (action.kind === 'stop') {
          await stopAll();
          return;
        }
        if (action.kind === 'hold') {
          await hold(action.scope, action.seconds, { source: 'timeline' });
          if (action.scope === 'all') pauseMs = action.seconds * 1000;
        } else {
          for (const item of event.actions) {
            if (!timeline || timeline.generation !== generation) return;
            await execute(item, { source: 'timeline' });
          }
        }
        if (!timeline || timeline.generation !== generation) return;
        const next = timeline.events[index + 1];
        if (!next) {
          if (timeline.cycleSeconds) scheduleTimelineIndex(0, (timeline.cycleSeconds - event.at) * 1000);
          else cancelTimeline('时间轴已完成');
          return;
        }
        scheduleTimelineIndex(index + 1, (next.at - event.at) * 1000 + pauseMs);
      } catch (error) {
        cancelTimeline('时间轴因写入失败而停止');
        publish({ lastError: error.message || String(error) });
      }
    }

    async function startSequence(command) {
      requireReady();
      cancelTimeline();
      timelineGeneration += 1;
      timeline = { events: command.events, cycleSeconds: command.cycleSeconds || 0, index: 0, taskId: null, nextDueAt: scheduler.now(), generation: timelineGeneration };
      onLog(command.cycleSeconds ? `循环编排开始 · 每轮 ${command.cycleSeconds} 秒` : `时间轴开始 · ${command.events[command.events.length - 1].at}秒`, 'system');
      await runTimelineIndex(0, timeline.generation);
      return snapshot();
    }

    async function stopAll() {
      requireReady();
      vibrationReady = false;
      vibrationRevision += 1;
      stopGeneration += 1;
      clearAllTimers();
      clearAllHolds();
      cancelTimeline();
      publish({ busy: true, lastError: '' });
      if (typeof transport.stopAll === 'function') {
        try {
          await transport.stopAll();
          onLog('已向手机提交全部停止', 'system');
          return snapshot();
        } catch (error) {
          applyNativeError(error?.message || String(error));
          throw error;
        }
      }
      const actions = [
        { kind: 'stretch', mode: 0, raw: 'STRETCH:OFF' },
        { kind: 'vibrate', mode: 0, raw: 'VIBRATE:OFF' },
        { kind: 'flap', mode: 0, raw: 'FLAP:OFF' },
        { kind: 'heat', enabled: false, raw: 'HEAT:OFF' },
      ];
      let firstError = null;
      for (const action of actions) {
        try { await transport.sendHex(protocol.encode(action)); }
        catch (error) { if (!firstError) firstError = error; }
      }
      knownStrengthChannels.clear();
      if (!firstError) {
        knownStrengthChannels.add('vibrate');
        knownStrengthChannels.add('flap');
      }
      publish({
        stretch: 0,
        vibrate: 0,
        vibrateLevel: 0,
        flap: 0,
        heat: false,
        busy: false,
        lastError: firstError ? (firstError.message || String(firstError)) : '',
      });
      onLog(firstError ? `停止时有写入失败：${firstError.message || firstError}` : '全部停止', firstError ? 'error' : 'system');
      if (firstError) throw firstError;
      return snapshot();
    }

    async function disconnect() {
      if (state.connected && state.protocolConfirmed) {
        try { await stopAll(); } catch (error) { onLog('断开前未能完整停止', 'error'); }
      }
      try { await transport.disconnect(); }
      finally { handleTransportDisconnected(); }
      return snapshot();
    }

    function handleTransportDisconnected() {
      knownStrengthChannels.clear();
      vibrationReady = false;
      vibrationRevision += 1;
      stopGeneration += 1;
      clearAllTimers();
      clearAllHolds();
      cancelTimeline();
      publish({
        connected: false,
        connecting: false,
        busy: false,
        deviceName: '',
        protocolConfirmed: false,
        transportKind: '',
        stretch: 0,
        vibrate: 0,
        vibrateLevel: 0,
        flap: 0,
        heat: false,
      });
      onLog('已断开', 'system');
      return snapshot();
    }

    onState(snapshot());
    return Object.freeze({ connect, disconnect, execute, executeText, updateVibrationLevel, applyNativeAction, applyNativeError, handleTransportDisconnected, stopAll, getState: snapshot });
  }

  function createUnavailableTransport() {
    return {
      async connect() { throw new Error('当前环境不支持蓝牙连接'); },
      async disconnect() {},
      isConnected() { return false; },
      async sendHex() { throw new Error('当前环境不支持蓝牙连接'); },
      describe() { return { kind: '', protocolConfirmed: false }; },
    };
  }

  function createNativeTransport(bridge, options = {}) {
    const scheduler = options.scheduler || root;
    const onLog = options.onLog || function () {};
    let connected = false;
    let pending = null;
    let info = { kind: 'Android 原生', deviceName: '', protocolConfirmed: false };
    function rejectPending(message) {
      const attempt = pending;
      if (!attempt) return;
      pending = null;
      scheduler.clearTimeout(attempt.timer);
      attempt.reject(new Error(message));
    }
    return {
      connect() {
        return new Promise((resolve, reject) => {
          if (Number(bridge.getSvakomControlVersion?.() || 0) < 2) {
            reject(new Error('请先安装三路独立控制新版 App；旧版仍使用联动指令'));
            return;
          }
          const attempt = { resolve, reject, timer: null };
          pending = attempt;
          attempt.timer = scheduler.setTimeout(() => {
            if (pending !== attempt) return;
            rejectPending('手机蓝牙连接超时（20 秒），请查看下方连接记录后重试');
            try { bridge.disconnect(); } catch (error) {}
          }, 20000);
          try {
            onLog('使用 Android 原生蓝牙，正在请求扫描…', 'system');
            bridge.selectProfile?.('svakom');
            if (bridge.isConnected?.() && bridge.getProfile?.() === 'svakom') {
              this.onConnected('svakom', bridge.getDeviceName?.());
              return;
            }
            bridge.connect();
            onLog('原生扫描请求已返回，等待设备连接结果…', 'system');
          } catch (error) { rejectPending(error?.message || String(error)); }
        });
      },
      async disconnect() { rejectPending('连接已取消'); if (!bridge.getProfile || bridge.getProfile() === 'svakom') bridge.disconnect(); connected = false; },
      isConnected() { try { return (!bridge.getProfile || bridge.getProfile() === 'svakom') && (connected || Boolean(bridge.isConnected())); } catch (error) { return connected; } },
      async sendHex(hex) { if (bridge.getProfile && bridge.getProfile() !== 'svakom') throw new Error('当前连接不属于 SVAKOM'); bridge.sendData(hex); },
      async executeText(command) {
        if (command.startsWith('LOOP:') && Number(bridge.getSvakomControlVersion?.() || 0) < 3) throw new Error('AI 循环编排需要安装新版 App 1.24');
        bridge.executeCommand(command);
      },
      async updateVibrationLevel(level) {
        if (typeof bridge.setSvakomVibrationLevel !== 'function') throw new Error('实时调力度需要更新 App；当前可调好后点击震动模式应用');
        if (!bridge.setSvakomVibrationLevel(level)) throw new Error('震动力度未能应用，请查看连接记录');
      },
      async stopAll() { if (!bridge.getProfile || bridge.getProfile() === 'svakom') bridge.emergencyStop(); },
      describe() { return { ...info }; },
      onConnected(profile, deviceName) {
        if (!pending) return;
        const attempt = pending;
        pending = null;
        scheduler.clearTimeout(attempt.timer);
        connected = true;
        info = { kind: 'Android 原生', deviceName: deviceName || 'SL278', protocolConfirmed: !profile || profile === 'svakom' };
        attempt.resolve(info);
      },
      onDisconnected() { connected = false; rejectPending('蓝牙在连接完成前断开，请重试'); },
      onError(message) { rejectPending(message); },
    };
  }

  function bootstrap() {
    if (!doc?.body?.classList.contains('toy-svakom-page')) return;
    const logElement = doc.getElementById('toyLog');
    const log = (message, kind) => {
      const line = doc.createElement('p');
      line.className = `toy-log-${kind || 'system'}`;
      line.textContent = `${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}  ${message}`;
      logElement.appendChild(line);
      while (logElement.children.length > 40) logElement.firstElementChild.remove();
      logElement.scrollTop = logElement.scrollHeight;
    };

    let transport = createUnavailableTransport();
    if (root.AionBle && /AionChatApp/i.test(root.navigator?.userAgent || '')) {
      transport = createNativeTransport(root.AionBle, { onLog: log });
    } else if (typeof root.createSvakomWebBle === 'function') {
      transport = root.createSvakomWebBle({ bluetooth: root.navigator?.bluetooth, onLog: log,
        onDisconnected: () => controller.handleTransportDisconnected() });
    }

    const modeButtons = [];
    function renderModes(containerId, modes, prefix) {
      const container = doc.getElementById(containerId);
      modes.forEach(([name, description], index) => {
        const mode = index + 1;
        const button = doc.createElement('button');
        button.type = 'button';
        button.className = 'toy-mode-button';
        button.dataset.svakomCommand = `${prefix}:${mode}`;
        button.disabled = true;
        button.innerHTML = `<span>${String(mode).padStart(2, '0')}</span><strong>${name}</strong><small>${description}</small>`;
        container.appendChild(button);
        modeButtons.push(button);
      });
    }
    renderModes('svakomStretchModes', STRETCH_MODES, 'STRETCH');
    renderModes('svakomVibrateModes', VIBRATE_MODES, 'VIBRATE');
    renderModes('svakomFlapModes', FLAP_MODES, 'FLAP');

    const controls = Array.from(doc.querySelectorAll('[data-svakom-command]'));
    const connectionState = doc.getElementById('toyConnectionState');
    const connectionDetail = doc.getElementById('toyConnectionDetail');
    const connectButton = doc.getElementById('toyConnectButton');
    const stopButton = doc.getElementById('toyStopAllButton');
    const commandInput = doc.getElementById('svakomCommandInput');
    const commandRun = doc.getElementById('svakomCommandRun');
    const commandHint = doc.getElementById('svakomCommandHint');

    let aiControl = null;
    let stateReporter = null;
    let aiEnabled = false;
    let reportTimer = null;
    function reportLatest() {
      if (!stateReporter) return;
      root.clearTimeout(reportTimer);
      reportTimer = root.setTimeout(() => stateReporter.report(controller.getState(), aiEnabled), 150);
    }
    const controller = createSvakomController({
      transport,
      onLog: log,
      onState(state) {
        if (!state.connected) aiControl?.disconnected();
        reportLatest();
        doc.body.classList.toggle('is-connected', state.connected && state.protocolConfirmed);
        doc.body.classList.toggle('has-error', Boolean(state.lastError));
        connectionState.textContent = state.connecting ? '正在连接' : state.connected ? (state.protocolConfirmed ? '已连接' : '协议待确认') : '未连接';
        connectionDetail.textContent = state.lastError || (state.connected ? `${state.deviceName} · ${state.transportKind || '蓝牙'}` : '将显示实际名称并验证蓝牙协议');
        connectButton.textContent = state.connected ? '断开' : '连接';
        connectButton.disabled = state.connecting || state.busy;
        doc.getElementById('svakomStretchState').textContent = state.stretch ? `模式 ${state.stretch}` : '停止';
        doc.getElementById('svakomVibrateState').textContent = state.vibrate ? `模式 ${state.vibrate}` : '停止';
        doc.getElementById('svakomFlapState').textContent = state.flap ? `${state.flap} 档` : '停止';
        doc.getElementById('svakomHeatState').textContent = state.heat ? '开启' : '关闭';
        const ready = state.connected && state.protocolConfirmed && !state.busy;
        controls.forEach(button => { button.disabled = !ready; });
        stopButton.disabled = !state.connected || !state.protocolConfirmed;
        commandRun.disabled = !ready;
        doc.getElementById('toyTimelineRun').disabled = !ready;
        commandHint.textContent = state.protocolConfirmed ? '输入后会先完整校验，再执行' : '连接并确认协议后可用';
        modeButtons.forEach(button => {
          const [kind, value] = button.dataset.svakomCommand.split(':');
          button.classList.toggle('is-active', state[kind.toLowerCase()] === Number(value));
        });
      },
    });

    const aiToggle = doc.getElementById('toyAiEnabled');
    const aiStatus = doc.getElementById('toyAiStatus');
    stateReporter = root.createSvakomStateReporter({ request: root.fetch.bind(root),
      source: root.crypto?.randomUUID?.() || `page-${Date.now()}-${Math.random().toString(36).slice(2)}`,
      onError: error => log(error.message, 'error'),
    });
    root.setInterval(() => stateReporter.report(controller.getState(), aiEnabled), 5000);
    aiControl = root.createSvakomAiControl({ controller, request: root.fetch.bind(root), onLog: log,
      onState(value) {
        aiEnabled = value.enabled;
        reportLatest();
        aiToggle.checked = value.enabled;
        aiToggle.disabled = value.blocked;
        aiStatus.textContent = value.blocked ? '正在同步控制权…' : value.active ? 'AI 循环编排运行中 · 可随时全部停止' : value.enabled ? '已开启 · AI 可编排，连接后才能执行' : '已关闭 · 不提供提示词，手动控制仍可用';
      },
    });
    const aiError = error => { log(error.message, 'error'); aiStatus.textContent = error.message; };
    aiToggle.addEventListener('change', () => aiControl.toggle(aiToggle.checked).catch(aiError));
    aiControl.refresh().catch(aiError);
    // Dedicated socket: no legacy toy routing, history replay or duplicate global alarm handlers.
    function connectAiSocket() {
      const socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`);
      socket.onopen = () => aiControl.refresh().catch(aiError);
      socket.onmessage = event => {
        try { aiControl.receive(JSON.parse(event.data)).catch(aiError); } catch (error) { aiError(error); }
      };
      socket.onclose = () => root.setTimeout(connectAiSocket, 2000);
      socket.onerror = () => socket.close();
    }
    connectAiSocket();
    doc.addEventListener('visibilitychange', () => { if (!doc.hidden) aiControl.refresh().catch(aiError); });
    const manualTakeover = () => aiControl.takeover();

    const phaseContainer = doc.getElementById('toyTimelinePhases');
    const timelineSummary = doc.getElementById('toyTimelineSummary');
    function readPhases() {
      return Array.from(phaseContainer.children).map(row => Object.fromEntries(
        Array.from(row.querySelectorAll('[data-field]')).map(input => [input.dataset.field, input.value])
      ));
    }
    function updateTimelineSummary() {
      let at = 0;
      Array.from(phaseContainer.children).forEach((row, index) => {
        const end = at + Number(row.querySelector('[data-field="seconds"]').value);
        row.querySelector('.toy-phase-time').textContent = `第 ${index + 1} 段 · ${at}～${end} 秒`;
        at = end;
      });
      try { const plan = buildTimeline(readPhases()); timelineSummary.textContent = `共 ${plan.seconds} 秒 · 结束时全部停止`; }
      catch (error) { timelineSummary.textContent = error.message; }
    }
    function addPhase() {
      if (phaseContainer.children.length >= 63) return;
      const row = doc.createElement('div');
      row.className = 'toy-phase';
      const optionsFor = modes => '<option value="KEEP">保持不变</option><option value="OFF">停止</option>' +
        modes.map(([name], i) => `<option value="${i + 1}">${i + 1} · ${name}</option>`).join('');
      row.innerHTML = `<div class="toy-phase-head"><strong class="toy-phase-time"></strong><button class="toy-channel-stop" type="button">删除</button></div>
        <div class="toy-phase-fields">
          <label>持续秒数<input aria-label="持续秒数" data-field="seconds" type="number" min="1" max="3600" value="10"></label>
          <label>主体动作<select aria-label="主体动作" data-field="stretch">${optionsFor(STRETCH_MODES)}</select></label>
          <label>主体震动<select aria-label="主体震动" data-field="vibrate">${optionsFor(VIBRATE_MODES)}</select></label>
          <label>拍打档位<select aria-label="拍打档位" data-field="flap">${optionsFor(FLAP_MODES)}</select></label>
          <label>震动力度<input aria-label="震动力度" data-field="level" type="number" min="1" max="10" value="2"></label>
        </div>`;
      row.querySelector('[data-field="stretch"]').value = phaseContainer.children.length ? 'KEEP' : '1';
      row.querySelector('button').addEventListener('click', () => { row.remove(); updateTimelineSummary(); });
      row.addEventListener('input', updateTimelineSummary);
      phaseContainer.appendChild(row);
      updateTimelineSummary();
    }
    doc.getElementById('toyAddPhase').addEventListener('click', addPhase);
    doc.getElementById('toyTimelineRun').addEventListener('click', async () => {
      try {
        await manualTakeover();
        const plan = buildTimeline(readPhases());
        commandInput.value = plan.command;
        await controller.executeText(plan.command);
        timelineSummary.textContent = `已提交 ${plan.seconds} 秒编排 · 可随时全部停止`;
      } catch (error) { timelineSummary.textContent = error.message; log(error.message, 'error'); }
    });
    addPhase();
    const vibrateLevel = doc.getElementById('toyVibrateLevel');
    bindVibrationSlider(vibrateLevel, doc.getElementById('toyVibrateLevelValue'), controller,
      error => log(error.message, 'error'));
    controls.forEach(button => button.addEventListener('click', async () => {
      const raw = button.dataset.svakomCommand;
      const channel = raw.split(':', 1)[0].toLowerCase();
      const duration = Number(doc.querySelector(`[data-duration-channel="${channel}"]`)?.value || 0);
      try {
        await manualTakeover();
        const command = channel === 'vibrate' && !raw.endsWith(':OFF') ? `${raw}:${vibrateLevel.value}` : raw;
        await controller.executeText(withDuration(command, duration));
      }
      catch (error) { log(error.message, 'error'); }
    }));
    connectButton.addEventListener('click', async () => {
      try {
        if (controller.getState().connected) { await manualTakeover(); await controller.disconnect(); }
        else {
          if ((await root.ToyNavigation.readSelection()).active !== 'svakom') throw new Error('请先选用 SVAKOM');
          await controller.connect();
        }
      } catch (error) {}
    });
    stopButton.addEventListener('click', async () => {
      try { await manualTakeover(); } catch (error) { aiError(error); }
      try { await controller.stopAll(); } catch (error) {}
    });
    commandRun.addEventListener('click', async () => {
      try { await manualTakeover(); await controller.executeText(commandInput.value); }
      catch (error) { commandHint.textContent = error.message; log(error.message, 'error'); }
    });
    commandInput.addEventListener('keydown', event => {
      if (event.key === 'Enter') { event.preventDefault(); commandRun.click(); }
    });

    root.toyNativeBle = {
      onConnected(profile, deviceName) { transport.onConnected?.(profile, deviceName); },
      onDisconnected() { transport.onDisconnected?.(); controller.handleTransportDisconnected(); },
      onError(message) { transport.onError?.(message); controller.applyNativeError(message); log(message, 'error'); },
      onLog(message) { log(message, 'system'); },
      onNativeAction(action) { controller.applyNativeAction(action); log(`手机执行 ${action}`, 'system'); },
    };
    root.stopAndDisconnectToy = async () => {
      await manualTakeover();
      if (controller.getState().connected) await controller.disconnect();
    };
    root.svakomController = controller;
  }

  const api = { createSvakomController, withDuration, buildTimeline, createNativeTransport, bindVibrationSlider, STRETCH_MODES, VIBRATE_MODES, FLAP_MODES };
  if (doc) {
    if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', bootstrap, { once: true });
    else bootstrap();
  }
  return api;
});

(function(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.AnkniProtocol = api;
})(typeof window === 'undefined' ? globalThis : window, function() {
  'use strict';
  const MODES = ['停止','持续浅尝','渐入佳境','大开大合','短浅顶撞','蛮牛冲撞','愈渐愈强','持续挑逗','边缘将至','快速震弹','冲刺'];
  function integer(value, min, max) {
    if (!Number.isInteger(value) || value < min || value > max) throw new Error('ANKNI 参数超出范围');
    return value;
  }
  function packet(command, payload) {
    const bytes = [0xAA, command, payload.length, ...payload];
    bytes.push(bytes.reduce((sum, v) => sum + v, 0) & 255);
    return bytes.map(v => v.toString(16).padStart(2, '0')).join('').toUpperCase();
  }
  function prefix(value) {
    if (!['0F','0A'].includes(value)) throw new Error('无效经典模式前缀');
    return parseInt(value, 16);
  }
  function intensity(v, s, swap = false) {
    integer(v, 0, 100); integer(s, 0, 100);
    return packet(8, swap ? [s, v, 100] : [v, s, 100]);
  }
  function stop(value = '0F') { return [packet(8, [0,0,0]), packet(prefix(value), [0,0])]; }
  function classic(mode, value = '0F') { integer(mode, 1, 10); return packet(prefix(value), [mode, mode]); }
  function parse(value) {
    const raw = String(value || '').replace(/\s+/g, '').toUpperCase();
    if (raw === 'STOP') return {kind:'stop', raw};
    if (/^SET:\d{1,3},\d{1,3}$/.test(raw)) {
      const [v,s] = raw.slice(4).split(',').map(Number);
      integer(v,0,100); integer(s,0,100);
      return {kind:'set', v,s,raw};
    }
    if (/^MODE:\d{1,2}$/.test(raw)) return {kind:'mode', mode:integer(Number(raw.slice(5)),1,10),raw};
    if (!/^(LOOP|SEQ):/.test(raw) || raw.length > 8192) throw new Error('无效 ANKNI 指令');
    const rows = raw.slice(raw.indexOf(':')+1).split(';');
    if (rows.length > 128) throw new Error('最多 128 段');
    let duration = 0;
    const phases = rows.map(row => {
      if (!/^\d{1,6},\d{1,2}$/.test(row)) throw new Error('每段需要：持续毫秒,模式编号');
      const [ms,mode] = row.split(',').map(Number);
      integer(ms,200,600000); integer(mode,0,10); duration += ms;
      return {ms,mode};
    });
    if (duration > 600000) throw new Error('每轮最多 10 分钟');
    return {kind:'timeline', loop:raw.startsWith('LOOP:'), phases, duration, raw};
  }
  return {MODES, intensity, classic, stop, parse};
});

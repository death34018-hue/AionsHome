/* Strudel runs in a sandboxed frame. Scores never execute in the chat page. */
'use strict';

const pendingSamples = new Map();
let nextSampleId = 0;
let repl;
let manifest;
let initializing;
let current;
let playing = false;
let looping = false;
let volume = .55;
let generation = 0;
let lastCycle = 0;
let stopAtCycle = 0;
let scorePattern;
let startQueue = Promise.resolve();
let finishWarmup;
let master;
let endPhase = '';
let quietSince = null;

function notify(type, data = {}) {
  parent.postMessage({channel: 'aionshome-composition', type, ...data}, '*');
}

// The host fetches these fixed local sample URLs with its normal credentials.
// This also works for a phone signed into the home through a protected domain.
window.fetch = function sampleFetch(url) {
  const requestId = ++nextSampleId;
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      pendingSamples.delete(requestId);
      reject(new Error('音色加载超时，请重试'));
    }, 30000);
    pendingSamples.set(requestId, {resolve, reject, timeout});
    notify('sample-request', {requestId, url: String(url)});
  });
};

async function initialize(samples) {
  if (!initializing) {
    manifest = samples;
    initializing = strudel.initStrudel({
      prebake: () => strudel.samples(manifest),
      onEvalError: error => notify('error', {id: current?.id, message: error.message}),
    }).then(value => { repl = value; });
  }
  return initializing;
}

function applyVolume() {
  const context = strudel.getAudioContext();
  const output = strudel.getSuperdoughAudioController().output.destinationGain;
  if (master?.output !== output) {
    const limiter = context.createDynamicsCompressor();
    limiter.threshold.value = -5;
    limiter.knee.value = 3;
    limiter.ratio.value = 16;
    limiter.attack.value = .003;
    limiter.release.value = .15;
    const meter = context.createAnalyser();
    meter.fftSize = 2048;
    output.disconnect(context.destination);
    output.connect(limiter).connect(meter).connect(context.destination);
    master?.limiter.disconnect();
    master?.meter.disconnect();
    master = {output, limiter, meter, samples: new Float32Array(meter.fftSize)};
  }
  // Give quiet arrangements ~8 dB more headroom on the slider, with peak control.
  output.gain.setTargetAtTime(volume * 2.5, context.currentTime, .015);
}

function stop() {
  generation++;
  finishWarmup?.();
  finishWarmup = null;
  playing = false;
  endPhase = '';
  quietSince = null;
  lastCycle = 0;
  repl?.stop();
  notify('state', {id: current?.id, playing: false, cycle: 0});
}

async function warmSamples(pattern, cycles, isCurrent = () => true) {
  const urls = new Set();
  const banks = new Map();
  for (const hap of pattern.queryArc(0, cycles)) {
    for (const name of [hap.value.s].flat()) {
      const bank = manifest[name];
      if (!bank) continue;
      if (!banks.has(name)) {
        const absolute = paths => [paths].flat().map(path => manifest._base + path);
        banks.set(name, Array.isArray(bank) ? absolute(bank) : Object.fromEntries(
          Object.entries(bank).map(([note, paths]) => [note, absolute(paths)])));
      }
      // Reuse Strudel's actual pitch/index selection, including sample aliases.
      urls.add(strudel.getSampleInfo({...hap.value, s: name}, banks.get(name)).url);
    }
  }
  const remaining = [...urls];
  let next = 0;
  await Promise.all(Array.from({length: Math.min(3, remaining.length)}, async () => {
    while (isCurrent() && next < remaining.length) {
      await strudel.loadBuffer(remaining[next++], strudel.getAudioContext());
    }
  }));
}

async function start(message, requestedGeneration) {
  const piece = message.piece;
  current = piece;
  stopAtCycle = piece.cycles;
  looping = Boolean(message.loop);
  volume = message.volume;
  notify('loading', {id: current.id});
  try {
    await initialize(message.samples);
    await strudel.initAudio();
    await strudel.getAudioContext().resume();
    if (generation !== requestedGeneration) return;
    repl.setCps(piece.bpm / 240);
    const pattern = await strudel.evaluate(piece.code, false);
    if (generation !== requestedGeneration) return;
    if (!pattern || repl.state.evalError) throw repl.state.evalError || new Error('乐谱暂时无法演奏');
    await Promise.race([
      warmSamples(pattern, piece.cycles, () => generation === requestedGeneration),
      new Promise(resolve => { finishWarmup = resolve; }),
    ]);
    if (generation !== requestedGeneration) return;
    finishWarmup = null;
    scorePattern = repeatScore(pattern, piece.cycles, () => looping ? Infinity : stopAtCycle);
    await repl.setPattern(scorePattern, false);
    applyVolume();
    await repl.start();
    playing = true;
    notify('state', {id: current.id, playing: true, cycle: 0});
  } catch (error) {
    if (generation !== requestedGeneration) return;
    stop();
    notify('error', {id: current?.id, message: error.message || '音色加载失败，请重试'});
  }
}

// Freeze the saved first N bars, so repeating a score also repeats <...> variations.
function repeatScore(pattern, cycles, getEnd = () => Infinity) {
  return new strudel.Pattern(state => {
    const end = getEnd();
    if (Number(state.span.begin) >= end) return [];
    const offset = Math.floor(Number(state.span.begin) / cycles) * cycles;
    return pattern.query(state.withSpan(span => span.withTime(time => time.sub(offset))))
      .map(hap => hap.withSpan(span => {
        const shifted = span.withTime(time => time.add(offset));
        return Number(shifted.end) > end ? shifted.withEnd(() => end) : shifted;
      }));
  }).splitQueries();
}

function noteEvents(cycle) {
  const pattern = scorePattern;
  if (!pattern) return [];
  return pattern.queryArc(Math.max(0, cycle - 3), cycle + 3).map(hap => {
    const value = hap.value;
    let midi;
    if (value.note !== undefined) midi = strudel.noteToMidi(value.note);
    else if (value.freq) midi = 69 + 12 * Math.log2(value.freq / 440);
    else midi = {bd: 36, sd: 38, hh: 42}[value.s];
    if (!Number.isFinite(midi) || !hap.whole) return null;
    return {start: Number(hap.whole.begin), end: Number(hap.whole.end), midi,
      instrument: String(value.s || ''), color: value.color || ''};
  }).filter(Boolean);
}

window.addEventListener('message', async event => {
  if (event.source !== parent || event.data?.channel !== 'aionshome-composition') return;
  const message = event.data;
  if (message.type === 'sample-response') {
    const request = pendingSamples.get(message.requestId);
    if (!request) return;
    pendingSamples.delete(message.requestId);
    clearTimeout(request.timeout);
    if (message.error) request.reject(new Error(message.error));
    else request.resolve(new Response(message.buffer));
    return;
  }
  if (message.type === 'play') {
    stop();
    const requestedGeneration = generation;
    startQueue = startQueue.then(() => generation === requestedGeneration && start(message, requestedGeneration));
    await startQueue;
    return;
  }
  if (message.type === 'stop') { stop(); return; }
  if (message.type === 'pause' && playing) {
    await strudel.getAudioContext().suspend();
    lastCycle = Math.max(0, Math.min(repl.scheduler.now(), looping ? Infinity : stopAtCycle));
    playing = false;
  }
  if (message.type === 'resume' && current && !playing && endPhase !== 'ended') {
    await strudel.getAudioContext().resume();
    playing = true;
  }
  if (message.type === 'volume') { volume = message.volume; if (repl) applyVolume(); }
  if (message.type === 'loop') {
    looping = Boolean(message.loop);
    if (!looping && current) {
      stopAtCycle = (Math.floor(lastCycle / current.cycles) + 1) * current.cycles;
    }
  }
  notify('state', {id: current?.id, playing, cycle: lastCycle, phase: endPhase});
});

function tailIsQuiet() {
  master.meter.getFloatTimeDomainData(master.samples);
  let peak = 0;
  for (const sample of master.samples) peak = Math.max(peak, Math.abs(sample));
  const now = strudel.getAudioContext().currentTime;
  quietSince = peak > .001 ? null : quietSince ?? now;
  return quietSince !== null && now - quietSince >= .18;
}

setInterval(() => {
  if (!repl || !current || !playing) return;
  try {
    const cycle = Math.max(0, repl.scheduler.now());
    lastCycle = looping ? cycle : Math.min(cycle, stopAtCycle);
    if (!looping && cycle >= stopAtCycle) {
      endPhase = tailIsQuiet() ? 'ended' : 'tail';
      if (endPhase === 'ended') {
        playing = false;
        repl.stop();
      }
    } else {
      endPhase = '';
      quietSince = null;
    }
    notify('tick', {id: current.id, playing, cycle: lastCycle, phase: endPhase,
      endAt: looping ? null : stopAtCycle, cps: repl.scheduler.cps, notes: noteEvents(lastCycle)});
  } catch (error) {
    stop();
    notify('error', {id: current.id, message: error.message || '乐谱暂时无法显示'});
  }
}, 80);

notify('ready');

'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');

function player() {
  let messageHandler, tick;
  const states = [];
  const parent = {postMessage: value => states.push(value)};
  const context = {console: {log() {}, warn() {}}, performance,
    setTimeout, clearTimeout, setInterval: fn => { tick = fn; }, clearInterval() {},
    window: {addEventListener: (name, fn) => { if (name === 'message') messageHandler = fn; }},
    document: {addEventListener() {}, dispatchEvent() {}}, parent};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(__dirname + '/static/vendor/strudel-1.3.0/strudel.js', 'utf8'), context);
  vm.runInContext(fs.readFileSync(__dirname + '/static/composition-player.js', 'utf8'), context);
  return {context, states, tick: () => tick(),
    send: message => messageHandler({source: parent, data: {channel: 'aionshome-composition', ...message}})};
}

function audioOutput(context) {
  const audio = {currentTime: 0, peak: 0, resume: async () => {}, destination: {}};
  function node() { return {connect: target => target, disconnect() {}}; }
  audio.createDynamicsCompressor = () => Object.assign(node(), {threshold:{}, knee:{}, ratio:{}, attack:{}, release:{}});
  audio.createAnalyser = () => Object.assign(node(), {getFloatTimeDomainData: buffer => buffer.fill(audio.peak)});
  const output = Object.assign(node(), {gain:{setTargetAtTime: value => {audio.gain = value;}}});
  context.strudel.getAudioContext = () => audio;
  context.strudel.getSuperdoughAudioController = () => ({output:{destinationGain:output}});
  return audio;
}

test('loop repeats the saved bars including slow angle-bracket variations', () => {
  const {context} = player();
  const actual = vm.runInContext(`
    repeatScore(strudel.note(strudel.mini('<c4 g4 e4>')), 2)
      .queryArc(0, 6).map(hap => hap.value.note).join(',')
  `, context);
  assert.equal(actual, 'c4,g4,c4,g4,c4,g4');
  const drums = vm.runInContext(`
    scorePattern = strudel.s(strudel.mini('bd sd hh sd'));
    noteEvents(0).filter(note => note.start < 1).sort((a, b) => a.start - b.start)
      .map(note => note.midi).join(',')
  `, context);
  assert.equal(drums, '36,38,42,38');
});

test('metadata sets tempo and disabling loop finishes the current pass', async () => {
  const runtime = player();
  const {context} = runtime;
  const pattern = context.strudel.note(context.strudel.mini('c4'));
  let now = 0, tempo;
  const repl = {state: {pattern}, scheduler: {now: () => now, cps: .25},
    setCps: value => { tempo = value; }, setPattern: async value => { repl.state.pattern = value; },
    start: async () => {}, stop() {}};
  context.strudel.initStrudel = async () => repl;
  context.strudel.initAudio = async () => {};
  const audio = audioOutput(context);
  context.strudel.evaluate = async () => pattern;
  await runtime.send({type: 'play', piece: {id: 'one', bpm: 60, cycles: 2, code: ''}, samples: {}, volume: .5, loop: true});
  assert.equal(tempo, .25);
  assert.equal(audio.gain, 1.25);
  now = 2.5;
  runtime.tick();
  await runtime.send({type: 'loop', loop: false});
  now = 3.9;
  runtime.tick();
  assert.equal(runtime.states.at(-1).playing, true);
  assert.equal(repl.state.pattern.queryArc(3.95, 4.1).some(hap => Number(hap.whole.begin) >= 4), false);
  now = 4;
  audio.peak = .03;
  runtime.tick();
  assert.equal(runtime.states.at(-1).phase, 'tail');
  audio.peak = 0;
  audio.currentTime = .2;
  runtime.tick();
  audio.currentTime = .5;
  runtime.tick();
  const last = runtime.states.at(-1);
  assert.equal(last.playing, false);
  assert.equal(last.phase, 'ended');
  assert.equal(last.cycle, 4);
  assert.ok(last.notes.length);
});

test('sample warmup downloads only selected notes with at most three concurrent loads', async () => {
  const {context} = player();
  context.testManifest = {_base:'/samples/', piano:{C4:'c4.mp3', Ds4:'ds4.mp3', G4:'g4.mp3',
    A4:'a4.mp3', C5:'c5.mp3', C6:'unused.mp3'}};
  let active = 0, peak = 0;
  const loaded = [];
  context.strudel.getAudioContext = () => ({});
  context.strudel.loadBuffer = async url => {
    loaded.push(url);
    peak = Math.max(peak, ++active);
    await new Promise(resolve => setTimeout(resolve, 5));
    active--;
  };
  await vm.runInContext(`manifest = testManifest;
    warmSamples(strudel.note(strudel.mini('c4 e4 g4 a4 c5 c4')).s('piano'), 1)`, context);
  assert.deepEqual(loaded.sort(), ['c4','c5','ds4','g4','a4'].map(name=>'/samples/'+name+'.mp3').sort());
  assert.ok(peak <= 3);
});

test('switching to a ready score does not wait for a stopped score download', async () => {
  const runtime = player();
  const {context} = runtime;
  const note = context.strudel.note(context.strudel.mini('c4'));
  let began, release;
  const downloading = new Promise(resolve => { began = resolve; });
  const repl = {state:{}, scheduler:{now:()=>0, cps:.25}, setCps(){},
    setPattern:async () => {}, start:async () => {}, stop(){}};
  context.strudel.initStrudel = async () => repl;
  context.strudel.initAudio = async () => {};
  audioOutput(context);
  context.strudel.evaluate = async code => note.s(code);
  context.strudel.loadBuffer = () => { began(); return new Promise(resolve=>{release=resolve;}); };
  const samples = {_base:'/samples/', piano:{C4:'c4.mp3'}};
  const first = runtime.send({type:'play', piece:{id:'old', code:'piano', bpm:60, cycles:1}, samples, volume:.5});
  await downloading;
  const second = runtime.send({type:'play', piece:{id:'ready', code:'sine', bpm:60, cycles:1}, samples, volume:.5});
  const immediate = await Promise.race([second.then(()=>true), new Promise(resolve=>setTimeout(()=>resolve(false),200))]);
  release();
  await Promise.all([first, second]);
  assert.equal(immediate, true);
  assert.equal(runtime.states.at(-1).id, 'ready');
  assert.equal(runtime.states.at(-1).playing, true);
});

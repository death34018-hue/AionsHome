'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');
const test = require('node:test');

const chart = require(path.join(__dirname, 'static', 'health-weight-chart.js'));

const weights = [
  { date: '2026-05-25', weight_kg: 64.0, note: '' },
  { date: '2026-10-20', weight_kg: 66.7, note: '' },
  { date: '2026-11-04', weight_kg: 66.7, note: '' },
  { date: '2026-11-07', weight_kg: 66.5, note: '水肿' },
  { date: '2026-11-10', weight_kg: 66.4, note: '' },
];

test('niceTicks uses steps whose labels round exactly to one decimal', () => {
  for (const [lo, hi] of [[65.7, 67.1], [63.2, 67.3], [66.1, 66.4], [40, 95]]) {
    const y = chart.niceTicks(lo, hi, 5);
    assert.ok(y.min <= lo && y.max >= hi, `${lo}-${hi} -> ${y.min}-${y.max}`);
    assert.ok(y.ticks.length >= 2 && y.ticks.length <= 6, `${lo}-${hi} ticks ${y.ticks}`);
    for (const t of y.ticks) assert.equal(Number(t.toFixed(1)), t);
  }
});

test('30-day view keeps only records inside the window and summarizes them', () => {
  const view = chart.computeView(weights, [], '30', '2026-11-10');
  assert.equal(view.end - view.start, 29);
  assert.deepEqual(view.visible.map(p => p.date), ['2026-10-20', '2026-11-04', '2026-11-07', '2026-11-10']);
  assert.equal(view.stats.min, 66.4);
  assert.equal(view.stats.max, 66.7);
  assert.equal(view.stats.change, -0.3);
  assert.equal(view.totalCount, 5);
});

test('all-time view starts at the first record', () => {
  const view = chart.computeView(weights, [], 'all', '2026-11-10');
  assert.equal(chart.dayToStr(view.start), '2026-05-25');
  assert.equal(view.visible.length, 5);
  assert.equal(view.stats.change, 2.4);
});

test('period bands are clipped to the visible window', () => {
  const periods = [
    { start_date: '2026-10-08', end_date: '2026-10-14' },
    { start_date: '2026-11-09', end_date: '' },
    { start_date: '2026-09-01', end_date: '2026-09-05' },
  ];
  const view = chart.computeView(weights, periods, '30', '2026-11-10');
  assert.deepEqual(view.bands.map(b => [chart.dayToStr(b.start), chart.dayToStr(b.end)]), [
    ['2026-10-12', '2026-10-14'],
    ['2026-11-09', '2026-11-09'],
  ]);
});

test('empty window reports no stats', () => {
  const view = chart.computeView(weights, [], '7', '2026-12-31');
  assert.equal(view.visible.length, 0);
  assert.equal(view.stats, null);
  assert.equal(view.totalCount, 5);
});

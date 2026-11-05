'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const {createCache} = require('./static/composition-cache.js');

function storage() {
  const data = new Map();
  return {getItem: key => data.get(key) || null, setItem: (key, value) => data.set(key, value)};
}
const piece = {id:'one', title:'晚灯', code:'note("c4").s("piano")', bpm:80, cycles:4, favorite:false};

test('a fetched score survives a new page without another network request', async () => {
  const disk = storage();
  let calls = 0;
  const first = createCache({storage:disk, fetch: async () => { calls++; return Response.json(piece); }});
  await first.getPiece('one');
  const next = createCache({storage:disk, fetch: async () => { calls++; throw new Error('offline'); }});
  assert.equal((await next.getPiece('one')).code, piece.code);
  assert.equal(calls, 1);
});

test('offline library keeps its snapshot and successful favorite updates stay local', async () => {
  const disk = storage();
  const online = createCache({storage:disk, fetch: async () => Response.json({compositions:[piece]})});
  await online.loadList();
  online.rememberPiece({...piece, favorite:true});
  const offline = createCache({storage:disk, fetch: async () => { throw new Error('offline'); }});
  await assert.rejects(offline.loadList());
  assert.equal(offline.getCachedList()[0].favorite, true);
});

test('failed downloads can retry and unavailable local storage never blocks playback', async () => {
  let attempts = 0;
  const cache = createCache({storage:{getItem(){throw Error('blocked');}, setItem(){throw Error('full');}},
    fetch: async () => { if (++attempts === 1) throw Error('offline'); return Response.json(piece); }});
  await assert.rejects(cache.getPiece('one'), {message:'暂时连不上小家，请稍后重试'});
  assert.equal((await cache.getPiece('one')).id, 'one');
  assert.equal(attempts, 2);
});

test('an old cached score plays immediately while its refresh is still waiting', async () => {
  const disk = storage();
  const cache = createCache({storage:disk, fetch: async () => Response.json(piece)});
  await cache.getPiece('one');
  const saved = JSON.parse(disk.getItem('aionshome.music-box.v1'));
  saved.items.one.cachedAt = 0;
  disk.setItem('aionshome.music-box.v1', JSON.stringify(saved));
  let complete;
  const slow = createCache({storage:disk, fetch: () => new Promise(resolve => { complete = resolve; })});
  assert.equal((await slow.getPiece('one')).id, 'one');
  assert.equal(typeof complete, 'function');
  complete(Response.json(piece));
});

test('a delayed list cannot overwrite a more recent confirmed favorite', async () => {
  let complete;
  const cache = createCache({storage:storage(), fetch: () => new Promise(resolve => { complete = resolve; })});
  const loading = cache.loadList();
  cache.rememberPiece({...piece, favorite:true, updated_at:101});
  complete(Response.json({compositions:[{...piece, favorite:false, updated_at:100}]}));
  assert.equal((await loading)[0].favorite, true);
  assert.equal(cache.getCachedList()[0].favorite, true);
});

test('an unchanged catalog uses a conditional request instead of redownloading scores', async () => {
  const disk = storage();
  const first = createCache({storage:disk, fetch:async()=>Response.json({compositions:[piece]}, {headers:{ETag:'"one"'}})});
  await first.loadList();
  const next = createCache({storage:disk, fetch:async(url, options)=>{
    assert.equal(options.headers['If-None-Match'], '"one"');
    return new Response(null, {status:304});
  }});
  assert.equal((await next.loadList())[0].code, piece.code);
});

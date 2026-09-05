const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div', id = '') {
    this.tagName = tag.toUpperCase(); this.id = id; this.children = []; this.textContent = '';
    this.className = ''; this.dataset = {}; this.classList = {toggle(){}}; this.open = false;
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, fn) { this['on' + name] = fn; }
  showModal() { this.open = true; }
  close() { this.open = false; }
}

const elements = new Map();
const get = id => {
  if (!elements.has(id)) elements.set(id, new Element('div', id));
  return elements.get(id);
};
const requests = [];
let deletedItem = false, deletedTrip = false;
const item = {id:'wish-1', actor:'aion', item_id:'805862215859', title:'机械手', price:'19.7',
  url:'https://item.taobao.com/item.htm?id=805862215859', image:'', found_at:1};
const trip = {id:'trip-1', actor:'aion', started_at:1, ended_at:2, status:'finished', keyword:'机械手',
  motive:'想看看', candidate_count:1, selected:[item], notes:[], summary:'逛完了', error:''};
const state = () => ({names:{aion:'测试角色',connor:'另一角色'},items:deletedItem ? [] : [item],
  settings:{transport:'native_bridge'},busy:false,last_run:null,trips:deletedTrip ? [] : [trip],active_trips:[],avatars:{}});
const context = {
  window:{TaobaoCards:{returnUrl:()=>'/chat',navigate(){}}}, location:{search:''}, URL, URLSearchParams,
  document:{createElement:tag=>new Element(tag),getElementById:get,querySelector:()=>null},
  confirm:()=>true, clearTimeout(){}, setTimeout(){}, console,
  fetch:async (url, options={}) => {
    requests.push([url, options.method || 'GET']);
    if (url === '/api/taobao/items/wish-1' && options.method === 'DELETE') deletedItem = true;
    if (url === '/api/taobao/trips/trip-1' && options.method === 'DELETE') deletedTrip = true;
    return {ok:true,json:async()=>url === '/api/taobao/state' ? state() : {deleted:true}};
  },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('static/taobao.js','utf8'), context);

const descendants = node => [node, ...node.children.flatMap(descendants)];
const buttonNamed = (root, text) => descendants(root).find(node => node.tagName === 'BUTTON' && node.textContent === text);

(async()=>{
  await new Promise(resolve => setImmediate(resolve));
  const itemDelete = buttonNamed(get('wishlist'), '删除');
  assert.ok(itemDelete, 'saved product cards expose a direct delete action');
  await itemDelete.onclick({stopPropagation(){}});
  assert.ok(requests.some(([url, method]) => url === '/api/taobao/items/wish-1' && method === 'DELETE'));

  get('tripDetailButton').onclick();
  const tripDelete = buttonNamed(get('detailBody'), '删除这篇小记');
  assert.ok(tripDelete, 'trip details expose their own delete action');
  await tripDelete.onclick();
  assert.ok(requests.some(([url, method]) => url === '/api/taobao/trips/trip-1' && method === 'DELETE'));
  console.log('shopping delete actions: wishlist item and trip record stay separate OK');
})().catch(error=>{console.error(error);process.exitCode=1;});

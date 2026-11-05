const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync(require('node:path').join(__dirname,'static/theater-export.js'),'utf8');

test('native exports use distinct filenames and the authenticated same-origin download bridge',async()=>{
  const saved=[];let now=1000;
  const frame={AionImageSaver:{},AionRepairFiles:{supportsTheaterExport:()=>true,save:(...args)=>saved.push(args)}};
  const window={parent:frame,top:frame};
  const context=vm.createContext({window,URL,location:{href:'https://home.test/theater'},showToast(){},Date:{now:()=>now++}});
  vm.runInContext(source+';globalThis.exporter=TheaterExport',context);
  await context.exporter.download('tc_123','月下故事','docx',false);
  await context.exporter.download('tc_123','月下故事','docx',true);
  assert.notEqual(saved[0][1],saved[1][1],'Android must not reuse an existing download destination');
  assert.match(saved[0][1],/^月下故事.*\.docx$/);
  assert.equal(new URL(saved[0][0]).origin,'https://home.test');
  assert.equal(new URL(saved[1][0]).searchParams.get('include_drafts'),'true');
});

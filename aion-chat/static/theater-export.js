/* Download a novel as one portable file, including through the native attachment bridge. */
const TheaterExport = (() => {
  async function download(cid, title, format='docx', includeDrafts=false) {
    const url=new URL(`/api/theater/studio/books/${encodeURIComponent(cid)}/export`,location.href);
    url.searchParams.set('format',format);
    url.searchParams.set('include_drafts',String(includeDrafts));
    let native=null, inApp=false;
    for(const frame of [window,window.parent,window.top]) {
      try {
        inApp ||= !!frame.AionImageSaver;
        if(frame.AionRepairFiles?.supportsTheaterExport?.()) {native=frame.AionRepairFiles;break;}
      } catch(_) {}
    }
    const name=(title.replace(/[\\/:*?"<>|\x00-\x1f]/g,'_').replace(/[. ]+$/g,'').slice(0,100)||'小剧场小说')+'.'+format;
    if(native) {
      const uniqueName=name.slice(0,-format.length-1)+'-'+Date.now()+'.'+format;
      native.save(url.href,uniqueName);showToast('已开始导出，可在下载通知中查看文件');return;
    }
    if(inApp)throw Error('当前 App 版本暂不支持小说文件下载，请更新 App，或用浏览器打开小家后导出');
    const response=await fetch(url.href);
    if(!response.ok) {
      const error=await response.json().catch(()=>({}));
      throw Error(typeof error.detail==='string'?error.detail:'导出失败，请重试');
    }
    const blob=await response.blob();
    const objectUrl=URL.createObjectURL(blob), link=document.createElement('a');
    const disposition=response.headers.get('Content-Disposition')||'';
    const encoded=disposition.match(/filename\*=UTF-8''([^;]+)/i);
    let filename=name;
    if(encoded) {try {filename=decodeURIComponent(encoded[1]);} catch(_) {}}
    link.href=objectUrl;link.download=filename;document.body.append(link);link.click();link.remove();
    setTimeout(()=>URL.revokeObjectURL(objectUrl),60000);
    showToast('小说已导出，可在下载文件中找到');
  }
  return {download};
})();

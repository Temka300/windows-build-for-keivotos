const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
(async()=>{
 const browser=await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL,ignoreDefaultArgs:['--hide-scrollbars']});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});
 const page=await context.newPage();page.setDefaultTimeout(15000);
 const report={checks:[],errors:[],console:[]};
 function watchErrors(target){target.on('pageerror',e=>report.errors.push(e.message));target.on('console',m=>{if(m.type()==='error')report.console.push({text:m.text(),location:m.location()});});}
 watchErrors(page);
 await context.tracing.start({screenshots:true,snapshots:true});
 const check=x=>{report.checks.push(x);console.log('PASS: '+x);};
 async function api(url,method='GET',body){return page.evaluate(async({url,method,body})=>{const r=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});return {status:r.status,data:await r.json()};},{url,method,body});}
 async function shot(name){await page.screenshot({path:path.join(config.output,name+'.png')});}
 async function readerReady(target=page){
  await target.waitForFunction(()=>document.querySelector('.reader-error')||document.querySelector('.reader .paged-page img')||document.querySelector('.reader .page img'));
  assert.equal(await target.getByRole('alertdialog',{name:'Resume manga'}).count(),0);
 }

 try{
  if(config.continuation){
   const expected=config.continuation;await page.goto(config.url);
   const retainedDetails=await api('/api/manga/metadata?'+new URLSearchParams({source_id:expected.source_id,path:'Blue Hour'}));
   assert.equal(retainedDetails.status,200);assert.deepEqual(retainedDetails.data.fields,{summary:'고요한 동네 산책 이야기',tags:['동네산책','Local']});

   await page.getByRole('button',{name:'Open Keivotos menu'}).click();await page.locator('.app-drawer').getByRole('button',{name:'Manga',exact:true}).click();
   await page.getByRole('navigation',{name:'Manga categories'}).getByRole('button',{name:'Reading',exact:true}).click();
   await page.getByRole('button',{name:expected.series_path,exact:true}).click();
   const chapterName=expected.path.split('/').pop();await page.getByRole('button',{name:chapterName,exact:true}).click();
   await page.waitForFunction(({chapterName,expected})=>{
    const reader=document.querySelector('.reader'),node=reader?.querySelector(`[data-chapter="${expected.path}"] .page[data-page="${expected.page}"] img`);
    return reader?.classList.contains('vertical')&&node?.naturalWidth>0&&reader.querySelector('.reader-chapter').textContent===chapterName&&reader.querySelector('.page-counter').textContent==='1 / 1';
   },{chapterName,expected});
   assert.deepEqual(report.errors,[]);check('A fresh browser against a second server process resumes the continued chapter, page and vertical layout');return;
  }
  await page.goto(config.url);
  assert.equal((await api('/api/manga/library')).status,409);
  assert.equal((await api('/api/suite/modules/manga/enable','POST')).status,200);
  const add=await api('/api/suite/folders/apply','POST',{folders:[{path:config.media,display_name:'Manga',role:'manga'}]});assert.equal(add.status,200,JSON.stringify(add));
  assert.equal((await api('/api/manga/library')).data.total,8);
  await page.getByRole('button',{name:'Open Keivotos menu'}).click();await page.locator('.app-drawer').getByRole('button',{name:'Manga',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('.manga-tile img').length===8&&[...document.querySelectorAll('.manga-tile img')].every(i=>i.complete&&i.naturalWidth>0));
  report.timings=await page.evaluate(async sourceId=>{
   const measured={runtime:'Windows Edge via loopback',fixture:'generated folder PNGs and deflated CBZ',chapters:{},covers:performance.getEntriesByType('resource').filter(entry=>entry.name.includes('/api/manga/page?')&&entry.name.includes('cover=true')).map(entry=>({duration_ms:entry.duration,transfer_bytes:entry.transferSize}))};
   for(const [kind,path] of [['folder','The Quiet Valley/Chapter 1'],['archive','The Quiet Valley/Chapter 2.cbz']]){
    const parameters=new URLSearchParams({source_id:sourceId,path,kind});
    const start=performance.now();parameters.set('metadata','true');const metadata=await(await fetch('/api/manga/pages?'+parameters,{cache:'no-store'})).json();parameters.delete('metadata');
    const times={manifest_ms:performance.now()-start,first:[],turn:[],revisit:[]};
    for(let sample=0;sample<3;sample++)for(const [name,page] of [['first',0],['turn',1],['revisit',0]]){
     parameters.set('page',String(page));parameters.set('cover','false');
     parameters.set('representation','original');parameters.set('revision',metadata.revisions[page]);
     const started=performance.now();const response=await fetch('/api/manga/page?'+parameters,{cache:'no-store'});
     if(!response.ok)throw new Error('Timing page failed: '+response.status);
     const url=URL.createObjectURL(await response.blob());const image=new Image();
     try{image.src=url;await image.decode();times[name].push(performance.now()-started);}
     finally{URL.revokeObjectURL(url);}
    }
    measured.chapters[kind]=times;
   }
   return measured;
  },add.data.sources.find(source=>source.role==='manga').source_id);
  await shot('manga-library');
  await page.getByLabel('Search manga').fill('quiet');await page.waitForTimeout(450);assert.equal(await page.locator('.manga-tile').count(),1);
  await page.getByLabel('Search manga').fill('');await page.waitForTimeout(450);
  check('optional Manga enables, adopts a source, renders eight covers and searches names');
  const sourceId=add.data.sources.find(source=>source.role==='manga').source_id;
  const capturedDetails=await api('/api/manga/metadata/refresh','POST',{source_id:sourceId,path:'Blue Hour'});
  assert.equal(capturedDetails.status,200);assert.deepEqual(capturedDetails.data.fields,{summary:'고요한 동네 산책 이야기',tags:['동네산책','Local']});
  for(const query of ['동네산책','고요한 동네']){
   await page.getByLabel('Search manga').fill(query);await page.waitForTimeout(450);await page.getByRole('button',{name:'Blue Hour',exact:true}).waitFor();assert.equal(await page.locator('.manga-tile').count(),1);
  }
  await page.getByLabel('Search manga').fill('Unsupported replacement title');await page.getByText('No manga found.',{exact:true}).waitFor();
  await page.getByLabel('Search manga').fill('');await page.waitForFunction(()=>document.querySelectorAll('.manga-tile').length===8);
  check('Explicit local metadata refresh makes saved tags and descriptions searchable while keeping original Manga names and the existing layout');

  // Item 30 uses the existing guarded metadata routes and real generated files.
  {
   const info=()=>page.getByRole('dialog',{name:'Blue Hour chapters',exact:true});
   const editor=()=>page.getByRole('dialog',{name:'Edit manga details',exact:true});
   const decision=()=>page.getByRole('alertdialog',{name:'Unsaved manga details',exact:true});
   const getDetails=()=>api('/api/manga/metadata?'+new URLSearchParams({source_id:sourceId,path:'Blue Hour'}));
   const enter=async()=>{await page.getByRole('button',{name:'Blue Hour',exact:true}).click();await info().getByRole('button',{name:'Edit',exact:true}).waitFor({state:'visible'});await page.waitForFunction(()=>!document.querySelector('.metadata-actions button:disabled'));};
   await enter();await info().getByText('고요한 동네 산책 이야기',{exact:true}).waitFor();
   assert.equal(await info().locator('.detail-tags button').count(),2);
   await shot('manga-info-below-cover');
   await page.setViewportSize({width:360,height:640});await shot('manga-info-below-cover-narrow');await page.setViewportSize({width:1440,height:1000});
   const listBefore=await info().locator('.chapter-list').boundingBox();
   await info().getByRole('button',{name:'Edit',exact:true}).click();
   assert.equal(await editor().getByRole('textbox').count(),2);
   assert(await editor().getByLabel('Description',{exact:true}).evaluate(node=>node===document.activeElement));
   await editor().getByLabel('Description',{exact:true}).fill('내가 쓴 설명');await editor().getByLabel('Tags',{exact:true}).fill('Personal tag\n한국어 태그');
   await page.keyboard.press('Control+s');await editor().waitFor({state:'detached'});
   await info().getByText('내가 쓴 설명',{exact:true}).waitFor();assert.deepEqual((await getDetails()).data.fields,{summary:'내가 쓴 설명',tags:['Personal tag','한국어 태그']});
   const listAfter=await info().locator('.chapter-list').boundingBox();assert.equal(listAfter.height,listBefore.height);
   assert(await info().getByRole('button',{name:'Edit',exact:true}).evaluate(node=>node===document.activeElement));
   check('Manga info shows saved tags/description below the smaller cover; its two-field editor saves immediately without shrinking the chapter column');

   await info().getByRole('button',{name:'Edit',exact:true}).click();await editor().getByLabel('Description',{exact:true}).fill('Keep this draft');
   assert(await page.evaluate(()=>{const event=new Event('beforeunload',{cancelable:true});window.dispatchEvent(event);return event.defaultPrevented;}));
   await editor().getByRole('button',{name:'Cancel',exact:true}).click();await decision().getByRole('button',{name:'Keep editing',exact:true}).click();
   assert.equal(await editor().getByLabel('Description',{exact:true}).inputValue(),'Keep this draft');
   const external=(await getDetails()).data;
   assert.equal((await api('/api/manga/metadata/overrides','POST',{source_id:sourceId,path:'Blue Hour',fields:{...external.overrides,tags:['External tag']},version:external.version,source_revision:external.source_revision,request_id:crypto.randomUUID()})).status,200);
   await editor().getByRole('button',{name:'Save',exact:true}).click();await editor().getByRole('alert').filter({hasText:'Details changed.'}).waitFor();
   assert.equal(await editor().getByLabel('Description',{exact:true}).inputValue(),'Keep this draft');
   await editor().getByRole('button',{name:'Reload',exact:true}).click();await decision().getByRole('button',{name:'Discard changes',exact:true}).click();
   await page.waitForFunction(()=>document.querySelector('#manga-detail-tags')?.value==='External tag'&&!document.querySelector('#manga-detail-tags')?.disabled);
   await editor().getByLabel('Description',{exact:true}).fill('');await editor().getByLabel('Tags',{exact:true}).fill('');await editor().getByRole('button',{name:'Save',exact:true}).click();await editor().waitFor({state:'detached'});
   assert.deepEqual((await getDetails()).data.fields,{});assert.equal(await info().locator('.metadata-details').count(),0);
   await info().getByRole('button',{name:'Edit',exact:true}).click();
   for(const field of ['summary','tags'])await editor().locator('.field-heading:has(label[for="manga-detail-'+field+'"]) button').click();
   assert.equal(await editor().getByLabel('Description',{exact:true}).inputValue(),'고요한 동네 산책 이야기');
   await editor().getByRole('button',{name:'Save',exact:true}).click();await editor().waitFor({state:'detached'});assert.deepEqual((await getDetails()).data.overrides,{});
   check('Cancel/Keep editing and unload protection retain drafts; real conflicts require Reload, while clear and Use local preserve separate personal choices');

   const submitted=[];let loseReply=true;
   const lostSave=async route=>{submitted.push(route.request().postDataJSON());if(loseReply){loseReply=false;const response=await route.fetch();assert.equal(response.status(),200);await route.fulfill({status:503,json:{detail:'Injected lost save reply'}});}else await route.continue();};
   await page.route('**/api/manga/metadata/overrides',lostSave);
   await info().getByRole('button',{name:'Edit',exact:true}).click();await editor().getByLabel('Description',{exact:true}).fill('Saved once with retry');await editor().getByRole('button',{name:'Save',exact:true}).click();
   await editor().getByRole('alert').filter({hasText:'Details were not saved.'}).waitFor();assert(await editor().getByLabel('Description',{exact:true}).isDisabled());
   await editor().getByRole('button',{name:'Retry',exact:true}).click();await editor().waitFor({state:'detached'});assert.equal(submitted.length,2);assert.deepEqual(submitted[0],submitted[1]);
   await page.unroute('**/api/manga/metadata/overrides',lostSave);
   let failRefresh=true,refreshCalls=0;
   const refreshFailure=route=>{refreshCalls++;return failRefresh?route.fulfill({status:503,json:{detail:'Injected refresh failure'}}):route.continue();};
   await page.route('**/api/manga/metadata/refresh',refreshFailure);await info().getByRole('button',{name:'Refresh',exact:true}).click();
   await info().getByRole('alert').filter({hasText:'Local details could not be refreshed.'}).waitFor();await info().getByText('Saved once with retry',{exact:true}).waitFor();
   failRefresh=false;await info().getByRole('button',{name:'Retry',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('.metadata-actions button:disabled'));assert.equal(refreshCalls,2);
   await page.unroute('**/api/manga/metadata/refresh',refreshFailure);
   await info().getByRole('button',{name:'Edit',exact:true}).click();await editor().locator('.field-heading:has(label[for="manga-detail-summary"]) button').click();await editor().getByRole('button',{name:'Save',exact:true}).click();await editor().waitFor({state:'detached'});
   await info().getByRole('button',{name:'Back to manga',exact:true}).click();
   check('A lost successful save retries the exact request once; failed Refresh retains confirmed facts and Retry repeats only the requested refresh');

   let releaseRead;const lateRead=async route=>{if(new URL(route.request().url()).searchParams.get('path')!=='Blue Hour')return route.continue();const response=await route.fetch();await new Promise(resolve=>releaseRead=resolve);try{await route.fulfill({response,json:{...(await response.json()),fields:{summary:'Stale response must not appear'}}});}catch{}};
   await page.route('**/api/manga/metadata?*',lateRead);await page.getByRole('button',{name:'Blue Hour',exact:true}).click();while(!releaseRead)await page.waitForTimeout(10);
   await info().getByRole('button',{name:'Back to manga',exact:true}).click();await page.getByRole('button',{name:'Paper Moon',exact:true}).click();releaseRead();await page.waitForTimeout(150);await page.unroute('**/api/manga/metadata?*',lateRead);
   const paper=page.getByRole('dialog',{name:'Paper Moon chapters',exact:true});await page.waitForFunction(()=>!document.querySelector('.metadata-actions button:disabled'));
   assert.equal(await paper.locator('.metadata-details').count(),0);assert.equal(await paper.getByText('Stale response must not appear',{exact:true}).count(),0);
   await paper.getByRole('button',{name:'Refresh',exact:true}).click();await paper.locator('.detail-tags button').first().waitFor();
   assert.equal(await paper.locator('.detail-tags button').count(),24);assert.equal(await paper.locator('.metadata-details img').count(),0);
   for(const viewport of [{width:1440,height:1000},{width:360,height:460},{width:1440,height:420}]){
    await page.setViewportSize(viewport);await paper.getByRole('button',{name:'More',exact:true}).click();
    assert.equal(await paper.getByRole('button',{name:'Less',exact:true}).getAttribute('aria-expanded'),'true');
    const side=paper.locator('.manga-details-pane');await side.hover();await page.mouse.wheel(0,600);await page.waitForTimeout(120);assert(await side.evaluate(node=>node.scrollTop>0));
    await paper.getByRole('button',{name:'Less',exact:true}).click();
    assert(await paper.evaluate(node=>{const box=node.getBoundingClientRect();return box.x>=0&&box.y>=0&&box.right<=innerWidth&&box.bottom<=innerHeight&&node.scrollWidth<=node.clientWidth+1;}));
   }
   await page.setViewportSize({width:1440,height:1000});await shot('manga-info-metadata');await paper.getByRole('button',{name:'Back to manga',exact:true}).click();
   check('Late metadata replies cannot cross series; missing facts leave no empty panel, and many Unicode tags/plain descriptions fit desktop, short and narrow windows');

   // A local intercepted library row creates the real notice without acquisition.
   await api('/api/suite/modules/youtube/enable','POST');
   const noticeRow={id:'item30-notice',url:'https://www.youtube.com/watch?v=abcdefghijk',source_id:sourceId,title:'Local notification fixture',status:'queued',progress:0,path:'',thumbnail:'',error:''};
   const noticeLibrary=route=>route.fulfill({json:{items:[noticeRow],total:1}});await page.route('**/api/youtube/library?*',noticeLibrary);
   await page.getByRole('button',{name:'Open Keivotos menu'}).click();await page.locator('.app-drawer').getByRole('button',{name:'YouTube',exact:true}).click();
   const notice=page.locator('[data-download-notification]').filter({hasText:'Local notification fixture'});await notice.waitFor();await notice.hover();
   await page.getByRole('button',{name:'Open Keivotos menu'}).click();await page.locator('.app-drawer').getByRole('button',{name:'Manga',exact:true}).click();await enter();
   await info().getByRole('button',{name:'Edit',exact:true}).click();await editor().getByLabel('Description',{exact:true}).fill('Protected from Downloads');
   await notice.hover();await notice.getByRole('button',{name:'Downloads',exact:true}).click();await page.waitForTimeout(200);
   assert.equal(await page.locator('.downloads-view').count(),0);assert.equal(await editor().getByLabel('Description',{exact:true}).inputValue(),'Protected from Downloads');
   await editor().getByRole('button',{name:'Cancel',exact:true}).click();await decision().getByRole('button',{name:'Discard changes',exact:true}).click();await info().getByRole('button',{name:'Back to manga',exact:true}).click();
   await notice.hover();await notice.getByRole('button',{name:'Downloads',exact:true}).click();await page.locator('.downloads-view').waitFor();
   await page.getByRole('button',{name:'Open Keivotos menu'}).click();await page.locator('.app-drawer').getByRole('button',{name:'Manga',exact:true}).click();
   await page.unroute('**/api/youtube/library?*',noticeLibrary);await notice.getByRole('button',{name:'Dismiss notification',exact:true}).click();
   assert.deepEqual((await getDetails()).data.fields,{summary:'고요한 동네 산책 이야기',tags:['동네산책','Local']});
   check('The real Downloads notice preserves an open Manga draft; after explicit Discard and closing info, it opens Downloads without starting a download');

   for(const unavailable of ['hidden','disabled']){
    const confirmed=(await getDetails()).data;await enter();await info().getByRole('button',{name:'Edit',exact:true}).click();await editor().getByLabel('Description',{exact:true}).fill('Unavailable source draft');
    try{
     if(unavailable==='hidden')assert.equal((await api('/api/suite/folders/apply','POST',{folders:[{source_id:sourceId,path:config.media,display_name:'Manga',role:'manga',visible:false}]})).status,200);
     else assert.equal((await api('/api/suite/modules/manga/disable','POST')).status,200);
     await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await editor().waitFor({state:'detached'});assert.equal(await page.locator('.chapter-dialog,.manga-tile img').count(),0);
    }finally{
     if(unavailable==='hidden')await api('/api/suite/folders/apply','POST',{folders:[{source_id:sourceId,path:config.media,display_name:'Manga',role:'manga',visible:true}]});
     else await api('/api/suite/modules/manga/enable','POST');
     await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await page.getByRole('button',{name:'Blue Hour',exact:true}).waitFor();
    }
    assert.deepEqual((await getDetails()).data.overrides,confirmed.overrides);
   }
   check('Hidden sources and disabled Manga remove the stale editor and cover references without saving a draft or altering confirmed personal choices');
  }
  if(config.metadataOnly){assert.deepEqual(report.errors,[]);return;}

  const responseChecks=await page.evaluate(async sourceId=>{
   const parameters=new URLSearchParams({source_id:sourceId,path:'The Quiet Valley/Chapter 1',kind:'folder',metadata:'true'});
   const metadata=await(await fetch('/api/manga/pages?'+parameters,{cache:'no-store'})).json();
   parameters.delete('metadata');parameters.set('page','0');parameters.set('representation','original');parameters.set('revision',metadata.revisions[0]);
   const original=await fetch('/api/manga/page?'+parameters,{cache:'no-store'});const body=await original.arrayBuffer();
   const unchanged=await fetch('/api/manga/page?'+parameters,{cache:'no-store',headers:{'If-None-Match':original.headers.get('etag')}});
   const raw=await(await fetch('/api/files/file?'+new URLSearchParams({source_id:sourceId,path:'The Quiet Valley/Chapter 1/00-cover.png'}))).arrayBuffer();
   const identical=body.byteLength===raw.byteLength&&new Uint8Array(body).every((value,index)=>value===new Uint8Array(raw)[index]);
   parameters.set('revision','0'.repeat(64));const stale=await fetch('/api/manga/page?'+parameters,{cache:'no-store',headers:{'If-None-Match':original.headers.get('etag')}});
   parameters.delete('revision');parameters.delete('representation');const legacy=await fetch('/api/manga/page?'+parameters,{cache:'no-store'});
   return {status:original.status,mime:original.headers.get('content-type'),cache:original.headers.get('cache-control'),nosniff:original.headers.get('x-content-type-options'),unchanged:unchanged.status,unchangedBytes:(await unchanged.arrayBuffer()).byteLength,stale:stale.status,legacyMime:legacy.headers.get('content-type'),identical};
  },sourceId);
  assert.equal(responseChecks.status,200);assert.equal(responseChecks.mime,'image/png');assert.equal(responseChecks.cache,'private, no-cache');
  assert.equal(responseChecks.nosniff,'nosniff');assert.equal(responseChecks.unchanged,304);assert.equal(responseChecks.unchangedBytes,0);
  assert.equal(responseChecks.stale,409);assert.equal(responseChecks.legacyMime,'image/webp');assert(responseChecks.identical);
  report.responses=responseChecks;
  check('Original preserves exact PNG bytes, uses private validators, refuses forged revisions and retains legacy WebP defaults');
  const tile=page.getByRole('button',{name:'The Quiet Valley',exact:true});
  const mangaTitle=page.locator('.manga-tile[title="The Quiet Valley"] .manga-name');
  for(const width of [1440,360]){
   await page.setViewportSize({width,height:1000});
   for(const text of ['The Quiet Valley','静かな谷を越えて旅を続ける物語','The Quiet Valley and the morning train that kept going','unbroken'.repeat(18)]){
    await mangaTitle.evaluate((node,value)=>node.textContent=value,text);
    const metrics=await mangaTitle.evaluate(node=>({height:node.getBoundingClientRect().height,width:node.getBoundingClientRect().width,card:node.closest('button').getBoundingClientRect().width,font:parseFloat(getComputedStyle(node).fontSize),clamp:getComputedStyle(node).webkitLineClamp,wrap:getComputedStyle(node).overflowWrap}));
    assert.equal(metrics.clamp,'2');assert.equal(metrics.wrap,'anywhere');
    assert(Math.abs(metrics.height-metrics.font*2.7)<1);assert(metrics.width<=metrics.card);
   }
  }
  await mangaTitle.evaluate(node=>node.textContent='The Quiet Valley');
  await page.setViewportSize({width:1440,height:1000});
  assert.equal(await tile.getAttribute('title'),'The Quiet Valley');
  check('Manga titles reserve two rows and contain Japanese, long Latin and unbroken names at both widths');
  await tile.click({button:'right'});
  await page.getByRole('menu',{name:'Location for The Quiet Valley'}).getByRole('menuitem',{name:'Show in Files'}).click();
  await page.getByRole('navigation',{name:'Current folder'}).getByRole('button',{name:'The Quiet Valley'}).waitFor();
  await page.getByRole('button',{name:'Open Keivotos menu'}).click();
  await page.locator('.app-drawer').getByRole('button',{name:'Manga',exact:true}).click();
  await tile.click();
  const dialog=page.getByRole('dialog',{name:'The Quiet Valley chapters'});
  await dialog.getByRole('button',{name:'Chapter 10',exact:true}).waitFor();
  assert.equal(await dialog.locator('.series-title').textContent(),'The Quiet Valley');
  report.chapterWindow=await dialog.boundingBox();assert.equal(report.chapterWindow.height,500);
  assert.deepEqual(await page.locator('.chapter-list button').evaluateAll(nodes=>nodes.map(n=>n.getAttribute('aria-label'))),['Chapter 1','Chapter 2','Chapter 10']);
  let revealed=null;
  const revealRoute=async route=>{revealed=route.request().postDataJSON();await route.fulfill({json:{status:'revealed'}});};
  await page.route('**/api/files/reveal',revealRoute);
  await dialog.getByRole('button',{name:'Chapter 1',exact:true}).click({button:'right'});
  await page.getByRole('menu',{name:'Location for Chapter 1'}).getByRole('menuitem',{name:'Open in Explorer'}).click();
  assert.equal(revealed.path,'The Quiet Valley/Chapter 1');
  await page.unroute('**/api/files/reveal',revealRoute);
  await dialog.getByRole('button',{name:'Chapter 2',exact:true}).click({button:'right'});
  await page.getByRole('menu',{name:'Location for Chapter 2'}).getByRole('menuitem',{name:'Show in Files'}).click();
  await page.locator('[data-files-entry-path="The Quiet Valley/Chapter 2.cbz"]').waitFor();
  await page.getByRole('button',{name:'Open Keivotos menu'}).click();
  await page.locator('.app-drawer').getByRole('button',{name:'Manga',exact:true}).click();
  await tile.click();
  await dialog.getByRole('button',{name:'Chapter 10',exact:true}).waitFor();
  check('Manga series, folder chapter and archive chapter menus target registered Files items');
  await shot('manga-chapters');
  await page.mouse.click(20,500);await dialog.waitFor({state:'detached'});assert(await tile.evaluate(n=>n===document.activeElement));
  await tile.click();await dialog.getByRole('button',{name:'Chapter 1',exact:true}).click();
  const reader=page.getByRole('dialog',{name:'Manga reader'});
  assert.equal(await reader.locator('.series-title').textContent(),'The Quiet Valley');
  await page.getByAltText('Page 1',{exact:true}).waitFor();
  const box=await reader.boundingBox();assert.equal(box.width,1440);assert.equal(box.height,1000);
  assert.equal(await reader.locator('img').count(),1);
  assert.equal(await page.getByLabel('Reading layout').inputValue(),'paged');
  async function assertPagedImageFits(){
    await readerReady();
    await page.waitForFunction(()=>{const image=document.querySelector('.paged-page img');return image?.complete&&image.naturalWidth>0;});
    const bounds=await reader.locator('.paged-page img').evaluate(image=>{
      const page=image.closest('.paged-page').getBoundingClientRect();
      const rect=image.getBoundingClientRect();
      return {page:{left:page.left,top:page.top,right:page.right,bottom:page.bottom},image:{left:rect.left,top:rect.top,right:rect.right,bottom:rect.bottom,width:rect.width,height:rect.height},naturalRatio:image.naturalWidth/image.naturalHeight};
    });
    assert(bounds.image.left>=bounds.page.left-1&&bounds.image.top>=bounds.page.top-1);
    assert(bounds.image.right<=bounds.page.right+1&&bounds.image.bottom<=bounds.page.bottom+1);
    assert(Math.abs(bounds.image.width/bounds.image.height-bounds.naturalRatio)<.01);
  }
  await assertPagedImageFits();
  assert.equal(await reader.locator('.page-counter').textContent(),'1 / 13');
  const originalUrl=new URL(await reader.locator('img').getAttribute('src'),config.url);
  assert.equal(originalUrl.searchParams.get('representation'),'original');assert.match(originalUrl.searchParams.get('revision'),/^[a-f0-9]{64}$/);
  await page.mouse.click(700,900);await page.getByAltText('Page 2',{exact:true}).waitFor();
  await page.mouse.click(700,100);await page.getByAltText('Page 1',{exact:true}).waitFor();
  await reader.focus();await page.keyboard.press('ArrowRight');await page.getByAltText('Page 2',{exact:true}).waitFor();
  await page.keyboard.press('PageDown');await page.getByAltText('Page 3',{exact:true}).waitFor();
  assert.equal(await reader.locator('.page-counter').textContent(),'3 / 13');
  await assertPagedImageFits();
  await shot('manga-reader-paged');
  check('Paged shows one contained page; bottom/top clicks and keyboard move exactly one page');
  await reader.focus();
  for(const [key,count] of [['ArrowLeft',2],['ArrowUp',1],['PageUp',1],['ArrowDown',2],['PageDown',3],[' ',4]]){
   await page.keyboard.press(key);assert.equal(await reader.locator('.page-counter').textContent(),count+' / 13');
  }
  for(let repeat=0;repeat<16;repeat++)await page.keyboard.down('ArrowRight');await page.keyboard.up('ArrowRight');
  assert.equal(await reader.locator('.page-counter').textContent(),'13 / 13');
  for(let repeat=0;repeat<16;repeat++)await page.keyboard.down('ArrowLeft');await page.keyboard.up('ArrowLeft');
  assert.equal(await reader.locator('.page-counter').textContent(),'1 / 13');
  await page.getByLabel('Reading layout').focus();await page.keyboard.press('ArrowDown');
  assert.equal(await page.getByLabel('Reading layout').inputValue(),'vertical');assert.equal(await reader.locator('.page-counter').textContent(),'1 / 13');
  await page.keyboard.press('ArrowUp');assert.equal(await page.getByLabel('Reading layout').inputValue(),'paged');
  assert.equal(await reader.locator('.page-counter').textContent(),'1 / 13');
  await page.mouse.click(700,750);assert.equal(await reader.locator('.page-counter').textContent(),'2 / 13');
  await page.mouse.click(700,250);assert.equal(await reader.locator('.page-counter').textContent(),'1 / 13');
  await page.keyboard.press('ArrowRight');assert.equal(await reader.locator('.page-counter').textContent(),'2 / 13');
  await page.mouse.move(700,500);await page.mouse.down();await page.mouse.move(700,350,{steps:5});await page.mouse.up();
  assert.equal(await reader.locator('.page-counter').textContent(),'2 / 13');
  assert.equal(await reader.locator('.reader-controls').getAttribute('aria-hidden'),'false');
  await reader.focus();await page.keyboard.press('ArrowRight');
  check('all Paged keys and native repeat events clamp; focused layout arrows remain native, exact mouse zones turn once, and dragging does not toggle controls');
  await page.mouse.click(700,500);
  await page.waitForTimeout(230);
  assert.equal(await reader.locator('.reader-controls').evaluate(n=>getComputedStyle(n).opacity),'0');
  assert.equal(await reader.locator('.page-counter').evaluate(n=>getComputedStyle(n.closest('.reader-controls')).opacity),'0');
  assert.equal(await reader.locator('.reader-controls').getAttribute('aria-hidden'),'true');
  await page.keyboard.press('Tab');
  await page.waitForTimeout(230);
  assert.equal(await reader.locator('.reader-controls').evaluate(n=>getComputedStyle(n).opacity),'1');
  await page.mouse.click(700,500);await page.mouse.click(700,500);
  await page.waitForTimeout(230);
  assert.equal(await reader.locator('.reader-controls').evaluate(n=>getComputedStyle(n).opacity),'1');
  assert.equal(await page.getByAltText('Page 3',{exact:true}).count(),1);
  check('center click animates reader controls without turning a page; Tab restores hidden controls');
  const back=page.getByRole('button',{name:'Back to chapters'});
  await page.mouse.move(1100,400);await page.waitForTimeout(220);
  const resting=(await back.boundingBox()).width;
  const captured=back.evaluate(n=>new Promise(resolve=>{
    const frames=[];const deadline=performance.now()+3000;let started=null;
    function sample(now){
      if(n.matches(':hover'))started??=now;
      if(started!==null)frames.push({time:now-started,width:n.getBoundingClientRect().width});
      if(started!==null&&now-started>350||now>deadline)resolve(frames);
      else requestAnimationFrame(sample);
    }
    requestAnimationFrame(sample);
  }));
  await back.hover();
  const frames=await captured;const expanded=(await back.boundingBox()).width;
  const middle=frames.find(frame=>frame.width>resting&&frame.width<expanded)?.width??resting;
  report.backHoverFrames=frames;
  assert(middle>resting&&middle<expanded);
  assert.equal(await back.evaluate(n=>getComputedStyle(n).backgroundColor),'rgb(51, 51, 59)');
  await page.mouse.move(1100,400);await page.waitForTimeout(220);assert.equal((await back.boundingBox()).width,resting);
  await page.getByLabel('Reading layout').selectOption('vertical');
  await page.waitForTimeout(350);
  assert(await reader.evaluate(n=>n.scrollTop>0));
  const scrollBeforeWheel=await reader.evaluate(node=>node.scrollTop);
  await page.mouse.move(700,600);await page.mouse.wheel(0,1700);
  await page.waitForFunction(before=>document.querySelector('.reader').scrollTop>before,scrollBeforeWheel);
  async function assertVerticalCounter(){await page.waitForFunction(()=>{
   const reader=document.querySelector('.reader'),bounds=reader.getBoundingClientRect(),center=bounds.top+bounds.height/2;
   const current=[...reader.querySelectorAll('.page')].find(node=>{const rect=node.getBoundingClientRect();return rect.top<=center&&rect.bottom>center;});
   return current&&reader.querySelector('.page-counter').textContent===(Number(current.dataset.page)+1)+' / 13';
  });}
  await assertVerticalCounter();
  const scrollbar=await reader.evaluate(node=>{
   const bounds=node.getBoundingClientRect(),size=bounds.width-node.clientWidth,thumb=node.clientHeight*node.clientHeight/node.scrollHeight;
   return {size,x:bounds.right-size/2,y:bounds.top+node.scrollTop/(node.scrollHeight-node.clientHeight)*(node.clientHeight-thumb)+thumb/2,scroll:node.scrollTop};
  });
  if(scrollbar.size>0){
   await page.mouse.move(scrollbar.x,scrollbar.y);await page.mouse.down();await page.mouse.move(scrollbar.x,scrollbar.y+180,{steps:8});await page.mouse.up();
   await page.waitForFunction(before=>document.querySelector('.reader').scrollTop>before,scrollbar.scroll);await assertVerticalCounter();
   report.navigation={scrollbarDrag:'native scrollbar drag passed'};
  }else report.navigation={scrollbarDrag:'native browser exposes no scrollbar hit area; wheel verified'};
  assert.equal(await reader.locator('.reader-controls').getAttribute('aria-hidden'),'false');
  check('Vertical mouse wheel and available native scrollbar dragging scroll naturally, update the reading-center counter and keep controls visible');
  const width=await page.locator('.page[data-page="1"]').evaluate(n=>n.getBoundingClientRect().width);assert.equal(width,1000);
  await reader.evaluate(node=>{
   const target=node.querySelector('.page[data-page="5"]'),bounds=node.getBoundingClientRect(),rect=target.getBoundingClientRect();
   node.scrollTop+=rect.top+rect.height/2-(bounds.top+bounds.height/2);
  });
  await page.waitForFunction(()=>document.querySelector('.page-counter')?.textContent==='6 / 13');
  const verticalCurrent=await reader.locator('.page-counter').textContent();
  await shot('manga-reader-vertical');
  await page.getByLabel('Reading layout').selectOption('paged');
  assert.equal(await reader.locator('.paged-page').getAttribute('data-page'),'5');
  assert.equal(await reader.locator('.page-counter').textContent(),verticalCurrent);
  await reader.focus();
  for(let i=0;i<7;i++)await page.keyboard.press('ArrowRight');
  await page.getByAltText('Page 13',{exact:true}).waitFor();
  assert.equal(await reader.locator('.page-counter').textContent(),'13 / 13');
  await page.keyboard.press('ArrowRight');await reader.getByRole('group',{name:'Chapter boundary',exact:true}).waitFor();
  assert.equal(await page.getByAltText('Page 13',{exact:true}).count(),1);
  await reader.getByRole('button',{name:'Return to page',exact:true}).click();
  await page.emulateMedia({reducedMotion:'reduce'});
  await page.mouse.click(700,500);
  assert.equal(await reader.locator('.reader-controls').evaluate(n=>getComputedStyle(n).transitionDuration),'0s');
  await page.mouse.click(700,500);
  await page.emulateMedia({reducedMotion:'no-preference'});
  await page.getByLabel('Reading layout').focus();await page.keyboard.press('Tab');assert(await back.evaluate(n=>n===document.activeElement));
  await back.click();await reader.waitFor({state:'detached'});assert(await dialog.getByRole('button',{name:'Chapter 1',exact:true}).evaluate(n=>n===document.activeElement));
  check('chapter window closes outside; vertical scroll and Paged retain position, show the last-page boundary, respect reduced motion and restore focus');
  await dialog.getByRole('button',{name:'Chapter 2',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('img[alt="Page 1"]')?.naturalWidth>0);
  await page.keyboard.press('Escape');await reader.waitFor({state:'detached'});
  await page.keyboard.press('Escape');await dialog.waitFor({state:'detached'});
  check('CBZ chapter pages decode without extraction and Escape returns through chapters to the library');
  await tile.click();await dialog.getByRole('button',{name:'Chapter 10',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.paged-page img')?.naturalWidth>0);
  await reader.focus();for(let i=0;i<4;i++)await page.keyboard.press('ArrowRight');
  await page.getByAltText('Page 5',{exact:true}).waitFor();await assertPagedImageFits();
  const oriented=await reader.locator('img').evaluate(image=>({width:image.naturalWidth,height:image.naturalHeight,url:image.getAttribute('src')}));
  assert.equal(oriented.width,60);assert.equal(oriented.height,120);assert(oriented.url.includes('representation=original'));
  await page.keyboard.press('ArrowRight');await page.getByRole('alert').filter({hasText:'Original page 6'}).waitFor();
  await page.getByRole('button',{name:'Open converted page',exact:true}).focus();await page.keyboard.press('Space');
  await page.waitForFunction(()=>document.querySelector('.paged-page img')?.complete&&document.querySelector('.paged-page img')?.naturalWidth===120);
  assert((await reader.locator('img').getAttribute('src')).includes('representation=webp'));
  assert.equal(await reader.locator('.paged-page').getAttribute('data-page'),'5');
  await reader.focus();await page.keyboard.press('ArrowRight');await page.getByRole('alert').filter({hasText:'Original page 7'}).waitFor();
  await page.getByRole('button',{name:'Open converted page',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.paged-page img')?.complete&&document.querySelector('.paged-page img')?.naturalWidth===120);
  await shot('manga-original-conversion');await page.keyboard.press('Escape');await reader.waitFor({state:'detached'});await page.keyboard.press('Escape');await dialog.waitFor({state:'detached'});
  check('Original JPEG MIME and EXIF orientation render correctly; BMP and animation require explicit converted-still acceptance');
  await context.route('**/api/manga/pages?*',route=>route.fulfill({status:422,contentType:'application/json',body:JSON.stringify({detail:'This chapter cannot be opened. See Logs.'})}));
  await tile.click();await dialog.getByRole('button',{name:'Chapter 1',exact:true}).click();
  await page.getByRole('alert').filter({hasText:'See Logs'}).waitFor();
  assert.equal(await reader.locator('.page-counter').textContent(),'— / —');
  await back.click();await reader.waitFor({state:'detached'});await page.keyboard.press('Escape');
  await context.unroute('**/api/manga/pages?*');
  check('chapter errors remain concise and preserve the route back to the library');
  const pendingCount=async route=>{
   if(new URL(route.request().url()).searchParams.get('path')!=='The Quiet Valley/Chapter 1'){await route.continue();return;}
   const response=await route.fetch();await new Promise(resolve=>setTimeout(resolve,450));await route.fulfill({response});
  };
  await page.route('**/api/manga/pages?*',pendingCount);
  try{
   await tile.click();await dialog.getByRole('button',{name:'Chapter 1',exact:true}).click();
   assert.equal(await reader.locator('.page-counter').textContent(),'— / —');
   await page.keyboard.press('Escape');await reader.waitFor({state:'detached'});
   assert(await dialog.getByRole('button',{name:'Chapter 1',exact:true}).evaluate(node=>node===document.activeElement));
   await dialog.getByRole('button',{name:'Chapter 2',exact:true}).click();await assertPagedImageFits();
   await page.waitForTimeout(550);assert.equal(await reader.locator('.page-counter').textContent(),'1 / 4');
   assert((await reader.locator('img').getAttribute('src')).includes('Chapter+2.cbz'));
   await back.click();await reader.waitFor({state:'detached'});await page.keyboard.press('Escape');
  }finally{await page.unroute('**/api/manga/pages?*',pendingCount);}
  check('Escape cancels delayed chapter counts, restores chapter focus and ignores their late response after opening another chapter');
  await tile.click();await dialog.getByRole('button',{name:'Chapter 1',exact:true}).click();await assertPagedImageFits();
  const previousUrl=await reader.locator('img').getAttribute('src');
  const coverFile=path.join(config.mediaLocal,'The Quiet Valley','Chapter 1','00-cover.png');
  const preservedFixture=fs.readFileSync(coverFile);
  try{
   fs.writeFileSync(coverFile,fs.readFileSync(path.join(config.mediaLocal,'The Quiet Valley','Chapter 1','1.png')));
   await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
   await page.getByRole('alert').filter({hasText:'This chapter changed'}).waitFor();
   assert.equal(await reader.locator('img').count(),0);
   await page.getByRole('button',{name:'Reopen chapter',exact:true}).click();await assertPagedImageFits();
   assert.notEqual(await reader.locator('img').getAttribute('src'),previousUrl);
   const openedUrl=await reader.locator('img').getAttribute('src');
   assert.equal((await api('/api/suite/folders/apply','POST',{folders:[{source_id:sourceId,path:config.media,display_name:'Manga',role:'manga',visible:false}]})).status,200);
   await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await reader.waitFor({state:'detached'});
   assert.equal(await page.locator('.manga-tile img').count(),0);
   assert.equal((await api(openedUrl)).status,404);
  }finally{fs.writeFileSync(coverFile,preservedFixture);}
  assert.equal((await api('/api/suite/folders/apply','POST',{folders:[{source_id:sourceId,path:config.media,display_name:'Manga',role:'manga',visible:true}]})).status,200);
  await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await tile.waitFor();
  await page.waitForFunction(()=>document.querySelectorAll('.manga-tile img').length===8&&[...document.querySelectorAll('.manga-tile img')].every(image=>image.complete&&image.naturalWidth>0));
  check('changed chapters discard mounted images and reopen with new revisions; hidden sources drop reader and cover references');
  assert.equal((await api('/api/suite/modules/manga/disable','POST')).status,200);
  assert.equal((await api('/api/manga/library')).status,409);
  assert.equal((await api('/api/manga/page?'+new URLSearchParams({source_id:sourceId,path:'The Quiet Valley/Chapter 1',kind:'folder',representation:'original'}))).status,409);
  assert((await api('/api/files/sources')).data.some(s=>s.role==='manga'));
  assert.equal((await api('/api/suite/modules/manga/enable','POST')).status,200);
  assert.equal((await api('/api/manga/library')).data.total,8);
  const sample=(await api('/api/manga/library')).data.items[0],windowRequests=[];
  const pagination=async route=>{
   const url=new URL(route.request().url());if(url.searchParams.get('q')!=='pagination'){await route.continue();return;}
   const offset=Number(url.searchParams.get('offset')||0);windowRequests.push(offset);
   const items=Array.from({length:120},(_,index)=>({...sample,path:'pagination-'+index,name:'Pagination series '+index}));
   await route.fulfill({json:{items:items.slice(offset,offset+60),total:120}});
  };
  await page.route('**/api/manga/library?*',pagination);
  await page.getByLabel('Search manga').fill('pagination');await page.waitForFunction(()=>document.querySelectorAll('.manga-tile').length===60);
  await page.getByRole('button',{name:'Load more',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('.manga-tile').length===120);
  const scrollBefore=await page.locator('.manga-surface main').evaluate(node=>node.scrollTop);
  windowRequests.length=0;await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
  await page.waitForFunction(()=>document.querySelector('.manga-grid')?.getAttribute('aria-busy')==='false');
  assert.deepEqual(windowRequests,[0,60]);assert.equal(await page.locator('.manga-tile').count(),120);
  assert.equal(await page.locator('.manga-surface main').evaluate(node=>node.scrollTop),scrollBefore);
  await page.unroute('**/api/manga/library?*',pagination);await page.getByLabel('Search manga').fill('');
  await page.waitForFunction(()=>document.querySelectorAll('.manga-tile').length===8);
  check('focus refresh preserves expanded Load more results and library scroll position');
  let readerTitle='boku ni onnanoko',metadataMode='normal',countRoutes=0;
  const titleRoute=async route=>{
   const response=await route.fetch(),body=await response.json();
   await route.fulfill({response,json:{...body,items:body.items.map(item=>item.path==='The Quiet Valley'?{...item,name:readerTitle}:item)}});
  };
  const countRoute=async route=>{
   if(metadataMode==='normal'){await route.continue();return;}
   countRoutes++;try{
   const response=await route.fetch(),body=await response.json();await new Promise(resolve=>setTimeout(resolve,260));
   if(metadataMode==='error'){await route.fulfill({status:422,json:{detail:'This chapter cannot be opened. See Logs.'}});return;}
   await route.fulfill({response,json:{...body,count:1,revisions:body.revisions.slice(0,1)}});
   }finally{countRoutes--;}
  };
  await page.route('**/api/manga/library?*',titleRoute);await page.route('**/api/manga/pages?*',countRoute);
  try{
   for(const name of ['boku ni onnanoko','静かな谷を越えて旅を続ける物語 '.repeat(8)+'unbroken'.repeat(18)]){
    readerTitle=name;await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
    const namedTile=page.getByRole('button',{name,exact:true});await namedTile.waitFor();await namedTile.click();
    const namedDialog=page.getByRole('dialog',{name:name+' chapters'});
    assert.equal(await namedDialog.locator('.series-title').textContent(),name);
    assert.equal(await namedDialog.locator('.series-title').getAttribute('title'),name);
    const headerBox=await namedDialog.locator('.chapter-heading').boundingBox(),coverBox=await namedDialog.locator('.chapter-cover').boundingBox();assert(headerBox.y+headerBox.height<=coverBox.y);
    await namedDialog.getByRole('button',{name:'Chapter 1',exact:true}).click();await assertPagedImageFits();
    await reader.focus();for(let i=0;i<20;i++)await page.keyboard.press('ArrowLeft');
    await page.waitForFunction(()=>document.querySelector('.paged-page')?.dataset.page==='0'&&document.querySelector('.paged-page img')?.naturalWidth>0);
    assert.equal(await reader.locator('.series-title').textContent(),name);assert.equal(await reader.locator('.series-title').getAttribute('title'),name);
    for(const width of [1440,720,360]){
     await page.setViewportSize({width,height:1000});
     const boxes=await reader.locator('.reader-controls').evaluate(node=>{
      const parts=[...node.children].map(child=>{const rect=child.getBoundingClientRect();return {x:rect.x,y:rect.y,right:rect.right,bottom:rect.bottom,width:rect.width};});
      const title=node.querySelector('.series-title');return {parts,titleClamp:getComputedStyle(title).webkitLineClamp,backing:getComputedStyle(node.querySelector('.reader-heading')).backgroundColor};
     });
     assert.equal(boxes.titleClamp,'2');assert.equal(boxes.backing,'rgba(0, 0, 0, 0.6)');
     const counter=boxes.parts[1];assert(Math.abs(counter.x+counter.width/2-width/2)<1);
     for(const part of boxes.parts)assert(part.x>=0&&part.right<=width);
     for(let first=0;first<boxes.parts.length;first++)for(let second=first+1;second<boxes.parts.length;second++){
      const a=boxes.parts[first],b=boxes.parts[second];assert(!(a.x<b.right&&a.right>b.x&&a.y<b.bottom&&a.bottom>b.y),'header controls must not overlap');
     }
     await back.hover();await page.waitForTimeout(220);
     const afterHover=await reader.locator('.page-counter').boundingBox();assert(Math.abs(afterHover.x+afterHover.width/2-width/2)<1);
     await page.mouse.move(width-20,500);
    }
    await shot('manga-header-compact');await page.setViewportSize({width:1440,height:1000});
    await reader.focus();await page.keyboard.press('ArrowRight');await assertPagedImageFits();
    assert.equal(await reader.locator('.page-counter').textContent(),'2 / 13');await shot('manga-header-bright');
    await back.click();await reader.waitFor({state:'detached'});await page.keyboard.press('Escape');
   }
   check('real series names appear beside Back with two-line full titles; translucent header and centered counter avoid collisions across widths and hover');
   metadataMode='single';await page.setViewportSize({width:360,height:1000});
   const namedTile=page.getByRole('button',{name:readerTitle,exact:true});await namedTile.click();
   const namedDialog=page.getByRole('dialog',{name:readerTitle+' chapters'});
   await namedDialog.getByRole('button',{name:'Chapter 1',exact:true}).click();
   assert.equal(await reader.locator('.page-counter').textContent(),'— / —');
   await assertPagedImageFits();assert.equal(await reader.locator('.page-counter').textContent(),'1 / 1');
   await reader.focus();await page.keyboard.press('ArrowRight');assert.equal(await reader.locator('.page-counter').textContent(),'1 / 1');
   await page.getByLabel('Reading layout').selectOption('vertical');await page.waitForTimeout(120);assert.equal(await reader.locator('.page-counter').textContent(),'1 / 1');
   await page.getByLabel('Reading layout').selectOption('paged');await back.click();await reader.waitFor({state:'detached'});
   metadataMode='error';await namedDialog.getByRole('button',{name:'Chapter 1',exact:true}).click();
   assert.equal(await reader.locator('.page-counter').textContent(),'— / —');
   await reader.getByRole('alert').waitFor();assert.equal(await reader.locator('.page-counter').textContent(),'— / —');
   const errorBox=await reader.getByRole('alert').boundingBox(),controlsBox=await reader.locator('.reader-controls').boundingBox();assert(errorBox.y>=controlsBox.y+controlsBox.height);
   await back.click();await reader.waitFor({state:'detached'});await page.keyboard.press('Escape');
   check('loading and errors keep a neutral page count; a single-page chapter stays 1/1 in both layouts and compact errors clear the header');
  }finally{
   while(countRoutes)await new Promise(resolve=>setTimeout(resolve,25));
   await page.unroute('**/api/manga/library?*',titleRoute);await page.unroute('**/api/manga/pages?*',countRoute);
   await page.setViewportSize({width:1440,height:1000});await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await tile.waitFor();
  }
  const warmContext=await browser.newContext({viewport:{width:1440,height:1000}});
  // These performance cases deliberately start fresh; durable direct resume
  // is exercised separately below with unmodified backend responses.
  await warmContext.route('**/api/manga/progress/read',async route=>{const response=await route.fetch(),data=await response.json();await route.fulfill({response,json:{...data,position:null}});});
  await warmContext.tracing.start({screenshots:true,snapshots:true});
  await warmContext.addInitScript(()=>{
   const NativeImage=window.Image;window.__warmImages=[];window.__warmStarts=[];
   const src=Object.getOwnPropertyDescriptor(HTMLImageElement.prototype,'src');
   const width=Object.getOwnPropertyDescriptor(HTMLImageElement.prototype,'naturalWidth').get,height=Object.getOwnPropertyDescriptor(HTMLImageElement.prototype,'naturalHeight').get;
   window.Image=function(...args){
    const image=new NativeImage(...args);window.__warmImages.push(new WeakRef(image));
    Object.defineProperty(image,'src',{get:()=>src.get.call(image),set:value=>{if(image.fetchPriority==='low')window.__warmStarts.push(value);src.set.call(image,value);}});
    // Exercise the retention policy without allocating oversized fixture images.
    Object.defineProperty(image,'naturalWidth',{get:()=>window.__largeWarmPages&&image.fetchPriority==='low'?2800:width.call(image)});
    Object.defineProperty(image,'naturalHeight',{get:()=>window.__largeWarmPages&&image.fetchPriority==='low'?2800:height.call(image)});
    return image;
   };
   window.Image.prototype=NativeImage.prototype;
  });
  const warm=await warmContext.newPage();warm.setDefaultTimeout(15000);
  watchErrors(warm);
  const requests=[];let delay=100,holdFirst=false,holdNeighbor=false,active=0,peak=0;
  const warmerImages=()=>warm.evaluate(()=>window.__warmImages.map(ref=>ref.deref()).filter(image=>image?.fetchPriority==='low'&&image.hasAttribute('src')).map(image=>({url:image.getAttribute('src'),complete:image.complete,width:image.naturalWidth,height:image.naturalHeight})));
  const settled=()=>warm.waitForFunction(()=>window.__warmImages.map(ref=>ref.deref()).filter(image=>image?.fetchPriority==='low'&&image.hasAttribute('src')).every(image=>image.complete&&image.naturalWidth>0));
  const fullPageRoute=async route=>{
   const url=new URL(route.request().url());if(url.searchParams.get('cover')!=='false'){await route.continue();return;}
   const index=Number(url.searchParams.get('page')),chapter=url.searchParams.get('path');
   requests.push({index,chapter,representation:url.searchParams.get('representation'),time:Date.now()});active++;peak=Math.max(peak,active);
   try{
    const response=await route.fetch();
    await new Promise(resolve=>setTimeout(resolve,(holdFirst&&index===0?350:holdNeighbor&&index===1?650:delay)));
    await route.fulfill({response});
   }finally{active--;}
  };
  await warm.route('**/api/manga/page?*',fullPageRoute);
  try{
   await warm.goto(config.url);await warm.getByRole('button',{name:'Open Keivotos menu'}).click();
   await warm.locator('.app-drawer').getByRole('button',{name:'Manga',exact:true}).click();
   const warmTile=warm.getByRole('button',{name:'The Quiet Valley',exact:true}),warmReader=warm.getByRole('dialog',{name:'Manga reader'});
   const warmDialog=warm.getByRole('dialog',{name:'The Quiet Valley chapters'});
   async function openChapter(name){await warmTile.click();await warmDialog.getByRole('button',{name,exact:true}).click();await readerReady(warm);}
   async function exitChapter(){await warm.getByRole('button',{name:'Back to chapters'}).click();await warmReader.waitFor({state:'detached'});assert.equal((await warmerImages()).length,0);}
   async function ready(index){await warm.waitForFunction(index=>{const image=document.querySelector('.paged-page img');return image?.complete&&image.naturalWidth>0&&Number(image.closest('.paged-page').dataset.page)===index;},index);}
   const firstRequest=warm.waitForRequest(request=>new URL(request.url()).searchParams.get('cover')==='false');
   holdFirst=true;await openChapter('Chapter 1');await firstRequest;
   await warm.waitForTimeout(90);assert.deepEqual(requests.map(request=>request.index),[0]);assert.equal((await warmerImages()).length,0);
   await ready(0);await warm.waitForFunction(()=>window.__warmImages.filter(ref=>ref.deref()?.getAttribute('src')).length===2);await settled();
   assert.deepEqual(requests.map(request=>request.index),[0,1,2]);assert.equal(peak,1);
   report.prefetch={coldSequence:requests.map(request=>request.index),initialRequestConcurrency:peak,warmTurns:[]};
   holdFirst=false;await warmReader.focus();
   await warm.evaluate(()=>{
    const reader=document.querySelector('.reader');window.__turnTimes=[];let started=0;
    reader.addEventListener('keydown',event=>{if(['ArrowRight','ArrowLeft'].includes(event.key))started=performance.now();},true);
    reader.addEventListener('load',event=>{if(event.target.matches('.paged-page img')&&started){window.__turnTimes.push(performance.now()-started);started=0;}},true);
   });
   for(let sample=0;sample<3;sample++){
    await warm.keyboard.press('ArrowRight');await ready(1);await settled();
    await warm.keyboard.press('ArrowLeft');await ready(0);await settled();
   }
   report.prefetch.warmTurns=await warm.evaluate(()=>window.__turnTimes);
   assert.equal(requests.filter(request=>request.index===1).length,1,'warm visible page must reuse its prefetched image');
   assert.equal(requests.filter(request=>request.index===0).length,1,'revisit must reuse the displayed original');
   assert((await warmerImages()).length<=4);
   check('visible page completes before serial low-priority next-two prefetch; warm turns and revisits reuse exact revision URLs');
   for(let index=0;index<20;index++)await warm.keyboard.down('ArrowRight');await warm.keyboard.up('ArrowRight');await ready(12);await settled();
   assert(requests.every(request=>request.index>=0&&request.index<13));
   const lastImages=await warmerImages();assert(lastImages.every(image=>[11,12].includes(Number(new URL(image.url,config.url).searchParams.get('page')))));
   await exitChapter();await warm.keyboard.press('Escape');
   check('first/last boundaries remain bounded and closing the reader releases every retained preview');
   // A new document removes the old reader's image sharing window.
   await warm.reload();await warm.getByRole('button',{name:'Open Keivotos menu'}).click();
   await warm.locator('.app-drawer').getByRole('button',{name:'Manga',exact:true}).click();
   requests.length=0;holdNeighbor=true;
   await openChapter('Chapter 1');await ready(0);
   await warm.waitForFunction(()=>window.__warmImages.some(ref=>ref.deref()?.getAttribute('src')?.includes('page=1&')));
   await warmReader.focus();for(let index=0;index<6;index++)await warm.keyboard.press('ArrowRight');await ready(6);
   await warm.waitForTimeout(720);await settled();
   assert(!(await warm.evaluate(()=>window.__warmStarts)).some(url=>Number(new URL(url,config.url).searchParams.get('page'))===2),'cancelled old neighbor must not start its old next-two prefetch');
   assert((await warmerImages()).every(image=>[5,6,7,8].includes(Number(new URL(image.url,config.url).searchParams.get('page')))));
   await warm.getByLabel('Reading layout').selectOption('vertical');assert.equal((await warmerImages()).length,0);
   await warm.waitForTimeout(250);assert((await warmReader.locator('img').count())>0&&(await warmReader.locator('img').count())<13);
   await warm.getByLabel('Reading layout').selectOption('paged');await exitChapter();
   holdNeighbor=false;await warm.evaluate(()=>window.__largeWarmPages=true);
   await warmDialog.getByRole('button',{name:'Chapter 2',exact:true}).click();await readerReady(warm);await ready(0);
   await warm.waitForTimeout(500);await settled();
   const capped=await warmerImages();assert.equal(capped.length,1);
   assert.equal(capped.reduce((sum,image)=>sum+image.width*image.height*4,0),31360000);
   assert.deepEqual(requests.filter(request=>request.chapter==='The Quiet Valley/Chapter 2.cbz').map(request=>request.index),[0,1,2]);
   report.prefetch.budgetProbe={method:'2800x2800 dimension override on detached previews; no corresponding pixel allocation',retainedImages:capped.length,estimatedDecodedBytes:31360000,limitBytes:32*1024*1024};
   await warm.evaluate(()=>window.__largeWarmPages=false);
   assert((await warmerImages()).every(image=>new URL(image.url,config.url).searchParams.get('path')==='The Quiet Valley/Chapter 2.cbz'));
   await exitChapter();await warmDialog.getByRole('button',{name:'Chapter 10',exact:true}).click();await readerReady(warm);await ready(0);
   await warmReader.focus();for(let index=0;index<3;index++)await warm.keyboard.press('ArrowRight');await ready(3);
   await warm.waitForTimeout(450);assert.equal(await warmReader.getByRole('alert').count(),0);
   assert(requests.some(request=>request.chapter==='The Quiet Valley/Chapter 10.cbz'&&request.index===5));
   assert(!requests.some(request=>request.chapter==='The Quiet Valley/Chapter 10.cbz'&&request.index===5&&request.representation==='webp'));
   await warm.keyboard.press('ArrowRight');await ready(4);await warm.keyboard.press('ArrowRight');
   await warm.getByRole('alert').filter({hasText:'Original page 6'}).waitFor();
   await exitChapter();await warm.keyboard.press('Escape');
   check('rapid turns abandon stale work; mode/chapter switches clear previews; adjacent failure stays silent until displayed');
  }finally{
   await warm.unrouteAll({behavior:'wait'});
   await warmContext.tracing.stop({path:path.join(config.output,'prefetch-trace.zip')});await warmContext.close();
  }
  const corruptContext=await browser.newContext({viewport:{width:1440,height:1000}});
  await corruptContext.route('**/api/manga/progress/read',async route=>{const response=await route.fetch(),data=await response.json();await route.fulfill({response,json:{...data,position:null}});});
  await corruptContext.tracing.start({screenshots:true,snapshots:true});
  const corrupt=await corruptContext.newPage();watchErrors(corrupt);
  await corrupt.route('**/api/manga/page?*',async route=>{
   const url=new URL(route.request().url());
   if(url.searchParams.get('cover')==='false'&&url.searchParams.get('page')==='0'&&url.searchParams.get('representation')==='original'){
    await route.fulfill({status:200,contentType:'image/png',headers:{'Cache-Control':'no-store'},body:'deliberately invalid generated image response'});
   }else await route.continue();
  });
  try{
   await corrupt.goto(config.url);await corrupt.getByRole('button',{name:'Open Keivotos menu'}).click();
   await corrupt.locator('.app-drawer').getByRole('button',{name:'Manga',exact:true}).click();
   await corrupt.getByRole('button',{name:'The Quiet Valley',exact:true}).click();
   await corrupt.getByRole('dialog',{name:'The Quiet Valley chapters'}).getByRole('button',{name:'Chapter 2',exact:true}).click();
   await readerReady(corrupt);
   const failedReader=corrupt.getByRole('dialog',{name:'Manga reader'});
   await failedReader.getByRole('alert').filter({hasText:'Original page 1'}).waitFor();
   assert.equal(await failedReader.locator('.page-counter').textContent(),'1 / 4');
   await failedReader.focus();await corrupt.keyboard.press('ArrowRight');
   await corrupt.waitForFunction(()=>document.querySelector('.paged-page img')?.naturalWidth>0);
   assert.equal(await failedReader.locator('.page-counter').textContent(),'2 / 4');assert.equal(await failedReader.getByRole('alert').count(),0);
   await corrupt.keyboard.press('ArrowLeft');await failedReader.getByRole('alert').filter({hasText:'Original page 1'}).waitFor();
   await corrupt.getByRole('button',{name:'Open converted page',exact:true}).focus();await corrupt.keyboard.press('Space');
   await corrupt.waitForFunction(()=>document.querySelector('.paged-page img')?.naturalWidth>0);
   assert.equal(await failedReader.locator('.page-counter').textContent(),'1 / 4');
   assert((await failedReader.locator('img').getAttribute('src')).includes('representation=webp'));
   await corrupt.keyboard.press('Escape');await failedReader.waitFor({state:'detached'});
   check('a browser-rejected image keeps its page count, allows navigation away/back and accepts explicit conversion with Space without turning pages');
  }finally{
   await corruptContext.tracing.stop({path:path.join(config.output,'navigation-error-trace.zip')});await corruptContext.close();
  }
  let progressContext=await browser.newContext({viewport:{width:1440,height:1000}});
  let progressPage=await progressContext.newPage();watchErrors(progressPage);
  async function enterProgress(){
   await progressPage.goto(config.url);
   await progressPage.getByRole('button',{name:'Open Keivotos menu'}).click();
   await progressPage.locator('.app-drawer').getByRole('button',{name:'Manga',exact:true}).click();
   await progressPage.getByRole('button',{name:'The Quiet Valley',exact:true}).click();
   await progressPage.getByRole('dialog',{name:'The Quiet Valley chapters'}).getByRole('button',{name:'Chapter 1',exact:true}).click();
  }
  async function progressData(){return progressPage.evaluate(async source_id=>{
   const metadata=await(await fetch('/api/manga/pages?'+new URLSearchParams({source_id,path:'The Quiet Valley/Chapter 1',kind:'folder',metadata:'true'}))).json();
   return (await fetch('/api/manga/progress/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_id,series_path:'The Quiet Valley',path:'The Quiet Valley/Chapter 1',kind:'folder',revision:metadata.revision})})).json();
  },sourceId);}
  async function progressReady(index){await progressPage.waitForFunction(index=>{
   const image=document.querySelector('.paged-page img');return image?.complete&&image.naturalWidth>0&&Number(image.closest('.paged-page').dataset.page)===index;
  },index);}
  await enterProgress();await readerReady(progressPage);
  const progressReader=()=>progressPage.getByRole('dialog',{name:'Manga reader'});
  await progressPage.getByLabel('Reading layout').selectOption('paged');
  await progressReader().focus();for(let i=0;i<20;i++)await progressPage.keyboard.press('ArrowLeft');await progressReady(0);
  await progressReader().focus();for(let i=0;i<3;i++)await progressPage.keyboard.press('ArrowRight');await progressReady(3);
  await progressPage.getByRole('button',{name:'Back to chapters'}).click();await progressReader().waitFor({state:'detached'});
  assert.equal((await progressData()).position.page,3,'prefetched pages cannot overwrite displayed progress');
  const progressDialog=()=>progressPage.getByRole('dialog',{name:'The Quiet Valley chapters'});
  const firstBar=()=>progressDialog().getByRole('progressbar',{name:'Chapter 1 reading position'});
  await progressPage.waitForFunction(()=>document.querySelector('.chapter-list').getAttribute('aria-busy')==='false');
  assert.equal(await firstBar().getAttribute('aria-valuetext'),'Page 4 of 13');
  assert(Math.abs(Number(await firstBar().getAttribute('aria-valuenow'))-4/13*100)<.01);
  assert.equal(await progressDialog().getByRole('button',{name:/Continue reading/}).count(),0);
  assert.equal(await progressDialog().getByRole('progressbar',{name:'Chapter 2 reading position'}).count(),1);
  const firstRow=progressDialog().getByRole('button',{name:'Chapter 1',exact:true});
  await progressPage.waitForFunction(()=>!document.querySelector('.metadata-actions button:disabled'));
  await progressDialog().getByRole('button',{name:'Back to manga',exact:true}).focus();await progressPage.keyboard.press('Tab');
  assert(await progressDialog().getByRole('button',{name:'Refresh',exact:true}).evaluate(node=>node===document.activeElement));await progressPage.keyboard.press('Tab');
  assert(await progressDialog().getByRole('button',{name:'Edit',exact:true}).evaluate(node=>node===document.activeElement));await progressPage.keyboard.press('Tab');
  assert(await firstRow.evaluate(node=>node===document.activeElement&&node.matches(':focus-visible')));
  const compact=await firstRow.evaluate(node=>{const r=node.getBoundingClientRect(),bar=node.querySelector('.chapter-progress').getBoundingClientRect(),fill=node.querySelector('.chapter-progress-fill').getBoundingClientRect(),style=getComputedStyle(node);return {text:node.textContent.trim(),height:r.height,barHeight:bar.height,left:bar.left-r.left,right:r.right-bar.right,bottom:r.bottom-bar.bottom,fraction:fill.width/bar.width,outline:style.outlineColor,outlineWidth:parseFloat(style.outlineWidth),outlineOffset:parseFloat(style.outlineOffset),clipped:style.overflow==='hidden',radius:parseFloat(style.borderRadius)};});
  assert.equal(compact.text,'Chapter 1›');assert.equal(compact.height,46);assert.equal(compact.barHeight,4);
  assert.equal(compact.left,0);assert.equal(compact.right,0);assert.equal(compact.bottom,0);
  assert(Math.abs(compact.fraction-4/13)<.001);assert(compact.clipped&&compact.radius>0);
  assert.equal(compact.outlineWidth,2);assert.equal(compact.outlineOffset,-2);
  assert(!/Not started|\d+\s*\/\s*\d+/.test(await progressDialog().locator('.chapter-list').textContent()));
  const accent=await firstBar().locator('.chapter-progress-fill').evaluate(node=>({fill:getComputedStyle(node).backgroundColor,expected:getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()}));
  const expectedAccent=await progressPage.evaluate(color=>{const node=document.createElement('span');node.style.color=color;document.body.append(node);const result=getComputedStyle(node).color;node.remove();return result;},accent.expected);
  assert.equal(accent.fill,expectedAccent);assert.equal(compact.outline,expectedAccent);
  await progressPage.screenshot({path:path.join(config.output,'manga-chapter-progress.png')});
  check('46px chapter rows show only title and a 4px progress fill flush with the rounded bottom edge, matching the focus outline accent; the partial fill matches saved progress, and page/count remains accessible without visible status labels or a Continue button');
  await progressContext.close();
  progressContext=await browser.newContext({viewport:{width:1440,height:1000}});progressPage=await progressContext.newPage();watchErrors(progressPage);
  await enterProgress();
  await progressReady(3);await readerReady(progressPage);
  assert(await progressReader().evaluate(n=>n===document.activeElement));
  await progressReader().press('Tab');assert(await progressPage.getByRole('button',{name:'Back to chapters',exact:true}).evaluate(n=>n===document.activeElement));
  check('clicking a chapter directly resumes its displayed Paged position in a fresh browser session, with reader focus and no chooser');
  await progressPage.getByLabel('Reading layout').selectOption('vertical');
  await progressPage.waitForFunction(()=>document.querySelector('.page[data-page="3"] img')?.naturalWidth>0);
  await progressReader().evaluate(node=>{const target=node.querySelector('.page[data-page="5"]'),rect=target.getBoundingClientRect(),bounds=node.getBoundingClientRect();node.scrollTop+=rect.top+rect.height*0.4-(bounds.top+bounds.height/2);});
  await progressPage.waitForFunction(()=>document.querySelector('.page[data-page="5"] img')?.naturalWidth>0&&document.querySelector('.page-counter').textContent==='6 / 13');
  await progressPage.waitForTimeout(100);
  await progressPage.getByRole('button',{name:'Back to chapters'}).click();await progressReader().waitFor({state:'detached'});
  const verticalSaved=(await progressData()).position;
  assert.equal(verticalSaved.page,5);assert.equal(verticalSaved.mode,'vertical');assert(Math.abs(verticalSaved.anchor-0.4)<0.03);
  await progressPage.getByRole('dialog',{name:'The Quiet Valley chapters'}).getByRole('button',{name:'Chapter 1',exact:true}).click();
  assert.equal(await progressPage.getByRole('alertdialog',{name:'Resume manga'}).count(),0);
  await progressPage.waitForFunction(()=>document.querySelector('.page[data-page="5"] img')?.naturalWidth>0&&document.querySelector('.page-counter').textContent==='6 / 13');
  const restoredAnchor=await progressReader().evaluate(node=>{const rect=node.querySelector('.page[data-page="5"]').getBoundingClientRect(),bounds=node.getBoundingClientRect();return (bounds.top+bounds.height/2-rect.top)/rect.height;});
  assert(Math.abs(restoredAnchor-verticalSaved.anchor)<0.03);
  await progressPage.screenshot({path:path.join(config.output,'manga-progress-resume.png')});
  await progressPage.getByLabel('Reading layout').selectOption('paged');await progressReady(5);
  await progressPage.getByRole('button',{name:'Back to chapters'}).click();await progressReader().waitFor({state:'detached'});
  await progressPage.getByRole('dialog',{name:'The Quiet Valley chapters'}).getByRole('button',{name:'Chapter 1',exact:true}).click();
  await progressReady(5);
  await progressReader().focus();for(let i=0;i<5;i++)await progressPage.keyboard.press('ArrowLeft');await progressReady(0);
  await progressPage.getByRole('button',{name:'Back to chapters'}).click();await progressReader().waitFor({state:'detached'});
  assert.equal((await progressData()).position.page,0);
  check('direct Vertical resume restores the reading-center page and relative anchor; layout switches retain the page and ordinary page turns can return to the start');
  await progressDialog().getByRole('button',{name:'Chapter 1',exact:true}).click();await progressReady(0);
  const failedPositionRoute=route=>route.fulfill({status:503,json:{detail:'Generated temporary storage failure'}});
  await progressPage.route('**/api/manga/progress',failedPositionRoute);
  await progressReader().focus();await progressPage.keyboard.press('ArrowRight');await progressReady(1);
  await progressReader().getByRole('status').filter({hasText:'Reading position could not be saved'}).waitFor();
  assert.equal((await progressData()).position.page,0);
  await progressPage.unroute('**/api/manga/progress',failedPositionRoute);
  await progressPage.getByRole('button',{name:'Retry saving position',exact:true}).click();
  await progressReader().getByRole('status').filter({hasText:'Reading position could not be saved'}).waitFor({state:'detached'});
  assert.equal((await progressData()).position.page,1);
  assert.equal(await progressReader().locator('.page-counter').textContent(),'2 / 13');
  check('direct resume retains failed-save acknowledgement/retry and never advances confirmed progress on a failed write');
  await progressPage.getByRole('button',{name:'Back to chapters'}).click();await progressReader().waitFor({state:'detached'});
  await progressPage.waitForFunction(()=>document.querySelector('.chapter-list').getAttribute('aria-busy')==='false');
  assert.equal(await firstBar().getAttribute('aria-valuetext'),'Page 2 of 13');
  // Changed file identity must not apply old progress. Byte restoration alone
  // does not restore a POSIX change-time revision; history remains in SQLite.
  const progressCover=path.join(config.mediaLocal,'The Quiet Valley','Chapter 1','00-cover.png'),oldCover=fs.readFileSync(progressCover);
  const oldCoverStat=fs.statSync(progressCover);
  try{
   fs.writeFileSync(progressCover,fs.readFileSync(path.join(config.mediaLocal,'The Quiet Valley','Chapter 1','1.png')));
   await progressDialog().getByRole('button',{name:'Back to manga'}).click();
   await progressPage.getByRole('button',{name:'The Quiet Valley',exact:true}).click();
   await progressPage.waitForFunction(()=>document.querySelector('.chapter-list').getAttribute('aria-busy')==='false');
   assert.equal(await firstBar().getAttribute('aria-valuetext'),'Not started');
   await progressDialog().getByRole('button',{name:'Chapter 1',exact:true}).click();await progressReady(0);
   assert.equal(await progressPage.getByRole('alertdialog',{name:'Resume manga'}).count(),0);
   await progressPage.getByRole('button',{name:'Back to chapters'}).click();await progressReader().waitFor({state:'detached'});
  }finally{fs.writeFileSync(progressCover,oldCover);fs.utimesSync(progressCover,oldCoverStat.atime,oldCoverStat.mtime);}
  await progressDialog().getByRole('button',{name:'Back to manga'}).click();
  await progressPage.getByRole('button',{name:'The Quiet Valley',exact:true}).click();
  await progressPage.waitForFunction(()=>document.querySelector('.chapter-list').getAttribute('aria-busy')==='false');
  assert.equal(await firstBar().getAttribute('aria-valuetext'),'Not started');
  await progressDialog().getByRole('button',{name:'Chapter 1',exact:true}).click();await progressReady(0);
  await progressPage.getByRole('button',{name:'Back to chapters'}).click();await progressReader().waitFor({state:'detached'});
  check('changed chapter identities show empty progress and open at their first page without a prompt; restoring bytes alone never applies a stale position');
  await progressContext.close();
  const startupContext=await browser.newContext({viewport:{width:1440,height:1000}});
  const startup=await startupContext.newPage();watchErrors(startup);
  await startup.goto(config.url);await startup.getByRole('button',{name:'Open Keivotos menu'}).click();
  await startup.locator('.app-drawer').getByRole('button',{name:'Manga',exact:true}).click();
  await startup.getByRole('button',{name:'Blue Hour',exact:true}).click();
  const longDialog=startup.getByRole('dialog',{name:'Blue Hour chapters'}),longList=longDialog.locator('.chapter-list');
  await startup.waitForFunction(()=>document.querySelector('.chapter-list').getAttribute('aria-busy')==='false');
  assert.equal(await longDialog.getByRole('button',{name:/^Chapter /}).count(),134);
  const longBox=await longDialog.boundingBox();assert.equal(longBox.height,report.chapterWindow.height);assert.equal(longBox.width,report.chapterWindow.width);
  for(const viewport of [{width:1440,height:1000},{width:390,height:520},{width:720,height:360}]){
   await startup.setViewportSize(viewport);
   const geometry=await longDialog.evaluate(node=>{const r=node.getBoundingClientRect(),list=node.querySelector('.chapter-list'),b=list.getBoundingClientRect(),header=node.querySelector('.chapter-heading').getBoundingClientRect();return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,list:{top:b.top,bottom:b.bottom,height:b.height,scroll:list.scrollHeight,client:list.clientHeight},headerBottom:header.bottom};});
   assert(geometry.left>=0&&geometry.top>=0&&geometry.right<=viewport.width&&geometry.bottom<=viewport.height);
   assert(geometry.list.top>=geometry.headerBottom&&geometry.list.bottom<=geometry.bottom);
   assert(geometry.list.scroll>geometry.list.client&&geometry.list.client>0);
   await longList.hover();const before=await longList.evaluate(n=>n.scrollTop);
   await startup.mouse.wheel(0,240);await startup.waitForFunction(before=>document.querySelector('.chapter-list').scrollTop>before,before);
   await longList.evaluate(n=>n.scrollTop=0);
   const scrollbar=await longList.evaluate(node=>{const r=node.getBoundingClientRect(),style=getComputedStyle(node),width=node.offsetWidth-node.clientWidth;return {width,x:r.right-width/2,y:r.top+Math.max(20,node.clientHeight*node.clientHeight/node.scrollHeight)/2,thumb:getComputedStyle(node,'::-webkit-scrollbar-thumb').backgroundColor,scrollbarWidth:getComputedStyle(node,'::-webkit-scrollbar').width};});
   assert.equal(scrollbar.width,8);assert.equal(scrollbar.scrollbarWidth,'8px');assert(['rgb(58, 58, 74)','rgb(74, 74, 90)'].includes(scrollbar.thumb));
   await startup.mouse.move(scrollbar.x,scrollbar.y);await startup.mouse.down();await startup.mouse.move(scrollbar.x,scrollbar.y+80,{steps:8});await startup.mouse.up();
   await startup.waitForFunction(()=>document.querySelector('.chapter-list').scrollTop>0);
   const last=longDialog.getByRole('button',{name:'Chapter 141',exact:true});await longDialog.focus();await last.focus();
   const visible=await last.evaluate(node=>{const b=node.getBoundingClientRect(),r=node.closest('.chapter-list').getBoundingClientRect();return b.top>=r.top-1&&b.bottom<=r.bottom+1;});assert(visible);
   await last.press('Shift+F10');await startup.getByRole('menu',{name:'Location for Chapter 141'}).waitFor();
   await startup.keyboard.press('Escape');assert(await last.evaluate(n=>n===document.activeElement));
   await startup.screenshot({path:path.join(config.output,`manga-scroll-${viewport.width}-${viewport.height}.png`)});
   await longList.evaluate(n=>n.scrollTop=0);
  }
  await startup.setViewportSize({width:1440,height:1000});
  await longDialog.getByRole('button',{name:'Chapter 141',exact:true}).click();
  await startup.waitForFunction(()=>document.querySelector('.paged-page img')?.naturalWidth>0);
  await startup.getByRole('button',{name:'Back to chapters'}).click();await startup.getByRole('dialog',{name:'Manga reader'}).waitFor({state:'detached'});
  await startup.waitForFunction(()=>document.querySelector('.chapter-list').getAttribute('aria-busy')==='false');
  assert.equal(await longDialog.getByRole('progressbar',{name:'Chapter 141 reading position'}).getAttribute('aria-valuetext'),'Page 1 of 1');
  const completedBar=await longDialog.getByRole('progressbar',{name:'Chapter 141 reading position'}).evaluate(node=>{const row=node.closest('.chapter-row').getBoundingClientRect(),bar=node.getBoundingClientRect(),fill=node.querySelector('.chapter-progress-fill').getBoundingClientRect();return {rowWidth:row.width,barWidth:bar.width,fillWidth:fill.width,bottom:row.bottom-bar.bottom};});
  assert.equal(completedBar.fillWidth,completedBar.rowWidth);assert.equal(completedBar.barWidth,completedBar.rowWidth);assert.equal(completedBar.bottom,0);
  await startup.screenshot({path:path.join(config.output,'manga-chapter-progress-complete.png')});
  assert.equal(await longDialog.getByRole('progressbar',{name:'Chapter 1 reading position'}).getAttribute('aria-valuetext'),'Not started');
  const unreadWidth=await longDialog.getByRole('progressbar',{name:'Chapter 1 reading position'}).locator('.chapter-progress-fill').evaluate(node=>node.getBoundingClientRect().width);assert.equal(unreadWidth,0);
  assert(await longDialog.getByRole('button',{name:'Chapter 141',exact:true}).evaluate(n=>n===document.activeElement));
  check('three and 134 chapters use the same fixed window; standard Keivotos scrollbar dragging, wheel and keyboard scroll only the chapter list at desktop/narrow/short sizes, with context focus and progress past 120 chapters');
  const failedSummary=route=>route.fulfill({status:503,json:{detail:'Generated progress lookup failure'}});
  await startup.route('**/api/manga/progress/series?*',failedSummary);
  await startup.evaluate(()=>window.dispatchEvent(new Event('focus')));
  await longDialog.getByRole('status').filter({hasText:'Saved reading positions are unavailable'}).waitFor();
  assert.equal(await longDialog.getByRole('progressbar',{name:'Chapter 1 reading position'}).getAttribute('aria-valuetext'),'Reading position unavailable');
  await startup.unroute('**/api/manga/progress/series?*',failedSummary);
  await startup.evaluate(()=>window.dispatchEvent(new Event('focus')));
  await startup.waitForFunction(()=>document.querySelector('.chapter-list').getAttribute('aria-busy')==='false'&&document.querySelector('[aria-label="Chapter 141 reading position"]').getAttribute('aria-valuetext')==='Page 1 of 1');
  const savedLast=await startup.evaluate(async source_id=>{const r=await fetch('/api/manga/progress/series?'+new URLSearchParams({source_id,series_path:'Blue Hour',offset:120}));return (await r.json()).items;},sourceId);
  const failedRead=route=>route.fulfill({status:503,json:{detail:'Generated saved-position lookup failure'}}),positionMutations=[];
  const trackMutation=request=>{if(request.method()==='POST'&&(/\/api\/manga\/progress(?:\/session)?$/.test(new URL(request.url()).pathname)))positionMutations.push(request.url());};
  startup.on('request',trackMutation);await startup.route('**/api/manga/progress/read',failedRead);
  await longDialog.getByRole('button',{name:'Chapter 141',exact:true}).click();
  await startup.waitForFunction(()=>document.querySelector('.paged-page img')?.naturalWidth>0);
  await startup.getByRole('status').filter({hasText:'Saved reading position is unavailable'}).waitFor();
  await startup.waitForTimeout(550);
  await startup.getByRole('button',{name:'Back to chapters'}).click();await startup.getByRole('dialog',{name:'Manga reader'}).waitFor({state:'detached'});
  await startup.unroute('**/api/manga/progress/read',failedRead);startup.off('request',trackMutation);
  assert.deepEqual(positionMutations,[]);
  const afterLast=await startup.evaluate(async source_id=>{const r=await fetch('/api/manga/progress/series?'+new URLSearchParams({source_id,series_path:'Blue Hour',offset:120}));return (await r.json()).items;},sourceId);assert.deepEqual(afterLast,savedLast);
  check('summary failure shows unavailable progress and recovers on focus; a failed saved-position read opens without a chooser and never creates a session or overwrites saved progress');
  const starting=startup.waitForRequest(r=>r.url().endsWith('/api/manga/progress/session'));
  await startup.route('**/api/manga/progress/session',async route=>{const response=await route.fetch();await new Promise(resolve=>setTimeout(resolve,350));await route.fulfill({response});});
  await startup.getByRole('dialog',{name:'Blue Hour chapters'}).getByRole('button',{name:'Chapter 1',exact:true}).click();await starting;
  assert.equal(await startup.getByRole('alertdialog',{name:'Resume manga'}).count(),0);
  assert.match(await startup.getByRole('dialog',{name:'Manga reader'}).textContent(),/Loading/);
  await startup.waitForFunction(()=>document.querySelector('.paged-page img')?.naturalWidth>0);
  await startup.getByRole('button',{name:'Back to chapters'}).click();await startup.getByRole('dialog',{name:'Manga reader'}).waitFor({state:'detached'});
  await startupContext.close();check('a fresh chapter stays in Loading while its session starts and never shows a false changed-chapter prompt');
  assert.equal((await api('/api/suite/modules/manga/enable','POST')).status,200);
  await page.reload();await page.getByRole('button',{name:'Open Keivotos menu',exact:true}).click();
  await page.locator('.app-drawer').getByRole('button',{name:'Manga',exact:true}).click();
  await page.getByRole('button',{name:'Blue Hour',exact:true}).waitFor();
  const categoryNav=page.getByRole('navigation',{name:'Manga categories',exact:true});
  assert.equal(await categoryNav.count(),0);
  const reading=(await api('/api/manga/categories','POST',{name:'Reading',request_id:crypto.randomUUID()})).data;
  await page.reload();await categoryNav.waitFor();
  assert.deepEqual(await categoryNav.getByRole('button').allTextContents(),['Default','Reading']);
  assert.equal(await categoryNav.getByRole('button',{name:'Default',exact:true}).getAttribute('aria-pressed'),'true');
  const favorites=(await api('/api/manga/categories','POST',{name:'Favorites',request_id:crypto.randomUUID()})).data;
  await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await categoryNav.getByRole('button',{name:'Favorites',exact:true}).waitFor();
  assert.deepEqual(await categoryNav.getByRole('button').allTextContents(),['Default','Reading','Favorites']);
  assert.equal(await categoryNav.getByRole('button',{name:'Default',exact:true}).getAttribute('aria-pressed'),'true');
  const designStyle=await categoryNav.evaluate(nav=>{
   const button=nav.querySelector('button'),s=getComputedStyle(button),u=getComputedStyle(button,'::after');
   return {underline:nav.classList.contains('manga-underline'),gap:getComputedStyle(nav).gap,padding:getComputedStyle(nav).padding,font:s.fontSize,buttonPadding:s.padding,background:s.backgroundColor,lineHeight:u.height,lineColor:u.backgroundColor,color:s.color,scale:new DOMMatrix(u.transform).a};
  });
  assert.deepEqual(designStyle,{underline:true,gap:'24px',padding:'10px 20px 8px',font:'14px',buttonPadding:'7px 0px 10px',background:'rgba(0, 0, 0, 0)',lineHeight:'2px',lineColor:designStyle.color,color:designStyle.color,scale:1});
  const designReading=categoryNav.getByRole('button',{name:'Reading',exact:true});
  await page.mouse.move(1,1);await page.waitForTimeout(160);
  const designRest=await designReading.evaluate(node=>getComputedStyle(node).color);
  await designReading.hover();assert.notEqual(await designReading.evaluate(node=>getComputedStyle(node).color),designRest);
  await categoryNav.getByRole('button',{name:'Default',exact:true}).focus();await page.keyboard.press('Tab');assert(await designReading.evaluate(node=>node===document.activeElement));assert.equal(await designReading.evaluate(node=>getComputedStyle(node).outlineStyle),'solid');
  await page.mouse.move(1,1);
  const designMotion=await designReading.evaluate(async node=>{
   const scale=()=>new DOMMatrix(getComputedStyle(node,'::after').transform).a;
   const before=scale();node.click();await new Promise(resolve=>setTimeout(resolve,55));const middle=scale();await new Promise(resolve=>setTimeout(resolve,180));return {before,middle,after:scale()};
  });
  assert.equal(designMotion.before,0);assert(designMotion.middle>0&&designMotion.middle<1,JSON.stringify(designMotion));assert.equal(designMotion.after,1);
  await categoryNav.getByRole('button',{name:'Default',exact:true}).click();await page.waitForTimeout(160);
  await shot('manga-category-underline-desktop');
  check('The approved Manga-only underline has compact transparent labels, a 2px accent line, clear hover/focus and a measured 130ms selection transition');
  const book=(await api('/api/manga/library')).data.items.find(item=>item.name==='Blue Hour');
  const identity={source_id:book.source_id,path:book.path};
  const membershipUrl='/api/manga/category-membership';
  async function member(){return (await api(membershipUrl+'?'+new URLSearchParams(identity))).data;}
  const picker=page.getByRole('dialog',{name:'Add to categories',exact:true});
  async function openPicker(){
   await page.getByRole('button',{name:'Blue Hour',exact:true}).click({button:'right'});
   await page.getByRole('menu',{name:'Location for Blue Hour',exact:true}).getByRole('menuitem',{name:'Add to categories',exact:true}).click();
   await picker.getByRole('checkbox',{name:'Reading',exact:true}).waitFor();
  }
  await openPicker();await picker.getByRole('checkbox',{name:'Reading',exact:true}).check();await picker.getByRole('checkbox',{name:'Favorites',exact:true}).check();
  await picker.getByRole('button',{name:'Save',exact:true}).click();await picker.waitFor({state:'hidden'});
  await page.getByRole('button',{name:'Blue Hour',exact:true}).waitFor({state:'detached'});
  await categoryNav.getByRole('button',{name:'Reading',exact:true}).click();await page.getByRole('button',{name:'Blue Hour',exact:true}).waitFor();
  assert.equal(await page.locator('.manga-tile').count(),1);
  const search=page.getByRole('textbox',{name:'Search manga',exact:true});
  await search.fill('Quiet');await page.getByText('No manga found.',{exact:true}).waitFor();await search.fill('');await page.getByRole('button',{name:'Blue Hour',exact:true}).waitFor();
  check('Manga hides an empty strip, then shows Default/custom names and filters search by exact series membership');

  await page.getByRole('button',{name:'Blue Hour',exact:true}).click();
  await page.getByRole('dialog',{name:'Blue Hour chapters',exact:true}).getByRole('button',{name:'Local',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('input[aria-label="Search manga"]')?.value==='Local'&&document.querySelectorAll('.manga-tile').length===1);
  assert.equal(await categoryNav.getByRole('button',{name:'Reading',exact:true}).getAttribute('aria-pressed'),'true');
  assert.equal((await page.locator('.manga-tile').textContent()).trim(),'Blue Hour');await search.fill('');await page.getByRole('button',{name:'Blue Hour',exact:true}).waitFor();
  check('Clicking a Manga tag uses existing search within the selected category, excluding matching tags from other categories');

  await page.getByRole('button',{name:'Blue Hour',exact:true}).click();
  const chapters=page.getByRole('dialog',{name:'Blue Hour chapters',exact:true});
  await chapters.getByRole('button',{name:'Chapter 1',exact:true}).click({button:'right'});
  const chapterMenu=page.getByRole('menu',{name:'Location for Chapter 1',exact:true});await chapterMenu.waitFor();
  assert.equal(await chapterMenu.getByRole('menuitem',{name:'Add to categories',exact:true}).count(),0);await page.keyboard.press('Escape');
  await chapters.getByRole('button',{name:'Chapter 1',exact:true}).click();await readerReady();
  const current=await member();assert.equal((await api(membershipUrl,'POST',{...identity,category_ids:[],version:current.version,request_id:crypto.randomUUID()})).status,200);
  await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await page.waitForTimeout(350);
  await page.getByRole('dialog',{name:'Manga reader',exact:true}).waitFor();
  assert(new URL(await page.locator('.reader img').first().getAttribute('src'),config.url).searchParams.get('path').startsWith('Blue Hour/'));
  await page.getByRole('button',{name:'Back to chapters',exact:true}).click();await page.getByRole('button',{name:'Back to manga',exact:true}).click();
  await page.getByText('No manga in this category.',{exact:true}).waitFor();
  await categoryNav.getByRole('button',{name:'Default',exact:true}).click();await page.getByRole('button',{name:'Blue Hour',exact:true}).waitFor();
  check('Categories belong to series, not chapter menus; changing membership externally does not close an active reader');

  await openPicker();await picker.getByRole('checkbox',{name:'Reading',exact:true}).check();
  const before=await member();assert.equal((await api(membershipUrl,'POST',{...identity,category_ids:[favorites.id],version:before.version,request_id:crypto.randomUUID()})).status,200);
  await picker.getByRole('button',{name:'Save',exact:true}).click();
  await picker.getByRole('alert').filter({hasText:'membership changed'}).waitFor();assert(await picker.getByRole('checkbox',{name:'Reading',exact:true}).isChecked());
  await picker.getByRole('button',{name:'Reload saved selection',exact:true}).click();
  await picker.getByRole('checkbox',{name:'Favorites',exact:true}).waitFor();assert(await picker.getByRole('checkbox',{name:'Favorites',exact:true}).isChecked());
  assert.equal(await picker.getByRole('checkbox',{name:'Reading',exact:true}).isChecked(),false);
  await picker.getByRole('checkbox',{name:'Reading',exact:true}).check();await picker.getByRole('button',{name:'Save',exact:true}).click();await picker.waitFor({state:'hidden'});
  check('A stale membership picker preserves its draft and refuses to overwrite newer choices until explicitly reloaded');

  await categoryNav.getByRole('button',{name:'Favorites',exact:true}).click();await page.getByRole('button',{name:'Blue Hour',exact:true}).waitFor();
  let mode='lost',releasePending,receipts=[];
  await page.route('**/api/manga/category-membership',async route=>{
   if(route.request().method()!=='POST')return route.fallback();
   receipts.push(route.request().postDataJSON());
   if(mode==='pending')await new Promise(resolve=>releasePending=resolve);
   const response=await route.fetch();
   if(mode==='lost'){mode='normal';return route.fulfill({status:503,json:{detail:'Fixture lost response. Retry Save.'}});}
   return route.fulfill({response});
  });
  await openPicker();await picker.getByRole('checkbox',{name:'Favorites',exact:true}).uncheck();
  await picker.getByRole('button',{name:'Save',exact:true}).click();await picker.getByRole('alert').filter({hasText:'Fixture lost response'}).waitFor();
  await picker.getByRole('button',{name:'Save',exact:true}).click();await picker.waitFor({state:'hidden'});
  assert.equal(receipts[0].request_id,receipts[1].request_id);assert.deepEqual((await member()).category_ids,[reading.id]);
  await page.getByText('No manga in this category.',{exact:true}).waitFor();
  await categoryNav.getByRole('button',{name:'Default',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.manga-grid')?.getAttribute('aria-busy')==='false');
  assert.equal(await page.getByRole('button',{name:'Blue Hour',exact:true}).count(),0);
  await categoryNav.getByRole('button',{name:'Reading',exact:true}).click();await page.getByRole('button',{name:'Blue Hour',exact:true}).waitFor();
  await openPicker();await picker.getByRole('checkbox',{name:'Reading',exact:true}).uncheck();await picker.getByRole('button',{name:'Save',exact:true}).click();await picker.waitFor({state:'hidden'});
  await categoryNav.getByRole('button',{name:'Default',exact:true}).click();await page.getByRole('button',{name:'Blue Hour',exact:true}).waitFor();
  check('An exact retry after a committed save loses its reply does not duplicate changes; only clearing the final membership returns a series to Default');

  await openPicker();await page.setViewportSize({width:360,height:480});
  const pickerBounds=await picker.boundingBox();assert(pickerBounds.x>=0&&pickerBounds.y>=0&&pickerBounds.x+pickerBounds.width<=360&&pickerBounds.y+pickerBounds.height<=480);
  await page.keyboard.press('Shift+Tab');assert(await picker.getByRole('button',{name:'Save',exact:true}).evaluate(node=>node===document.activeElement));
  await page.keyboard.press('Tab');assert(await picker.getByRole('checkbox',{name:'Reading',exact:true}).evaluate(node=>node===document.activeElement));
  await picker.getByRole('checkbox',{name:'Reading',exact:true}).check();mode='pending';
  await picker.getByRole('button',{name:'Save',exact:true}).click();await picker.getByRole('button',{name:'Saving…',exact:true}).waitFor();
  assert(await picker.getByRole('button',{name:'Cancel',exact:true}).isDisabled());await page.keyboard.press('Escape');await page.keyboard.press('Tab');
  assert(await picker.evaluate(node=>node.contains(document.activeElement)));
  while(!releasePending)await page.waitForTimeout(20);releasePending();await picker.waitFor({state:'hidden'});mode='normal';
  await page.setViewportSize({width:1440,height:1000});await categoryNav.getByRole('button',{name:'Reading',exact:true}).click();await page.getByRole('button',{name:'Blue Hour',exact:true}).waitFor();
  await page.reload();await categoryNav.getByRole('button',{name:'Reading',exact:true}).waitFor();await page.getByRole('button',{name:'Blue Hour',exact:true}).waitFor();
  assert.equal(await categoryNav.getByRole('button',{name:'Reading',exact:true}).getAttribute('aria-pressed'),'true');
  report.categories={owner:'manga',category_id:reading.id,total:1,item:identity,category_ids:[reading.id]};await shot('manga-categories');
  check('The narrow membership dialog traps focus and cannot dismiss a pending save; saved selection and last category survive reload');
  const designLong='한국어 공부와 함께 읽을 아주 긴 카테고리 이름';
  const designExtra=(await api('/api/manga/categories','POST',{name:designLong,request_id:crypto.randomUUID()})).data;
  assert(designExtra.id);await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await categoryNav.getByRole('button',{name:designLong,exact:true}).waitFor();
  for(const viewport of [{width:360,height:480},{width:320,height:480}]){
   await page.setViewportSize(viewport);await categoryNav.getByRole('button',{name:'Default',exact:true}).focus();await page.keyboard.press('End');
   assert.equal(await categoryNav.getByRole('button',{name:designLong,exact:true}).getAttribute('aria-pressed'),'true');
   assert(await categoryNav.evaluate(nav=>nav.scrollWidth>nav.clientWidth&&nav.scrollLeft>0));
   assert(await categoryNav.getByRole('button',{name:designLong,exact:true}).evaluate(node=>node===document.activeElement&&getComputedStyle(node).whiteSpace==='nowrap'));
   await page.keyboard.press('Home');assert.equal(await categoryNav.getByRole('button',{name:'Default',exact:true}).getAttribute('aria-pressed'),'true');
   await page.keyboard.press('ArrowLeft');assert.equal(await categoryNav.getByRole('button',{name:designLong,exact:true}).getAttribute('aria-pressed'),'true');
   await page.keyboard.press('ArrowRight');await page.keyboard.press('ArrowRight');
   assert.equal(await designReading.getAttribute('aria-pressed'),'true');await page.getByRole('button',{name:'Blue Hour',exact:true}).waitFor();
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   await shot('manga-category-underline-'+viewport.width);
  }
  await page.emulateMedia({reducedMotion:'reduce'});
  assert.equal(await designReading.evaluate(node=>getComputedStyle(node,'::after').transitionDuration),'0s');
  await categoryNav.getByRole('button',{name:'Favorites',exact:true}).click();await designReading.click();
  assert.equal(await designReading.evaluate(node=>new DOMMatrix(getComputedStyle(node,'::after').transform).a),1);
  await page.emulateMedia({reducedMotion:'no-preference'});
  await page.evaluate(()=>document.documentElement.dataset.motion='reduced');
  assert(await designReading.evaluate(node=>parseFloat(getComputedStyle(node,'::after').transitionDuration)<.001));
  await page.evaluate(()=>delete document.documentElement.dataset.motion);
  const designHide=async route=>{
   const response=await route.fetch(),data=await response.json();const items=data.items.filter(item=>item.id!==reading.id);await route.fulfill({response,json:{...data,items,total:items.length}});
  };
  await page.route('**/api/manga/categories?*',designHide);await page.reload();await categoryNav.getByRole('button',{name:'Default',exact:true}).waitFor();
  assert.equal(await categoryNav.getByRole('button',{name:'Default',exact:true}).getAttribute('aria-pressed'),'true');
  await page.unroute('**/api/manga/categories?*',designHide);
  await page.evaluate(()=>localStorage.setItem('keivotos:manga-category',JSON.stringify('missing-category')));await page.reload();await designReading.waitFor();
  assert.equal(await categoryNav.getByRole('button',{name:'Default',exact:true}).getAttribute('aria-pressed'),'true');
  await designReading.click();await page.reload();await page.getByRole('button',{name:'Blue Hour',exact:true}).waitFor();assert.equal(await designReading.getAttribute('aria-pressed'),'true');
  await categoryNav.evaluate(nav=>{for(const name of ['Favorites','Default','Reading'])[...nav.querySelectorAll('button')].find(button=>button.textContent===name).click();});
  await page.waitForTimeout(350);await page.getByRole('button',{name:'Blue Hour',exact:true}).waitFor();assert.equal(await designReading.getAttribute('aria-pressed'),'true');
  await page.setViewportSize({width:1440,height:1000});
  check('Manga long Unicode labels scroll at 320/360px; arrows/Home/End, rapid changes, reduced motion and invalid/deleted saved-category fallback retain the final scoped selection and reload behavior');
  const acrossContext=await browser.newContext({viewport:{width:1440,height:1000},hasTouch:true}),across=await acrossContext.newPage();watchErrors(across);across.setDefaultTimeout(15000);
  let releaseNext,releaseSession;
  const ar=()=>across.getByRole('dialog',{name:'Manga reader',exact:true});
  async function enterSeries(name){await across.getByRole('button',{name,exact:true}).click();}
  async function enterChapter(name){await across.getByRole('dialog').filter({has:across.locator('.chapter-list')}).getByRole('button',{name,exact:true}).click();await readerReady(across);}
  async function pagedReady(name,index){await across.waitForFunction(({name,index})=>document.querySelector('.reader-chapter')?.textContent===name&&document.querySelector('.paged-page')?.dataset.page===String(index)&&document.querySelector('.paged-page img')?.naturalWidth>0,{name,index});}
  async function saved(series,path,kind='archive'){return across.evaluate(async identity=>{
   const params=new URLSearchParams({...identity,metadata:'true'}),metadata=await(await fetch('/api/manga/pages?'+params)).json();
   return(await fetch('/api/manga/progress/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...identity,revision:metadata.revision})})).json();
  },{source_id:sourceId,series_path:series,path,kind});}
  async function centerChapter(name,index=0){
   const target=ar().locator('.chapter-content').filter({has:across.getByRole('heading',{name,exact:true})}).locator(`.page[data-page="${index}"]`);
   await target.waitFor();await target.evaluate(node=>{const r=node.closest('.reader'),b=r.getBoundingClientRect(),p=node.getBoundingClientRect();r.scrollTop+=p.top+p.height/2-(b.top+b.height/2);});
   await across.waitForFunction(({name,index})=>{
    const reader=document.querySelector('.reader'),node=[...reader.querySelectorAll('.chapter-content')].find(n=>n.querySelector('h3').textContent===name)?.querySelector(`.page[data-page="${index}"]`);
    return node?.querySelector('img')?.naturalWidth>0&&reader.querySelector('.reader-chapter').textContent===name&&reader.querySelector('.page-counter').textContent.startsWith((index+1)+' /');
   },{name,index});
  }
  try{
   await across.goto(config.url);await across.getByRole('button',{name:'Open Keivotos menu'}).click();await across.locator('.app-drawer').getByRole('button',{name:'Manga',exact:true}).click();
   await across.getByRole('navigation',{name:'Manga categories'}).getByRole('button',{name:'Default',exact:true}).waitFor();
   assert(await across.evaluate(()=>matchMedia('(pointer:coarse)').matches));
   assert(await across.getByRole('navigation',{name:'Manga categories'}).getByRole('button').first().evaluate(node=>node.getBoundingClientRect().height>=44));
   check('Touch category targets stay at least 44px while retaining the same compact names and underline');
   await enterSeries('After The Rain');await enterChapter('Chapter 2');await across.getByLabel('Reading layout').selectOption('paged');
   await pagedReady('Chapter 2',0);await across.mouse.move(100,500);
   await across.mouse.wheel(0,120);await pagedReady('Chapter 2',1);
   await across.mouse.wheel(0,-120);await pagedReady('Chapter 2',0);
   await across.mouse.wheel(0,60);
   for(let i=0;i<12;i++){await across.waitForTimeout(30);await across.mouse.wheel(0,8);}
   await pagedReady('Chapter 2',1);
   await across.mouse.wheel(0,-60);await pagedReady('Chapter 2',0);
   await across.waitForTimeout(220);
   for(let i=0;i<4;i++){await across.mouse.wheel(0,2);await across.waitForTimeout(20);}
   assert.equal(await ar().locator('.paged-page').getAttribute('data-page'),'0');
   for(let i=0;i<6;i++){await across.mouse.wheel(0,8);await across.waitForTimeout(25);}
   await pagedReady('Chapter 2',1);
   await across.mouse.wheel(0,-120);await pagedReady('Chapter 2',0);
   check('Native Paged wheel-down/up turns next/previous; direction reversal is immediate and a long precise touchpad burst produces one turn after its noise threshold');
   const ignored=await ar().evaluate(reader=>{
    const result=[];
    for(const options of [{deltaX:120,deltaY:0},{deltaY:120,ctrlKey:true},{deltaY:120,metaKey:true},{deltaY:120,altKey:true},{deltaY:120,shiftKey:true}]){
     const event=new WheelEvent('wheel',{...options,bubbles:true,cancelable:true});reader.dispatchEvent(event);result.push(event.defaultPrevented);
    }
    const event=new WheelEvent('wheel',{deltaY:120,bubbles:true,cancelable:true});reader.querySelector('select').dispatchEvent(event);result.push(event.defaultPrevented);return result;
   });assert.deepEqual(ignored,[false,false,false,false,false,false]);await pagedReady('Chapter 2',0);
   await across.getByRole('button',{name:'Back to chapters'}).hover();await across.mouse.wheel(0,120);await pagedReady('Chapter 2',0);
   await ar().evaluate(reader=>reader.dispatchEvent(new WheelEvent('wheel',{deltaY:1,deltaMode:1,bubbles:true,cancelable:true})));await pagedReady('Chapter 2',1);
   await ar().evaluate(reader=>reader.dispatchEvent(new WheelEvent('wheel',{deltaY:-1,deltaMode:2,bubbles:true,cancelable:true})));await pagedReady('Chapter 2',0);
   check('Paged wheel handling leaves horizontal/modified/control input native and supports line/page wheel units without multiplying turns');
   await ar().focus();await across.keyboard.press('ArrowRight');await across.keyboard.press('ArrowRight');await pagedReady('Chapter 2',2);
   await across.getByRole('button',{name:'Back to chapters'}).click();await ar().waitFor({state:'detached'});
   assert.equal((await saved('After The Rain','After The Rain/Chapter 2.zip')).position.page,2);
   await enterChapter('Chapter 1');await ar().focus();for(let i=0;i<3;i++)await across.keyboard.press('ArrowRight');await pagedReady('Chapter 1',3);
   await across.keyboard.down('ArrowRight');await ar().getByRole('group',{name:'Chapter boundary',exact:true}).waitFor();
   for(let i=0;i<8;i++)await across.keyboard.down('ArrowRight');await across.keyboard.up('ArrowRight');
   assert.equal(await ar().locator('.reader-chapter').textContent(),'Chapter 1');
   await across.waitForTimeout(220);await across.mouse.move(100,500);await across.mouse.wheel(0,120);
   for(let i=0;i<12;i++){await across.waitForTimeout(30);await across.mouse.wheel(0,12);}await pagedReady('Chapter 2',0);
   await across.waitForTimeout(500);assert.equal((await saved('After The Rain','After The Rain/Chapter 1.cbz')).position.page,3);
   assert.equal((await saved('After The Rain','After The Rain/Chapter 2.zip')).position.page,0);
   await across.mouse.wheel(0,-120);await ar().getByRole('button',{name:'Previous chapter: Chapter 1',exact:true}).waitFor();
   for(let i=0;i<8;i++){await across.waitForTimeout(30);await across.mouse.wheel(0,-12);}assert.equal(await ar().locator('.reader-chapter').textContent(),'Chapter 2');
   await across.waitForTimeout(220);await across.mouse.wheel(0,-120);
   for(let i=0;i<8;i++){await across.waitForTimeout(30);await across.mouse.wheel(0,-12);}await pagedReady('Chapter 1',3);
   check('Paged boundaries cross natural CBZ/ZIP chapter order, start next at page one despite saved progress, reverse to the preceding last page, and repeated keys cannot skip chapters');
   check('Separate wheel gestures enter chapter boundaries and cross once; momentum stays latched across asynchronous chapter loads in both directions');
   let nextMode='fail';
   const nextRoute=async route=>{if(new URL(route.request().url()).searchParams.get('path')!=='After The Rain/Chapter 2.zip'){await route.continue();return;}
    if(nextMode==='fail'){await route.fulfill({status:422,json:{detail:'Generated corrupt chapter'}});return;}
    await new Promise(resolve=>{releaseNext=resolve;});await route.continue();};
   await across.route('**/api/manga/pages?*',nextRoute);await ar().focus();await across.keyboard.press('ArrowRight');await ar().getByRole('button',{name:'Next chapter: Chapter 2',exact:true}).click();
   await ar().getByRole('alert').filter({hasText:'Generated corrupt chapter'}).waitFor();await pagedReady('Chapter 1',3);
   nextMode='hold';await ar().getByRole('button',{name:'Retry chapter',exact:true}).click();while(!releaseNext)await across.waitForTimeout(10);
   await ar().getByRole('button',{name:'Return to page',exact:true}).click();releaseNext();await across.waitForTimeout(200);await pagedReady('Chapter 1',3);
   releaseNext=undefined;await ar().focus();await across.keyboard.press('ArrowRight');await ar().getByRole('button',{name:'Next chapter: Chapter 2',exact:true}).click();while(!releaseNext)await across.waitForTimeout(10);
   await across.keyboard.press('ArrowLeft');releaseNext();await across.waitForTimeout(200);await pagedReady('Chapter 1',3);
   releaseNext=undefined;await across.keyboard.press('ArrowRight');await ar().getByRole('button',{name:'Next chapter: Chapter 2',exact:true}).click();while(!releaseNext)await across.waitForTimeout(10);
   await pagedReady('Chapter 1',3);await across.keyboard.press('Escape');await ar().waitFor({state:'detached'});releaseNext();
   await enterChapter('Chapter 10');await pagedReady('Chapter 10',0);await across.waitForTimeout(250);await pagedReady('Chapter 10',0);await across.unroute('**/api/manga/pages?*',nextRoute);
   await ar().focus();for(let i=0;i<4;i++)await across.keyboard.press('ArrowRight');
   await ar().getByRole('group',{name:'Chapter boundary',exact:true}).getByText('End of series',{exact:true}).waitFor();
   assert.equal(await ar().getByRole('button',{name:/Next chapter:/}).count(),0);await ar().getByRole('button',{name:'Return to page'}).click();
   await across.mouse.move(100,500);await across.waitForTimeout(220);await across.mouse.wheel(0,120);await ar().getByText('End of series',{exact:true}).waitFor();
   await across.waitForTimeout(220);await across.mouse.wheel(0,120);assert.equal(await ar().locator('.reader-chapter').textContent(),'Chapter 10');await ar().getByRole('button',{name:'Return to page'}).click();
   await across.getByRole('button',{name:'Back to chapters'}).click();await enterChapter('Chapter 1');await pagedReady('Chapter 1',3);
   await ar().focus();for(let i=0;i<4;i++)await across.keyboard.press('ArrowLeft');await ar().getByText('First chapter',{exact:true}).waitFor();
   await ar().getByRole('button',{name:'Return to page'}).click();
   check('A failing/slow next chapter preserves the current page with Retry; Return to page, reversal and Escape cancel pending jumps, late replies stay discarded, and series boundaries never wrap');
   await across.getByLabel('Reading layout').selectOption('vertical');await centerChapter('Chapter 1',3);
   await ar().locator('.chapter-content[data-chapter="After The Rain/Chapter 2.zip"]').waitFor();
   assert.equal((await saved('After The Rain','After The Rain/Chapter 2.zip')).position.page,0,'appending pages must not change progress');
   await across.mouse.move(700,600);await across.mouse.wheel(0,1700);await centerChapter('Chapter 2',0);
   await across.waitForTimeout(500);assert.equal((await saved('After The Rain','After The Rain/Chapter 1.cbz')).position.page,3);
   assert.equal((await saved('After The Rain','After The Rain/Chapter 2.zip')).position.mode,'vertical');
   await across.setViewportSize({width:390,height:760});await centerChapter('Chapter 2',1);
   await across.getByLabel('Reading layout').selectOption('paged');await pagedReady('Chapter 2',1);
   await across.mouse.move(100,500);await across.waitForTimeout(220);await across.mouse.wheel(0,120);await pagedReady('Chapter 2',2);
   await across.getByLabel('Reading layout').selectOption('vertical');await centerChapter('Chapter 2',2);
   await across.mouse.move(100,500);await across.mouse.wheel(0,12);
   await across.getByLabel('Reading layout').selectOption('paged');const switchedPage=await ar().locator('.paged-page').getAttribute('data-page');
   await across.mouse.move(100,500);await across.mouse.wheel(0,12);assert.equal(await ar().locator('.paged-page').getAttribute('data-page'),switchedPage);
   await ar().focus();await across.keyboard.press('ArrowLeft');await pagedReady('Chapter 2',1);
   await across.getByLabel('Reading layout').selectOption('vertical');await centerChapter('Chapter 2',1);
   await across.screenshot({path:path.join(config.output,'manga-continuation-narrow.png')});
   await across.getByRole('button',{name:'Back to chapters'}).click();await ar().waitFor({state:'detached'});
   await across.waitForFunction(()=>document.querySelector('.chapter-list').getAttribute('aria-busy')==='false');
   const acrossDialog=across.getByRole('dialog',{name:'After The Rain chapters',exact:true});
   assert.equal(await acrossDialog.getByRole('progressbar',{name:'Chapter 1 reading position'}).getAttribute('aria-valuetext'),'Page 4 of 4');
   assert.equal(await acrossDialog.getByRole('progressbar',{name:'Chapter 2 reading position'}).getAttribute('aria-valuetext'),'Page 2 of 4');
   assert(await acrossDialog.getByRole('button',{name:'Chapter 1',exact:true}).evaluate(node=>node===document.activeElement));
   await enterChapter('Chapter 2');await centerChapter('Chapter 2',1);
   check('Vertical wheel continuation keeps per-chapter visible-page progress, updates title/counter, retains layout/resize position, reconciles bars/focus, and directly resumes the entered chapter');
   const touch=await acrossContext.newCDPSession(across),scrollBeforeTouch=await ar().evaluate(n=>n.scrollTop);
   await touch.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:200,y:600}]});
   for(let y=560;y>=240;y-=40){await touch.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:200,y}]});await across.waitForTimeout(20);}
   await touch.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
   await across.waitForFunction(before=>document.querySelector('.reader').scrollTop>before,scrollBeforeTouch);
   await across.waitForTimeout(300);await centerChapter('Chapter 2',1);
   const keyboardScroll=await ar().evaluate(n=>n.scrollTop);await ar().focus();await across.keyboard.press('PageDown');
   await across.waitForFunction(before=>document.querySelector('.reader').scrollTop>before,keyboardScroll);await centerChapter('Chapter 2',1);
   check('Native touch swipes and vertical PageDown scroll the continuous reader without being treated as page clicks');
   await across.getByRole('button',{name:'Back to chapters'}).click();await across.getByRole('button',{name:'Back to manga'}).click();
   let failAppend=true;
   const appendRoute=async route=>{if(new URL(route.request().url()).searchParams.get('path')==='Blue Hour/Chapter 12'&&failAppend)await route.fulfill({status:422,json:{detail:'Generated next chapter failure'}});else await route.continue();};
   await across.route('**/api/manga/pages?*',appendRoute);
   await across.getByRole('navigation',{name:'Manga categories'}).getByRole('button',{name:'Reading',exact:true}).click();await enterSeries('Blue Hour');await enterChapter('Chapter 11');
   if(await across.getByLabel('Reading layout').inputValue()!=='vertical')await across.getByLabel('Reading layout').selectOption('vertical');
   await centerChapter('Chapter 11',0);await ar().getByRole('alert').filter({hasText:'Generated next chapter failure'}).waitFor();
   assert.equal(await ar().locator('.reader-chapter').textContent(),'Chapter 11');assert(await ar().locator('.page img').count()>0);
   failAppend=false;await ar().getByRole('button',{name:'Retry chapter',exact:true}).click();await ar().locator('[data-chapter="Blue Hour/Chapter 12"]').waitFor();await across.unroute('**/api/manga/pages?*',appendRoute);
   assert.equal((await saved('Blue Hour','Blue Hour/Chapter 12','folder')).position,null);
   const heldSession=async route=>{if(route.request().postDataJSON().path==='Blue Hour/Chapter 12'){const response=await route.fetch();await new Promise(resolve=>{releaseSession=resolve;});await route.fulfill({response});}else await route.continue();};
   await across.route('**/api/manga/progress/session',heldSession);
   await ar().locator('[data-chapter="Blue Hour/Chapter 12"] .page').evaluate(node=>{const r=node.closest('.reader'),b=r.getBoundingClientRect(),p=node.getBoundingClientRect();r.scrollTop+=p.top+p.height/2-(b.top+b.height/2);});
   while(!releaseSession)await across.waitForTimeout(10);await centerChapter('Chapter 11',0);
   await across.getByLabel('Reading layout').selectOption('paged');releaseSession();await across.waitForTimeout(250);await pagedReady('Chapter 11',0);
   assert.equal((await saved('Blue Hour','Blue Hour/Chapter 12','folder')).position,null,'a stale session reply cannot mark the next chapter');
   await across.unroute('**/api/manga/progress/session',heldSession);await across.getByLabel('Reading layout').selectOption('vertical');
   check('A failed vertical append leaves the current chapter readable with Retry; appended pages and a late session reply after reversal/layout change never mark unread progress');
   for(let chapter=11;chapter<=18;chapter++){
    await centerChapter('Chapter '+chapter,0);
    await across.waitForFunction(()=>document.querySelectorAll('.chapter-content').length<=3);
    assert(await ar().locator('.page img').count()<=10);
   }
   for(let chapter=17;chapter>=14;chapter--)await centerChapter('Chapter '+chapter,0);
   await across.screenshot({path:path.join(config.output,'manga-continuation-window.png')});
   const before=await ar().locator('.chapter-content').filter({has:across.getByRole('heading',{name:'Chapter 14',exact:true})}).locator('.page').evaluate(n=>n.getBoundingClientRect().top);
   await across.waitForTimeout(500);const after=await ar().locator('.chapter-content').filter({has:across.getByRole('heading',{name:'Chapter 14',exact:true})}).locator('.page').evaluate(n=>n.getBoundingClientRect().top);assert(Math.abs(after-before)<2);
   report.continuation={source_id:sourceId,series_path:'Blue Hour',path:'Blue Hour/Chapter 14',kind:'folder',page:0,mode:'vertical'};
   await across.getByRole('button',{name:'Back to chapters'}).click();
   check('A long vertical series rolls forward and backward through at most three mounted chapters with bounded images and no delayed layout jump');
  }finally{releaseNext?.();releaseSession?.();await acrossContext.close();}
  assert.deepEqual(report.errors,[]);
  check('disable rejects Manga work and preserves source registration and rediscovery');
 }finally{fs.writeFileSync(path.join(config.output,'report.json'),JSON.stringify(report,null,2));await context.tracing.stop({path:path.join(config.output,'trace.zip')});await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

// Actual archive/capture API plus timed Twitter UI and local video interaction.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
assert.match(config.home,/[/\\]keivotos-modularization-[^/\\]+[/\\]home$/);
const report={checks:[],errors:[],expectedConsole:[],requests:[]};
let expectedLostReply=false;
(async()=>{
 const browser=await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});
 await context.tracing.start({screenshots:true,snapshots:true});
 const page=await context.newPage();page.setDefaultTimeout(12000);
 page.on('pageerror',e=>report.errors.push(e.message));
 page.on('console',m=>{
  if(m.type()!=='error')return;
  const url=m.location().url?new URL(m.location().url):null;
  if(expectedLostReply&&url?.origin===config.url&&url.pathname==='/api/twitter/downloads'&&m.text().includes('ERR_FAILED')){
   report.expectedConsole.push({url:url.href,message:m.text()});return;
  }
  // Once the hidden nested owner excludes captures, the Files directory has
  // no cover candidate. Keep that reviewed image miss separate from failures.
  if(url?.origin===config.url&&url.pathname==='/api/files/thumbnail'&&
     url.searchParams.get('path')==='captures'&&m.text().includes('404')){
   report.expectedConsole.push({url:url.href,message:m.text()});return;
  }
  report.errors.push(m.text());
 });
 await page.route('**/*',route=>{
  const url=new URL(route.request().url());
  if(url.origin!==config.url){report.errors.push('External request '+url.href);return route.abort();}
  report.requests.push(url.pathname);return route.continue();
 });
 const check=s=>{report.checks.push(s);console.log('PASS: '+s);};
 async function api(relative,method='GET',data){
  const response=await context.request.fetch(config.url+relative,{method,data,headers:{Origin:config.url}});
  return {status:response.status(),data:await response.json()};
 }
 function hashes(directory){
  const values={};
  for(const entry of fs.readdirSync(directory,{withFileTypes:true})){
   const name=path.join(directory,entry.name);
   if(entry.isDirectory())Object.assign(values,hashes(name));
   else if(entry.isFile())values[name]=crypto.createHash('sha256').update(fs.readFileSync(name)).digest('hex');
  }
  return values;
 }
 const original=hashes(config.mediaLocal);
 try{
  const modules=(await api('/api/suite/modules')).data;
  const twitter=modules.find(m=>m.slug==='twitter');
  assert(twitter&&twitter.experimental&&!twitter.enabled&&twitter.disableable);
  assert.equal((await api('/api/twitter/library')).status,409);
  assert.equal((await api('/api/twitter/folders')).status,409);
  assert.equal((await api('/api/twitter/capture')).status,409);
  assert.equal((await api('/api/twitter/media')).status,409);
  assert.equal((await api('/api/suite/modules/twitter/enable','POST')).status,200);
  assert.equal((await api('/api/suite/modules/twitter/status')).data.state,'running');
  const registration=await api('/api/suite/folders/apply','POST',{folders:[{path:config.media,display_name:'Saved posts fixture',role:'twitter'}]});
  assert.equal(registration.status,200,JSON.stringify(registration));
  const owner=registration.data.sources.find(s=>s.role==='twitter');assert(owner);
  assert.equal((await api('/api/twitter/folders')).data[0].available,true);
  check('Twitter is optional/experimental and disabled by default; enabling and shared role registration work');

  const saved=[];let offset=0;
  do{
   const result=await api(`/api/twitter/library?offset=${offset}&limit=17`);
   assert.equal(result.status,200);assert.equal(result.data.total,69);
   saved.push(...result.data.items);offset=result.data.next_offset;
  }while(offset!==null);
  assert.equal(saved.length,68);
  assert.equal(new Set(saved.map(r=>r.source_id+':'+r.path)).size,68);
  assert.deepEqual(saved.map(r=>r.capture_id),saved.map(r=>r.capture_id).sort().reverse());
  const info=JSON.parse(fs.readFileSync(path.join(config.mediaLocal,'fixture-info.json'),'utf8'));
  assert(saved.every(r=>r.raw_text===info.raw_text&&r.available));
  assert.equal((await api('/api/twitter/library?q='+encodeURIComponent('이름'))).data.items.length,68);
  assert.equal((await api('/api/twitter/library?author=id%3A123')).data.items.length,68);
  assert.deepEqual((await api('/api/twitter/library?q=no-match')).data.items,[]);
  check('Incremental newest-first pages preserve exact Unicode/spacing, author snapshots and identities; incomplete capture excluded');

  for(const relative of ['/api/twitter/library?limit=241','/api/twitter/library?offset=-1','/api/twitter/library?q='+('x'.repeat(257))]){
   assert.equal((await api(relative)).status,422);
  }
  const record=saved[0];
  const detail=`/api/twitter/capture?source_id=${owner.source_id}&path=${encodeURIComponent(record.path)}`;
  assert.deepEqual((await api(detail)).data,record);
  assert.equal((await api('/api/twitter/capture?source_id='+owner.source_id+'&path=..%2Foutside')).status,422);
  assert.equal((await api('/api/twitter/capture?source_id=unknown&path='+encodeURIComponent(record.path))).status,404);
  check('Real HTTP validation rejects oversized pages/queries, traversal, unknown sources and incomplete detail');

  await page.goto(config.url);
  await page.getByRole('button',{name:'Open Keivotos menu',exact:true}).click();
  await page.getByRole('button',{name:'Settings',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'Settings',exact:true});await dialog.waitFor();
  await dialog.getByRole('navigation',{name:'Settings sections'}).getByRole('button',{name:'Modules',exact:true}).click();
  assert.equal(await page.getByRole('switch',{name:'Twitter module',exact:true}).getAttribute('aria-checked'),'true');
  await dialog.getByRole('navigation',{name:'Settings sections'}).getByRole('button',{name:'Folders',exact:true}).click();
  await dialog.getByText('Saved posts fixture',{exact:true}).waitFor();
  await dialog.getByRole('button',{name:'Manage folders…',exact:true}).click();
  const manager=page.getByRole('dialog',{name:'Manage folders',exact:true});await manager.waitFor();
  assert.equal(await manager.getByRole('textbox',{name:'Folder display name',exact:true}).inputValue(),'Saved posts fixture');
  assert.equal(await page.locator('select option[value="twitter"]').count()>0,true);
  await manager.getByRole('button',{name:'Close',exact:true}).click();
  await page.getByRole('button',{name:'Close settings',exact:true}).click();
  check('Real shared Settings displays Twitter module enablement and its existing folder role');

  const mediaUrl=`/api/twitter/media?source_id=${owner.source_id}&path=${encodeURIComponent(record.path)}&asset=video`;
  const range=await context.request.get(config.url+mediaUrl,{headers:{Range:'bytes=0-31'}});
  assert.equal(range.status(),206);assert.equal((await range.body()).length,32);
  assert.match(range.headers()['content-type'],/video\/mp4/);
  assert.match(range.headers()['content-range'],/^bytes 0-31\//);
  const avatar=await context.request.get(config.url+`/api/twitter/media?source_id=${owner.source_id}&path=${encodeURIComponent(record.path)}&asset=avatar`);
  assert.equal(avatar.status(),200);assert.match(avatar.headers()['content-type'],/image\/jpeg/);
  await page.evaluate(url=>{
   const probe=document.createElement('video');probe.id='twitter-archive-media-probe';probe.controls=true;probe.preload='metadata';probe.src=url;
   probe.style.cssText='position:fixed;right:24px;bottom:24px;width:240px;z-index:9999';document.body.append(probe);
  },config.url+mediaUrl);
  await page.waitForFunction(()=>document.getElementById('twitter-archive-media-probe')?.readyState>=1);
  assert.equal(await page.locator('#twitter-archive-media-probe').evaluate(v=>v.paused),true);
  await page.locator('#twitter-archive-media-probe').evaluate(v=>v.play());
  await page.waitForFunction(()=>document.getElementById('twitter-archive-media-probe').currentTime>.1);
  await page.locator('#twitter-archive-media-probe').evaluate(v=>{v.pause();v.currentTime=1.5;});
  await page.waitForFunction(()=>Math.abs(document.getElementById('twitter-archive-media-probe').currentTime-1.5)<.1);
  await page.locator('#twitter-archive-media-probe').evaluate(v=>{v.pause();v.removeAttribute('src');v.load();v.remove();});
  const unsatisfied=await context.request.get(config.url+mediaUrl,{headers:{Range:'bytes=999999999-'}});
  assert.equal(unsatisfied.status(),416);
  assert.equal((await api(mediaUrl.replace('asset=video','asset=source'))).status,422);
  check('Twitter JPEG/MP4 route reuses shared ranges; actual native playback/seek and invalid ranges/assets are verified');

  const fixture=JSON.parse(fs.readFileSync(path.join(config.mediaLocal,'fixture-endpoint.json'),'utf8')).url;
  async function control(value){return (await context.request.post(fixture+'/control',{data:value})).json();}
  async function stats(){return (await context.request.get(fixture+'/stats')).json();}
  async function until(jobId,status){
   const end=Date.now()+20000;
   while(Date.now()<end){
    const value=(await api('/api/twitter/downloads/'+jobId)).data;
    if(value.status===status)return value;
    if(value.status==='failed'&&status!=='failed')throw new Error(JSON.stringify(value));
    await page.waitForTimeout(100);
   }
   throw new Error('Twitter job timeout '+jobId+' '+status);
  }
  assert.equal((await api('/api/twitter/settings','POST',{source_id:owner.source_id})).status,200);
  await page.reload();await page.waitForLoadState('networkidle');
  assert.equal((await api('/api/twitter/settings')).data.saved_source_id,owner.source_id);
  assert.equal((await api('/api/twitter/downloads','POST',{url:'https://outside.invalid/status/333',source_id:owner.source_id,request_id:crypto.randomUUID()})).status,400);
  await control({delay:.5,fail:false});
  const payload={url:'https://x.com/SavedAuthor/status/333',source_id:owner.source_id,request_id:crypto.randomUUID()};
  const accepted=await api('/api/twitter/downloads','POST',payload);assert.equal(accepted.status,200);
  const repeated=await api('/api/twitter/downloads','POST',payload);assert.equal(repeated.data.id,accepted.data.id);
  assert.equal((await api('/api/twitter/downloads','POST',{...payload,request_id:crypto.randomUUID()})).status,400);
  const completed=await until(accepted.data.id,'complete');assert.equal(completed.progress,100);
  assert.equal((await stats()).source_requests,1);
  const capture=(await api(`/api/twitter/capture?source_id=${owner.source_id}&path=${encodeURIComponent(completed.path)}`)).data;
  assert.equal(capture.raw_text,'  한글 世界\nline two \t\n');
  const firstDirectory=path.dirname(path.join(config.mediaLocal,completed.path));const firstSaved=hashes(firstDirectory);
  const manifest=JSON.parse(fs.readFileSync(path.join(firstDirectory,'manifest.json'),'utf8'));
  assert.equal(manifest.complete,true);
  for(const [name,value] of Object.entries(manifest.files)){
   const bytes=fs.readFileSync(path.join(firstDirectory,name));assert.equal(bytes.length,value.size);
   assert.equal(crypto.createHash('sha256').update(bytes).digest('hex'),value.sha256);
  }
  check('Saved destination survives reload; invalid links refuse; repeated UUID and concurrent duplicate admission create one verified real local MP4 capture');

  await control({delay:0});
  const second=await api('/api/twitter/downloads','POST',{...payload,request_id:crypto.randomUUID()});
  const secondDone=await until(second.data.id,'complete');assert.notEqual(secondDone.path,completed.path);
  assert.deepEqual(firstSaved,hashes(firstDirectory));
  const page1=(await api('/api/twitter/downloads?limit=1')).data;assert.equal(page1.items.length,1);assert.equal(page1.next_offset,1);
  assert.equal((await api('/api/twitter/downloads?limit=1&offset=1')).data.items.length,1);
  check('Explicit later capture preserves a second immutable snapshot; durable job listing paginates without overwriting the earlier capture');

  await control({fail:true});
  const badPayload={...payload,url:'https://x.com/SavedAuthor/status/444',request_id:crypto.randomUUID()};
  const bad=await api('/api/twitter/downloads','POST',badPayload);
  const failed=await until(bad.data.id,'failed');assert.match(failed.error,/one direct MP4/);
  const partial=path.dirname(path.join(config.mediaLocal,failed.path));
  assert.equal(fs.existsSync(path.join(partial,'manifest.json')),false);
  const preserved=hashes(partial);assert(Object.keys(preserved).some(p=>p.endsWith('source.json')));
  await control({fail:false,delay:.5});
  const retryToken=crypto.randomUUID();
  assert.equal((await api(`/api/twitter/downloads/${bad.data.id}/retry`,'POST',{request_id:retryToken})).status,200);
  const duplicateRetry=await api(`/api/twitter/downloads/${bad.data.id}/retry`,'POST',{request_id:retryToken});assert.equal(duplicateRetry.data.id,bad.data.id);
  const retried=await until(bad.data.id,'complete');assert.equal(retried.attempt,2);assert.notEqual(retried.path,failed.path);
  assert.deepEqual(preserved,hashes(partial));
  assert.equal((await api(`/api/twitter/downloads/${bad.data.id}/retry`,'POST',{request_id:retryToken})).data.attempt,2);
  check('Unsupported source retains its incomplete response; explicit Retry makes another directory under the same job and repeated retry acknowledgement stays idempotent');

  await control({delay:4});
  const pending=await api('/api/twitter/downloads','POST',{...payload,url:'https://x.com/SavedAuthor/status/555',request_id:crypto.randomUUID()});
  await until(pending.data.id,'running');
  const queued=await api('/api/twitter/downloads','POST',{...payload,url:'https://x.com/SavedAuthor/status/666',request_id:crypto.randomUUID()});
  assert.equal((await api('/api/suite/modules/twitter/disable','POST')).status,200);
  assert.equal((await api('/api/twitter/downloads')).status,409);
  const stoppedStats=await stats();
  assert.equal((await api('/api/suite/modules/twitter/enable','POST')).status,200);
  assert.equal((await api('/api/twitter/downloads/'+pending.data.id)).data.status,'interrupted');
  assert.equal((await api('/api/twitter/downloads/'+queued.data.id)).data.status,'interrupted');
  await page.waitForTimeout(500);assert.equal((await stats()).source_requests,stoppedStats.source_requests);
  assert.deepEqual(firstSaved,hashes(firstDirectory));
  check('Disabling reaps the active helper and interrupts queued work; re-enable keeps durable receipts and starts no remote work until Retry');
  await control({delay:0});

  // Force smaller real API pages to exercise continuation without retaining a
  // second synthetic archive or returning fabricated UI responses.
  const smallPages=async route=>{const url=new URL(route.request().url());url.searchParams.set('limit','17');await route.fulfill({response:await route.fetch({url:url.href})});};
  await page.route('**/api/twitter/library?*',smallPages);
  await page.getByRole('button',{name:'Open Keivotos menu',exact:true}).click();
  await page.locator('.app-drawer').getByRole('button',{name:'Twitter',exact:true}).click();
  const archive=page.getByRole('main',{name:'Twitter archive'});
  const posts=archive.locator('[data-twitter-post]');
  const search=page.getByRole('textbox',{name:'Search saved posts or paste a Twitter URL'});
  const reveal=page.locator('.twitter-surface .download-reveal');
  const download=page.locator('.twitter-surface .download');
  const sidebar=page.getByRole('navigation',{name:'Twitter sections'});
  const firstPageCount=(await api('/api/twitter/library?limit=17')).data.items.length;
  await page.waitForFunction(count=>document.querySelectorAll('[data-twitter-post]').length===count,firstPageCount);
  assert.equal(await page.title(),'Keivotos - Twitter');
  assert.equal(await posts.first().locator('.text').textContent(),info.raw_text);
  assert(await archive.boundingBox().then(box=>box.width<=601));
  assert.equal(await posts.locator('video[autoplay]').count(),0);
  assert(await posts.locator('video').evaluateAll(videos=>videos.every(v=>v.preload==='none'&&v.paused&&v.readyState===0)));
  assert.equal(await page.locator('a[href^="https://pbs.twimg.com"],img[src^="https://"]').count(),0);
  check('Twitter surface discovers automatically; central feed, captured author/text and guarded local video render without autoplay or remote images');

  const inputBox=await search.boundingBox();
  assert.equal(await reveal.getAttribute('inert'),'');assert.equal(await download.getAttribute('tabindex'),'-1');
  for(const invalid of ['http://x.com/SavedAuthor/status/777','https://x.com.evil.invalid/SavedAuthor/status/777','https://user@x.com/SavedAuthor/status/777','https://x.com:444/SavedAuthor/status/777','https://x.com/SavedAuthor/status/0','https://x.com/SavedAuthor/status/777/photo/1']){
   await search.fill(invalid);await page.waitForTimeout(240);assert.equal(await reveal.getAttribute('inert'),'');
  }
  await search.fill('');await page.waitForTimeout(250);
  await search.fill('https://twitter.com/SavedAuthor/status/777?s=20');
  await page.waitForTimeout(60);
  const middle=await reveal.evaluate(node=>({opacity:+getComputedStyle(node).opacity,clip:getComputedStyle(node).clipPath,transform:getComputedStyle(node).transform}));
  assert(middle.opacity>0&&middle.opacity<1,JSON.stringify(middle));
  await page.waitForTimeout(210);assert.equal(await reveal.evaluate(node=>+getComputedStyle(node).opacity),1);
  const urlBox=await search.boundingBox();assert(Math.abs(inputBox.x-urlBox.x)<.1&&Math.abs(inputBox.width-urlBox.width)<.1);
  await search.fill('not a link');await page.waitForTimeout(50);
  assert(await reveal.evaluate(node=>+getComputedStyle(node).opacity)<1);
  await search.fill('https://x.com/i/web/status/777');await page.waitForTimeout(25);await search.fill('');await page.waitForTimeout(25);await search.fill('https://x.com/SavedAuthor/status/777');
  await page.waitForTimeout(280);assert.equal(await reveal.getAttribute('inert'),null);assert.equal(await download.getAttribute('tabindex'),'0');
  report.reveal={middle,stable_input:inputBox,visible_input:urlBox};
  await page.emulateMedia({reducedMotion:'reduce'});
  assert(await reveal.evaluate(node=>getComputedStyle(node).transitionDuration.split(',').every(value=>parseFloat(value)<=.00001)));
  await search.fill('');await search.fill('https://x.com/SavedAuthor/status/777');assert.equal(await reveal.evaluate(node=>+getComputedStyle(node).opacity),1);
  await page.emulateMedia({reducedMotion:'no-preference'});
  await page.evaluate(()=>document.documentElement.dataset.motion='reduced');
  assert(await reveal.evaluate(node=>getComputedStyle(node).transitionDuration.split(',').every(value=>parseFloat(value)<=.00001)));
  await page.evaluate(()=>document.documentElement.dataset.motion='system');
  await search.fill('');await page.waitForTimeout(260);
  await search.focus();await search.press('Tab');assert.notEqual(await page.evaluate(()=>document.activeElement?.className),'download');
  check('Valid link alone reveals Download with measured intermediate motion and fixed input geometry; invalid links, reversal, inert Tab order and both reduced-motion preferences work');

  await search.fill('이름');await page.waitForTimeout(260);await page.waitForFunction(count=>document.querySelectorAll('[data-twitter-post]').length===count,firstPageCount);
  await archive.getByRole('button',{name:'Load more',exact:true}).click();
  await page.waitForFunction(count=>document.querySelectorAll('[data-twitter-post]').length===count,firstPageCount+17);
  const clicked=posts.nth(24).getByRole('button',{name:/^Read saved post/});
  await clicked.focus();const beforeScroll=await archive.evaluate(node=>node.scrollTop);const clickedKey=await posts.nth(24).getAttribute('data-twitter-post');
  await clicked.click();await archive.locator('article.detail').waitFor();
  assert.equal(await archive.locator('article.detail .text').textContent(),info.raw_text);
  await archive.getByRole('button',{name:'Back to saved view'}).click();
  assert.equal(await search.inputValue(),'이름');assert.equal(await posts.count(),firstPageCount+17);
  assert(Math.abs(await archive.evaluate(node=>node.scrollTop)-beforeScroll)<2);
  assert.equal(await page.evaluate(()=>document.activeElement?.closest('[data-twitter-post]')?.getAttribute('data-twitter-post')),clickedKey);
  const authorButton=posts.first().getByRole('button',{name:'Posts by Saved author 이름'});
  await authorButton.click();await archive.locator('.author-heading').waitFor();
  assert.equal(await archive.locator('.author-heading span').textContent(),'@SavedAuthor');
  await archive.getByRole('button',{name:'Back to saved view'}).click();
  await sidebar.getByRole('button',{name:'Authors',exact:true}).click();
  await page.locator('[data-twitter-author="id:123"]').waitFor();
  assert.equal(await page.locator('[data-twitter-author]').count(),1);
  await page.locator('[data-twitter-author="id:123"]').click();await archive.locator('.author-heading').waitFor();
  await archive.getByRole('button',{name:'Back to saved view'}).click();
  assert.equal(await page.evaluate(()=>document.activeElement?.getAttribute('data-twitter-author')),'id:123');
  await sidebar.getByRole('button',{name:'Saved posts',exact:true}).click();
  await search.fill('absent phrase');await archive.getByText('No saved posts found',{exact:true}).waitFor();
  assert.equal(await archive.getByRole('button',{name:'Load more',exact:true}).count(),0);
  await search.fill('');await page.waitForTimeout(260);await page.waitForFunction(count=>document.querySelectorAll('[data-twitter-post]').length===count,firstPageCount);
  check('Real smaller pages append without duplicate identities; exact detail/author views restore query, loaded rows, scroll and originating focus; filtered empty pages continue to null');

  const playing=posts.first().locator('video');
  await playing.evaluate(video=>video.play());await page.waitForFunction(()=>document.querySelector('[data-twitter-post] video')?.currentTime>.1);
  await playing.evaluate(video=>{video.pause();video.currentTime=1.5;});
  await page.waitForFunction(()=>Math.abs(document.querySelector('[data-twitter-post] video')?.currentTime-1.5)<.1);
  await playing.evaluate(async video=>{window.__twitterOldVideo=video;await video.play();});
  await posts.first().getByRole('button',{name:/^Read saved post/}).click();await archive.locator('article.detail').waitFor();
  assert(await page.evaluate(()=>window.__twitterOldVideo.paused&&!window.__twitterOldVideo.hasAttribute('src')));
  const detailVideo=archive.locator('article.detail video');
  await detailVideo.evaluate(video=>video.play());
  await detailVideo.evaluate(video=>video.requestFullscreen());
  await page.waitForFunction(()=>document.fullscreenElement?.tagName==='VIDEO');
  await page.evaluate(()=>document.exitFullscreen());
  await page.waitForFunction(()=>!document.fullscreenElement);
  await detailVideo.evaluate(video=>{window.__twitterOldVideo=video;});
  await page.getByRole('button',{name:'Open Keivotos menu'}).click();await page.locator('.app-drawer').getByRole('button',{name:'Files',exact:true}).click();
  assert(await page.evaluate(()=>window.__twitterOldVideo.paused&&!window.__twitterOldVideo.hasAttribute('src')));
  await page.getByRole('button',{name:'Open Keivotos menu'}).click();await page.locator('.app-drawer').getByRole('button',{name:'Twitter',exact:true}).click();await posts.first().waitFor();
  check('Native inline video plays, seeks and enters/exits fullscreen only on demand; detail navigation and module exit pause and release the exact old media node');

  const posted=[];let releaseAck;const ackGate=new Promise(resolve=>releaseAck=resolve);
  const lostAck=async route=>{
   if(route.request().method()!=='POST'){await route.continue();return;}
   posted.push(route.request().postDataJSON());const response=await route.fetch();assert.equal(response.status(),200);
   if(posted.length===1){await ackGate;expectedLostReply=true;await route.abort('failed');}else await route.fulfill({response});
  };
  await page.route('**/api/twitter/downloads',lostAck);await control({delay:.7,fail:false});
  await search.fill('https://twitter.com/SavedAuthor/status/731?s=20');await download.click();
  await page.waitForFunction(()=>document.querySelector('.twitter-surface .download')?.textContent==='Starting…');
  assert.equal(await search.isDisabled(),true);assert.equal(await page.getByLabel('Save to',{exact:true}).isDisabled(),true);
  await download.press('Enter');await page.waitForTimeout(200);assert.equal(posted.length,1);
  releaseAck();await page.getByRole('button',{name:'Retry request',exact:true}).waitFor();
  await page.reload();await page.getByRole('button',{name:'Retry request',exact:true}).waitFor();
  await page.getByRole('button',{name:'Retry request',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.twitter-surface input')?.value==='');
  assert.equal(posted.length,2);assert.deepEqual(posted[1],posted[0]);
  const recovered=await until(posted[0].request_id,'complete');
  await page.getByRole('button',{name:'View saved post 731',exact:true}).waitFor();
  await page.getByRole('button',{name:'View saved post 731',exact:true}).click();await archive.locator('article.detail').waitFor();
  assert.equal(await archive.locator('article.detail').getAttribute('data-twitter-post'),owner.source_id+':'+recovered.path);
  await archive.getByRole('button',{name:'Back to saved view'}).click();
  await search.fill('https://x.com/SavedAuthor/status/731');
  await search.dispatchEvent('keydown',{key:'Enter',code:'Enter',isComposing:true,keyCode:229});await page.waitForTimeout(100);assert.equal(posted.length,2);
  await search.press('Enter');await page.waitForFunction(()=>document.querySelector('.twitter-surface input')?.value==='');
  assert.equal(posted.length,3);assert.notEqual(posted[2].request_id,posted[0].request_id);
  await until(posted[2].request_id,'complete');
  await page.unroute('**/api/twitter/downloads',lostAck);
  check('Pending Download blocks duplicate submit and destination changes; lost committed acknowledgement survives reload and retries the identical UUID; later Enter creates a separate snapshot and composition Enter submits nothing');

  await control({fail:true,delay:0});await search.fill('https://x.com/SavedAuthor/status/732');await download.click();
  await page.getByRole('button',{name:'Retry post 732',exact:true}).waitFor();
  assert(await page.locator('[data-twitter-job]').filter({has:page.getByRole('button',{name:'Retry post 732',exact:true})}).textContent().then(text=>text.includes('one direct MP4')));
  await control({fail:false,delay:.7});await page.getByRole('button',{name:'Retry post 732',exact:true}).click();
  await page.getByRole('button',{name:'View saved post 732',exact:true}).waitFor();
  await control({delay:3});await search.fill('https://x.com/SavedAuthor/status/733');await download.click();
  await page.getByRole('button',{name:'Cancel post 733',exact:true}).waitFor();await page.getByRole('button',{name:'Cancel post 733',exact:true}).click();
  await page.getByRole('button',{name:'Retry post 733',exact:true}).waitFor();await control({delay:0});
  check('Actual generated unsupported capture exposes its saved error and Retry; a pending capture cancels through the UI while retaining its receipt and partial files');

  const grip=page.locator('[data-sidebar-owner="twitter"] [data-module-sidebar-grip]');
  const savedYoutubePosition=await page.evaluate(()=>localStorage.getItem('keivotos:youtube-sidebar-handle-position'));
  await grip.focus();await grip.press('End');assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('keivotos:twitter-sidebar-handle-position'))),90);
  await grip.click();await page.waitForTimeout(75);const movingWidth=await page.locator('[data-sidebar-owner="twitter"]').evaluate(node=>node.getBoundingClientRect().width);assert(movingWidth>0&&movingWidth<256);
  await grip.click();await page.waitForTimeout(320);assert.equal(await grip.getAttribute('aria-expanded'),'true');
  await grip.click();await page.waitForTimeout(320);assert.equal(await grip.getAttribute('aria-expanded'),'false');
  await page.reload();await posts.first().waitFor();assert.equal(await grip.getAttribute('aria-expanded'),'false');
  assert.equal(await page.evaluate(()=>localStorage.getItem('keivotos:youtube-sidebar-handle-position')),savedYoutubePosition);
  await grip.click();await page.waitForTimeout(320);
  await page.screenshot({path:path.join(config.output,'twitter-feed.png')});
  await page.setViewportSize({width:320,height:520});
  await grip.click();await page.waitForTimeout(320);
  await search.fill('https://x.com/SavedAuthor/status/734');await page.waitForTimeout(260);
  assert(await download.isVisible());assert(await download.boundingBox().then(box=>box.x+box.width<=320));
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.getByRole('button',{name:'Downloads',exact:true}).click();await page.getByLabel('Save to',{exact:true}).waitFor();
  assert(await page.getByRole('complementary',{name:'Twitter downloads'}).boundingBox().then(box=>box.x>=0&&box.x+box.width<=320));
  await page.screenshot({path:path.join(config.output,'twitter-narrow-downloads.png')});
  await page.getByRole('button',{name:'Close downloads',exact:true}).click();
  assert.equal(await page.getByRole('button',{name:'Downloads',exact:true}).evaluate(node=>document.activeElement===node),true);
  await page.getByRole('button',{name:'Downloads',exact:true}).click();
  await page.getByRole('button',{name:'Close downloads',exact:true}).press('Escape');
  assert.equal(await page.getByRole('button',{name:'Downloads',exact:true}).getAttribute('aria-expanded'),'false');
  assert.equal(await page.getByRole('button',{name:'Downloads',exact:true}).evaluate(node=>document.activeElement===node),true);
  await page.setViewportSize({width:1440,height:1000});await search.fill('');
  check('Twitter reuses the animated movable sidebar grip with independent saved keys; rapid reversal, reload and 320px feed/search/download panel fit without overflow');
  await page.unroute('**/api/twitter/library?*',smallPages);
  await page.getByRole('button',{name:'Open Keivotos menu'}).click();await page.locator('.app-drawer').getByRole('button',{name:'Files',exact:true}).click();

  const nested=await api('/api/suite/folders/apply','POST',{folders:[{path:config.media+'/captures',display_name:'Separate archive owner',role:'files',visible:false}]});
  assert.equal(nested.status,200,JSON.stringify(nested));
  assert.deepEqual((await api('/api/twitter/library')).data.items,[]);
  assert.equal((await api(detail)).status,403);
  assert.equal((await context.request.get(config.url+mediaUrl)).status(),403);
  check('Fresh nested registration excludes old parent index records and blocks metadata/media even for hidden children');

  assert.equal((await api('/api/suite/modules/twitter/disable','POST')).status,200);
  assert.equal((await api('/api/twitter/library')).status,409);
  assert.equal((await context.request.get(config.url+mediaUrl)).status(),409);
  assert.equal((await api('/api/suite/modules/twitter/enable','POST')).status,200);
  await page.reload();
  await page.waitForLoadState('networkidle');
  assert.equal((await api('/api/suite/modules/twitter/status')).data.state,'running');
  const after=hashes(config.mediaLocal);
  for(const [name,digest] of Object.entries(original))assert.equal(after[name],digest);
  assert.deepEqual(report.errors,[]);
  check('Reload/disable/re-enable preserve registrations and every original byte; no unexpected browser errors or external request');
  await page.screenshot({path:path.join(config.output,'twitter-archive-settings.png')});
 }catch(error){
  await page.screenshot({path:path.join(config.output,'failure.png')});fs.writeFileSync(path.join(config.output,'failure.html'),await page.content());throw error;
 }finally{
  fs.writeFileSync(path.join(config.output,'report.json'),JSON.stringify(report,null,2));
  await context.tracing.stop({path:path.join(config.output,'trace.zip')});await browser.close();
 }
})().catch(error=>{console.error(error);process.exitCode=1;});

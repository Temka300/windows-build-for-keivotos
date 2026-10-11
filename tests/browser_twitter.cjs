// Stage 27: real archive HTTP, Settings registration and shared local media.
// Feed/Download motion is added with the following approved UI stage.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
assert.match(config.home,/[/\\]keivotos-modularization-[^/\\]+[/\\]home$/);
const report={checks:[],errors:[],expectedConsole:[],requests:[]};
(async()=>{
 const browser=await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});
 await context.tracing.start({screenshots:true,snapshots:true});
 const page=await context.newPage();page.setDefaultTimeout(12000);
 page.on('pageerror',e=>report.errors.push(e.message));
 page.on('console',m=>{
  if(m.type()!=='error')return;
  const url=m.location().url?new URL(m.location().url):null;
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
  assert.deepEqual(original,hashes(config.mediaLocal));
  assert.deepEqual(report.errors,[]);
  check('Reload/disable/re-enable preserve registrations and every original byte; no unexpected browser errors or external request');
  await page.screenshot({path:path.join(config.output,'twitter-archive-settings.png')});
 }finally{
  fs.writeFileSync(path.join(config.output,'report.json'),JSON.stringify(report,null,2));
  await context.tracing.stop({path:path.join(config.output,'trace.zip')});await browser.close();
 }
})().catch(error=>{console.error(error);process.exitCode=1;});

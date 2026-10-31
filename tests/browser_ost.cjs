const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
assert.match(config.home,/[/\\]keivotos-modularization-[^/\\]+[/\\]home$/);
const report={checks:[],errors:[],console:[],covers:[],audio:[],timings:{}};
(async()=>{
 const browser=await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});
 await context.tracing.start({screenshots:true,snapshots:true});
 const page=await context.newPage();page.setDefaultTimeout(15000);
 page.on('pageerror',error=>report.errors.push(error.stack??error.message));
 page.on('console',message=>{if(message.type()==='error')report.console.push(message.text());});
 let expectedFailures=0,playbackStarted=false;
 const musicReads=new Set();
 page.on('request',request=>{const url=new URL(request.url());if(url.pathname.startsWith('/api/ost/')&&!['/api/ost/metadata-jobs','/api/ost/metadata-refresh'].includes(url.pathname))musicReads.add(request);});
 const finishedMusicRead=request=>musicReads.delete(request);page.on('requestfinished',finishedMusicRead);page.on('requestfailed',finishedMusicRead);
 page.on('response',response=>{
  if(response.status()<500)return;
  if(response.status()===503&&['/api/ost/library','/api/ost/tracks','/api/files/audio-metadata','/api/files/module-metadata'].includes(new URL(response.url()).pathname)&&expectedFailures>0){expectedFailures--;return;}
  report.errors.push(`${response.status()} ${response.url()}`);
 });
 await page.route('**/*',route=>{
  const url=new URL(route.request().url());
  if(url.origin!==config.url){report.errors.push('Unexpected external request');return route.abort();}
  if(url.pathname==='/api/ost/cover')report.covers.push(url.searchParams.get('path'));
  if(url.pathname==='/api/files/file'){
   report.audio.push(url.searchParams.get('path'));
   if(!playbackStarted)report.errors.push('Discovery unexpectedly requested audio playback');
  }
  return route.continue();
 });
 const check=message=>{report.checks.push(message);fs.writeFileSync(path.join(config.output,'progress.json'),JSON.stringify({checks:report.checks,errors:report.errors}));console.log('PASS: '+message);};
 const row=relative=>page.getByRole('list',{name:'Music folder contents'}).locator(`[data-ost-entry-path=${JSON.stringify(relative)}]`);
 const root=id=>page.getByRole('list',{name:'Music folders'}).locator(`[data-ost-source=${JSON.stringify(id)}]`);
 async function api(url,method='GET',body){return page.evaluate(async({url,method,body})=>{
  const response=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  return {status:response.status,data:await response.json()};
 },{url,method,body});}
 async function foldersSection(){
  const grip=page.locator('[data-sidebar-owner="ost"] [data-module-sidebar-grip]');
  const closed=await grip.getAttribute('aria-expanded')==='false';
  if(closed){await grip.click();await page.waitForTimeout(350);}
  await page.getByRole('navigation',{name:'Music sections'}).getByRole('button',{name:'Folders',exact:true}).click();
  if(closed){await grip.click();await page.waitForTimeout(350);}
 }
 async function chooseModule(folders=true){
  await page.getByRole('button',{name:'Open Keivotos menu',exact:true}).click();
  await page.locator('.app-drawer').getByRole('button',{name:'Music',exact:true}).click();
  await page.locator('.ost-surface').waitFor();
  if(folders){
   await foldersSection();
   // The view now retains its last folder on module return. This helper's
   // callers explicitly request the roots, so navigate there normally.
   while(await page.locator('.ost-surface .location .back').count()){
    await page.locator('.ost-surface .location .back').click();
    await page.waitForFunction(()=>document.querySelector('.ost-surface main')?.getAttribute('aria-busy')==='false');
   }
   await page.getByRole('list',{name:'Music folders'}).waitFor();
  }
 }
 async function openFolder(relative){await row(relative).click();await page.getByRole('navigation',{name:'Music location'}).getByRole('button',{name:path.posix.basename(relative),exact:true}).waitFor();await page.waitForFunction(()=>document.querySelector('.ost-surface main')?.getAttribute('aria-busy')==='false');}
 async function back(){await page.locator('.ost-surface .back').click();await page.waitForFunction(()=>document.querySelector('.ost-surface main')?.getAttribute('aria-busy')==='false');}
 async function pixel(button){const image=button.locator('img');await image.waitFor();await page.waitForFunction(element=>element?.complete&&element.naturalWidth>0,await image.elementHandle());return image.evaluate(image=>{const canvas=document.createElement('canvas');canvas.width=1;canvas.height=1;const draw=canvas.getContext('2d');draw.drawImage(image,0,0);return [...draw.getImageData(0,0,1,1).data].slice(0,3);});}
 async function returnToRoots(){
  await page.getByRole('navigation',{name:'Music location'}).getByRole('button').first().click();
  await page.waitForFunction(()=>document.querySelector('.ost-surface main')?.getAttribute('aria-busy')==='false');
  await page.getByRole('button',{name:'Back to Music',exact:true}).click();await page.getByRole('list',{name:'Music folders'}).waitFor();
 }
 async function openSettings(){
  await page.getByRole('button',{name:'Open Keivotos menu',exact:true}).click();
  await page.locator('.app-drawer').getByRole('button',{name:'Settings',exact:true}).click();
  await page.getByRole('dialog',{name:'Settings',exact:true}).waitFor();
 }
 async function closeSettings(){
  await page.getByRole('button',{name:'Close settings',exact:true}).click();
  if(await page.getByRole('button',{name:'Close menu',exact:true}).count())await page.getByRole('button',{name:'Close menu',exact:true}).click();
  await page.locator('.app-drawer').waitFor({state:'detached'});
 }
 async function pauseAndBrowse(){
  await page.getByRole('region',{name:'Music player',exact:true}).waitFor();
  await page.waitForFunction(()=>{const audio=document.querySelector('audio');return audio&&audio.readyState>=2&&!audio.paused;});
  await page.getByRole('button',{name:'Pause',exact:true}).click();
  await page.getByRole('button',{name:'Back to tracks',exact:true}).click();
  await page.locator('.ost-surface main').waitFor({state:'visible'});
 }
 try{
  await page.goto(config.url);
  assert.equal((await api('/api/ost/cover')).status,409);
  assert.equal((await api('/api/ost/metadata?source_id=missing&path=track.wav')).status,409);
  assert.equal((await api('/api/ost/albums')).status,409);assert.equal((await api('/api/ost/album-tracks?source_id=missing&anchor=track.wav')).status,409);
  assert.equal((await context.request.get(config.url+'/api/ost/tracks')).status(),409);
  assert.equal((await api('/api/suite/modules/ost/enable','POST')).status,200);
  const registration=await api('/api/suite/folders/apply','POST',{folders:[{path:config.media,display_name:'Soundtracks',role:'ost'}]});
  assert.equal(registration.status,200,JSON.stringify(registration));
  const owner=registration.data.sources.find(source=>source.role==='ost');
  await page.reload();await chooseModule(false);await page.getByRole('list',{name:'All Music tracks'}).waitFor();
  assert.deepEqual(await page.getByRole('navigation',{name:'Music sections'}).getByRole('button').allTextContents(),['Library','Albums','Folders']);
  const musicSearch=page.getByRole('textbox',{name:'Search music',exact:true});
  const searchStyle=await musicSearch.evaluate(node=>{const css=getComputedStyle(node);return {height:node.getBoundingClientRect().height,font:css.fontSize,padding:css.padding,border:css.borderRadius};});
  assert.equal(searchStyle.height,34);assert.equal(searchStyle.font,'14px');assert.equal(searchStyle.padding,'6px 12px');assert.equal(searchStyle.border,'8px');
  await foldersSection();
  check('Library is the default; sidebar order is Library/Albums/Folders; Music search matches Danbooru input height, type, padding and radius');
  const metadataPath='Album 2/00 - Opening.flac';
  const originalAudioBytes=fs.readFileSync(path.join(config.mediaLocal||config.media,metadataPath));
  const originalHash=crypto.createHash('sha256').update(originalAudioBytes).digest('hex');
  const parameters=new URLSearchParams({source_id:owner.source_id,path:metadataPath});
  const cold=(await api(`/api/ost/library?source_id=${owner.source_id}&parent=Album%202`)).data.items.find(item=>item.path===metadataPath);
  assert(['pending','ready'].includes(cold.audio_metadata.status));
  if(cold.audio_metadata.status==='pending')assert.equal(cold.audio_metadata.display_title,'00 - Opening');
  const parsed=await context.request.get(config.url+'/api/ost/metadata?'+parameters);assert.equal(parsed.status(),200);
  assert.equal(parsed.headers()['cache-control'],'no-store');const metadata=await parsed.json();
  assert.equal(metadata.status,'ready');assert.equal(metadata.display_title,'새벽 / First light');
  assert.deepEqual(metadata.tags.title,['  새벽 / First light  ']);assert.deepEqual(metadata.tags.artist,['Artist one','歌手 二']);
  assert.equal(metadata.duration,4);assert.equal(metadata.tags.album[0],'Blue hours');
  const filesMetadata=await context.request.get(config.url+'/api/files/audio-metadata?'+parameters);
  assert.deepEqual(await filesMetadata.json(),metadata);assert.equal(filesMetadata.headers()['cache-control'],'no-store');
  const warm=(await api(`/api/ost/library?source_id=${owner.source_id}&parent=Album%202`)).data.items.find(item=>item.path===metadataPath);
  assert.deepEqual(warm.audio_metadata,metadata);
  check('Music and Files share lazy cached tags, exact multiple values, clean title fallback and real duration; discovery stays parse-free and responses are no-store');

  await root(owner.source_id).click();await openFolder('Album 2');
  await row(metadataPath).click({button:'right'});await page.getByRole('menuitem',{name:'Show in Files',exact:true}).click();
  const information=page.getByRole('dialog',{name:'File information',exact:true});
  async function info(relative){await page.locator(`[data-files-entry-path=${JSON.stringify(relative)}]`).click({button:'right'});await page.getByRole('menuitem',{name:'Show info',exact:true}).click();await information.waitFor();}
  const audioInfo=information;
  const embeddedInfo=information;
  await info(metadataPath);await information.getByText('0:04',{exact:true}).waitFor();
  assert.equal(await information.locator('dl').count(),1);
  assert.equal(await information.getByText('Title',{exact:true}).count(),0);assert.equal(await information.locator('a').count(),0);
  assert.equal(await information.getByText('0:04',{exact:true}).count(),1);
  await information.getByRole('button',{name:'Origin',exact:true}).click();
  await information.getByRole('textbox',{name:'Title',exact:true}).waitFor();
  assert.equal(await information.getByRole('textbox',{name:'Title',exact:true}).inputValue(),'  새벽 / First light  ');
  assert.equal(await information.getByPlaceholder('Creator name').inputValue(),'Artist one; 歌手 二');
  await page.waitForFunction(()=>{const image=document.querySelector('[role="dialog"] img[alt="Local music cover"]');return image?.complete&&image.naturalWidth>0;});
  assert((await information.getByPlaceholder('Anything else worth noting…').inputValue()).includes('<img src="https://example.invalid/never">'));
  assert.equal(await information.locator('script').count(),0);
  await information.getByRole('button',{name:'Properties',exact:true}).click();assert.equal(await information.locator('dl').count(),1);
  await information.getByRole('button',{name:'Origin',exact:true}).click();await page.setViewportSize({width:360,height:440});
  await information.getByRole('textbox',{name:'Title',exact:true}).scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(config.output,'audio-metadata-360.png')});
  const infoBox=await information.boundingBox();assert(infoBox.x>=0&&infoBox.x+infoBox.width<=360);
  assert(await information.locator('input,textarea').evaluateAll(nodes=>nodes.every(node=>node.getBoundingClientRect().right<=360)));
  await information.getByRole('button',{name:'Close',exact:true}).click();await page.setViewportSize({width:1440,height:1000});
  check('one Properties list holds unique technical facts; existing Origin controls auto-fill tags, multiple artists, local art and inert text with narrow scrolling');

  let resolveMetadata;const metadataStarted=new Promise(resolve=>resolveMetadata=resolve);
  const delayedMetadata=async route=>{if(new URL(route.request().url()).searchParams.get('path')===metadataPath){resolveMetadata();await new Promise(resolve=>setTimeout(resolve,450));}return route.continue().catch(()=>{});};
  await page.route('**/api/files/audio-metadata?**',delayedMetadata);await info(metadataPath);await metadataStarted;
  await information.getByRole('button',{name:'Close',exact:true}).click();await info('Album 2/Track 2.wav');
  await audioInfo.getByText('0:20',{exact:true}).waitFor();await page.waitForTimeout(550);
  await information.getByRole('button',{name:'Origin',exact:true}).click();
  assert.equal(await information.getByRole('textbox',{name:'Title',exact:true}).count(),0);assert.equal(await information.getByPlaceholder('Creator name').count(),0);
  await page.unroute('**/api/files/audio-metadata?**',delayedMetadata);await information.getByRole('button',{name:'Close',exact:true}).click();
  expectedFailures++;let failMetadata=true;
  const failedMetadata=route=>{if(failMetadata){failMetadata=false;return route.fulfill({status:503,json:{detail:'Generated metadata failure'}});}return route.continue();};
  await page.route('**/api/files/audio-metadata?**',failedMetadata);await info(metadataPath);
  await audioInfo.getByText('Generated metadata failure',{exact:true}).waitFor();
  await audioInfo.getByRole('button',{name:'Retry metadata',exact:true}).click();await audioInfo.getByText('0:04',{exact:true}).waitFor();
  await page.unroute('**/api/files/audio-metadata?**',failedMetadata);await information.getByRole('button',{name:'Close',exact:true}).click();
  assert.equal(crypto.createHash('sha256').update(fs.readFileSync(path.join(config.mediaLocal||config.media,metadataPath))).digest('hex'),originalHash);
  check('closing/reopening Properties ignores delayed tags from another file; a failed read retries; original tagged audio stays byte-identical');
  await chooseModule();
  assert.equal((await context.request.get(config.url+'/api/ost/tracks?limit=241')).status(),422);
  assert.equal((await context.request.get(config.url+'/api/ost/tracks?offset=-1')).status(),422);
  assert.equal(await page.getByRole('button',{name:'Manage Music folders',exact:true}).count(),0);
  assert.equal(await page.getByRole('heading',{name:'Soundtrack folders',exact:true}).count(),0);
  assert.equal(await root(owner.source_id).locator('.art').innerText(),'📁');
  await page.screenshot({path:path.join(config.output,'ost-sources-clean.png')});
  await root(owner.source_id).click();
  await row('Root track.wav').waitFor();
  assert.deepEqual(await page.getByRole('navigation',{name:'Music location'}).getByRole('button').allTextContents(),['Soundtracks']);
  assert.equal(await page.locator('.ost-surface main h2').count(),0);
  assert.equal(await row('Album 2').locator('.art').innerText(),'📁');
  assert.equal(await row('Album 2').locator('small').count(),0);
  await page.screenshot({path:path.join(config.output,'ost-folder-clean.png')});
  assert.deepEqual(await page.getByRole('list',{name:'Music folder contents'}).locator('.name').allTextContents(),['Album 2','Album 10','Corrupt art','Empty','Large library','No art','Root track.wav']);
  await openFolder('Album 2');
  assert.deepEqual(await page.getByRole('list',{name:'Music folder contents'}).locator('.name').allTextContents(),['Deep','00 - Opening.flac','A long soundtrack title with quiet rain and a distant train that wraps across two rows while its complete name remains readable 曲 2.wav','Track 2.wav','Track 10.wav']);
  const embedded=await pixel(row('Album 2/00 - Opening.flac'));
  const fallback=await pixel(row('Album 2/Track 2.wav'));
  assert(embedded[0]>embedded[2]+20);assert(fallback[2]>fallback[0]+20);
  assert.equal(report.audio.length,0);playbackStarted=true;
  await row('Album 2/Track 2.wav').click();await pauseAndBrowse();assert.equal(await row('Album 2/Track 2.wav').getAttribute('aria-pressed'),'true');
  assert.equal(await page.getByRole('switch',{name:'Auto next',exact:true}).getAttribute('aria-checked'),'true');
  const cover=await context.request.get(`${config.url}/api/ost/cover?source_id=${owner.source_id}&path=Album%202%2FTrack%202.wav`);
  assert.equal(cover.status(),200);assert.equal(cover.headers()['content-type'],'image/webp');assert.match(cover.headers()['cache-control'],/immutable/);
  check('actual hierarchy, mixed folders, natural names, local/embedded precedence and immutable WebP covers; discovery requests no audio, track click opens playback');

  await openFolder('Album 2/Deep');
  await row('Album 2/Deep/曲 2.wav').locator('img').waitFor({state:'detached'});
  await back();await row('Album 2/Deep').waitFor();
  assert.equal(await row('Album 2/Deep').evaluate(element=>document.activeElement===element),true);
  assert.equal(await row('Album 2/Track 2.wav').getAttribute('aria-pressed'),'true');
  await back();await openFolder('Corrupt art');
  const recovered=await pixel(row('Corrupt art/Track 2.wav'));assert(recovered[0]>recovered[1]+15);
  await back();await openFolder('Empty');
  await page.getByRole('status').filter({hasText:'No folders or supported audio files here.'}).waitFor();
  await back();await openFolder('No art');
  await row('No art/Track 2.wav').locator('img').waitFor({state:'detached'});
  assert.equal(await row('No art/unsupported.aac').count(),0);
  assert.equal(await row('No art/note.txt').count(),0);
  await row('No art/broken.mp3').click();await page.getByRole('alert').filter({hasText:'This track is unavailable'}).waitFor();
  await page.getByRole('button',{name:'Back to tracks',exact:true}).click();assert.equal(await row('No art/broken.mp3').getAttribute('aria-pressed'),'true');
  check('nested Unicode, ancestor-only Back, focus/selection restoration, corrupt-art fallback, empty folders and neutral no-art/unsupported states');

  await back();await openFolder('Large library');
  await page.getByText('120 / 260 items',{exact:true}).waitFor();
  assert.equal(await page.getByRole('list',{name:'Music folder contents'}).locator('li').count(),120);
  await page.waitForTimeout(200);
  const firstPageCovers=report.covers.filter(value=>/^Large library\/Track /.test(value));
  assert(firstPageCovers.length<=120);
  const scroller=page.locator('.ost-surface main');
  await scroller.evaluate(element=>element.scrollTop=element.scrollHeight);
  const before=await scroller.evaluate(element=>element.scrollTop);
  report.timings.largeFolderSavedScroll=before;
  await page.getByRole('button',{name:'Load more',exact:true}).click();
  await page.getByText('240 / 260 items',{exact:true}).waitFor();
  report.timings.largeFolderMoreScroll=await scroller.evaluate(element=>element.scrollTop);
  assert(Math.abs(await scroller.evaluate(element=>element.scrollTop)-before)<3);
  const navigateAt=Date.now();await back();await openFolder('Large library');
  await page.getByText('240 / 260 items',{exact:true}).waitFor();
  report.timings.largeFolderReturnScroll=await scroller.evaluate(element=>element.scrollTop);
  assert(Math.abs(await scroller.evaluate(element=>element.scrollTop)-before)<3);
  report.timings.largeFolderReturnMs=Date.now()-navigateAt;
  await page.getByRole('button',{name:'Load more',exact:true}).click();await page.getByText('260 / 260 items',{exact:true}).waitFor();
  assert.equal(await page.getByRole('button',{name:'Load more',exact:true}).count(),0);
  assert.equal(await page.getByRole('list',{name:'Music folder contents'}).locator('li').count(),260);
  report.timings.firstPageCoverRequests=firstPageCovers.length;
  check('260-track folder paginates 120/240/260; images stay within loaded pages; Load more and Back retain count and scroll');

  await back();await openFolder('Album 2');
  const backButton=page.locator('.ost-surface .back');await page.mouse.move(1000,800);await page.waitForTimeout(230);
  assert(Math.abs((await backButton.boundingBox()).width-40)<1);
  await backButton.hover();await page.waitForTimeout(60);const middle=(await backButton.boundingBox()).width;assert(middle>40&&middle<56);
  await page.waitForTimeout(190);assert(Math.abs((await backButton.boundingBox()).width-56)<1);
  await page.mouse.move(1000,800);await page.waitForTimeout(230);assert(Math.abs((await backButton.boundingBox()).width-40)<1);
  await page.evaluate(()=>document.documentElement.dataset.motion='reduced');await backButton.hover();
  assert(await backButton.evaluate(element=>parseFloat(getComputedStyle(element).transitionDuration)<=.001));
  await page.evaluate(()=>delete document.documentElement.dataset.motion);
  const longTrack=page.getByRole('button',{name:/^Play track A long soundtrack title/});
  for(const width of [1440,720,360]){
   await page.setViewportSize({width,height:1000});await page.waitForTimeout(330);
   const compactGrip=page.locator('[data-sidebar-owner="ost"] [data-module-sidebar-grip]');
   if(width<=700&&await compactGrip.getAttribute('aria-expanded')==='true'){await compactGrip.click();await page.waitForTimeout(330);}
   assert.equal(await longTrack.getAttribute('title'),'A long soundtrack title with quiet rain and a distant train that wraps across two rows while its complete name remains readable 曲 2.wav');
   assert.equal(await longTrack.locator('.name').evaluate(element=>getComputedStyle(element).webkitLineClamp),'2');
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
   await page.screenshot({path:path.join(config.output,`ost-album-${width}.png`)});
  }
  await page.setViewportSize({width:1440,height:1000});
  check('Back expands/reverses with real timing and reduced motion; long names and rows fit desktop, 720px and 360px');

  let revealed,resolveReveal;const revealStarted=new Promise(resolve=>resolveReveal=resolve);
  const reveal=async route=>{revealed=route.request().postDataJSON();resolveReveal();await route.fulfill({json:{status:'revealed'}});};
  await page.route('**/api/files/reveal',reveal);
  const track=row('Album 2/Track 2.wav');await track.focus();await track.press('Shift+F10');
  let menu=page.getByRole('menu',{name:'Location for Track 2.wav'});
  assert.deepEqual(await menu.getByRole('menuitem').allTextContents(),['Show in Files','Open in Explorer']);
  await menu.getByRole('menuitem',{name:'Show in Files'}).waitFor({state:'visible'});
  await page.waitForFunction(()=>document.querySelector('[role="menu"] [role="menuitem"]')?.disabled===false);
  await page.waitForFunction(()=>document.activeElement?.textContent==='Show in Files');
  await page.keyboard.press('ArrowDown');
  await page.waitForFunction(()=>document.activeElement?.textContent==='Open in Explorer');
  await page.keyboard.press('Enter');await revealStarted;
  assert.deepEqual(revealed,{source_id:owner.source_id,path:'Album 2/Track 2.wav'});
  await track.click({button:'right'});await menu.getByRole('menuitem',{name:'Show in Files'}).click();
  const shown=page.locator('[data-files-entry-path="Album 2/Track 2.wav"]');await shown.waitFor();
  assert.equal(await shown.evaluate(element=>document.activeElement===element),true);
  await chooseModule();await root(owner.source_id).click();await row('Album 2').click({button:'right'});
  await page.getByRole('menu',{name:'Location for Album 2'}).getByRole('menuitem',{name:'Show in Files'}).click();
  await page.locator('[data-files-breadcrumb-path="Album 2"]').waitFor();
  await chooseModule();await root(owner.source_id).click();await openFolder('Album 2');
  await row('Album 2/Deep').click({button:'right'});
  await page.getByRole('menu',{name:'Location for Deep'}).press('Escape');
  assert.equal(await row('Album 2/Deep').evaluate(element=>document.activeElement===element),true);
  await page.unroute('**/api/files/reveal',reveal);
  check('Track menus retain two location actions; folder menus preserve Show in Files, Explorer and Escape focus beside Refresh metadata');

  await back();await openFolder('No art');
  const original=path.join(config.mediaLocal||config.media,'No art','Track 2.wav'),preserved=path.join(config.mediaLocal||config.media,'No art','preserved.wav');
  const originalBytes=fs.readFileSync(original);fs.renameSync(original,preserved);
  await openSettings();
  await closeSettings();
  await page.waitForFunction(()=>document.querySelector('[data-ost-entry-path="No art/Track 2.wav"]')?.getAttribute('aria-disabled')==='true');
  await page.waitForFunction(()=>document.querySelector('.ost-surface main')?.getAttribute('aria-busy')==='false');
  await row('No art/Track 2.wav').focus();await row('No art/Track 2.wav').press('Shift+F10');
  menu=page.getByRole('menu',{name:'Location for Track 2.wav'});
  assert.equal(await menu.getByRole('menuitem',{name:'Show in Files'}).isDisabled(),true);
  assert.equal(await menu.getByRole('menuitem',{name:'Open in Explorer'}).isDisabled(),true);
  await menu.press('Escape');assert.deepEqual(fs.readFileSync(preserved),originalBytes);fs.renameSync(preserved,original);
  await back();
  const childPath=config.media+'/Album 10';
  const second=await api('/api/suite/folders/apply','POST',{folders:[{path:childPath,display_name:'Soundtracks',role:'ost'}]});
  assert.equal(second.status,200,JSON.stringify(second));
  const child=second.data.sources.find(value=>value.path.replaceAll('\\','/')===childPath.replaceAll('\\','/'));
  const outer=await api(`/api/ost/library?source_id=${owner.source_id}`);assert(!outer.data.items.some(item=>item.path==='Album 10'));
  await page.reload();await foldersSection();await page.getByRole('list',{name:'Music folders'}).waitFor();
  assert.equal(await page.getByRole('button',{name:'Open Music folder Soundtracks',exact:true}).count(),2);
  await root(child.source_id).click();await row('Track 2.wav').waitFor();
  assert.equal(await row('Album 2/Track 2.wav').count(),0);
  check('unavailable indexed audio remains recoverable with disabled location actions; originals preserved; duplicate source/track names retain separate nested ownership');

  await openSettings();
  const hidden=await api('/api/suite/folders/apply','POST',{folders:[{source_id:child.source_id,display_name:'Soundtracks',role:'ost',visible:false}]});assert.equal(hidden.status,200,JSON.stringify(hidden));
  await closeSettings();
  await page.getByRole('status').filter({hasText:'This folder is no longer assigned to Music.'}).waitFor();
  await root(owner.source_id).click();await row('No art').waitFor();
  let failOnce=true;expectedFailures=1;
  const failure=async route=>{if(failOnce&&new URL(route.request().url()).searchParams.get('parent')==='No art'){failOnce=false;return route.fulfill({status:503,json:{detail:'Generated discovery failure'}});}return route.continue();};
  await page.route('**/api/ost/library?**',failure);await row('No art').click();
  await page.getByRole('alert').filter({hasText:'Generated discovery failure'}).getByRole('button',{name:'Retry',exact:true}).click();
  await row('No art/Track 2.wav').waitFor();await page.unroute('**/api/ost/library?**',failure);await back();
  let start;const started=new Promise(resolve=>start=resolve);
  const delay=async route=>{if(new URL(route.request().url()).searchParams.get('parent')==='Empty'){start();await new Promise(resolve=>setTimeout(resolve,450));}return route.continue();};
  await page.route('**/api/ost/library?**',delay);await row('Empty').click();await started;await back();
  await row('Root track.wav').waitFor();await page.waitForTimeout(550);
  assert.deepEqual(await page.getByRole('navigation',{name:'Music location'}).getByRole('button').allTextContents(),['Soundtracks']);
  assert.equal(await row('Root track.wav').count(),1);await page.unroute('**/api/ost/library?**',delay);
  assert.deepEqual(report.errors,[]);
  check('closing Settings reconciles lost ownership; API failures retry; fast Back cancels delayed discovery without stale rows or focus');
  await page.screenshot({path:path.join(config.output,'ost-root-rows.png')});

  const sections=page.getByRole('navigation',{name:'Music sections'}),dock=page.locator('[data-sidebar-owner="ost"]'),grip=dock.locator('[data-module-sidebar-grip]');
  if(await grip.getAttribute('aria-expanded')==='false'){await grip.click();await page.waitForTimeout(350);}
  await page.waitForTimeout(350);assert.equal(await grip.locator('.dots > span').count(),3);
  await page.evaluate(()=>{localStorage.setItem('keivotos:youtube-sidebar-open','false');localStorage.setItem('keivotos:language-sidebar-handle-position','41');});
  await grip.click();await page.waitForTimeout(90);const duringClose=await dock.evaluate(node=>node.getBoundingClientRect().width);assert(duringClose>0&&duringClose<256);
  await grip.click();await page.waitForTimeout(350);assert.equal(await grip.getAttribute('aria-expanded'),'true');
  const gripBox=await grip.boundingBox(),dockBox=await dock.boundingBox();
  await page.mouse.move(gripBox.x+20,gripBox.y+32);await page.mouse.down();await page.mouse.move(gripBox.x+20,dockBox.y+dockBox.height*.75,{steps:8});await page.mouse.up();
  assert.equal(await grip.getAttribute('aria-expanded'),'true');assert(Number(await page.evaluate(()=>localStorage.getItem('keivotos:ost-sidebar-handle-position')))>70);
  await grip.focus();await page.keyboard.press('Home');await page.keyboard.press('ArrowDown');assert.equal(await page.evaluate(()=>localStorage.getItem('keivotos:ost-sidebar-handle-position')),'15');
  assert.equal(await page.evaluate(()=>localStorage.getItem('keivotos:youtube-sidebar-open')),'false');assert.equal(await page.evaluate(()=>localStorage.getItem('keivotos:language-sidebar-handle-position')),'41');
  await sections.getByRole('button',{name:'Folders',exact:true}).focus();await page.mouse.move(1200,100);await page.waitForTimeout(1850);
  assert(Number(await grip.locator('.module-sidebar-grip-visual').evaluate(node=>getComputedStyle(node).opacity))<.05);
  await grip.hover();await page.waitForTimeout(200);assert(Number(await grip.locator('.module-sidebar-grip-visual').evaluate(node=>getComputedStyle(node).opacity))>.95);
  await grip.click();await page.waitForTimeout(350);await page.reload();await foldersSection();await root(owner.source_id).waitFor();assert.equal(await grip.getAttribute('aria-expanded'),'false');
  await grip.click();await page.waitForTimeout(350);await root(owner.source_id).click();
  await page.setViewportSize({width:360,height:440});await page.waitForTimeout(350);
  const narrowPanel=await dock.locator('.module-sidebar-panel').boundingBox(),narrowGrip=await grip.boundingBox();assert(Math.abs(narrowGrip.x-narrowPanel.x-narrowPanel.width)<1);assert(narrowGrip.x+narrowGrip.width<=360);
  await grip.click();await page.waitForTimeout(350);assert((await grip.boundingBox()).x>=0);
  await grip.click();await page.waitForTimeout(350);assert.equal(await grip.getAttribute('aria-expanded'),'true');
  await page.emulateMedia({reducedMotion:'reduce'});assert(await dock.evaluate(node=>parseFloat(getComputedStyle(node).transitionDuration)<.001));
  await page.emulateMedia({reducedMotion:'no-preference'});await page.evaluate(()=>document.documentElement.dataset.motion='reduced');assert(await dock.evaluate(node=>parseFloat(getComputedStyle(node).transitionDuration)<.001));
  await page.evaluate(()=>delete document.documentElement.dataset.motion);await page.setViewportSize({width:1440,height:1000});await page.waitForTimeout(350);
  check('Music three-dot sidebar animates/reverses, drags and restores independently; keyboard placement, delayed reveal and hover return remain');

  const allTracks=page.getByRole('list',{name:'All Music tracks'});
  const trackRow=(id,relative)=>allTracks.locator(`[data-ost-source=${JSON.stringify(id)}][data-ost-entry-path=${JSON.stringify(relative)}]`);
  await sections.getByRole('button',{name:'Library',exact:true}).click();await allTracks.waitFor();await page.waitForFunction(()=>document.querySelector('.ost-surface main')?.getAttribute('aria-busy')==='false');
  assert.equal(await allTracks.locator('li').count(),120);assert.equal(await allTracks.locator('[aria-label^="Open folder"]').count(),0);
  assert.equal(await trackRow(owner.source_id,'Album 2/Deep/曲 2.wav').count(),1);assert.equal(await allTracks.locator(`[data-ost-source=${JSON.stringify(child.source_id)}]`).count(),0);
  assert.equal(await allTracks.locator('.details small').count(),0);
  assert.equal(await allTracks.locator('.format').first().innerText(),'FLAC');
  assert.equal(await allTracks.locator('.name').first().innerText(),'새벽 / First light');
  assert(!String(await allTracks.locator('button').first().getAttribute('title')).includes('Album 2/'));
  const libraryScroll=page.locator('.ost-surface main');await libraryScroll.evaluate(node=>node.scrollTop=440);
  await sections.getByRole('button',{name:'Folders',exact:true}).click();await row('Root track.wav').waitFor();
  await sections.getByRole('button',{name:'Library',exact:true}).click();await allTracks.waitFor();await page.waitForFunction(()=>document.querySelector('.ost-surface main')?.getAttribute('aria-busy')==='false');
  assert(Math.abs(await libraryScroll.evaluate(node=>node.scrollTop)-440)<3);
  const boundary=(await api('/api/ost/tracks?offset=119&limit=2')).data.items;
  await trackRow(boundary[0].source_id,boundary[0].path).click();
  await page.waitForFunction(item=>{const node=document.querySelector('audio');return node&&!node.paused&&node.readyState>=2&&new URL(node.src).searchParams.get('source_id')===item.source_id&&new URL(node.src).searchParams.get('path')===item.path;},boundary[0]);
  await page.getByRole('button',{name:'Next song',exact:true}).click();
  await page.waitForFunction(item=>{const node=document.querySelector('audio');return node&&!node.paused&&node.readyState>=2&&new URL(node.src).searchParams.get('source_id')===item.source_id&&new URL(node.src).searchParams.get('path')===item.path;},boundary[1]);
  if(await page.getByRole('button',{name:'Pause',exact:true}).count())await page.getByRole('button',{name:'Pause',exact:true}).click();
  await page.getByRole('button',{name:'Back to tracks',exact:true}).click();assert.equal(await allTracks.locator('li').count(),120);
  await page.getByRole('button',{name:'Load more',exact:true}).click();await page.getByText('240 / 269 tracks',{exact:true}).waitFor();
  await page.getByRole('button',{name:'Load more',exact:true}).click();await page.getByText('269 / 269 tracks',{exact:true}).waitFor();
  const identities=await allTracks.locator('button').evaluateAll(nodes=>nodes.map(node=>JSON.stringify([node.dataset.ostSource,node.dataset.ostEntryPath])));assert.equal(new Set(identities).size,269);
  await page.screenshot({path:path.join(config.output,'ost-library.png')});
  check('Library flattens nested visible Music tracks, excludes hidden owned roots, paginates 120/240/269 and restores independent section scroll');

  let failLibrary=true;expectedFailures++;
  const failTracks=async route=>{if(failLibrary){failLibrary=false;return route.fulfill({status:503,json:{detail:'Generated Library failure'}});}return route.continue();};
  await sections.getByRole('button',{name:'Folders',exact:true}).click();await row('Root track.wav').waitFor();await page.route('**/api/ost/tracks?**',failTracks);
  await sections.getByRole('button',{name:'Library',exact:true}).click();await page.getByRole('alert').filter({hasText:'Generated Library failure'}).waitFor();assert.equal(await allTracks.locator('li').count(),269);
  await page.getByRole('button',{name:'Retry library',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.ost-surface main')?.getAttribute('aria-busy')==='false');await page.unroute('**/api/ost/tracks?**',failTracks);
  const restoredChild=await api('/api/suite/folders/apply','POST',{folders:[{source_id:child.source_id,display_name:'Soundtracks',role:'ost',visible:true}]});assert.equal(restoredChild.status,200);
  const mirrored=await api('/api/suite/folders/apply','POST',{folders:[{path:config.media+'/../second-music',display_name:'ZZ Soundtracks',role:'ost'}]});assert.equal(mirrored.status,200,JSON.stringify(mirrored));
  const mirrorSource=mirrored.data.sources.find(item=>item.display_name==='ZZ Soundtracks');
  await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await page.waitForFunction(()=>document.querySelector('.ost-surface main')?.getAttribute('aria-busy')==='false');
  await sections.getByRole('button',{name:'Folders',exact:true}).click();await row('Root track.wav').waitFor();await returnToRoots();await root(child.source_id).waitFor();
  await sections.getByRole('button',{name:'Library',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.ost-surface main')?.getAttribute('aria-busy')==='false');
  while(await page.getByRole('button',{name:'Load more',exact:true}).count()){await page.getByRole('button',{name:'Load more',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.ost-surface main')?.getAttribute('aria-busy')==='false');}
  await trackRow(child.source_id,'Track 2.wav').waitFor();assert.equal(await trackRow(owner.source_id,'Album 10/Track 2.wav').count(),0);
  assert.equal(await trackRow(owner.source_id,'Album 2/Track 2.wav').count(),1);assert.equal(await trackRow(mirrorSource.source_id,'Album 2/Track 2.wav').count(),1);
  await trackRow(child.source_id,'Track 2.wav').click();await page.waitForFunction(id=>{const node=document.querySelector('audio');return node&&!node.paused&&node.readyState>=2&&new URL(node.src).searchParams.get('source_id')===id;},child.source_id);
  await page.getByRole('button',{name:'Previous song',exact:true}).click();await page.waitForFunction(id=>{const node=document.querySelector('audio');return node&&!node.paused&&node.readyState>=2&&new URL(node.src).searchParams.get('source_id')===id;},owner.source_id);
  await page.getByRole('button',{name:'Next song',exact:true}).click();await page.waitForFunction(id=>{const node=document.querySelector('audio');return node&&!node.paused&&new URL(node.src).searchParams.get('source_id')===id;},child.source_id);
  await page.getByRole('button',{name:'Back to tracks',exact:true}).click();assert.equal(await trackRow(child.source_id,'Track 2.wav').getAttribute('aria-pressed'),'true');
  await sections.getByRole('button',{name:'Folders',exact:true}).click();assert.equal(await page.locator('audio').evaluate(node=>node.paused),false);
  await sections.getByRole('button',{name:'Library',exact:true}).click();assert.equal(await page.locator('audio').evaluate(node=>node.paused),false);
  await openSettings();assert.equal((await api('/api/suite/folders/apply','POST',{folders:[{source_id:child.source_id,display_name:'Soundtracks',role:'ost',visible:false}]})).status,200);await closeSettings();await page.locator('.audio-player').waitFor({state:'detached'});
  assert.equal((await api('/api/suite/folders/apply','POST',{folders:[{source_id:mirrorSource.source_id,display_name:'ZZ Soundtracks',role:'ost',visible:false}]})).status,200);
  await sections.getByRole('button',{name:'Folders',exact:true}).click();await root(owner.source_id).click();await row('Root track.wav').waitFor();
  check('Library failures retain rows/Retry; cross-source queue and current highlight keep exact identities; section browsing continues audio and source loss releases it');

  const audio=page.locator('audio');
  const player=()=>page.getByRole('region',{name:'Music player',exact:true});
  const transport=()=>page.getByRole('region',{name:'Music playback controls',exact:true});
  const play=()=>page.getByRole('button',{name:'Play',exact:true});
  const pause=()=>page.getByRole('button',{name:'Pause',exact:true});
  const loop=()=>page.getByRole('button',{name:/^Loop (off|all|one)$/});
  const auto=()=>page.getByRole('switch',{name:'Auto next',exact:true});
 async function waitTrack(relative){await page.waitForFunction(relative=>{const node=document.querySelector('audio');return node&&new URL(node.src).searchParams.get('path')===relative&&node.readyState>=2&&!node.paused;},relative);}
  async function paused(){if(!await audio.evaluate(node=>node.paused))await pause().click();await page.waitForFunction(()=>document.querySelector('audio')?.paused);}
  async function startTrack(relative){await row(relative).click();await player().waitFor();await waitTrack(relative);}
  async function finishTrack(){if(await audio.evaluate(node=>node.paused))await play().click();await page.waitForFunction(()=>{const node=document.querySelector('audio');return node&&!node.paused&&node.duration>0;});await audio.evaluate(node=>node.currentTime=node.duration-.12);}
  async function browse(){await page.getByRole('button',{name:'Back to tracks',exact:true}).click();await page.locator('.ost-surface main').waitFor({state:'visible'});}
  async function cycleTo(mode){for(let i=0;i<3&&await loop().getAttribute('aria-label')!==`Loop ${mode}`;i++)await loop().click();assert.equal(await loop().getAttribute('aria-label'),`Loop ${mode}`);}

  // Music transport uses no Video/YouTube implementation or enabled state.
  const modules=await api('/api/suite/modules');
  for(const id of ['video','youtube'])if(modules.data.find(value=>value.id===id)?.enabled)assert.equal((await api(`/api/suite/modules/${id}/disable`,'POST')).status,200);
  await openFolder('Album 2');await startTrack('Album 2/Track 2.wav');
  await page.waitForFunction(()=>document.querySelector('audio')?.currentTime>.05);
  assert(Math.abs(await audio.evaluate(node=>node.duration)-20)<.1);
  assert.equal(await auto().getAttribute('aria-checked'),'true');assert.equal(await loop().getAttribute('aria-label'),'Loop off');
  await paused();await player().focus();await page.keyboard.press('ArrowRight');
  assert(await audio.evaluate(node=>node.currentTime>=5&&node.currentTime<5.5));
  await page.keyboard.press('ArrowLeft');assert(await audio.evaluate(node=>node.currentTime<.5));
  await page.keyboard.press('Space');await page.waitForFunction(()=>!document.querySelector('audio')?.paused);
  await page.keyboard.down('Space');await page.keyboard.down('Space');await page.keyboard.up('Space');
  assert.equal(await audio.evaluate(node=>node.paused),true);
  const seek=page.getByRole('slider',{name:'Seek',exact:true});await seek.focus();await seek.press('Home');await seek.press('ArrowRight');
  assert(await audio.evaluate(node=>node.currentTime>0&&node.currentTime<.5));
  const volume=page.getByRole('slider',{name:'Volume',exact:true});
  const otherVolumes=await page.evaluate(()=>[localStorage.getItem('keivotos:video-volume-percent'),localStorage.getItem('keivotos:youtube-volume-percent')]);
  await volume.focus();await volume.press('Home');assert.equal(await audio.evaluate(node=>node.volume),0);
  await volume.press('End');assert.equal(await audio.evaluate(node=>node.volume),1);
  for(let i=0;i<6;i++)await volume.press('PageDown');for(let i=0;i<3;i++)await volume.press('ArrowRight');
  assert.equal(await volume.inputValue(),'43');assert.equal(await audio.evaluate(node=>node.volume),.43);
  await page.getByRole('button',{name:'Mute',exact:true}).click();assert.equal(await audio.evaluate(node=>node.volume),0);
  await page.getByRole('button',{name:'Unmute',exact:true}).click();assert.equal(await audio.evaluate(node=>node.volume),.43);
  assert.deepEqual(await page.evaluate(()=>[localStorage.getItem('keivotos:video-volume-percent'),localStorage.getItem('keivotos:youtube-volume-percent')]),otherVolumes);
  assert.equal(await page.evaluate(()=>localStorage.getItem('keivotos:ost-volume-percent')),'43');
  check('real WAV playback with Video/YouTube disabled; Space, repeat protection, five-second seeks, native slider keys, mute and independent 0–100 volume');

  for(const width of [1440,720,360]){
   await page.setViewportSize({width,height:1000});await page.waitForTimeout(330);
   if(width<=700&&await grip.getAttribute('aria-expanded')==='true'){await grip.click();await page.waitForTimeout(330);}
   const playBox=await play().boundingBox(),seekBox=await seek.boundingBox(),volumeBox=await volume.boundingBox();
   const currentBox=await page.getByLabel('Current time',{exact:true}).boundingBox(),durationBox=await page.getByLabel('Track duration',{exact:true}).boundingBox();
   const playerBox=await page.locator('.audio-player').boundingBox();
   assert(Math.abs(playBox.x+playBox.width/2-(playerBox.x+playerBox.width/2))<2);
   assert(seekBox.y>playBox.y+playBox.height&&seekBox.y>volumeBox.y+volumeBox.height);
   assert(currentBox.x+currentBox.width<seekBox.x&&durationBox.x>seekBox.x+seekBox.width);
   assert(Math.abs(currentBox.y+currentBox.height/2-seekBox.y-seekBox.height/2)<2);
   assert(Math.abs(durationBox.y+durationBox.height/2-seekBox.y-seekBox.height/2)<2);
   assert(1000-seekBox.y-seekBox.height>=10);
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   const nextBox=await page.getByRole('button',{name:'Next song',exact:true}).boundingBox(),previousBox=await page.getByRole('button',{name:'Previous song',exact:true}).boundingBox(),loopBox=await loop().boundingBox();
   assert(Math.abs((previousBox.x+nextBox.x+nextBox.width)/2-(playerBox.x+playerBox.width/2))<2);
   assert(loopBox.x>=nextBox.x+nextBox.width&&loopBox.x+loopBox.width<=width);
   await page.screenshot({path:path.join(config.output,`ost-player-${width}.png`)});
  }
  await page.setViewportSize({width:1440,height:1000});
  const originalAudio=await audio.elementHandle();await player().focus();await page.keyboard.press('Escape');
  assert.equal(await row('Album 2/Track 2.wav').evaluate(node=>document.activeElement===node),true);
  await play().click();await openFolder('Album 2/Deep');
  assert.equal(await audio.elementHandle().then(handle=>handle.evaluate((node,old)=>node===old,originalAudio)),true);
  await page.waitForTimeout(150);assert.equal(await audio.evaluate(node=>node.paused),false);
  await page.getByRole('button',{name:'Open player for Track 2.wav',exact:true}).click();await player().waitFor();await browse();
  await row('Album 2/Deep/曲 2.wav').waitFor();await paused();await back();
  const longPath='Album 2/A long soundtrack title with quiet rain and a distant train that wraps across two rows while its complete name remains readable 曲 2.wav';
  await startTrack(longPath);await paused();await page.setViewportSize({width:360,height:1000});
  assert.equal(await player().locator('h2').getAttribute('title'),path.posix.basename(longPath));
  assert.equal(await player().locator('h2').evaluate(node=>getComputedStyle(node).webkitLineClamp),'2');
  assert((await player().locator('h2').boundingBox()).height<=60);
  await page.screenshot({path:path.join(config.output,'ost-player-long-title-360.png')});
  await page.setViewportSize({width:1440,height:1000});await browse();
  check('centered Play and bottom seek bar fit 1440/720/360px; Escape restores row focus; the same audio continues through folder browsing and cover-view return');

  await startTrack('Album 2/Track 2.wav');await paused();
  await page.getByRole('button',{name:'Next song',exact:true}).click();await waitTrack('Album 2/Track 10.wav');await paused();
  assert.equal(await page.getByRole('button',{name:'Next song',exact:true}).isDisabled(),true);
  await page.getByRole('button',{name:'Previous song',exact:true}).click();await waitTrack('Album 2/Track 2.wav');await paused();
  await finishTrack();await waitTrack('Album 2/Track 10.wav');await paused();
  await finishTrack();await page.waitForFunction(()=>document.querySelector('audio')?.ended);
  assert.equal(new URL(await audio.getAttribute('src')).searchParams.get('path'),'Album 2/Track 10.wav');
  await auto().click();assert.equal(await auto().getAttribute('aria-checked'),'false');
  await cycleTo('one');await finishTrack();
  await page.waitForFunction(()=>{const node=document.querySelector('audio');return node&&!node.paused&&!node.ended&&node.currentTime<1;});
  assert.equal(new URL(await audio.getAttribute('src')).searchParams.get('path'),'Album 2/Track 10.wav');await paused();
  await cycleTo('off');await finishTrack();await page.waitForFunction(()=>document.querySelector('audio')?.ended);
  await auto().click();await cycleTo('all');await finishTrack();await waitTrack('Album 2/00 - Opening.flac');await paused();
  await page.getByRole('button',{name:'Previous song',exact:true}).click();await waitTrack('Album 2/Track 10.wav');await paused();
  await cycleTo('off');await browse();
  check('natural ended events advance with Auto next, Off stops at the folder boundary, One repeats independently, All wraps in both directions; actual FLAC decodes');

  await back();await openFolder('Large library');await page.getByText('120 / 260 items',{exact:true}).waitFor();
  await startTrack('Large library/Track 120.wav');await paused();
  await page.getByRole('button',{name:'Next song',exact:true}).click();await waitTrack('Large library/Track 121.wav');await paused();await browse();
  await page.getByText('120 / 260 items',{exact:true}).waitFor();
  assert.equal(await row('Large library/Track 121.wav').count(),0);
  await page.getByRole('button',{name:'Load more',exact:true}).click();await row('Large library/Track 121.wav').waitFor();
  assert.equal(await row('Large library/Track 121.wav').getAttribute('aria-pressed'),'true');
  await startTrack('Large library/Track 1.wav');await paused();await cycleTo('all');
  await page.getByRole('button',{name:'Previous song',exact:true}).click();await waitTrack('Large library/Track 260.wav');await paused();await cycleTo('off');await browse();
  await back();await openFolder('Large library');
  // Restore 240 loaded rows, then deliberately return to a fresh 120-row listing.
  await page.reload();await foldersSection();await root(owner.source_id).click();await openFolder('Large library');
  let resolveQueue;const queueStarted=new Promise(resolve=>resolveQueue=resolve);
  const queueDelay=async route=>{if(new URL(route.request().url()).searchParams.get('parent')==='Large library'&&new URL(route.request().url()).searchParams.get('offset')==='120'){resolveQueue();await new Promise(resolve=>setTimeout(resolve,450));}return route.continue().catch(()=>{});};
  await page.route('**/api/ost/library?**',queueDelay);
  await startTrack('Large library/Track 120.wav');await paused();await page.getByRole('button',{name:'Next song',exact:true}).click();await queueStarted;
  assert.equal(await page.getByRole('button',{name:'Next song',exact:true}).isDisabled(),true);
  await browse();await startTrack('Large library/Track 2.wav');await paused();await page.waitForTimeout(550);
  assert.equal(new URL(await audio.getAttribute('src')).searchParams.get('path'),'Large library/Track 2.wav');
  await page.unroute('**/api/ost/library?**',queueDelay);await browse();
  let queueFailure=true;expectedFailures++;
  const failQueue=async route=>{const url=new URL(route.request().url());if(queueFailure&&url.searchParams.get('parent')==='Large library'&&url.searchParams.get('offset')==='120'){queueFailure=false;return route.fulfill({status:503,json:{detail:'Generated queue failure'}});}return route.continue();};
  await page.route('**/api/ost/library?**',failQueue);await startTrack('Large library/Track 120.wav');await paused();
  await page.getByRole('button',{name:'Next song',exact:true}).click();await page.getByRole('status').filter({hasText:'Generated queue failure'}).waitFor();
  assert.equal(new URL(await audio.getAttribute('src')).searchParams.get('path'),'Large library/Track 120.wav');
  await page.getByRole('button',{name:'Next song',exact:true}).click();await waitTrack('Large library/Track 121.wav');await paused();
  await page.unroute('**/api/ost/library?**',failQueue);await browse();await back();
  check('queue crosses unloaded pages and wraps the current folder without changing browse pagination; rapid selection cancels delayed Next; failed queue extension retries without losing the track');

  await openFolder('Album 2');
  await page.evaluate(()=>{
   window.__ostPlay=HTMLMediaElement.prototype.play;window.__ostDeny=true;
   HTMLMediaElement.prototype.play=function(){if(this.tagName==='AUDIO'&&window.__ostDeny){window.__ostDeny=false;return Promise.reject(new DOMException('Generated policy rejection','NotAllowedError'));}return window.__ostPlay.call(this);};
  });
  await row('Album 2/Track 2.wav').click();await page.getByRole('status').filter({hasText:'Press Play to start audio.'}).waitFor();
  assert.equal(await page.getByRole('alert').count(),0);assert.equal(await play().isDisabled(),false);
  await play().click();await page.waitForFunction(()=>!document.querySelector('audio')?.paused);await paused();
  await page.evaluate(()=>{HTMLMediaElement.prototype.play=window.__ostPlay;delete window.__ostPlay;});await browse();
  await page.evaluate(()=>{
   window.__ostPlay=HTMLMediaElement.prototype.play;window.__ostDelay=true;
   HTMLMediaElement.prototype.play=function(){if(this.tagName==='AUDIO'&&window.__ostDelay){window.__ostDelay=false;return new Promise((resolve,reject)=>setTimeout(()=>reject(new Error('Generated stale playback failure')),350));}return window.__ostPlay.call(this);};
  });
  await row('Album 2/Track 2.wav').click();await player().waitFor();await browse();await startTrack('Album 2/Track 10.wav');await paused();await page.waitForTimeout(450);
  assert.equal(await page.getByRole('alert').count(),0);
  await page.evaluate(()=>{HTMLMediaElement.prototype.play=window.__ostPlay;delete window.__ostPlay;});await browse();await back();
  await openFolder('No art');
  const missing=path.join(config.mediaLocal||config.media,'No art','Track 2.wav'),saved=path.join(config.mediaLocal||config.media,'No art','preserved-for-playback.wav');
  const savedBytes=fs.readFileSync(missing);fs.renameSync(missing,saved);
  await row('No art/Track 2.wav').click();await page.getByRole('alert').filter({hasText:/track.*(unavailable|play)/}).waitFor();
  assert.equal(await play().isDisabled(),true);fs.renameSync(saved,missing);assert.deepEqual(fs.readFileSync(missing),savedBytes);
  await page.getByRole('button',{name:'Retry track',exact:true}).click();await page.waitForFunction(()=>{const node=document.querySelector('audio');return node&&node.readyState>=2&&!node.paused;});await paused();await browse();
  await row('No art/broken.mp3').click();await page.getByRole('alert').waitFor();
  await page.getByRole('button',{name:'Next song',exact:true}).click();await waitTrack('No art/Track 2.wav');await paused();await browse();await back();
  check('autoplay policy keeps Play usable; stale play rejection cannot damage a newer track; actual missing/corrupt audio explains failure and Retry/Next recover with original bytes preserved');

  await openFolder('Album 2');
  let resolveMedia;const mediaStarted=new Promise(resolve=>resolveMedia=resolve);
  const mediaDelay=async route=>{if(new URL(route.request().url()).searchParams.get('path')==='Album 2/00 - Opening.flac'){resolveMedia();await new Promise(resolve=>setTimeout(resolve,450));}return route.continue().catch(()=>{});};
  await page.route('**/api/files/file?**',mediaDelay);
  await row('Album 2/00 - Opening.flac').click();await mediaStarted;
  assert.equal(await page.getByLabel('Track duration',{exact:true}).innerText(),'—:—');
  assert.equal(await seek.getAttribute('aria-valuetext'),'Duration unavailable');
  assert.equal(await page.getByRole('button',{name:'Next song',exact:true}).isDisabled(),false);
  await page.getByRole('button',{name:'Next song',exact:true}).click();await waitTrack(longPath);await paused();await page.waitForTimeout(550);
  assert.equal(await page.getByRole('alert').count(),0);
  assert.equal(new URL(await audio.getAttribute('src')).searchParams.get('path'),longPath);
  await page.unroute('**/api/files/file?**',mediaDelay);await browse();await back();
  check('Next stays usable during actual delayed media loading; cancelled old media events and play promises cannot replace or fail the new track');

  await openFolder('Album 2');await startTrack('Album 2/Track 2.wav');await cycleTo('all');
  const burstAudio=await audio.elementHandle();
  const delayedBurst=async route=>{await new Promise(resolve=>setTimeout(resolve,300));return route.continue().catch(()=>{});};
  for(const [label,alternating,delayMedia] of [['alternating',true,false],['same-direction',false,false],['delayed',true,true]]){
   if(delayMedia)await page.route('**/api/files/file?**',delayedBurst);
   for(let n=0;n<32;n++){
    const button=page.getByRole('button',{name:alternating&&n%2?'Previous song':'Next song',exact:true});
    const box=await button.boundingBox();await page.mouse.click(box.x+box.width/2,box.y+box.height/2);await page.waitForTimeout(12);
   }
   await waitTrack('Album 2/Track 2.wav');const first=await audio.evaluate(node=>node.currentTime);await page.waitForTimeout(350);assert((await audio.evaluate(node=>node.currentTime))>first);
   assert.equal(await audio.evaluate((node,original)=>node===original,burstAudio),true);
   assert.equal(await page.getByLabel('Track duration',{exact:true}).innerText(),'0:20');
   assert.equal(await page.getByRole('alert').count(),0);report.timings['burst-'+label]=await audio.evaluate(node=>({time:node.currentTime,duration:node.duration,paused:node.paused}));
   if(delayMedia)await page.unroute('**/api/files/file?**',delayedBurst);
  }
  await audio.evaluate(node=>{node.dispatchEvent(new Event('pause'));node.dispatchEvent(new Event('ended'));});
  assert.equal(await pause().count(),1);assert.equal(new URL(await audio.getAttribute('src')).searchParams.get('path'),'Album 2/Track 2.wav');
  await paused();const timerSeekBox=await seek.boundingBox();await page.mouse.click(timerSeekBox.x+timerSeekBox.width*.35,timerSeekBox.y+timerSeekBox.height/2);await page.waitForTimeout(80);
  const observedSeconds=Math.floor(await audio.evaluate(node=>node.currentTime));assert(observedSeconds>0);assert.equal(await page.getByLabel('Current time',{exact:true}).innerText(),`${Math.floor(observedSeconds/60)}:${String(observedSeconds%60).padStart(2,'0')}`);
  await page.setViewportSize({width:360,height:440});await page.waitForTimeout(350);assert(await page.getByLabel('Current time',{exact:true}).isVisible());assert(await page.getByLabel('Track duration',{exact:true}).isVisible());
  await page.screenshot({path:path.join(config.output,'ost-player-timer-compact.png')});await page.setViewportSize({width:1440,height:1000});await cycleTo('off');await browse();await back();
  check('96 rapid native clicks, delayed media and stale pause/ended events retain one playable audio node; elapsed/duration agree with playback and seek in a short window');

  // Verify decoded nonzero PCM through the browser audio graph; hardware output is not asserted.
  await startTrack('Root track.wav');
  await cycleTo('all');await finishTrack();
  await page.waitForFunction(()=>{const node=document.querySelector('audio');return node&&!node.paused&&!node.ended&&node.currentTime<1;});
  assert.equal(new URL(await audio.getAttribute('src')).searchParams.get('path'),'Root track.wav');
  await cycleTo('off');
  await page.waitForFunction(()=>document.querySelector('audio')?.currentTime>.05);
  const signal=await audio.evaluate(async node=>{
   const graph=new AudioContext(),source=graph.createMediaElementSource(node),analyser=graph.createAnalyser();
   analyser.fftSize=256;source.connect(analyser);analyser.connect(graph.destination);await graph.resume();
   let rms=0;const data=new Float32Array(analyser.fftSize);
   for(let i=0;i<20&&rms<=.001;i++){await new Promise(resolve=>setTimeout(resolve,50));analyser.getFloatTimeDomainData(data);rms=Math.sqrt(data.reduce((sum,value)=>sum+value*value,0)/data.length);}
   const facts={rms,graphState:graph.state,paused:node.paused,muted:node.muted,time:node.currentTime,duration:node.duration,volume:node.volume};
   await graph.close();return facts;
  });
  report.timings.decodedAudio=signal;assert(signal.rms>.001,JSON.stringify(signal));
  await audio.evaluate(node=>window.__releasedAudio=node);
  await page.getByRole('button',{name:'Open Keivotos menu',exact:true}).click();await page.locator('.app-drawer').getByRole('button',{name:'Files',exact:true}).click();
  await page.locator('.audio-player').waitFor({state:'detached'});
  assert.equal(await page.evaluate(()=>window.__releasedAudio.paused&&window.__releasedAudio.getAttribute('src')===null&&window.__releasedAudio.readyState===0),true);
  await page.waitForTimeout(200);assert.equal(await page.evaluate(()=>window.__releasedAudio.currentTime),0);
  await chooseModule();await root(owner.source_id).click();await openFolder('Album 2');await startTrack('Album 2/Track 2.wav');
  assert.equal(await audio.evaluate(node=>node.volume),.43);assert.equal(await auto().getAttribute('aria-checked'),'true');
  await audio.evaluate(node=>window.__releasedAudio=node);
  await openSettings();
  assert.equal((await api('/api/suite/folders/apply','POST',{folders:[{source_id:owner.source_id,display_name:'Soundtracks',role:'ost',visible:false}]})).status,200);
  await closeSettings();
  await page.locator('.audio-player').waitFor({state:'detached'});
  assert.equal(await page.evaluate(()=>window.__releasedAudio.paused&&window.__releasedAudio.getAttribute('src')===null),true);
  assert.equal((await api('/api/suite/folders/apply','POST',{folders:[{source_id:owner.source_id,display_name:'Soundtracks',role:'ost',visible:true}]})).status,200);
  await page.reload();await foldersSection();await root(owner.source_id).click();await openFolder('Album 2');await startTrack('Album 2/Track 2.wav');
  await audio.evaluate(node=>window.__releasedAudio=node);
  await openSettings();
  await page.getByRole('navigation',{name:'Settings sections'}).getByRole('button',{name:'Modules',exact:true}).click();
  const ostSwitch=page.getByRole('switch',{name:'Music module',exact:true});await page.waitForFunction(()=>document.querySelector('[role="switch"][aria-label="Music module"]')?.disabled===false);await ostSwitch.click();
  await page.locator('.ost-surface').waitFor({state:'detached'});
  assert.equal(await page.evaluate(()=>window.__releasedAudio.paused&&window.__releasedAudio.getAttribute('src')===null),true);
  await closeSettings();
  assert.equal((await context.request.get(config.url+'/api/ost/metadata?'+parameters)).status(),409);
  assert.deepEqual(await (await context.request.get(config.url+'/api/files/audio-metadata?'+parameters)).json(),metadata);
  check('a one-track folder replays under All; browser audio graph decodes nonzero PCM; source loss/module exit/disable release media; scoped Music preferences survive reload/re-entry');

  const albumLocal=path.join(config.mediaLocal||config.media,'../album-music');
  function hashes(directory){const values={};function visit(folder){for(const entry of fs.readdirSync(folder,{withFileTypes:true})){const file=path.join(folder,entry.name);if(entry.isDirectory())visit(file);else values[path.relative(directory,file)]=crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');}}visit(directory);return values;}
  const albumOriginals=hashes(albumLocal);
  assert.equal((await api('/api/suite/modules/ost/enable','POST')).status,200);
  const musicRegistration=await api('/api/suite/folders/apply','POST',{folders:[
    {source_id:owner.source_id,display_name:'Soundtracks',role:'ost',visible:false},
    {path:config.media+'/../album-music',display_name:'Album library',role:'ost'},
    {path:config.media+'/../album-mirror',display_name:'Album mirror',role:'ost'}]});
  assert.equal(musicRegistration.status,200,JSON.stringify(musicRegistration));
  const albumOwner=musicRegistration.data.sources.find(item=>item.display_name==='Album library');
  const albumMirror=musicRegistration.data.sources.find(item=>item.display_name==='Album mirror');
  async function detailsReady(){await page.waitForFunction(async()=>{const response=await fetch('/api/ost/metadata-jobs');const result=await response.json();return result.items.every(job=>!['requested','started','working'].includes(job.state));},{},{timeout:90000});}
  for(const source of [albumOwner,albumMirror])assert.equal((await api('/api/ost/metadata-refresh','POST',{source_id:source.source_id,parent:''})).status,200);
  await detailsReady();
  async function sectionTo(name){const handle=page.locator('[data-sidebar-owner="ost"] [data-module-sidebar-grip]');const closed=await handle.getAttribute('aria-expanded')==='false';if(closed){await handle.click();await page.waitForTimeout(350);}await page.getByRole('navigation',{name:'Music sections'}).getByRole('button',{name,exact:true}).click();if(closed){await handle.click();await page.waitForTimeout(350);}}
  async function searchFor(query,count){const response=page.waitForResponse(response=>{const url=new URL(response.url());return url.pathname==='/api/ost/tracks'&&(url.searchParams.get('q')??'')===query;});await musicSearch.fill(query);await response;await page.waitForFunction(count=>document.querySelector('.ost-surface main')?.getAttribute('aria-busy')==='false'&&document.querySelectorAll('[aria-label="All Music tracks"]>li').length===count,count);await detailsReady();}
  await page.reload();await chooseModule(false);await allTracks.waitFor();await detailsReady();
  assert.equal(await allTracks.locator('li').count(),19);
  const opening=trackRow(albumOwner.source_id,'Blue/Disc 1/01 - First.flac');
  await opening.locator('.name').filter({hasText:'Opening song'}).waitFor();
  assert.equal(await opening.locator('.format').innerText(),'FLAC');assert.equal(await opening.locator('.duration').innerText(),'0:20');
  assert.equal(await opening.getAttribute('title'),'Opening song');assert.equal(await opening.locator('small').count(),0);
  const durationBox=await opening.locator('.duration').boundingBox(),openingBox=await opening.boundingBox();assert(Math.abs(durationBox.x+durationBox.width-(openingBox.x+openingBox.width-8))<2);
  const corrupt=trackRow(albumOwner.source_id,'Unknown/Corrupt.mp3');assert.equal(await corrupt.locator('.name').innerText(),'Corrupt');assert.equal(await corrupt.locator('.duration').innerText(),'—');
  await page.screenshot({path:path.join(config.output,'music-library-clean.png')});
  check('Library shows embedded/fallback titles, separate format badges and rightmost durations without paths; corrupt and missing tags keep their tracks');

  await page.locator('.ost-surface main').evaluate(node=>node.scrollTop=400);
  await searchFor('歌手',1);assert.equal(await allTracks.locator('.name').first().innerText(),'A later song');
  await searchFor('100%_',1);assert.equal(await allTracks.locator('.name').first().innerText(),'100%_Song');
  await searchFor('Blue',5);await opening.click();await waitTrack('Blue/Disc 1/01 - First.flac');
  const albumAudio=await audio.elementHandle();const beforeSearch=await audio.evaluate(node=>node.currentTime);
  await searchFor('100%_',1);assert.equal(await audio.evaluate(node=>node.paused),false);assert(await audio.evaluate(node=>node.currentTime)>=beforeSearch);
  await page.getByRole('button',{name:'Next song',exact:true}).click();await waitTrack('Blue/Disc 1/02 - Later.flac');
  assert.equal(await audio.elementHandle().then(handle=>handle.evaluate((node,old)=>node===old,albumAudio)),true);
  check('local Unicode/literal title, artist and album search debounces; changing search preserves the playing node and its captured filtered queue');

  let searchStarted;const oldSearch=new Promise(resolve=>searchStarted=resolve);
  const delayedSearch=async route=>{if(new URL(route.request().url()).searchParams.get('q')==='Opening'){searchStarted();await new Promise(resolve=>setTimeout(resolve,550));}return route.continue().catch(()=>{});};
  await page.route('**/api/ost/tracks?**',delayedSearch);await musicSearch.fill('Opening');await oldSearch;await searchFor('Finale',1);await page.waitForTimeout(600);
  assert.equal(await allTracks.locator('.name').first().innerText(),'Finale 曲');assert.equal(await musicSearch.evaluate(node=>node===document.activeElement),true);await page.unroute('**/api/ost/tracks?**',delayedSearch);
  let failSearch=true;expectedFailures++;
  const failedSearch=route=>{if(failSearch&&new URL(route.request().url()).searchParams.get('q')==='missing'){failSearch=false;return route.fulfill({status:503,json:{detail:'Generated search failure'}});}return route.continue();};
  await page.route('**/api/ost/tracks?**',failedSearch);await musicSearch.fill('missing');await page.getByRole('alert').filter({hasText:'Generated search failure'}).waitFor();assert.equal(await allTracks.locator('li').count(),1);
  await page.getByRole('button',{name:'Retry library',exact:true}).click();await page.getByText('No music matches this search.',{exact:true}).waitFor();await page.unroute('**/api/ost/tracks?**',failedSearch);
  await searchFor('',19);assert(Math.abs(await page.locator('.ost-surface main').evaluate(node=>node.scrollTop)-400)<3);
  check('late search replies cannot replace newer results or steal focus; failed search retains rows/Retry and clearing restores the unfiltered position');

  await sectionTo('Albums');await page.getByRole('list',{name:'Music albums'}).waitFor();await detailsReady();
  assert.equal(await page.getByRole('heading',{name:'Albums',exact:true}).count(),1);
  const albumList=(await api('/api/ost/albums')).data.items;
  assert.equal(albumList.filter(item=>item.name==='Blue').length,3);
  const blueAlbum=albumList.find(item=>item.source_id===albumOwner.source_id&&item.anchor.startsWith('Blue/'));
  const redAlbum=albumList.find(item=>item.source_id===albumOwner.source_id&&item.name==='Red');
  const albumTile=item=>page.locator(`[data-ost-album=${JSON.stringify(item.id)}]`);
  await albumTile(blueAlbum).click();const albumView=page.getByRole('region',{name:'Album Blue',exact:true});
  const albumRows=page.getByRole('list',{name:'Album tracks'});await page.waitForFunction(()=>document.querySelectorAll('[aria-label="Album tracks"] button').length===3&&document.querySelector('.ost-surface main')?.getAttribute('aria-busy')==='false');
  assert.deepEqual(await albumRows.locator('.name').allTextContents(),['Opening song','A later song','Finale 曲']);
  assert.deepEqual(await albumView.locator('h3').allTextContents(),['Disc 1','Disc 2']);assert.deepEqual(await albumRows.locator('.number').allTextContents(),['1','2','1']);
  assert.equal((await albumView.innerText()).includes('Blue/Disc'),false);
  const albumArt=albumView.locator('.cover img');await albumArt.waitFor();await page.waitForFunction(node=>node.complete&&node.naturalWidth>0,await albumArt.elementHandle());
  const trackArt=albumRows.locator('button').first().locator('.art img');await trackArt.waitFor();
  await page.waitForFunction(()=>{const node=document.querySelector('[aria-label="Album tracks"] button .art img');return node?.complete&&node.naturalWidth>0;});
  const artBox=await albumView.locator('.cover').boundingBox(),nameBox=await albumView.getByRole('heading',{name:'Blue',exact:true}).boundingBox();assert(nameBox.x>=artBox.x+artBox.width);
  const albumBack=page.getByRole('button',{name:'Back to Albums',exact:true});await page.mouse.move(1300,800);await page.waitForTimeout(220);assert(Math.abs((await albumBack.boundingBox()).width-40)<1);const albumHover=albumBack.evaluate(node=>new Promise(resolve=>{
   const frames=[],start=performance.now();const sample=()=>{const elapsed=performance.now()-start;frames.push({elapsed,width:node.getBoundingClientRect().width});if(elapsed<360)requestAnimationFrame(sample);else resolve(frames);};requestAnimationFrame(sample);
  }));await albumBack.hover();report.timings.albumHoverFrames=await albumHover;
  assert(report.timings.albumHoverFrames.some(frame=>frame.width>40&&frame.width<56));assert(Math.abs((await albumBack.boundingBox()).width-56)<1);
  await page.evaluate(()=>document.documentElement.dataset.motion='reduced');assert(await albumBack.evaluate(node=>parseFloat(getComputedStyle(node).transitionDuration)<.001));await page.evaluate(()=>delete document.documentElement.dataset.motion);
  await page.screenshot({path:path.join(config.output,'music-album-blue.png')});
  check('Albums keeps same-name locations/roots separate; one album view combines numbered discs with cover/name, track order, formats and durations, without file paths');

  await albumRows.locator('button').first().click();await waitTrack('Blue/Disc 1/01 - First.flac');
  assert.equal(await player().locator('h2').innerText(),'Opening song');assert.equal(await player().locator('.folder').innerText(),'Blue');
  await page.getByRole('button',{name:'Next song',exact:true}).click();await waitTrack('Blue/Disc 1/02 - Later.flac');
  await page.getByRole('button',{name:'Next song',exact:true}).click();await waitTrack('Blue/Disc 2/01 - Finale.flac');
  await browse();assert.equal(await albumRows.locator('[aria-pressed="true"]').getAttribute('data-ost-entry-path'),'Blue/Disc 2/01 - Finale.flac');
  await page.getByRole('button',{name:'Back to Albums',exact:true}).click();await albumTile(blueAlbum).waitFor();
  await page.waitForFunction(id=>document.activeElement?.dataset.ostAlbum===id,blueAlbum.id);
  assert.equal(await albumTile(blueAlbum).evaluate(node=>node===document.activeElement),true);
  await albumTile(redAlbum).click();const redView=page.getByRole('region',{name:'Album Red',exact:true});await redView.getByRole('heading',{name:'Part B',exact:true}).waitFor();
  assert.deepEqual(await redView.locator('h3').allTextContents(),['Part A','Part B']);assert.equal(await audio.evaluate(node=>node.paused),false);
  assert.equal(await audio.elementHandle().then(handle=>handle.evaluate((node,old)=>node===old,albumAudio)),true);
  check('album playback crosses disc boundaries with exact identities and one audio node; Back restores tile focus and browsing another album leaves the queue playing');

  await page.setViewportSize({width:360,height:440});const albumGrip=page.locator('[data-sidebar-owner="ost"] [data-module-sidebar-grip]');if(await albumGrip.getAttribute('aria-expanded')==='true'){await albumGrip.click();await page.waitForTimeout(350);}
  await redView.scrollIntoViewIfNeeded();assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  assert.equal((await musicSearch.boundingBox()).height,34);await page.screenshot({path:path.join(config.output,'music-album-360.png')});
  await page.getByRole('button',{name:'Back to Albums',exact:true}).click();await detailsReady();assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:path.join(config.output,'music-albums-360.png')});
  const longAlbum=albumList.find(item=>item.source_id===albumOwner.source_id&&item.anchor.startsWith('Long/'));await albumTile(longAlbum).click();await albumRows.locator('.name').waitFor();
  assert.equal(await albumRows.locator('.name').evaluate(node=>getComputedStyle(node).webkitLineClamp),'2');assert((await albumRows.locator('button').getAttribute('title')).includes('曲 한글'));assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:path.join(config.output,'music-album-long-360.png')});
  await page.getByRole('button',{name:'Back to Albums',exact:true}).click();await detailsReady();
  await page.setViewportSize({width:1440,height:1000});
  const untaggedAlbum=(await api('/api/ost/albums')).data.items.find(item=>item.name==='Untagged');await albumTile(untaggedAlbum).click();await page.waitForFunction(()=>document.querySelectorAll('[aria-label="Album tracks"] button').length===3);
  assert.deepEqual(await albumRows.locator('.name').allTextContents(),['Track 2','Track 10','曲']);
  check('untagged conventional discs retain natural ordering; arbitrary tagged folder names label disc sections; album tiles/table and search fit narrow short windows');

  await openSettings();assert.equal((await api('/api/suite/folders/apply','POST',{folders:[{source_id:albumOwner.source_id,display_name:'Album library',role:'ost',visible:false}]})).status,200);await closeSettings();
  await page.locator('.audio-player').waitFor({state:'detached'});await page.getByText('This album is no longer available.',{exact:true}).waitFor();
  assert.deepEqual(hashes(albumLocal),albumOriginals);
  await page.reload();await chooseModule(false);await allTracks.waitFor();await detailsReady();assert.equal(await allTracks.locator('li').count(),1);
  assert.equal(await trackRow(albumMirror.source_id,'Blue/Disc 1/01 - First.flac').locator('.name').innerText(),'Opening song');
  check('source loss closes its album/releases audio while another root remains readable; reload retains derived details and every generated original hash');

  assert.equal((await api('/api/suite/folders/apply','POST',{folders:[{source_id:owner.source_id,display_name:'Soundtracks',role:'ost',visible:true},{source_id:albumMirror.source_id,display_name:'Album mirror',role:'ost',visible:false}]})).status,200);
  await page.reload();await chooseModule(false);await detailsReady();await sectionTo('Albums');await detailsReady();
  const largeAlbum=(await api('/api/ost/albums')).data.items.find(item=>item.name==='Large library');
  // A cold read must paint rows before delayed tag reads finish.
  assert.equal((await api('/api/files/sources/'+owner.source_id+'/scan','POST')).status,200);
  let releaseAlbumMetadata;const albumMetadataGate=new Promise(resolve=>releaseAlbumMetadata=resolve);
  const holdAlbumMetadata=async route=>{const url=new URL(route.request().url());if(url.searchParams.get('source_id')===owner.source_id&&url.searchParams.get('path')?.startsWith('Large library/'))await albumMetadataGate;return route.continue().catch(()=>{});};
  await page.route('**/api/ost/metadata?*',holdAlbumMetadata);
  try{
    await albumTile(largeAlbum).click();
    await page.waitForFunction(()=>document.querySelectorAll('[aria-label="Album tracks"] button').length===120&&document.querySelector('.ost-surface main')?.getAttribute('aria-busy')==='false');
    assert.equal(await page.getByText('Reading album…',{exact:true}).count(),0);
    await albumRows.locator('button').first().click();await waitTrack('Large library/Track 1.wav');await browse();
    assert.equal(await albumRows.locator('button').count(),120);
    assert.equal(await page.locator('.metadata-status').count(),0);
    check('Cached album rows appear and play without navigation tag requests; returning to the list retains the loaded page');
  }finally{releaseAlbumMetadata();await page.unroute('**/api/ost/metadata?*',holdAlbumMetadata);}

  const albumBoundary=albumRows.locator('[data-ost-entry-path="Large library/Track 120.wav"]');await albumBoundary.click();await waitTrack('Large library/Track 120.wav');const largeAudio=await audio.elementHandle();
  await page.getByRole('button',{name:'Next song',exact:true}).click();await waitTrack('Large library/Track 121.wav');await browse();assert.equal(await albumRows.locator('button').count(),120);
  await page.getByRole('button',{name:'Load more',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('[aria-label="Album tracks"] button').length===240);
  assert.equal(await albumRows.locator('[aria-pressed="true"]').getAttribute('data-ost-entry-path'),'Large library/Track 121.wav');
  await albumRows.locator('button').first().click();await waitTrack('Large library/Track 1.wav');await cycleTo('all');await page.getByRole('button',{name:'Previous song',exact:true}).click();await waitTrack('Large library/Track 260.wav');
  assert.equal(await audio.elementHandle().then(handle=>handle.evaluate((node,old)=>node===old,largeAudio)),true);await browse();await page.getByRole('button',{name:'Load more',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('[aria-label="Album tracks"] button').length===260);
  assert.equal((await context.request.get(config.url+'/api/ost/albums?limit=241')).status(),422);
  assert.equal((await context.request.get(config.url+'/api/ost/tracks?q='+encodeURIComponent('x'.repeat(513)))).status(),422);
  assert.equal((await context.request.get(config.url+'/api/ost/album-tracks?'+new URLSearchParams({source_id:owner.source_id,anchor:'../outside.wav'}))).status(),400);
  const guardedAlbumResponse=await context.request.get(config.url+'/api/ost/albums');assert.equal(guardedAlbumResponse.headers()['cache-control'],'no-store');
  check('260-track album paginates 120/240/260; Next crosses an unloaded page, Previous wraps to the true last track, browse counts stay separate, API bounds and guards remain');

  const passiveLocal=path.join(path.dirname(config.mediaLocal||config.media),'metadata-samples');
  const passiveFixture=JSON.parse(fs.readFileSync(path.join(passiveLocal,'fixture-info.json'),'utf8'));
  const passiveOriginals=hashes(passiveLocal);
  await page.getByRole('button',{name:'Open Keivotos menu',exact:true}).click();await page.locator('.app-drawer').getByRole('button',{name:'Files',exact:true}).click();
  async function savedInfo(relative,sourceName='Saved metadata',tab='origin'){
   await page.locator('aside button').filter({has:page.getByText(sourceName,{exact:true})}).first().click();
   const parts=relative.split('/');for(let i=0;i<parts.length-1;i++)await page.locator(`[data-files-entry-path=${JSON.stringify(parts.slice(0,i+1).join('/'))}]`).click();
   await info(relative);
   if(tab==='origin')await information.getByRole('button',{name:'Origin',exact:true}).click();
  }
  const creator=information.getByPlaceholder('Creator name');
  const description=information.getByPlaceholder('Where is this from? What is it?');
  const titleField=information.getByRole('textbox',{name:'Title',exact:true});
  const sourceUrl='https://www.youtube.com/watch?v=abcdefghijk';
  const noteParams=new URLSearchParams({source_id:passiveFixture.source_id,path:passiveFixture.youtube[1]});
  const noteBefore=(await api('/api/files/info?'+noteParams)).data;
  let originWrites=0;const countWrites=request=>{if(request.url().includes('/api/files/info')&&request.method()==='PUT')originWrites++;};page.on('request',countWrites);
  await savedInfo(passiveFixture.youtube[1]);await creator.waitFor();
  assert.equal(await creator.inputValue(),'Captured channel');
  assert.equal(await titleField.inputValue(),'Saved song 한글 <script>window.bad=1</script>');
  assert.equal(await information.getByPlaceholder('https://…').first().inputValue(),sourceUrl);
  assert.equal(await information.locator('section').count(),0);assert.equal(await information.locator('script,img').count(),0);
  assert.equal(await page.evaluate(()=>window.bad),undefined);
  await creator.focus();await titleField.focus();await page.waitForTimeout(100);assert.equal(originWrites,0);
  await page.screenshot({path:path.join(config.output,'files-youtube-origin.png')});
  await information.getByRole('button',{name:'Properties',exact:true}).click();
  assert.equal(await information.locator('dl').count(),1);assert.equal(await information.getByText('2:05',{exact:true}).count(),1);
  assert.equal(await information.locator('a').count(),0);assert.equal(await creator.count(),0);
  assert.equal(await information.getByText('Saved file',{exact:true}).count(),0);assert.equal(await information.getByText('File format',{exact:true}).count(),0);
  const savedPathValue=(await information.getByTitle('Click to copy').innerText()).replaceAll('\\','/');assert(savedPathValue.endsWith(passiveFixture.youtube[1]),savedPathValue);assert(!savedPathValue.includes('D:'));
  const passiveResponse=await context.request.get(config.url+'/api/files/module-metadata?'+noteParams);assert.equal(passiveResponse.headers()['cache-control'],'no-store');
  await page.screenshot({path:path.join(config.output,'files-youtube-properties.png')});
  await information.getByRole('button',{name:'Close',exact:true}).click();assert.deepEqual((await api('/api/files/info?'+noteParams)).data,noteBefore);assert.equal(originWrites,0);
  check('YouTube auto-fills native Author/Links/Details; opening and focus/blur perform no writes; Properties uses one unique facts list and the current path');

  await savedInfo(passiveFixture.youtube[0]);await titleField.waitFor();assert.equal(await creator.count(),0);
  assert.equal(await information.getByRole('textbox',{name:'Downloaded',exact:true}).count(),0);
  await information.getByRole('button',{name:'Close',exact:true}).click();await savedInfo('ordinary.webm');
  await page.waitForFunction(()=>!document.querySelector('[role="dialog"]')?.textContent.includes('Reading metadata'));
  assert.equal(await information.locator('textarea,input:not([type=file])').count(),0);
  await information.getByRole('button',{name:/Add info/}).click();await information.getByRole('button',{name:'Details',exact:true}).click();
  await information.getByRole('textbox',{name:'Detail name',exact:true}).fill('Edition');await information.getByRole('textbox',{name:'Edition',exact:true}).fill('Personal copy');
  await information.getByRole('button',{name:'Properties',exact:true}).click();await page.waitForFunction(async({source})=>(await(await fetch('/api/files/info?'+new URLSearchParams({source_id:source,path:'ordinary.webm'}))).json())?.details?.[0]?.value==='Personal copy',{source:passiveFixture.source_id});
  await information.getByRole('button',{name:'Close',exact:true}).click();await savedInfo('ordinary.webm');assert.equal(await information.getByRole('textbox',{name:'Edition',exact:true}).inputValue(),'Personal copy');
  await information.getByRole('button',{name:'Remove all details',exact:true}).click();await information.getByRole('button',{name:'Close',exact:true}).click();
  check('legacy unknown facts stay absent, ordinary files gain no inferred identity, and the single new Details option edits/saves/removes ordinary named values');

  for(const [index,relative] of passiveFixture.twitter.entries()){
   await savedInfo(relative);await creator.waitFor();assert.equal(await creator.inputValue(),'Saved author 이름 @SavedAuthor');
   assert.equal(await description.inputValue(),'  Preserved 한글\n<img src="https://example.invalid/never">  ');
   assert.equal(await information.locator('img,script').count(),0);assert.equal(await information.getByRole('textbox',{name:'Posted',exact:true}).count(),1);
   assert.equal(await information.getByPlaceholder('https://…').count(),2);
   if(index===2)await page.screenshot({path:path.join(config.output,'files-twitter-origin.png')});
   await information.getByRole('button',{name:'Close',exact:true}).click();
  }
  assert.equal((await api('/api/youtube/library')).status,409);assert.equal((await api('/api/twitter/library')).status,409);
  check('disabled owners still fill native Origin controls offline for video/photo/text-only Twitter captures with exact inert text, author, dates and source/profile links');

  let releaseSaved;const savedStarted=new Promise(resolve=>releaseSaved=resolve);
  const delayedSaved=async route=>{if(new URL(route.request().url()).searchParams.get('path')===passiveFixture.youtube[0]){releaseSaved();await new Promise(resolve=>setTimeout(resolve,450));}return route.continue().catch(()=>{});};
  await page.route('**/api/files/module-metadata?**',delayedSaved);await savedInfo(passiveFixture.youtube[0]);await savedStarted;await information.getByRole('button',{name:'Close',exact:true}).click();await savedInfo('ordinary.webm');
  await page.waitForTimeout(600);assert.equal(await creator.count(),0);await page.unroute('**/api/files/module-metadata?**',delayedSaved);await information.getByRole('button',{name:'Close',exact:true}).click();
  let savedFailure=true;expectedFailures++;const failSaved=route=>{if(savedFailure){savedFailure=false;return route.fulfill({status:503,json:{detail:'Generated saved metadata failure'}});}return route.continue();};
  await page.route('**/api/files/module-metadata?**',failSaved);await savedInfo(passiveFixture.youtube[1]);await information.getByText('Generated saved metadata failure',{exact:true}).waitFor();
  await information.getByRole('button',{name:'Retry metadata',exact:true}).click();await creator.waitFor();assert.equal(await creator.inputValue(),'Captured channel');await page.unroute('**/api/files/module-metadata?**',failSaved);
  await information.getByRole('button',{name:'Close',exact:true}).click();
  // User edits during a pending read must win over late defaults.
  await page.route('**/api/files/module-metadata?**',delayedSaved);await savedInfo(passiveFixture.youtube[0]);await information.getByRole('button',{name:/Add info/}).click();await information.getByRole('button',{name:'Author',exact:true}).click();
  await creator.fill('Typed while loading');await page.waitForTimeout(600);assert.equal(await creator.inputValue(),'Typed while loading');await page.unroute('**/api/files/module-metadata?**',delayedSaved);
  const editedSaved=page.waitForResponse(response=>response.request().method()==='PUT'&&new URL(response.url()).pathname==='/api/files/info'&&response.request().postDataJSON()?.path===passiveFixture.youtube[0]);
  await information.getByRole('button',{name:'Properties',exact:true}).click();await (await editedSaved).finished();
  await information.getByRole('button',{name:'Close',exact:true}).click();
  check('stale reads cannot populate another subject, failed metadata retries, and late defaults cannot overwrite typing in the existing editor');

  const factsBefore=(await passiveResponse.json()).sections;
  await api('/api/files/info','PUT',{source_id:passiveFixture.source_id,path:passiveFixture.youtube[1],description:'Personal note',author:'Manual author',extra_info:'Keep this',details:[{label:'Title',value:'Manual title'}],links:[{url:sourceUrl,label:'Existing source',kind:'discussion'}]});
  await savedInfo(passiveFixture.youtube[1]);await titleField.waitFor();
  await page.waitForFunction(()=>!document.querySelector('[role="dialog"]')?.textContent.includes('Reading metadata'));
  assert.equal(await creator.inputValue(),'Manual author');assert.equal(await description.inputValue(),'Personal note');assert.equal(await titleField.inputValue(),'Manual title');assert.equal(await information.getByPlaceholder('Anything else worth noting…').inputValue(),'Keep this');
  assert.equal(await information.getByPlaceholder('https://…').evaluateAll(nodes=>nodes.filter(node=>node.value==='https://www.youtube.com/watch?v=abcdefghijk').length),1);assert.equal(await information.getByPlaceholder('Label (optional)').first().inputValue(),'Existing source');
  await information.getByRole('button',{name:'Remove author',exact:true}).click();await page.waitForFunction(async({source,path})=>(await(await fetch('/api/files/info?'+new URLSearchParams({source_id:source,path}))).json())?.metadata_applied===true,{source:passiveFixture.source_id,path:passiveFixture.youtube[1]});
  await information.getByRole('button',{name:'Close',exact:true}).click();await savedInfo(passiveFixture.youtube[1]);await titleField.waitFor();assert.equal(await creator.count(),0);assert.equal(await titleField.inputValue(),'Manual title');
  await information.getByRole('button',{name:'Remove description',exact:true}).click();await information.getByRole('button',{name:'Remove all links',exact:true}).click();await information.getByRole('button',{name:'Remove extra info',exact:true}).click();await information.getByRole('button',{name:'Remove all details',exact:true}).click();
  await page.waitForFunction(async({source,path})=>{const note=await(await fetch('/api/files/info?'+new URLSearchParams({source_id:source,path}))).json();return note?.metadata_applied&&!note.description&&!note.author&&!note.extra_info&&!note.links.length&&!note.details.length;},{source:passiveFixture.source_id,path:passiveFixture.youtube[1]});
  await information.getByRole('button',{name:'Close',exact:true}).click();await savedInfo(passiveFixture.youtube[1]);await page.waitForTimeout(200);assert.equal(await information.locator('textarea,input:not([type=file])').count(),0);await information.getByRole('button',{name:'Close',exact:true}).click();
  assert.deepEqual((await api('/api/files/module-metadata?'+noteParams)).data.sections,factsBefore);
  check('manual fields and link labels/kinds win; missing fields fill once, source URLs deduplicate, edits/removals including a fully cleared note persist without changing captured facts');

  assert.equal((await api('/api/suite/modules/ost/disable','POST')).status,200);await page.reload();await page.getByRole('button',{name:'Open Keivotos menu',exact:true}).click();await page.locator('.app-drawer').getByRole('button',{name:'Files',exact:true}).click();
  await savedInfo(metadataPath,'Soundtracks','properties');await information.getByText('0:04',{exact:true}).waitFor();assert.equal(await information.locator('dl').count(),1);
  for(const label of ['File format','MIME type','Lossless','Saved file'])assert.equal(await information.getByText(label,{exact:true}).count(),0);
  await information.getByRole('button',{name:'Origin',exact:true}).click();await titleField.waitFor();assert.equal(await creator.inputValue(),'Artist one; 歌手 二');
  await page.waitForFunction(()=>{const image=document.querySelector('[role="dialog"] img[alt="Local music cover"]');return image?.complete&&image.naturalWidth>0;});
  await page.screenshot({path:path.join(config.output,'files-music-origin.png')});await information.getByRole('button',{name:'Close',exact:true}).click();
  check('Music-disabled Files auto-fills editable Author/Details/Extra info and the existing Images/videos strip; one Properties list contains unique audio facts');

  await savedInfo(passiveFixture.twitter[2]);await creator.waitFor();await page.setViewportSize({width:360,height:440});await information.getByRole('textbox',{name:'Captured',exact:true}).scrollIntoViewIfNeeded();
  const passiveBox=await information.boundingBox();assert(passiveBox.x>=0&&passiveBox.x+passiveBox.width<=360);assert(await information.locator('input,textarea').evaluateAll(nodes=>nodes.every(node=>node.getBoundingClientRect().right<=360)));
  await page.screenshot({path:path.join(config.output,'files-origin-360.png')});await information.getByRole('button',{name:'Close',exact:true}).click();await page.setViewportSize({width:1440,height:1000});
  assert.deepEqual(hashes(passiveLocal),passiveOriginals);assert.equal(crypto.createHash('sha256').update(fs.readFileSync(path.join(config.mediaLocal||config.media,metadataPath))).digest('hex'),originalHash);page.off('request',countWrites);
  check('the ordinary editor fits a short/narrow modal with persistent footer and scrolling; original captures/media/tags remain byte-identical');
  // Refresh is an explicit, scoped operation over generated fixture files.
  assert.equal((await api('/api/suite/modules/ost/enable','POST')).status,200);
  const fixtureRoot=path.resolve(config.mediaLocal||config.media);
  assert.match(fixtureRoot,/[/\\]keivotos-modularization-[^/\\]+[/\\]media$/);
  const unchanged=hashes(fixtureRoot),refreshFolder=path.join(fixtureRoot,'Refresh target');fs.mkdirSync(refreshFolder);
  const freshTrack=path.join(refreshFolder,'Fresh.wav');fs.copyFileSync(path.join(fixtureRoot,'Root track.wav'),freshTrack);
  await chooseModule();
  await root(owner.source_id).click({button:'right'});
  let refreshMenu=page.getByRole('menu',{name:'Location for Soundtracks',exact:true});
  assert.deepEqual(await refreshMenu.getByRole('menuitem').allTextContents(),['Show in Files','Open in Explorer','Refresh metadata']);
  await page.waitForFunction(()=>!document.querySelector('[role="menuitem"]')?.disabled);
  const scanUrl='**/api/ost/metadata-refresh';
  const failRefresh=route=>route.fulfill({status:409,json:{detail:'Generated folder-work conflict. Try again.'}});
  await page.route(scanUrl,failRefresh);await refreshMenu.getByRole('menuitem',{name:'Refresh metadata',exact:true}).click();
  await page.getByText('Generated folder-work conflict. Try again.',{exact:true}).waitFor();
  await page.unroute(scanUrl,failRefresh);assert.equal(await root(owner.source_id).evaluate(node=>node===document.activeElement),true);
  check('Folder Refresh metadata uses the existing guarded menu; a failed rescan keeps files/focus and permits retry');
  async function refreshFrom(button,label){
    await button.click({button:'right'});const menu=page.getByRole('menu',{name:'Location for '+label,exact:true});
    await page.waitForFunction(()=>!document.querySelector('[role="menuitem"]')?.disabled);
    const accepted=page.waitForResponse(response=>response.request().method()==='POST'&&new URL(response.url()).pathname==='/api/ost/metadata-refresh');
    await menu.getByRole('menuitem',{name:'Refresh metadata',exact:true}).click();
    const job=await (await accepted).json();
    let done;const deadline=Date.now()+90000;
    while(Date.now()<deadline){
      done=(await api('/api/ost/metadata-jobs')).data.items.find(row=>row.id===job.id);
      if(done&&['completed','completed-with-warnings'].includes(done.state))break;
      await page.waitForTimeout(150);
    }
    assert(done&&['completed','completed-with-warnings'].includes(done.state),'Metadata job did not complete');
    if(!done.failed)await page.getByRole('status').filter({hasText:`Metadata refreshed for ${done.processed} tracks.`}).last().waitFor({timeout:90000});
  }
  await refreshFrom(root(owner.source_id),'Soundtracks');
  let refreshed=(await api('/api/ost/metadata?'+new URLSearchParams({source_id:owner.source_id,path:'Refresh target/Fresh.wav'}))).data;
  assert.equal(refreshed.duration,4);await root(owner.source_id).click();await row('Refresh target').waitFor();
  const shorter=Buffer.from(fs.readFileSync(freshTrack).subarray(0,44+32000));shorter.writeUInt32LE(shorter.length-8,4);shorter.writeUInt32LE(shorter.length-44,40);fs.writeFileSync(freshTrack,shorter);
  await refreshFrom(row('Refresh target'),'Refresh target');
  refreshed=(await api('/api/ost/metadata?'+new URLSearchParams({source_id:owner.source_id,path:'Refresh target/Fresh.wav'}))).data;
  assert.equal(refreshed.duration,2);assert.equal(fs.readFileSync(freshTrack).equals(shorter),true);
  const afterRefresh=hashes(fixtureRoot);for(const [name,digest] of Object.entries(unchanged))assert.equal(afterRefresh[name],digest,name);
  check('Root refresh discovers new audio; nested refresh updates changed duration, keeps every pre-existing original exact and writes no tags');

  // Keep actual loaded lists while navigating; delay only the ordinary API read.
  await sectionTo('Library');await page.waitForFunction(()=>document.querySelector('.ost-surface main')?.getAttribute('aria-busy')==='false');
  const retainedCount=await allTracks.locator('li').count();assert(retainedCount>0);
  await page.locator('.ost-surface main').evaluate(node=>node.scrollTop=300);
  const retainedScroll=await page.locator('.ost-surface main').evaluate(node=>node.scrollTop);
  let metadataNavigationCalls=0;
  const countMetadata=()=>metadataNavigationCalls++;page.on('request',request=>{if(new URL(request.url()).pathname==='/api/ost/metadata')countMetadata();});
  const slowRows=async route=>{await new Promise(resolve=>setTimeout(resolve,1100));return route.continue().catch(()=>{});};
  await page.route('**/api/ost/tracks?**',slowRows);
  await page.getByRole('button',{name:'Open Keivotos menu',exact:true}).click();await page.locator('.app-drawer').getByRole('button',{name:'Files',exact:true}).click();
  await chooseModule(false);assert.equal(await allTracks.locator('li').count(),retainedCount);
  assert.equal(await page.getByText('Loading Music tracks…',{exact:true}).count(),0);
  await page.waitForTimeout(150);assert.equal(await allTracks.locator('li').count(),retainedCount);
  assert(Math.abs(await page.locator('.ost-surface main').evaluate(node=>node.scrollTop)-retainedScroll)<3);
  await page.waitForTimeout(1200);assert.equal(await allTracks.locator('li').count(),retainedCount);
  await page.unroute('**/api/ost/tracks?**',slowRows);
  await sectionTo('Albums');await sectionTo('Library');assert.equal(await allTracks.locator('li').count(),retainedCount);
  assert.equal(metadataNavigationCalls,0);
  check('Module/section reentry retains loaded Music rows and scroll while delayed reconciliation runs; ordinary navigation makes zero tag requests');

  // Timed shared-notification rendering uses controlled operation status. Actual
  // Files scans/reader counts/interruption run in isolated backend integration.
  await foldersSection();if(await page.getByRole('button',{name:'Back to Music',exact:true}).count())await page.getByRole('button',{name:'Back to Music',exact:true}).click();
  let progressJob={id:'generated-music-notice',source_id:owner.source_id,label:'Soundtracks',state:'working',progress:78,processed:39,total:50,failed:0,discover:true,created_at:new Date().toISOString()};
  const acceptProgress=route=>route.fulfill({json:progressJob});
  let progressVisible=true,firstProgressPoll=true,releaseStalePoll,stalePollStarted,progressPolls=0,progressBlocked=false;
  const stalePollGate=new Promise(resolve=>releaseStalePoll=resolve),stalePollSeen=new Promise(resolve=>stalePollStarted=resolve);
  const progressStatus=async route=>{
    progressPolls++;
    if(progressBlocked)return route.fulfill({status:409,json:{detail:'Enable Music before using its operations'}});
    if(firstProgressPoll){firstProgressPoll=false;stalePollStarted();await stalePollGate;return route.fulfill({json:{items:[]}}).catch(()=>{});}
    return route.fulfill({json:{items:progressVisible?[progressJob]:[]}});
  };
  await page.route('**/api/ost/metadata-refresh',acceptProgress);await page.route('**/api/ost/metadata-jobs',progressStatus);
  // Enable-time discovery supplies the stale read; idle polling is deliberately absent.
  await page.reload();await chooseModule();await stalePollSeen;
  await root(owner.source_id).click({button:'right'});await page.getByRole('menu',{name:'Location for Soundtracks',exact:true}).getByRole('menuitem',{name:'Refresh metadata',exact:true}).click();
  const progressNotice=page.locator('.action-notification').filter({hasText:'Refreshing metadata… 39/50 tracks · 78%'});await progressNotice.waitFor();
  releaseStalePoll();await page.waitForTimeout(800);assert.equal(await page.getByText('Metadata refresh stopped.',{exact:true}).count(),0);assert(await progressNotice.isVisible());
  assert.equal(await page.locator('.metadata-status').count(),0);assert.equal(await progressNotice.locator('.notification-progress span').evaluate(node=>node.style.width),'78%');
  assert.equal(await progressNotice.getByRole('button',{name:'Downloads',exact:true}).count(),0);
  await page.getByRole('button',{name:'Open Keivotos menu',exact:true}).click();await page.locator('.app-drawer').getByRole('button',{name:'Files',exact:true}).click();
  await page.waitForTimeout(6400);assert(await progressNotice.isVisible());
  progressJob={...progressJob,processed:48,progress:96};
  const laterNotice=page.locator('.action-notification').filter({hasText:'Refreshing metadata… 48/50 tracks · 96%'});await laterNotice.waitFor();
  await page.setViewportSize({width:360,height:440});const noticeBox=await laterNotice.boundingBox();assert(noticeBox.x>=0&&noticeBox.x+noticeBox.width<=360);
  await page.screenshot({path:path.join(config.output,'music-metadata-progress-360.png')});await page.setViewportSize({width:1440,height:1000});
  progressJob={...progressJob,processed:50,progress:100,state:'completed'};
  await page.getByRole('status').filter({hasText:'Metadata refreshed for 50 tracks.'}).waitFor();
  const finishedPolls=progressPolls;await page.waitForTimeout(1400);assert.equal(progressPolls,finishedPolls);
  check('Completed Music metadata stops all progress requests while idle, including outside Music');
  await chooseModule();
  progressJob={...progressJob,id:'generated-removed-notice',state:'working',processed:2,progress:4};
  await root(owner.source_id).click({button:'right'});await page.getByRole('menu',{name:'Location for Soundtracks',exact:true}).getByRole('menuitem',{name:'Refresh metadata',exact:true}).click();
  await page.getByRole('status').filter({hasText:'Refreshing metadata… 2/50 tracks · 4%'}).waitFor();
  progressVisible=false;await page.getByRole('status').filter({hasText:'Metadata refresh stopped.'}).waitFor();
  progressVisible=true;progressJob={...progressJob,id:'generated-dismissed-notice',processed:39,total:47,progress:83};
  await root(owner.source_id).click({button:'right'});await page.getByRole('menu',{name:'Location for Soundtracks',exact:true}).getByRole('menuitem',{name:'Refresh metadata',exact:true}).click();
  const dismissMusic=page.locator('.action-notification').filter({hasText:'Refreshing metadata… 39/47 tracks · 83%'});await dismissMusic.waitFor();
  await dismissMusic.getByRole('button',{name:'Dismiss notification',exact:true}).click();
  progressJob={...progressJob,state:'completed',processed:47,progress:100};await page.waitForTimeout(1000);
  assert.equal(await page.getByText('Metadata refreshed for 47 tracks.',{exact:true}).count(),0);
  // Each successful rescan client wakes the idle observer without a Music visit.
  async function actualMetadataIdle(){
    const deadline=Date.now()+90000;
    while(Date.now()<deadline){
      // APIRequestContext bypasses the controlled page status responses.
      const response=await context.request.get(config.url+'/api/ost/metadata-jobs');assert.equal(response.status(),200);
      const data=await response.json();if(data.items.every(job=>!['requested','started','working'].includes(job.state)))return;
      await page.waitForTimeout(200);
    }
    assert.fail('Actual rescan worker did not become idle');
  }
  await root(owner.source_id).click({button:'right'});await page.getByRole('menuitem',{name:'Show in Files',exact:true}).click();
  await page.getByRole('button',{name:'Rescan',exact:true}).waitFor();
  progressJob={...progressJob,id:'generated-files-scan',discover:false,state:'working',processed:2,total:50,progress:4};
  await page.getByRole('button',{name:'Rescan',exact:true}).click();
  await page.getByRole('status').filter({hasText:'Reading music details… 2/50 tracks · 4%'}).waitFor();
  progressJob={...progressJob,state:'completed',processed:50,progress:100};
  await page.locator('[data-notification-id="ost-metadata-generated-files-scan"]').filter({hasText:'Metadata refreshed for 50 tracks.'}).waitFor();
  const afterFiles=progressPolls;await page.waitForTimeout(1100);assert.equal(progressPolls,afterFiles);
  await actualMetadataIdle();
  await openSettings();await page.getByRole('button',{name:'Folders',exact:true}).click();
  const settingsFolder=page.locator('#setting-folder-roles .space-y-2 > div').filter({has:page.getByTitle(owner.path,{exact:true})});
  progressJob={...progressJob,id:'generated-suite-scan',state:'working',processed:4,progress:8};
  const suiteScan=page.waitForResponse(response=>response.request().method()==='POST'&&new URL(response.url()).pathname===`/api/suite/folders/${owner.source_id}/rescan`);
  await settingsFolder.getByRole('button',{name:'Re-scan',exact:true}).click();assert.equal((await suiteScan).status(),200);
  await page.getByRole('status').filter({hasText:'Reading music details… 4/50 tracks · 8%'}).waitFor();
  progressJob={...progressJob,state:'completed',processed:50,progress:100};
  await page.locator('[data-notification-id="ost-metadata-generated-suite-scan"]').filter({hasText:'Metadata refreshed for 50 tracks.'}).waitFor();
  await actualMetadataIdle();await closeSettings();const afterSuite=progressPolls;await page.waitForTimeout(1100);assert.equal(progressPolls,afterSuite);
  check('Real Files Rescan and Folders Re-scan wake idle Music notifications, finish outside Music and become silent again');
  await chooseModule();
  progressJob={...progressJob,id:'generated-conflict',discover:true,state:'working',processed:1,progress:2};
  await root(owner.source_id).click({button:'right'});await page.getByRole('menuitem',{name:'Refresh metadata',exact:true}).click();
  await page.getByRole('status').filter({hasText:'Refreshing metadata… 1/50 tracks · 2%'}).waitFor();
  // Module transitions correctly refuse while cover/library responses hold leases.
  const readsDeadline=Date.now()+90000;while(musicReads.size&&Date.now()<readsDeadline)await page.waitForTimeout(50);
  assert.equal(musicReads.size,0);await page.waitForTimeout(100);
  assert.equal((await api('/api/suite/modules/ost/disable','POST')).status,200);progressBlocked=true;
  await page.locator('.ost-surface').waitFor({state:'detached'});
  const blockedPolls=progressPolls;await page.waitForTimeout(1600);assert.equal(progressPolls,blockedPolls);
  assert.equal(await page.getByText('Refreshing metadata… 1/50 tracks · 2%',{exact:true}).count(),0);
  check('A real Music disable discovered through HTTP 409 reconciles the stale browser registry once and stops polling');
  await page.unroute('**/api/ost/metadata-refresh',acceptProgress);await page.unroute('**/api/ost/metadata-jobs',progressStatus);
  assert.equal((await api('/api/suite/modules/ost/enable','POST')).status,200);await page.reload();await chooseModule();
  check('A removed metadata operation releases its retained notice; dismissing progress never resurrects it on completion');
  check('Metadata progress uses one shared notification/bar with real counts and percentages, survives 6 seconds and module exit, fits narrow windows and completes outside the search header');

  await sectionTo('Library');await trackRow(owner.source_id,'Album 2/Track 2.wav').click();await waitTrack('Album 2/Track 2.wav');
  await audio.evaluate(async node=>{window.__metadataPlayingAudio=node;node.currentTime=1;await node.play();});
  const playingSource=await audio.getAttribute('src');
  assert.equal((await api('/api/ost/metadata-refresh','POST',{source_id:owner.source_id,parent:'Album 2'})).status,200);
  await detailsReady();await page.waitForTimeout(900);
  assert(await audio.evaluate(node=>node===window.__metadataPlayingAudio));assert.equal(await audio.getAttribute('src'),playingSource);
  assert.equal(await audio.evaluate(node=>node.paused),false);assert(await audio.evaluate(node=>node.currentTime)>1.5);
  await paused();await browse();await chooseModule();
  check('A real selected-folder metadata job updates cached details without replacing, pausing or rewinding the active audio element');

  const shuffle=page.getByRole('button',{name:'Shuffle',exact:true}),previousSong=page.getByRole('button',{name:'Previous song',exact:true}),nextSong=page.getByRole('button',{name:'Next song',exact:true});
  const shuffleOriginals=hashes(fixtureRoot),shuffleAlbums=hashes(albumLocal);
  const currentSong=()=>audio.evaluate(node=>new URL(node.src).searchParams.get('path'));
  const sameShuffleAudio=async()=>assert(await audio.evaluate(node=>node===window.__shuffleAudio));
  async function awaitShuffleTrack(){await page.waitForFunction(()=>{const n=document.querySelector('audio');return n&&n.readyState>=2&&!n.paused;});await paused();}
  async function shuffleHint(text,hover=false){
   await player().focus();await page.mouse.move(800,250);const hint=page.locator('#music-shuffle-tooltip');await hint.waitFor({state:'hidden'});
   if(hover){await shuffle.locator('..').hover();await page.waitForTimeout(80);assert.equal(await hint.isVisible(),false);}else await shuffle.focus();
   await hint.waitFor({state:'visible'});assert.equal(await hint.innerText(),text);const b=await hint.boundingBox();assert(b.x>=0&&b.y>=0&&b.x+b.width<=page.viewportSize().width);
  }
  if(await root(owner.source_id).count())await root(owner.source_id).click();await row('Album 2').waitFor();
  await openFolder('Album 2');await startTrack('Album 2/Track 2.wav');await cycleTo('off');if(await auto().getAttribute('aria-checked')!=='true')await auto().click();
  assert.equal(await shuffle.getAttribute('aria-pressed'),'false');
  const shufflePrefs=await page.evaluate(()=>[localStorage.getItem('keivotos:video-volume-percent'),localStorage.getItem('keivotos:youtube-volume-percent'),localStorage.getItem('keivotos:video-playback-speed'),localStorage.getItem('keivotos:youtube-playback-speed')]);
  await audio.evaluate(async node=>{window.__shuffleAudio=node;node.currentTime=2;await node.play();});const beforeToggle=await audio.evaluate(node=>({src:node.src,time:node.currentTime,volume:node.volume}));
  await page.evaluate(()=>{window.__shuffleRng=Math.random;Math.random=()=>0;});
  await shuffle.focus();await page.keyboard.press('Space');assert.equal(await shuffle.getAttribute('aria-pressed'),'true');await sameShuffleAudio();
  assert.equal(await audio.evaluate(node=>node.paused),false);assert.equal(await audio.evaluate(node=>node.src),beforeToggle.src);await page.waitForTimeout(200);assert((await audio.evaluate(node=>node.currentTime))>beforeToggle.time);assert.equal(await audio.evaluate(node=>node.volume),beforeToggle.volume);await paused();
  assert(await previousSong.isDisabled());await cycleTo('all');assert(await previousSong.isDisabled());await cycleTo('off');
  for(const width of [1440,720,360]){
   await page.setViewportSize({width,height:1000});await page.waitForTimeout(330);if(width<=700&&await grip.getAttribute('aria-expanded')==='true'){await grip.click();await page.waitForTimeout(330);}
   await shuffleHint('Turn shuffle off',width===1440);const a=await shuffle.boundingBox(),b=await previousSong.boundingBox(),c=await pause().count()?await pause().boundingBox():await play().boundingBox(),v=await page.locator('.audio-player').boundingBox();
   assert(a.x+a.width<=b.x&&b.x-a.x-a.width<16&&Math.abs(a.y-b.y)<2);assert(Math.abs(c.x+c.width/2-(v.x+v.width/2))<2);assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   await page.screenshot({path:path.join(config.output,`music-shuffle-${width}.png`)});
  }
  await page.setViewportSize({width:1440,height:1000});
  check('Shuffle defaults off, toggles by native Space without restarting/pausing audio, stays left of Previous with centered Play, short hints and desktop/narrow geometry');
  const albumPool=(await api('/api/ost/library?'+new URLSearchParams({source_id:owner.source_id,parent:'Album 2',limit:'240'}))).data.items.filter(item=>!item.is_dir&&item.available).map(item=>item.path);
  const heard=[await currentSong()];while(heard.length<albumPool.length){await nextSong.click();await awaitShuffleTrack();heard.push(await currentSong());await sameShuffleAudio();}
  assert.equal(new Set(heard).size,albumPool.length);assert.deepEqual([...heard].sort(),[...albumPool].sort());assert(await nextSong.isDisabled());
  await finishTrack();await page.waitForFunction(()=>document.querySelector('audio')?.ended);assert.equal(await currentSong(),heard.at(-1));
  for(let at=heard.length-2;at>=0;at--){await previousSong.click();await waitTrack(heard[at]);await paused();}assert(await previousSong.isDisabled());
  for(let at=1;at<heard.length;at++){await nextSong.click();await waitTrack(heard[at]);await paused();}assert(await nextSong.isDisabled());
  check('Shuffle plays each available folder track once, Loop Off exhausts naturally, and Previous/Next retrace the actual heard sequence');
  await page.evaluate(()=>{Math.random=()=>.999999;});
  await cycleTo('all');const roundTail=await currentSong();await nextSong.click();await awaitShuffleTrack();const nextRound=await currentSong();assert.notEqual(nextRound,roundTail);
  await previousSong.click();await waitTrack(roundTail);await paused();await nextSong.click();await waitTrack(nextRound);await paused();
  await finishTrack();await page.waitForFunction(path=>new URL(document.querySelector('audio').src).searchParams.get('path')!==path,nextRound);await awaitShuffleTrack();const naturalShuffle=await currentSong();
  await cycleTo('one');await finishTrack();await page.waitForFunction(()=>{const n=document.querySelector('audio');return n&&!n.paused&&!n.ended&&n.currentTime<1;});assert.equal(await currentSong(),naturalShuffle);await paused();
  await nextSong.click();await awaitShuffleTrack();assert.notEqual(await currentSong(),naturalShuffle);await sameShuffleAudio();
  check('Loop All starts a different shuffled round with real backward/forward history; natural Auto next shuffles while Loop One retains replay priority');
  await page.evaluate(()=>{Math.random=window.__shuffleRng;});
  await cycleTo('off');const heldPath=await currentSong(),heldPosition=await audio.evaluate(node=>node.currentTime);await shuffle.click();assert.equal(await shuffle.getAttribute('aria-pressed'),'false');assert.equal(await currentSong(),heldPath);assert(Math.abs((await audio.evaluate(node=>node.currentTime))-heldPosition)<.1);await sameShuffleAudio();
  const naturalIndex=albumPool.indexOf(heldPath),direction=naturalIndex>0?-1:1;await(direction<0?previousSong:nextSong).click();await waitTrack(albumPool[naturalIndex+direction]);await paused();await shuffle.click();assert(await previousSong.isDisabled());
  assert.deepEqual(await page.evaluate(()=>[localStorage.getItem('keivotos:video-volume-percent'),localStorage.getItem('keivotos:youtube-volume-percent'),localStorage.getItem('keivotos:video-playback-speed'),localStorage.getItem('keivotos:youtube-playback-speed')]),shufflePrefs);
  check('Turning shuffle off restores natural order from the current song without resetting audio; re-enable starts fresh history and preserves other owner preferences');
  await page.reload();await chooseModule(true);await root(owner.source_id).click();await openFolder('Album 2');await startTrack('Album 2/Track 2.wav');await paused();assert.equal(await shuffle.getAttribute('aria-pressed'),'true');assert(await previousSong.isDisabled());
  assert.equal(await page.evaluate(()=>localStorage.getItem('keivotos:ost-shuffle')),'true');await audio.evaluate(node=>window.__shuffleAudio=node);await browse();await openFolder('Album 2/Deep');const capturedFolder=await currentSong();await nextSong.click();await awaitShuffleTrack();assert(albumPool.includes(await currentSong()));assert.notEqual(await currentSong(),capturedFolder);await sameShuffleAudio();
  check('Only the toggle survives reload; history resets on a new queue and browsing another folder preserves the captured shuffled playback queue');
  await page.reload();await chooseModule(true);await root(owner.source_id).click();await openFolder('Large library');await page.getByText('120 / 260 items',{exact:true}).waitFor();await startTrack('Large library/Track 1.wav');await paused();await audio.evaluate(node=>window.__shuffleAudio=node);
  const queuePage='**/api/ost/library?*';const isShufflePage=url=>url.searchParams.get('parent')==='Large library'&&Number(url.searchParams.get('offset'))>=120;
  const shufflePageFailure=async route=>{if(!isShufflePage(new URL(route.request().url())))return route.fallback();expectedFailures++;return route.fulfill({status:503,json:{detail:'Injected shuffle page failure'}});};
  await page.route(queuePage,shufflePageFailure);await nextSong.click();await page.getByRole('status').filter({hasText:'Injected shuffle page failure'}).waitFor();assert.equal(await currentSong(),'Large library/Track 1.wav');assert(await nextSong.isEnabled());await sameShuffleAudio();await page.unroute(queuePage,shufflePageFailure);
  let releaseShufflePage,startShufflePage;const heldShufflePage=new Promise(resolve=>releaseShufflePage=resolve),shufflePageStarted=new Promise(resolve=>startShufflePage=resolve);let shufflePageCalls=0;
  const holdShufflePage=async route=>{if(!isShufflePage(new URL(route.request().url())))return route.fallback();shufflePageCalls++;const response=await route.fetch();startShufflePage();await heldShufflePage;try{await route.fulfill({response});}catch{/* Mode changes cancel this request. */}};
  await page.route(queuePage,holdShufflePage);await nextSong.click();await shufflePageStarted;assert(await nextSong.isDisabled());await nextSong.dispatchEvent('click');assert.equal(shufflePageCalls,1);await shuffle.evaluate(node=>{node.click();node.click();});assert.equal(await shuffle.getAttribute('aria-pressed'),'true');await shuffle.click();releaseShufflePage();await page.unroute(queuePage,holdShufflePage);await page.waitForTimeout(200);assert.equal(await currentSong(),'Large library/Track 1.wav');await sameShuffleAudio();
  await nextSong.click();await waitTrack('Large library/Track 2.wav');await paused();await shuffle.click();await page.evaluate(()=>{window.__musicRandom=Math.random;Math.random=()=>.999999;});await nextSong.click();await waitTrack('Large library/Track 260.wav');await paused();await page.evaluate(()=>{Math.random=window.__musicRandom;});await sameShuffleAudio();
  await browse();await page.getByText('120 / 260 items',{exact:true}).waitFor();
  check('Shuffle includes all 260 tracks without expanding browse pages; failed/double/pending requests preserve audio, turning it off cancels safely, and retry can reach the last page');
  await startTrack('Large library/Track 1.wav');await paused();let releaseReplacement,startReplacement;const replacementHeld=new Promise(resolve=>releaseReplacement=resolve),replacementStarted=new Promise(resolve=>startReplacement=resolve);
  const holdReplacement=async route=>{if(!isShufflePage(new URL(route.request().url())))return route.fallback();const response=await route.fetch();startReplacement();await replacementHeld;try{await route.fulfill({response});}catch{/* An explicit new queue supersedes this response. */}};
  await page.route(queuePage,holdReplacement);await nextSong.click();await replacementStarted;await browse();await back();await startTrack('Root track.wav');await paused();releaseReplacement();await page.unroute(queuePage,holdReplacement);await page.waitForTimeout(200);assert.equal(await currentSong(),'Root track.wav');assert(await previousSong.isDisabled());assert(await nextSong.isDisabled());
  await cycleTo('off');await finishTrack();await page.waitForFunction(()=>document.querySelector('audio')?.ended);await cycleTo('all');await finishTrack();await page.waitForFunction(()=>{const n=document.querySelector('audio');return n&&!n.paused&&!n.ended&&n.currentTime<1;});assert.equal(await currentSong(),'Root track.wav');await cycleTo('one');await finishTrack();await page.waitForFunction(()=>{const n=document.querySelector('audio');return n&&!n.paused&&!n.ended&&n.currentTime<1;});await paused();await cycleTo('off');
  check('A new queue cancels old shuffle page reads; single-track queues stop with Loop Off and replay naturally with Loop All/One');
  await browse();await openSettings();assert.equal((await api('/api/suite/folders/apply','POST',{folders:[{source_id:owner.source_id,display_name:'Soundtracks',role:'ost',visible:false}]})).status,200);await closeSettings();await page.locator('audio').waitFor({state:'detached'});assert.equal(await page.evaluate(()=>localStorage.getItem('keivotos:ost-shuffle')),'true');
  assert.equal((await api('/api/suite/folders/apply','POST',{folders:[{source_id:owner.source_id,display_name:'Soundtracks',role:'ost',visible:true}]})).status,200);
  await page.reload();await chooseModule(false);await detailsReady();await searchFor('Track 26',2);await trackRow(owner.source_id,'Large library/Track 26.wav').click();await waitTrack('Large library/Track 26.wav');await paused();assert(await previousSong.isDisabled());await browse();await searchFor('Track 25',11);await nextSong.click();await waitTrack('Large library/Track 260.wav');await paused();assert(await nextSong.isDisabled());
  check('Source loss releases shuffled audio and keeps only its saved toggle; Library shuffle retains its original search while browsing another query');
  assert.equal((await api('/api/suite/folders/apply','POST',{folders:[{source_id:albumOwner.source_id,display_name:'Album library',role:'ost',visible:true}]})).status,200);
  await page.reload();await chooseModule(false);await detailsReady();await sectionTo('Albums');await detailsReady();await albumTile(blueAlbum).click();await page.evaluate(()=>{window.__musicRandom=Math.random;Math.random=()=>.999999;});await albumRows.locator('button').first().click();await waitTrack('Blue/Disc 1/01 - First.flac');await paused();await audio.evaluate(node=>window.__shuffleAudio=node);await nextSong.click();await waitTrack('Blue/Disc 2/01 - Finale.flac');await paused();await page.evaluate(()=>{Math.random=window.__musicRandom;});await sameShuffleAudio();await browse();
  assert.deepEqual(await albumView.locator('h3').allTextContents(),['Disc 1','Disc 2']);assert.deepEqual(await albumRows.locator('.number').allTextContents(),['1','2','1']);await page.getByRole('button',{name:'Back to Albums',exact:true}).click();await albumTile(largeAlbum).click();await page.waitForFunction(()=>document.querySelectorAll('[aria-label="Album tracks"] button').length===120);await page.evaluate(()=>{window.__musicRandom=Math.random;Math.random=()=>.999999;});await albumRows.locator('button').first().click();await waitTrack('Large library/Track 1.wav');await paused();await nextSong.click();await waitTrack('Large library/Track 260.wav');await paused();await page.evaluate(()=>{Math.random=window.__musicRandom;});await browse();assert.equal(await albumRows.locator('button').count(),120);
  check('Album shuffle crosses publisher discs and later pages while preserving disc labels/numbers and the 120-row browsing prefix');
  assert.deepEqual(hashes(fixtureRoot),shuffleOriginals);assert.deepEqual(hashes(albumLocal),shuffleAlbums);await shuffle.click();assert.equal(await shuffle.getAttribute('aria-pressed'),'false');
  check('Shuffle writes only its local on/off preference; all generated originals, publisher layouts and tagged album bytes remain exact');

  assert.deepEqual(report.errors,[]);
 }finally{
  fs.writeFileSync(path.join(config.output,'report.json'),JSON.stringify(report,null,2));
  await context.tracing.stop({path:path.join(config.output,'trace.zip')});await browser.close();
 }
})().catch(error=>{console.error(error);process.exitCode=1;});

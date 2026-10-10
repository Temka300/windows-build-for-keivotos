const assert=require('node:assert/strict');const fs=require('node:fs');const path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
(async()=>{
 const browser=await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});const page=await context.newPage();page.setDefaultTimeout(15000);
 const report={checks:[],errors:[]};page.on('pageerror',e=>report.errors.push(e.message));
 await page.route('**/*',route=>{if(new URL(route.request().url()).origin!==config.url){report.errors.push('Unexpected remote request');return route.abort();}return route.continue();});
 await context.tracing.start({screenshots:true,snapshots:true});
 const check=x=>{report.checks.push(x);console.log('PASS: '+x);};
 async function api(url,method='GET',body){return page.evaluate(async({url,method,body})=>{const r=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});return {status:r.status,data:await r.json()};},{url,method,body});}
 async function libraryRow(predicate){
  const deadline=Date.now()+15000;
  while(Date.now()<deadline){
   const response=await api('/api/youtube/library');assert.equal(response.status,200);
   const row=response.data.items.find(predicate);if(row)return row;
   await page.waitForTimeout(100);
  }
  throw new Error('Expected YouTube job state was not reached');
 }
 const input=page.getByLabel('Search downloads or paste a YouTube URL');
 const url='https://www.youtube.com/watch?v=abcdefghijk';
 try{
  await page.goto(config.url);assert.equal((await api('/api/youtube/library')).status,409);
  const videoPresent=(await api('/api/suite/modules')).data.some(module=>module.slug==='video');
  assert.equal((await api('/api/suite/modules/youtube/enable','POST')).status,200);
  assert.equal((await api('/api/video/library')).status,videoPresent?409:404);
  assert.equal((await api('/api/suite/folders/apply','POST',{folders:[{path:config.media,display_name:'Downloads',role:'youtube'}]})).status,200);
  await page.evaluate(()=>{
   localStorage.setItem('keivotos:sidebar-handle-position','37');
   window.__youtubeEntryWidths=[];
   const record=(frame)=>{const node=document.querySelector('[data-sidebar-owner="youtube"]');if(node){window.__youtubeEntryWidths.push(node.getBoundingClientRect().width);frame++;}if(frame<50)requestAnimationFrame(()=>record(frame));};
   requestAnimationFrame(()=>record(0));
  });
  await page.getByRole('button',{name:'Open Keivotos menu'}).click();await page.locator('.app-drawer').getByRole('button',{name:'YouTube',exact:true}).click();
  const toggle=page.getByRole('button',{name:'Download options',exact:true});
  for(const text of ['valley','https://example.com/watch?v=abcdefghijk','https://youtube.com.evil/watch?v=abcdefghijk','https://youtube.com/playlist?list=abc']){
   await input.fill(text);assert.equal(await page.getByRole('button',{name:'Download',exact:true}).count(),0);
  }
  const dock=page.locator('[data-sidebar-owner="youtube"]'),grip=dock.locator('[data-module-sidebar-grip]');
  const sections=page.getByRole('navigation',{name:'YouTube sections'});
  assert.deepEqual(await sections.locator('button').allTextContents(),['All videos','Playlists']);
  await page.waitForTimeout(350);
  const entry=await page.evaluate(()=>window.__youtubeEntryWidths);
  assert(entry.some(width=>width<1)&&entry.some(width=>width>1&&width<255)&&entry.some(width=>width>=255),JSON.stringify(entry));
  await grip.click({force:true});await page.waitForTimeout(65);
  const closingWidth=(await dock.boundingBox()).width;assert(closingWidth>0&&closingWidth<256);
  await grip.click({force:true});await page.waitForTimeout(340);assert((await dock.boundingBox()).width>=255);
  await grip.click();await page.waitForTimeout(330);assert((await dock.boundingBox()).width<1);
  assert(await dock.locator('.module-sidebar-panel').evaluate(node=>node.inert));
  assert.equal(await page.getByRole('button',{name:'Playlists',exact:true}).count(),0);
  await grip.click();await page.waitForTimeout(330);
  check('YouTube has exactly All videos / Playlists; entry slide, rapid reversal and closed-panel inertness work');

  const gripBox=await grip.boundingBox(),dockBox=await dock.boundingBox();
  await page.mouse.move(gripBox.x+20,gripBox.y+32);await page.mouse.down();
  await page.mouse.move(gripBox.x+20,dockBox.y+dockBox.height*.76,{steps:8});await page.mouse.up();
  assert.equal(await grip.getAttribute('aria-expanded'),'true');
  const movedPosition=Number(await page.evaluate(()=>localStorage.getItem('keivotos:youtube-sidebar-handle-position')));assert(movedPosition>70&&movedPosition<82);
  await grip.focus();await page.keyboard.press('Home');assert.equal(Number(await page.evaluate(()=>localStorage.getItem('keivotos:youtube-sidebar-handle-position'))),10);
  await page.keyboard.press('End');await page.keyboard.press('ArrowUp');
  assert.equal(Number(await page.evaluate(()=>localStorage.getItem('keivotos:youtube-sidebar-handle-position'))),85);
  await page.reload();await sections.waitFor();await page.waitForTimeout(350);
  assert.equal(await page.evaluate(()=>localStorage.getItem('keivotos:youtube-sidebar-handle-position')),'85');
  assert.equal(await page.evaluate(()=>localStorage.getItem('keivotos:sidebar-handle-position')),'37');
  assert.equal(await page.evaluate(()=>localStorage.getItem('keivotos:language-sidebar-handle-position')),null);
  await grip.click();await page.waitForTimeout(330);await page.reload();
  await grip.waitFor();assert.equal(await grip.getAttribute('aria-expanded'),'false');assert(await dock.locator('.module-sidebar-panel').evaluate(node=>node.inert));
  await grip.click();await page.waitForTimeout(330);
  check('YouTube grip drag does not toggle, keyboard positions clamp, reload restores open/position and other owners keep separate keys');

  await page.mouse.move(900,120);await page.waitForTimeout(1850);
  const visual=grip.locator('.module-sidebar-grip-visual');assert(Number(await visual.evaluate(node=>getComputedStyle(node).opacity))<.05);
  await grip.hover();await page.waitForTimeout(200);assert(Number(await visual.evaluate(node=>getComputedStyle(node).opacity))>.95);
  await page.mouse.move(900,120);await page.waitForTimeout(220);assert(Number(await visual.evaluate(node=>getComputedStyle(node).opacity))<.05);
  await page.evaluate(()=>document.documentElement.dataset.motion='reduced');
  assert(Number.parseFloat(await dock.evaluate(node=>getComputedStyle(node).transitionDuration))<.001);
  assert(Number.parseFloat(await dock.locator('.module-sidebar-panel').evaluate(node=>getComputedStyle(node).transitionDuration))<.001);
  await page.evaluate(()=>{delete document.documentElement.dataset.motion;});
  await page.setViewportSize({width:1440,height:140});
  assert(await sections.evaluate(node=>node.scrollHeight>node.clientHeight));
  await sections.hover();await page.mouse.wheel(0,150);await page.waitForTimeout(80);
  assert(await sections.evaluate(node=>node.scrollTop>0));
  await page.setViewportSize({width:360,height:780});
  await page.getByRole('button',{name:'Playlists',exact:true}).click();await page.getByRole('heading',{name:'Playlists',exact:true}).waitFor();
  await page.waitForTimeout(330);assert.equal(await grip.getAttribute('aria-expanded'),'false');
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.setViewportSize({width:1440,height:1000});await grip.click();await page.waitForTimeout(330);
  check('YouTube grip hides after delay and returns on hover; reduced motion, scrollable body and narrow navigation avoid overflow');

  const plus=page.getByRole('button',{name:'New playlist',exact:true});
  await page.getByText('No playlists yet.',{exact:true}).waitFor();await plus.click();
  const createDialog=page.getByRole('dialog',{name:'New playlist',exact:true}),title=createDialog.getByLabel('Title',{exact:true});
  await title.waitFor();assert.equal(await title.evaluate(node=>document.activeElement===node),true);
  await title.fill('Unsaved title');await page.keyboard.press('Shift+Tab');
  assert(await createDialog.getByRole('button',{name:'Close new playlist'}).evaluate(node=>document.activeElement===node));
  await page.keyboard.press('Shift+Tab');assert(await createDialog.getByRole('button',{name:'Create',exact:true}).evaluate(node=>document.activeElement===node));
  await page.keyboard.press('Tab');assert(await createDialog.getByRole('button',{name:'Close new playlist'}).evaluate(node=>document.activeElement===node));
  await page.keyboard.press('Escape');await createDialog.waitFor({state:'detached'});assert(await plus.evaluate(node=>document.activeElement===node));
  assert.equal((await api('/api/youtube/playlists')).data.total,0);
  check('New playlist opens the compact title form, traps focus, cancels with Escape and restores the + button without mutation');

  const creates=[];let lost=true;
  const loseCreate=async route=>{
   if(route.request().method()!=='POST')return route.continue();
   creates.push(route.request().postDataJSON());
   if(lost){lost=false;const response=await route.fetch();await new Promise(resolve=>setTimeout(resolve,220));return route.fulfill({response,status:503,json:{detail:'Generated lost create acknowledgement'}});}
   return route.continue();
  };
  await page.route('**/api/youtube/playlists',loseCreate);
  await plus.click();await title.fill('  공부 음악  ');await createDialog.getByRole('button',{name:'Create',exact:true}).click();
  await createDialog.getByRole('button',{name:'Creating…',exact:true}).waitFor();await page.keyboard.press('Enter');
  await createDialog.getByRole('alert').waitFor();assert.equal(await title.inputValue(),'  공부 음악  ');assert.equal(creates.length,1);
  const acknowledgedOnDisk=(await api('/api/youtube/playlists')).data.items[0];assert.equal(acknowledgedOnDisk.title,'공부 음악');
  await createDialog.getByRole('button',{name:'Create',exact:true}).click();await createDialog.waitFor({state:'detached'});
  await page.locator('.playlist-row').filter({hasText:'공부 음악'}).waitFor();
  assert.equal(creates.length,2);assert.equal(creates[0].request_id,creates[1].request_id);
  assert.equal((await api('/api/youtube/playlists')).data.total,1);
  assert.equal(await page.locator('.playlist-row').count(),1);assert(await plus.evaluate(node=>document.activeElement===node));
  await page.unroute('**/api/youtube/playlists',loseCreate);
  await page.screenshot({path:path.join(config.output,'youtube-playlists.png')});
  check('Create retains title after a lost acknowledgement, serializes submission, retries its saved token and inserts one Unicode playlist immediately');

  for(let i=0;i<65;i++)assert.equal((await api('/api/youtube/playlists','POST',{title:'Generated playlist '+i,request_id:await page.evaluate(()=>crypto.randomUUID())})).status,200);
  await sections.getByRole('button',{name:'All videos',exact:true}).click();
  let releaseList,startList;const heldList=new Promise(resolve=>releaseList=resolve),startedList=new Promise(resolve=>startList=resolve);let holdFirst=true;
  const staleList=async route=>{
   if(route.request().method()!=='GET'||!holdFirst)return route.continue();
   holdFirst=false;startList();await heldList;
   try{await route.fulfill({json:{items:[],total:0}});}catch{/* The old component's abort can finish before this test response. */}
  };
  await page.route('**/api/youtube/playlists?*',staleList);
  await sections.getByRole('button',{name:'Playlists',exact:true}).click();await startedList;
  await sections.getByRole('button',{name:'All videos',exact:true}).click();await sections.getByRole('button',{name:'Playlists',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('.playlist-row').length===60);releaseList();await page.waitForTimeout(180);
  assert.equal(await page.locator('.playlist-row').count(),60);
  await page.getByRole('button',{name:'Load more playlists',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('.playlist-row').length===66);
  assert.equal(new Set(await page.locator('.playlist-row').evaluateAll(nodes=>nodes.map(node=>node.dataset.playlistId))).size,66);
  await page.unroute('**/api/youtube/playlists?*',staleList);
  await sections.getByRole('button',{name:'All videos',exact:true}).click();await input.waitFor();
  check('Playlist pagination retains every row; rapid section changes ignore an old delayed response and All videos keeps its existing controls');

  const beforeSearch=await input.boundingBox(),beforeOptions=await toggle.boundingBox();
  const reveal=page.locator('.download-reveal');
  assert((await reveal.getAttribute('inert'))!==null);
  await input.fill(url);assert.equal(await page.getByLabel('Video quality').count(),0);
  assert.equal(await page.locator('.download-grid article').count(),0);
  const duringClip=await reveal.evaluate(node=>getComputedStyle(node).clipPath);
  await page.waitForTimeout(70);
  const middleClip=await reveal.evaluate(node=>getComputedStyle(node).clipPath);
  await page.waitForTimeout(220);
  const finalClip=await reveal.evaluate(node=>getComputedStyle(node).clipPath);
  assert.notEqual(duringClip,finalClip);assert.notEqual(middleClip,finalClip);
  assert.equal(finalClip,'inset(0px)');
  const afterSearch=await input.boundingBox(),afterOptions=await toggle.boundingBox();
  assert(Math.abs(beforeSearch.x-afterSearch.x)<1&&Math.abs(beforeSearch.width-afterSearch.width)<1);
  assert(Math.abs(beforeOptions.x-afterOptions.x)<1);
  for(let i=0;i<3;i++){
   await input.fill('https://example.com/watch?v=abcdefghijk');
   assert.equal(await page.getByRole('button',{name:'Download',exact:true}).count(),0);
   await input.fill(url);
   assert.equal(await page.getByRole('button',{name:'Download',exact:true}).count(),1);
  }
  await page.waitForTimeout(250);
  await page.setViewportSize({width:360,height:780});
  const narrowSearch=await input.boundingBox(),narrowDownload=await page.getByRole('button',{name:'Download',exact:true}).boundingBox();
  assert(narrowSearch.width>=80&&narrowDownload.x+narrowDownload.width<=360);
  await page.setViewportSize({width:1440,height:1000});
  await page.evaluate(()=>document.documentElement.dataset.motion='reduced');
  assert(Number.parseFloat(await reveal.evaluate(node=>getComputedStyle(node).transitionDuration))<.001);
  await page.evaluate(()=>{delete document.documentElement.dataset.motion;});
  check('search width and options position stay fixed; Download reveals left to right, reverses rapidly and fits narrow screens');
  const top=await page.locator('main').last().evaluate(n=>n.getBoundingClientRect().top);
  await toggle.click();await page.waitForTimeout(60);const middle=await page.locator('.download-options').evaluate(n=>n.getBoundingClientRect().height);await page.waitForTimeout(220);const full=await page.locator('.download-options').evaluate(n=>n.getBoundingClientRect().height);assert(middle>0&&middle<full);
  assert.equal(await page.locator('main').last().evaluate(n=>n.getBoundingClientRect().top),top);
  assert.equal(await page.getByLabel('Video quality').inputValue(),'best');assert.equal(await page.getByLabel('Audio quality').inputValue(),'best');
  const toggleBox=await toggle.boundingBox(),searchBox=await input.boundingBox(),downloadBox=await page.getByRole('button',{name:'Download',exact:true}).boundingBox();
  assert(toggleBox.x+toggleBox.width<searchBox.x&&searchBox.x-toggleBox.x-toggleBox.width<16);
  assert(downloadBox.x>searchBox.x+searchBox.width&&downloadBox.x-searchBox.x-searchBox.width<16);
  const videoBox=await page.getByLabel('Video quality').boundingBox(),audioBox=await page.getByLabel('Audio quality').boundingBox();
  assert(Math.abs(videoBox.y-audioBox.y)<1&&audioBox.x>videoBox.x);
  assert.equal(await page.locator('.download-options').getByRole('button',{name:'Download',exact:true}).count(),0);
  await page.screenshot({path:path.join(config.output,'youtube-options.png')});
  await page.locator('.module-title').click();assert.equal(await toggle.getAttribute('aria-expanded'),'true');
  await toggle.click();await page.waitForTimeout(250);assert.equal(await page.getByLabel('Video quality').count(),0);
  await toggle.click();await page.waitForTimeout(250);await page.getByRole('button',{name:'Download',exact:true}).click();
  await page.getByRole('progressbar').waitFor();
  await page.getByRole('status').filter({hasText:/Download queued:|Downloading:/}).waitFor();
  assert.equal(await page.locator('.action-notification[data-notification-id^="youtube-download-"]').count(),1);
  await page.locator('.download-grid article').first().click({button:'right'});
  const unfinishedMenu=page.getByRole('menu');
  assert(await unfinishedMenu.getByRole('menuitem',{name:'Show in Files'}).isDisabled());
  assert(await unfinishedMenu.getByRole('menuitem',{name:'Open in Explorer'}).isDisabled());
  assert(await unfinishedMenu.getByRole('menuitem',{name:'Add to playlists'}).isDisabled());
  await unfinishedMenu.getByText('This download has no completed local file.').waitFor();
  await page.keyboard.press('Escape');
  await page.waitForTimeout(1100);
  await input.fill('https://youtu.be/lmnopqrstuv');await page.waitForTimeout(250);
  await page.screenshot({path:path.join(config.output,'youtube-downloading.png')});
  check('URL controls animate open and close; qualities default to Best; explicit Download creates a progress card');
  await libraryRow(x=>x.status==='complete');
  await page.waitForTimeout(1100);await input.fill('');await page.waitForTimeout(300);
  await page.waitForFunction(()=>document.querySelector('.thumbnail img')?.naturalWidth>0);
  await page.screenshot({path:path.join(config.output,'youtube-library.png')});
  const result=(await api('/api/youtube/library')).data.items[0];assert.equal(result.status,'complete');
  await page.getByRole('status').filter({hasText:'Download complete:'}).waitFor();
  assert.equal(await page.locator('.action-notification[data-notification-id="youtube-download-'+result.id+'"]').count(),1);
  assert(result.path.endsWith('Valley.mp4'));
  const youtubeTitle=page.locator(`[data-job-id="${result.id}"] .download-title`);
  for(const width of [1440,360]){
   await page.setViewportSize({width,height:1000});
   for(const text of [result.title,'静かな谷を越えて旅を続ける物語','A quiet journey through the valley that needs more than two rows','unbroken'.repeat(18)]){
    await youtubeTitle.evaluate((node,value)=>node.textContent=value,text);
    const metrics=await youtubeTitle.evaluate(node=>({height:node.getBoundingClientRect().height,width:node.getBoundingClientRect().width,card:node.closest('article').getBoundingClientRect().width,font:parseFloat(getComputedStyle(node).fontSize),clamp:getComputedStyle(node).webkitLineClamp,wrap:getComputedStyle(node).overflowWrap}));
    assert.equal(metrics.clamp,'2');assert.equal(metrics.wrap,'anywhere');
    assert(Math.abs(metrics.height-metrics.font*2.7)<1);assert(metrics.width<=metrics.card);
   }
  }
  await youtubeTitle.evaluate((node,value)=>node.textContent=value,result.title);
  await page.setViewportSize({width:1440,height:1000});
  assert.equal(await youtubeTitle.getAttribute('title'),result.title);
  assert.equal(await page.locator(`[data-job-id="${result.id}"]`).getAttribute('aria-label'),result.title);
  check('YouTube titles reserve two rows and contain Japanese, long Latin and unbroken names at both widths');
  const files=(await api('/api/files/search?q=Valley')).data;assert(files.some(x=>x.relative_path===result.path));

  await input.fill('not found');await page.waitForTimeout(350);assert.equal(await page.locator('.download-grid article').count(),0);
  await input.fill('valley');await page.waitForTimeout(350);assert.equal(await page.locator('.download-grid article').count(),1);
  check('completed output and local thumbnail appear in YouTube, Files discovery; local search works');
  let revealed=null;
  const revealRoute=async route=>{revealed=route.request().postDataJSON();await route.fulfill({json:{status:'revealed'}});};
  await page.route('**/api/files/reveal',revealRoute);
  await page.locator('.download-grid article').click({button:'right'});
  await page.getByRole('menu').getByRole('menuitem',{name:'Open in Explorer'}).click();
  assert.deepEqual(revealed,{source_id:result.source_id,path:result.path});
  await page.unroute('**/api/files/reveal',revealRoute);
  await page.locator('.download-grid article').click({button:'right'});
  await page.getByRole('menu').getByRole('menuitem',{name:'Show in Files'}).click();
  await page.locator('[data-files-entry-path]').filter({hasText:'Valley.mp4'}).waitFor();
  await page.getByRole('button',{name:'Open Keivotos menu'}).click();
  await page.locator('.app-drawer').getByRole('button',{name:'YouTube',exact:true}).click();
  await page.getByRole('button',{name:'Open '+result.title,exact:true}).waitFor();
  check('YouTube completed media uses exact Files/Explorer location; unfinished jobs disable both actions');
  const tile=page.getByRole('button',{name:'Open '+result.title,exact:true});
  await tile.click();await page.getByRole('dialog',{name:'YouTube player'}).waitFor();
  assert.equal(await page.getByRole('button',{name:'Playlist videos',exact:true}).count(),0);
  await page.waitForFunction(()=>document.querySelector('video')?.currentTime>0.1);
  await page.getByRole('button',{name:'Pause',exact:true}).click();
  assert(await page.locator('video').evaluate(v=>v.paused));
  await page.getByRole('dialog',{name:'YouTube player'}).focus();
  await page.locator('video').evaluate(node=>node.currentTime=.2);
  await page.keyboard.press('ArrowRight');
  await page.waitForFunction(()=>document.querySelector('video').currentTime>1);
  await page.keyboard.press('ArrowLeft');
  assert((await page.locator('video').evaluate(node=>node.currentTime))<.25);
  await page.keyboard.press('Space');await page.waitForFunction(()=>!document.querySelector('video').paused);
  await page.keyboard.press('Space');await page.waitForFunction(()=>document.querySelector('video').paused);
  check('YouTube shared player responds to Space and five-second seek keys');
  await page.getByRole('button',{name:'Play',exact:true}).click();
  await page.locator('video').evaluate(node=>node.loop=true);
  await page.getByRole('dialog',{name:'YouTube player'}).focus();
  await page.mouse.move(700,400);await page.mouse.move(1439,400);
  assert(await page.locator('.player-controls').evaluate(node=>node.classList.contains('controls-hidden')));
  await page.mouse.move(700,400);
  assert(await page.locator('.player-controls').evaluate(node=>!node.classList.contains('controls-hidden')));
  await page.mouse.move(700,999);
  assert(await page.locator('.player-controls').evaluate(node=>node.classList.contains('controls-hidden')));
  await page.mouse.move(700,400);
  await page.locator('video').evaluate(node=>node.loop=false);
  await page.getByRole('button',{name:'Pause',exact:true}).click();
  check('YouTube shared player hides controls at right/bottom edges and reveals them on return');
  const volume=page.getByRole('slider',{name:'Volume'});
  await volume.fill('62');await volume.dispatchEvent('input');
  await page.waitForFunction(()=>Math.abs(document.querySelector('video').volume-.62)<.001);
  assert.equal(await volume.getAttribute('aria-valuetext'),'62%');
  assert.equal(await page.evaluate(()=>localStorage.getItem('keivotos:youtube-volume-percent')),'62');
  assert.equal(await page.evaluate(()=>localStorage.getItem('keivotos:video-volume-percent')),'100');
  await page.getByLabel('Seek',{exact:true}).fill('1');
  await page.getByLabel('Seek',{exact:true}).dispatchEvent('input');
  await page.waitForFunction(()=>document.querySelector('video').currentTime>=0.9);
  await page.waitForFunction(()=>document.querySelector('[aria-label="Playback time"]')?.textContent==='0:01 / 0:04');
  for(const width of [1440,360]){
   await page.setViewportSize({width,height:1000});
   const timerBox=await page.getByLabel('Playback time',{exact:true}).boundingBox(),volumeBox=await page.locator('.volume-control').boundingBox();
   assert(timerBox.x>=volumeBox.x+volumeBox.width&&timerBox.x-volumeBox.x-volumeBox.width<16);
   assert(Math.abs(timerBox.y+timerBox.height/2-volumeBox.y-volumeBox.height/2)<2);
   assert(timerBox.x+timerBox.width<=width&&(await volume.boundingBox()).width>=40);
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   await page.screenshot({path:path.join(config.output,`youtube-playback-time-${width}.png`)});
  }
  await page.setViewportSize({width:1440,height:1000});
  check('YouTube uses the same elapsed/duration display right of volume, updates after seeking and fits desktop/compact controls');
  assert(await page.getByRole('button',{name:'Previous video',exact:true}).isDisabled());
  assert(await page.getByRole('button',{name:'Next video',exact:true}).isDisabled());
  await page.getByRole('button',{name:'Fullscreen',exact:true}).click();
  await page.waitForFunction(()=>!!document.fullscreenElement);
  await page.getByRole('button',{name:'Fullscreen',exact:true}).click();
  await page.waitForFunction(()=>!document.fullscreenElement);
  await page.getByRole('button',{name:'Back to downloads'}).hover();await page.waitForTimeout(220);
  await page.screenshot({path:path.join(config.output,'youtube-player.png')});
  await page.getByRole('button',{name:'Back to downloads'}).click();
  assert(await tile.evaluate(n=>n===document.activeElement));
  assert.equal(context.pages().length,1);
  assert.equal((await api('/api/video/library')).status,videoPresent?409:404);
  check('YouTube uses shared playback, seek, fullscreen, hover Back and focus return with Video disabled; no new tab');
  const second=(await api('/api/youtube/downloads','POST',{url:'https://youtu.be/lmnopqrstuv',video:'best',audio:'best'})).data;
  await libraryRow(x=>x.id===second.id&&x.status==='complete');
  await page.reload();await input.waitFor();
  const tiles=page.locator('.play-download');await page.waitForFunction(()=>document.querySelectorAll('.play-download').length===2);
  await tiles.first().click();await page.waitForFunction(()=>document.querySelector('video')?.currentTime>0.1);
  assert.equal(await page.getByRole('slider',{name:'Volume'}).inputValue(),'62');
  assert(Math.abs((await page.locator('video').evaluate(node=>node.volume))-.62)<.001);
  const firstSrc=await page.locator('video').getAttribute('src');
  await page.getByRole('button',{name:'Next video',exact:true}).click();
  await page.waitForFunction(src=>document.querySelector('video')?.getAttribute('src')!==src,firstSrc);
  assert(Math.abs((await page.locator('video').evaluate(node=>node.volume))-.62)<.001);
  assert(await page.getByRole('button',{name:'Next video',exact:true}).isDisabled());
  await page.getByRole('button',{name:'Previous video',exact:true}).click();
  await page.waitForFunction(src=>document.querySelector('video')?.getAttribute('src')===src,firstSrc);
  await page.getByRole('button',{name:'Fullscreen',exact:true}).focus();await page.keyboard.press('Tab');
  assert(await page.getByRole('button',{name:'Back to downloads'}).evaluate(n=>n===document.activeElement));
  await page.keyboard.press('Escape');await page.getByRole('dialog',{name:'YouTube player'}).waitFor({state:'detached'});
  assert(await tiles.first().evaluate(n=>n===document.activeElement));
  check('YouTube previous/next follows its completed downloads; focus is trapped and Escape restores the original tile');

  const playlistRows=(await api('/api/youtube/playlists?limit=120')).data.items;
  const chosen=playlistRows[0],other=playlistRows[1],hiddenChoice=playlistRows[playlistRows.length-1];
  const memberUrl='/api/youtube/videos/'+result.id+'/playlists';
  assert.equal((await api(memberUrl,'POST',{playlist_ids:[hiddenChoice.id],version:0})).status,200);
  const localBytes=fs.readFileSync(path.join(config.mediaLocal,result.path));
  const membershipDialog=page.getByRole('dialog',{name:'Add to playlists',exact:true});
  const card=page.locator('[data-job-id="'+result.id+'"]');
  async function selectPlaylists(){
    await card.focus();await page.keyboard.press('Shift+F10');
    const menu=page.getByRole('menu');await menu.getByRole('menuitem',{name:'Add to playlists'}).waitFor();
    await page.waitForFunction(()=>document.querySelector('[role="menuitem"]')===document.activeElement);
    assert.deepEqual(await menu.getByRole('menuitem').allTextContents(),['Show in Files','Open in Explorer','Add to playlists']);
    await menu.focus();await page.keyboard.press('ArrowUp');assert(await menu.getByRole('menuitem',{name:'Add to playlists'}).evaluate(n=>n===document.activeElement));
    await page.keyboard.press('End');assert(await menu.getByRole('menuitem',{name:'Add to playlists'}).evaluate(n=>n===document.activeElement));
    await page.keyboard.press('ArrowDown');assert(await menu.getByRole('menuitem',{name:'Show in Files'}).evaluate(n=>n===document.activeElement));
    await page.keyboard.press('ArrowUp');await page.keyboard.press('Enter');
    await membershipDialog.waitFor();await page.waitForFunction(()=>document.querySelectorAll('.playlist-choice').length===60);
  }
  await selectPlaylists();
  assert(await page.locator('.youtube-surface').evaluate(n=>n.inert));
  assert.equal(await membershipDialog.getByRole('checkbox').count(),60);
  await membershipDialog.getByRole('checkbox',{name:chosen.title,exact:true}).check();
  await membershipDialog.getByRole('button',{name:'Load more playlists'}).click();
  await page.waitForFunction(()=>document.querySelectorAll('.playlist-choice').length===66);
  assert(await membershipDialog.getByRole('checkbox',{name:hiddenChoice.title,exact:true}).isChecked());
  assert(await membershipDialog.getByRole('checkbox',{name:chosen.title,exact:true}).isChecked());
  await membershipDialog.getByRole('button',{name:'Save',exact:true}).focus();await page.keyboard.press('Tab');
  assert(await membershipDialog.getByRole('button',{name:'Close playlist selection'}).evaluate(n=>n===document.activeElement));
  await page.keyboard.press('Shift+Tab');assert(await membershipDialog.getByRole('button',{name:'Save',exact:true}).evaluate(n=>n===document.activeElement));
  let releaseSave,startSave;const saveHeld=new Promise(resolve=>releaseSave=resolve),saveStarted=new Promise(resolve=>startSave=resolve);
  let lostSave=true,saveCalls=0;const submitted=[];
  const loseMembershipResponse=async route=>{
    if(route.request().method()!=='POST')return route.continue();
    saveCalls++;submitted.push(route.request().postDataJSON());
    if(!lostSave)return route.continue();lostSave=false;
    const committed=await route.fetch();assert.equal(committed.status(),200);startSave();await saveHeld;
    await route.fulfill({status:503,json:{detail:'Injected lost membership acknowledgement'}});
  };
  await page.route('**/api/youtube/videos/'+result.id+'/playlists',loseMembershipResponse);
  await membershipDialog.getByRole('button',{name:'Save',exact:true}).click();await saveStarted;
  assert(await membershipDialog.getByRole('button',{name:'Saving…'}).isDisabled());
  assert(await membershipDialog.getByRole('checkbox').first().isDisabled());
  await page.keyboard.press('Escape');assert.equal(await membershipDialog.count(),1);assert.equal(saveCalls,1);
  releaseSave();await membershipDialog.getByRole('alert').waitFor();
  assert(await membershipDialog.getByRole('checkbox',{name:chosen.title,exact:true}).isChecked());
  await membershipDialog.getByRole('button',{name:'Retry',exact:true}).click();await membershipDialog.waitFor({state:'detached'});
  assert.equal(saveCalls,2);assert.deepEqual(submitted[0],submitted[1]);await page.unroute('**/api/youtube/videos/'+result.id+'/playlists',loseMembershipResponse);
  let membership=(await api(memberUrl)).data;assert.equal(membership.version,2);assert.deepEqual(new Set(membership.playlist_ids),new Set([chosen.id,hiddenChoice.id]));
  assert(await card.evaluate(n=>n===document.activeElement));await page.getByRole('status').filter({hasText:'Playlists updated for'}).waitFor();
  check('Three-action menu and picker contain keyboard focus; paginated selection survives a lost Save response and exact retry without duplicate membership');

  await selectPlaylists();await membershipDialog.getByRole('checkbox',{name:chosen.title,exact:true}).uncheck();
  await page.keyboard.press('Escape');await membershipDialog.waitFor({state:'detached'});
  assert.deepEqual((await api(memberUrl)).data,membership);
  await selectPlaylists();await membershipDialog.getByRole('checkbox',{name:other.title,exact:true}).check();
  assert.equal((await api(memberUrl,'POST',{playlist_ids:[chosen.id],version:membership.version})).status,200);
  await membershipDialog.getByRole('button',{name:'Save',exact:true}).click();await membershipDialog.getByRole('alert').waitFor();
  assert(await membershipDialog.getByRole('checkbox',{name:other.title,exact:true}).isChecked());
  assert(await membershipDialog.getByRole('button',{name:'Save',exact:true}).isDisabled());
  await membershipDialog.getByRole('button',{name:'Reload saved selection'}).click();
  await page.waitForFunction(()=>!document.querySelector('.playlist-membership-dialog [role="alert"]'));
  await membershipDialog.locator('.playlist-choices[aria-busy="false"]').waitFor();
  assert(!await membershipDialog.getByRole('checkbox',{name:other.title,exact:true}).isChecked());
  await membershipDialog.getByRole('checkbox',{name:chosen.title,exact:true}).uncheck();
  await membershipDialog.getByRole('checkbox',{name:other.title,exact:true}).check();
  await membershipDialog.getByRole('button',{name:'Save',exact:true}).click();await membershipDialog.waitFor({state:'detached'});
  membership=(await api(memberUrl)).data;assert.deepEqual(membership.playlist_ids,[other.id]);assert.equal(membership.version,4);
  check('Cancel preserves committed membership; a real version conflict retains pending choices until explicit reload, and unchecking removes only the relationship');

  let releaseRead,startRead;const readHeld=new Promise(resolve=>releaseRead=resolve),readStarted=new Promise(resolve=>startRead=resolve);let delayedRead=true;
  const oldRead=async route=>{
    if(route.request().method()!=='GET'||!delayedRead)return route.continue();
    delayedRead=false;startRead();await readHeld;
    try{await route.fulfill({json:{playlist_ids:[chosen.id],version:999}});}catch{/* Closed picker aborted this read. */}
  };
  await page.route('**/api/youtube/videos/'+result.id+'/playlists',oldRead);
  await card.click({button:'right'});await page.getByRole('menuitem',{name:'Add to playlists'}).click();await readStarted;
  await page.keyboard.press('Escape');await membershipDialog.waitFor({state:'detached'});
  await selectPlaylists();releaseRead();await page.waitForTimeout(160);
  assert(await membershipDialog.getByRole('checkbox',{name:other.title,exact:true}).isChecked());
  assert(!await membershipDialog.getByRole('checkbox',{name:chosen.title,exact:true}).isChecked());
  await page.unroute('**/api/youtube/videos/'+result.id+'/playlists',oldRead);
  await membershipDialog.getByRole('button',{name:'Cancel',exact:true}).click();await membershipDialog.waitFor({state:'detached'});
  check('Closing/reopening during a delayed membership read ignores the destroyed picker response');

  const emptyMembershipList=route=>route.fulfill({json:{items:[],total:0}});
  await page.route('**/api/youtube/playlists?*',emptyMembershipList);
  await card.click({button:'right'});await page.getByRole('menuitem',{name:'Add to playlists'}).click();
  await membershipDialog.getByText('No playlists yet.',{exact:true}).waitFor();
  await membershipDialog.getByRole('button',{name:'Create playlist'}).click();
  const nestedCreate=page.getByRole('dialog',{name:'New playlist',exact:true});
  await nestedCreate.getByLabel('Title',{exact:true}).fill('Created from video picker');
  await page.unroute('**/api/youtube/playlists?*',emptyMembershipList);
  await nestedCreate.getByRole('button',{name:'Create',exact:true}).click();await nestedCreate.waitFor({state:'detached'});
  await membershipDialog.getByRole('checkbox',{name:'Created from video picker',exact:true}).check();
  await page.setViewportSize({width:360,height:420});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await membershipDialog.getByRole('button',{name:'Save',exact:true}).click();await membershipDialog.waitFor({state:'detached'});
  await page.setViewportSize({width:1440,height:1000});
  const createdFromPicker=(await api('/api/youtube/playlists?limit=120')).data.items.find(p=>p.title==='Created from video picker');
  membership=(await api(memberUrl)).data;assert.deepEqual(new Set(membership.playlist_ids),new Set([other.id,createdFromPicker.id]));
  await sections.getByRole('button',{name:'Playlists',exact:true}).click();
  await page.locator('[data-playlist-id="'+createdFromPicker.id+'"]').getByText('1 video',{exact:true}).waitFor();
  await sections.getByRole('button',{name:'All videos',exact:true}).click();await card.waitFor();
  check('Empty-list picker offers existing creation UI, preserves prior membership, fits narrow screens and playlist counts reconcile after Save');

  const jobRows=(await api('/api/youtube/library')).data.items;
  const otherVideo=jobRows.find(j=>j.status==='complete'&&j.id!==result.id);assert(otherVideo);
  assert.deepEqual((await api('/api/youtube/videos/'+otherVideo.id+'/playlists')).data.playlist_ids,[]);
  const audioLibrary=async route=>{const response=await route.fetch(),data=await response.json();data.items=data.items.map(j=>j.id===result.id?{...j,video:'audio'}:j);await route.fulfill({json:data});};
  await page.route('**/api/youtube/library?*',audioLibrary);await page.reload();await card.waitFor();
  await card.click({button:'right'});const audioMenu=page.getByRole('menu');
  assert(await audioMenu.getByRole('menuitem',{name:'Add to playlists'}).isDisabled());
  assert.equal(await audioMenu.getByRole('menuitem',{name:'Add to playlists'}).getAttribute('title'),'Audio-only downloads cannot be added to video playlists.');
  await audioMenu.getByRole('menuitem',{name:'Open in Explorer'}).waitFor({state:'visible'});
  await page.waitForFunction(()=>!document.querySelector('[role="menuitem"]')?.disabled);
  await page.keyboard.press('End');assert(await audioMenu.getByRole('menuitem',{name:'Open in Explorer'}).evaluate(n=>n===document.activeElement));
  await page.keyboard.press('Escape');await page.unroute('**/api/youtube/library?*',audioLibrary);await page.reload();await card.waitFor();
  await selectPlaylists();
  assert.equal((await api('/api/suite/modules/youtube/disable','POST')).status,200);
  await membershipDialog.getByRole('checkbox',{name:chosen.title,exact:true}).check();await membershipDialog.getByRole('button',{name:'Save',exact:true}).click();
  await membershipDialog.getByRole('alert').waitFor();assert(await membershipDialog.getByRole('checkbox',{name:chosen.title,exact:true}).isChecked());
  await membershipDialog.getByRole('button',{name:'Cancel',exact:true}).click();
  assert.equal((await api('/api/suite/modules/youtube/enable','POST')).status,200);
  assert.deepEqual((await api(memberUrl)).data,membership);
  assert.deepEqual(fs.readFileSync(path.join(config.mediaLocal,result.path)),localBytes);
  await page.reload();await card.waitFor();
  check('Audio-only Add is disabled and keyboard traversal skips it; owner disable rejects a pending Save while preserving membership, other videos and original media bytes');



  await input.fill(url);await toggle.click();await page.waitForTimeout(250);await page.getByRole('button',{name:'Download',exact:true}).click();
  const pending=(await api('/api/youtube/library')).data.items.find(item=>item.status==='queued'||item.status==='downloading');assert(pending);
  await page.getByRole('button',{name:/^Cancel /}).click();
  await page.waitForFunction(id=>!document.querySelector(`[data-job-id="${id}"]`),pending.id);
  assert.equal((await api('/api/youtube/library')).data.items.some(item=>item.id===pending.id),false);
  await page.reload();await page.getByRole('button',{name:'Open Keivotos menu'}).click();await page.locator('.app-drawer').getByRole('button',{name:'YouTube',exact:true}).click();
  assert.equal(await page.getByText('Cancelled',{exact:true}).count(),0);
  const retried=await api(`/api/youtube/downloads/${pending.id}/retry`,'POST');assert.equal(retried.status,200);
  await page.reload();await page.getByRole('button',{name:'Open Keivotos menu'}).click();await page.locator('.app-drawer').getByRole('button',{name:'YouTube',exact:true}).click();
  await page.getByRole('progressbar').waitFor();
  await api('/api/suite/modules/youtube/disable','POST');assert.equal((await api('/api/youtube/library')).status,409);
  await api('/api/suite/modules/youtube/enable','POST');
  const restored=(await api('/api/youtube/library')).data.items;assert(restored.some(x=>x.status==='interrupted'));assert(restored.some(x=>x.status==='complete'));
  check('cancelled cards stay hidden after refresh and reload; retry remains possible, disable drains the queue and preserves completed media');

  async function terminal(id,status){
   return libraryRow(x=>x.id===id&&x.status===status);
  }
  async function replacementOf(id){
   return libraryRow(x=>x.retry_of===id);
  }
  const failed=(await api('/api/youtube/downloads','POST',{url:'https://youtu.be/retryfailed'})).data;
  await terminal(failed.id,'failed');await page.reload();await input.waitFor();
  const originalCard=page.locator(`[data-job-id="${failed.id}"]`),retryButton=originalCard.getByRole('button',{name:/^Retry /});
  await retryButton.waitFor();let retryCalls=0;
  const retryPattern=`**/api/youtube/downloads/${failed.id}/retry`;
  await page.route(retryPattern,async route=>{
   retryCalls++;
   if(retryCalls===1)return route.fulfill({status:400,json:{detail:'Fixture retry refused'}});
   const response=await route.fetch();await page.waitForTimeout(350);await route.fulfill({response});
  });
  await retryButton.click();await page.getByRole('alert').filter({hasText:'Fixture retry refused'}).waitFor();
  assert(await retryButton.isEnabled());assert.equal((await api('/api/youtube/library')).data.items.some(x=>x.id===failed.id),true);
  await retryButton.evaluate(n=>{n.click();n.click();});
  await page.waitForFunction(id=>document.querySelector(`[data-job-id="${id}"] button.job-action`)?.disabled,failed.id);
  await originalCard.waitFor({state:'detached'});assert.equal(retryCalls,2);await page.unroute(retryPattern);
  const replacement=await replacementOf(failed.id);
  await terminal(replacement.id,'failed');
  await page.locator(`[data-job-id="${replacement.id}"]`).getByRole('button',{name:/^Retry /}).click();
  const successful=await replacementOf(replacement.id);
  await terminal(successful.id,'complete');await page.reload();await input.fill('Retry failure fixture');
  await page.waitForFunction(()=>document.querySelectorAll('.download-grid article').length===1&&!!document.querySelector('.play-download'));
  assert.equal(await page.locator(`[data-job-id="${failed.id}"], [data-job-id="${replacement.id}"]`).count(),0);
  for(const id of [failed.id,replacement.id])assert.equal(fs.readFileSync(path.join(config.mediaLocal,'.keivotos','youtube',id,'unfinished.part'),'utf8'),'preserved failed attempt');
  await page.screenshot({path:path.join(config.output,'retry-success.png')});
  check('rejected Retry retains its card; rapid double click submits once; failed replacement stays retryable; successful replacement alone survives reload/search; partial bytes retained');

  const repair=(await api('/api/youtube/downloads','POST',{url:'https://youtu.be/repairvideo'})).data;
  const saved=await terminal(repair.id,'failed');assert.equal(saved.failure_phase,'integration');assert(saved.path&&saved.error.includes('Video saved'));
  const publishedFile=path.join(config.mediaLocal,...saved.path.split('/')),publishedBytes=fs.readFileSync(publishedFile);
  const foldersBefore=fs.readdirSync(path.join(config.mediaLocal,'YouTube')).sort();
  // Decode the retained MP4 before retrying: this is a working video whose origin registration failed.
  await page.evaluate(async item=>{
   const video=document.createElement('video'),url=new URL('/api/files/file',location.origin);
   url.searchParams.set('source_id',item.source_id);url.searchParams.set('path',item.path);video.src=url.toString();video.muted=true;
   document.body.append(video);try{await video.play();await new Promise(resolve=>setTimeout(resolve,150));if(video.currentTime<=0)throw new Error('Saved fixture did not play');}
   finally{video.pause();video.removeAttribute('src');video.load();video.remove();}
  },saved);
  await page.reload();await input.fill('Registration recovery fixture');
  const repairCard=page.locator(`[data-job-id="${repair.id}"]`);await repairCard.getByRole('button',{name:/^Retry /}).click();
  const repaired=await replacementOf(repair.id);
  const registered=await terminal(repaired.id,'complete');assert.equal(registered.path,saved.path);
  assert.deepEqual(fs.readFileSync(publishedFile),publishedBytes);assert.deepEqual(fs.readdirSync(path.join(config.mediaLocal,'YouTube')).sort(),foldersBefore);
  await repairCard.waitFor({state:'detached'});await page.locator(`[data-job-id="${repaired.id}"] .play-download`).click();
  await page.waitForFunction(()=>document.querySelector('video')?.currentTime>0.1);await page.keyboard.press('Escape');
  await page.reload();await input.fill('Registration recovery fixture');
  await page.waitForFunction(()=>document.querySelectorAll('.download-grid article').length===1&&!!document.querySelector('.play-download'));
  check('playable published MP4 with annotation failure is registered by Retry at the identical path/bytes, with no new output folder; completed playback/reload works');

  const stale=(await api('/api/youtube/downloads','POST',{url:'https://youtu.be/stalefailed'})).data;
  const staleItem=await terminal(stale.id,'failed');let accepted=null,holdNext=false,held=false,release;
  const releaseStale=new Promise(resolve=>release=resolve);
  const mockRows=Array.from({length:119},(_,i)=>({...staleItem,id:`pagination-${i}`,title:`Pagination fixture ${i}`,status:i===0?'downloading':'complete',path:'',thumbnail:'',error:'',retry_of:'',superseded_by:''}));
  const libraryPattern='**/api/youtube/library?*';
  await page.route(libraryPattern,async route=>{
   const request=new URL(route.request().url());
   if(request.searchParams.get('q')!=='Pagination fixture')return route.continue();
   const rows=[...mockRows,accepted||staleItem],offset=Number(request.searchParams.get('offset')),limit=Number(request.searchParams.get('limit')||60);
   const snapshot={items:rows.slice(offset,offset+limit),total:rows.length};
   if(holdNext){holdNext=false;held=true;await releaseStale;}
   await route.fulfill({json:snapshot});
  });
  await input.fill('Pagination fixture');await page.waitForFunction(()=>document.querySelectorAll('.download-grid article').length===60);
  await page.getByRole('button',{name:'Load more',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('.download-grid article').length===120);
  holdNext=true;await page.waitForFunction(()=>document.querySelector('.download-grid')?.getAttribute('aria-busy')==='true');
  for(let i=0;i<50&&!held;i++)await page.waitForTimeout(20);assert(held);
  const staleRetryPattern=`**/api/youtube/downloads/${stale.id}/retry`;
  await page.route(staleRetryPattern,async route=>{const response=await route.fetch();accepted=await response.json();await route.fulfill({response});});
  await page.locator(`[data-job-id="${stale.id}"]`).getByRole('button',{name:/^Retry /}).click();
  await page.waitForFunction(id=>!document.querySelector(`[data-job-id="${id}"]`)&&document.querySelectorAll('.download-grid article').length===120,stale.id);
  release();await page.waitForTimeout(1250);
  assert.equal(await page.locator(`[data-job-id="${stale.id}"]`).count(),0);assert.equal(await page.locator(`[data-job-id="${accepted.id}"]`).count(),1);
  await page.reload();await input.fill('Pagination fixture');await page.waitForFunction(()=>document.querySelectorAll('.download-grid article').length===60);
  await page.getByRole('button',{name:'Load more',exact:true}).click();await page.locator(`[data-job-id="${accepted.id}"]`).waitFor();
  assert.equal(await page.locator(`[data-job-id="${stale.id}"]`).count(),0);
  await page.unroute(libraryPattern);await page.unroute(staleRetryPattern);await input.fill('');
  check('timed delayed old poll cannot revive a retried second-page card; 120 loaded rows remain unique and reload/pagination keeps the replacement');
  await page.reload();await page.getByRole('button',{name:'Open Keivotos menu'}).click();await page.getByRole('button',{name:'Settings',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'Settings',exact:true});
  await page.setViewportSize({width:1440,height:600});
  const nav=dialog.getByRole('navigation',{name:'Settings sections'});
  await nav.getByRole('button',{name:'Downloads',exact:true}).click();
  assert(await nav.evaluate(n=>n.scrollHeight>n.clientHeight&&n.scrollTop>0));
  assert.equal(await page.getByLabel('Save to path',{exact:true}).inputValue(),config.media);
  assert.equal(await page.getByRole('button',{name:'Change folder'}).isDisabled(),true);
  await page.screenshot({path:path.join(config.output,'youtube-settings-default.png')});
  await page.getByRole('button',{name:'Custom',exact:true}).click();
  await page.waitForFunction(()=>!document.querySelector('#youtube-save-path')?.disabled);
  await page.route('**/api/files/pick',route=>route.fulfill({json:{native:false,path:null}}));
  await page.getByRole('button',{name:'Change folder'}).click();
  const picker=page.getByRole('dialog',{name:'Choose a folder',exact:true});
  await picker.getByLabel('Folder path',{exact:true}).fill(config.media.replace(/[/\\][^/\\]+$/,'/custom-downloads'));
  await picker.getByRole('button',{name:'Go',exact:true}).click();
  await picker.getByRole('button',{name:'Choose this folder',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#youtube-save-path')?.value.endsWith('custom-downloads'));
  assert((await api('/api/youtube/settings')).data.path.endsWith('custom-downloads'));
  assert.equal(await dialog.getByRole('alert').count(),0);
  await page.screenshot({path:path.join(config.output,'youtube-settings-custom.png')});
  await page.getByRole('button',{name:'Change folder'}).click();await picker.getByRole('button',{name:'Cancel',exact:true}).click();
  assert((await api('/api/youtube/settings')).data.path.endsWith('custom-downloads'));

  await page.getByRole('button',{name:'Default',exact:true}).click();
  check('YouTube role resolves default path; Custom enables folder picker; settings categories scroll at unchanged dimensions');

  await page.getByRole('button',{name:'Close settings',exact:true}).click();
  await page.locator('.app-drawer').getByRole('button',{name:'YouTube',exact:true}).click();
  const queueSeed=(await api('/api/youtube/downloads','POST',{url:'https://youtu.be/playlistfix',source_id:result.source_id,video:'best',audio:'best'})).data;
  let queueReady=false;const seedDeadline=Date.now()+15000;
  while(Date.now()<seedDeadline){const response=await api('/api/youtube/playlists?limit=120');assert.equal(response.status,200);if(response.data.items.some(item=>item.title==='Ordered queue fixture')){queueReady=true;break;}await page.waitForTimeout(100);}
  assert(queueReady,'The complete disposable playlist fixture must be committed before opening its list');
  await sections.getByRole('button',{name:'Playlists',exact:true}).click();
  const orderedName='Ordered queue fixture',singleName='Single video fixture';
  await page.getByRole('button',{name:'Open playlist '+orderedName,exact:true}).waitFor();
  const seededPlaylists=(await api('/api/youtube/playlists?limit=120')).data.items;
  const ordered=seededPlaylists.find(p=>p.title===orderedName),single=seededPlaylists.find(p=>p.title===singleName),unavailable=seededPlaylists.find(p=>p.title==='Unavailable queue fixture');
  assert(ordered&&single&&unavailable);
  const firstQueue=(await api('/api/youtube/playlists/'+ordered.id+'/items?limit=120')).data;
  const lastQueue=(await api('/api/youtube/playlists/'+ordered.id+'/items?offset=120&limit=120')).data;
  const queued=[...firstQueue.items,...lastQueue.items];assert.equal(queued.length,131);assert.equal(firstQueue.total,131);assert.equal(queued[0].job_id,queueSeed.id);
  assert.equal(queued.filter(row=>!row.available).length,3);assert.equal(queued[129].job,null);
  const queueBytes=fs.readFileSync(path.join(config.mediaLocal,queued[2].job.path));
  await page.getByRole('button',{name:'Open playlist '+orderedName,exact:true}).click();
  const detail=page.locator('.playlist-detail');
  await page.waitForFunction(()=>document.querySelectorAll('.playlist-detail .playlist-video').length===60);
  assert(await detail.locator('[data-playlist-job-id="'+queued[1].job_id+'"] button').isDisabled());
  await detail.getByRole('button',{name:'Load more playlist videos'}).click();await page.waitForFunction(()=>document.querySelectorAll('.playlist-detail .playlist-video').length===120);
  await detail.getByRole('button',{name:'Load more playlist videos'}).click();await page.waitForFunction(()=>document.querySelectorAll('.playlist-detail .playlist-video').length===131);
  assert.equal(new Set(await detail.locator('[data-playlist-job-id]').evaluateAll(nodes=>nodes.map(n=>n.dataset.playlistJobId))).size,131);
  assert(await detail.locator('[data-playlist-job-id="'+queued[129].job_id+'"] button').isDisabled());
  await detail.evaluate(n=>n.scrollTop=n.scrollHeight);const detailScroll=await detail.evaluate(n=>n.scrollTop);assert(detailScroll>0);
  await detail.getByRole('button',{name:'Open Playlist video 128',exact:true}).click();
  const queuePlayer=page.getByRole('dialog',{name:'YouTube player',exact:true});
  await queuePlayer.waitFor();await page.waitForFunction(()=>document.querySelector('video')?.currentTime>.1);
  await page.getByRole('button',{name:'Pause',exact:true}).click();
  const current128=await page.locator('video').getAttribute('src');
  await page.getByRole('button',{name:'Back to playlist',exact:true}).click();await queuePlayer.waitFor({state:'detached'});
  assert(Math.abs((await detail.evaluate(n=>n.scrollTop))-detailScroll)<2);
  assert(await detail.getByRole('button',{name:'Open Playlist video 128',exact:true}).evaluate(n=>n===document.activeElement));
  await detail.evaluate(n=>n.scrollTop=0);await detail.getByRole('button',{name:'Open Playlist video 000',exact:true}).click();
  await queuePlayer.waitFor();await page.waitForFunction(()=>document.querySelector('video')?.currentTime>.1);
  await page.getByRole('button',{name:'Pause',exact:true}).click();
  const auto=page.getByRole('switch',{name:'Auto next',exact:true});if(await auto.getAttribute('aria-checked')==='true')await auto.click();
  const loop=()=>page.getByRole('button',{name:/^Loop (off|all|one)$/});
  for(let count=0;await loop().getAttribute('aria-label')!=='Loop off'&&count<3;count++)await loop().click();
  const queueButton=page.getByRole('button',{name:'Playlist videos',exact:true}),panel=page.getByRole('complementary',{name:'Playlist videos',exact:true});
  const iconBox=await queueButton.boundingBox(),fsBox=await page.getByRole('button',{name:'Fullscreen',exact:true}).boundingBox();
  assert(iconBox.x>=fsBox.x+fsBox.width&&iconBox.x-fsBox.x-fsBox.width<16);
  await page.locator('video').evaluate(n=>{n.currentTime=.75;window.__queueVideo=n;});
  const initialTime=await page.locator('video').evaluate(n=>n.currentTime),initialSrc=await page.locator('video').getAttribute('src');
  await page.evaluate(()=>{window.__panelTransforms=[];const started=performance.now();const sample=()=>{const n=document.querySelector('.playlist-queue-panel');window.__panelTransforms.push(getComputedStyle(n).transform);if(performance.now()-started<750)requestAnimationFrame(sample);};requestAnimationFrame(sample);});
  await queueButton.click();await page.waitForTimeout(340);
  await page.waitForFunction(()=>window.__panelTransforms.some(value=>Math.abs(new DOMMatrix(value).m41)<.1));
  assert(await page.evaluate(()=>window.__panelTransforms.some(value=>{const x=new DOMMatrix(value).m41;return x>1&&x<document.querySelector('.playlist-queue-panel').getBoundingClientRect().width+31;})));assert(await page.evaluate(()=>window.__panelTransforms.some(value=>Math.abs(new DOMMatrix(value).m41)<.1)));
  assert(await page.locator('video').evaluate(n=>n===window.__queueVideo));
  assert(Math.abs((await page.locator('video').evaluate(n=>n.currentTime))-initialTime)<.1);assert.equal(await page.locator('video').getAttribute('src'),initialSrc);
  assert(await panel.getByRole('button',{name:'Close playlist videos'}).evaluate(n=>n===document.activeElement));
  assert.equal(await panel.locator('.queue-member').count(),131);
  await panel.getByRole('button',{name:'Refresh playlist',exact:true}).focus();await page.keyboard.press('Tab');
  assert(await panel.getByRole('button',{name:'Close playlist videos'}).evaluate(n=>n===document.activeElement));
  await page.keyboard.press('Shift+Tab');assert(await panel.getByRole('button',{name:'Refresh playlist',exact:true}).evaluate(n=>n===document.activeElement));
  await page.screenshot({path:path.join(config.output,'playlist-panel.png')});
  await page.keyboard.press('Escape');assert.equal(await queuePlayer.count(),1);assert.equal(await queueButton.getAttribute('aria-expanded'),'false');
  assert(await page.locator('.playlist-queue-panel').evaluate(n=>n.inert));
  assert(await queueButton.evaluate(n=>n===document.activeElement));assert(await page.locator('video').evaluate(n=>n===window.__queueVideo));
  await queueButton.click();await page.waitForTimeout(70);await queueButton.click();await page.waitForTimeout(60);await queueButton.click();await page.waitForTimeout(340);
  assert.equal(await queueButton.getAttribute('aria-expanded'),'true');
  assert(Math.abs(await panel.evaluate(n=>new DOMMatrix(getComputedStyle(n).transform).m41))<.1);
  assert(await page.locator('video').evaluate(n=>n===window.__queueVideo));
  assert.equal(await page.locator('video').getAttribute('src'),initialSrc);
  await panel.getByRole('button',{name:'Close playlist videos'}).click();
  check('131 ordered members paginate without duplicates, keep missing references disabled, restore playlist scroll/focus, and place a sliding focus-contained panel beside Fullscreen without resetting media/time');

  await queueButton.click();await page.waitForTimeout(340);
  await panel.locator('[data-playlist-job-id="'+queued[2].job_id+'"]').click();
  await page.waitForFunction(src=>document.querySelector('video')?.getAttribute('src')!==src,initialSrc);
  await page.waitForFunction(()=>document.querySelector('video')?.currentTime>.1);await page.getByRole('button',{name:'Pause',exact:true}).click();
  assert.equal(await queueButton.getAttribute('aria-expanded'),'true');
  await panel.getByRole('heading',{name:'Playlist video 002',exact:true}).waitFor();
  await panel.getByRole('button',{name:'Close playlist videos'}).click();await page.getByRole('button',{name:'Previous video',exact:true}).click();
  await page.waitForFunction(src=>document.querySelector('video')?.getAttribute('src')===src,initialSrc);
  await page.waitForFunction(()=>document.querySelector('video')?.currentTime>.1);await page.getByRole('button',{name:'Pause',exact:true}).click();
  await page.getByRole('button',{name:'Next video',exact:true}).click();
  await page.waitForFunction(src=>document.querySelector('video')?.getAttribute('src')!==src,initialSrc);
  await page.waitForFunction(()=>document.querySelector('video')?.currentTime>.1);await page.getByRole('button',{name:'Pause',exact:true}).click();
  const secondSrc=await page.locator('video').getAttribute('src');assert.notEqual(secondSrc,current128);
  const failQueueRead=route=>route.fulfill({status:503,json:{detail:'Injected playlist read failure'}});
  await page.route('**/api/youtube/playlists/'+ordered.id+'/items?*',failQueueRead);
  await page.getByRole('button',{name:'Next video',exact:true}).click();
  await queuePlayer.getByRole('alert').filter({hasText:'Injected playlist read failure'}).waitFor();
  assert.equal(await page.locator('video').getAttribute('src'),secondSrc);
  await page.unroute('**/api/youtube/playlists/'+ordered.id+'/items?*',failQueueRead);
  await queueButton.click();await panel.getByRole('button',{name:'Retry playlist',exact:true}).click();
  await page.waitForFunction(()=>!document.querySelector('.playlist-queue-panel [role="alert"]'));
  await panel.locator('.queue-members[aria-busy="false"]').waitFor();
  await panel.getByRole('button',{name:'Close playlist videos'}).click();
  await auto.click();await loop().click();assert.equal(await loop().getAttribute('aria-label'),'Loop all');
  await page.locator('video').evaluate(n=>{n.currentTime=n.duration-.12;void n.play();});
  await page.waitForFunction(src=>document.querySelector('video')?.getAttribute('src')!==src,secondSrc);
  await page.waitForFunction(()=>document.querySelector('video')?.currentTime>.1);await page.getByRole('button',{name:'Pause',exact:true}).click();
  assert(await page.locator('video').evaluate(n=>new URL(n.currentSrc).searchParams.get('path').endsWith('Video 003.mp4')));
  check('Panel selection and previous/next stay in playlist order, skip missing media, recover a bounded API error without changing the current video, and natural Auto next follows that queue');

  await queueButton.click();await page.waitForTimeout(330);
  await panel.locator('[data-playlist-job-id="'+queued[128].job_id+'"]').click();
  await page.waitForFunction(src=>document.querySelector('video')?.getAttribute('src')===src,current128);
  await panel.getByRole('button',{name:'Close playlist videos'}).click();
  await page.locator('video').evaluate(n=>{n.currentTime=n.duration-.12;void n.play();});
  await page.waitForFunction(src=>document.querySelector('video')?.getAttribute('src')===src,initialSrc);
  await page.waitForFunction(()=>document.querySelector('video')?.currentTime>.1);await page.getByRole('button',{name:'Pause',exact:true}).click();
  await page.getByRole('button',{name:'Previous video',exact:true}).click();
  await page.waitForFunction(src=>document.querySelector('video')?.getAttribute('src')===src,current128);
  await page.waitForFunction(()=>document.querySelector('video')?.currentTime>.1);await page.getByRole('button',{name:'Pause',exact:true}).click();
  await page.getByRole('button',{name:'Next video',exact:true}).click();
  await page.waitForFunction(src=>document.querySelector('video')?.getAttribute('src')===src,initialSrc);
  await page.waitForFunction(()=>document.querySelector('video')?.currentTime>.1);await page.getByRole('button',{name:'Pause',exact:true}).click();
  await loop().click();assert.equal(await loop().getAttribute('aria-label'),'Loop one');
  await page.locator('video').evaluate(n=>{n.currentTime=n.duration-.12;void n.play();});
  await page.waitForFunction(()=>document.querySelector('video')?.currentTime>.1&&document.querySelector('video')?.currentTime<1&&!document.querySelector('video').paused);
  assert.equal(await page.locator('video').getAttribute('src'),initialSrc);
  await page.getByRole('button',{name:'Pause',exact:true}).click();
  await loop().click();assert.equal(await loop().getAttribute('aria-label'),'Loop off');
  await auto.click();assert.equal(await auto.getAttribute('aria-checked'),'false');
  await page.locator('video').evaluate(n=>{n.currentTime=n.duration-.12;void n.play();});
  await page.waitForFunction(()=>document.querySelector('video')?.ended);await page.waitForTimeout(150);
  assert.equal(await page.locator('video').getAttribute('src'),initialSrc);assert(await page.locator('video').evaluate(n=>n.paused));
  await auto.click();await loop().click();
  check('Loop All wraps both directions past unavailable tail members; Loop One repeats the same source and Auto next off stops without another selection');

  await queueButton.click();await page.waitForTimeout(320);
  await page.evaluate(()=>document.documentElement.dataset.motion='reduced');
  assert(Number.parseFloat(await panel.evaluate(n=>getComputedStyle(n).transitionDuration))<.001);
  await panel.getByRole('button',{name:'Close playlist videos'}).click();await queueButton.click();
  await page.evaluate(()=>{delete document.documentElement.dataset.motion;});
  await page.getByRole('button',{name:'Fullscreen',exact:true}).click();await page.waitForFunction(()=>!!document.fullscreenElement);
  assert(await panel.evaluate(n=>document.fullscreenElement.contains(n)));
  await page.evaluate(()=>{window.__fullscreenEscapePrevented=null;document.addEventListener('keydown',event=>{if(event.key==='Escape')window.__fullscreenEscapePrevented=event.defaultPrevented;},{once:true});});
  await page.keyboard.press('Escape');assert.equal(await page.evaluate(()=>window.__fullscreenEscapePrevented),false);
  // Headless Edge does not perform browser-chrome Escape handling. Leave the
  // key to the browser, then verify native fullscreen exit through its control.
  if(await page.evaluate(()=>!!document.fullscreenElement))await page.getByRole('button',{name:'Fullscreen',exact:true}).click();
  await page.waitForFunction(()=>!document.fullscreenElement);
  assert.equal(await queuePlayer.count(),1);
  if(await queueButton.getAttribute('aria-expanded')==='true')await page.keyboard.press('Escape');
  await queueButton.click();await page.waitForTimeout(330);
  await page.setViewportSize({width:360,height:780});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  const panelBox=await panel.boundingBox();assert(panelBox.x>=0&&panelBox.x+panelBox.width<=360);
  await panel.getByRole('button',{name:'Close playlist videos'}).click();await page.setViewportSize({width:1440,height:1000});
  await queueButton.click();await page.waitForTimeout(330);
  await page.locator('video').evaluate(n=>{n.loop=true;void n.play();});
  await page.mouse.move(1439,150);await page.waitForTimeout(5300);
  assert(!await page.locator('.player-controls').evaluate(n=>n.classList.contains('controls-hidden')));
  await page.locator('video').evaluate(n=>{n.loop=false;n.pause();});
  await panel.getByRole('button',{name:'Close playlist videos'}).click();
  await page.getByRole('button',{name:'Back to playlist',exact:true}).click();await queuePlayer.waitFor({state:'detached'});
  await detail.getByRole('button',{name:'Back to playlists',exact:true}).click();
  await page.getByRole('button',{name:'Open playlist '+orderedName,exact:true}).waitFor();
  check('Panel remains inside native fullscreen, Escape stays unprevented for the browser, reduced motion/narrow layout work, and an open panel prevents idle/edge hiding without remounting media');

  await page.getByRole('button',{name:'Open playlist '+singleName,exact:true}).click();
  await detail.getByRole('button',{name:'Open Playlist video 002',exact:true}).click();await queuePlayer.waitFor();
  await page.waitForFunction(()=>document.querySelector('video')?.currentTime>.1);await page.getByRole('button',{name:'Pause',exact:true}).click();
  assert.equal(await loop().getAttribute('aria-label'),'Loop all');assert.equal(await auto.getAttribute('aria-checked'),'true');
  const singleSrc=await page.locator('video').getAttribute('src');
  await page.locator('video').evaluate(n=>{n.currentTime=n.duration-.12;void n.play();});
  await page.waitForFunction(()=>document.querySelector('video')?.currentTime>.1&&document.querySelector('video')?.currentTime<1&&!document.querySelector('video').paused);
  assert.equal(await page.locator('video').getAttribute('src'),singleSrc);
  await page.getByRole('button',{name:'Pause',exact:true}).click();
  await page.waitForFunction(()=>{const v=document.querySelector('video');return v?.buffered.length&&v.buffered.end(v.buffered.length-1)>=v.duration-.01;});
  const originalLocal=path.resolve(config.mediaLocal,queued[2].job.path),preservedLocal=originalLocal+'.preserved';
  assert(originalLocal.startsWith(path.resolve(config.mediaLocal)+path.sep));assert(!fs.existsSync(preservedLocal));fs.renameSync(originalLocal,preservedLocal);
  try{
    await queueButton.click();await panel.getByRole('button',{name:'Refresh playlist',exact:true}).click();await panel.locator('.queue-members[aria-busy="false"]').waitFor();
    await panel.locator('.current-video .unavailable').waitFor();
    await panel.getByRole('button',{name:'Close playlist videos'}).click();
    await page.locator('video').evaluate(n=>{n.currentTime=n.duration-.12;void n.play();});
    await page.waitForFunction(()=>document.querySelector('video')?.ended);
    await queuePlayer.getByRole('alert').filter({hasText:'No other playable videos'}).waitFor();
    await page.waitForTimeout(220);assert(await page.locator('video').evaluate(n=>n.paused&&n.currentTime>=n.duration-.1));
  }finally{fs.renameSync(preservedLocal,originalLocal);}
  assert.deepEqual(fs.readFileSync(originalLocal),queueBytes);
  await page.getByRole('button',{name:'Back to playlist',exact:true}).click();await queuePlayer.waitFor({state:'detached'});
  await detail.getByRole('button',{name:'Back to playlists',exact:true}).click();
  await page.getByRole('button',{name:'Open playlist Unavailable queue fixture',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('.playlist-detail .playlist-video').length===3);
  assert.equal(await detail.locator('.play-download:not(:disabled)').count(),0);
  assert.equal((await api('/api/youtube/playlists/'+unavailable.id+'/items')).data.total,3);
  await detail.getByRole('button',{name:'Back to playlists',exact:true}).click();
  check('Loop All replays a valid single member; after source loss it stops once with a notice, preserves exact media bytes and retains an entirely unavailable playlist');

  await page.getByRole('button',{name:'Open playlist '+orderedName,exact:true}).click();
  await detail.getByRole('button',{name:'Open Playlist video 000',exact:true}).click();await queuePlayer.waitFor();
  await page.waitForFunction(()=>document.querySelector('video')?.currentTime>.1);await page.getByRole('button',{name:'Pause',exact:true}).click();
  const epochSrc=await page.locator('video').getAttribute('src');
  let releaseNavigation,startNavigation;const navigationHeld=new Promise(resolve=>releaseNavigation=resolve),navigationStarted=new Promise(resolve=>startNavigation=resolve);let holdNavigation=true;
  const delayedNavigation=async route=>{
    if(!holdNavigation)return route.continue();holdNavigation=false;startNavigation();await navigationHeld;
    try{await route.continue();}catch{/* Closing aborts the old owner request. */}
  };
  await page.route('**/api/youtube/playlists/'+ordered.id+'/items?*',delayedNavigation);
  await page.getByRole('button',{name:'Next video',exact:true}).click();await navigationStarted;
  await page.getByRole('button',{name:'Back to playlist',exact:true}).click();await queuePlayer.waitFor({state:'detached'});
  await detail.getByRole('button',{name:'Open Playlist video 002',exact:true}).click();await queuePlayer.waitFor();
  await page.waitForFunction(()=>document.querySelector('video')?.currentTime>.1);
  const newerSrc=await page.locator('video').getAttribute('src');assert.notEqual(newerSrc,epochSrc);
  releaseNavigation();await page.waitForTimeout(160);assert.equal(await page.locator('video').getAttribute('src'),newerSrc);
  await page.unroute('**/api/youtube/playlists/'+ordered.id+'/items?*',delayedNavigation);
  await page.getByRole('button',{name:'Pause',exact:true}).click();
  const changedMembershipUrl='/api/youtube/videos/'+queued[2].job_id+'/playlists';
  const beforeQueueMembership=(await api(changedMembershipUrl)).data;
  const removedQueueMembership=await api(changedMembershipUrl,'POST',{playlist_ids:beforeQueueMembership.playlist_ids.filter(id=>id!==ordered.id),version:beforeQueueMembership.version});
  assert.equal(removedQueueMembership.status,200);
  try{
    await page.getByRole('button',{name:'Next video',exact:true}).click();
    await queuePlayer.getByRole('alert').filter({hasText:'no longer in this playlist'}).waitFor();
    assert.equal(await page.locator('video').getAttribute('src'),newerSrc);
    await queueButton.click();await panel.getByText('This video is no longer in this playlist.',{exact:true}).waitFor();
    assert.equal(await panel.locator('.queue-member').count(),130);
    await panel.getByRole('button',{name:'Close playlist videos'}).click();
  }finally{
    const restoredQueueMembership=await api(changedMembershipUrl,'POST',{playlist_ids:beforeQueueMembership.playlist_ids,version:removedQueueMembership.data.version});
    assert.equal(restoredQueueMembership.status,200);
  }
  assert.equal((await api('/api/youtube/playlists/'+ordered.id+'/items')).data.total,131);
  check('A real membership change while playing is rechecked before navigation, keeps the current source, reports removal and preserves other members');
  await page.evaluate(()=>window.__disabledPlaylistVideo=document.querySelector('video'));
  await page.getByRole('button',{name:'Back to playlist',exact:true}).click();await queuePlayer.waitFor({state:'detached'});
  assert(await page.evaluate(()=>window.__disabledPlaylistVideo.paused));
  assert.equal((await api('/api/suite/modules/youtube/disable','POST')).status,200);
  assert.equal((await api('/api/youtube/playlists/'+ordered.id+'/items')).status,409);
  assert.equal((await api('/api/suite/modules/youtube/enable','POST')).status,200);
  assert.equal((await api('/api/youtube/playlists/'+ordered.id+'/items')).data.total,131);
  assert.deepEqual(fs.readFileSync(originalLocal),queueBytes);
  check('Closing aborts stale playlist navigation; a new selection keeps its source, exit pauses the old video and disable/re-enable preserves every playlist member'+(videoPresent?'':' with Video physically absent'));


  await sections.getByRole('button',{name:'All videos',exact:true}).click();
  await input.fill('A quiet journey');await card.waitFor();await page.waitForTimeout(300);
  const notices=page.getByRole('region',{name:'Notifications'}),noticeRows=notices.locator('.action-notification');
  while(await noticeRows.count())await noticeRows.first().getByRole('button',{name:'Dismiss notification'}).click();
  const gridBefore=await page.locator('.download-grid').boundingBox();
  const cardBefore=await card.boundingBox(),scrollBefore=await page.locator('.youtube-body main').evaluate(n=>n.scrollTop);
  let oldestNotice='',movedNotice='',oldY=0;const noticeStart=Date.now();
  for(let index=0;index<6;index++){
    await selectPlaylists();const choice=membershipDialog.getByRole('checkbox').first();
    await choice.setChecked(!await choice.isChecked());await membershipDialog.getByRole('button',{name:'Save',exact:true}).click();
    await membershipDialog.waitFor({state:'detached'});
    await page.waitForFunction(count=>document.querySelectorAll('.action-notification').length===count,Math.min(index+1,5));
    if(index===0)oldestNotice=await noticeRows.first().getAttribute('data-notification-id');
    if(index===4){movedNotice=await noticeRows.last().getAttribute('data-notification-id');oldY=(await noticeRows.last().boundingBox()).y;}
    assert(await card.evaluate(n=>n===document.activeElement));
  }
  assert(Date.now()-noticeStart<6000,'Six actual Saves must complete inside the notice lifetime');
  const liveIds=await noticeRows.evaluateAll(nodes=>nodes.map(n=>n.dataset.notificationId));
  assert.equal(liveIds.length,5);assert(!liveIds.includes(oldestNotice));assert.equal(new Set(liveIds).size,5);
  await page.waitForTimeout(230);
  assert((await notices.locator('[data-notification-id="'+movedNotice+'"]').boundingBox()).y<oldY-20);
  assert.deepEqual(await page.locator('.download-grid').boundingBox(),gridBefore);
  assert.deepEqual(await card.boundingBox(),cardBefore);
  assert.equal(await page.locator('.youtube-body main').evaluate(n=>n.scrollTop),scrollBefore);
  assert.equal(await page.locator('.membership-notice').count(),0);
  check('Six real membership Saves create at most five notices, older notices move upward, the oldest is replaced and grid/scroll/focus stay fixed');

  const heldNotice=noticeRows.last(),heldId=await heldNotice.getAttribute('data-notification-id');
  await heldNotice.hover();await page.waitForTimeout(6400);
  assert.equal(await noticeRows.count(),1);assert.equal(await noticeRows.first().getAttribute('data-notification-id'),heldId);
  await heldNotice.getByRole('button',{name:'Dismiss notification'}).focus();await page.mouse.move(600,120);await page.waitForTimeout(400);
  assert.equal(await noticeRows.count(),1);
  await input.focus();await page.waitForTimeout(6100);assert.equal(await noticeRows.count(),0);
  check('Real six-second expiry pauses independently for hover and keyboard focus, resumes afterwards and leaves no stale notices');

  await selectPlaylists();await membershipDialog.getByRole('button',{name:'Save',exact:true}).click();await membershipDialog.waitFor({state:'detached'});
  const retainedId=await noticeRows.first().getAttribute('data-notification-id');
  await page.getByRole('button',{name:'Open Keivotos menu'}).click();await page.locator('.app-drawer').getByRole('button',{name:'Settings',exact:true}).click();
  const noticeSettings=page.getByRole('dialog',{name:'Settings',exact:true});
  await noticeSettings.getByRole('navigation',{name:'Settings sections'}).getByRole('button',{name:'Appearance',exact:true}).first().click();
  for(const corner of ['bottom-left','top-left','top-right','bottom-right']){
    await page.getByLabel('Notification position',{exact:true}).selectOption(corner);await page.waitForTimeout(60);
    assert.equal(await notices.getAttribute('data-position'),corner);
    assert.equal(await noticeRows.first().getAttribute('data-notification-id'),retainedId);
    const box=await notices.boundingBox();
    assert(corner.endsWith('left')?box.x<30:box.x>1000);
    assert(corner.startsWith('top')?box.y<30:box.y>700);
  }
  await page.getByLabel('Search settings',{exact:true}).fill('notification position');
  await noticeSettings.getByRole('button',{name:/Notification position/}).click();await page.getByLabel('Notification position',{exact:true}).waitFor();
  await page.getByLabel('Notification position',{exact:true}).selectOption('top-left');
  await page.getByRole('button',{name:'Close settings',exact:true}).click();
  await page.setViewportSize({width:360,height:780});
  const narrow=await notices.boundingBox();assert(narrow.x>=0&&narrow.x+narrow.width<=360);
  await page.screenshot({path:path.join(config.output,'notifications-corner.png')});
  await page.setViewportSize({width:1440,height:1000});await page.reload();await input.waitFor();
  assert.equal(await notices.getAttribute('data-position'),'top-left');assert.equal(await noticeRows.count(),0);
  assert(await notices.evaluate(n=>{const r=n.getBoundingClientRect();return !n.contains(document.elementFromPoint(r.x+r.width/2,r.y+2));}));
  check('All four corners reposition existing notices, Settings search finds the control, narrow geometry fits and reload retains only the corner');

  await input.fill('A quiet journey');await card.waitFor();await page.waitForTimeout(250);
  await page.emulateMedia({reducedMotion:'reduce'});
  await selectPlaylists();await membershipDialog.getByRole('button',{name:'Save',exact:true}).click();await membershipDialog.waitFor({state:'detached'});
  assert(await noticeRows.first().evaluate(n=>n.getAnimations().every(a=>a.playState!=='running')));
  await noticeRows.first().getByRole('button',{name:'Dismiss notification'}).click();assert.equal(await noticeRows.count(),0);
  await selectPlaylists();await membershipDialog.getByRole('button',{name:'Save',exact:true}).click();await membershipDialog.waitFor({state:'detached'});
  await card.locator('.play-download').click();const notificationPlayer=page.getByRole('dialog',{name:'YouTube player',exact:true});
  await notificationPlayer.waitFor();await notificationPlayer.getByRole('button',{name:'Fullscreen',exact:true}).click();
  await page.waitForFunction(()=>!!document.fullscreenElement);
  assert(await notices.evaluate(n=>document.fullscreenElement.contains(n)));
  assert.equal(await noticeRows.count(),1);
  await notificationPlayer.getByRole('button',{name:'Fullscreen',exact:true}).click();await page.waitForFunction(()=>!document.fullscreenElement);
  assert(await notices.evaluate(n=>!n.closest('.video-viewer')));
  await notices.evaluate(n=>window.__notificationHost=n);
  await notificationPlayer.getByRole('button',{name:'Fullscreen',exact:true}).click();await page.waitForFunction(()=>!!document.fullscreenElement);
  await notificationPlayer.getByRole('button',{name:'Back to downloads',exact:true}).click();await notificationPlayer.waitFor({state:'detached'});await page.waitForFunction(()=>!document.fullscreenElement);
  await notices.waitFor();assert(await notices.evaluate(n=>n===window.__notificationHost&&!n.closest('.video-viewer')));
  if(await noticeRows.count())await noticeRows.first().getByRole('button',{name:'Dismiss notification'}).click();assert.equal(await noticeRows.count(),0);
  await page.emulateMedia({reducedMotion:'no-preference'});
  check('Reduced motion suppresses entry animation; the same host enters/exits native fullscreen, survives closing the fullscreen viewer and permits manual dismissal');

  // Combined 19–21: five real references, unchanged order, accent and management.
  await sections.getByRole('button',{name:'Playlists',exact:true}).click();
  const fiveName='Five ordered videos',five=(await api('/api/youtube/playlists','POST',{title:fiveName,request_id:await page.evaluate(()=>crypto.randomUUID())})).data;
  const fiveIds=[0,2,3,4,5].map(index=>queued[index].job_id);
  for(const identity of fiveIds){const m=(await api('/api/youtube/videos/'+identity+'/playlists')).data;assert.equal((await api('/api/youtube/videos/'+identity+'/playlists','POST',{playlist_ids:[...m.playlist_ids,five.id],version:m.version})).status,200);}
  await page.reload();await page.getByRole('button',{name:'Open Keivotos menu'}).click();await page.locator('.app-drawer').getByRole('button',{name:'YouTube',exact:true}).click();
  await sections.getByRole('button',{name:'Playlists',exact:true}).click();
  async function centered(control){const {b,s}=await control.evaluate(n=>({b:n.getBoundingClientRect().toJSON(),s:n.querySelector('svg').getBoundingClientRect().toJSON()}));assert(Math.abs(b.x+b.width/2-s.x-s.width/2)<.6,JSON.stringify({button:b,svg:s}));assert(Math.abs(b.y+b.height/2-s.y-s.height/2)<.6,JSON.stringify({button:b,svg:s}));assert(b.width>=32&&b.height>=32);}
  await centered(page.getByRole('button',{name:'New playlist',exact:true}));
  await page.getByRole('button',{name:'New playlist',exact:true}).focus();await page.keyboard.press('Enter');await page.getByRole('dialog',{name:'New playlist',exact:true}).waitFor();await page.keyboard.press('Escape');
  await page.getByRole('button',{name:'Open playlist '+fiveName,exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('.playlist-detail .playlist-video').length===5);
  await centered(detail.getByRole('button',{name:'Back to playlists',exact:true}));
  await detail.getByRole('button',{name:'Back to playlists',exact:true}).focus();await page.keyboard.press('Enter');await page.getByRole('button',{name:'Open playlist '+fiveName,exact:true}).waitFor();
  async function appearance(accent,scale='default'){
   await page.getByRole('button',{name:'Open Keivotos menu'}).click();await page.locator('.app-drawer').getByRole('button',{name:'Settings',exact:true}).click();
   const d=page.getByRole('dialog',{name:'Settings',exact:true});await d.getByRole('navigation',{name:'Settings sections'}).getByRole('button',{name:'Appearance',exact:true}).first().click();
   await page.getByLabel('Accent style',{exact:true}).selectOption(accent);await page.locator('#setting-interface-scale').getByRole('button',{name:scale==='comfortable'?'Comfortable':'Default',exact:true}).click();await page.getByRole('button',{name:'Close settings',exact:true}).click();if(await page.locator('.app-drawer-layer').count()){await page.keyboard.press('Escape');await page.locator('.app-drawer-layer').waitFor({state:'detached'});}
  }
  await appearance('green','comfortable');await centered(page.getByRole('button',{name:'New playlist',exact:true}));
  await page.getByRole('button',{name:'Open playlist '+fiveName,exact:true}).click();await detail.locator('.download-grid[aria-busy="false"]').waitFor();await centered(detail.getByRole('button',{name:'Back to playlists',exact:true}));
  check('SVG Plus and Back remain centered at Default/Comfortable scale with preserved keyboard actions and click areas');
  await detail.locator('[data-playlist-job-id="'+fiveIds[0]+'"] .play-download').click();await queuePlayer.waitFor();
  await page.waitForFunction(()=>document.querySelector('video')?.currentTime>.1);await page.getByRole('button',{name:'Pause',exact:true}).click();
  if(await auto.getAttribute('aria-checked')==='true')await auto.click();for(let i=0;await loop().getAttribute('aria-label')!=='Loop off'&&i<3;i++)await loop().click();
  await queueButton.click();await page.waitForTimeout(340);
  const orderedIds=()=>panel.locator('.queue-member').evaluateAll(nodes=>nodes.map(n=>n.dataset.playlistJobId));
  assert.deepEqual(await orderedIds(),fiveIds);assert.equal(await panel.locator('.current-video img').count(),1);
  let greenColor='';
  for(const identity of [fiveIds[0],fiveIds[2],fiveIds[4]]){
   if(identity!==fiveIds[0]){await panel.locator('[data-playlist-job-id="'+identity+'"]').click();await page.waitForFunction(()=>document.querySelector('video')?.currentTime>.1);await page.getByRole('button',{name:'Pause',exact:true}).click();}
   assert.deepEqual(await orderedIds(),fiveIds);const row=panel.locator('.queue-member[aria-current="true"]');assert.equal(await row.count(),1);assert.equal(await row.getAttribute('data-playlist-job-id'),identity);
   const color=await row.evaluate(n=>({border:getComputedStyle(n).borderTopColor,accent:getComputedStyle(document.documentElement).getPropertyValue('--accent').trim(),color:getComputedStyle(n).backgroundColor}));
   greenColor=color.border;assert(color.border.includes('rgb'));assert.notEqual(color.color,'rgb(27, 27, 38)');
  }
  await page.screenshot({path:path.join(config.output,'ordered-five-green.png')});
  await panel.getByRole('button',{name:'Close playlist videos'}).click();await page.getByRole('button',{name:'Back to playlist',exact:true}).click();await queuePlayer.waitFor({state:'detached'});
  await appearance('cyan');await detail.locator('[data-playlist-job-id="'+fiveIds[2]+'"] .play-download').click();await queuePlayer.waitFor();await page.waitForFunction(()=>document.querySelector('video')?.currentTime>.1);await page.getByRole('button',{name:'Pause',exact:true}).click();await queueButton.click();await page.waitForTimeout(340);
  assert.notEqual(await panel.locator('.queue-member[aria-current="true"]').evaluate(n=>getComputedStyle(n).borderTopColor),greenColor);assert.deepEqual(await orderedIds(),fiveIds);
  await page.setViewportSize({width:360,height:780});const smallPanel=await panel.boundingBox();assert(smallPanel.x>=0&&smallPanel.x+smallPanel.width<=360);assert.equal(await panel.locator('.queue-member').count(),5);await page.setViewportSize({width:1440,height:1000});
  await panel.getByRole('button',{name:'Close playlist videos'}).click();await page.getByRole('button',{name:'Back to playlist',exact:true}).click();await queuePlayer.waitFor({state:'detached'});
  check('Large preview stays above all five ordered rows; first/middle/last only change the accent-highlighted current row, including real accent and narrow-layout changes');
  const playlistMenu=page.getByRole('menu',{name:/Playlist actions/});
  const containingCard=detail.locator('[data-playlist-job-id="'+fiveIds[2]+'"] .play-download');
  await containingCard.click({button:'right'});assert.equal(await playlistMenu.getAttribute('aria-label'),'Playlist actions for '+fiveName);assert.deepEqual(await playlistMenu.getByRole('menuitem').allTextContents(),['Rename playlist','Delete playlist']);await page.keyboard.press('Escape');assert(await containingCard.evaluate(n=>n===document.activeElement));
  for(const xy of [[1,1],[1439,1],[1,999],[1439,999]]){await detail.dispatchEvent('contextmenu',{clientX:xy[0],clientY:xy[1]});await playlistMenu.waitFor();const b=await playlistMenu.boundingBox();assert(b.x>=7&&b.y>=7&&b.x+b.width<=1433&&b.y+b.height<=993);await page.keyboard.press('ArrowDown');assert.equal(await page.locator(':focus').textContent(),'Delete playlist');await page.keyboard.press('Home');assert.equal(await page.locator(':focus').textContent(),'Rename playlist');await page.keyboard.press('Escape');}
  await detail.focus();await page.keyboard.press('Shift+F10');await playlistMenu.getByRole('menuitem',{name:'Rename playlist'}).click();
  const renameDialog=page.getByRole('dialog',{name:'Rename playlist',exact:true}),renameTitle=renameDialog.getByLabel('Title',{exact:true});await renameDialog.waitFor();assert.equal(await renameTitle.inputValue(),fiveName);await renameTitle.fill('Cancelled');await page.keyboard.press('Escape');assert.equal((await api('/api/youtube/playlists/'+five.id+'/items')).data.playlist.title,fiveName);assert(await detail.evaluate(n=>n===document.activeElement));
  check('Right-click on playlist videos/background and keyboard invocation target the containing playlist; menu clamps at all corners, navigates and restores focus; Rename cancel preserves the title');
  await containingCard.focus();await page.keyboard.press('Shift+F10');await playlistMenu.getByRole('menuitem',{name:'Rename playlist'}).click();await renameDialog.waitFor();await renameTitle.fill('Updated 공부 playlist');
  const renameUrl='**/api/youtube/playlists/'+five.id+'/rename';let renamePayloads=[];
  const lostRename=async route=>{renamePayloads.push(route.request().postDataJSON());await route.fetch();await route.abort('failed');};await page.route(renameUrl,lostRename);
  await renameDialog.getByRole('button',{name:'Save',exact:true}).click();await renameDialog.getByRole('alert').waitFor();assert.equal(await renameTitle.inputValue(),'Updated 공부 playlist');await page.unroute(renameUrl,lostRename);
  const recordRename=async route=>{renamePayloads.push(route.request().postDataJSON());await route.continue();};await page.route(renameUrl,recordRename);await renameDialog.getByRole('button',{name:'Save',exact:true}).click();await renameDialog.waitFor({state:'detached'});await page.unroute(renameUrl,recordRename);assert.deepEqual(renamePayloads[0],renamePayloads[1]);await detail.getByRole('heading',{name:'Updated 공부 playlist',exact:true}).waitFor();assert(await containingCard.evaluate(n=>n===document.activeElement));
  await detail.focus();await page.keyboard.press('Shift+F10');await playlistMenu.getByRole('menuitem',{name:'Rename playlist'}).click();await renameDialog.waitFor();await renameTitle.fill('Retained pending title');
  let fresh=(await api('/api/youtube/playlists/'+five.id+'/items')).data.playlist;assert.equal((await api('/api/youtube/playlists/'+five.id+'/rename','POST',{title:'External update',revision:fresh.revision,request_id:await page.evaluate(()=>crypto.randomUUID())})).status,200);
  await renameDialog.getByRole('button',{name:'Save',exact:true}).click();await renameDialog.getByRole('alert').filter({hasText:'changed'}).waitFor();assert.equal(await renameTitle.inputValue(),'Retained pending title');assert(await renameDialog.getByRole('button',{name:'Save',exact:true}).isDisabled());await renameDialog.getByRole('button',{name:'Reload playlist',exact:true}).click();await renameDialog.getByRole('button',{name:'Save',exact:true}).waitFor({state:'visible'});await page.waitForFunction(()=>!document.querySelector('.playlist-rename-dialog .save').disabled);assert.equal(await renameTitle.inputValue(),'Retained pending title');await renameDialog.getByRole('button',{name:'Save',exact:true}).click();await renameDialog.waitFor({state:'detached'});await detail.getByRole('heading',{name:'Retained pending title',exact:true}).waitFor();
  check('Rename reconciles title immediately, repeats the exact token after a lost acknowledgement, and retains typed text through a real conflict and explicit reload');
  await detail.getByRole('button',{name:'Back to playlists',exact:true}).click();const fiveCard=page.getByRole('button',{name:'Open playlist Retained pending title',exact:true});await fiveCard.waitFor();await fiveCard.click({button:'right'});await playlistMenu.getByRole('menuitem',{name:'Delete playlist'}).click();
  const deleteDialog=page.getByRole('dialog',{name:'Delete playlist',exact:true});await deleteDialog.getByText('Delete “Retained pending title” with 5 videos?',{exact:true}).waitFor();assert(await deleteDialog.getByRole('button',{name:'Cancel',exact:true}).evaluate(n=>n===document.activeElement));await page.keyboard.press('Escape');assert(await fiveCard.evaluate(n=>n===document.activeElement));assert.equal((await api('/api/youtube/playlists/'+five.id+'/items')).status,200);
  await fiveCard.click();await detail.focus();await page.keyboard.press('Shift+F10');await playlistMenu.getByRole('menuitem',{name:'Delete playlist'}).click();await deleteDialog.waitFor();
  const extra=queued[6].job_id,extraMembership=(await api('/api/youtube/videos/'+extra+'/playlists')).data;assert.equal((await api('/api/youtube/videos/'+extra+'/playlists','POST',{playlist_ids:[...extraMembership.playlist_ids,five.id],version:extraMembership.version})).status,200);
  await deleteDialog.getByRole('button',{name:'Delete playlist',exact:true}).click();await deleteDialog.getByRole('alert').filter({hasText:'changed'}).waitFor();assert(await deleteDialog.getByRole('button',{name:'Delete playlist',exact:true}).isDisabled());await deleteDialog.getByRole('button',{name:'Reload playlist',exact:true}).click();await deleteDialog.getByText('Delete “Retained pending title” with 6 videos?',{exact:true}).waitFor();await page.waitForFunction(()=>!document.querySelector('.playlist-delete-dialog .delete').disabled);
  const retainedMedia=new Map();for(const identity of [...fiveIds,extra]){const job=(await api('/api/youtube/playlists/'+five.id+'/items')).data.items.find(row=>row.job_id===identity).job;for(const name of [job.path,job.thumbnail]){const file=path.join(config.mediaLocal,...name.split('/'));retainedMedia.set(file,fs.readFileSync(file));}}
  let archivePayloads=[];const archiveUrl='**/api/youtube/playlists/'+five.id+'/archive',lostArchive=async route=>{archivePayloads.push(route.request().postDataJSON());await route.fetch();await route.abort('failed');};await page.route(archiveUrl,lostArchive);await deleteDialog.getByRole('button',{name:'Delete playlist',exact:true}).click();await deleteDialog.getByRole('alert').waitFor();await page.unroute(archiveUrl,lostArchive);
  const recordArchive=async route=>{archivePayloads.push(route.request().postDataJSON());await route.continue();};await page.route(archiveUrl,recordArchive);await deleteDialog.getByRole('button',{name:'Delete playlist',exact:true}).click();await deleteDialog.waitFor({state:'detached'});await page.unroute(archiveUrl,recordArchive);assert.deepEqual(archivePayloads[0],archivePayloads[1]);await page.locator('.playlists-view').waitFor();assert.equal(await fiveCard.count(),0);assert.equal((await api('/api/youtube/playlists/'+five.id+'/items')).status,404);for(const [file,bytes] of retainedMedia)assert.deepEqual(fs.readFileSync(file),bytes);
  for(const identity of fiveIds)assert(!(await api('/api/youtube/videos/'+identity+'/playlists')).data.playlist_ids.includes(five.id));
  assert(await page.getByRole('button',{name:'New playlist',exact:true}).evaluate(n=>n===document.activeElement));
  await sections.getByRole('button',{name:'All videos',exact:true}).click();await input.fill('Playlist video 000');await page.waitForFunction(()=>document.querySelectorAll('.download-grid [data-job-id]').length===1&&document.querySelector('.download-grid')?.getAttribute('aria-busy')==='false');const membershipCard=page.locator('[data-job-id="'+fiveIds[0]+'"]');await membershipCard.scrollIntoViewIfNeeded();await membershipCard.focus();await page.waitForTimeout(60);await page.keyboard.press('Shift+F10');await page.getByRole('menu').getByRole('menuitem',{name:'Add to playlists'}).click();await membershipDialog.waitFor();assert.equal(await membershipDialog.getByText('Retained pending title',{exact:true}).count(),0);await page.keyboard.press('Escape');
  check('Delete menus on list cards/detail show title and count, default to Cancel, reject stale counts, retry lost acknowledgements exactly, hide the playlist/picker immediately and preserve all generated media/thumbnail bytes');
  const reconcile=(await api('/api/youtube/playlists','POST',{title:'Player reconciliation',request_id:await page.evaluate(()=>crypto.randomUUID())})).data;
  for(const identity of fiveIds.slice(0,2)){const m=(await api('/api/youtube/videos/'+identity+'/playlists')).data;assert.equal((await api('/api/youtube/videos/'+identity+'/playlists','POST',{playlist_ids:[...m.playlist_ids,reconcile.id],version:m.version})).status,200);}
  await sections.getByRole('button',{name:'Playlists',exact:true}).click();await page.getByRole('button',{name:'Open playlist Player reconciliation',exact:true}).click();await detail.locator('.download-grid[aria-busy="false"]').waitFor();await detail.locator('[data-playlist-job-id="'+fiveIds[0]+'"] .play-download').click();await queuePlayer.waitFor();await page.waitForFunction(()=>document.querySelector('video')?.currentTime>.1);await page.getByRole('button',{name:'Pause',exact:true}).click();await queueButton.click();await page.waitForTimeout(340);
  const reconcileSrc=await page.locator('video').getAttribute('src');await page.locator('video').evaluate(n=>window.__reconcileVideo=n);
  fresh=(await api('/api/youtube/playlists/'+reconcile.id+'/items')).data.playlist;assert.equal((await api('/api/youtube/playlists/'+reconcile.id+'/rename','POST',{title:'Renamed during playback',revision:fresh.revision,request_id:await page.evaluate(()=>crypto.randomUUID())})).status,200);await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await panel.getByRole('heading',{name:'Renamed during playback',exact:true}).waitFor();assert.equal(await page.locator('video').getAttribute('src'),reconcileSrc);assert(await page.locator('video').evaluate(n=>n===window.__reconcileVideo));
  fresh=(await api('/api/youtube/playlists/'+reconcile.id+'/items')).data.playlist;assert.equal((await api('/api/youtube/playlists/'+reconcile.id+'/archive','POST',{revision:fresh.revision,item_count:fresh.item_count,request_id:await page.evaluate(()=>crypto.randomUUID())})).status,200);await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await panel.getByRole('alert').getByText('Playlist is unavailable.',{exact:true}).waitFor();assert.equal(await panel.locator('.queue-member').count(),0);assert.equal(await page.locator('video').getAttribute('src'),reconcileSrc);assert(await queuePlayer.getByRole('button',{name:'Next video',exact:true}).isDisabled());await panel.getByRole('button',{name:'Close playlist videos'}).click();await page.getByRole('button',{name:'Back to playlist',exact:true}).click();await queuePlayer.waitFor({state:'detached'});await detail.getByRole('button',{name:'Back to playlists',exact:true}).click();assert.equal(await page.getByRole('button',{name:'Open playlist Renamed during playback',exact:true}).count(),0);
  check('Focus reconciliation refreshes a renamed player panel without remounting media and rejects an externally archived queue while preserving current playback and Back navigation');
  assert.deepEqual(report.errors,[]);
 }finally{fs.writeFileSync(path.join(config.output,'report.json'),JSON.stringify(report,null,2));await context.tracing.stop({path:path.join(config.output,'trace.zip')});await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

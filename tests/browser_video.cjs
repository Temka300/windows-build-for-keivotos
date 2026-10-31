const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
(async()=>{
 const browser=await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});
 const page=await context.newPage();page.setDefaultTimeout(15000);
 const report={checks:[],errors:[],failures:[]};page.on('pageerror',e=>report.errors.push(e.message));
 page.on('request',r=>{if(r.url().endsWith('/api/video/playback-error'))report.failures.push(r.postDataJSON());});
 await context.tracing.start({screenshots:true,snapshots:true});
 const check=x=>{report.checks.push(x);console.log('PASS: '+x);};
 async function api(url,method='GET',body){return page.evaluate(async({url,method,body})=>{const r=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});return {status:r.status,data:await r.json()};},{url,method,body});}
 async function seekToEnd(){
  await page.waitForFunction(()=>{const video=document.querySelector('video');return video&&video.readyState>=1&&Number.isFinite(video.duration)&&video.duration>0;});
  await page.locator('video').evaluate(async video=>{video.currentTime=Math.max(0,video.duration-.15);if(video.paused)await video.play();});
 }
 const cursor=()=>page.locator('video').evaluate(node=>getComputedStyle(node).cursor);
 async function playerHint(control,text,mode='hover'){
  const tip=page.locator('#'+await control.getAttribute('aria-describedby'));
  await page.locator('.video-viewer').focus();await page.mouse.move(180,180);await tip.waitFor({state:'hidden'});
  if(mode==='focus')await control.focus();else{await control.locator('..').hover();await page.waitForTimeout(80);assert.equal(await tip.isVisible(),false);}
  await tip.waitFor({state:'visible'});assert.equal(await tip.innerText(),text);
  const box=await tip.boundingBox(),viewport=page.viewportSize();assert(box.x>=0&&box.y>=0&&box.x+box.width<=viewport.width&&box.y+box.height<=viewport.height,JSON.stringify({box,viewport}));
  assert(await tip.evaluate(node=>getComputedStyle(node).pointerEvents==='none'));
  return tip;
 }

 async function playTitle(name){
  await page.getByRole('button',{name,exact:true}).click();
  const info=page.getByRole('dialog',{name:name+' episodes',exact:true});await info.waitFor();
  await info.getByRole('button',{name:name.replace(/\.(mp4|m4v|webm|mkv|mov|avi|ogv)$/i,''),exact:true}).click();
 }
 async function back(){
  await page.getByRole('button',{name:'Back to episodes',exact:true}).click();
  await page.getByRole('dialog',{name:'Video player',exact:true}).waitFor({state:'detached'});
  await page.getByRole('button',{name:'Back to videos',exact:true}).click();
  assert.notEqual(await page.locator('body').evaluate(node=>getComputedStyle(node).cursor),'none');
 }

 try{
  if(config.continuation){
    const saved=config.continuation;
    await page.addInitScript(()=>{const original=HTMLMediaElement.prototype.play;HTMLMediaElement.prototype.play=function(){return window.__freezePlayer?Promise.resolve():original.call(this);};window.__freezePlayer=true;});
    await page.goto(config.url);
    await page.getByRole('button',{name:'Open Keivotos menu'}).click();await page.locator('.app-drawer').getByRole('button',{name:'Video',exact:true}).click();
    await page.getByRole('textbox',{name:'Search videos',exact:true}).fill('Resume');
    await page.getByRole('button',{name:'Resume',exact:true}).click();
    const row=page.locator('[data-episode-path="Resume/Episode 2.mp4"]');
    await row.getByRole('progressbar').waitFor();assert(Math.abs(Number(await row.getByRole('progressbar').getAttribute('aria-valuenow'))-saved.position)<.01);
    await row.click();await page.waitForFunction(()=>document.querySelector('video')?.readyState>=1);
    await page.waitForFunction(value=>Math.abs(document.querySelector('video').currentTime-value)<.1,saved.position);
    const state=(await api('/api/video/progress/read','POST',saved.item)).data;
    assert.equal(state.position.position,saved.position);assert.equal(state.position.sequence,saved.sequence);
    check('Fresh browser and restarted server retain the row bar and restore the confirmed time without writing from metadata');
    if(saved.metadata){
      await page.getByRole('button',{name:'Back to episodes',exact:true}).click();await page.getByRole('button',{name:'Back to videos',exact:true}).click();await page.getByRole('textbox',{name:'Search videos',exact:true}).fill('Original Series');await page.getByRole('button',{name:'Original Series',exact:true}).click();
      const info=page.getByRole('dialog',{name:'Original Series episodes',exact:true});await info.getByText(saved.metadata.fields.summary,{exact:true}).waitFor();const current=await api('/api/video/metadata?'+new URLSearchParams(saved.metadata.subject));assert.deepEqual(current.data.fields,saved.metadata.fields);assert.equal(current.data.version,saved.metadata.version);assert.equal(await info.locator('.metadata-details img').count(),0);
      check('Tags/description, personal choices and unchanged folder names survive a fresh browser/server alongside watch progress');
    }

    assert.deepEqual(report.errors,[]);return;
  }
  await page.addInitScript(()=>{if(!sessionStorage.getItem('speed-fixture')){localStorage.setItem('keivotos:video-playback-speed',JSON.stringify('fast'));localStorage.setItem('keivotos:youtube-playback-speed','1.25');sessionStorage.setItem('speed-fixture','1');}});
  await page.goto(config.url);
  assert.equal((await api('/api/video/library')).status,409);
  assert.equal((await api('/api/suite/modules/video/enable','POST')).status,200);
  assert.equal((await api('/api/suite/modules/youtube/enable','POST')).status,200);
  const add=await api('/api/suite/folders/apply','POST',{folders:[{path:config.media,display_name:'Videos',role:'video'}]});assert.equal(add.status,200,JSON.stringify(add));
  const library=await api('/api/video/library');assert.equal(library.data.total,5);assert.equal(library.data.items.some(x=>x.name==='notes.txt'),false);
  await page.reload();
  const folderPath=page.getByRole('navigation',{name:'Current folder'});
  await folderPath.getByRole('button',{name:'Videos',exact:true}).waitFor();
  const searchBounds=await page.getByPlaceholder('Search this folder…').boundingBox();
  const pathBounds=await folderPath.boundingBox();
  const fileAreaBounds=await page.locator('section > div.flex-1').first().boundingBox();
  assert(searchBounds.y+searchBounds.height<=pathBounds.y);
  assert(pathBounds.y+pathBounds.height<=fileAreaBounds.y);
  await folderPath.getByRole('button',{name:'Videos',exact:true}).click();
  check('Files folder path sits below Search and above the file area; source navigation remains clickable');
  await page.getByRole('button',{name:'Open Keivotos menu'}).click();await page.locator('.app-drawer').getByRole('button',{name:'Video',exact:true}).click();
  await page.getByRole('button',{name:'sample.mp4',exact:true}).waitFor();
  assert.equal(await page.getByRole('navigation',{name:'Video categories',exact:true}).count(),0);
  assert.equal(await page.getByRole('button',{name:'Playlist videos',exact:true}).count(),0);
  const sampleTile=page.getByRole('button',{name:'sample.mp4',exact:true});
  const otherTile=page.getByRole('button',{name:'sample.webm',exact:true});
  const videoTitle=page.locator('.video-tile[title="sample.mp4"] .video-name');
  for(const width of [1440,360]){
   await page.setViewportSize({width,height:1000});
   for(const text of ['sample.mp4','静かな谷を越えて旅を続ける物語.mp4','A very long video title that needs more than two rows in the library.mp4','unbroken'.repeat(18)+'.mp4']){
    await videoTitle.evaluate((node,value)=>node.textContent=value,text);
    const metrics=await videoTitle.evaluate(node=>({height:node.getBoundingClientRect().height,width:node.getBoundingClientRect().width,card:node.closest('button').getBoundingClientRect().width,font:parseFloat(getComputedStyle(node).fontSize),clamp:getComputedStyle(node).webkitLineClamp,wrap:getComputedStyle(node).overflowWrap}));
    assert.equal(metrics.clamp,'2');assert.equal(metrics.wrap,'anywhere');
    assert(Math.abs(metrics.height-metrics.font*2.7)<1);assert(metrics.width<=metrics.card);
   }
  }
  await videoTitle.evaluate(node=>node.textContent='sample.mp4');
  await page.setViewportSize({width:1440,height:1000});
  assert.equal(await sampleTile.getAttribute('title'),'sample.mp4');
  check('Video titles reserve two rows and contain Japanese, long Latin and unbroken names at both widths');
  await sampleTile.click({button:'right'});
  let locationMenu=page.getByRole('menu',{name:'Location for sample.mp4'});
  await locationMenu.getByRole('menuitem',{name:'Show in Files'}).waitFor();
  await page.waitForFunction(()=>!document.querySelector('[role="menuitem"]')?.disabled);
  await page.keyboard.press('ArrowDown');
  assert(await locationMenu.getByRole('menuitem',{name:'Open in Explorer'}).evaluate(node=>node===document.activeElement));
  await page.keyboard.press('Escape');
  assert(await sampleTile.evaluate(node=>node===document.activeElement));
  await sampleTile.focus();await page.keyboard.press('Shift+F10');
  await page.getByRole('menu',{name:'Location for sample.mp4'}).waitFor();
  await page.keyboard.press('Escape');
  for(const [x,y] of [[2,2],[1438,2],[2,998],[1438,998]]){
    await sampleTile.dispatchEvent('contextmenu',{clientX:x,clientY:y,bubbles:true,cancelable:true});
    const bounds=await page.getByRole('menu',{name:'Location for sample.mp4'}).boundingBox();
    assert(bounds.x>=7&&bounds.y>=7&&bounds.x+bounds.width<=1433&&bounds.y+bounds.height<=993);
    await page.keyboard.press('Escape');
  }
  await sampleTile.click({button:'right'});
  await page.locator('.module-title').click();
  assert.equal(await page.getByRole('menu').count(),0);
  await sampleTile.click({button:'right'});
  await page.locator('.video-surface main').dispatchEvent('scroll');
  assert.equal(await page.getByRole('menu').count(),0);
  check('shared menu opens from keyboard, stays within all screen corners, and dismisses outside or on scroll');
  let revealed=null;
  const revealRoute=async route=>{revealed=route.request().postDataJSON();await route.fulfill({json:{status:'revealed'}});};
  await page.route('**/api/files/reveal',revealRoute);
  await otherTile.click({button:'right'});
  await sampleTile.click({button:'right'});
  locationMenu=page.getByRole('menu',{name:'Location for sample.mp4'});
  await locationMenu.getByRole('menuitem',{name:'Open in Explorer'}).click();
  assert.deepEqual(revealed,{source_id:add.data.sources.find(x=>x.role==='video').source_id,path:'sample.mp4'});
  assert(await sampleTile.evaluate(node=>node===document.activeElement));
  await page.unroute('**/api/files/reveal',revealRoute);
  await sampleTile.click({button:'right'});
  await page.getByRole('menu',{name:'Location for sample.mp4'}).getByRole('menuitem',{name:'Show in Files'}).click();
  const filesTile=page.locator('[data-files-entry-path="sample.mp4"]');
  await filesTile.waitFor();
  await page.waitForFunction(()=>document.querySelector('[data-files-entry-path="sample.mp4"]')===document.activeElement);
  await page.getByRole('button',{name:'Open Keivotos menu'}).click();
  await page.locator('.app-drawer').getByRole('button',{name:'Video',exact:true}).click();
  await sampleTile.waitFor();
  check('Video location menu supports keyboard focus, exact Explorer target, second right-click replacement and Show in Files');
  await page.getByLabel('Search videos').fill('sample.webm');await page.waitForTimeout(400);
  assert.equal(await page.locator('.video-tile').count(),1);
  await page.getByLabel('Search videos').fill('');await page.waitForTimeout(400);
  await page.screenshot({path:path.join(config.output,'library.png')});
  check('Video enables, adopts a folder, filters indexed media and searches names');
  for(const name of ['sample.mp4','sample.m4v','sample.webm']){
   await playTitle(name);
   await page.waitForFunction(()=>document.querySelector('video')?.currentTime>0.1);
   const rect=await page.locator('.video-viewer').boundingBox();assert.equal(rect.width,1440);assert.equal(rect.height,1000);
   await page.getByRole('button',{name:'Pause',exact:true}).click();assert(await page.locator('video').evaluate(v=>v.paused));
   await page.getByLabel('Seek',{exact:true}).fill('2');await page.getByLabel('Seek',{exact:true}).dispatchEvent('input');
   await page.waitForFunction(()=>document.querySelector('video').currentTime>=1.9);
   await page.getByRole('button',{name:'Play',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('video').paused);
   await page.getByRole('button',{name:'Fullscreen',exact:true}).click();await page.waitForFunction(()=>!!document.fullscreenElement);
   await page.getByRole('button',{name:'Fullscreen',exact:true}).click();await page.waitForFunction(()=>!document.fullscreenElement);
   await page.screenshot({path:path.join(config.output,'player-'+name+'.png')});await back();
  }
  check('real H.264 MP4/M4V and VP8 WebM decode, pause, seek, resume and enter/exit fullscreen in a full-browser player');
  await playTitle('sample.mp4');
  const viewer=page.getByRole('dialog',{name:'Video player'}),video=page.locator('video');
  await page.waitForFunction(()=>document.querySelector('video')?.currentTime>0.1);
  await page.getByRole('button',{name:'Pause',exact:true}).click();
  const speedButton=page.getByRole('button',{name:'Playback speed',exact:true});
  const speeds=page.getByRole('group',{name:'Playback speeds',exact:true});
  assert.equal(await video.evaluate(node=>node.playbackRate),1);
  // Temporary rates never become normal-speed preferences.
  async function holdBase(rate){
   await video.evaluate(node=>node.pause());await speedButton.click();await speeds.getByRole('button',{name:rate===1?'1.0x':rate+'x',exact:true}).click();await viewer.focus();await page.waitForFunction(rate=>document.querySelector('video').playbackRate===rate,rate);
  }
  async function beginHold(){await video.evaluate(async node=>{node.currentTime=.1;await node.play();});await viewer.focus();await page.keyboard.down('Space');await page.waitForFunction(()=>document.querySelector('video').playbackRate!==Number(JSON.parse(localStorage.getItem('keivotos:video-playback-speed'))));}
  report.holdTimings=[];
  for(const base of [1,1.25,2]){
   await holdBase(base);await beginHold();
   assert.equal(await video.evaluate(node=>node.playbackRate),base*1.5);
   assert.equal(await page.evaluate(()=>localStorage.getItem('keivotos:video-playback-speed')),JSON.stringify(base));
   const start=await video.evaluate(node=>({time:performance.now(),media:node.currentTime}));await page.waitForTimeout(600);const end=await video.evaluate(node=>({time:performance.now(),media:node.currentTime}));
   assert(Math.abs(end.media-start.media-base*1.5*(end.time-start.time)/1000)<.35,JSON.stringify({base,start,end}));report.holdTimings.push({base,boost:base*1.5,start,end});
   await page.keyboard.up('Space');await page.waitForFunction(base=>document.querySelector('video').playbackRate===base,base);assert.equal(await video.evaluate(node=>node.paused),false);await video.evaluate(node=>node.pause());
  }
  check('Holding Space changes measured media progression to 1.5x the chosen 1x/1.25x/2x base and restores that exact base without saving the boost');
  await holdBase(1);await page.keyboard.down('Space');await page.waitForTimeout(350);assert(await video.evaluate(node=>node.paused));await page.keyboard.up('Space');assert(await video.evaluate(node=>node.paused));
  await page.keyboard.press('Space');await page.waitForFunction(()=>!document.querySelector('video').paused);await page.keyboard.press('Space');await page.waitForFunction(()=>document.querySelector('video').paused);
  await viewer.dispatchEvent('keydown',{key:' ',code:'Space',repeat:true,bubbles:true,cancelable:true});await page.waitForTimeout(300);assert(await video.evaluate(node=>node.paused));assert.equal(await video.evaluate(node=>node.playbackRate),1);
  check('Short taps still play/pause, a paused hold does not start playback and a repeat-only keydown cannot begin a gesture');
  await beginHold();await video.evaluate(node=>node.pause());await page.waitForFunction(()=>document.querySelector('video').playbackRate===1);await page.keyboard.up('Space');assert(await video.evaluate(node=>node.paused));
  await beginHold();await viewer.getByRole('button',{name:'Playback speed',exact:true}).focus();await page.waitForFunction(()=>document.querySelector('video').playbackRate===1);await page.keyboard.up('Space');await video.evaluate(node=>node.pause());
  await viewer.focus();await viewer.dispatchEvent('keydown',{key:' ',code:'Space',isComposing:true,bubbles:true,cancelable:true});await page.waitForTimeout(300);assert(await video.evaluate(node=>node.paused));assert.equal(await video.evaluate(node=>node.playbackRate),1);
  await viewer.dispatchEvent('keyup',{key:' ',code:'Space',isComposing:true,bubbles:true,cancelable:true});
  await viewer.dispatchEvent('keydown',{key:' ',code:'Space',ctrlKey:true,bubbles:true,cancelable:true});await page.waitForTimeout(300);assert(await video.evaluate(node=>node.paused));
  await viewer.dispatchEvent('keyup',{key:' ',code:'Space',ctrlKey:true,bubbles:true,cancelable:true});
  for(const tag of ['input','textarea','select','div']){
   await viewer.evaluate((node,tag)=>{const editor=document.createElement(tag);editor.id='hold-native-editor';editor.setAttribute('aria-label','Hold native editor');if(tag==='div'){editor.contentEditable='true';editor.tabIndex=0;}if(tag==='select')editor.innerHTML='<option>Native choice</option>';node.append(editor);},tag);
   const editor=page.locator('#hold-native-editor');await editor.focus();await page.keyboard.down('Space');await page.waitForTimeout(300);await page.keyboard.up('Space');assert(await video.evaluate(node=>node.paused));assert.equal(await video.evaluate(node=>node.playbackRate),1);await editor.evaluate(node=>node.remove());
  }
  await viewer.getByRole('button',{name:'Play',exact:true}).focus();await page.keyboard.down('Space');await page.waitForTimeout(300);assert.equal(await video.evaluate(node=>node.playbackRate),1);await page.keyboard.up('Space');await page.waitForFunction(()=>!document.querySelector('video').paused);await video.evaluate(node=>node.pause());
  check('Pause and control focus restore normal speed; IME/modifiers/editable/native controls keep their own held-Space actions');
  await beginHold();const otherPage=await context.newPage();await otherPage.bringToFront();await page.waitForFunction(()=>document.querySelector('video').playbackRate===1);await otherPage.close();await page.bringToFront();await page.keyboard.up('Space');
  await video.evaluate(node=>node.pause());await viewer.focus();await page.keyboard.down('Space');await page.evaluate(()=>window.dispatchEvent(new Event('blur')));await page.waitForTimeout(350);await page.keyboard.up('Space');assert(await video.evaluate(node=>node.paused));assert.equal(await video.evaluate(node=>node.playbackRate),1);
  check('Actual tab focus loss and pre-threshold blur drain the held gesture without a delayed tap or stuck speed');
  await video.evaluate(node=>{const descriptor=Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype,'playbackRate');Object.defineProperty(node,'playbackRate',{configurable:true,get:()=>descriptor.get.call(node),set:value=>{if(value>1)throw new DOMException('Isolated unsupported boost','NotSupportedError');descriptor.set.call(node,value);}});});
  await video.evaluate(async node=>{node.currentTime=.1;await node.play();});await viewer.focus();await page.keyboard.down('Space');await page.waitForTimeout(350);assert.equal(await video.evaluate(node=>node.playbackRate),1);await page.keyboard.up('Space');assert.equal(await page.evaluate(()=>localStorage.getItem('keivotos:video-playback-speed')),'1');await video.evaluate(node=>{delete node.playbackRate;node.pause();});
  check('An unsupported boosted rate falls back to the unchanged normal speed without a script error or unwanted play/pause');
  await beginHold();await video.evaluate(node=>{Object.defineProperty(node,'error',{configurable:true,value:{code:1}});node.dispatchEvent(new Event('error'));delete node.error;});await page.waitForFunction(()=>document.querySelector('video').playbackRate===1);await page.keyboard.up('Space');
  await beginHold();await video.evaluate(node=>{node.loop=false;node.currentTime=node.duration-.05;});await page.waitForFunction(()=>document.querySelector('video').ended);assert.equal(await video.evaluate(node=>node.playbackRate),1);await page.keyboard.up('Space');assert(await video.evaluate(node=>node.paused));
  await video.evaluate(node=>{node.currentTime=.1;});await viewer.getByRole('button',{name:'Fullscreen',exact:true}).click();await page.waitForFunction(()=>!!document.fullscreenElement);await beginHold();await viewer.getByRole('button',{name:'Fullscreen',exact:true}).click();await page.waitForFunction(()=>!document.fullscreenElement);await page.keyboard.up('Space');assert.equal(await video.evaluate(node=>node.playbackRate),1);
  check('Media abort, natural ending and native fullscreen changes restore the base rate and do not replay on key release');
  await beginHold();await video.evaluate(node=>window.__oldHoldVideo=node);await viewer.getByRole('button',{name:'Next video',exact:true}).click();await page.waitForFunction(()=>document.querySelector('video')&&document.querySelector('video')!==window.__oldHoldVideo);await page.keyboard.up('Space');assert(await page.evaluate(()=>window.__oldHoldVideo.paused&&window.__oldHoldVideo.playbackRate===1&&!window.__oldHoldVideo.isConnected));assert.equal(await video.evaluate(node=>node.playbackRate),1);
  await beginHold();await video.evaluate(node=>window.__closedHoldVideo=node);await back();await playTitle('sample.mp4');await page.waitForFunction(()=>document.querySelector('video')?.readyState>=1);await viewer.dispatchEvent('keydown',{key:' ',code:'Space',repeat:true,bubbles:true,cancelable:true});await page.keyboard.up('Space');assert.equal(await video.evaluate(node=>node.playbackRate),1);assert(await page.evaluate(()=>window.__closedHoldVideo.paused&&window.__closedHoldVideo.playbackRate===1&&!window.__closedHoldVideo.isConnected));
  check('Next-item and close/reopen release old media at its normal rate; an old held key cannot boost the new video');
  async function holdSettings(){await back();await page.getByRole('button',{name:'Open Keivotos menu',exact:true}).click();await page.locator('.app-drawer').getByRole('button',{name:'Settings',exact:true}).click();await page.getByRole('navigation',{name:'Settings sections'}).getByRole('button',{name:'Player',exact:true}).click();}
  async function returnToHoldVideo(){await page.getByRole('button',{name:'Close settings',exact:true}).click();await page.getByRole('dialog',{name:'Settings',exact:true}).waitFor({state:'detached'});await page.keyboard.press('Escape');await playTitle('sample.mp4');await page.waitForFunction(()=>document.querySelector('video')?.readyState>=1);await video.evaluate(node=>node.pause());await viewer.focus();}
  await holdSettings();await page.getByRole('switch',{name:'Hold Space to speed up',exact:true}).click();await returnToHoldVideo();
  await page.keyboard.down('Space');await page.waitForFunction(()=>!document.querySelector('video').paused);await page.waitForTimeout(350);assert.equal(await video.evaluate(node=>node.playbackRate),1);await page.keyboard.up('Space');assert.equal(await video.evaluate(node=>node.paused),false);
  await holdSettings();await page.getByRole('switch',{name:'Hold Space to speed up',exact:true}).click();await page.getByLabel('Hold speed multiplier',{exact:true}).selectOption('2');await returnToHoldVideo();await beginHold();assert.equal(await video.evaluate(node=>node.playbackRate),2);await page.keyboard.up('Space');assert.equal(await video.evaluate(node=>node.playbackRate),1);
  await holdSettings();await page.getByLabel('Hold speed multiplier',{exact:true}).selectOption('1.5');await returnToHoldVideo();
  assert.equal(await page.evaluate(()=>localStorage.getItem('keivotos:youtube-playback-speed')),'1.25');assert.equal(await page.evaluate(()=>localStorage.getItem('keivotos:ost-playback-speed')),null);
  check('Real Player settings disable hold for immediate Space, change its multiplier and preserve other owners’ normal speeds');

  for(const rate of [.5,.75,1,1.25,1.5,1.75,2]){
   await speedButton.click();assert.equal(await speeds.getByRole('button').count(),7);
   const label=rate===1?'1.0x':rate+'x';await speeds.getByRole('button',{name:label,exact:true}).click();
   await page.waitForFunction(rate=>document.querySelector('video').playbackRate===rate,rate);
   assert(await speedButton.evaluate(node=>node===document.activeElement));
   await video.evaluate(node=>{node.currentTime=.2;});
   const start=await video.evaluate(async node=>{await node.play();return {media:node.currentTime,time:performance.now()};});
   await page.waitForTimeout(650);
   const end=await video.evaluate(node=>{node.pause();return {media:node.currentTime,time:performance.now()};});
   assert(Math.abs((end.media-start.media)-rate*(end.time-start.time)/1000)<.25,JSON.stringify({rate,start,end}));
  }
  check('Seven speed buttons change native playback and measured media-time progression; invalid stored speed defaults to 1.0x');
  await speedButton.click();await speeds.getByRole('button',{name:'1.5x',exact:true}).click();
  await back();await playTitle('sample.webm');await page.waitForFunction(()=>document.querySelector('video')?.playbackRate===1.5);
  await back();await page.reload();await playTitle('sample.mp4');await page.waitForFunction(()=>document.querySelector('video')?.playbackRate===1.5);
  assert.equal(await page.evaluate(()=>localStorage.getItem('keivotos:youtube-playback-speed')),'1.25');
  assert.equal(await page.evaluate(()=>localStorage.getItem('keivotos:ost-playback-speed')),null);
  await video.evaluate(node=>{node.pause();node.currentTime=.2;});
  await speedButton.click();await speeds.getByRole('button',{name:'1.5x',exact:true}).focus();
  await page.keyboard.press('Home');assert(await speeds.getByRole('button',{name:'0.5x',exact:true}).evaluate(node=>node===document.activeElement));
  await page.keyboard.press('End');await page.keyboard.press('ArrowRight');
  assert(await speeds.getByRole('button',{name:'0.5x',exact:true}).evaluate(node=>node===document.activeElement));
  const positionBefore=await video.evaluate(node=>node.currentTime);await page.keyboard.press('ArrowLeft');await page.keyboard.press('Escape');
  assert.equal(await speeds.count(),0);assert(await viewer.isVisible());assert(await speedButton.evaluate(node=>node===document.activeElement));
  assert(Math.abs((await video.evaluate(node=>node.currentTime))-positionBefore)<.1);
  await speedButton.click();await speeds.getByRole('button',{name:'2x',exact:true}).focus();await page.keyboard.press('Tab');
  assert(!(await speeds.evaluate(node=>node.contains(document.activeElement))));
  await page.keyboard.press('Escape');assert.equal(await speeds.count(),0);assert(await viewer.isVisible());
  assert(await speedButton.evaluate(node=>node===document.activeElement));
  check('Speed persists across new media and reload independently of YouTube/Music; keyboard arrows navigate rates without seeking and Escape returns focus');
  for(const width of [1440,360]){
   await page.setViewportSize({width,height:780});await speedButton.click();
   const box=await speeds.boundingBox();assert(box.x>=7&&box.y>=7&&box.x+box.width<=width-7&&box.y+box.height<=773);
   for(const button of await speeds.getByRole('button').all()){const rect=await button.boundingBox();assert(rect.width>=38&&rect.height>=32);}
   await page.screenshot({path:path.join(config.output,'speeds-'+width+'.png')});await page.keyboard.press('Escape');
  }
  await page.setViewportSize({width:1440,height:1000});
  await page.locator('.player-controls').evaluate(node=>{node.style.top='0';node.style.bottom='auto';});
  await speedButton.click();assert((await speeds.boundingBox()).y>(await speedButton.boundingBox()).y);
  await video.click();assert.equal(await speeds.count(),0);assert(await viewer.isVisible());
  await page.locator('.player-controls').evaluate(node=>{node.style.removeProperty('top');node.style.removeProperty('bottom');});
  await page.getByRole('button',{name:'Fullscreen',exact:true}).click();await page.waitForFunction(()=>!!document.fullscreenElement);
  await speedButton.click();await page.keyboard.press('Escape');assert(await page.evaluate(()=>!!document.fullscreenElement));
  await page.getByRole('button',{name:'Fullscreen',exact:true}).click();await page.waitForFunction(()=>!document.fullscreenElement);
  await video.evaluate(async node=>{node.loop=true;node.currentTime=0;await node.play();});await speedButton.click();
  await page.mouse.move(1439,400);await page.waitForTimeout(5250);
  assert(await speeds.isVisible());assert(await page.locator('.player-controls').evaluate(node=>!node.classList.contains('controls-hidden')));
  await page.keyboard.press('Escape');await video.evaluate(node=>{node.loop=false;node.pause();});
  await speedButton.click();await speeds.getByRole('button',{name:'1.0x',exact:true}).click();
  check('Speed picker fits desktop/narrow and fullscreen, flips below near the top, dismisses outside and protects controls beyond the idle delay');
  await video.evaluate(async node=>{node.loop=true;node.currentTime=0;await node.play();});
  await viewer.focus();await page.mouse.move(700,400);
  await page.waitForTimeout(1000);assert.notEqual(await cursor(),'none');
  await page.waitForTimeout(4250);assert.equal(await cursor(),'none');
  assert(await page.locator('.player-controls').evaluate(node=>node.classList.contains('controls-hidden')));
  assert.notEqual(await page.locator('body').evaluate(node=>getComputedStyle(node).cursor),'none');
  await page.mouse.move(710,405);assert.notEqual(await cursor(),'none');
  await page.mouse.move(1439,400);assert.equal(await cursor(),'none');
  await page.keyboard.press('Tab');assert.notEqual(await cursor(),'none');
  assert(await viewer.evaluate(node=>node.contains(document.activeElement)));
  await viewer.focus();await page.mouse.move(1438,400);await page.mouse.move(1439,400);assert.equal(await cursor(),'none');
  await viewer.dispatchEvent('touchstart');assert.notEqual(await cursor(),'none');
  await video.evaluate(node=>{node.loop=false;node.pause();});assert.notEqual(await cursor(),'none');
  check('Pointer shares the default five-second control timer, remains scoped to the video and returns on movement, keyboard, touch and pause');
  await video.evaluate(node=>node.currentTime=.2);
  await viewer.focus();await page.keyboard.press('ArrowRight');
  await page.waitForFunction(()=>document.querySelector('video').currentTime>1);
  const knownDuration=await video.evaluate(node=>node.duration);
  assert(Math.abs((await video.evaluate(node=>node.currentTime))-knownDuration)<.25);
  await page.keyboard.press('ArrowLeft');
  assert((await video.evaluate(node=>node.currentTime))<.25);
  await page.keyboard.press('Space');await page.waitForFunction(()=>!document.querySelector('video').paused);
  await viewer.dispatchEvent('keydown',{key:' ',code:'Space',repeat:true,bubbles:true,cancelable:true});
  assert.equal(await video.evaluate(node=>node.paused),false);
  await page.keyboard.press('Space');await page.waitForFunction(()=>document.querySelector('video').paused);
  await video.evaluate(node=>{node.currentTime=.5;Object.defineProperty(node,'duration',{configurable:true,get:()=>Infinity});node.dispatchEvent(new Event('durationchange'));});
  await viewer.focus();await page.keyboard.press('ArrowRight');
  assert((await video.evaluate(node=>node.currentTime))<1);
  await video.evaluate(node=>{delete node.duration;node.dispatchEvent(new Event('durationchange'));});
  await viewer.focus();await page.keyboard.press('Control+ArrowRight');
  assert((await video.evaluate(node=>node.currentTime))<1);
  await viewer.dispatchEvent('keydown',{key:'ArrowRight',isComposing:true,bubbles:true,cancelable:true});
  assert((await video.evaluate(node=>node.currentTime))<1);
  const editor=await viewer.evaluate(node=>{const input=document.createElement('input');input.setAttribute('aria-label','Temporary text entry');node.append(input);return true;});
  assert(editor);
  await page.getByLabel('Temporary text entry').focus();await page.keyboard.press('ArrowRight');await page.keyboard.press('Space');
  assert.equal(await video.evaluate(node=>node.paused),true);
  await page.getByLabel('Temporary text entry').evaluate(node=>node.remove());
  await page.getByLabel('Seek',{exact:true}).focus();
  const rangeBefore=await video.evaluate(node=>node.currentTime);await page.keyboard.press('ArrowRight');
  assert((await video.evaluate(node=>node.currentTime))-rangeBefore<1);
  await page.getByRole('button',{name:'Play',exact:true}).focus();await page.keyboard.press('Space');
  await page.waitForFunction(()=>!document.querySelector('video').paused);
  check('Space toggles once; arrows seek five seconds with clamps; focused controls and text retain native keys');
  const volumeInput=page.getByRole('slider',{name:'Volume'});
  for(const percent of [0,1,50,100]){
   await volumeInput.fill(String(percent));await volumeInput.dispatchEvent('input');
   await page.waitForFunction(expected=>Math.abs(document.querySelector('video').volume-expected/100)<.001,percent);
   assert.equal(await volumeInput.getAttribute('aria-valuetext'),`${percent}%`);
  }
  const mute=page.getByRole('button',{name:'Mute',exact:true});
  assert.equal(await mute.evaluate(node=>getComputedStyle(node).backgroundColor),'rgba(0, 0, 0, 0)');
  assert.equal((await mute.boundingBox()).width,(await page.getByRole('button',{name:'Next video'}).boundingBox()).width);
  await mute.click();assert.equal(await volumeInput.inputValue(),'0');
  await page.getByRole('button',{name:'Unmute',exact:true}).click();assert.equal(await volumeInput.inputValue(),'100');
  await video.evaluate(node=>node.volume=.64);
  await page.waitForFunction(()=>document.querySelector('input[aria-label="Volume"]')?.value==='64');
  await volumeInput.fill('37');await volumeInput.dispatchEvent('input');
  await volumeInput.focus();await page.keyboard.press('ArrowRight');
  assert.equal(await volumeInput.inputValue(),'38');
  assert(Math.abs((await video.evaluate(node=>node.volume))-.38)<.001);
  await viewer.focus();
  await page.setViewportSize({width:360,height:780});
  const volumeBounds=await volumeInput.boundingBox(),controlBounds=await page.locator('.control-row').boundingBox();
  assert(volumeBounds.width>=40&&controlBounds.x+controlBounds.width<=360);
  const nextBounds=await page.getByRole('button',{name:'Next video'}).boundingBox();
  const volumeGroup=await page.locator('.volume-control').boundingBox();
  assert(volumeGroup.x>=nextBounds.x+nextBounds.width&&volumeGroup.x-nextBounds.x-nextBounds.width<12);
  assert.equal(await page.locator('.volume-icon').count(),1);
  await page.setViewportSize({width:1440,height:1000});
  check('Video volume maps 0/1/50/100 percent, follows native changes and keeps a usable narrow keyboard slider');
  const time=page.getByLabel('Playback time',{exact:true});
  await video.evaluate(node=>node.pause());
  await page.getByLabel('Seek',{exact:true}).fill('2');await page.getByLabel('Seek',{exact:true}).dispatchEvent('input');
  await page.waitForFunction(()=>document.querySelector('[aria-label="Playback time"]')?.textContent==='0:02 / 0:04');
  await video.evaluate(node=>{Object.defineProperty(node,'duration',{configurable:true,get:()=>1440});node.dispatchEvent(new Event('durationchange'));});
  await page.waitForFunction(()=>document.querySelector('[aria-label="Playback time"]')?.textContent==='0:02 / 24:00');
  await page.screenshot({path:path.join(config.output,'player-playback-time-1440.png')});
  await video.evaluate(node=>{Object.defineProperty(node,'duration',{configurable:true,get:()=>7200});Object.defineProperty(node,'currentTime',{configurable:true,get:()=>3661});node.dispatchEvent(new Event('durationchange'));node.dispatchEvent(new Event('timeupdate'));});
  await page.waitForFunction(()=>document.querySelector('[aria-label="Playback time"]')?.textContent==='1:01:01 / 2:00:00');
  for(const width of [1440,720,360]){
   await page.setViewportSize({width,height:1000});
   const timerBox=await time.boundingBox(),volumeBox=await page.locator('.volume-control').boundingBox();
   assert(timerBox.x>=volumeBox.x+volumeBox.width&&timerBox.x-volumeBox.x-volumeBox.width<16);
   assert(Math.abs(timerBox.y+timerBox.height/2-volumeBox.y-volumeBox.height/2)<2);
   assert(timerBox.x+timerBox.width<=width);
   assert((await volumeInput.boundingBox()).width>=40);
   for(const label of ['Previous video','Next video','Fullscreen']){
    const box=await page.getByRole('button',{name:label,exact:true}).boundingBox();assert(box.x>=0&&box.x+box.width<=width);
   }
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   await page.screenshot({path:path.join(config.output,`player-hour-time-${width}.png`)});
  }
  await video.evaluate(node=>{delete node.currentTime;Object.defineProperty(node,'duration',{configurable:true,get:()=>Infinity});node.dispatchEvent(new Event('durationchange'));node.dispatchEvent(new Event('timeupdate'));});
  await page.waitForFunction(()=>document.querySelector('[aria-label="Playback time"]')?.textContent==='0:02 / 0:00');
  await video.evaluate(node=>{delete node.duration;node.dispatchEvent(new Event('durationchange'));});
  await page.waitForFunction(()=>document.querySelector('[aria-label="Playback time"]')?.textContent==='0:02 / 0:04');
  await page.setViewportSize({width:1440,height:1000});
  check('playback time follows real seeking, formats minutes/hours and unknown duration safely, sits right of volume and keeps all controls usable at 1440/720/360px');
  const backButton=page.getByRole('button',{name:'Back to episodes'});
  await page.mouse.move(700,400);
  assert.equal(await backButton.innerText(),'');
  const restingWidth=(await backButton.boundingBox()).width;
  await backButton.hover();await page.waitForTimeout(250);
  assert((await backButton.boundingBox()).width>restingWidth);
  assert.equal(await backButton.evaluate(node=>getComputedStyle(node).backgroundColor),'rgb(51, 51, 59)');
  assert.equal(await backButton.evaluate(node=>getComputedStyle(node).borderTopWidth),'0px');
  await page.screenshot({path:path.join(config.output,'player-back-hover.png')});
  const seekBox=await page.getByLabel('Seek',{exact:true}).boundingBox();
  const playBox=await page.getByRole('button',{name:/^(Play|Pause)$/}).boundingBox();
  assert(seekBox.y+seekBox.height<=playBox.y);
  await page.getByRole('button',{name:'Next video',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('video').src.includes('sample.webm'));
  assert(Math.abs((await page.locator('video').evaluate(node=>node.volume))-.38)<.001);
  await page.getByRole('button',{name:'Previous video',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('video').src.includes('sample.mp4'));
  await back();assert(await page.getByRole('button',{name:'sample.mp4',exact:true}).evaluate(node=>node===document.activeElement));
  check('borderless chevron expands to an opaque hover pill; seek sits above transport; previous/next retain originating focus');

  await playTitle('sample.mp4');await page.waitForFunction(()=>document.querySelector('video')?.readyState>=1);await video.evaluate(node=>node.pause());
  const hintNext=page.getByRole('button',{name:'Next video',exact:true}),hintAuto=page.getByRole('switch',{name:'Auto next',exact:true});
  const hintSource=await video.getAttribute('src'),hintPosition=await video.evaluate(node=>node.currentTime);
  await playerHint(hintNext,'Next video');await playerHint(hintNext,'Next video','focus');
  await page.keyboard.press('Tab');assert(await page.getByRole('button',{name:'Mute',exact:true}).evaluate(node=>node===document.activeElement));
  await page.mouse.move(180,180);await page.locator('#player-next-tooltip').waitFor({state:'hidden'});
  const wasAuto=await hintAuto.getAttribute('aria-checked')==='true';
  await playerHint(hintAuto,wasAuto?'Turn auto-next off':'Turn auto-next on','focus');
  await page.keyboard.press('Space');assert.equal(await hintAuto.getAttribute('aria-checked'),String(!wasAuto));
  assert.equal(await page.locator('#player-auto-next-tooltip').innerText(),wasAuto?'Turn auto-next on':'Turn auto-next off');
  await page.keyboard.press('Enter');assert.equal(await hintAuto.getAttribute('aria-checked'),String(wasAuto));
  assert.equal(await video.getAttribute('src'),hintSource);assert(Math.abs((await video.evaluate(node=>node.currentTime))-hintPosition)<.1);assert(await video.evaluate(node=>node.paused));
  check('Short Next/Auto next hints appear on timed hover or keyboard focus, update on Space/Enter and preserve names, focus order and paused media');
  for(const width of [1440,360]){
   await page.setViewportSize({width,height:780});await playerHint(hintNext,'Next video');await page.screenshot({path:path.join(config.output,`next-tooltip-${width}.png`)});
   await playerHint(hintAuto,wasAuto?'Turn auto-next off':'Turn auto-next on','focus');await page.screenshot({path:path.join(config.output,`auto-next-tooltip-${width}.png`)});
  }
  await page.getByRole('button',{name:'Fullscreen',exact:true}).click();await page.waitForFunction(()=>!!document.fullscreenElement);
  const fullscreenTip=await playerHint(hintAuto,wasAuto?'Turn auto-next off':'Turn auto-next on','focus');assert(await fullscreenTip.evaluate(node=>document.fullscreenElement.contains(node)));
  await page.screenshot({path:path.join(config.output,'tooltip-fullscreen.png')});
  await page.getByRole('button',{name:'Fullscreen',exact:true}).click();await page.waitForFunction(()=>!document.fullscreenElement);
  await page.setViewportSize({width:1440,height:1000});await back();assert.equal(await page.locator('[role="tooltip"]').count(),0);
  check('Both short hints fit desktop/360px and native fullscreen without extra tab stops; closing removes them');

  for(const name of ['unsupported.avi','broken.mp4']){await playTitle(name);await page.getByRole('alert').filter({hasText:'See Logs'}).waitFor();assert.notEqual(await cursor(),'none');await page.waitForTimeout(200);await back();}
  assert(report.failures.some(x=>x.reason==='unsupported_format'));assert(report.failures.some(x=>['decode','unsupported_codec','playback'].includes(x.reason)));
  check('unsupported container and broken video report bounded diagnostic events without crashing');
  await playTitle('sample.webm');
  await page.waitForFunction(()=>document.querySelector('video')?.currentTime>0.1);
  await page.locator('.video-viewer').evaluate(node=>node.requestFullscreen=()=>Promise.reject(new Error('fixture unavailable')));
  await page.getByRole('button',{name:'Fullscreen',exact:true}).click();
  await page.getByRole('alert').filter({hasText:'Fullscreen is unavailable'}).waitFor();
  assert.notEqual(await cursor(),'none');
  await page.locator('.video-viewer').focus();await page.mouse.move(1439,400);
  assert.equal(await page.locator('.player-controls').evaluate(node=>node.classList.contains('controls-hidden')),false);
  await page.getByRole('button',{name:'Pause',exact:true}).click();
  assert(await page.locator('video').evaluate(v=>v.paused));
  await page.getByRole('button',{name:'Fullscreen',exact:true}).focus();await page.keyboard.press('Tab');
  assert.equal(await page.getByRole('button',{name:'Back to episodes'}).evaluate(node=>node===document.activeElement),true);
  await back();assert.equal(await page.getByRole('button',{name:'sample.webm',exact:true}).evaluate(node=>node===document.activeElement),true);
  check('fullscreen rejection keeps playback controls usable; focus stays inside the player and returns to the tile');
  assert.equal((await api('/api/suite/modules/video/disable','POST')).status,200);
  assert.equal((await api('/api/video/library')).status,409);
  const sources=await api('/api/files/sources');assert(sources.data.some(x=>x.role==='video'));
  assert.equal((await api('/api/suite/modules/video/enable','POST')).status,200);
  assert.equal((await api('/api/video/library')).data.total,5);
  check('disable rejects Video operations while preserving folder assignments and re-enable restores discovery');
  // Force small real API pages to exercise navigation at the loaded boundary.
  await page.route('**/api/video/library?*',async route=>{
   const url=new URL(route.request().url());url.searchParams.set('limit','2');
   const response=await route.fetch({url:url.toString()});await route.fulfill({response});
  });
  await page.reload();
  await page.getByRole('button',{name:'Open Keivotos menu'}).click();
  await page.locator('.app-drawer').getByRole('button',{name:'Video',exact:true}).click();
  await page.getByLabel('Search videos').fill('sample');await page.waitForTimeout(400);
  await playTitle('sample.mp4');
  assert.equal(await page.getByRole('slider',{name:'Volume'}).inputValue(),'38');
  assert(Math.abs((await page.locator('video').evaluate(node=>node.volume))-.38)<.001);
  await page.getByRole('button',{name:'Next video',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('video').src.includes('sample.webm'));
  assert(await page.getByRole('button',{name:'Next video',exact:true}).isDisabled());
  await page.locator('video').evaluate(node=>node.pause());
  await playerHint(page.getByRole('button',{name:'Next video',exact:true}),'No next video');
  const boundaryLoop=page.getByRole('button',{name:'Loop off',exact:true});await boundaryLoop.click();
  assert(await page.getByRole('button',{name:'Next video',exact:true}).isEnabled());await playerHint(page.getByRole('button',{name:'Next video',exact:true}),'Next video','focus');
  await page.getByRole('button',{name:'Loop all',exact:true}).click();await page.getByRole('button',{name:'Loop one',exact:true}).click();
  assert(await page.getByRole('button',{name:'Next video',exact:true}).isDisabled());
  check('Disabled last-video hover says No next video; Loop All restores Next video without changing boundary or loop behavior');
  await page.getByRole('button',{name:'Previous video',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('video').src.includes('sample.mp4'));
  await page.getByRole('button',{name:'Previous video',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('video').src.includes('sample.m4v'));
  assert(await page.getByRole('button',{name:'Previous video',exact:true}).isDisabled());
  await back();
  check('next fetches another page within filtered results; first/last controls disable without wrapping');

  await page.getByRole('button',{name:'Open Keivotos menu'}).click();
  await page.getByRole('button',{name:'Settings',exact:true}).click();
  const settings=page.getByRole('dialog',{name:'Settings',exact:true});
  await settings.getByRole('navigation',{name:'Settings sections'}).getByRole('button',{name:'Player',exact:true}).click();
  const delay=settings.getByLabel('Hide controls after');
  assert.equal(await delay.inputValue(),'5');
  await delay.selectOption('1');
  await settings.getByRole('button',{name:'Close settings'}).click();
  await page.locator('.app-drawer').getByRole('button',{name:'Video',exact:true}).click();
  await playTitle('sample.mp4');
  await page.locator('video').evaluate(video=>video.loop=true);
  await page.mouse.move(700,400);await page.waitForTimeout(1200);
  assert.equal(await page.locator('.player-controls').evaluate(node=>getComputedStyle(node).opacity),'0');
  assert.equal(await cursor(),'none');
  await page.mouse.move(710,405);
  await page.waitForFunction(()=>getComputedStyle(document.querySelector('.player-controls')).opacity==='1');
  assert.notEqual(await cursor(),'none');
  check('Player setting persists and controls hide after one idle second, then return on pointer movement');

  const controls=page.locator('.player-controls');
  const visible=async()=>{const shown=await controls.evaluate(node=>!node.classList.contains('controls-hidden'));if(shown)assert.notEqual(await cursor(),'none');else assert.equal(await cursor(),'none');return shown;};
  await page.mouse.move(700,400);assert(await visible());
  await page.mouse.move(0,400);assert(await visible());
  await page.mouse.move(700,0);assert(await visible());
  await page.mouse.move(1439,400);assert.equal(await visible(),false);
  await page.mouse.move(700,400);assert(await visible());
  await page.mouse.move(700,999);assert.equal(await visible(),false);
  await page.mouse.move(1439,999);assert.equal(await visible(),false);
  for(let i=0;i<3;i++){await page.mouse.move(700,400);assert(await visible());await page.mouse.move(1439,400);assert.equal(await visible(),false);}
  await page.mouse.move(700,400);
  const seekEdge=await page.getByLabel('Seek',{exact:true}).boundingBox();
  await page.mouse.move(seekEdge.x+seekEdge.width/2,seekEdge.y+seekEdge.height/2);
  await page.mouse.down();await page.mouse.move(1439,999);assert(await visible());
  await page.waitForTimeout(1200);assert(await visible());
  await page.mouse.up();await page.mouse.move(700,400);
  const volumeEdge=await page.getByRole('slider',{name:'Volume'}).boundingBox();
  await page.mouse.move(volumeEdge.x+volumeEdge.width/2,volumeEdge.y+volumeEdge.height/2);
  await page.mouse.down();await page.mouse.move(1439,999);assert(await visible());
  await page.waitForTimeout(1200);assert(await visible());
  await page.mouse.up();await page.mouse.move(700,400);
  await page.getByRole('slider',{name:'Volume'}).focus();await page.mouse.move(1439,400);assert(await visible());
  await page.getByLabel('Seek',{exact:true}).focus();await page.keyboard.press('ArrowLeft');
  await page.waitForTimeout(1200);assert(await visible());
  await page.mouse.move(1439,400);assert(await visible());
  await page.locator('.video-viewer').focus();await page.mouse.move(1438,400);await page.mouse.move(1439,400);assert.equal(await visible(),false);
  await page.mouse.move(700,400);
  await page.getByRole('button',{name:'Fullscreen',exact:true}).click();
  await page.waitForFunction(()=>!!document.fullscreenElement);
  await page.locator('.video-viewer').focus();
  await page.mouse.move(1439,400);assert.equal(await visible(),false);
  await page.mouse.move(700,400);assert(await visible());
  await page.getByRole('button',{name:'Fullscreen',exact:true}).click();
  await page.waitForFunction(()=>!document.fullscreenElement);
  await page.getByRole('button',{name:'Pause',exact:true}).click();
  await page.mouse.move(1439,400);assert(await visible());
  await page.mouse.move(700,400);
  await page.getByRole('button',{name:'Play',exact:true}).click();
  await page.locator('video').evaluate(video=>video.loop=false);
  await page.evaluate(()=>document.documentElement.dataset.motion='reduced');
  assert(Number.parseFloat(await controls.evaluate(node=>getComputedStyle(node).transitionDuration))<.001);
  await page.locator('video').evaluate(node=>node.loop=true);await viewer.focus();await page.mouse.move(700,400);
  await page.waitForTimeout(1200);assert.equal(await cursor(),'none');
  await page.keyboard.press('Tab');assert.notEqual(await cursor(),'none');
  await speedButton.focus();await page.keyboard.press('Enter');await speeds.waitFor();
  await page.mouse.move(1439,400);await page.waitForTimeout(1200);assert(await visible());
  await page.keyboard.press('Escape');await page.locator('video').evaluate(node=>node.loop=false);
  await page.evaluate(()=>{delete document.documentElement.dataset.motion;});
  check('Pointer and controls share edge/idle hiding; long seek/volume drags, focus, pause and an open speed picker protect both in fullscreen/reduced motion');

  const loop=()=>page.getByRole('button',{name:/^Loop (off|all|one)$/});
  assert.equal(await loop().getAttribute('aria-label'),'Loop off');
  await loop().click();assert.equal(await loop().getAttribute('aria-label'),'Loop all');
  await loop().click();assert.equal(await loop().getAttribute('aria-label'),'Loop one');
  const badge=await page.locator('.loop-one-mark').boundingBox(),button=await loop().boundingBox();
  assert(badge.x>=button.x&&badge.x<button.x+button.width/2&&badge.y<button.y+button.height/2);
  await loop().click();assert.equal(await loop().getAttribute('aria-label'),'Loop off');
  const current=await page.locator('video').getAttribute('src');
  await seekToEnd();
  await page.waitForFunction(()=>document.querySelector('video')?.ended);
  assert.equal(await page.locator('video').getAttribute('src'),current);
  const auto=page.getByRole('switch',{name:'Auto next'});
  assert.equal((await page.locator('.auto-next').innerText()).trim(),'');
  await auto.click();assert.equal(await auto.getAttribute('aria-checked'),'true');
  await page.getByRole('button',{name:'Play',exact:true}).click();
  await seekToEnd();
  await page.waitForFunction(src=>document.querySelector('video')?.getAttribute('src')!==src,current);
  assert((await page.locator('video').getAttribute('src')).includes('sample.webm'));
  await loop().click();assert.equal(await loop().getAttribute('aria-label'),'Loop all');
  await seekToEnd();
  await page.waitForFunction(()=>document.querySelector('video')?.getAttribute('src')?.includes('sample.m4v'));
  await loop().click();assert.equal(await loop().getAttribute('aria-label'),'Loop one');
  const repeated=await page.locator('video').getAttribute('src');
  await seekToEnd();
  await page.waitForFunction(()=>{const video=document.querySelector('video');return video&&!video.ended&&video.currentTime<1;});
  assert.equal(await page.locator('video').getAttribute('src'),repeated);
  await back();
  check('Loop cycles Off/All/One, Auto next advances, All wraps, and One replays with a top-left badge');

  {
  await page.unroute('**/api/video/library?*');
  const categoryNav=page.getByRole('navigation',{name:'Video categories',exact:true});
  await page.getByRole('button',{name:'Open Keivotos menu',exact:true}).click();
  await page.getByRole('button',{name:'Settings',exact:true}).click();
  const settings=page.getByRole('dialog',{name:'Settings',exact:true});
  await settings.getByRole('navigation',{name:'Settings sections'}).getByRole('button',{name:'Categories',exact:true}).click();
  for(const name of ['Watching','Rewatch']){
   await settings.getByRole('button',{name:'Create new category',exact:true}).click();
   const create=page.getByRole('dialog',{name:'Create new category',exact:true});
   await create.getByLabel('Category name',{exact:true}).fill(name);await create.getByRole('button',{name:'Create',exact:true}).click();await create.waitFor({state:'hidden'});
  }
  await settings.getByRole('button',{name:'Close settings',exact:true}).click();
  await page.locator('.app-drawer').getByRole('button',{name:'Video',exact:true}).click();
  await categoryNav.waitFor();
  assert.deepEqual(await categoryNav.getByRole('button').allTextContents(),['Default','Watching','Rewatch']);
  assert.equal(await categoryNav.getByRole('button',{name:'Default',exact:true}).getAttribute('aria-pressed'),'true');
  const searchBox=page.getByRole('textbox',{name:'Search videos',exact:true});
  const searchBounds2=await searchBox.boundingBox(),stripBounds=await categoryNav.boundingBox(),coverBounds=await page.locator('.video-tile').first().boundingBox();
  assert(searchBounds2.y+searchBounds2.height<=stripBounds.y&&stripBounds.y+stripBounds.height<=coverBounds.y);
  assert.equal(await categoryNav.getByRole('button',{name:'Watching',exact:true}).evaluate(node=>getComputedStyle(node).backgroundColor),'rgba(0, 0, 0, 0)');
  const plainCategoryStyle=await categoryNav.evaluate(nav=>{
   const button=nav.querySelector('button'),style=getComputedStyle(button);
   return {underline:nav.classList.contains('manga-underline'),gap:getComputedStyle(nav).gap,padding:getComputedStyle(nav).padding,buttonPadding:style.padding,font:style.fontSize,background:style.backgroundColor,pseudo:getComputedStyle(button,'::after').content};
  });
  assert.deepEqual(plainCategoryStyle,{underline:false,gap:'24px',padding:'14px 20px 10px',buttonPadding:'2px 0px',font:'14px',background:'rgba(0, 0, 0, 0)',pseudo:'none'});
  check('Zero categories hide the strip; first Settings creation reveals Default and plain accent-name filters between Search and covers');
  const categories=(await api('/api/video/categories')).data.items;
  const watching=categories.find(item=>item.name==='Watching'),rewatch=categories.find(item=>item.name==='Rewatch');
  const sample=(await api('/api/video/library')).data.items.find(item=>item.name==='sample.mp4');
  const picker=page.getByRole('dialog',{name:'Add to categories',exact:true});
  async function openPicker(name){
   await page.getByRole('button',{name,exact:true}).click({button:'right'});
   const menu=page.getByRole('menu',{name:'Location for '+name,exact:true});
   assert.equal(await menu.getByRole('menuitem',{name:'Show in Files',exact:true}).count(),1);
   assert.equal(await menu.getByRole('menuitem',{name:'Open in Explorer',exact:true}).count(),1);
   await menu.getByRole('menuitem',{name:'Add to categories',exact:true}).click();
   await picker.getByRole('checkbox',{name:'Watching',exact:true}).waitFor();
  }
  await openPicker('sample.mp4');await picker.getByRole('checkbox',{name:'Watching',exact:true}).check();
  await picker.getByRole('button',{name:'Cancel',exact:true}).click();await picker.waitFor({state:'hidden'});
  assert(await page.getByRole('button',{name:'sample.mp4',exact:true}).evaluate(node=>node===document.activeElement));
  await openPicker('sample.mp4');assert.equal(await picker.getByRole('checkbox',{name:'Watching',exact:true}).isChecked(),false);
  await picker.getByRole('checkbox',{name:'Watching',exact:true}).check();await picker.getByRole('checkbox',{name:'Rewatch',exact:true}).check();
  await picker.getByRole('button',{name:'Save',exact:true}).click();await picker.waitFor({state:'hidden'});
  await page.getByRole('button',{name:'sample.mp4',exact:true}).waitFor({state:'detached'});
  assert(await categoryNav.getByRole('button',{name:'Default',exact:true}).evaluate(node=>node===document.activeElement));
  await openPicker('sample.webm');await picker.getByRole('checkbox',{name:'Watching',exact:true}).check();await picker.getByRole('button',{name:'Save',exact:true}).click();await picker.waitFor({state:'hidden'});
  await categoryNav.getByRole('button',{name:'Watching',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('.video-tile').length===2);
  await searchBox.fill('unsupported');await page.getByText('No videos found.',{exact:true}).waitFor();
  await searchBox.fill('');await page.getByRole('button',{name:'sample.mp4',exact:true}).waitFor();
  check('Existing context actions remain; Cancel preserves membership/focus, multi-membership updates Default immediately and search stays within the selected category');
  await playTitle('sample.mp4');
  while(await loop().getAttribute('aria-label')!=='Loop off')await loop().click();
  if(await auto.getAttribute('aria-checked')==='true')await auto.click();
  await page.getByRole('button',{name:'Next video',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('video')?.getAttribute('src')?.includes('sample.webm'));
  assert(await page.getByRole('button',{name:'Next video',exact:true}).isDisabled());await back();
  check('Video Previous/Next follows only the selected category and stops at its boundary');

  const addition=await api('/api/suite/folders/apply','POST',{folders:[{path:config.media+'/../category-pages',display_name:'Category pages',role:'video'}]});assert.equal(addition.status,200);
  const paged=(await api('/api/video/categories','POST',{name:'Paged',request_id:crypto.randomUUID()})).data;
  await page.evaluate(async category=>{
   const items=(await(await fetch('/api/video/library?limit=240&q=clip')).json()).items;
   if(items.length!==125)throw new Error('Expected generated pagination fixture');
   for(const item of items){const response=await fetch('/api/video/category-membership',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_id:item.source_id,path:item.path,category_ids:[category],version:0,request_id:crypto.randomUUID()})});if(!response.ok)throw new Error('Could not assign pagination fixture');}
  },paged.id);
  await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
  await categoryNav.getByRole('button',{name:'Paged',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('.video-tile').length===120);
  await playTitle('clip119.mp4');await page.getByRole('button',{name:'Next video',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('video')?.getAttribute('src')?.includes('clip120.mp4'));await back();
  await page.locator('.video-surface main').evaluate(node=>node.scrollTop=500);
  await categoryNav.getByRole('button',{name:'Watching',exact:true}).click();await page.getByRole('button',{name:'sample.mp4',exact:true}).waitFor();
  await categoryNav.getByRole('button',{name:'Paged',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('.video-tile').length===125&&document.querySelector('.video-surface main').scrollTop>400);
  check('Filtered playback loads the next category page and switching back restores the loaded count and scroll position');

  let releaseStale;
  const delayed=async route=>{
   if(new URL(route.request().url()).searchParams.get('category')!==watching.id)return route.continue();
   const response=await route.fetch();await new Promise(resolve=>releaseStale=resolve);
   try{await route.fulfill({response});}catch{/* The superseded request is intentionally aborted. */}
  };
  await page.route('**/api/video/catalog?*',delayed);
  await categoryNav.getByRole('button',{name:'Watching',exact:true}).click();
  while(!releaseStale)await page.waitForTimeout(20);
  await categoryNav.getByRole('button',{name:'Paged',exact:true}).click();releaseStale();
  await page.waitForFunction(()=>document.querySelectorAll('.video-tile').length===125);
  assert.equal(await page.getByRole('button',{name:'sample.mp4',exact:true}).count(),0);await page.unroute('**/api/video/catalog?*',delayed);
  check('A delayed reply from the previous category cannot replace the current category');

  for(let index=0;index<12;index++)assert.equal((await api('/api/video/categories','POST',{name:'Long category '+index,request_id:crypto.randomUUID()})).status,200);
  await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await categoryNav.getByRole('button',{name:'Long category 11',exact:true}).waitFor();
  await page.setViewportSize({width:360,height:700});await categoryNav.getByRole('button',{name:'Default',exact:true}).focus();await page.keyboard.press('End');
  await page.waitForFunction(()=>document.querySelector('.library-categories').scrollLeft>0);
  assert.equal(await categoryNav.getByRole('button',{name:'Long category 11',exact:true}).getAttribute('aria-pressed'),'true');
  await page.keyboard.press('Home');assert.equal(await categoryNav.getByRole('button',{name:'Default',exact:true}).getAttribute('aria-pressed'),'true');
  await page.setViewportSize({width:1440,height:1000});await categoryNav.getByRole('button',{name:'Watching',exact:true}).click();await page.getByRole('button',{name:'sample.mp4',exact:true}).waitFor();
  await page.reload();await categoryNav.getByRole('button',{name:'Watching',exact:true}).waitFor();
  assert.equal(await categoryNav.getByRole('button',{name:'Watching',exact:true}).getAttribute('aria-pressed'),'true');await page.getByRole('button',{name:'sample.mp4',exact:true}).waitFor();
  await page.screenshot({path:path.join(config.output,'video-categories.png')});
  report.categories={owner:'video',category_id:watching.id,total:2,item:{source_id:sample.source_id,path:sample.path},category_ids:[watching.id,rewatch.id].sort()};
  check('Name-only categories scroll horizontally by keyboard on narrow screens and remember the last valid module selection after reload');
  await searchBox.fill('sample.mp4');await page.waitForFunction(()=>document.querySelectorAll('.video-tile').length===1);
  await playTitle('sample.mp4');await page.waitForFunction(()=>document.querySelector('video')?.readyState>=1);await video.evaluate(node=>node.pause());
  const randomButton=page.getByRole('button',{name:'Random video',exact:true});assert(await randomButton.isDisabled());
  await playerHint(randomButton,'No other videos');await back();
  await searchBox.fill('sample');await page.waitForFunction(()=>document.querySelectorAll('.video-tile').length===2);await playTitle('sample.mp4');await page.waitForFunction(()=>document.querySelector('video')?.readyState>=1);await video.evaluate(node=>node.pause());
  const randomRequests=[];page.on('request',request=>{if(new URL(request.url()).pathname==='/api/video/random')randomRequests.push(new URL(request.url()));});
  const originalRandomState=await video.evaluate(node=>{window.__randomOldVideo=node;return {volume:node.volume,speed:node.playbackRate};});
  const oldLoop=await loop().getAttribute('aria-label'),oldAuto=await auto.getAttribute('aria-checked');
  await playerHint(randomButton,'Random video','focus');await page.keyboard.press('Enter');await page.waitForFunction(()=>document.querySelector('video')?.src.includes('sample.webm'));
  assert(await page.evaluate(()=>window.__randomOldVideo.paused&&!window.__randomOldVideo.isConnected));
  assert.deepEqual(await video.evaluate(node=>({volume:node.volume,speed:node.playbackRate})),originalRandomState);
  assert.equal(await loop().getAttribute('aria-label'),oldLoop);assert.equal(await auto.getAttribute('aria-checked'),oldAuto);
  assert(randomRequests.every(url=>url.searchParams.get('category')===watching.id&&url.searchParams.get('q')==='sample'));
  await randomButton.click();await page.waitForFunction(()=>document.querySelector('video')?.src.includes('sample.mp4'));await video.evaluate(node=>node.pause());
  check('Random keeps single-item searches disabled, chooses the other real video in the current category/search, releases old media and preserves speed/volume/loop/Auto next');
  for(const width of [1440,360]){
   await page.setViewportSize({width,height:780});await playerHint(randomButton,'Random video','focus');
   const a=await randomButton.boundingBox(),b=await speedButton.boundingBox();assert(a.x+a.width<=b.x&&b.x-a.x-a.width<16&&Math.abs(a.y-b.y)<3);
   assert.equal(await randomButton.getAttribute('aria-pressed'),null);await page.screenshot({path:path.join(config.output,`random-video-${width}.png`)});
  }
  await page.getByRole('button',{name:'Fullscreen',exact:true}).click();await page.waitForFunction(()=>!!document.fullscreenElement);const randomTip=await playerHint(randomButton,'Random video','focus');assert(await randomTip.evaluate(node=>document.fullscreenElement.contains(node)));
  await page.screenshot({path:path.join(config.output,'random-video-fullscreen.png')});await page.getByRole('button',{name:'Fullscreen',exact:true}).click();await page.waitForFunction(()=>!document.fullscreenElement);await page.setViewportSize({width:1440,height:1000});
  check('Random is a one-shot icon immediately left of speed, with a short keyboard/hover hint fitting narrow and native fullscreen layouts');
  const randomFailure=route=>route.fulfill({status:503,json:{detail:'Injected random selection failure'}});await page.route('**/api/video/random?*',randomFailure);
  const failedRandomSrc=await video.getAttribute('src');await randomButton.click();await viewer.getByRole('alert').filter({hasText:'Injected random selection failure'}).waitFor();assert.equal(await video.getAttribute('src'),failedRandomSrc);assert(await randomButton.isEnabled());await page.unroute('**/api/video/random?*',randomFailure);
  await randomButton.click();await page.waitForFunction(src=>document.querySelector('video')?.getAttribute('src')!==src,failedRandomSrc);await video.evaluate(node=>node.pause());
  let releaseRandom,startRandom;const heldRandom=new Promise(resolve=>releaseRandom=resolve),randomStarted=new Promise(resolve=>startRandom=resolve);let randomCalls=0;
  const delayedRandom=async route=>{randomCalls++;const response=await route.fetch();startRandom();await heldRandom;try{await route.fulfill({response});}catch{/* The closed player cancels this request. */}};
  await page.route('**/api/video/random?*',delayedRandom);await randomButton.click();await randomStarted;assert(await randomButton.isDisabled());await randomButton.dispatchEvent('click');assert.equal(randomCalls,1);await playerHint(randomButton,'Loading…');
  await back();await playTitle('sample.mp4');await page.waitForFunction(()=>document.querySelector('video')?.readyState>=1);const reopenedRandomSrc=await video.getAttribute('src');releaseRandom();await page.waitForTimeout(200);assert.equal(await video.getAttribute('src'),reopenedRandomSrc);assert(await randomButton.isEnabled());await page.unroute('**/api/video/random?*',delayedRandom);await back();
  assert(await page.getByRole('button',{name:'sample.mp4',exact:true}).evaluate(node=>node===document.activeElement));
  check('Failed Random retains current media and retries; double activation submits once, close aborts delayed selection and Back restores its original tile');
  const randomCategory=(await api('/api/video/categories','POST',{name:'Random pages',request_id:crypto.randomUUID()})).data;
  const clips=(await api('/api/video/library?q=clip&limit=240')).data.items;
  for(const name of ['clip000.mp4','clip124.mp4']){
   const item=clips.find(row=>row.path===name),membership=(await api('/api/video/category-membership?source_id='+item.source_id+'&path='+item.path)).data;
   assert.equal((await api('/api/video/category-membership','POST',{source_id:item.source_id,path:item.path,category_ids:[...membership.category_ids,randomCategory.id],version:membership.version,request_id:crypto.randomUUID()})).status,200);
  }
  const oneRandomPage=async route=>{const url=new URL(route.request().url());if(url.searchParams.get('category')!==randomCategory.id)return route.fallback();url.searchParams.set('limit','1');const response=await route.fetch({url:url.toString()});await route.fulfill({response});};
  await page.route(/\/api\/video\/(library|catalog)\?/,oneRandomPage);await searchBox.fill('clip');await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await categoryNav.getByRole('button',{name:'Random pages',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('.video-tile').length===1);await playTitle('clip000.mp4');
  await randomButton.click();await page.waitForFunction(()=>document.querySelector('video')?.src.includes('clip124.mp4'));assert(await page.getByRole('button',{name:'Next video',exact:true}).isDisabled());
  await page.getByRole('button',{name:'Previous video',exact:true}).click();await page.waitForFunction(()=>document.querySelector('video')?.src.includes('clip000.mp4'));await back();assert(await page.getByRole('button',{name:'clip000.mp4',exact:true}).evaluate(node=>node===document.activeElement));await page.unroute(/\/api\/video\/(library|catalog)\?/,oneRandomPage);
  check('Random reaches an initially unloaded scoped page, reconciles normal Previous/Next positions and retains the originating Back focus');


  // Series browsing is tested after all original flat-player/category checks.
  const seriesAdded=await api('/api/suite/folders/apply','POST',{folders:[{path:config.media+'/../series-library',display_name:'Series videos',role:'video'}]});
  assert.equal(seriesAdded.status,200,JSON.stringify(seriesAdded));
  const seriesSource=seriesAdded.data.sources.find(item=>item.display_name==='Series videos').source_id;
  await searchBox.fill('');await categoryNav.getByRole('button',{name:'Default',exact:true}).click();
  await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
  const orderTile=page.locator('.video-tile[title="Order"]');await orderTile.waitFor();
  await orderTile.click();let seriesInfo=page.getByRole('dialog',{name:'Order episodes',exact:true});
  await seriesInfo.locator('.episode-row').first().waitFor();
  assert.equal(await seriesInfo.locator('.season-name,.episode-details,.chevron').count(),0);
  assert.deepEqual((await seriesInfo.locator('.episode-row').allTextContents()).slice(0,3).map(text=>text.replace('›','').trim()),['Episode 1','Episode 2','Episode 10']);
  assert(await seriesInfo.locator('.info-cover img').evaluate(img=>img.complete&&img.naturalWidth>0));
  const shortWindow=await seriesInfo.boundingBox();
  await seriesInfo.locator('[data-episode-path="Order/Season 1/Episode 2.mp4"]').click();
  await page.waitForFunction(()=>document.querySelector('video')?.readyState>=1);await video.evaluate(node=>node.pause());
  while(await loop().getAttribute('aria-label')!=='Loop off')await loop().click();
  if(await auto.getAttribute('aria-checked')==='true')await auto.click();
  await page.getByRole('button',{name:'Next video',exact:true}).click();
  await page.waitForFunction(()=>new URL(document.querySelector('video').src).searchParams.get('path')==='Order/Season 1/Episode 10.mp4');
  await video.evaluate(node=>node.pause());
  await page.getByRole('button',{name:'Back to episodes',exact:true}).click();
  assert(await seriesInfo.locator('[data-episode-path="Order/Season 1/Episode 2.mp4"]').evaluate(node=>node===document.activeElement));
  await page.getByRole('button',{name:'Back to videos',exact:true}).click();assert(await orderTile.evaluate(node=>node===document.activeElement));
  check('Series tiles open local covers and plain episode rows without section headings; episodes sort 1/2/10 and player Back restores the originating row then tile');

  await orderTile.click();seriesInfo=page.getByRole('dialog',{name:'Order episodes',exact:true});
  await seriesInfo.locator('.episode-row').first().waitFor();
  let episodeReveal=null;
  const revealEpisode=async route=>{episodeReveal=route.request().postDataJSON();await route.fulfill({json:{status:'revealed'}});};
  await page.route('**/api/files/reveal',revealEpisode);
  const exactEpisode=seriesInfo.locator('[data-episode-path="Order/Season 1/Episode 10.mp4"]');
  await exactEpisode.focus();await page.keyboard.press('Shift+F10');
  await page.getByRole('menu',{name:'Location for Episode 10.mp4'}).getByRole('menuitem',{name:'Open in Explorer',exact:true}).click();
  assert.deepEqual(episodeReveal,{source_id:seriesSource,path:'Order/Season 1/Episode 10.mp4'});
  assert(await exactEpisode.evaluate(node=>node===document.activeElement));await page.unroute('**/api/files/reveal',revealEpisode);
  await page.keyboard.press('Escape');assert(await orderTile.evaluate(node=>node===document.activeElement));
  await orderTile.click({button:'right'});const seriesMenu=page.getByRole('menu',{name:'Location for Order',exact:true});
  await seriesMenu.getByRole('menuitem',{name:'Show in Files',exact:true}).waitFor();
  assert.equal(await seriesMenu.getByRole('menuitem',{name:'Add to categories',exact:true}).count(),0);
  await page.keyboard.press('Escape');
  check('Episode context actions retain exact source/path and focus; series location actions target their real folder without rewriting file memberships');

  await searchBox.fill('Season 10');await page.waitForFunction(()=>document.querySelectorAll('.video-tile').length===2);
  await orderTile.click();seriesInfo=page.getByRole('dialog',{name:'Order episodes',exact:true});
  await seriesInfo.locator('.episode-row').first().waitFor();assert.equal(await seriesInfo.locator('.episode-row').count(),1);
  assert.equal(await seriesInfo.locator('.season-name').count(),0);await page.keyboard.press('Escape');
  await searchBox.fill('Show 2');await page.waitForFunction(()=>document.querySelectorAll('.video-tile').length===1);
  const showTile=page.locator('.video-tile[title="Show 2"]');await showTile.click();
  const showInfo=page.getByRole('dialog',{name:'Show 2 episodes',exact:true});await page.waitForFunction(()=>document.querySelectorAll('.episode-row').length===120);
  assert.equal(Math.round((await showInfo.boundingBox()).height),Math.round(shortWindow.height));
  const episodeList=showInfo.locator('.episode-list');await episodeList.evaluate(node=>node.scrollTop=node.scrollHeight);
  await showInfo.getByRole('button',{name:'Load more',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('.episode-row').length===127);
  await episodeList.evaluate(node=>node.scrollTop=700);const episodeScrollBefore=await episodeList.evaluate(node=>node.scrollTop);
  const rowToPlay=showInfo.locator('[data-episode-path="Show 2/Season 1/Episode 15.mp4"]');await rowToPlay.focus();
  await rowToPlay.click();await page.waitForFunction(()=>document.querySelector('video')?.readyState>=1);await video.evaluate(node=>node.pause());
  await randomButton.click();await page.waitForFunction(()=>new URL(document.querySelector('video').src).searchParams.get('path').startsWith('Show 2/')&&!new URL(document.querySelector('video').src).searchParams.get('path').endsWith('/Episode 15.mp4'));
  await video.evaluate(node=>node.pause());
  await page.getByRole('button',{name:'Back to episodes',exact:true}).click();assert.equal(await showInfo.locator('.episode-row').count(),127);
  assert(await rowToPlay.evaluate(node=>node===document.activeElement));
  const retainedScroll=await episodeList.evaluate(node=>node.scrollTop);assert(retainedScroll>0);
  await page.keyboard.press('Escape');assert(await showTile.evaluate(node=>node===document.activeElement));
  let releaseEpisodes,startEpisodes;const episodeGate=new Promise(resolve=>releaseEpisodes=resolve),episodeStarted=new Promise(resolve=>startEpisodes=resolve);
  const delayedEpisodes=async route=>{const response=await route.fetch();startEpisodes();await episodeGate;try{await route.fulfill({response});}catch{}};
  await page.route('**/api/video/episodes?*',delayedEpisodes);await showTile.click();await episodeStarted;
  assert.equal(await showInfo.locator('.episode-row').count(),127);
  assert(Math.abs((await episodeList.evaluate(node=>node.scrollTop))-retainedScroll)<2);
  releaseEpisodes();await page.unroute('**/api/video/episodes?*',delayedEpisodes);await page.waitForTimeout(200);
  check('Episode paging reaches all 127 rows; Random stays in its series and cached rows, loaded prefix, scrolling and focus survive Back and a delayed reopen');

  await page.setViewportSize({width:360,height:700});await page.waitForTimeout(100);
  const compactInfo=await showInfo.boundingBox(),compactList=await episodeList.boundingBox();
  assert(compactInfo.x>=0&&compactInfo.x+compactInfo.width<=360&&compactList.width>100);
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await showInfo.locator('.back').focus();await page.keyboard.press('Shift+Tab');assert(await showInfo.evaluate(node=>node.contains(document.activeElement)));
  await page.screenshot({path:path.join(config.output,'video-series-compact.png')});
  await page.keyboard.press('Escape');await page.setViewportSize({width:1440,height:1000});
  await page.emulateMedia({reducedMotion:'reduce'});await showTile.click();await showInfo.locator('.episode-row').first().waitFor();
  assert.equal(await showInfo.locator('.back').evaluate(node=>getComputedStyle(node).transitionDuration),'0s');
  await page.screenshot({path:path.join(config.output,'video-series-desktop.png')});await page.keyboard.press('Escape');await page.emulateMedia({reducedMotion:'no-preference'});
  check('Compact cover/episode window fits desktop and 360px, traps focus, scrolls independently and respects reduced motion');

  await searchBox.fill('Order');await orderTile.waitFor();
  let failEpisodes=true;
  const failedEpisodes=async route=>{if(failEpisodes)return route.fulfill({status:503,json:{detail:'Injected episode listing failure'}});return route.continue();};
  await page.route('**/api/video/episodes?*',failedEpisodes);await orderTile.click();seriesInfo=page.getByRole('dialog',{name:'Order episodes',exact:true});
  await seriesInfo.getByRole('alert').filter({hasText:'Injected episode listing failure'}).waitFor();
  failEpisodes=false;await seriesInfo.getByRole('button',{name:'Retry',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('.episode-row').length===6);
  await page.unroute('**/api/video/episodes?*',failedEpisodes);await page.keyboard.press('Escape');
  check('Episode load failure keeps the window usable and retries without changing local media');

  const singleCategory=(await api('/api/video/categories','POST',{name:'Series category',request_id:crypto.randomUUID()})).data;
  const episodePath='Order/Season 1/Episode 2.mp4';
  const episodeMember=(await api('/api/video/category-membership?source_id='+seriesSource+'&path='+encodeURIComponent(episodePath))).data;
  assert.equal((await api('/api/video/category-membership','POST',{source_id:seriesSource,path:episodePath,category_ids:[singleCategory.id],version:episodeMember.version,request_id:crypto.randomUUID()})).status,200);
  await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await categoryNav.getByRole('button',{name:'Series category',exact:true}).click();
  await orderTile.waitFor();await orderTile.click();seriesInfo=page.getByRole('dialog',{name:'Order episodes',exact:true});
  await seriesInfo.locator('.episode-row').first().waitFor();assert.equal(await seriesInfo.locator('.episode-row').count(),1);
  assert.equal(await seriesInfo.locator('.episode-row').getAttribute('data-episode-path'),episodePath);
  await seriesInfo.locator('.episode-row').click({button:'right'});await page.getByRole('menu',{name:'Location for Episode 2.mp4'}).getByRole('menuitem',{name:'Add to categories',exact:true}).click();
  await picker.getByRole('checkbox',{name:'Series category',exact:true}).uncheck();await picker.getByRole('button',{name:'Save',exact:true}).click();await picker.waitFor({state:'hidden'});
  await page.waitForFunction(()=>document.querySelectorAll('.episode-row').length===0);
  const preservedMembership=(await api('/api/video/category-membership?source_id='+seriesSource+'&path='+encodeURIComponent(episodePath))).data;
  assert.deepEqual(preservedMembership.category_ids,[]);await page.keyboard.press('Escape');
  check('Grouped categories contain only existing matching video members; removing the last membership updates the initiating episode list without creating folder categories');


  await categoryNav.getByRole('button',{name:'Default',exact:true}).click();await searchBox.fill('Order');await orderTile.waitFor();
  let releaseLate,startLate;const lateGate=new Promise(resolve=>releaseLate=resolve),lateStarted=new Promise(resolve=>startLate=resolve);
  const lateEpisodeResponse=async route=>{const response=await route.fetch();startLate();await lateGate;try{await route.fulfill({response});}catch{}};
  await page.route('**/api/video/episodes?*',lateEpisodeResponse);await orderTile.click();await lateStarted;
  assert.equal((await api('/api/suite/folders/apply','POST',{folders:[{source_id:seriesSource,path:config.media+'/../series-library',display_name:'Series videos',role:'files'}]})).status,200);
  await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await page.getByRole('dialog',{name:'Order episodes',exact:true}).waitFor({state:'detached'});
  releaseLate();await page.unroute('**/api/video/episodes?*',lateEpisodeResponse);await page.waitForTimeout(200);
  assert.equal(await page.locator('.episode-row').count(),0);assert.equal(await orderTile.count(),0);
  assert.equal((await api('/api/suite/folders/apply','POST',{folders:[{source_id:seriesSource,path:config.media+'/../series-library',display_name:'Series videos',role:'video'}]})).status,200);
  await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await orderTile.waitFor();await orderTile.click();
  seriesInfo=page.getByRole('dialog',{name:'Order episodes',exact:true});await seriesInfo.locator('.episode-row').first().click();await page.waitForFunction(()=>document.querySelector('video')?.readyState>=1);
  await video.evaluate(node=>window.__releasedSeriesVideo=node);
  assert.equal((await api('/api/suite/folders/apply','POST',{folders:[{source_id:seriesSource,path:config.media+'/../series-library',display_name:'Series videos',role:'files'}]})).status,200);
  await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await page.getByRole('dialog',{name:'Video player',exact:true}).waitFor({state:'detached'});
  assert(await page.evaluate(()=>window.__releasedSeriesVideo.paused&&!window.__releasedSeriesVideo.isConnected));
  const preservedSource=(await api('/api/files/sources')).data.find(item=>item.source_id===seriesSource);assert.equal(preservedSource.role,'files');
  check('Releasing a source cancels delayed episode replies, clears stale windows and stops old media while preserving the Files source');

  }
  // Dedicated synthetic long clips keep progress checks independent of the four-second player fixtures.
  const progressCategories=page.getByRole('navigation',{name:'Video categories',exact:true}),progressSearch=page.getByRole('textbox',{name:'Search videos',exact:true});
  const watchSource=(await api('/api/suite/folders/apply','POST',{folders:[{path:config.media+'/../watch-library',display_name:'Watch progress',role:'video'}]})).data;
  const watchFolder=(await api('/api/files/sources')).data.find(item=>item.display_name==='Watch progress');assert(watchFolder);
  await progressCategories.getByRole('button',{name:'Default',exact:true}).click();await progressSearch.fill('Resume');
  const resumeTile=page.getByRole('button',{name:'Resume',exact:true});await resumeTile.click();
  const resumeInfo=page.getByRole('dialog',{name:'Resume episodes',exact:true});
  const row1=resumeInfo.locator('[data-episode-path="Resume/Episode 1.mp4"]'),row2=resumeInfo.locator('[data-episode-path="Resume/Episode 2.mp4"]');
  const watchItem={source_id:watchFolder.source_id,path:'Resume/Episode 1.mp4'};
  const readWatch=async(item=watchItem)=>(await api('/api/video/progress/read','POST',item)).data;
  assert.equal((await readWatch()).position,null);assert.equal(await resumeInfo.getByRole('progressbar').count(),0);
  await page.evaluate(()=>{const original=HTMLMediaElement.prototype.play;HTMLMediaElement.prototype.play=function(){return window.__freezePlayer?Promise.resolve():original.call(this);};window.__freezePlayer=true;});
  await row1.click();await page.waitForFunction(()=>document.querySelector('video')?.readyState>=1);await page.waitForTimeout(250);
  assert.equal((await readWatch()).position,null);
  await page.getByRole('button',{name:'Back to episodes',exact:true}).click();assert.equal((await readWatch()).position,null);
  check('Opening episodes, loading metadata and leaving an unplayed clip create no watch position');
  await row1.click();await page.waitForFunction(()=>document.querySelector('video')?.readyState>=1);
  const seek=page.getByRole('slider',{name:'Seek',exact:true});
  await seek.fill('7.2');await seek.dispatchEvent('input');
  await page.waitForFunction(async item=>{const state=await (await fetch('/api/video/progress/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(item)})).json();return Math.abs(state.position?.position-7.2)<.1;},watchItem);
  await page.getByRole('button',{name:'Back to episodes',exact:true}).click();
  const bar=row1.getByRole('progressbar');await bar.waitFor();assert.equal(await bar.evaluate(node=>getComputedStyle(node).height),'4px');
  assert(Math.abs(Number(await bar.getAttribute('aria-valuenow'))-7.2)<.1);assert(!await row1.innerText().then(text=>text.includes('%')));
  for(const width of [1440,360]){await page.setViewportSize({width,height:900});assert((await bar.boundingBox()).width<(await row1.boundingBox()).width);}
  await page.setViewportSize({width:1440,height:1000});await page.screenshot({path:path.join(config.output,'watch-progress.png')});
  await row1.click();await page.waitForFunction(()=>document.querySelector('video')?.readyState>=1&&Math.abs(document.querySelector('video').currentTime-7.2)<.1);
  const restored=await readWatch();await page.waitForTimeout(250);assert.deepEqual((await readWatch()).position,restored.position);
  check('Completed explicit seek saves immediately; compact 4px accessible bars and matching-time resume survive Back without metadata writes');
  let releasePositions,startPositions;const positionGate=new Promise(resolve=>releasePositions=resolve),positionStarted=new Promise(resolve=>startPositions=resolve);
  const latePositions=async route=>{const response=await route.fetch();startPositions();await positionGate;try{await route.fulfill({response});}catch{}};
  await page.route('**/api/video/progress/positions',latePositions);await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await positionStarted;
  await seek.fill('8.2');await seek.dispatchEvent('input');
  await page.waitForFunction(async item=>{const state=await (await fetch('/api/video/progress/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(item)})).json();return Math.abs((state.position?.position??-1)-8.2)<.1;},watchItem);
  releasePositions();await page.unroute('**/api/video/progress/positions',latePositions);await page.waitForTimeout(150);
  await page.getByRole('button',{name:'Back to episodes',exact:true}).click();assert(Math.abs(Number(await bar.getAttribute('aria-valuenow'))-8.2)<.1);
  await row1.click();await page.waitForFunction(()=>document.querySelector('video')?.readyState>=1&&Math.abs(document.querySelector('video').currentTime-8.2)<.1);
  check('A delayed row-position response cannot replace a newer acknowledged playback save');

  await page.evaluate(()=>window.__freezePlayer=false);await page.getByRole('button',{name:'Play',exact:true}).click();await page.waitForTimeout(1100);await page.getByRole('button',{name:'Pause',exact:true}).click();
  await page.waitForTimeout(150);const played=await readWatch();assert(played.position.position>8);
  check('Actual timed playback is observed and pause flushes its newer position');
  let failSave=true;
  const failingSave=route=>failSave?route.fulfill({status:503,json:{detail:'Injected watch-save failure'}}):route.continue();
  await page.route('**/api/video/progress',failingSave);await seek.fill('11');await seek.dispatchEvent('input');
  await page.getByRole('alert').filter({hasText:'Progress was not saved.'}).waitFor();assert.equal((await readWatch()).position.position,played.position.position);
  failSave=false;await page.getByRole('button',{name:'Retry',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('.progress-error'));
  assert(Math.abs((await readWatch()).position.position-11)<.1);await page.unroute('**/api/video/progress',failingSave);
  check('Save failure preserves the last acknowledgement; Retry confirms the same seek before continuing');
  await page.getByRole('button',{name:'Back to episodes',exact:true}).click();
  let failRead=true;const failingRead=route=>failRead?route.fulfill({status:503,json:{detail:'Injected watch-read failure'}}):route.continue();
  await page.route('**/api/video/progress/read',failingRead);await page.evaluate(()=>window.__freezePlayer=true);await row1.click();
  await page.getByRole('alert').filter({hasText:'Progress is unavailable.'}).waitFor();await seek.fill('3');await seek.dispatchEvent('input');
  failRead=false;assert(Math.abs((await readWatch()).position.position-11)<.1);await page.getByRole('button',{name:'Retry',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('.progress-error'));
  await page.unroute('**/api/video/progress/read',failingRead);await seek.fill('5');await seek.dispatchEvent('input');await page.waitForTimeout(150);assert(Math.abs((await readWatch()).position.position-5)<.1);
  check('Failed lookup allows playback without overwriting unknown history; Retry re-enables observed saves');
  const beforeStale=await readWatch();
  const second=await context.newPage();await second.addInitScript(()=>{HTMLMediaElement.prototype.play=function(){return Promise.resolve();};});
  await second.goto(config.url);await second.getByRole('button',{name:'Open Keivotos menu'}).click();await second.locator('.app-drawer').getByRole('button',{name:'Video',exact:true}).click();
  await second.getByRole('textbox',{name:'Search videos',exact:true}).fill('Resume');await second.getByRole('button',{name:'Resume',exact:true}).click();
  await second.locator('[data-episode-path="Resume/Episode 1.mp4"]').click();await second.waitForFunction(()=>document.querySelector('video')?.readyState>=1);
  assert((await readWatch()).generation>beforeStale.generation);await second.close();await page.bringToFront();
  await seek.fill('9');await seek.dispatchEvent('input');await page.getByRole('alert').filter({hasText:'Progress was not saved.'}).waitFor();
  assert.equal((await readWatch()).position.position,beforeStale.position.position);
  await page.getByRole('button',{name:'Back to episodes',exact:true}).click();
  check('A newer playback session rejects delayed writes from the old player without changing its confirmed value');
  await row1.click();await page.waitForFunction(()=>document.querySelector('video')?.readyState>=1);
  await page.evaluate(()=>window.__freezePlayer=false);await seek.fill('19.8');await seek.dispatchEvent('input');await page.getByRole('button',{name:'Play',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('video')?.ended);await page.waitForFunction(async item=>{const state=await (await fetch('/api/video/progress/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(item)})).json();return !!state.position?.ended;},watchItem);assert((await readWatch()).position.ended);
  await page.getByRole('button',{name:'Back to episodes',exact:true}).click();await page.evaluate(()=>window.__freezePlayer=true);await row1.click();
  await page.waitForFunction(()=>document.querySelector('video')?.readyState>=1);assert.equal(await page.locator('video').evaluate(node=>node.currentTime),0);assert((await readWatch()).position.ended);
  await page.getByRole('button',{name:'Back to episodes',exact:true}).click();
  check('Finished episodes reopen at the beginning and retain completion until a new observation');
  await row2.click();await page.waitForFunction(()=>document.querySelector('video')?.readyState>=1);await seek.fill('6.4');await seek.dispatchEvent('input');await page.waitForTimeout(150);
  const restartItem={source_id:watchFolder.source_id,path:'Resume/Episode 2.mp4'};
  await page.waitForFunction(async item=>{const state=await (await fetch('/api/video/progress/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(item)})).json();return Math.abs((state.position?.position??-1)-6.4)<.1;},restartItem);
  const restartState=await readWatch(restartItem);
  await page.getByRole('button',{name:'Back to episodes',exact:true}).click();
  report.continuation={item:restartItem,position:restartState.position.position,sequence:restartState.position.sequence};
  await page.keyboard.press('Escape');
  {
  // Item 27 intentionally interprets only tags and description.
  assert.equal((await api('/api/suite/folders/apply','POST',{folders:[{path:config.media+'/../metadata-library',display_name:'Metadata videos',role:'video'}]})).status,200);
  const source=(await api('/api/files/sources')).data.find(source=>source.display_name==='Metadata videos');assert(source);
  const subject={source_id:source.source_id,path:'Original Series',kind:'series'};
  const getDetails=async(value=subject)=>(await api('/api/video/metadata?'+new URLSearchParams(value))).data;
  assert.deepEqual((await getDetails()).fields,{});
  assert.equal((await api('/api/suite/folders/'+source.source_id+'/rescan','POST')).status,200);
  const search=page.getByRole('textbox',{name:'Search videos',exact:true});await search.fill('Original Series');await page.getByRole('button',{name:'Original Series',exact:true}).click();
  const info=page.getByRole('dialog',{name:'Original Series episodes',exact:true});await info.getByText('A locally stored anime series.',{exact:true}).waitFor();
  const hostileDescription='<img src="https://invalid.example/art" onerror="window.__metadataExecuted=true">';
  const hostileRead=async route=>{const response=await route.fetch();const value=await response.json();value.fields.summary=hostileDescription;await route.fulfill({response,json:value});};
  await page.route('**/api/video/metadata?*',hostileRead);await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await info.getByText(hostileDescription,{exact:true}).waitFor();
  assert.equal(await info.locator('.metadata-details img').count(),0);assert.equal(await page.evaluate(()=>window.__metadataExecuted),undefined);
  await page.unroute('**/api/video/metadata?*',hostileRead);await page.evaluate(()=>window.dispatchEvent(new Event('focus')));await info.getByText('A locally stored anime series.',{exact:true}).waitFor();
  assert.deepEqual(Object.keys((await getDetails()).fields).sort(),['summary','tags']);assert.equal(await info.locator('.episode-row').count(),12);
  assert.deepEqual(await info.locator('.episode-row').allTextContents().then(values=>values.map(text=>text.replace('›','').trim())),Array.from({length:12},(_,index)=>'Episode '+(index+1)));
  assert.equal(await info.getByRole('progressbar').count(),0);
  const episode={source_id:source.source_id,path:'Original Series/Episode 1.mp4'};
  assert.equal((await api('/api/video/progress/read','POST',episode)).data.position,null);
  check('Explicit Files rescan saves only tags/description; supplied titles stay ignored, real names/order stay unchanged, descriptions render as text and NFO watched/playcount never create progress');
  assert.equal(await info.locator('.season-name,.episode-details,.chevron').count(),0);
  assert(await info.locator('.episode-row').allTextContents().then(values=>values.every(value=>!/[>›ⓘ]|\.(mp4|m4v|webm|mkv|mov|avi|ogv)$/i.test(value.trim()))));
  for(const width of [1440,360]){
   await page.setViewportSize({width,height:800});const box=await info.boundingBox();assert(box.x>=0&&box.x+box.width<=width);assert.equal(box.height,500);assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   assert.equal(await info.locator('.episode-row').first().evaluate(node=>node.getBoundingClientRect().height),46);await page.screenshot({path:path.join(config.output,'video-metadata-'+width+'.png')});
  }
  await page.setViewportSize({width:1440,height:1000});
  check('The compact desktop/360px window has plain extension-free episode names, no section labels/info icons/arrows, unchanged tags and normal Play rows');
  await info.getByRole('button',{name:'Study',exact:true}).click();await info.waitFor({state:'detached'});assert.equal(await search.inputValue(),'Study');await page.getByRole('button',{name:'Original Series',exact:true}).waitFor();
  await search.fill('100%_');await page.getByRole('button',{name:'Original Series',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('.episode-row').length===1);assert.equal(await info.locator('.episode-row').getAttribute('data-episode-path'),episode.path);
  await info.getByRole('button',{name:'Back to videos',exact:true}).click();await search.fill('하늘 音楽');await page.waitForFunction(()=>!document.querySelector('.video-grid[aria-busy="true"]'));assert.equal(await page.locator('.video-tile').count(),0);
  await search.fill('Original Series');await page.getByRole('button',{name:'Original Series',exact:true}).click();await info.getByRole('button',{name:'Edit',exact:true}).click();
  const editor=page.getByRole('dialog',{name:'Edit video details',exact:true});assert.equal(await editor.locator('textarea').count(),2);assert.equal(await editor.getByLabel('Title',{exact:true}).count(),0);
  await editor.getByLabel('Description',{exact:true}).fill('Personal description');let lost=false;const tokens=[];
  const lostSave=async route=>{tokens.push(route.request().postDataJSON().request_id);if(!lost){await route.fetch();lost=true;return route.abort('failed');}return route.continue();};
  await page.route('**/api/video/metadata/overrides',lostSave);await editor.getByRole('button',{name:'Save',exact:true}).click();await editor.getByRole('alert').filter({hasText:'Details were not saved.'}).waitFor();
  await editor.getByRole('button',{name:'Retry',exact:true}).click();await editor.waitFor({state:'detached'});await page.unroute('**/api/video/metadata/overrides',lostSave);assert.equal(tokens.length,2);assert.equal(tokens[0],tokens[1]);assert.equal((await getDetails()).version,1);
  await info.getByText('Personal description',{exact:true}).waitFor();
  check('Literal tag filters preserve category scope; unsupported titles are not searched, the editor has exactly two fields, and a lost Save retries one identity with immediate description reconciliation');
  await info.getByRole('button',{name:'Edit',exact:true}).click();await editor.getByLabel('Description',{exact:true}).fill('Unsaved description');await editor.getByRole('button',{name:'Cancel',exact:true}).click();
  const decision=page.getByRole('alertdialog',{name:'Unsaved video details',exact:true});await decision.getByRole('button',{name:'Keep editing',exact:true}).click();assert.equal(await editor.getByLabel('Description',{exact:true}).inputValue(),'Unsaved description');
  const external=await getDetails();assert.equal((await api('/api/video/metadata/overrides','POST',{...subject,fields:{...external.overrides,tags:['Personal tag']},version:external.version,source_revision:external.source_revision,request_id:crypto.randomUUID()})).status,200);
  await editor.getByRole('button',{name:'Save',exact:true}).click();await editor.getByRole('alert').filter({hasText:'Details changed.'}).waitFor();assert.equal(await editor.getByLabel('Description',{exact:true}).inputValue(),'Unsaved description');
  await editor.getByRole('button',{name:'Reload',exact:true}).click();await decision.getByRole('button',{name:'Discard changes',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#video-detail-tags')?.value==='Personal tag'&&!document.querySelector('#video-detail-tags')?.disabled);
  await editor.getByLabel('Description',{exact:true}).fill('');await editor.locator('.field-heading:has(label[for="video-detail-summary"]) button').click();await page.waitForFunction(()=>document.querySelector('#video-detail-summary')?.value==='A locally stored anime series.');
  await editor.getByRole('button',{name:'Cancel',exact:true}).click();await decision.getByRole('button',{name:'Discard changes',exact:true}).click();await editor.waitFor({state:'detached'});assert.equal((await getDetails()).version,2);assert.equal((await getDetails()).fields.summary,'Personal description');
  check('Dirty Cancel, real conflicts, explicit Reload, clear/inherit and Discard preserve personal choices and update the two-field form visibly');
  let fail=true,calls=0;const failRefresh=route=>{calls++;return fail?route.fulfill({status:503,json:{detail:'Injected metadata refresh failure'}}):route.continue();};
  await page.route('**/api/video/metadata/refresh',failRefresh);await info.getByRole('button',{name:'Refresh',exact:true}).click();await info.locator('.metadata-error').waitFor();await info.getByText('Personal description',{exact:true}).waitFor();
  fail=false;await info.locator('.metadata-error').getByRole('button',{name:'Retry',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('.metadata-actions button:disabled'));assert.equal(calls,2);await page.unroute('**/api/video/metadata/refresh',failRefresh);
  let release,started,finished;const gate=new Promise(resolve=>release=resolve),start=new Promise(resolve=>started=resolve),finish=new Promise(resolve=>finished=resolve);
  const delayed=async route=>{const response=await route.fetch();started();await gate;try{await route.fulfill({response});}finally{finished();}};
  await page.route('**/api/video/progress/read',delayed);await info.locator('[data-episode-path="'+episode.path+'"]').click();await start;
  await info.getByRole('button',{name:'Refresh',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('.metadata-actions button:disabled'));release();await finish;await page.unroute('**/api/video/progress/read',delayed);await page.waitForFunction(()=>document.querySelector('video')?.readyState>=1);
  const revision=(await api('/api/video/progress/read','POST',episode)).data.revision;const seek=page.getByRole('slider',{name:'Seek',exact:true});await seek.fill('5.5');await seek.dispatchEvent('input');await page.getByRole('button',{name:'Back to episodes',exact:true}).click();
  await info.getByRole('button',{name:'Refresh',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('.metadata-actions button:disabled'));await page.waitForFunction(()=>!!document.activeElement?.closest('.video-info'));
  const watch=(await api('/api/video/progress/read','POST',episode)).data;assert.equal(watch.revision,revision);assert(watch.position.position>=5.4);await page.keyboard.press('Escape');await info.waitFor({state:'detached'});
  check('Refresh failure retains confirmed details; Retry repeats refresh, a delayed refresh does not cancel opening playback, and watch revision/time plus keyboard focus remain intact');
  assert.equal((await api('/api/suite/modules/youtube/enable','POST')).status,200);assert.equal((await api('/api/suite/folders/apply','POST',{folders:[{path:config.media+'/../metadata-downloads',display_name:'Metadata notices',role:'youtube'}]})).status,200);
  const noticeSource=(await api('/api/files/sources')).data.find(source=>source.display_name==='Metadata notices');await page.getByRole('button',{name:'Open Keivotos menu'}).click();await page.locator('.app-drawer').getByRole('button',{name:'YouTube',exact:true}).click();
  await page.getByLabel('Search downloads or paste a YouTube URL').fill('https://www.youtube.com/watch?v=abcdefghijk');await page.getByRole('button',{name:'Download',exact:true}).click();const notice=page.locator('[data-download-notification]').last();await notice.waitFor();await notice.hover();
  await page.getByRole('button',{name:'Open Keivotos menu'}).click();await page.locator('.app-drawer').getByRole('button',{name:'Video',exact:true}).click();await search.fill('Original Series');await page.getByRole('button',{name:'Original Series',exact:true}).click();await info.getByRole('button',{name:'Edit',exact:true}).click();await editor.getByLabel('Description',{exact:true}).fill('Keep this draft');
  await notice.hover();await notice.getByRole('button',{name:'Downloads',exact:true}).click();await page.waitForTimeout(250);assert.equal(await page.locator('.downloads-view').count(),0);assert.equal(await editor.getByLabel('Description',{exact:true}).inputValue(),'Keep this draft');
  await editor.getByRole('button',{name:'Cancel',exact:true}).click();await decision.getByRole('button',{name:'Discard changes',exact:true}).click();await info.getByRole('button',{name:'Back to videos',exact:true}).click();await notice.hover();await notice.getByRole('button',{name:'Downloads',exact:true}).click();await page.locator('.downloads-view').waitFor();
  assert.equal((await getDetails()).fields.summary,'Personal description');await page.waitForFunction(async id=>{const rows=await(await fetch('/api/youtube/library')).json();return rows.items.some(row=>row.source_id===id&&row.status==='complete');},noticeSource.source_id);
  await page.getByRole('button',{name:'Open Keivotos menu'}).click();await page.locator('.app-drawer').getByRole('button',{name:'Video',exact:true}).click();
  check('The existing shell blocks the Downloads shortcut while a details editor is open, preserving its draft; after explicit Discard/close the same shortcut opens Downloads');
  const folder=path.join(path.dirname(config.mediaLocal),'metadata-library');const hashes=JSON.parse(fs.readFileSync(path.join(path.dirname(config.mediaLocal),'metadata-original-hashes.json'),'utf8'));for(const [name,hash] of Object.entries(hashes))assert.equal(require('node:crypto').createHash('sha256').update(fs.readFileSync(path.join(folder,name))).digest('hex'),hash,name);
  report.continuation.metadata=await getDetails();assert.equal((await api('/api/video/library?'+new URLSearchParams({q:'native tag'}))).data.total,1);assert.equal((await api('/api/video/library?'+new URLSearchParams({q:'Ambiguous shared movie title'}))).data.total,0);
  check('Every original metadata/manifest/video byte stays unchanged; native tags are searchable, unsupported fields stay ignored and saved two-field details are ready for restart verification');
  await search.fill('Separate Season');await page.waitForFunction(()=>document.querySelectorAll('.video-tile').length===2);
  for(const [name,count] of [['Separate Season 1',3],['Separate Season 2',2]]){
   await page.getByRole('button',{name,exact:true}).click();const season=page.getByRole('dialog',{name:name+' episodes',exact:true});await page.waitForFunction(total=>document.querySelectorAll('.episode-row').length===total,count);
   assert.deepEqual(await season.locator('.episode-row').allTextContents().then(values=>values.map(value=>value.trim())),Array.from({length:count},(_,index)=>'Episode '+(index+1)));
   assert((await season.locator('.episode-row').evaluateAll(nodes=>nodes.map(node=>node.dataset.episodePath))).every(relative=>relative.startsWith(name+'/')));
   assert.equal(await season.locator('.season-name,.episode-details,.chevron').count(),0);await season.getByRole('button',{name:'Back to videos',exact:true}).click();
  }
  check('Separate sibling season folders stay separate titles and each shows only its actual episode count with clean names; files and folders are never renamed or merged');

  }
  assert.deepEqual(report.errors,[]);
 }finally{fs.writeFileSync(path.join(config.output,'report.json'),JSON.stringify(report,null,2));await context.tracing.stop({path:path.join(config.output,'trace.zip')});await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

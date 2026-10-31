const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
(async()=>{
 const browser=await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});
 const page=await context.newPage();page.setDefaultTimeout(15000);
 const report={checks:[],errors:[]};page.on('pageerror',e=>report.errors.push(e.message));
 await page.route('**/*',route=>{if(new URL(route.request().url()).origin!==config.url){report.errors.push('Unexpected remote request');return route.abort();}return route.continue();});
 await context.tracing.start({screenshots:true,snapshots:true});
 const check=x=>{report.checks.push(x);console.log('PASS: '+x);};
 async function api(url,method='GET',body){return page.evaluate(async({url,method,body})=>{const r=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});return {status:r.status,data:await r.json()};},{url,method,body});}
 async function until(fn,label){const end=Date.now()+20000;while(Date.now()<end){const value=await fn();if(value)return value;await page.waitForTimeout(120);}throw new Error('Timed out: '+label);}
 async function drawer(){if(await page.locator('.app-drawer').count())return;await page.getByRole('button',{name:'Open Keivotos menu'}).click();await page.waitForTimeout(240);}
 async function downloads(){await drawer();await page.locator('.drawer-downloads').click();await page.locator('.downloads-view').waitFor();}
 const row=id=>page.locator('[data-download-id="'+id+'"]');
 async function start(multipart=false){const r=await api('/api/fixture/downloads/start','POST',{multipart});assert.equal(r.status,200,JSON.stringify(r));await until(async()=>{const v=await api('/api/downloads/'+r.data.item_id);return v.status===200&&v.data.can_delete;},'active download');await row(r.data.item_id).waitFor();return r.data;}
 async function proof(job){return (await api('/api/fixture/downloads/proof')).data[job.job_id];}
 async function menu(job){await row(job.item_id).getByRole('button').click();await page.getByRole('menuitem',{name:'Delete',exact:true}).waitFor();}
 try{
  await page.goto(config.url);await drawer();
  const utility=page.locator('.drawer-downloads');
  assert.equal(await page.locator('.app-drawer nav').getByRole('button',{name:'Downloads',exact:true}).count(),0);
  assert.equal(await utility.locator('img').count(),0);assert.equal(Math.round((await utility.locator('svg').boundingBox()).width),18);
  assert.equal(await utility.evaluate(n=>n.parentElement.tagName),'FOOTER');
  assert(!(await api('/api/suite/modules')).data.some(m=>m.slug==='downloads'));
  await page.screenshot({path:path.join(config.output,'downloads-drawer.png')});
  const before=await page.evaluate(()=>localStorage.getItem('keivotos:active-module'));
  await utility.click();await page.waitForTimeout(65);
  const moving=await page.locator('.app-drawer').boundingBox();assert(moving.x<0&&moving.x>-320,JSON.stringify(moving));
  await page.locator('.downloads-view').waitFor();
  assert.equal(await page.evaluate(()=>localStorage.getItem('keivotos:active-module')),before);
  assert.equal(await page.locator('.downloads-scroll').innerText(),'');
  check('Downloads is a smaller footer utility outside modules; drawer exit animates before navigation and module preference is preserved');
  assert.equal((await api('/api/suite/modules/youtube/enable','POST')).status,200);
  assert.equal((await api('/api/suite/folders/apply','POST',{folders:[{path:config.media,display_name:'Fixture downloads',role:'youtube'}]})).status,200);
  assert.equal((await api('/api/fixture/downloads/seed','POST',{count:280})).status,200);
  await until(async()=>await page.locator('.download-row').count()>=100,'history initial page');
  assert.equal(await page.locator('.download-row').count(),100);
  assert.equal(await page.locator('.download-row button').count(),0);
  assert(await page.locator('.download-day h2').count()>0);
  assert(await page.locator('.download-group hr+h3').count()>=3);
  await page.waitForFunction(()=>[...document.querySelectorAll('.download-thumbnail img')].some(i=>i.naturalWidth>0));
  const compact=await page.locator('.download-row').first().evaluate(n=>({height:n.getBoundingClientRect().height,bar:n.querySelector('.download-track').getBoundingClientRect().height,text:n.innerText,name:n.querySelector('.download-name').textContent}));
  assert.equal(compact.height,50);assert.equal(compact.bar,3);assert.deepEqual(compact.text.trim().split(/\s*\n\s*/),[compact.name,'100%']);
  const scroll=page.locator('.downloads-scroll');await scroll.focus();await page.keyboard.press('PageDown');await page.waitForTimeout(300);assert(await scroll.evaluate(n=>n.scrollTop)>100);
  await until(async()=>{await scroll.evaluate(n=>n.scrollTop=n.scrollHeight);return await page.locator('.download-row').count()===280;},'all 280 paged rows');
  assert.equal(await page.locator('.download-row').count(),280);
  assert.equal(await page.locator('.download-row').evaluateAll(ns=>new Set(ns.map(n=>n.dataset.downloadId)).size),280);
  await page.getByRole('heading',{name:'Unknown date',exact:true}).waitFor();
  await scroll.evaluate(n=>n.scrollTop=0);await page.waitForTimeout(300);
  await page.screenshot({path:path.join(config.output,'downloads-wide.png')});
  await page.setViewportSize({width:360,height:740});await page.waitForTimeout(300);
  assert(await page.locator('.download-name').evaluateAll(ns=>ns.some(n=>n.scrollWidth>n.clientWidth)));
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.screenshot({path:path.join(config.output,'downloads-narrow.png')});
  await page.setViewportSize({width:1440,height:1000});await scroll.evaluate(n=>n.scrollTop=0);
  check('Compact thumbnail/name/3px bar rows, dates and module separators; local thumbnails, long Unicode names, narrow layout and keyboard/lazy paging of 280 unique rows');

  // Use the real Download button while the admission gate is held.
  await page.getByRole('button',{name:'Pause all',exact:true}).click();
  await drawer();await page.locator('.app-drawer').getByRole('button',{name:'YouTube',exact:true}).click();
  const input=page.getByLabel('Search downloads or paste a YouTube URL');
  await input.fill('https://www.youtube.com/watch?v=zzzzzzzzzzz');
  const acknowledged=page.waitForResponse(r=>r.url().endsWith('/api/youtube/downloads')&&r.request().method()==='POST');
  await page.getByRole('button',{name:'Download',exact:true}).click();
  const realJob=await (await acknowledged).json();

  const downloadNotice=page.locator('[data-notification-id="youtube-download-'+realJob.id+'"]');
  await downloadNotice.waitFor();await page.waitForTimeout(220);
  const shortcut=downloadNotice.getByRole('button',{name:'Downloads',exact:true});
  assert.equal(await shortcut.count(),0,'Collapsed action is absent from the accessibility tree');
  assert(await downloadNotice.locator('.notification-action').evaluate(n=>n.inert));
  assert(!(await downloadNotice.evaluate(n=>n.contains(document.activeElement))),'Arrival never steals focus');
  const baseWidth=(await downloadNotice.locator('.notification-card').boundingBox()).width;
  await page.evaluate(id=>{
    window.__noticeWidths=[];
    const sample=()=>{const card=document.querySelector('[data-notification-id="'+id+'"] .notification-card');if(card)window.__noticeWidths.push(card.getBoundingClientRect().width);if(window.__noticeWidths.length<25)requestAnimationFrame(sample);};
    sample();
  },'youtube-download-'+realJob.id);
  await downloadNotice.hover();await page.waitForTimeout(240);
  const widths=await page.evaluate(()=>window.__noticeWidths);
  assert(widths.some(width=>width>baseWidth+.05&&width<baseWidth*1.014),JSON.stringify(widths));
  assert((await downloadNotice.locator('.notification-card').boundingBox()).width>baseWidth*1.014);
  await shortcut.waitFor();
  for(let reverse=0;reverse<3;reverse++){await page.mouse.move(700,80);await page.waitForTimeout(45);await downloadNotice.hover();await page.waitForTimeout(45);}
  await page.waitForTimeout(220);await page.waitForTimeout(6200);assert.equal(await downloadNotice.count(),1);
  await downloadNotice.getByRole('button',{name:'Dismiss notification'}).focus();await page.mouse.move(700,80);await page.waitForTimeout(6200);
  assert.equal(await downloadNotice.count(),1,'Keyboard hold survives pointer leaving and a full expiry interval');
  await page.keyboard.press('Tab');assert(await shortcut.evaluate(n=>n===document.activeElement));
  await page.screenshot({path:path.join(config.output,'notification-downloads-reveal.png')});
  await page.keyboard.press('Enter');await page.locator('.downloads-view').waitFor();
  assert(await page.locator('.downloads-scroll').evaluate(n=>n===document.activeElement));
  check('Download notice enlarges through measured intermediate frames; hover/focus holds, rapid reversals and keyboard shortcut preserve expiry and focus');

  const queuedPage=await api('/api/downloads?owner=youtube');
  const accepted=queuedPage.data.items.find(i=>i.state==='queued'&&i.name.includes('zzzzzzzzzzz'));
  assert(accepted,'Accepted real Download is present before worker/metadata');
  await row(accepted.id).waitFor();assert.equal(await row(accepted.id).locator('.download-percent').innerText(),'0%');
  assert.equal(accepted.started_at,null);assert.equal(accepted.completed_at,null);
  await page.getByRole('button',{name:'Resume',exact:true}).click();
  const expectedTitle='Fixture YouTube title '+realJob.id.slice(0,6);
  await until(async()=>await row(accepted.id).locator('.download-name').innerText()===expectedTitle,'real metadata title');
  await until(async()=>await row(accepted.id).locator('.download-percent').count()===1&&
    parseInt(await row(accepted.id).locator('.download-percent').innerText())>0,'visible live percentage');
  await page.screenshot({path:path.join(config.output,'youtube-download-progress.png')});
  const beforePercent=await row(accepted.id).locator('.download-percent').innerText();
  await until(async()=>await row(accepted.id).locator('.download-percent').innerText()!==beforePercent,'live percentage changes');
  await page.setViewportSize({width:360,height:740});
  assert.equal((await row(accepted.id).boundingBox()).height,50);
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.screenshot({path:path.join(config.output,'youtube-download-progress-narrow.png')});
  await page.setViewportSize({width:1440,height:1000});
  assert.equal((await api('/api/fixture/downloads/finish','POST',{job_id:realJob.id})).status,200);
  await until(async()=>{const v=await api('/api/downloads/'+accepted.id);return v.data.state==='completed';},'title retained after publication');
  assert.equal(await row(accepted.id).locator('.download-name').innerText(),expectedTitle);
  await until(async()=>await row(accepted.id).locator('.download-percent').innerText()==='100%','completed percentage');
  await page.reload();await downloads();
  await row(accepted.id).waitFor();
  assert.equal(await row(accepted.id).locator('.download-name').innerText(),expectedTitle);
  assert.equal(await row(accepted.id).locator('.download-percent').innerText(),'100%');
  assert.equal((await api('/api/fixture/downloads/proof')).data._legacy.name,'video.webm');
  check('Real Download click appears before metadata with 0%; saved title repairs old filename rows read-only; title/live percentage survive transfer, completion and reload without growing rows');

  const first=await start();await scroll.evaluate(n=>n.scrollTop=0);
  await menu(first);assert(await page.getByRole('menuitem',{name:'Delete',exact:true}).evaluate(n=>n===document.activeElement));
  await page.keyboard.press('Escape');assert.equal(await page.getByRole('menuitem',{name:'Delete',exact:true}).count(),0);
  assert(await row(first.item_id).getByRole('button').evaluate(n=>n===document.activeElement));
  await menu(first);await page.locator('h1').click();assert.equal(await page.getByRole('menuitem',{name:'Delete',exact:true}).count(),0);
  await menu(first);const menuBox=await page.getByRole('menu').boundingBox();assert(menuBox.x>=0&&menuBox.y>=0&&menuBox.x+menuBox.width<=1440);
  await page.screenshot({path:path.join(config.output,'downloads-menu.png')});await page.keyboard.press('Escape');
  await page.getByRole('button',{name:'Pause all',exact:true}).click();await page.getByRole('button',{name:'Resume',exact:true}).waitFor();
  await page.waitForTimeout(250);const held=await proof(first);await page.waitForTimeout(600);assert.deepEqual((await proof(first)).parts,held.parts);
  const queued=(await api('/api/fixture/downloads/start','POST',{})).data;await page.waitForTimeout(400);assert.equal((await proof(queued)).status,'queued');
  await page.getByRole('button',{name:'Resume',exact:true}).click();await page.getByRole('button',{name:'Pause all',exact:true}).waitFor();
  await until(async()=>JSON.stringify((await proof(first)).parts)!==JSON.stringify(held.parts),'resumed writes');
  await menu(first);await page.getByRole('menuitem',{name:'Delete',exact:true}).click();await row(first.item_id).waitFor({state:'detached'});
  const removed=await proof(first);assert(Object.values(removed.parts).every(x=>x===null));assert.equal(removed.metadata,held.metadata);
  await until(async()=>(await api('/api/downloads/'+queued.item_id)).data.can_delete,'queued worker starts after delete');
  await row(queued.item_id).waitFor();
  check('Keyboard menu focus/Escape/outside close; Pause freezes real writes and queue admission, Resume continues, Delete drains worker/removes its partial and preserves metadata');
  const stale=(await api('/api/downloads/'+queued.item_id)).data;
  await api('/api/fixture/downloads/finish','POST',{job_id:queued.job_id});
  await until(async()=>(await proof(queued)).published,'completed publication');
  await until(async()=>await row(queued.item_id).getByRole('button').count()===0,'completed menu removed');
  const staleDelete=await api('/api/downloads/'+queued.item_id+'/delete','POST',{request_id:await page.evaluate(()=>crypto.randomUUID()),expected_version:stale.control_version,confirm:false});
  assert.equal(staleDelete.status,409);assert((await proof(queued)).published);
  const multi=await start(true);await menu(multi);await page.getByRole('menuitem',{name:'Delete',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'Delete unfinished download?',exact:true});await dialog.waitFor();
  assert((await dialog.innerText()).includes('2 unfinished files'));assert(Object.values((await proof(multi)).parts).every(x=>x!==null));
  await dialog.getByRole('button',{name:'Keep files',exact:true}).click();assert(Object.values((await proof(multi)).parts).every(x=>x!==null));
  const multi2=await start(true);const preserved=await proof(multi2);await menu(multi2);await page.getByRole('menuitem',{name:'Delete',exact:true}).click();await dialog.waitFor();
  await dialog.getByRole('button',{name:'Delete',exact:true}).click();await row(multi2.item_id).waitFor({state:'detached'});
  assert(Object.values((await proof(multi2)).parts).every(x=>x===null));assert.equal((await proof(multi2)).metadata,preserved.metadata);
  check('Completion wins a stale Delete race; multi-file cleanup states its count before confirmation, Keep files preserves both partials and confirmed Delete preserves metadata');
  await page.emulateMedia({reducedMotion:'reduce'});
  await page.evaluate(()=>document.documentElement.dataset.motion='reduced');
  assert(await page.locator('.download-fill').evaluateAll(ns=>ns.every(n=>getComputedStyle(n).animationName==='none')));
  await api('/api/fixture/downloads/language','POST');await api('/api/suite/modules/language/enable','POST');
  await drawer();await page.locator('.app-drawer').getByRole('button',{name:'Language',exact:true}).click();
  await page.getByRole('tab',{name:'Welcome.md',exact:true}).click();
  const editor=page.getByRole('textbox',{name:'Edit Welcome.md'});await editor.fill('# Unsaved Downloads guard');
  await drawer();await page.locator('.drawer-downloads').click();
  const decision=page.getByRole('dialog',{name:'Unsaved Markdown changes'});await decision.waitFor();
  assert((await decision.innerText()).includes('Downloads'));
  await decision.getByRole('button',{name:'Keep editing',exact:true}).click();assert.equal(await editor.innerText(),'# Unsaved Downloads guard');
  await drawer();await page.locator('.drawer-downloads').click();await decision.getByRole('button',{name:'Discard changes',exact:true}).click();
  await page.locator('.downloads-view').waitFor();await page.waitForTimeout(500);
  check('Reduced motion disables bar animation; Language draft guard names Downloads, Keep editing retains text and explicit discard permits utility navigation');

  await page.emulateMedia({reducedMotion:'no-preference'});
  await page.evaluate(()=>{delete document.documentElement.dataset.motion;});
  let noticeNumber=0;
  async function newNotice(){
    await drawer();await page.locator('.app-drawer').getByRole('button',{name:'YouTube',exact:true}).click();
    const field=page.getByLabel('Search downloads or paste a YouTube URL');
    const url='https://www.youtube.com/watch?v=notify'+String(++noticeNumber).padStart(5,'0');
    await field.fill(url);
    const response=page.waitForResponse(r=>r.url().endsWith('/api/youtube/downloads')&&r.request().method()==='POST');
    await page.getByRole('button',{name:'Download',exact:true}).click();
    const job=await(await response).json();
    const note=page.locator('[data-notification-id="youtube-download-'+job.id+'"]');
    await note.waitFor();await note.hover();await page.waitForTimeout(220);
    return {job,note};
  }
  async function dismissAll(){
    const buttons=page.getByRole('button',{name:'Dismiss notification',exact:true});
    while(await buttons.count())await buttons.first().click();
  }
  async function closeMenu(){
    if(await page.locator('.app-drawer').count()){await page.keyboard.press('Escape');await page.locator('.app-drawer').waitFor({state:'detached'});}
  }
  for(const corner of ['bottom-right','bottom-left','top-right','top-left']){
    await dismissAll();await drawer();await page.locator('.app-drawer').getByRole('button',{name:'Settings',exact:true}).click();
    const settings=page.getByRole('dialog',{name:'Settings',exact:true});
    await settings.getByRole('navigation',{name:'Settings sections'}).getByRole('button',{name:'Appearance',exact:true}).first().click();
    await page.getByLabel('Notification position',{exact:true}).selectOption(corner);
    await page.getByRole('button',{name:'Close settings',exact:true}).click();await closeMenu();
    const {job,note}=await newNotice();
    for(const [width,height] of [[1440,1000],[360,440]]){
      await page.setViewportSize({width,height});await note.hover();await page.waitForTimeout(220);
      const card=await note.locator('.notification-card').boundingBox(),actionBox=await note.getByRole('button',{name:'Downloads',exact:true}).boundingBox();
      assert(card.x>=0&&card.y>=0&&card.x+card.width<=width&&card.y+card.height<=height,JSON.stringify({corner,card,width,height}));
      assert(actionBox.x>=card.x&&actionBox.y>=card.y&&actionBox.x+actionBox.width<=card.x+card.width);
      await note.getByRole('button',{name:'Downloads',exact:true}).hover();
      assert(await note.evaluate(n=>n.classList.contains('expanded')),'Pointer can move onto the revealed action');
    }
    await page.screenshot({path:path.join(config.output,'notification-'+corner+'-narrow.png')});
    await page.setViewportSize({width:1440,height:1000});
    await note.getByRole('button',{name:'Dismiss notification'}).click();
    await api('/api/youtube/downloads/'+job.id+'/cancel','POST');
  }
  check('Revealed action remains reachable and unclipped at all four saved corners in wide and short/narrow windows');
  // Restore the default corner before guard cases so a top-left toast does
  // not occupy the menu button used to prepare those cases.
  await dismissAll();await drawer();await page.locator('.app-drawer').getByRole('button',{name:'Settings',exact:true}).click();
  await page.getByRole('dialog',{name:'Settings',exact:true}).getByRole('navigation',{name:'Settings sections'}).getByRole('button',{name:'Appearance',exact:true}).first().click();
  await page.getByLabel('Notification position',{exact:true}).selectOption('bottom-right');
  await page.getByRole('button',{name:'Close settings',exact:true}).click();await closeMenu();
  // Keep an existing notification while preparing unsaved Language work.
  await dismissAll();
  const guardNotice=await newNotice();
  await drawer();await page.locator('.app-drawer').getByRole('button',{name:'Settings',exact:true}).click();
  await guardNotice.note.hover();assert(await guardNotice.note.getByRole('button',{name:'Downloads',exact:true}).isDisabled());
  await page.getByRole('button',{name:'Close settings',exact:true}).click();await closeMenu();
  await drawer();await page.locator('.app-drawer').getByRole('button',{name:'Language',exact:true}).click();
  await page.getByRole('tab',{name:'Welcome.md',exact:true}).click();
  const guardedEditor=page.getByRole('textbox',{name:'Edit Welcome.md'});
  await guardedEditor.fill('# Keep this notification navigation draft');
  await guardNotice.note.hover();await guardNotice.note.getByRole('button',{name:'Downloads',exact:true}).click();
  const guardDecision=page.getByRole('dialog',{name:'Unsaved Markdown changes'});await guardDecision.waitFor();
  assert((await guardDecision.innerText()).includes('Downloads'));
  await guardDecision.getByRole('button',{name:'Keep editing',exact:true}).click();
  assert.equal(await guardedEditor.innerText(),'# Keep this notification navigation draft');
  await guardNotice.note.hover();await guardNotice.note.getByRole('button',{name:'Downloads',exact:true}).click();
  await guardDecision.getByRole('button',{name:'Discard changes',exact:true}).click();
  await page.locator('.downloads-view').waitFor();assert.equal((await api('/api/fixture/downloads/proof')).data._notes,'# Local note\n');
  await api('/api/youtube/downloads/'+guardNotice.job.id+'/cancel','POST');
  await dismissAll();
  const saveNotice=await newNotice();
  await drawer();await page.locator('.app-drawer').getByRole('button',{name:'Language',exact:true}).click();
  await page.getByRole('tab',{name:'Welcome.md',exact:true}).click();
  await guardedEditor.fill('# Saved through notification navigation\n');
  await saveNotice.note.hover();await saveNotice.note.getByRole('button',{name:'Downloads',exact:true}).click();
  await guardDecision.getByRole('button',{name:'Save',exact:true}).click();await page.locator('.downloads-view').waitFor();
  assert.equal((await api('/api/fixture/downloads/proof')).data._notes,'# Saved through notification navigation\n');
  await api('/api/youtube/downloads/'+saveNotice.job.id+'/cancel','POST');
  check('Notification navigation uses Language Keep editing/Discard/Save decisions and preserves or saves the exact Markdown bytes');
  await dismissAll();
  const vocabularyNotice=await newNotice();
  await drawer();await page.locator('.app-drawer').getByRole('button',{name:'Language',exact:true}).click();
  const langGrip=page.locator('[data-sidebar-owner="language"] [data-module-sidebar-grip]');
  if(await langGrip.getAttribute('aria-expanded')==='false'){await langGrip.click();await page.waitForTimeout(330);}
  await page.getByRole('navigation',{name:'Language sections'}).getByRole('button',{name:'Vocabulary',exact:true}).click();
  await page.getByRole('button',{name:'New vocabulary entry',exact:true}).click();
  await page.getByLabel('Korean word',{exact:true}).fill('보관');
  await vocabularyNotice.note.hover();await vocabularyNotice.note.getByRole('button',{name:'Downloads',exact:true}).click();
  const vocabularyDecision=page.getByRole('dialog',{name:'Unsaved vocabulary',exact:true});await vocabularyDecision.waitFor();
  await vocabularyDecision.getByRole('button',{name:'Keep editing',exact:true}).click();
  assert.equal(await page.getByLabel('Korean word',{exact:true}).inputValue(),'보관');
  await vocabularyNotice.note.hover();await vocabularyNotice.note.getByRole('button',{name:'Downloads',exact:true}).click();
  await vocabularyDecision.getByRole('button',{name:'Discard',exact:true}).click();await page.locator('.downloads-view').waitFor();
  await api('/api/youtube/downloads/'+vocabularyNotice.job.id+'/cancel','POST');
  check('Unsaved Vocabulary also guards the notification shortcut without bypassing its retained draft');
  await dismissAll();
  const reducedNotice=await newNotice();
  await page.emulateMedia({reducedMotion:'reduce'});await reducedNotice.note.hover();await page.waitForTimeout(30);
  assert.equal(await reducedNotice.note.locator('.notification-card').evaluate(n=>getComputedStyle(n).transform),'none');
  assert(await reducedNotice.note.locator('.notification-action').evaluate(n=>parseFloat(getComputedStyle(n).transitionDuration)<.001));
  await reducedNotice.note.getByRole('button',{name:'Downloads',exact:true}).click();await page.locator('.downloads-view').waitFor();
  await api('/api/youtube/downloads/'+reducedNotice.job.id+'/cancel','POST');
  await page.emulateMedia({reducedMotion:'no-preference'});
  await dismissAll();
  await page.evaluate(()=>localStorage.setItem('keivotos:motion-preference',JSON.stringify('reduced')));await page.reload();
  const appReducedNotice=await newNotice();
  assert.equal(await appReducedNotice.note.locator('.notification-card').evaluate(n=>getComputedStyle(n).transform),'none');
  assert(await appReducedNotice.note.locator('.notification-action').evaluate(n=>parseFloat(getComputedStyle(n).transitionDuration)<.001));
  await api('/api/youtube/downloads/'+appReducedNotice.job.id+'/cancel','POST');
  await appReducedNotice.note.getByRole('button',{name:'Dismiss notification'}).click();
  check('System and app reduced-motion preferences reveal an accessible action instantly and suppress enlargement; Settings blocks shortcut activation');

  assert.deepEqual(report.errors,[]);
 }catch(error){report.failure=error.stack;await page.screenshot({path:path.join(config.output,'failure.png')}).catch(()=>{});throw error;}
 finally{fs.writeFileSync(path.join(config.output,'report.json'),JSON.stringify(report,null,2));await context.tracing.stop({path:path.join(config.output,'trace.zip')});await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});

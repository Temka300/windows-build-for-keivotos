// Owner Settings baseline with real preferences and explicit metadata-only fixtures.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
assert.match(config.home, /[/\\]keivotos-modularization-[^/\\]+[/\\]home$/);
const report = { checks: [], errors: [], requests: [] };
(async () => {
 const browser = await chromium.launch({ headless:true,channel:process.env.PLAYWRIGHT_CHANNEL });
 const context = await browser.newContext({ viewport:{width:1440,height:1000} });
 await context.tracing.start({ screenshots:true,snapshots:true });
 const page = await context.newPage();
 const expectedHttpErrors = new Set();
 page.setDefaultTimeout(10000);
 page.on('pageerror', e=>report.errors.push(e.message));
 page.on('console', m=>{if(m.type()==='error' && !(expectedHttpErrors.has(m.location().url) && m.text().startsWith('Failed to load resource:')))report.errors.push(m.text());});
 page.on('response', r=>{if(r.status()>=400 && !expectedHttpErrors.has(r.url()))report.errors.push(`${r.status()} ${r.url()}`);});
 let credentialReads=0, releaseReads=0, releaseReply;
 const releaseGate=new Promise(resolve=>releaseReply=resolve);
 let toolFixture=false, toolState="running";
 await page.route('**/*',async route=>{
  const u=new URL(route.request().url());
  if(u.origin!==config.url){report.errors.push('External request '+u.href);return route.abort();}
  report.requests.push(`${route.request().method()} ${u.pathname}`);
  if(u.pathname==='/api/suite/release'){
   releaseReads++;
   await releaseGate;
   return route.fulfill({json:{version,tag_name:`v${version}`,published_at:'2027-01-15T15:30:00Z'}});
  }
  if(toolFixture){
   const folder={name:'Fixture root',selector:'fixture',root_id:'fixture',path:config.home+'/fixture',count:3,registered:true};
   if(u.pathname==='/api/tools')return route.fulfill({json:[
    {id:'sync',name:'Sync',description:'Fixture sync',status:toolState},
    {id:'sqlite',name:'Rebuild',description:'Fixture rebuild',status:'idle'},
    {id:'clean-sidecars',name:'Clean',description:'Fixture clean',status:'idle'},
   ]});
   if(u.pathname==='/api/tools/sync/status')return route.fulfill({json:{status:toolState,output:'Fixture task',progress:1,total:3}});
   if(u.pathname==='/api/folders')return route.fulfill({json:[folder]});
   if(u.pathname==='/api/folders/fixture/removal-preview')return route.fulfill({json:{...folder,indexed_files:3,sidecar_files:2,sidecar_bytes:100,external_images_affected:0,sidecar_history_preserved:true}});
  }

  if(u.pathname==='/api/danbooru/credentials'){
   assert.equal(route.request().method(),'GET');credentialReads++;
   return route.fulfill({json:{configured:false,source:'none',username:null,has_api_key:false}});
  }
  return route.continue();
 });
 const check=s=>{report.checks.push(s);console.log('PASS: '+s);};
 const dialog=page.getByRole('dialog',{name:'Settings',exact:true});
 const section=s=>dialog.getByRole('navigation',{name:'Settings sections'}).getByRole('button',{name:s,exact:true}).last();
 const productSource=fs.readFileSync(path.join(process.env.KEIVOTOS_SOURCE_ROOT||path.join(__dirname,'..'),'frontend','src','lib','product.ts'),'utf8');
 const version=productSource.match(/^export const VERSION = '([^']+)';$/m)[1];
 async function open(){await page.getByRole('button',{name:'Open Keivotos menu',exact:true}).click();await page.getByRole('button',{name:'Settings',exact:true}).click();await dialog.waitFor();}
 try{
  await page.goto(config.url);
  await open();
  assert.equal(await section('Account').count(),0);
  assert.deepEqual(await dialog.locator('nav button').allTextContents(),['Modules','Appearance','Startup','Categories','Player','Data & storage','Backup','Advanced','About','Folders','Attachments']);
  for(const name of ['Startup','Player','Data & storage','Backup','Folders','Attachments','Appearance'])await section(name).click();
  await section('Player').click();
  const hold=dialog.getByRole('switch',{name:'Hold Space to speed up',exact:true}),multiplier=dialog.getByLabel('Hold speed multiplier',{exact:true});
  assert.equal(await hold.getAttribute('aria-checked'),'true');assert.equal(await multiplier.inputValue(),'1.5');
  await hold.click();assert(await multiplier.isDisabled());assert.equal(await page.evaluate(()=>localStorage.getItem('keivotos:player-hold-space')),'false');await hold.click();await multiplier.selectOption('2');
  const holdStorage=await page.evaluate(()=>({enabled:localStorage.getItem('keivotos:player-hold-space'),multiplier:localStorage.getItem('keivotos:player-hold-speed-multiplier')}));assert.deepEqual(holdStorage,{enabled:'true',multiplier:'2'});
  await section('Appearance').click();await section('Player').click();assert.equal(await multiplier.inputValue(),'2');
  await hold.click();await dialog.getByRole('button',{name:'Close settings',exact:true}).click();await dialog.waitFor({state:'detached'});await page.reload();await open();await section('Player').click();assert.equal(await hold.getAttribute('aria-checked'),'false');assert.equal(await multiplier.inputValue(),'2');assert(await multiplier.isDisabled());await hold.click();
  await dialog.getByPlaceholder('Find a setting').fill('hold space');await dialog.getByRole('button',{name:/Hold Space to speed up/}).click();await page.locator('#setting-hold-space').waitFor();assert(await page.locator('#setting-hold-space').evaluate(node=>node.classList.contains('setting-flash')));
  check('Hold settings survive a real reload and Settings search jumps to the existing Player control');

  await page.setViewportSize({width:390,height:780});await dialog.getByLabel('Hide controls after',{exact:true}).selectOption('10');await multiplier.scrollIntoViewIfNeeded();const holdBounds=await multiplier.boundingBox();assert(holdBounds.x>=0&&holdBounds.x+holdBounds.width<=390);for(const id of ['setting-hide-controls-seconds','setting-hold-space','setting-hold-speed-multiplier']){assert(await page.locator('#'+id).evaluate(node=>node.scrollWidth<=node.clientWidth+1),'Player row must not clip or overflow: '+id);}await page.screenshot({path:path.join(config.output,'hold-player-settings-narrow.png')});await page.setViewportSize({width:1440,height:1000});await dialog.getByLabel('Hide controls after',{exact:true}).selectOption('5');await multiplier.selectOption('1.5');
  check('Existing Player section has validated enabled/default/multiplier controls, disabled state, retained values and narrow geometry');

  assert.equal(releaseReads,0,'No release lookup before opening About');
  await section('About').click();
  const about=dialog.getByRole('region',{name:'About Keivotos'});
  await about.waitFor();
  const logo=about.getByRole('img',{name:'Keivotos logo'});
  await logo.waitFor();
  assert.equal(await logo.evaluate(image=>image.complete&&image.naturalWidth>0),true);
  await page.waitForFunction(()=>document.querySelector('#setting-about')!==null);
  assert.equal(await about.locator('#setting-about').textContent().then(text=>text.trim()),`Version ${version}`);
  await section('Appearance').click();await section('About').click();
  assert.equal(releaseReads,1,'Pending request shared across rapid navigation');
  releaseReply();
  await page.waitForFunction(expected=>document.querySelector('#setting-about')?.textContent.trim()===expected,`Version ${version} (2027/01/16)`);
  await page.screenshot({path:path.join(config.output,'about-modern.png')});
  await dialog.getByRole('searchbox').fill('release');
  await dialog.getByRole('button',{name:/About Keivotos/}).last().click();
  await dialog.locator('#setting-about.setting-flash').waitFor();
  await page.setViewportSize({width:600,height:480});
  await section('About').click();
  assert.equal(await about.isVisible(),true);
  assert.equal(await about.locator('#setting-about').textContent().then(text=>text.trim()),`Version ${version} (2027/01/16)`);
  await page.screenshot({path:path.join(config.output,'about-legacy.png')});
  await dialog.getByRole('navigation',{name:'Settings sections'}).getByRole('button',{name:'Advanced',exact:true}).first().click();
  await dialog.getByLabel('Settings style').selectOption('legacy');
  await section('About').click();
  assert.equal(await about.isVisible(),true);
  await dialog.getByRole('navigation',{name:'Settings sections'}).getByRole('button',{name:'Advanced',exact:true}).first().click();
  await dialog.getByLabel('Settings style').selectOption('modern');
  await page.setViewportSize({width:1440,height:1000});
  assert.equal(releaseReads,1,'About visits and both styles reuse the verified result');
  check('About exact date, delayed navigation, logo, search, compact layout and both styles');
  assert.equal(credentialReads,0);
  await page.getByRole('button',{name:'Close settings',exact:true}).click();
  await page.getByRole('button', {name:'Settings',exact:true}).click();
  await page.getByRole('navigation', {name:'Settings sections'}).getByRole('button', {name:'Modules',exact:true}).click();
  await page.getByRole('switch', {name:'Danbooru module',exact:true}).click();
  await page.waitForFunction(() => document.querySelector('[data-module-id="danbooru"] [role="switch"]')?.getAttribute('aria-checked') === 'true');
  await page.getByRole('button', {name:'Close settings',exact:true}).click();
  await page.getByRole('dialog', {name:'Settings',exact:true}).waitFor({state:'detached'});
  await page.locator('.app-drawer').getByRole('button', {name:'Danbooru',exact:true}).click();
  await open();
  assert.deepEqual(await dialog.locator('nav button').allTextContents(),['Modules','Appearance','Startup','Categories','Player','Data & storage','Backup','Advanced','About','Folders','Attachments','Account','Browsing','Display','Library','Advanced']);
  await section('Player').click();
  await dialog.getByLabel('Hide controls after').selectOption('1');
  assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('keivotos:player-hide-controls-seconds'))),1);
  await section('Account').click();
  await dialog.locator('#setting-danbooru-access > summary').click();
  await dialog.getByRole('textbox',{name:'Username',exact:true}).fill('unsaved_fixture');
  await section('Appearance').click();
  await section('Account').click();
  await dialog.locator('#setting-danbooru-access > summary').click();
  assert.equal(await dialog.getByRole('textbox',{name:'Username',exact:true}).inputValue(),'unsaved_fixture');
  assert.equal(credentialReads,1);
  check('owner section order, disabled visibility, lazy account loading and draft persistence');
  await dialog.getByRole('button',{name:'Betabooru',exact:true}).click();
  await page.waitForFunction(async()=>(await(await fetch('/api/danbooru/host')).json()).host==='betabooru');
  assert.equal(await dialog.getByRole('button',{name:'Betabooru',exact:true}).getAttribute('aria-pressed'),'true');
  check('manual Danbooru host selection saves to isolated config');
  await section('Browsing').click();
  await page.locator('#setting-home-layout').getByRole('button',{name:'Classic',exact:true}).click();
  await section('Display').click();
  await page.locator('#setting-media-playback').getByRole('button',{name:'Always',exact:true}).click();
  await section('Appearance').click();
  await page.locator('#setting-motion').getByRole('button',{name:'Reduced',exact:true}).click();
  assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('keivotos:home-layout'))),'classic');
  assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('keivotos:media-autoplay'))),'always');
  check('General and Danbooru preferences retain their existing storage keys');
  for(const name of ['Library','Advanced','Folders','Attachments','Data & storage','Backup']) {await section(name).click();await page.waitForTimeout(150);}
  await dialog.getByRole('searchbox').fill('animated media');
  await dialog.getByRole('button').filter({hasText:'Animated media'}).click();
  await page.locator('#setting-media-playback.setting-flash').waitFor();
  await page.waitForTimeout(1500);
  assert.equal(await page.locator('.setting-flash').count(),0);
  await section('Folders').click();
  await dialog.getByRole('button',{name:'Manage folders…',exact:true}).click();
  // The folder manager is a separate overlay and must escape the scroll pane.
  const manager=page.getByRole('dialog',{name:'Manage folders',exact:true});
  await manager.waitFor();
  assert.equal(await manager.evaluate(n=>!!n.closest('.settings-scroll')),false);
  await manager.getByRole('button',{name:'Cancel',exact:true}).click();
  await dialog.waitFor();
  check('all owner sections render, module search highlight expires, folder overlay stays outside scroll containment');
  await page.getByRole('button',{name:'Close settings',exact:true}).click();
  assert.equal(await page.evaluate(()=>document.documentElement.dataset.settingsOpen),undefined);
  await page.keyboard.press('Escape');
  await page.locator('.app-drawer').waitFor({state:'detached'});
  toolFixture=true;
  // Exercise the presentation resume hook without downloading/decoding media.
  await page.evaluate(()=>{
   const video=document.createElement('video');video.id='presentation-fixture';
   let paused=false;video.dataset.plays='0';video.dataset.pauses='0';
   Object.defineProperty(video,'paused',{get:()=>paused});
   video.pause=()=>{paused=true;video.dataset.pauses=String(Number(video.dataset.pauses)+1);};
   video.play=()=>{paused=false;video.dataset.plays=String(Number(video.dataset.plays)+1);return Promise.resolve();};
   document.querySelector('#app').appendChild(video);
  });
  await open();
  await section('Library').click();
  await dialog.getByText('Fixture task',{exact:true}).waitFor();
  await section('Backup').click();
  await page.waitForFunction(()=>[...document.querySelectorAll('button')].some(b=>b.textContent.trim()==='Back up now'&&b.disabled));
  toolState='done';
  await page.waitForFunction(()=>[...document.querySelectorAll('button')].some(b=>b.textContent.trim()==='Back up now'&&!b.disabled));
  check('module polling survives section changes and releases shared backup busy controls');
  await section('Advanced').click();
  await dialog.getByRole('button',{name:'Delete sidecars…',exact:true}).click();
  const removal=page.getByRole('dialog',{name:'What should be removed?',exact:true});
  await removal.getByText('External images affected: 0',{exact:true}).waitFor();
  await removal.getByRole('button',{name:/^Un-index only/}).waitFor();
  assert.equal(await removal.getByRole('button',{name:/^Un-index only/}).isEnabled(),true);
  assert.equal(await removal.evaluate(n=>!!n.closest('.settings-scroll')),false);
  await page.keyboard.press('Escape');
  await removal.waitFor({state:'detached'});await dialog.waitFor();
  await page.getByRole('button',{name:'Close settings',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#presentation-fixture').dataset.plays==='1');
  assert.equal(await page.locator('#presentation-fixture').getAttribute('data-pauses'),'1');
  assert.equal(await page.getByRole('dialog',{name:'What should be removed?',exact:true}).count(),0);
  assert(!report.requests.some(r=>r.startsWith('DELETE ')));
  check('module removal overlay keeps its warning, Escape leaves Settings open, media resume hook survives cleanup');

  await page.reload();
  await open();
  await section('Account').click();
  await dialog.locator('#setting-danbooru-access > summary').click();
  assert.equal(await dialog.getByRole('button',{name:'Betabooru',exact:true}).getAttribute('aria-pressed'),'true');
  check('selected Danbooru host survives reload');

  toolFixture=false;
  await section('Folders').click();
  const actions=dialog.getByLabel('Folder actions').first();
  const editor=page.getByRole('dialog',{name:'Edit folder path',exact:true});
  async function edit(){await actions.click();await dialog.getByRole('button',{name:'Edit path…',exact:true}).first().click();await editor.waitFor();}
  await edit();
  const pathField=editor.getByRole('textbox',{name:'Folder path',exact:true});
  assert.equal(await pathField.inputValue(),'D:\\Library');
  assert.equal(await pathField.evaluate(n=>n===document.activeElement),true);
  for(let i=0;i<9;i++){await page.keyboard.press('Tab');assert.equal(await editor.evaluate(n=>n.contains(document.activeElement)),true);}
  await pathField.fill('/cancelled edit');
  const mutationsBefore=report.requests.filter(request=>request.startsWith('PUT /api/suite/folders/')).length;
  await pathField.dispatchEvent('compositionstart');
  await editor.getByRole('button',{name:'Save',exact:true}).click();
  await page.waitForTimeout(80);
  assert.equal(report.requests.filter(request=>request.startsWith('PUT /api/suite/folders/')).length,mutationsBefore,'IME composition must not submit the path');
  await pathField.dispatchEvent('compositionend');
  await editor.getByRole('button',{name:'Cancel',exact:true}).click();
  const registered=()=>page.evaluate(async()=>(await fetch('/api/files/sources')).json());
  assert.equal((await registered()).find(s=>s.source_id==='copied-windows-root').path,'D:\\Library');
  check('offline Windows folder offers Edit path, traps focus and Cancel preserves registration');

  await edit();
  await pathField.fill(config.media+'/missing folder');
  const endpoint=config.url+'/api/suite/folders/copied-windows-root/path';
  expectedHttpErrors.add(endpoint);
  await editor.getByRole('button',{name:'Save',exact:true}).click();
  await editor.getByRole('alert').filter({hasText:'Folder does not exist'}).waitFor();
  assert.equal((await registered()).find(s=>s.source_id==='copied-windows-root').path,'D:\\Library');
  await pathField.fill('D:\\Library');
  assert.equal(await editor.getByRole('alert').count(),0);
  expectedHttpErrors.add(config.url+'/api/files/fs?'+new URLSearchParams({path:'D:\\Library'}));
  await editor.getByRole('button',{name:'Browse…',exact:true}).click();
  const picker=page.getByRole('dialog',{name:'Choose a folder',exact:true});
  await picker.waitFor();
  await picker.getByRole('textbox',{name:'Folder path'}).fill(config.media+'/Copied library 한글');
  await picker.getByRole('button',{name:'Go',exact:true}).click();
  await page.waitForFunction(()=>!document.querySelector('dialog[aria-label="Choose a folder"] footer button:last-child')?.disabled);
  await picker.getByRole('button',{name:'Choose this folder',exact:true}).click();
  assert.equal(await pathField.inputValue(),config.media+'/Copied library 한글');
  assert.equal((await registered()).find(s=>s.source_id==='copied-windows-root').path,'D:\\Library');
  check('invalid Save preserves registration; Browse stages a Unicode Linux location without implicit save');

  let releaseSave;
  const gate=new Promise(resolve=>{releaseSave=resolve;});
  await page.route('**/api/suite/folders/copied-windows-root/path',async route=>{await gate;await route.continue();});
  await editor.getByRole('button',{name:'Save',exact:true}).click();
  await editor.getByRole('button',{name:'Saving…',exact:true}).waitFor();
  await page.keyboard.press('Escape');
  assert.equal(await editor.isVisible(),true);
  assert.equal(await editor.getByRole('button',{name:'Cancel',exact:true}).isDisabled(),true);
  releaseSave();
  await editor.waitFor({state:'hidden'});
  await dialog.getByText('Updated Copied library. Files were not moved.',{exact:true}).waitFor();
  assert.equal((await registered()).filter(s=>s.source_id==='copied-windows-root').length,1);
  const nodes=await page.evaluate(async()=>(await fetch('/api/files/browse?source_id=copied-windows-root')).json());
  assert(nodes.some(n=>n.name==='one.txt'));
  const info=await page.evaluate(async()=>(await fetch('/api/files/info?source_id=copied-windows-root&path=')).json());
  assert(JSON.stringify(info).includes('Preserved folder note'));
  check('pending Save stays modal; actual relink immediately browses copied files with original folder metadata');

  await page.unroute('**/api/suite/folders/copied-windows-root/path');
  await page.reload();await open();await section('Folders').click();
  await edit();assert.equal(await pathField.inputValue(),config.media+'/Copied library 한글');
  await editor.getByRole('button',{name:'Save',exact:true}).click();
  await editor.waitFor({state:'hidden'});
  assert.equal((await registered()).filter(s=>s.source_id==='copied-windows-root').length,1);
  await page.setViewportSize({width:420,height:820});
  await edit();
  assert.equal(await editor.evaluate(n=>n.getBoundingClientRect().right<=innerWidth),true);
  await page.keyboard.press('Escape');await editor.waitFor({state:'hidden'});
  check('path survives reload, same-path retry is idempotent and narrow keyboard dismissal preserves Settings');

  await page.setViewportSize({width:1440,height:1000});
  await section('Modules').click();
  const twitterSwitch=page.getByRole('switch',{name:'Twitter module',exact:true});
  await twitterSwitch.click();
  await page.waitForFunction(()=>document.querySelector('[data-module-id="twitter"] [role="switch"]')?.getAttribute('aria-checked')==='true');
  await section('Folders').click();
  async function relink(name,newPath){
   const row=dialog.getByText(name,{exact:true}).locator('xpath=ancestor::div[.//summary[@aria-label="Folder actions"]][1]');
   await row.getByLabel('Folder actions').click();
   await row.getByRole('button',{name:'Edit path…',exact:true}).click();
   await editor.waitFor();await pathField.fill(newPath);
   await editor.getByRole('button',{name:'Save',exact:true}).click();
   await editor.waitFor({state:'hidden'});
  }
  async function nestedOwnership(parentId,childId){
   const nodes=await page.evaluate(async id=>(await fetch('/api/files/browse?source_id='+id)).json(),childId);
   assert.deepEqual(nodes.map(n=>n.name),['post.txt']);
   const matches=await page.evaluate(async id=>(await fetch('/api/files/search?source_id='+id+'&q=post.txt')).json(),parentId);
   assert.equal(matches.length,1);assert.equal(matches[0].source_id,childId);
   const note=await page.evaluate(async id=>(await fetch('/api/files/info?source_id='+id+'&path=')).json(),childId);
   assert(JSON.stringify(note).includes('Preserved child note'));
   assert.equal((await registered()).find(s=>s.source_id===childId).role,'twitter');
  }
  await relink('Nested child',config.media+'/Copied library 한글/hehetwitter');
  await nestedOwnership('copied-windows-root','copied-child-root');
  assert.equal((await registered()).find(s=>s.source_id==='reverse-parent').path,'E:\\Library','Other saved Windows paths are not translated');
  check('parent-first nested repair transfers derived parent claims and preserves Twitter identity and notes');

  await relink('Reverse child',config.media+'/Reverse library/hehetwitter');
  assert.equal((await registered()).find(s=>s.source_id==='reverse-parent').path,'E:\\Library');
  await relink('Reverse library',config.media+'/Reverse library');
  await nestedOwnership('reverse-parent','reverse-child');
  await relink('Reverse child',config.media+'/Reverse library/hehetwitter');
  await nestedOwnership('reverse-parent','reverse-child');
  check('child-first nested repair and same-path retry preserve descendant ownership without bulk path changes');

  await section('Categories').click();
  const categoryPanel=dialog.getByRole('region',{name:'Library categories'});
  const categoryDialog=page.getByRole('dialog',{name:'Create new category',exact:true});
  const addCategory=categoryPanel.getByRole('button',{name:'Create new category',exact:true});
  const categoryName=categoryDialog.getByRole('textbox',{name:'Category name',exact:true});
  assert.equal(await addCategory.isDisabled(),true);
  assert.equal(report.requests.filter(request=>request.includes('/categories')).length,0);
  check('Categories follows Startup and disabled owners make no category requests');
  await section('Modules').click();
  for(const name of ['Video','Manga']){
   const endpoint=config.url+'/api/suite/modules/'+name.toLowerCase()+'/enable';
   expectedHttpErrors.add(endpoint);
   for(let attempt=0;attempt<3;attempt++){
    const changed=page.waitForResponse(response=>response.url()===endpoint&&response.request().method()==='POST');
    await page.getByRole('switch',{name:name+' module',exact:true}).click();
    const response=await changed;
    if(response.status()===200)break;
    assert.equal(response.status(),409);assert.match((await response.json()).detail,/Wait for module operations/);
    await page.waitForTimeout(150);
   }
   await page.waitForFunction(slug=>document.querySelector(`[data-module-id="${slug}"] [role="switch"]`)?.getAttribute('aria-checked')==='true',name.toLowerCase());
   expectedHttpErrors.delete(endpoint);
  }
  await dialog.getByRole('searchbox').fill('category');
  await dialog.getByRole('button',{name:/Categories Create separate/}).click();
  await dialog.locator('#setting-library-categories.setting-flash').waitFor();
  await categoryPanel.getByText('No categories yet.',{exact:true}).waitFor();
  assert.deepEqual(await categoryPanel.getByRole('group',{name:'Category module'}).getByRole('button').allTextContents(),['Video','Manga']);
  assert.equal(await categoryPanel.getByRole('list').count(),0);
  check('Category Settings search opens the owner-contributed Video and Manga lists');
  await addCategory.click();await categoryDialog.waitFor();assert(await categoryName.evaluate(node=>node===document.activeElement));
  await categoryName.fill('Cancelled category');await categoryDialog.getByRole('button',{name:'Cancel',exact:true}).click();
  await categoryDialog.waitFor({state:'hidden'});assert(await addCategory.evaluate(node=>node===document.activeElement));
  assert.equal((await(await context.request.get(config.url+'/api/video/categories')).json()).total,0);
  check('Create dialog focuses the name; Cancel restores the plus button without saving');
  async function createCategory(name){await addCategory.click();await categoryDialog.waitFor();await categoryName.fill(name);await categoryDialog.getByRole('button',{name:'Create',exact:true}).click();await categoryDialog.waitFor({state:'hidden'});}
  await createCategory('  공부 音楽  ');
  assert.deepEqual(await categoryPanel.getByRole('listitem').allTextContents(),['공부 音楽']);
  await categoryPanel.getByRole('button',{name:'Manga',exact:true}).click();await categoryPanel.getByText('No categories yet.',{exact:true}).waitFor();
  await createCategory('공부 音楽');
  assert.deepEqual(await categoryPanel.getByRole('listitem').allTextContents(),['공부 音楽']);
  check('Creation updates immediately, trims Unicode names and permits matching names in the other module');

  const categoriesUrl=config.url+'/api/manga/categories';expectedHttpErrors.add(categoriesUrl);
  await addCategory.click();await categoryName.fill('Default');await categoryDialog.getByRole('button',{name:'Create',exact:true}).click();
  await categoryDialog.getByRole('alert').filter({hasText:'reserved'}).waitFor();
  assert.equal(await categoryName.inputValue(),'Default');assert(await categoryName.evaluate(node=>node===document.activeElement));
  await categoryName.fill('공부 音楽');await categoryDialog.getByRole('button',{name:'Create',exact:true}).click();
  await categoryDialog.getByRole('alert').filter({hasText:'already exists'}).waitFor();
  await page.keyboard.press('Escape');await categoryDialog.waitFor({state:'hidden'});await dialog.waitFor();
  check('Reserved and duplicate names retain draft/focus; Escape closes only the category dialog');

  let createMode='normal',releasePending,receipts=[];
  await page.route('**/api/manga/categories',async route=>{
   if(route.request().method()!=='POST')return route.fallback();
   report.requests.push('POST /api/manga/categories');receipts.push(route.request().postDataJSON());
   if(createMode==='pending')await new Promise(resolve=>releasePending=resolve);
   const response=await route.fetch();
   if(createMode==='lost'){createMode='normal';return route.fulfill({status:503,json:{detail:'Generated lost response. Retry.'}});}
   return route.fulfill({response});
  });
  createMode='lost';await addCategory.click();await categoryName.fill('Retry shelf');
  await categoryDialog.getByRole('button',{name:'Create',exact:true}).click();
  await categoryDialog.getByRole('alert').filter({hasText:'Generated lost response'}).waitFor();
  await categoryDialog.getByRole('button',{name:'Create',exact:true}).click();await categoryDialog.waitFor({state:'hidden'});
  assert.equal(receipts[0].request_id,receipts[1].request_id);
  assert.equal((await(await context.request.get(categoriesUrl)).json()).items.filter(item=>item.name==='Retry shelf').length,1);
  check('Retry after a committed create loses its reply reuses the durable receipt and creates one row');

  await page.setViewportSize({width:600,height:480});await addCategory.click();await categoryDialog.waitFor();
  await categoryName.fill('IME shelf');await categoryName.dispatchEvent('compositionstart');
  const beforeIme=receipts.length;await page.keyboard.press('Enter');await page.waitForTimeout(120);assert.equal(receipts.length,beforeIme);
  await categoryName.dispatchEvent('compositionend');
  await page.keyboard.press('Shift+Tab');assert(await categoryDialog.getByRole('button',{name:'Create',exact:true}).evaluate(node=>node===document.activeElement));
  await page.keyboard.press('Tab');assert(await categoryName.evaluate(node=>node===document.activeElement));
  const bounds=await categoryDialog.boundingBox();assert(bounds.x>=0&&bounds.y>=0&&bounds.x+bounds.width<=600&&bounds.y+bounds.height<=480);
  createMode='pending';await categoryDialog.getByRole('button',{name:'Create',exact:true}).click();
  await categoryDialog.getByRole('button',{name:'Creating…',exact:true}).waitFor();
  await page.waitForFunction(()=>document.activeElement?.getAttribute('aria-label')==='Create new category');
  await page.keyboard.press('Escape');await page.keyboard.press('Tab');await categoryDialog.waitFor();
  assert(await categoryDialog.evaluate(node=>node.contains(document.activeElement)));
  while(!releasePending)await page.waitForTimeout(20);releasePending();
  await categoryDialog.waitFor({state:'hidden'});createMode='normal';
  await page.setViewportSize({width:1440,height:1000});
  check('Narrow dialog fits, traps keyboard focus, respects IME, and cannot dismiss a pending create');

  await page.reload();await page.getByRole('button',{name:'Open Keivotos menu',exact:true}).waitFor();await open();await section('Categories').click();
  await categoryPanel.getByRole('button',{name:'Manga',exact:true}).click();await categoryPanel.getByText('IME shelf',{exact:true}).waitFor();
  assert.deepEqual(await categoryPanel.getByRole('listitem').allTextContents(),['공부 音楽','Retry shelf','IME shelf']);
  await section('Modules').click();await page.getByRole('switch',{name:'Manga module',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('[data-module-id="manga"] [role="switch"]')?.getAttribute('aria-checked')==='false');
  await section('Categories').click();await categoryPanel.getByRole('button',{name:'Manga',exact:true}).click();assert(await addCategory.isDisabled());
  await section('Modules').click();await page.getByRole('switch',{name:'Manga module',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('[data-module-id="manga"] [role="switch"]')?.getAttribute('aria-checked')==='true');
  await section('Categories').click();await categoryPanel.getByRole('button',{name:'Manga',exact:true}).click();await categoryPanel.getByText('IME shelf',{exact:true}).waitFor();
  check('Names survive reload and module disable/enable in creation order');

  for(const style of ['modern','legacy']){
   for(const fixture of [
    {name:'unknown',published_at:null},
    {name:'wrong-version',published_at:'2027-01-15T15:30:00Z',version:'9.9.9'},
    {name:'invalid-date',published_at:'2027-02-29T12:00:00Z'},
    {name:'failed-read',status:503},
    {name:'invalid-json',invalid:true},
   ]){
    const isolated=await browser.newContext({viewport:{width:600,height:480},timezoneId:'America/New_York'});
    await isolated.addInitScript(style=>localStorage.setItem('keivotos:settings-style',JSON.stringify(style)),style);
    const candidate=await isolated.newPage();let reads=0;
    candidate.on('pageerror',e=>report.errors.push(e.message));
    candidate.on('console',m=>{if(m.type()==='error'&&!(fixture.status&&m.location().url===config.url+'/api/suite/release'&&m.text().startsWith('Failed to load resource:')))report.errors.push(m.text());});
    await candidate.route('**/*',async route=>{
     const u=new URL(route.request().url());
     if(u.origin!==config.url){report.errors.push('External request '+u.href);return route.abort();}
     if(u.pathname==='/api/files/sources')return route.fulfill({json:[]});
     if(u.pathname==='/api/suite/release'){
      reads++;
      if(fixture.status)return route.fulfill({status:fixture.status,json:{detail:'Fixture unavailable'}});
      if(fixture.invalid)return route.fulfill({contentType:'application/json',body:'invalid JSON'});
      const selected=fixture.version??version;
      return route.fulfill({json:{version:selected,tag_name:`v${selected}`,published_at:fixture.published_at}});
     }
     return route.continue();
    });
    await candidate.goto(config.url);
    await candidate.getByRole('button',{name:'Open Keivotos menu',exact:true}).click();
    await candidate.getByRole('button',{name:'Settings',exact:true}).click();
    const settings=candidate.getByRole('dialog',{name:'Settings',exact:true});
    const select=name=>settings.getByRole('navigation',{name:'Settings sections'}).getByRole('button',{name,exact:true}).last();
    const reply=candidate.waitForResponse(response=>new URL(response.url()).pathname==='/api/suite/release');
    await select('About').click();await reply;
    await candidate.waitForTimeout(180);
    assert.equal(await settings.locator('#setting-about').textContent().then(text=>text.trim()),`Version ${version}`,`${style}/${fixture.name}`);
    await select('Appearance').click();await select('About').click();await candidate.waitForTimeout(150);
    assert.equal(reads,1,`${style}/${fixture.name}: no repeat request`);
    assert.equal(await settings.locator('#setting-about').textContent().then(text=>text.trim()),`Version ${version}`);
    await isolated.close();
    check(`${style}: ${fixture.name} omits date and reuses the bounded read`);
   }
  }
  assert.deepEqual(report.errors,[]);
  check('presentation cleanup and clean browser/API logs');
 }catch(e){report.failure=e.stack;await page.screenshot({path:path.join(config.output,'failure.png')});throw e;}
 finally{fs.writeFileSync(path.join(config.output,'report.json'),JSON.stringify(report,null,2));await context.tracing.stop({path:path.join(config.output,'trace.zip')});await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

// Real Files-only application: no route mocks and no Danbooru source folders.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
assert.match(config.home,/[/\\]keivotos-modularization-[^/\\]+[/\\]home$/);
const report={checks:[],errors:[],requests:[],console:[],fallbacks:[]};
(async()=>{
 const browser=await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});
 await context.tracing.start({screenshots:true,snapshots:true});
 const page=await context.newPage();page.setDefaultTimeout(10000);
 page.on('pageerror',e=>report.errors.push(e.message));
 page.on('console',m=>{if(m.type()==='error')report.console.push(m.text());});
 page.on('response',r=>{
  const url=new URL(r.url());
  if(r.status()===404&&url.pathname==='/api/files/thumbnail'&&url.searchParams.get('path')==='preserved.txt'){
   report.fallbacks.push(r.url());return;
  }
  if(r.status()>=400)report.errors.push(`${r.status()} ${r.url()}`);
 });
 await page.route('**/*',route=>{
  const url=new URL(route.request().url());
  if(url.origin!==config.url){report.errors.push('External request '+url.href);return route.abort();}
  report.requests.push(url.pathname);return route.continue();
 });
 const check=s=>{report.checks.push(s);console.log('PASS: '+s);};
 try{
  const modules=await (await context.request.get(config.url+'/api/suite/modules')).json();
  assert.deepEqual(modules.map(m=>m.slug),['files']);
  const sourceResponse=await context.request.post(config.url+'/api/files/sources',{headers:{Origin:config.url},data:{path:config.media,display_name:'Preserved fixture'}});
  assert(sourceResponse.ok(),await sourceResponse.text());
  const source=await sourceResponse.json();
  const scan=await context.request.post(`${config.url}/api/files/sources/${source.source_id}/scan`,{headers:{Origin:config.url}});
  assert(scan.ok(),await scan.text());
  await context.addInitScript(()=>{
   localStorage.setItem('keivotos:startup-module',JSON.stringify('danbooru'));
   localStorage.setItem('keivotos:active-module',JSON.stringify('danbooru'));
  });
  await page.goto(config.url);
  await page.getByRole('button',{name:'Preserved fixture',exact:true}).click();
  await page.getByText('preserved.txt',{exact:true}).waitFor();
  assert.equal(await page.title(),'Keivotos');
  check('real Files-only startup, folder registration, scan and browsing; stale module preference falls back');
  await page.getByPlaceholder('Search this folder…').fill('preserved');
  await page.getByText('preserved.txt',{exact:true}).waitFor();
  await page.reload();
  await page.getByRole('button',{name:'Preserved fixture',exact:true}).click();
  await page.getByText('preserved.txt',{exact:true}).waitFor();
  check('Files search and reload preserve registered source data');
  await page.getByRole('button',{name:'Open Keivotos menu',exact:true}).click();
  assert.equal(await page.getByText('Danbooru',{exact:true}).count(),0);
  await page.getByRole('button',{name:'Settings',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'Settings',exact:true});await dialog.waitFor();
  assert.deepEqual(await dialog.locator('nav button').allTextContents(),['Modules','Appearance','Startup','Categories','Player','Data & storage','Backup','Advanced','About','Folders','Attachments']);
   await dialog.getByRole('navigation',{name:'Settings sections'}).getByRole('button',{name:'Player',exact:true}).click();
   const hold=dialog.getByRole('switch',{name:'Hold Space to speed up',exact:true});assert.equal(await hold.getAttribute('aria-checked'),'true');await dialog.getByLabel('Hold speed multiplier',{exact:true}).selectOption('2');await hold.click();assert(await dialog.getByLabel('Hold speed multiplier',{exact:true}).isDisabled());await hold.click();await dialog.getByLabel('Hold speed multiplier',{exact:true}).selectOption('1.5');
   check('Shared hold-Space settings work without any optional Video/YouTube/Music code or requests');

  await dialog.getByRole('navigation',{name:'Settings sections'}).getByRole('button',{name:'Categories',exact:true}).click();
  await dialog.getByText('Video and Manga categories are unavailable.',{exact:true}).waitFor();
  assert(await dialog.getByRole('button',{name:'Create new category',exact:true}).isDisabled());
  for(const name of ['Startup','Data & storage','Backup','Folders','Attachments','Appearance']){
   await dialog.getByRole('navigation',{name:'Settings sections'}).getByRole('button',{name,exact:true}).click();
  }
  await dialog.getByRole('navigation',{name:'Settings sections'}).getByRole('button',{name:'Appearance',exact:true}).first().click();
  await page.getByLabel('Notification position',{exact:true}).selectOption('top-right');
  assert.equal(await page.locator('.notification-overlay').getAttribute('data-position'),'top-right');
  assert.equal(await page.locator('.action-notification').count(),0);
  check('Shared notification host and saved position setting work with all optional modules physically absent');
  await page.getByRole('button',{name:'Close settings',exact:true}).click();
  check('suite drawer and every remaining Settings section work without module UI');

  const passive=await context.request.get(config.url+'/api/files/module-metadata?'+new URLSearchParams({source_id:source.source_id,path:'preserved.txt'}));
  assert.equal(passive.status(),200);assert.equal(passive.headers()['cache-control'],'no-store');assert.deepEqual(await passive.json(),{sections:[]});
  await page.locator('.app-drawer').getByRole('button',{name:'Files',exact:true}).click();
  await page.locator('[data-files-entry-path="preserved.txt"]').click({button:'right'});
  await page.getByRole('menuitem',{name:'Show info',exact:true}).click();
  const information=page.getByRole('dialog',{name:'File information',exact:true});await information.waitFor();
  await page.waitForFunction(()=>!document.querySelector('[role="dialog"]')?.textContent.includes('Reading metadata'));
  assert.equal(await information.getByRole('region',{name:/^(YouTube|Twitter) metadata$/}).count(),0);
  assert.equal(await information.locator('a').count(),0);
  await information.getByRole('button',{name:'Origin',exact:true}).click();
  assert.equal(await information.getByRole('region',{name:/^(YouTube|Twitter) metadata$/}).count(),0);
  assert.equal(await information.getByRole('button',{name:/Add info/}).count(),1);
  await information.getByRole('button',{name:/Add info/}).click();
  await information.getByRole('button',{name:'Details',exact:true}).click();
  await information.getByRole('textbox',{name:'Detail name',exact:true}).fill('Edition');
  await information.getByRole('textbox',{name:'Edition',exact:true}).fill('Files only');
  await information.getByRole('button',{name:'Properties',exact:true}).click();
  await page.waitForFunction(async(source)=>{
    const note=await(await fetch('/api/files/info?'+new URLSearchParams({source_id:source,path:'preserved.txt'}))).json();
    return note?.details?.[0]?.value==='Files only';
  },source.source_id);
  assert.equal(await information.locator('dl').count(),1);
  await information.getByRole('button',{name:'Close',exact:true}).click();
  check('Files metadata, native Origin Details editing and one Properties list work when all optional owners are physically absent');

  const paths=(await (await context.request.get(config.url+'/openapi.json')).json()).paths;
  assert(!paths['/api/images']);assert(!paths['/api/danbooru/credentials']);assert(!paths['/api/language/documents']);assert(paths['/api/files/sources']);
  assert(!paths['/api/ost/folders']);assert(!paths['/api/ost/library']);
  assert(!paths['/api/ost/cover']);
  assert(!paths['/api/ost/tracks']);
  assert(!paths['/api/ost/metadata']);assert(paths['/api/files/audio-metadata']);
  assert(!paths['/api/ost/albums']);assert(!paths['/api/ost/album-tracks']);
  assert.equal(await page.getByRole('textbox',{name:'Search music',exact:true}).count(),0);
  assert(!Object.keys(paths).some(p=>p.startsWith('/api/twitter')));
  assert(!report.requests.some(p=>p.startsWith('/api/twitter')));
  assert.equal(await page.getByRole('switch',{name:'Twitter module',exact:true}).count(),0);
  assert.equal(await page.getByRole('navigation',{name:'Twitter sections'}).count(),0);
  assert.equal(await page.locator('.twitter-surface').count(),0);
  assert.equal(await page.getByRole('textbox',{name:'Search saved posts or paste a Twitter URL'}).count(),0);
  assert.equal(await page.getByRole('navigation',{name:'Music sections'}).count(),0);
  assert.equal(await page.getByRole('switch',{name:'Music module',exact:true}).count(),0);
  assert(!report.requests.some(p=>p.startsWith('/api/ost')));
  assert(!Object.keys(paths).some(p=>p.startsWith('/api/youtube')));
  assert(!report.requests.some(p=>p.startsWith('/api/youtube')));
  assert.equal(await page.getByRole('navigation',{name:'YouTube sections'}).count(),0);
  assert.equal(await page.getByRole('navigation',{name:'Language sections'}).count(),0);
  assert.equal(await page.locator('[data-sidebar-owner]').count(),0);
  assert.equal(await page.locator('.module-sidebar-grip-track,.practice-actions,.practice-card').count(),0);
  assert.equal(await page.getByRole('menuitem',{name:'Add to playlists'}).count(),0);
  assert.equal(await page.getByRole('dialog',{name:'Add to playlists'}).count(),0);
  assert(!Object.keys(paths).some(p=>p.startsWith('/api/video/progress')));
  assert(!report.requests.some(p=>p.startsWith('/api/video/progress')));
  check('Files-only UI has no Video watch routes/requests or YouTube sidebar/playlists when optional owners are absent');
  assert(!report.requests.some(p=>p.startsWith('/api/danbooru')||p.startsWith('/api/language')||p==='/api/images'||p==='/api/tools'));
  await page.waitForTimeout(150);
  assert.equal(report.console.filter(message=>message==='Failed to load resource: the server responded with a status of 404 (Not Found)').length,report.fallbacks.length);
  assert.deepEqual(report.console.filter(message=>message!=='Failed to load resource: the server responded with a status of 404 (Not Found)'),[]);
  assert.deepEqual(report.errors,[]);
  check('module endpoints absent, no module requests or browser errors');
  await page.screenshot({path:path.join(config.output,'files-only.png')});
 }finally{
  fs.writeFileSync(path.join(config.output,'report.json'),JSON.stringify(report,null,2));
  await context.tracing.stop({path:path.join(config.output,'trace.zip')});await browser.close();
 }
})().catch(error=>{console.error(error);process.exitCode=1;});

// Current responses own Home. Every mutation is confined to generated fixture data.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
assert.match(config.home, /[/\\]keivotos-modularization-[^/\\]+[/\\]home$/);
assert.equal(new URL(config.url).hostname, '127.0.0.1');
const report = {checks:[], errors:[], timings:{}, requests:[], expectedArtworkErrors:[]};
const expectedArtwork=new Set();
(async()=>{
 const browser=await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL});
 const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'no-preference'});
 await context.tracing.start({screenshots:true,snapshots:true});
 const page=await context.newPage();page.setDefaultTimeout(12000);
 page.on('pageerror',error=>report.errors.push(error.message));
 page.on('console',message=>{if(message.type()==='error'){if(expectedArtwork.has(message.location().url))report.expectedArtworkErrors.push(message.text());else report.errors.push(message.text());}});
 page.on('response',response=>{if(response.status()>=400){if(response.status()===404&&expectedArtwork.has(response.url()))report.expectedArtworkErrors.push(response.status()+' '+response.url());else report.errors.push(response.status()+' '+response.url());}});
 await page.route('**/*',route=>{
  const url=new URL(route.request().url());
  if(url.origin!==config.url){report.errors.push('Unexpected external request '+url.href);return route.abort();}
  if(url.pathname.startsWith('/api/home/'))report.requests.push(url.pathname+url.search);
  return route.continue();
 });
 const wait=ms=>page.waitForTimeout(ms);
 const check=label=>{report.checks.push(label);console.log('PASS: '+label);};
 const call=async(url,body)=>{
  const response=await page.request[body?'post':'get'](config.url+url,body?{data:body}:undefined);
  assert(response.ok(),url+' '+response.status()+' '+await response.text());return response.json();
 };
 const change=action=>call('/api/fixture/home/change',{action});
 const empty=async()=>{await page.getByText('Your library, ready to wander',{exact:true}).waitFor();assert.equal(await page.locator('.tag-neighborhood,.home-lane,.spotlight-peek').count(),0);};
 const populated=async()=>{await page.locator('.spotlight-peek.is-active').waitFor();await page.locator('.home-lane').nth(2).waitFor();};
 async function settings(){await page.getByRole('button',{name:'Open Keivotos menu',exact:true}).click();await page.locator('.app-drawer').getByRole('button',{name:'Settings',exact:true}).click();await page.getByRole('navigation',{name:'Settings sections'}).getByRole('button',{name:'Browsing',exact:true}).click();}
 async function closeSettings(){await page.getByRole('button',{name:'Close settings',exact:true}).click();await page.getByRole('dialog',{name:'Settings',exact:true}).waitFor({state:'detached'});if(await page.locator('.app-drawer').count()){await page.keyboard.press('Escape');await page.locator('.app-drawer').waitFor({state:'detached'});}}
 async function holdResponse(endpoint,rating,fail=false){
  await page.evaluate(({endpoint,rating,fail})=>{
   const original=window.fetch;
   const state=window.__homeHold={captured:false,done:false};
   let release;const gate=new Promise(resolve=>release=resolve);state.release=release;
   window.fetch=async(input,options)=>{
    const url=new URL(typeof input==='object'&&'url'in input?input.url:String(input),location.origin);
    if(!state.captured&&url.pathname===endpoint&&url.searchParams.get('rating')===rating){
     state.captured=true;
     const response=await original(input,{...options,signal:undefined});state.received=true;
     await gate;state.done=true;
     if(fail)throw new Error('Expected abandoned fixture request');
     return response;
    }
    return original(input,options);
   };
   state.restore=()=>window.fetch=original;
  },{endpoint,rating,fail});
 }
 async function releaseHeld(){await page.evaluate(()=>{window.__homeHold.release();window.__homeHold.restore();});await page.waitForFunction(()=>window.__homeHold.done);await wait(300);}
 try{
  await page.goto(config.url);
  assert.equal((await call('/api/storage')).suite_home,config.home);
  await page.getByRole('button',{name:'Open Keivotos menu',exact:true}).click();
  await page.getByRole('button',{name:'Settings',exact:true}).click();
  await page.getByRole('navigation',{name:'Settings sections'}).getByRole('button',{name:'Modules',exact:true}).click();
  await page.getByRole('switch',{name:'Danbooru module',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('[data-module-id="danbooru"] [role="switch"]')?.getAttribute('aria-checked')==='true');
  await page.getByRole('button',{name:'Close settings',exact:true}).click();
  await page.getByRole('dialog',{name:'Settings',exact:true}).waitFor({state:'detached'});
  await page.locator('.app-drawer').getByRole('button',{name:'Danbooru',exact:true}).click();
  await populated();
  const tags=await call('/api/home/tags?rating=g&featured_limit=16&group_limit=10');
  const rails=await call('/api/home/image-rails?rating=g&per_rail=24');
  assert(tags.featured.length);assert(rails.rails.some(rail=>rail.items.length));
  const cachedState=await context.storageState();
  const proof=await call('/api/fixture/home/proof');assert(proof.media_preserved);
  check('Generated populated library supplies real Home responses and all media bytes are preserved');

  await change('empty');
  let tagsRelease,railsRelease,tagsSeen,railsSeen;
  const tagsGate=new Promise(resolve=>tagsRelease=resolve),railsGate=new Promise(resolve=>railsRelease=resolve);
  const tagCaptured=new Promise(resolve=>tagsSeen=resolve),railCaptured=new Promise(resolve=>railsSeen=resolve);
  const tagRoute=async route=>{tagsSeen();await tagsGate;await route.continue();};
  const railRoute=async route=>{railsSeen();await railsGate;await route.continue();};
  await page.route('**/api/home/tags?*',tagRoute);await page.route('**/api/home/image-rails?*',railRoute);
  await page.reload({waitUntil:'domcontentloaded'});await tagCaptured;
  await wait(250);assert.equal(await page.locator('.tag-neighborhood,.home-lane,.spotlight-peek').count(),0);
  assert.equal(await page.getByText('fixture character 1',{exact:true}).count(),0);
  tagsRelease();await railCaptured;await empty();await wait(220);await empty();
  railsRelease();await wait(300);await empty();
  await page.unroute('**/api/home/tags?*',tagRoute);await page.unroute('**/api/home/image-rails?*',railRoute);
  await page.screenshot({path:path.join(config.output,'empty-home.png')});
  check('Cached populated tags and rails never flash on startup or recreate neighborhoods while the current empty rails response is delayed');
  await page.getByRole('button',{name:'Browse',exact:true}).click();await page.getByTitle('Go Home',{exact:true}).click();await empty();
  check('Browse and back remains empty without depending on navigation to clear old content');

  const cold=await browser.newContext({storageState:cachedState,viewport:{width:1440,height:1000}});
  const coldPage=await cold.newPage();
  coldPage.on('pageerror',error=>report.errors.push(error.message));
  let coldRelease,coldSeen;
  const coldGate=new Promise(resolve=>coldRelease=resolve),coldCaptured=new Promise(resolve=>coldSeen=resolve);
  await coldPage.route('**/api/home/tags?*',async route=>{coldSeen();await coldGate;await route.continue();});
  await coldPage.goto(config.url,{waitUntil:'domcontentloaded'});await coldCaptured;await coldPage.waitForTimeout(180);
  assert.equal(await coldPage.locator('.tag-neighborhood,.home-lane,.spotlight-peek').count(),0);
  coldRelease();await coldPage.getByText('Your library, ready to wander',{exact:true}).waitFor();
  await coldPage.screenshot({path:path.join(config.output,'cold-empty-home.png')});await cold.close();
  check('A separate fresh browser context with the old persisted library also stays empty before current results arrive');

  await change('restore');
  await page.evaluate(()=>{localStorage.setItem('keivotos:home:v4:tags:g','{"featured":[{}],"groups":{"character":"bad"}}');localStorage.setItem('keivotos:home:v4:rails:g','{bad json');});
  await page.reload();await populated();
  check('Malformed legacy cached payloads cannot break a fresh valid Home response');

  await settings();
  await holdResponse('/api/home/tags','e');
  await page.locator('#setting-rating-filter select').selectOption('e');
  await page.waitForFunction(()=>window.__homeHold.received);
  await page.locator('#setting-rating-filter select').selectOption('g');
  await closeSettings();await populated();
  await releaseHeld();await populated();assert.equal(await page.getByText('Failed to load home',{exact:true}).count(),0);
  check('A late success from an older rating cannot replace the current populated scope even if transport cancellation loses the race');

  await settings();await holdResponse('/api/home/image-rails','e',true);
  await page.locator('#setting-rating-filter select').selectOption('e');await page.waitForFunction(()=>window.__homeHold.received);
  await page.locator('#setting-rating-filter select').selectOption('g');await closeSettings();await populated();
  await releaseHeld();await populated();
  check('A late failed rail request cannot clear the current lanes or report an abandoned owner error');

  await holdResponse('/api/home/tags','g');
  await page.locator('.spotlight-peek.is-active').click();
  await page.locator('.spotlight-hero-art').click();
  await page.getByRole('button',{name:'Favorite',exact:true}).click();
  await page.waitForFunction(()=>window.__homeHold.received);
  await page.getByTitle('Close',{exact:true}).click();
  await page.getByRole('button',{name:'Browse',exact:true}).click();
  // Let the outgoing grid finish before unindexing fixture rows; otherwise its
  // legitimate in-flight thumbnail reads race the fixture's deliberate delete.
  await page.getByRole('button',{name:'Open Keivotos menu',exact:true}).click();
  await page.locator('.app-drawer').getByRole('button',{name:'Downloads',exact:true}).click();
  await page.locator('.downloads-scroll').waitFor();await page.waitForLoadState('networkidle');
  await change('empty');
  await page.getByRole('button',{name:'Open Keivotos menu',exact:true}).click();
  await page.locator('.app-drawer').getByRole('button',{name:'Danbooru',exact:true}).click();
  await page.getByTitle('Go Home',{exact:true}).click();await empty();
  const emptyCache=await page.evaluate(()=>localStorage.getItem('keivotos:home:v4:tags:g'));
  await releaseHeld();await empty();
  assert.equal(await page.evaluate(()=>localStorage.getItem('keivotos:home:v4:tags:g')),emptyCache);
  await page.reload();await empty();
  check('A real Favorite refresh reloads mounted Home, and leaving during that request prevents later cache writes or timer work from reviving it');

  await change('restore');await change('register');
  assert((await call('/api/home/tags?rating=g&featured_limit=16&group_limit=10')).featured.length);
  assert((await call('/api/home/image-rails?rating=g&per_rail=24')).rails.some(rail=>rail.items.length));
  const removal=await page.request.delete(config.url+'/api/folders/home-fixture-root?mode=unindex_only');assert(removal.ok(),await removal.text());
  assert.equal((await call('/api/home/tags?rating=g&featured_limit=16&group_limit=10')).featured.length,0);
  assert((await call('/api/home/image-rails?rating=g&per_rail=24')).rails.every(rail=>!rail.items.length));
  assert((await call('/api/fixture/home/proof')).media_preserved);
  await page.reload();await empty();
  check('Real unindex-only folder removal immediately invalidates both server caches without deleting original fixture artwork');

  await change('small');await page.reload();await populated();
  assert.equal(await page.locator('.home-lane').count(),3);
  await page.screenshot({path:path.join(config.output,'small-library.png')});
  check('A valid one-image library still keeps all three discovery lanes through its existing reuse fallback');

  const missingCover=async route=>{expectedArtwork.add(route.request().url());await route.fulfill({status:404,contentType:'text/plain',body:'Expected isolated missing artwork'});};
  await page.route('**/api/thumbnail/1?*',missingCover);
  await page.reload();await populated();await wait(350);
  assert.equal(await page.locator('.tag-neighborhood').count(),4);
  assert(report.expectedArtworkErrors.length>0);
  await page.unroute('**/api/thumbnail/1?*',missingCover);
  check('A documented missing-thumbnail response cannot turn valid one-image tag records into an empty library or crash Home');
  await change('untagged');await page.reload();
  await page.locator('.home-lane').first().waitFor();assert.equal(await page.locator('.tag-neighborhood,.spotlight-peek').count(),0);
  assert.equal(await page.getByRole('region',{name:'Recently loved image lane',exact:true}).count(),1);
  check('Fresh untagged favorites remain visible even when the tags response is legitimately empty');

  await change('empty');await settings();await page.locator('#setting-home-layout').getByRole('button',{name:'Classic',exact:true}).click();await closeSettings();
  await page.reload();await page.getByText('Featured Tags',{exact:true}).waitFor();
  assert.equal(await page.locator('img[src*="/api/thumbnail/"]').count(),0);
  await settings();await page.locator('#setting-home-layout').getByRole('button',{name:'Discovery',exact:true}).click();await closeSettings();await empty();
  check('Both retained layouts render true empty results without phantom media or tag names');

  await change('restore');
  await page.addInitScript(()=>{const originalGet=Storage.prototype.getItem,originalSet=Storage.prototype.setItem;Storage.prototype.getItem=function(key){if(key.startsWith('keivotos:home:'))throw new DOMException('Fixture storage denied','SecurityError');return originalGet.call(this,key);};Storage.prototype.setItem=function(key,value){if(key.startsWith('keivotos:home:'))throw new DOMException('Fixture storage full','QuotaExceededError');return originalSet.call(this,key,value);};});
  await page.reload();await populated();
  await page.setViewportSize({width:390,height:780});await wait(250);
  await page.screenshot({path:path.join(config.output,'home-narrow.png')});
  assert((await call('/api/fixture/home/proof')).media_preserved);
  check('Unavailable Home browser storage still permits fresh narrow-screen content and preserves all 48 original fixture images');
  assert.deepEqual(report.errors,[]);check('No unexpected browser, HTTP or external-request errors');
 }catch(error){report.failure=error.stack;await page.screenshot({path:path.join(config.output,'failure.png')});throw error;}
 finally{fs.writeFileSync(path.join(config.output,'report.json'),JSON.stringify(report,null,2));await context.tracing.stop({path:path.join(config.output,'trace.zip')});await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});

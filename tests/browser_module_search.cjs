// Full production shell, disposable local APIs and controlled delayed replies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
assert.match(config.home, /[/\\]keivotos-modularization-[^/\\]+[/\\]home$/);
const report = {checks: [], errors: [], expectedFailures: [], requests: [], timings: {}};
(async () => {
 const browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL});
 const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
 const page = await context.newPage(); page.setDefaultTimeout(12000);
 page.on('pageerror', e => report.errors.push(e.message));
 page.on('console', message => {
  if (message.type() !== 'error') return;
  if (message.text().includes('503') && message.location().url.includes('/api/youtube/channels?q=bad')) report.expectedFailures.push(message.text());
  else report.errors.push(message.text());
 });
 let intercept = null;
 const pixel = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64');
 await page.route('**/*', async route => {
  const url = new URL(route.request().url());
  if (url.origin !== config.url) {report.errors.push('External request ' + url.href); return route.abort();}
  if (url.pathname.startsWith('/api/')) report.requests.push({path: url.pathname, q: url.searchParams.get('q'), author: url.searchParams.get('author'), offset: url.searchParams.get('offset'), method: route.request().method()});
  // Search fixtures never play videos or need to decode fake video thumbnails.
  if (url.pathname === '/api/files/thumbnail' || url.pathname === '/api/ost/cover' || url.pathname === '/api/manga/cover') return route.fulfill({contentType: 'image/png', body: pixel});
  if (intercept && await intercept(route, url)) return;
  return route.continue();
 });
 const check = text => {report.checks.push(text); console.log('PASS: ' + text);};
 const wait = ms => page.waitForTimeout(ms);
 async function until(predicate) {const deadline=Date.now()+10000;while(!predicate()){assert(Date.now()<deadline,'Expected search request did not start');await wait(10);}}
 const api = async (url, method = 'GET', data) => {
  const response = await context.request.fetch(config.url + url, {method, data, headers: {Origin: config.url}});
  const body = await response.json(); assert(response.ok(), JSON.stringify({url, status: response.status(), body})); return body;
 };
 async function choose(name) {
  await page.getByRole('button', {name: 'Open Keivotos menu', exact: true}).click();
  await page.locator('.app-drawer').getByRole('button', {name, exact: true}).click();
  await page.locator('.app-drawer').waitFor({state: 'detached'});
 }
 const libraryPath=owner=>'/api/'+owner+(owner==='video'?'/catalog':owner==='youtube'?'/channels':'/library');
 const reads = (start, owner) => report.requests.slice(start).filter(r => r.path === libraryPath(owner));
 async function ime(label, owner) {
  const input = page.getByRole('textbox', {name: label}); await input.waitFor(); await wait(100);
  const start = report.requests.length;
  await input.dispatchEvent('compositionstart'); await input.fill('ㅎ'); await wait(400);
  assert.equal(reads(start, owner).filter(r => r.q === 'ㅎ').length, 0);
  await input.fill('한국어'); await input.dispatchEvent('compositionend'); await wait(450);
  assert(reads(start, owner).some(r => r.q === '한국어'));
  assert.equal(report.requests.slice(start).filter(r => /^\/api\/(video|manga|youtube|twitter)\/(library|catalog|channels)$/.test(r.path) && r.path.split('/')[2] !== owner).length, 0);
  check(owner + ' waits for complete Korean composition and calls only its own search');
  return input;
 }
 try {
  await page.goto(config.url);
  const disabled = await context.request.get(config.url + '/api/video/library'); assert.equal(disabled.status(), 409);
  assert.equal((await context.request.get(config.url + '/api/files/search?q=x&offset=-1')).status(),422);
  assert.equal((await context.request.get(config.url + '/api/files/search?q=x&limit=1001')).status(),422);
  for (const id of ['danbooru', 'video', 'manga', 'youtube', 'ost', 'twitter', 'language']) await api('/api/suite/modules/' + id + '/enable', 'POST');
  await api('/api/suite/folders/apply', 'POST', {folders: [['Files','files'],['Video','video'],['Manga','manga'],['Twitter','twitter'],['Music','ost']].map(([name,role]) => ({path: config.media + '/' + name, display_name: 'Search ' + name.toLowerCase(), role}))});
  await page.reload();
  await page.locator('aside').getByRole('button', {name: /Search files$/}).click();
  let input = page.getByPlaceholder('Search this folder…'); await input.fill('match-'); await input.press('Enter');
  await page.waitForFunction(() => document.querySelectorAll('[data-files-entry-path]').length === 200);
  await page.getByRole('button', {name: 'Load more', exact: true}).click();
  await page.waitForFunction(() => document.querySelectorAll('[data-files-entry-path]').length === 215);
  assert.equal(await page.getByRole('button', {name: 'Load more', exact: true}).count(), 0);
  check('Files exposes every match beyond the first 200 through stable continuation');
  let release, started = false, gate = new Promise(resolve => release = resolve);
  const stale = {source_id:'fixture',relative_path:'stale-alpha.txt',parent:'',name:'stale-alpha.txt',ext:'txt',is_dir:false,size:1,mtime:1,available:true};
  intercept = async (route, url) => {
   if (url.pathname !== '/api/files/search' || url.searchParams.get('q') !== 'alpha') return false;
   started = true; await gate; await route.fulfill({json: [stale]}); return true;
  };
  await input.fill('alpha'); await input.press('Enter'); await page.waitForFunction(() => true);
  await until(()=>started);
  await page.getByRole('button', {name:'Clear search',exact:true}).click(); release(); await wait(250);
  assert.equal(await page.getByText('stale-alpha.txt', {exact:true}).count(), 0);
  assert.equal(await input.inputValue(), ''); intercept = null;
  check('Files Clear cancels pending results instead of letting them return');
  const beforeIme = report.requests.length;
  await input.dispatchEvent('compositionstart'); await input.fill('ㅎ');
  await input.dispatchEvent('keydown', {key:'Enter',isComposing:true,keyCode:229}); await wait(250);
  assert.equal(report.requests.slice(beforeIme).filter(r => r.path === '/api/files/search').length, 0);
  await input.dispatchEvent('compositionend'); await input.fill('match-'); await input.press('Enter');
  await page.waitForFunction(() => document.querySelectorAll('[data-files-entry-path]').length === 200);
  started = false; gate = new Promise(resolve => release = resolve);
  intercept = async (route,url) => {
   if (url.pathname !== '/api/files/search' || url.searchParams.get('offset') !== '200') return false;
   started = true; await gate; await route.fulfill({json:[stale]}); return true;
  };
  await page.getByRole('button',{name:'Load more',exact:true}).click();await until(()=>started);
  await input.fill('no-match');await input.press('Enter');release();await wait(250);
  assert.equal(await page.getByText('stale-alpha.txt',{exact:true}).count(),0);assert.equal(await page.locator('[data-files-entry-path]').count(),0);intercept=null;
  check('Files IME Enter and a late continuation cannot submit or revive the wrong query');
  const switchingSource=(await api('/api/files/sources')).find(source=>source.display_name==='Search video');
  started=false;gate=new Promise(resolve=>release=resolve);
  intercept=async(route,url)=>{
   if(url.pathname!=='/api/files/browse'||url.searchParams.get('source_id')!==switchingSource.source_id||started)return false;
   started=true;await gate;await route.fulfill({json:[stale]});return true;
  };
  await page.locator('aside').getByRole('button',{name:/Search video$/}).click();await until(()=>started);
  await input.fill('draft');await input.fill('');release();await wait(300);
  assert.equal(await page.locator('[data-files-entry-path]').count(),3);assert.equal(await page.getByText('stale-alpha.txt',{exact:true}).count(),0);intercept=null;
  check('Clearing an unsubmitted Files query during a source switch restores the correct folder listing');
  await choose('Video');input=await ime('Search videos','video');
  await input.fill('école');await wait(350);assert.equal(await page.locator('.video-tile').count(),1);assert.equal(await page.locator('.video-tile').getAttribute('title'),'ÉCOLE.mp4');
  await choose('Manga');input=await ime('Search manga','manga');
  await input.fill('STRASSE');await wait(350);assert.equal(await page.locator('.manga-tile').count(),1);
  check('Video and Manga keep Unicode matching in the production library');
  await choose('YouTube');input=await ime('Search downloads or paste a YouTube URL','youtube');
  intercept=async(route,url)=>{if(url.pathname!=='/api/youtube/channels'||url.searchParams.get('q')!=='bad')return false;await route.fulfill({status:503,json:{detail:'fixture search failure'}});return true;};
  await input.fill('bad');await page.getByText('fixture search failure',{exact:false}).waitFor();await input.fill('good');await wait(350);
  assert.equal(await page.getByText('fixture search failure',{exact:false}).count(),0);intercept=null;
  const noDownload=report.requests.length;await input.fill('https://www.youtube.com/watch?v=abcdefghijk');await wait(350);
  assert.equal(report.requests.slice(noDownload).filter(r=>r.method==='POST'&&r.path==='/api/youtube/downloads').length,0);
  check('YouTube clears a recovered search error and URL typing remains read-only');
  await choose('Twitter');input=await ime('Search saved posts or paste a Twitter URL','twitter');
  await input.fill('');await wait(350);
  await page.getByRole('button',{name:'Posts by Saved author 이름',exact:true}).first().click();await wait(150);
  const authorStart=report.requests.length;await input.fill('hello');await wait(350);
  const authorRead=reads(authorStart,'twitter').find(r=>r.q==='hello');assert(authorRead?.author);
  const author=authorRead.author;await input.fill('');await wait(350);assert.equal(reads(authorStart,'twitter').at(-1).author,author);
  assert.equal(await page.getByRole('button',{name:'Back to saved view',exact:true}).count(),1);
  await page.getByRole('button',{name:'Back to saved view',exact:true}).click();
  assert.equal(await input.inputValue(),'');
  const twitterNoDownload=report.requests.length;await input.fill('https://x.com/Saved123/status/2000000');await wait(300);
  assert.equal(report.requests.slice(twitterNoDownload).filter(r=>r.method==='POST'&&r.path==='/api/twitter/downloads').length,0);
  check('Twitter search/clear retains author scope and Back; URL typing starts no capture');
  await choose('Danbooru');await page.getByTitle('Go Home',{exact:true}).click();
  input=page.getByPlaceholder('Search tags, filenames, IDs, size, or shape...');
  const danStart=report.requests.length;await input.fill('한국');await input.dispatchEvent('keydown',{key:'Enter',isComposing:true,keyCode:229});await wait(150);
  assert.equal(report.requests.slice(danStart).filter(r=>r.path==='/api/images'&&r.q?.includes('한국')).length,0);
  await input.fill('id:1');await input.press('Enter');await wait(350);
  assert(report.requests.slice(danStart).some(r=>r.path==='/api/images'&&r.q==='id:1'));
  check('Danbooru guards IME Enter and opens Browse for a committed query from Home');
  started=false;gate=new Promise(resolve=>release=resolve);
  intercept=async(route,url)=>{if(url.pathname!=='/api/tags/suggest')return false;const q=url.searchParams.get('q');if(q==='alpha'){started=true;await gate;}await route.fulfill({json:[{id:1,name:q,category:'general',count:1}]});return true;};
  await input.fill('alpha');await until(()=>started);await input.fill('beta');await page.getByText('beta',{exact:true}).waitFor();release();await wait(200);
  assert.equal(await page.getByText('alpha',{exact:true}).count(),0);assert.equal(await page.getByText('beta',{exact:true}).count(),1);
  await input.press('Escape');await wait(350);assert.equal(await page.getByText('beta',{exact:true}).count(),0);
  check('Danbooru late suggestions cannot replace newer text or reopen after Escape');
  await input.fill('beta');await page.getByText('beta',{exact:true}).waitFor();
  const blur = await input.evaluate(node=>new Promise(resolve=>{
   node.blur();const started=performance.now();let early=false;
   setTimeout(()=>{early=[...document.querySelectorAll('button span')].some(span=>span.textContent==='beta');},60);
   setTimeout(()=>resolve({early,late:[...document.querySelectorAll('button span')].some(span=>span.textContent==='beta'),elapsed:performance.now()-started}),300);
  }));
  assert(blur.early&&!blur.late);report.timings.suggestionBlurMs=blur.elapsed;
  check('Danbooru retains the delayed blur dismissal for suggestion clicks');

  intercept=null;await input.fill('');await page.getByRole('button',{name:'Tags',exact:true}).first().click();
  const tags=page.getByPlaceholder('Search tags...');await tags.waitFor();
  await input.fill('filename:fixture');await input.press('Enter');assert(await tags.isVisible());
  check('Tags combo editing stays in Tags when its top search adds a term');
  const tagStart=report.requests.length;
  await tags.dispatchEvent('compositionstart');await tags.fill('ㅎ');await wait(400);
  assert.equal(report.requests.slice(tagStart).filter(r=>r.path==='/api/tags'&&r.q==='ㅎ').length,0);
  await tags.fill('fixture_character');await tags.dispatchEvent('compositionend');await wait(450);
  assert(report.requests.slice(tagStart).some(r=>r.path==='/api/tags'&&r.q==='fixture_character'));
  check('Separate Tags search waits for composition and retains its own list scope');
  await choose('Music');const music=page.getByRole('textbox',{name:'Search music',exact:true});await music.waitFor();await music.fill('music');await wait(400);assert.equal(await music.inputValue(),'music');
  await choose('Language');assert.equal(await page.getByRole('textbox',{name:/search/i}).count(),0);
  check('Music keeps its existing search; Language gains no invented control');
  for(const [owner,label,moduleName,selector,size] of [
   ['video','Search videos','Video','.video-tile',120],
   ['manga','Search manga','Manga','.manga-tile',120],
   ['youtube','Search downloads or paste a YouTube URL','YouTube','[data-channel-id]',60]
  ]){
   await choose(moduleName);const field=page.getByRole('textbox',{name:label});await field.waitFor();
   const base=owner==='youtube'?{id:'UC'+'0'.repeat(22),name:'Fixture channel',avatar_id:null,item_count:1}:(await api(libraryPath(owner))).items[0];
   const row=(number,name)=>({...base,id:owner==='youtube'?'UC'+String(number).padStart(22,'0'):'fixture-'+number,path:'search-fixture-'+number+(owner==='video'?'.mp4':''),name,title:name});
   started=false;gate=new Promise(resolve=>release=resolve);
   intercept=async(route,url)=>{
    if(url.pathname!==libraryPath(owner))return false;
    const q=url.searchParams.get('q'),offset=Number(url.searchParams.get('offset')||0);
    if(q==='paged'){
     if(offset){started=true;await gate;await route.fulfill({json:{items:[row(1000,'stale-page')],total:size+1}});}
     else await route.fulfill({json:{items:Array.from({length:size},(_,n)=>row(n,'page-'+n)),total:size+1}});
     return true;
    }
    if(q==='replacement'){await route.fulfill({json:{items:[row(2000,'replacement')],total:1}});return true;}
    return false;
   };
   await field.fill('paged');await wait(400);assert.equal(await page.locator(selector).count(),size);
   await page.getByRole('button',{name:owner==='youtube'?'Load more channels':'Load more',exact:true}).click();await until(()=>started);
   await field.fill('replacement');await wait(400);release();await wait(250);
   assert.equal(await page.locator(selector).count(),1);assert.equal(await page.getByText('stale-page',{exact:true}).count(),0);
   assert.equal(await page.getByText('replacement',{exact:true}).count(),1);intercept=null;
   check(owner+' rejects an old continuation after the query changes');
  }
  let disabledOwner=false;
  for(let attempt=0;attempt<50;attempt++){
   const response=await context.request.post(config.url+'/api/suite/modules/video/disable',{headers:{Origin:config.url}});
   if(response.ok()){disabledOwner=true;break;}
   assert.equal(response.status(),409);assert.match((await response.json()).detail,/Wait for module operations/);await wait(100);
  }
  assert(disabledOwner,'Module work did not drain in the bounded wait');await page.reload();
  const denied=await context.request.get(config.url+'/api/video/library?q=école');assert.equal(denied.status(),409);
  check('Disabled owner search remains unavailable');
  assert.deepEqual(report.errors,[]);await page.screenshot({path:path.join(config.output,'module-search.png')});
 } catch(error) {report.failure=error.stack;await page.screenshot({path:path.join(config.output,'failure.png')}).catch(()=>{});throw error;}
 finally {fs.writeFileSync(path.join(config.output,'report.json'),JSON.stringify(report,null,2));await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});

const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const report={checks:[],errors:[],console:[]};
(async()=>{
 const browser=await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL});
 const context=await browser.newContext({viewport:{width:1280,height:850}});
 // Verify the real Copy button's argument without touching the user's clipboard.
 await context.addInitScript(()=>{Object.defineProperty(navigator,'clipboard',{value:{writeText:async value=>{window.__copiedPath=value;}}});});
 const page=await context.newPage();page.setDefaultTimeout(15000);
 page.on('pageerror',error=>report.errors.push(error.message));page.on('console',message=>{if(message.type()==='error')report.console.push(message.text());});
 const check=text=>{report.checks.push(text);console.log('PASS: '+text);};
 const api=async(url,method='GET',body)=>{const response=await context.request.fetch(config.url+url,{method,headers:{Origin:config.url},data:body});assert(response.ok(),await response.text());return response.json();};
 const tile=relative=>page.locator(`[data-files-entry-path=${JSON.stringify(relative)}]`);
 const crumb=page.getByRole('navigation',{name:'Current folder'});
 const information=page.getByRole('dialog',{name:'File information',exact:true});
 async function openInfo(relative){await tile(relative).scrollIntoViewIfNeeded();await page.waitForTimeout(80);await tile(relative).click({button:'right'});await page.getByRole('menuitem',{name:'Show info',exact:true}).click();await information.waitFor();}
 async function copied(expected){const button=information.getByTitle('Click to copy');assert.equal(await button.innerText(),expected);await button.click();await page.waitForFunction(value=>window.__copiedPath===value,expected);}
 const originals={};
 function hashes(folder){for(const name of fs.readdirSync(folder,{withFileTypes:true})){const file=path.join(folder,name.name);if(name.isDirectory())hashes(file);else originals[file]=fs.readFileSync(file).toString('base64');}}
 hashes(config.mediaLocal||config.media);
 try{
  await page.goto(config.url);
  const outer=await api('/api/files/sources','POST',{path:config.media,display_name:'Parent shelf'});
  const upper=await api('/api/files/sources','POST',{path:config.media+'/CaseLibrary',display_name:'Upper shelf'});
  const lower=await api('/api/files/sources','POST',{path:config.media+'/caselibrary',display_name:'Lower shelf'});
  assert.notEqual(upper.source_id,lower.source_id);
  for(const source of [outer,upper,lower])await api('/api/files/sources/'+source.source_id+'/scan','POST');
  await page.reload();await page.getByRole('button',{name:'Parent shelf',exact:true}).first().click();
  await tile('caselibrary').click();await crumb.getByRole('button',{name:'Lower shelf',exact:true}).waitFor();await tile('ExactName.txt').waitFor();
  check('Opening a case-distinct nested Linux root selects its own source rather than its uppercase sibling');
  await openInfo('ExactName.txt');await copied(lower.path+'/ExactName.txt');await information.getByRole('button',{name:'Close',exact:true}).click();
  check('File Properties and its real Copy action preserve the exact POSIX root and filename spelling');
  for(const [source,name] of [[upper,'Upper shelf'],[lower,'Lower shelf']]){
   await page.evaluate(({id})=>window.__showInFiles(id,'Deep/子/Same.txt'),{id:source.source_id});
   await crumb.getByRole('button',{name,exact:true}).waitFor();await tile('Deep/子/Same.txt').waitFor();
   await page.waitForFunction(()=>document.activeElement?.getAttribute('data-files-entry-path')==='Deep/子/Same.txt');
   await openInfo('Deep/子/Same.txt');await copied(source.path+'/Deep/子/Same.txt');await information.getByRole('button',{name:'Close',exact:true}).click();
   const response=await context.request.get(config.url+'/api/files/file?'+new URLSearchParams({source_id:source.source_id,path:'Deep/子/Same.txt'}));assert.equal(response.status(),200);assert.equal(await response.text(),path.basename(source.path)+' nested');
  }
  check('Show in Files keeps same-name Unicode descendants, focus and copied paths attached to their exact case-distinct roots');
  await crumb.getByRole('button',{name:'Lower shelf',exact:true}).click();
  await openInfo('Deep');await copied(lower.path+'/Deep');await information.getByRole('button',{name:'Close',exact:true}).click();
  const sources=await api('/api/files/sources');assert.equal(sources.find(item=>item.source_id===upper.source_id).path,upper.path);assert.equal(sources.find(item=>item.source_id===lower.source_id).path,lower.path);
  for(const [file,data] of Object.entries(originals))assert.equal(fs.readFileSync(file).toString('base64'),data);
  await page.setViewportSize({width:360,height:440});await page.waitForTimeout(200);await openInfo('ExactName.txt');await copied(lower.path+'/ExactName.txt');const box=await information.boundingBox();assert(box.x>=0&&box.x+box.width<=360);await page.screenshot({path:path.join(config.output,'exact-path-360.png')});await information.getByRole('button',{name:'Close',exact:true}).click();
  check('Narrow Properties fits while exact paths, original bytes, registration IDs and stored source paths remain unchanged');
  assert.deepEqual(report.errors,[]);
 }finally{fs.writeFileSync(path.join(config.output,'report.json'),JSON.stringify(report,null,2));await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});

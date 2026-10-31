const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
(async()=>{
 const browser=await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 page.setDefaultTimeout(15000);
 const report={checks:[],errors:[],expectedConsoleErrors:[],reports:[]};
 const check=value=>{report.checks.push(value);console.log('PASS: '+value);};
 page.on('pageerror',error=>report.errors.push(error.message));
 page.on('console',message=>{if(message.type()==='error'){
   if(/503|429|422/.test(message.text()))report.expectedConsoleErrors.push(message.text());
   else report.errors.push(message.text());
 }});
 page.on('request',request=>{
   const url=new URL(request.url());
   if(url.origin!==config.url)report.errors.push('Unexpected remote request');
   if(url.pathname==='/api/diagnostics/frontend')report.reports.push(request.postDataJSON());
 });
 async function waitFor(predicate){
  for(let index=0;index<100;index++){if(predicate())return;await page.waitForTimeout(50);}
  throw new Error('Expected diagnostic event was not received');
 }
 try{
  await page.goto(config.url);
  await page.getByRole('button',{name:'Open Keivotos menu',exact:true}).waitFor();
  await page.evaluate(()=>{
   window.dispatchEvent(new ErrorEvent('error',{error:new TypeError('private script fixture body'),
    message:'private script fixture body',filename:'https://private.invalid/path?password=private',lineno:4,colno:2}));
   const event=new PromiseRejectionEvent('unhandledrejection',{reason:new RangeError('private rejection fixture body'),promise:Promise.resolve(),cancelable:true});
   event.preventDefault();window.dispatchEvent(event);
  });
  await waitFor(()=>report.reports.some(event=>event.code==='script_error')&&report.reports.some(event=>event.code==='unhandled_rejection'));
  for(const event of report.reports){
   assert(!JSON.stringify(event).includes('private'));
   assert.match(event.operation_id,/^[0-9a-f-]{36}$/);
  }
  check('real browser script/rejection events record only fixed safe facts and an operation ID');
  await page.evaluate(()=>fetch('/api/fixture/diagnostic-mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:true})}));
  const failedResponse=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/backups'&&response.status()===503);
  await page.getByRole('button',{name:'Open Keivotos menu',exact:true}).click();
  await page.getByRole('button',{name:'Settings',exact:true}).click();
  const settings=page.getByRole('dialog',{name:'Settings',exact:true});
  await settings.getByRole('button',{name:'Backup',exact:true}).click();
  const response=await failedResponse;
  const identity=await response.headerValue('X-Keivotos-Operation-ID');
  await waitFor(()=>report.reports.some(event=>event.code==='request_failed'&&event.operation_id===identity));
  report.failedOperation=identity;
  assert.equal(await settings.isVisible(),true);
  check('an actual Settings request failure shares its backend operation ID with the browser report');
  const rejected=await page.evaluate(async()=>{
   const response=await fetch('/api/diagnostics/frontend',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({code:'script_error',message:'private invalid message',body:'private invalid body'})});
   return response.status;
  });
  assert.equal(rejected,422);
  // The deliberately invalid payload above is tracked separately from valid reports.
  report.reports=report.reports.filter(event=>!Object.hasOwn(event,'message'));
  assert(report.reports.every(event=>!JSON.stringify(event).includes('private')));
  check('the real endpoint rejects raw text/body fields without accepting their contents');

  await page.evaluate(()=>fetch('/api/fixture/diagnostic-mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:false})}));
  await settings.getByRole('button',{name:'Advanced',exact:true}).click();
  const days=page.getByRole('spinbutton',{name:'Keep logs (days)',exact:true});
  const total=page.getByRole('spinbutton',{name:'Total log space (MiB)',exact:true});
  const rotation=page.getByRole('spinbutton',{name:'Rotate each file (MiB)',exact:true});
  await days.waitFor();
  assert.equal(await days.inputValue(),'14');
  assert.equal(await total.inputValue(),'256');assert.equal(await rotation.inputValue(),'16');
  await days.fill('21');await total.fill('64');await rotation.fill('2');
  await page.getByRole('button',{name:'Save log limits',exact:true}).click();
  await page.getByText('Log limits saved.',{exact:true}).waitFor();
  let limits=await page.evaluate(async()=>await(await fetch('/api/diagnostics/policy')).json());
  assert.deepEqual(limits.limits,{retention_days:21,total_mib:64,rotation_mib:2});
  check('log limits show the approved defaults and save through the actual isolated API');
  const originalPreferences=await page.evaluate(async()=>await(await fetch('/api/diagnostics')).json());
  await page.getByRole('button',{name:'Start debug logging',exact:true}).click();
  await page.getByRole('button',{name:'Stop debug logging',exact:true}).waitFor();
  limits=await page.evaluate(async()=>await(await fetch('/api/diagnostics/policy')).json());
  assert(limits.debug.active&&limits.debug.remaining_seconds>1790&&limits.debug.remaining_seconds<=1800);
  assert.deepEqual(await page.evaluate(async()=>await(await fetch('/api/diagnostics')).json()),originalPreferences);
  await page.getByRole('button',{name:'Stop debug logging',exact:true}).click();
  await page.getByRole('button',{name:'Start debug logging',exact:true}).waitFor();
  check('temporary debug logging starts/stops independently of both saved legacy preferences');
  await page.route('**/api/diagnostics/policy',route=>route.request().method()==='PUT'
    ?route.fulfill({status:503,json:{detail:'Intentional policy fixture failure'}}):route.continue());
  await days.fill('23');await page.getByRole('button',{name:'Save log limits',exact:true}).click();
  await settings.getByRole('alert').waitFor();
  assert.equal(await days.inputValue(),'23');
  limits=await page.evaluate(async()=>await(await fetch('/api/diagnostics/policy')).json());
  assert.equal(limits.limits.retention_days,21);
  await page.unroute('**/api/diagnostics/policy');
  await page.getByRole('button',{name:'Save log limits',exact:true}).click();
  await page.getByText('Log limits saved.',{exact:true}).waitFor();
  check('failed policy saves retain the draft and allow an explicit successful retry');
  for(const style of ['legacy','modern']){
    await page.locator('#settings-style').selectOption(style);
    await days.waitFor();await page.waitForTimeout(250);
    assert.equal(await days.inputValue(),'23');
    await page.screenshot({path:path.join(config.output,'policy-'+style+'.png'),fullPage:true});
  }
  check('log controls and saved limits remain available in both existing Settings styles');
  await page.getByRole('button',{name:'Start debug logging',exact:true}).click();
  await page.getByRole('button',{name:'Stop debug logging',exact:true}).waitFor();
  await page.evaluate(()=>fetch('/api/fixture/diagnostic-debug-expire',{method:'POST'}));
  await settings.getByRole('button',{name:'Appearance',exact:true}).click();
  await settings.getByRole('button',{name:'Advanced',exact:true}).click();
  await page.getByRole('button',{name:'Start debug logging',exact:true}).waitFor({timeout:10000});
  limits=await page.evaluate(async()=>await(await fetch('/api/diagnostics/policy')).json());
  assert.equal(limits.debug.active,false);
  check('a real timed debug deadline expires and returns the control to its inactive state');

  await page.setViewportSize({width:360,height:800});
  await page.keyboard.press('Escape');await settings.waitFor({state:'detached'});
  if(await page.locator('.app-drawer-layer').count()){
   await page.keyboard.press('Escape');await page.locator('.app-drawer-layer').waitFor({state:'detached'});
  }
  await page.getByRole('button',{name:'Open Keivotos menu',exact:true}).click();
  await page.keyboard.press('Escape');
  check('failure reporting leaves narrow layout and Escape/focus interaction responsive');
  await page.route('**/api/diagnostics/frontend',route=>route.fulfill({status:503,body:'reporting unavailable'}));
  const before=report.reports.length;
  await page.evaluate(()=>window.dispatchEvent(new ErrorEvent('error',{error:new ReferenceError('private reporting failure'),message:'private reporting failure'})));
  await waitFor(()=>report.reports.length>before);
  await page.waitForTimeout(500);
  assert.equal(report.reports.length,before+1);
  check('a failed reporting endpoint does not start a recursive request loop');
  assert.equal(report.errors.length,0,JSON.stringify(report.errors));
  await page.screenshot({path:path.join(config.output,'diagnostics.png'),fullPage:true});
 }finally{
  fs.writeFileSync(path.join(config.output,'report.json'),JSON.stringify(report,null,2));
  await browser.close();
 }
})().catch(error=>{console.error(error);process.exit(1);});

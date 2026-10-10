const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
assert.match(config.home,/[/\\]keivotos-modularization-[^/\\]+[/\\]home$/);
(async()=>{
 const browser=await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL});
 const context=await browser.newContext({viewport:{width:1280,height:850}});
 const page=await context.newPage();page.setDefaultTimeout(12000);
 const report={checks:[],errors:[],console:[]};
 const watchConsole=p=>p.on('console',m=>{if(m.type()==='error')report.console.push({text:m.text(),location:m.location()});});
 watchConsole(page);context.on('page',watchConsole);
 page.on('pageerror',error=>report.errors.push(error.message));
 await context.tracing.start({screenshots:true,snapshots:true});
 const check=value=>{report.checks.push(value);console.log('PASS: '+value);};
 async function api(url,method='GET',body){return page.evaluate(async({url,method,body})=>{const response=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});return {status:response.status,data:await response.json()};},{url,method,body});}
 async function waitDisk(name,expected){
  const filename=path.join(config.mediaLocal||config.media,name);
  for(let attempt=0;attempt<30&&fs.readFileSync(filename,'utf8')!==expected;attempt++)await new Promise(resolve=>setTimeout(resolve,100));
  assert.equal(fs.readFileSync(filename,'utf8'),expected);
 }
 async function editorText(locator){
  return locator.evaluate(node=>[...node.querySelectorAll('.cm-line')].map(line=>line.textContent).join('\n'));
 }
 try{
  await page.goto(config.url);
  assert.equal((await api('/api/suite/modules/language/enable','POST')).status,200);
  const emptyStudy=await context.newPage();emptyStudy.on('pageerror',error=>report.errors.push(error.message));
  await emptyStudy.goto(config.url);
  await emptyStudy.getByRole('button',{name:'Open Keivotos menu'}).click();
  await emptyStudy.locator('.app-drawer').getByRole('button',{name:'Language',exact:true}).click();
  const emptySections=emptyStudy.getByRole('navigation',{name:'Language sections'});
  assert.deepEqual(await emptySections.locator('button').allTextContents(),['Markdown','Practice','Vocabulary']);
  await emptySections.getByRole('button',{name:'Practice',exact:true}).click();
  await emptyStudy.getByRole('region',{name:'Practice',exact:true}).waitFor();
  assert.equal(await emptyStudy.getByRole('button',{name:'Add Language folders in Settings'}).count(),0);
  await emptySections.getByRole('button',{name:'Vocabulary',exact:true}).click();
  await emptyStudy.getByRole('heading',{name:'Vocabulary',exact:true}).waitFor();
  await emptyStudy.getByRole('button',{name:'New vocabulary entry',exact:true}).click();
  const folderlessEntry=emptyStudy.getByRole('dialog',{name:'New vocabulary',exact:true});
  await folderlessEntry.getByRole('textbox',{name:'Korean word',exact:true}).fill('폴더 없이');
  await folderlessEntry.getByRole('textbox',{name:'English translation',exact:true}).fill('Without a notes folder');
  await folderlessEntry.getByRole('button',{name:'Save',exact:true}).click();
  await emptyStudy.getByRole('cell',{name:'Without a notes folder',exact:true}).waitFor();
  assert.equal(await emptyStudy.getByRole('cell',{name:'폴더 없이',exact:true}).count(),1);
  await emptySections.getByRole('button',{name:'Markdown',exact:true}).click();
  await emptyStudy.getByRole('button',{name:'Add Language folders in Settings'}).waitFor();
  await emptyStudy.close();
  check('Markdown is the initial Language section; Practice and Vocabulary are reachable without assigning a notes folder');
  fs.mkdirSync((config.mediaLocal||config.media)+'-other');
  const applied=await api('/api/suite/folders/apply','POST',{folders:[
   {path:config.media,display_name:'Notes',role:'language'},
   {path:config.media+'-other',display_name:'Other notes',role:'language'}
  ]});
  assert.equal(applied.status,200,JSON.stringify(applied));
  const sourceId=applied.data.sources.find(source=>source.display_name==='Notes').source_id;
  const otherSourceId=applied.data.sources.find(source=>source.display_name==='Other notes').source_id;
  await page.getByRole('button',{name:'Open Keivotos menu'}).click();
  await page.locator('.app-drawer').getByRole('button',{name:'Language',exact:true}).click();
  const welcome=page.getByRole('tab',{name:'Welcome.md',exact:true});
  let revealed=null;
  const revealRoute=async route=>{revealed=route.request().postDataJSON();await route.fulfill({json:{status:'revealed'}});};
  await page.route('**/api/files/reveal',revealRoute);
  await welcome.click({button:'right'});
  await page.getByRole('menu',{name:'Location for Welcome.md'}).getByRole('menuitem',{name:'Open in Explorer'}).click();
  assert.deepEqual(revealed,{source_id:sourceId,path:'Welcome.md'});
  await page.unroute('**/api/files/reveal',revealRoute);
  await welcome.click({button:'right'});
  await page.getByRole('menu',{name:'Location for Welcome.md'}).getByRole('menuitem',{name:'Show in Files'}).click();
  await page.locator('[data-files-entry-path="Welcome.md"]').waitFor();
  await page.getByRole('button',{name:'Open Keivotos menu'}).click();
  await page.locator('.app-drawer').getByRole('button',{name:'Language',exact:true}).click();
  await welcome.waitFor();
  check('Language tab location menu reveals the exact Markdown file and navigates into Files');
  await welcome.click();
  const editor=page.getByRole('textbox',{name:'Edit Welcome.md'});
  await editor.fill('# Updated locally\n');
  await page.waitForTimeout(700);
  assert.equal(fs.readFileSync(path.join(config.mediaLocal||config.media,'Welcome.md'),'utf8'),'# Local note\n');
  await welcome.click({button:'right'});
  await page.getByRole('menu',{name:'Location for Welcome.md'}).getByRole('menuitem',{name:'Show in Files'}).click();
  await page.getByRole('dialog',{name:'Unsaved Markdown changes'}).getByRole('button',{name:'Keep editing'}).click();
  assert.equal(await editorText(editor),'# Updated locally\n');
  assert.equal(await page.locator('.language-surface').count(),1);
  check('Show in Files respects the Language unsaved-note decision');
  const closeWelcome=page.getByRole('button',{name:'Close Welcome.md'});
  assert.equal(await closeWelcome.locator('.dirty-glyph').evaluate(node=>getComputedStyle(node).display),'block');
  await closeWelcome.hover();
  assert.equal(await closeWelcome.locator('.close-glyph').evaluate(node=>getComputedStyle(node).display),'block');
  await closeWelcome.focus();
  assert.equal(await closeWelcome.locator('.close-glyph').evaluate(node=>getComputedStyle(node).display),'block');
  await editor.press('Control+s');
  await waitDisk('Welcome.md','# Updated locally\n');
  await editor.fill('# Temporary edit\n');
  await editor.fill('# Updated locally\n');
  assert.equal(await page.getByRole('button',{name:'Save Welcome.md'}).count(),0);
  assert.equal(await page.evaluate(sourceId=>localStorage.getItem(`keivotos:language-draft:${sourceId}:Welcome.md`),sourceId),null);
  await editor.fill('# Mouse save\n');
  await page.getByRole('button',{name:'Save Welcome.md'}).click();
  await waitDisk('Welcome.md','# Mouse save\n');
  check('Ctrl+S and the visible Save action write only on request; undo to saved text returns clean');

  let savedRequests=0;
  const failedIndexRoute=async route=>{
   if(route.request().method()!=='PUT'){await route.continue();return;}
   savedRequests++;
   const response=await route.fetch();
   assert.equal(response.status(),200);
   const data=await response.json();
   await route.fulfill({response,json:{...data,index_refresh:{status:'failed',warning:'The file change is saved; the Files listing needs refresh.'}}});
  };
  await page.route('**/api/language/document',failedIndexRoute);
  await editor.fill('# Saved despite index warning\n');
  await page.getByRole('button',{name:'Save Welcome.md'}).click();
  const retryIndex=page.getByRole('button',{name:'Retry Files refresh for Welcome.md'});
  await retryIndex.waitFor();
  await waitDisk('Welcome.md','# Saved despite index warning\n');
  assert.equal(await page.getByRole('button',{name:'Save Welcome.md'}).count(),0);
  await editor.fill('# New unsaved edit\n');
  await retryIndex.click();
  await page.getByRole('button',{name:'Refresh Files listing for Welcome.md'}).waitFor();
  assert.equal(await editorText(editor),'# New unsaved edit\n');
  assert.equal(await page.getByRole('button',{name:'Save Welcome.md'}).count(),1);
  assert.equal(savedRequests,1,'index repair must never repeat a file mutation');
  await waitDisk('Welcome.md','# Saved despite index warning\n');
  await page.unroute('**/api/language/document',failedIndexRoute);
  check('saved index warning acknowledges disk bytes; index-only repair preserves newer unsaved text');

  await editor.fill('# Close and save\n');
  await closeWelcome.click();
  const decision=page.getByRole('dialog',{name:'Unsaved Markdown changes'});
  await page.screenshot({path:path.join(config.output,'language-dirty-dialog.png')});
  await decision.press('Shift+Tab');
  assert.equal(await decision.getByRole('button',{name:'Discard changes'}).evaluate(node=>document.activeElement===node),true);
  await decision.press('Tab');
  assert.equal(await decision.getByRole('button',{name:'Save',exact:true}).evaluate(node=>document.activeElement===node),true);
  await decision.press('Escape');
  await decision.waitFor({state:'detached'});
  assert.equal(await editorText(editor),'# Close and save\n');
  await closeWelcome.click();
  await decision.getByRole('button',{name:'Save',exact:true}).click();
  await page.getByRole('tab',{name:'Welcome.md',exact:true}).waitFor({state:'detached'});
  await waitDisk('Welcome.md','# Close and save\n');
  await page.getByRole('combobox',{name:'Open Markdown file'}).selectOption('Welcome.md');
  await page.getByRole('tab',{name:'Welcome.md',exact:true}).waitFor();
  await page.getByRole('textbox',{name:'Edit Welcome.md'}).fill('# Discard this edit\n');
  await closeWelcome.click();
  await decision.getByRole('button',{name:'Discard changes'}).click();
  assert.equal(fs.readFileSync(path.join(config.mediaLocal||config.media,'Welcome.md'),'utf8'),'# Close and save\n');
  await page.getByRole('combobox',{name:'Open Markdown file'}).selectOption('Welcome.md');
  await page.getByRole('tab',{name:'Welcome.md',exact:true}).waitFor();
  assert.equal(await editorText(page.getByRole('textbox',{name:'Edit Welcome.md'})),'# Close and save\n');
  check('dirty dot and close control offer Save, Keep editing and Discard without deleting the file');

  await page.getByRole('button',{name:'New Markdown file'}).click();
  const rename=page.getByRole('textbox',{name:'Rename Markdown file'});
  await rename.waitFor();
  assert.equal(await rename.inputValue(),'New File');
  assert.deepEqual(await rename.evaluate(node=>[node.selectionStart,node.selectionEnd]),[0,8]);
  await rename.fill('Study');await rename.press('Enter');
  await page.getByRole('tab',{name:'Study.md'}).waitFor();
  await page.route('**/api/language/document',async route=>{
   if(route.request().method()==='PUT')await new Promise(resolve=>setTimeout(resolve,500));
   await route.continue();
  });
  const studyEditor=page.getByRole('textbox',{name:'Edit Study.md'});
  const firstSave=page.waitForRequest(request=>request.method()==='PUT'&&request.url().includes('/api/language/document'));
  await studyEditor.fill('First draft');
  await page.getByRole('button',{name:'Save Study.md'}).click();
  await firstSave;
  await studyEditor.fill('한국어 notes');
  await page.getByRole('button',{name:'Save Study.md'}).click();
  await page.getByRole('button',{name:'New Markdown file'}).click();
  await rename.press('Escape');
  await waitDisk('Study.md','한국어 notes');
  await page.unroute('**/api/language/document');
  await page.getByRole('button',{name:'New Markdown file'}).click();
  await rename.waitFor();
  assert.equal(await rename.inputValue(),'New File (2)');
  await rename.press('Escape');
  await page.getByRole('tab',{name:'New File (2).md'}).waitFor();
  await page.getByRole('tab',{name:'Study.md'}).click();
  await page.getByRole('textbox',{name:'Edit Study.md'}).fill('Save before rename');
  await page.getByRole('tab',{name:'Study.md'}).dblclick();
  await decision.getByRole('button',{name:'Keep editing'}).click();
  assert.equal(await rename.count(),0);
  await page.getByRole('tab',{name:'Study.md'}).dblclick();
  await decision.getByRole('button',{name:'Save',exact:true}).click();
  await rename.fill('Study 2');await rename.evaluate(node=>node.blur());
  await page.getByRole('tab',{name:'Study 2.md'}).waitFor();
  check('plus creates collision-safe notes; edits during save remain dirty and rename requires a save decision');

  const current=path.join(config.mediaLocal||config.media,'Study 2.md');
  fs.writeFileSync(current,'external content');
  await page.getByRole('textbox',{name:'Edit Study 2.md'}).fill('unsaved local draft');
  await page.getByRole('button',{name:'Save Study 2.md'}).click();
  await page.getByText(/Markdown file changed outside Keivotos/).waitFor();
  assert.equal(fs.readFileSync(current,'utf8'),'external content');
  assert.equal(await editorText(page.getByRole('textbox',{name:'Edit Study 2.md'})),'unsaved local draft');
  check('external edit conflict keeps both disk content and the visible local draft');

  await page.screenshot({path:path.join(config.output,'language.png')});
  assert.equal((await api('/api/suite/modules/language/disable','POST')).status,200);
  assert.equal((await api('/api/language/folders')).status,409);
  const disabledPage=await context.newPage();await disabledPage.goto(config.url);
  assert.equal(await disabledPage.getByRole('navigation',{name:'Language sections'}).count(),0);
  assert.equal(await disabledPage.locator('[data-sidebar-owner="language"]').count(),0);await disabledPage.close();
  assert.equal(fs.readFileSync(current,'utf8'),'external content');
  assert.equal(fs.readFileSync(path.join(config.mediaLocal||config.media,'Welcome.md'),'utf8'),'# Close and save\n');
  check('disable blocks Language operations and leaves original Markdown files untouched');

  assert.equal((await api('/api/suite/modules/language/enable','POST')).status,200);
  await page.reload();
  await page.getByRole('button',{name:'Open Keivotos menu'}).click();
  await page.locator('.app-drawer').getByRole('button',{name:'Language',exact:true}).click();
  await page.getByRole('tab',{name:'Welcome.md',exact:true}).click();
  await page.getByRole('textbox',{name:'Edit Welcome.md'}).fill('# Draft across restart\n');
  await page.close({runBeforeUnload:false});
  assert.equal(fs.readFileSync(path.join(config.mediaLocal||config.media,'Welcome.md'),'utf8'),'# Close and save\n');
  const reopened=await context.newPage();
  reopened.on('pageerror',error=>report.errors.push(error.message));
  await reopened.goto(config.url);
  await reopened.getByRole('button',{name:'Open Keivotos menu'}).click();
  await reopened.locator('.app-drawer').getByRole('button',{name:'Language',exact:true}).click();
  await reopened.getByRole('tab',{name:'Welcome.md',exact:true}).click();
  const recovered=reopened.getByRole('textbox',{name:'Edit Welcome.md'});
  assert.equal(await editorText(recovered),'# Draft across restart\n');
  assert.equal(await reopened.getByRole('button',{name:'Save Welcome.md'}).count(),1);
  await recovered.press('Control+s');
  await waitDisk('Welcome.md','# Draft across restart\n');
  await reopened.waitForFunction(sourceId=>!localStorage.getItem(`keivotos:language-draft:${sourceId}:Welcome.md`),sourceId);
  check('closing without saving preserves a recoverable draft across a new browser session');

  await recovered.fill('# Folder save A\n');
  await reopened.getByRole('button',{name:'New Markdown file'}).click();
  const newName=reopened.getByRole('textbox',{name:'Rename Markdown file'});
  await newName.fill('Second');await newName.press('Enter');
  await reopened.getByRole('textbox',{name:'Edit Second.md'}).fill('Folder save B');
  await reopened.getByRole('combobox',{name:'Language folder'}).selectOption(otherSourceId);
  const reopenedDecision=reopened.getByRole('dialog',{name:'Unsaved Markdown changes'});
  await reopenedDecision.getByRole('button',{name:'Save',exact:true}).click();
  await waitDisk('Welcome.md','# Folder save A\n');
  await waitDisk('Second.md','Folder save B');
  await reopened.getByRole('combobox',{name:'Language folder'}).selectOption(sourceId);
  await reopened.getByRole('tab',{name:'Welcome.md',exact:true}).click();
  await reopened.getByRole('textbox',{name:'Edit Welcome.md'}).fill('# Module leave draft\n');
  await reopened.getByRole('button',{name:'Open Keivotos menu'}).click();
  const drawer=reopened.locator('.app-drawer');
  await drawer.getByRole('button',{name:'Files',exact:true}).click();
  await reopenedDecision.getByRole('button',{name:'Keep editing'}).click();
  assert.equal(await editorText(reopened.getByRole('textbox',{name:'Edit Welcome.md'})),'# Module leave draft\n');
  await drawer.getByRole('button',{name:'Files',exact:true}).click();
  await reopenedDecision.getByRole('button',{name:'Discard changes'}).click();
  await reopened.getByRole('textbox',{name:'Edit Welcome.md'}).waitFor({state:'detached'});
  await waitDisk('Welcome.md','# Folder save A\n');
  await reopened.getByRole('button',{name:'Open Keivotos menu'}).click();
  await reopened.locator('.app-drawer').getByRole('button',{name:'Language',exact:true}).click();
  await reopened.getByRole('tab',{name:'Welcome.md',exact:true}).click();
  assert.equal(await editorText(reopened.getByRole('textbox',{name:'Edit Welcome.md'})),'# Folder save A\n');
  check('folder switch saves all dirty tabs; module switch asks before leaving and discard keeps disk bytes');

  const formatted=reopened.getByRole('textbox',{name:'Edit Welcome.md'});
  const toolbar=reopened.getByRole('toolbar',{name:'Markdown formatting'});
  for(let level=1;level<=6;level++){
   await formatted.fill('Heading text');
   await toolbar.getByRole('combobox',{name:'Heading level'}).selectOption(`heading-${level}`);
   assert.equal(await editorText(formatted),'#'.repeat(level)+' Heading text');
  }
  for(const [button,expected] of [
   ['Bold','**words**'],['Italic','*words*'],['Bold and italic','***words***'],['Highlight','==words==']
  ]){
   await formatted.fill('words');await formatted.press('Control+a');
   await toolbar.getByRole('button',{name:button,exact:true}).click();
   assert.equal(await editorText(formatted),expected);
  }
  await formatted.press('Control+z');
  assert.equal(await editorText(formatted),'words');
  await formatted.press('Control+y');
  assert.equal(await editorText(formatted),'==words==');
  assert.equal(await formatted.locator('.cm-markdown-highlight').count(),1);
  await formatted.fill('before');
  await toolbar.getByRole('button',{name:'Insert horizontal rule'}).click();
  assert.equal(await editorText(formatted),'before\n\n---\n');
  check('heading levels, rule, bold, italic, combined emphasis and highlight insert readable Markdown');

  const remoteRequests=[];
  reopened.on('request',request=>{if(!request.url().startsWith(config.url))remoteRequests.push(request.url());});
  const corpus='---\ntitle: 원문\n---\n\n# Heading\n\n**bold *nested*** and ***both***\n'+
   '| One | Two |\n| --- | --- |\n| escaped \\| pipe | 한국어 |\n\n'+
   '`==literal==` and \\==escaped== and ==visible==\n\n'+
   '```md\n==fenced==\n```\n\n'+
   '<script>window.__languageUnsafe = true</script>\n'+
   '![remote](https://example.invalid/never-load.png)\n';
  await formatted.fill(corpus);
  assert.equal(await formatted.locator('.cm-markdown-highlight').count(),1);
  assert.equal(await reopened.evaluate(()=>window.__languageUnsafe),undefined);
  await formatted.press('Control+s');
  await waitDisk('Welcome.md',corpus);
  await reopened.getByRole('tab',{name:'Study 2.md'}).click();
  await reopened.getByRole('tab',{name:'Welcome.md'}).click();
  assert.equal(await editorText(reopened.getByRole('textbox',{name:'Edit Welcome.md'})),corpus);
  assert.equal(await reopened.getByRole('button',{name:'Save Welcome.md'}).count(),0);
  assert.deepEqual(remoteRequests,[]);
  await reopened.screenshot({path:path.join(config.output,'language-markdown-editor.png')});
  check('mixed Markdown round-trips unchanged; literal markers stay literal and raw HTML/remote images do not run');
  // Deterministic clipboard seam: never inspect or overwrite the user's OS clipboard.
  await reopened.evaluate(()=>{
   window.__testClipboard={text:'',denyRead:false,denyWrite:false,delay:0};
   Object.defineProperty(navigator,'clipboard',{configurable:true,value:{
    readText:async()=>{const state=window.__testClipboard;await new Promise(resolve=>setTimeout(resolve,state.delay));if(state.denyRead)throw new DOMException('Denied','NotAllowedError');return state.text;},
    writeText:async text=>{const state=window.__testClipboard;await new Promise(resolve=>setTimeout(resolve,state.delay));if(state.denyWrite)throw new DOMException('Denied','NotAllowedError');state.text=text;}
   }});
  });
  const textMenu=reopened.getByRole('menu',{name:'Markdown text actions'});
  async function openTextMenu(){await formatted.press('Shift+F10');await textMenu.waitFor();}
  await formatted.fill('alpha beta');
  await formatted.press('Control+Home');
  for(let index=0;index<5;index++)await formatted.press('Shift+ArrowRight');
  const firstLine=await formatted.locator('.cm-line').first().boundingBox();
  await reopened.mouse.click(firstLine.x+15,firstLine.y+10,{button:'right'});
  await textMenu.waitFor();
  await textMenu.getByRole('menuitem',{name:'Copy',exact:true}).click();
  assert.equal(await reopened.evaluate(()=>window.__testClipboard.text),'alpha');
  assert.equal(await editorText(formatted),'alpha beta');
  await openTextMenu();
  await textMenu.getByRole('menuitem',{name:'Cut',exact:true}).click();
  assert.equal(await editorText(formatted),' beta');
  await formatted.press('Control+z');
  assert.equal(await editorText(formatted),'alpha beta');
  await openTextMenu();
  await textMenu.getByRole('menuitem',{name:'Paste',exact:true}).click();
  assert.equal(await editorText(formatted),'alpha beta');
  check('keyboard text menu retains selection; Copy is nonmutating and Cut deletes only copied text with one-step undo');

  await formatted.press('Control+Home');
  for(let index=0;index<5;index++)await formatted.press('Shift+ArrowRight');
  await reopened.evaluate(()=>{window.__testClipboard.denyWrite=true;});
  for(const kind of ['Copy','Cut']){
   await openTextMenu();await textMenu.getByRole('menuitem',{name:kind,exact:true}).click();
   await textMenu.getByRole('alert').waitFor();
   assert.match(await textMenu.getByRole('alert').innerText(),/Clipboard access was blocked/);
   assert.equal(await editorText(formatted),'alpha beta');
   await textMenu.press('Escape');
  }
  await reopened.evaluate(()=>{window.__testClipboard.denyRead=true;});
  await openTextMenu();await textMenu.getByRole('menuitem',{name:'Paste',exact:true}).click();
  await textMenu.getByRole('alert').waitFor();
  assert.match(await textMenu.getByRole('alert').innerText(),/Ctrl\+V/);
  await textMenu.press('Escape');
  assert.equal(await formatted.evaluate(node=>node.contains(document.activeElement)),true);
  assert.equal(await editorText(formatted),'alpha beta');
  await reopened.evaluate(()=>{Object.assign(window.__testClipboard,{denyRead:false,denyWrite:false,text:''});});
  await formatted.press('ArrowRight');
  await formatted.press('Control+s');await waitDisk('Welcome.md','alpha beta');
  await openTextMenu();
  assert.equal(await textMenu.getByRole('menuitem',{name:'Copy',exact:true}).isDisabled(),true);
  assert.equal(await textMenu.getByRole('menuitem',{name:'Cut',exact:true}).isDisabled(),true);
  await textMenu.getByRole('menuitem',{name:'Paste',exact:true}).click();
  assert.equal(await reopened.getByRole('button',{name:'Save Welcome.md'}).count(),0);
  check('denied clipboard never deletes content, exposes keyboard fallback and restores focus; empty Paste keeps a saved note clean');

  const paste='한국어 \\| literal\n<script>window.__clipboardUnsafe=true</script>\n![image](https://example.invalid/clipboard.png)';
  await reopened.evaluate(text=>{window.__testClipboard.text=text;},paste);
  await formatted.press('Control+End');await openTextMenu();
  await textMenu.getByRole('menuitem',{name:'Paste',exact:true}).click();
  assert.equal(await editorText(formatted),'alpha beta'+paste);
  assert.equal(await reopened.evaluate(()=>window.__clipboardUnsafe),undefined);
  assert.deepEqual(remoteRequests,[]);
  await formatted.press('Control+z');assert.equal(await editorText(formatted),'alpha beta');
  check('Paste inserts exact plain Unicode/Markdown source, stays inert and can be undone without remote requests');

  await formatted.press('Control+Home');
  for(let index=0;index<5;index++)await formatted.press('Shift+ArrowRight');
  await reopened.evaluate(()=>{window.__testClipboard.delay=450;});
  await openTextMenu();await textMenu.getByRole('menuitem',{name:'Cut',exact:true}).click();
  await reopened.getByRole('tab',{name:'Second.md',exact:true}).click();
  await reopened.waitForTimeout(550);
  assert.equal(await editorText(reopened.getByRole('textbox',{name:'Edit Second.md'})),'Folder save B');
  await reopened.getByRole('tab',{name:'Welcome.md',exact:true}).click();
  assert.equal(await editorText(formatted),'alpha beta');
  await reopened.evaluate(()=>{window.__testClipboard.delay=0;});
  check('switching notes during delayed Cut prevents late clipboard completion from deleting either note');

  await formatted.fill('beforeafter');await formatted.press('Control+Home');
  for(let index=0;index<6;index++)await formatted.press('ArrowRight');
  await openTextMenu();
  await textMenu.press('End');
  assert.equal(await textMenu.getByRole('menuitem',{name:'Insert table'}).evaluate(node=>document.activeElement===node),true);
  await reopened.keyboard.press('Enter');
  const initial='before\n\n|  |  |\n| --- | --- |\n|  |  |\n\nafter';
  assert.equal(await editorText(formatted),initial);
  const addRow=reopened.getByRole('button',{name:'Add table row',exact:true});
  const addColumn=reopened.getByRole('button',{name:'Add table column',exact:true});
  await addRow.waitFor();await addColumn.waitFor();
  await addRow.click();assert.equal(await editorText(formatted),initial.replace('|  |  |\n\nafter','|  |  |\n|  |  |\n\nafter'));
  await formatted.press('Control+z');assert.equal(await editorText(formatted),initial);
  await formatted.press('Control+y');
  await addRow.focus();await addRow.press('Space');
  await addColumn.focus();await addColumn.press('Enter');
  await addColumn.click();
  const grown='before\n\n|  |  |  |  |\n| --- | --- | --- | --- |\n|  |  |  |  |\n|  |  |  |  |\n|  |  |  |  |\n\nafter';
  assert.equal(await editorText(formatted),grown);
  await formatted.press('Control+z');
  assert.equal(await editorText(formatted),grown.replaceAll('|  |  |  |  |','|  |  |  |').replace('| --- | --- | --- | --- |','| --- | --- | --- |'));
  await formatted.press('Control+y');assert.equal(await editorText(formatted),grown);
  await reopened.screenshot({path:path.join(config.output,'language-table-controls.png')});
  check('Insert table preserves surrounding text at the caret; 2x2 grows one row/column per mouse or keyboard action with separate undo/redo');

  async function replaceSource(text){await formatted.press('Control+a');await reopened.keyboard.insertText(text);}
  const escapedTable='| Name | Meaning |\n| :--- | ---: |\n| escaped \\| pipe | 日本語 한국어 |';
  await replaceSource(escapedTable);
  await formatted.press('Control+Home');await formatted.press('ArrowDown');await formatted.press('ArrowDown');
  await formatted.press('Home');for(let index=0;index<4;index++)await formatted.press('ArrowRight');
  await addColumn.click();
  await formatted.type('X');
  const exactTable='| Name | Meaning |  |\n| :--- | ---: | --- |\n| esXcaped \\| pipe | 日本語 한국어 |  |';
  assert.equal(await editorText(formatted),exactTable);
  await addRow.click();
  const savedTable=exactTable+'\n|  |  |  |';
  await formatted.press('Control+s');await waitDisk('Welcome.md',savedTable);
  await reopened.getByRole('tab',{name:'Second.md'}).click();
  await reopened.getByRole('tab',{name:'Welcome.md'}).click();
  assert.equal(await editorText(formatted),savedTable);
  assert.equal(await reopened.getByRole('button',{name:'Save Welcome.md'}).count(),0);
  check('table growth preserves cursor, alignment, escaped pipes and Unicode; explicit save/reopen matches exact Markdown bytes');

  await replaceSource('A | B\n--- | ---\nx | y');await addColumn.waitFor();await addColumn.click();
  assert.equal(await editorText(formatted),'A | B |  |\n--- | --- | --- |\nx | y |  |');
  await addRow.click();
  assert.equal(await editorText(formatted),'A | B |  |\n--- | --- | --- |\nx | y |  |\n|  |  |  |');
  await replaceSource('| A | B |\n| --- | --- |\n| extra | cell | retained |');
  assert.equal(await addColumn.isDisabled(),true);assert.equal(await addRow.isDisabled(),true);
  assert.equal(await editorText(formatted),'| A | B |\n| --- | --- |\n| extra | cell | retained |');
  await replaceSource('```md\n| A | B |\n| --- | --- |\n| x | y |\n```');
  await formatted.press('Control+Home');await formatted.press('ArrowDown');
  assert.equal(await addColumn.count(),0);assert.equal(await addRow.count(),0);
  await openTextMenu();assert.equal(await textMenu.getByRole('menuitem',{name:'Insert table'}).isDisabled(),true);
  await textMenu.press('Escape');
  check('optional outer pipes expand correctly; ragged table cells are preserved and code-fenced tables stay literal');

  await replaceSource('> | A | B |\n> | --- | --- |\n> | x | y |');
  await addColumn.waitFor();await addColumn.click();await addRow.click();
  assert.equal(await editorText(formatted),'> | A | B |  |\n> | --- | --- | --- |\n> | x | y |  |\n> |  |  |  |');
  check('nested blockquote table growth preserves every Markdown prefix');

  await reopened.setViewportSize({width:360,height:700});
  const existingWidth=await reopened.evaluate(()=>document.documentElement.scrollWidth);
  await replaceSource('| Very long heading | 説明 |\n| --- | --- |\n| '+('long '.repeat(24))+' | 漢字 |');
  await addColumn.waitFor();await addColumn.focus();
  await addColumn.press('Enter');
  const bodyWidth=await reopened.evaluate(()=>document.documentElement.scrollWidth);
  assert.ok(bodyWidth<=Math.max(360,existingWidth),`table introduced horizontal overflow: ${bodyWidth} versus ${existingWidth}`);
  const buttonBounds=await addColumn.boundingBox();
  assert.ok(buttonBounds.x>=0&&buttonBounds.x+buttonBounds.width<=360);
  const bounds=await formatted.boundingBox();
  assert.ok(bounds.x>=0&&bounds.x+bounds.width<=360);
  report.compact={existingDocumentWidth:existingWidth,documentWidth:bodyWidth,editor:bounds,columnButton:buttonBounds};
  await reopened.mouse.click(bounds.x+bounds.width-12,bounds.y+bounds.height-16,{button:'right'});
  await textMenu.waitFor();const menuBounds=await textMenu.boundingBox();
  assert.ok(menuBounds.x>=8&&menuBounds.y>=8&&menuBounds.x+menuBounds.width<=352&&menuBounds.y+menuBounds.height<=692);
  await reopened.screenshot({path:path.join(config.output,'language-table-compact.png')});
  await textMenu.press('Escape');
  check('long tables retain line wrapping and keyboard controls; right-click menu fits a compact viewport and dismisses with Escape');

  // Requests belong to the folder/session that initiated them, even when a
  // different folder has the same filename and starting content revision.
  const racePage=await context.newPage();racePage.setDefaultTimeout(12000);
  racePage.on('pageerror',error=>report.errors.push(error.message));
  const notesA=config.mediaLocal||config.media,notesB=notesA+'-other';
  const shared='Race shared.md',onlyA='Race A.md',onlyB='Race B.md',template='# Shared template\n';
  fs.writeFileSync(path.join(notesA,shared),template,{flag:'wx'});
  fs.writeFileSync(path.join(notesB,shared),template,{flag:'wx'});
  fs.writeFileSync(path.join(notesA,onlyA),'Only A',{flag:'wx'});
  fs.writeFileSync(path.join(notesB,onlyB),'Only B',{flag:'wx'});
  await racePage.goto(config.url);
  await racePage.getByRole('button',{name:'Open Keivotos menu'}).click();
  await racePage.locator('.app-drawer').getByRole('button',{name:'Language',exact:true}).click();
  const raceFolder=racePage.getByRole('combobox',{name:'Language folder',exact:true});
  async function folderTo(id,visible){
    await raceFolder.selectOption(id);
    await racePage.getByRole('tab',{name:visible,exact:true}).waitFor();
    assert.equal(await raceFolder.inputValue(),id);
  }
  async function hold(method,endpoint,owner,name){
    let reached,release,completed,body;
    const received=new Promise(resolve=>reached=resolve),unlocked=new Promise(resolve=>release=resolve),done=new Promise(resolve=>completed=resolve);
    const handler=async route=>{
      const request=route.request(),url=new URL(request.url());
      const payload=method==='GET'?Object.fromEntries(url.searchParams):request.postDataJSON();
      if(request.method()!==method||url.pathname!==endpoint||payload.source_id!==owner||(name&&payload.name!==name))return route.continue();
      const response=await route.fetch();assert(response.ok(),await response.text());body=await response.json();
      reached();await unlocked;await route.fulfill({response});completed();
    };
    await racePage.route('**/api/language/**',handler);
    return {received,get body(){return body;},release:async()=>{
      const delivered=racePage.waitForResponse(response=>response.request().method()===method&&new URL(response.url()).pathname===endpoint);
      release();await done;await (await delivered).finished();
      await racePage.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
      await racePage.unroute('**/api/language/**',handler);
    }};
  }
  async function saveRace(name,text,owner,folder){
    const input=racePage.getByRole('textbox',{name:`Edit ${name}`,exact:true});
    await input.fill(text);
    const saving=racePage.waitForRequest(request=>request.method()==='PUT'&&new URL(request.url()).pathname==='/api/language/document');
    await input.press('Control+s');const payload=(await saving).postDataJSON();
    assert.equal(payload.source_id,owner);assert.equal(payload.name,name);
    await racePage.getByRole('button',{name:`Save ${name}`,exact:true}).waitFor({state:'detached'});
    assert.equal(fs.readFileSync(path.join(folder,name),'utf8'),text);
  }

  await folderTo(sourceId,onlyA);
  let delayed=await hold('GET','/api/language/document',sourceId,shared);
  await racePage.getByRole('tab',{name:shared,exact:true}).click();await delayed.received;
  await folderTo(otherSourceId,onlyB);await delayed.release();
  assert.equal(await racePage.getByRole('textbox',{name:`Edit ${shared}`,exact:true}).count(),0);
  assert.equal(fs.readFileSync(path.join(notesA,shared),'utf8'),template);
  assert.equal(fs.readFileSync(path.join(notesB,shared),'utf8'),template);
  await racePage.getByRole('tab',{name:shared,exact:true}).click();
  await saveRace(shared,'Explicit B edit',otherSourceId,notesB);
  await folderTo(sourceId,onlyA);await racePage.getByRole('tab',{name:shared,exact:true}).click();
  await saveRace(shared,'Explicit A edit',sourceId,notesA);
  assert.equal(fs.readFileSync(path.join(notesB,shared),'utf8'),'Explicit B edit');
  check('delayed cross-folder read cannot bind a note to another source; identical starting revisions save only after an explicit current-folder open');

  await folderTo(otherSourceId,onlyB);
  delayed=await hold('GET','/api/language/documents',sourceId);
  await raceFolder.selectOption(sourceId);await delayed.received;
  await folderTo(otherSourceId,onlyB);await delayed.release();
  assert.equal(await racePage.getByRole('tab',{name:onlyA,exact:true}).count(),0);
  assert.equal(await racePage.getByRole('tab',{name:onlyB,exact:true}).count(),1);
  check('an older folder listing cannot replace the current folder tabs');

  await folderTo(sourceId,onlyA);
  delayed=await hold('POST','/api/language/documents',sourceId);
  await racePage.getByRole('button',{name:'New Markdown file'}).click();await delayed.received;
  const created=delayed.body.name;
  assert.equal(fs.readFileSync(path.join(notesA,created),'utf8'),'');
  fs.writeFileSync(path.join(notesB,created),'',{flag:'wx'});
  await folderTo(otherSourceId,onlyB);await delayed.release();
  assert.equal(await racePage.getByRole('textbox',{name:'Rename Markdown file'}).count(),0);
  assert.equal(await racePage.getByRole('textbox',{name:`Edit ${created}`,exact:true}).count(),0);
  await racePage.getByRole('tab',{name:created,exact:true}).click();
  await saveRace(created,'Explicit B created-note edit',otherSourceId,notesB);
  assert.equal(fs.readFileSync(path.join(notesA,created),'utf8'),'');
  check('late create completion stays in its initiating folder and cannot open or rename a same-named note elsewhere');

  delayed=await hold('POST','/api/language/document/rename',otherSourceId,created);
  await racePage.getByRole('tab',{name:created,exact:true}).dblclick();
  const raceRename=racePage.getByRole('textbox',{name:'Rename Markdown file'});
  await raceRename.fill('Race renamed B');await raceRename.press('Enter');await delayed.received;
  await folderTo(sourceId,onlyA);await delayed.release();
  assert.equal(await racePage.getByRole('tab',{name:'Race renamed B.md',exact:true}).count(),0);
  assert.equal(await racePage.getByRole('textbox').count(),0);
  assert.equal(fs.readFileSync(path.join(notesB,'Race renamed B.md'),'utf8'),'Explicit B created-note edit');
  assert.equal(fs.readFileSync(path.join(notesA,created),'utf8'),'');
  check('an accepted rename finishes in its original folder without altering the new folder view');

  delayed=await hold('GET','/api/language/document',sourceId,onlyA);
  await racePage.getByRole('tab',{name:onlyA,exact:true}).click();await delayed.received;
  await racePage.getByRole('tab',{name:shared,exact:true}).click();
  await racePage.getByRole('textbox',{name:`Edit ${shared}`,exact:true}).waitFor();await delayed.release();
  assert.equal(await racePage.getByRole('textbox',{name:`Edit ${onlyA}`,exact:true}).count(),0);
  assert.equal(await editorText(racePage.getByRole('textbox',{name:`Edit ${shared}`,exact:true})),'Explicit A edit');
  check('a late note read cannot steal the newer note selection within the same folder');

  delayed=await hold('GET','/api/language/document',sourceId,onlyA);
  await racePage.getByRole('tab',{name:onlyA,exact:true}).click();await delayed.received;
  await racePage.getByRole('button',{name:`Close ${onlyA}`,exact:true}).click();await delayed.release();
  assert.equal(await racePage.getByRole('tab',{name:onlyA,exact:true}).count(),0);
  assert.equal(fs.readFileSync(path.join(notesA,onlyA),'utf8'),'Only A');
  await racePage.screenshot({path:path.join(config.output,'language-folder-ownership.png')});
  await racePage.close();
  check('closing a pending note prevents late reopening while retaining the original file');

  // The same CodeMirror instance must survive section navigation; a section
  // change cancels selection requests without changing their original files.
  const navName='Section study.md',lateName='Late section.md';
  const navOriginal='# Section study\n'+Array.from({length:35},(_,i)=>`Line ${i+1}: Korean study`).join('\n');
  fs.writeFileSync(path.join(notesA,navName),navOriginal,{flag:'wx'});
  fs.writeFileSync(path.join(notesA,lateName),'Delayed section note',{flag:'wx'});
  const nav=await context.newPage();nav.setDefaultTimeout(12000);nav.on('pageerror',error=>report.errors.push(error.message));
  await nav.addInitScript(()=>{
   localStorage.setItem('keivotos:language-sidebar-open','true');
   localStorage.setItem('keivotos:language-sidebar-handle-position','50');
   localStorage.setItem('keivotos:youtube-sidebar-open','false');
   localStorage.setItem('keivotos:youtube-sidebar-handle-position','41');
   window.__languageEntryWidths=[];
   const sample=()=>{const dock=document.querySelector('[data-sidebar-owner="language"]');if(dock)window.__languageEntryWidths.push(dock.getBoundingClientRect().width);if(window.__languageEntryWidths.length<50)requestAnimationFrame(sample);};requestAnimationFrame(sample);
  });
  await nav.goto(config.url);await nav.getByRole('button',{name:'Open Keivotos menu'}).click();
  await nav.locator('.app-drawer').getByRole('button',{name:'Language',exact:true}).click();
  const navSections=nav.getByRole('navigation',{name:'Language sections'}),navDock=nav.locator('[data-sidebar-owner="language"]'),navGrip=navDock.locator('[data-module-sidebar-grip]');
  await navSections.waitFor();await nav.waitForTimeout(350);
  const entry=await nav.evaluate(()=>window.__languageEntryWidths);
  assert(entry.some(width=>width<1)&&entry.some(width=>width>1&&width<255)&&entry.some(width=>width>=255),JSON.stringify(entry));
  await nav.evaluate(()=>{
   window.__languageReversalWidths=[];const deadline=performance.now()+450;
   const sample=()=>{window.__languageReversalWidths.push(document.querySelector('[data-sidebar-owner="language"]').getBoundingClientRect().width);if(performance.now()<deadline)requestAnimationFrame(sample);};requestAnimationFrame(sample);
  });
  await navGrip.click({force:true});await nav.waitForTimeout(340);
  assert((await nav.evaluate(()=>window.__languageReversalWidths)).some(width=>width>0&&width<256));
  await navGrip.click({force:true});await nav.waitForTimeout(340);assert((await navDock.boundingBox()).width>=255);
  await navGrip.click({force:true});await nav.waitForTimeout(65);await navGrip.click({force:true});await nav.waitForTimeout(340);
  assert.equal(await navGrip.getAttribute('aria-expanded'),'true');assert((await navDock.boundingBox()).width>=255);
  await navGrip.click();await nav.waitForTimeout(330);assert(await navDock.locator('.module-sidebar-panel').evaluate(node=>node.inert));
  await navGrip.click();await nav.waitForTimeout(330);
  const navGripBox=await navGrip.boundingBox(),navDockBox=await navDock.boundingBox();
  await nav.mouse.move(navGripBox.x+20,navGripBox.y+32);await nav.mouse.down();await nav.mouse.move(navGripBox.x+20,navDockBox.y+navDockBox.height*.76,{steps:8});await nav.mouse.up();
  assert.equal(await navGrip.getAttribute('aria-expanded'),'true');
  assert(Number(await nav.evaluate(()=>localStorage.getItem('keivotos:language-sidebar-handle-position')))>70);
  await navGrip.focus();await nav.keyboard.press('End');await nav.keyboard.press('ArrowUp');
  assert.equal(await nav.evaluate(()=>localStorage.getItem('keivotos:language-sidebar-handle-position')),'85');
  assert.equal(await nav.evaluate(()=>localStorage.getItem('keivotos:youtube-sidebar-handle-position')),'41');
  await navSections.getByRole('button',{name:'Markdown',exact:true}).focus();
  await nav.mouse.move(1000,120);await nav.waitForTimeout(1850);
  const navVisual=navGrip.locator('.module-sidebar-grip-visual');assert(Number(await navVisual.evaluate(node=>getComputedStyle(node).opacity))<.05);
  await navGrip.hover();await nav.waitForTimeout(200);assert(Number(await navVisual.evaluate(node=>getComputedStyle(node).opacity))>.95);
  await nav.mouse.move(1000,120);await nav.waitForTimeout(220);assert(Number(await navVisual.evaluate(node=>getComputedStyle(node).opacity))<.05);
  await nav.evaluate(()=>document.documentElement.dataset.motion='reduced');
  assert(Number.parseFloat(await navDock.evaluate(node=>getComputedStyle(node).transitionDuration))<.001);
  await nav.evaluate(()=>{delete document.documentElement.dataset.motion;});
  check('Language shared dock slides on entry, reverses rapidly, makes closed content inert, drags without toggling and respects keyboard positions, hover return and reduced motion independently of YouTube');

  await nav.getByRole('tab',{name:navName,exact:true}).click();
  const navEditor=nav.getByRole('textbox',{name:`Edit ${navName}`,exact:true});await navEditor.waitFor();
  await navEditor.press('Control+End');for(let i=0;i<5;i++)await navEditor.press('Shift+ArrowLeft');await navEditor.press('Control+b');
  await nav.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  const navSaved=await editorText(navEditor);assert.notEqual(navSaved,navOriginal);
  assert.equal(navSaved,await nav.evaluate(({sourceId,navName})=>JSON.parse(localStorage.getItem(`keivotos:language-draft:${sourceId}:${navName}`)).content,{sourceId,navName}));
  const retained=await navEditor.evaluate(node=>{window.__retainedEditor=node.closest('.cm-editor');return {selection:window.getSelection().toString(),scroll:node.closest('.cm-editor').querySelector('.cm-scroller').scrollTop};});
  assert(retained.scroll>0);
  await navSections.getByRole('button',{name:'Practice',exact:true}).click();
  const navDecision=nav.getByRole('dialog',{name:'Unsaved Markdown changes'});
  await navDecision.getByRole('button',{name:'Keep editing'}).click();
  assert.equal(await editorText(navEditor),navSaved);assert.equal(fs.readFileSync(path.join(notesA,navName),'utf8'),navOriginal);
  assert.equal(await navSections.getByRole('button',{name:'Markdown',exact:true}).getAttribute('aria-current'),'page');
  await navSections.getByRole('button',{name:'Practice',exact:true}).click();await navDecision.getByRole('button',{name:'Save',exact:true}).click();
  await nav.getByRole('region',{name:'Practice',exact:true}).waitFor();assert.equal(fs.readFileSync(path.join(notesA,navName),'utf8'),navSaved);
  assert(await nav.locator('.markdown-section').evaluate(node=>node.hidden&&node.inert));
  assert(await nav.locator('.cm-editor').evaluate(node=>node===window.__retainedEditor));
  await navSections.getByRole('button',{name:'Markdown',exact:true}).click();await navEditor.waitFor();
  assert.equal(await nav.getByRole('combobox',{name:'Language folder',exact:true}).inputValue(),sourceId);
  assert(await nav.locator('.cm-editor').evaluate(node=>node===window.__retainedEditor));
  const returned=await navEditor.evaluate(node=>({selection:window.getSelection().toString(),scroll:node.closest('.cm-editor').querySelector('.cm-scroller').scrollTop}));
  assert.equal(returned.selection,retained.selection);assert(Math.abs(returned.scroll-retained.scroll)<2);
  await navEditor.press('Control+z');assert.equal(await editorText(navEditor),navOriginal);
  await navEditor.press('Control+y');assert.equal(await editorText(navEditor),navSaved);
  assert.equal(await nav.getByRole('button',{name:`Save ${navName}`,exact:true}).count(),0);
  check('Keep editing stays in Markdown; Save acknowledges the original note before Practice; return retains source, note, same mounted editor, selection, scroll, undo and redo');

  await navEditor.fill('# Discard section draft');
  assert(await nav.evaluate(({sourceId,navName})=>!!localStorage.getItem(`keivotos:language-draft:${sourceId}:${navName}`),{sourceId,navName}));
  await navSections.getByRole('button',{name:'Vocabulary',exact:true}).click();await navDecision.getByRole('button',{name:'Discard changes'}).click();
  await nav.getByRole('heading',{name:'Vocabulary',exact:true}).waitFor();
  await navSections.getByRole('button',{name:'Markdown',exact:true}).click();
  assert.equal(await editorText(navEditor),navSaved);assert.equal(fs.readFileSync(path.join(notesA,navName),'utf8'),navSaved);
  assert.equal(await nav.evaluate(({sourceId,navName})=>localStorage.getItem(`keivotos:language-draft:${sourceId}:${navName}`),{sourceId,navName}),null);
  assert(await nav.locator('.cm-editor').evaluate(node=>node===window.__retainedEditor));
  await navEditor.fill('# Section save rejected');
  const rejectedSave=route=>route.request().method()==='PUT'?route.fulfill({status:503,json:{detail:'Generated section save failure'}}):route.continue();
  await nav.route('**/api/language/document',rejectedSave);
  await navSections.getByRole('button',{name:'Practice',exact:true}).click();await navDecision.getByRole('button',{name:'Save',exact:true}).click();
  await nav.getByText('Generated section save failure',{exact:true}).waitFor();
  assert.equal(await navSections.getByRole('button',{name:'Markdown',exact:true}).getAttribute('aria-current'),'page');
  assert.equal(await editorText(navEditor),'# Section save rejected');assert.equal(fs.readFileSync(path.join(notesA,navName),'utf8'),navSaved);
  await nav.unroute('**/api/language/document',rejectedSave);
  await navSections.getByRole('button',{name:'Vocabulary',exact:true}).click();await navDecision.getByRole('button',{name:'Discard changes'}).click();
  await navSections.getByRole('button',{name:'Markdown',exact:true}).click();assert.equal(await editorText(navEditor),navSaved);
  check('Discard restores saved text in the retained editor and clears only its draft; failed section Save keeps Markdown and recoverable text without altering original bytes');

  let releaseSectionRead,startSectionRead,sectionReadDone;
  const sectionReadHeld=new Promise(resolve=>releaseSectionRead=resolve),sectionReadStarted=new Promise(resolve=>startSectionRead=resolve),sectionReadCompleted=new Promise(resolve=>sectionReadDone=resolve);
  const lateSectionRead=async route=>{
   if(route.request().method()!=='GET'||new URL(route.request().url()).searchParams.get('name')!==lateName)return route.continue();
   const response=await route.fetch();assert(response.ok());startSectionRead();await sectionReadHeld;await route.fulfill({response});sectionReadDone();
  };
  await nav.route('**/api/language/document?*',lateSectionRead);
  await nav.getByRole('tab',{name:lateName,exact:true}).click();await sectionReadStarted;
  await navSections.getByRole('button',{name:'Practice',exact:true}).click();await nav.getByRole('region',{name:'Practice',exact:true}).waitFor();
  releaseSectionRead();await sectionReadCompleted;await nav.waitForTimeout(80);
  await navSections.getByRole('button',{name:'Vocabulary',exact:true}).click();await navSections.getByRole('button',{name:'Markdown',exact:true}).click();
  assert.equal(await editorText(navEditor),navSaved);assert.equal(await nav.getByRole('textbox',{name:`Edit ${lateName}`,exact:true}).count(),0);
  assert.equal(fs.readFileSync(path.join(notesA,lateName),'utf8'),'Delayed section note');await nav.unroute('**/api/language/document?*',lateSectionRead);
  check('a held note read cannot replace the retained note after a section change, including returning through Vocabulary');

  for(const viewport of [{width:1280,height:140},{width:360,height:700}]){
   await nav.setViewportSize(viewport);
   if(viewport.height===140){assert(await navSections.evaluate(node=>node.scrollHeight>node.clientHeight));await navSections.hover();await nav.mouse.wheel(0,160);await nav.waitForTimeout(100);assert(await navSections.evaluate(node=>node.scrollTop>0));}
   const bounds=await navEditor.boundingBox();assert(bounds.x>=0&&bounds.x+bounds.width<=viewport.width);
  }
  await nav.screenshot({path:path.join(config.output,'language-study-navigation-compact.png')});
  await nav.setViewportSize({width:1280,height:850});await navGrip.click();await nav.waitForTimeout(330);
  const reloadContext=await browser.newContext({viewport:{width:1280,height:850},storageState:await context.storageState()});
  const restoredNav=await reloadContext.newPage();restoredNav.on('pageerror',error=>report.errors.push(error.message));
  await restoredNav.goto(config.url);await restoredNav.getByRole('button',{name:'Open Keivotos menu'}).click();await restoredNav.locator('.app-drawer').getByRole('button',{name:'Language',exact:true}).click();
  const restoredGrip=restoredNav.locator('[data-sidebar-owner="language"] [data-module-sidebar-grip]');await restoredGrip.waitFor();
  assert.equal(await restoredGrip.getAttribute('aria-expanded'),'false');
  assert.equal(await restoredNav.evaluate(()=>localStorage.getItem('keivotos:language-sidebar-handle-position')),'85');
  assert.equal(await restoredNav.evaluate(()=>localStorage.getItem('keivotos:youtube-sidebar-handle-position')),'41');
  await reloadContext.close();await navGrip.click();await nav.waitForTimeout(330);
  await nav.screenshot({path:path.join(config.output,'language-study-navigation.png')});
  check('Language sections scroll in short windows, editor fits a narrow window, and a fresh browser session restores Language dock preferences independently of YouTube');

  await navSections.getByRole('button',{name:'Practice',exact:true}).click();
  const practice=nav.getByRole('region',{name:'Practice',exact:true});
  const category=practice.getByRole('combobox',{name:'Category',exact:true});
  const direction=practice.getByRole('combobox',{name:'Answer in',exact:true});
  const from=practice.getByRole('spinbutton',{name:'From',exact:true}),to=practice.getByRole('spinbutton',{name:'To',exact:true});
  const prompt=practice.locator('.practice-prompt'),feedback=practice.locator('.practice-feedback');
  const practiceCheck=practice.getByRole('button',{name:'Check',exact:true}),practiceNext=practice.getByRole('button',{name:'Next',exact:true});
  const answerBox=()=>practice.locator('.practice-answer');
  async function practiceApi(){return nav.evaluate(async()=>{const r=await fetch('/api/language/study/preferences');return {status:r.status,data:await r.json()};});}
  async function choosePractice(id,language,value){
   await category.selectOption(id);await direction.selectOption(language);
   await from.fill(String(value));await from.press('Tab');await to.fill(String(value));await to.press('Tab');
   await nav.waitForFunction(({id,language,value})=>{const p=document.querySelector('.practice-card');return p&&p.querySelector('select').value===id&&p.querySelectorAll('select')[1].value===language&&p.querySelectorAll('input[type=number]')[0].value===String(value)&&p.querySelectorAll('input[type=number]')[1].value===String(value);},{id,language,value});
  }
  async function savedPractice(expected){
   for(let attempt=0;attempt<50;attempt++){
    const r=await practiceApi();if(r.status===200&&JSON.stringify(r.data)===JSON.stringify(expected))return;
    await nav.waitForTimeout(50);
   }
   assert.deepEqual((await practiceApi()).data,expected);
  }
  await category.waitFor();await nav.waitForFunction(()=>!document.querySelector('.practice-card select').disabled);
  await choosePractice('sino','english',17);assert.equal(await prompt.textContent(),'십칠');
  await answerBox().fill('17');await answerBox().press('Enter');assert.match(await feedback.textContent(),/^Correct/);
  await nav.keyboard.press('Enter');assert.equal(await prompt.textContent(),'십칠');assert.match(await feedback.textContent(),/^Correct/);
  await practiceNext.click();assert.equal(await answerBox().inputValue(),'');assert.equal(await feedback.textContent(),'');
  assert(await answerBox().evaluate(node=>node===document.activeElement));
  await answerBox().fill('Seventeen');await practiceCheck.click();assert.match(await feedback.textContent(),/^Correct/);
  check('centered practice card accepts English digits and words; Enter checks once and Next explicitly clears feedback and focuses the answer');

  const koreanExamples=[['sino',16,'십육','심뉵'],['native',20,'스물','스무'],['months',6,'유월','육월'],['months',10,'시월','십월'],['dates',31,'삼십일일','삼십일'],['hours',11,'열한 시','열하나 시'],['minutes',59,'오십구 분','쉰아홉 분']];
  for(const [id,value,written,wrong] of koreanExamples){
   await choosePractice(id,'korean',value);await answerBox().fill(wrong);await practiceCheck.click();
   assert.match(await feedback.textContent(),/^Not quite/);assert((await feedback.textContent()).includes(written));
   await practiceNext.click();await answerBox().fill(written);await practiceCheck.click();assert.match(await feedback.textContent(),/^Correct/);
  }
  check('all six categories use canonical written Korean and reject wrong number systems, pronunciation spellings, month forms and uncontracted hours');

  await choosePractice('hours','korean',3);await answerBox().dispatchEvent('compositionstart');await answerBox().fill('세시');
  await answerBox().press('Enter');assert.equal(await feedback.textContent(),'');
  await answerBox().dispatchEvent('keydown',{key:'Enter',code:'Enter',keyCode:229,isComposing:true});assert.equal(await feedback.textContent(),'');
  await answerBox().dispatchEvent('compositionend');assert.equal(await feedback.textContent(),'');
  await answerBox().press('Enter');assert.match(await feedback.textContent(),/^Correct/);
  await practiceNext.click();await direction.selectOption('english');assert.equal(await prompt.textContent(),'세 시');
  await answerBox().fill("three o'clock");await answerBox().press('Enter');assert.match(await feedback.textContent(),/^Correct/);
  await from.fill('13');await from.press('Tab');await practice.getByRole('alert').waitFor();assert(await practiceCheck.isDisabled());
  await from.fill('3');await from.press('Tab');assert.equal(await practice.getByRole('alert').count(),0);
  check('composing Hangul and keyCode 229 never grade an unfinished answer; a later Enter grades once; category-specific unit spacing and range guards work');

  const savedChoice={category:'hours',answer_language:'english',minimum:3,maximum:3};await savedPractice(savedChoice);
  const restartContext=await browser.newContext({viewport:{width:1280,height:850}});
  const restartPage=await restartContext.newPage();restartPage.on('pageerror',error=>report.errors.push(error.message));
  await restartPage.goto(config.url);await restartPage.getByRole('button',{name:'Open Keivotos menu'}).click();
  await restartPage.locator('.app-drawer').getByRole('button',{name:'Language',exact:true}).click();
  await restartPage.getByRole('navigation',{name:'Language sections'}).getByRole('button',{name:'Practice',exact:true}).click();
  await restartPage.waitForFunction(()=>document.querySelector('.practice-card select')?.value==='hours');
  assert.equal(await restartPage.locator('.practice-card select').nth(1).inputValue(),'english');
  assert.equal(await restartPage.locator('.practice-card input[type=number]').first().inputValue(),'3');
  assert.equal(await restartPage.locator('.practice-prompt').textContent(),'세 시');await restartContext.close();
  await answerBox().fill('three');await context.setOffline(true);await answerBox().press('Enter');assert.match(await feedback.textContent(),/^Correct/);
  await practiceNext.click();await answerBox().fill('3:00');await practiceCheck.click();assert.match(await feedback.textContent(),/^Correct/);await context.setOffline(false);
  check('a fresh browser context without local storage restores SQLite practice choices; checking and Next work offline without network grading');

  let failedChoiceWrites=0;
  const failChoices=async route=>{if(route.request().method()==='PUT'){failedChoiceWrites++;await route.fulfill({status:503,json:{detail:'Generated practice settings failure'}});}else await route.continue();};
  await nav.route('**/api/language/study/preferences',failChoices);
  await choosePractice('minutes','english',25);await answerBox().fill('twenty-five minutes');
  await practice.getByText('These choices could not be saved. Practice still works; retry to keep them after restart.',{exact:true}).waitFor();
  const failedPrompt=await prompt.textContent();await practiceCheck.click();assert.match(await feedback.textContent(),/^Correct/);
  assert.equal(await answerBox().inputValue(),'twenty-five minutes');assert.equal(await prompt.textContent(),failedPrompt);assert(failedChoiceWrites>0);
  await nav.unroute('**/api/language/study/preferences',failChoices);await practice.getByRole('button',{name:'Retry settings'}).click();
  await savedPractice({category:'minutes',answer_language:'english',minimum:25,maximum:25});
  assert.equal(await practice.getByRole('button',{name:'Retry settings'}).count(),0);assert.match(await feedback.textContent(),/^Correct/);
  check('failed settings writes retain the current prompt, answer and feedback; explicit retry saves the current choices without repeating or discarding practice');

  let releaseChoices,startedChoices;const choicesHeld=new Promise(resolve=>releaseChoices=resolve),choicesStarted=new Promise(resolve=>startedChoices=resolve);let heldChoiceOnce=false;
  const holdChoices=async route=>{
   if(route.request().method()!=='PUT'||heldChoiceOnce)return route.continue();
   heldChoiceOnce=true;const response=await route.fetch();startedChoices();await choicesHeld;await route.fulfill({response});
  };
  await nav.route('**/api/language/study/preferences',holdChoices);
  await category.selectOption('months');await choicesStarted;await choosePractice('native','english',17);await answerBox().fill('seventeen');
  await navSections.getByRole('button',{name:'Vocabulary',exact:true}).click();releaseChoices();
  await savedPractice({category:'native',answer_language:'english',minimum:17,maximum:17});
  await navSections.getByRole('button',{name:'Practice',exact:true}).click();assert.equal(await prompt.textContent(),'열일곱');assert.equal(await answerBox().inputValue(),'seventeen');
  await nav.unroute('**/api/language/study/preferences',holdChoices);await practiceCheck.click();assert.match(await feedback.textContent(),/^Correct/);
  check('rapid category/direction/range changes serialize and coalesce pending saves; a delayed older acknowledgement cannot reset newer practice or hidden-section text');

  for(const viewport of [{width:360,height:700},{width:1280,height:260}]){
   await nav.setViewportSize(viewport);const card=await practice.locator('.practice-card').boundingBox();assert(card.width<=viewport.width&&card.x>=0&&card.x+card.width<=viewport.width);
   await practiceNext.scrollIntoViewIfNeeded();const button=await practiceNext.boundingBox();assert(button.y>=0&&button.y+button.height<=viewport.height);
  }
  await nav.setViewportSize({width:1280,height:850});await practice.locator('.practice-card').scrollIntoViewIfNeeded();
  await nav.screenshot({path:path.join(config.output,'language-korean-practice.png')});
  check('practice card fits narrow windows and scrolls inside short windows with controls reachable');

  {
  // Vocabulary mutations use only the runner's generated isolated user database.
  const vocabularyApi=async(url,method='GET',body)=>nav.evaluate(async({url,method,body})=>{const response=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});return {status:response.status,data:await response.json()};},{url,method,body});
  nav.on('request',request=>{if(new URL(request.url()).origin!==config.url)report.errors.push('External vocabulary request '+request.url());});
  await navSections.getByRole('button',{name:'Vocabulary',exact:true}).click();
  const vocabulary=nav.getByRole('region',{name:'Vocabulary',exact:true});
  const addVocabulary=vocabulary.getByRole('button',{name:'New vocabulary entry',exact:true});
  const entryPanel=nav.getByRole('dialog',{name:'New vocabulary',exact:true});
  const word=entryPanel.getByRole('textbox',{name:'Korean word',exact:true});
  const pronunciation=entryPanel.getByRole('textbox',{name:'Pronunciation',exact:true});
  const translation=entryPanel.getByRole('textbox',{name:'English translation',exact:true});
  const vocabularyDecision=nav.getByRole('dialog',{name:'Unsaved vocabulary',exact:true});
  const vocabularyList=async()=>vocabularyApi('/api/language/vocabulary?limit=100');
  async function openEntry(){await addVocabulary.click();await word.waitFor();await nav.waitForTimeout(320);const focus=await word.evaluate(node=>({focused:node===document.activeElement,active:document.activeElement?.outerHTML.slice(0,250),visibility:getComputedStyle(node).visibility,disabled:node.disabled,inert:node.closest('.vocabulary-entry-drawer').inert}));assert.equal(focus.focused,true,JSON.stringify(focus));}
  async function closeEmptyEntry(){await entryPanel.getByRole('button',{name:'Close new vocabulary',exact:true}).click();await nav.waitForTimeout(320);assert.equal(await addVocabulary.evaluate(node=>node===document.activeElement),true);}
  async function discardEntry(){await vocabularyDecision.getByRole('button',{name:'Discard',exact:true}).click();await nav.waitForTimeout(320);}
  const initialVocabulary=(await vocabularyList()).data.total;
  await vocabulary.getByRole('cell',{name:'Without a notes folder',exact:true}).waitFor();
  const plusBox=await addVocabulary.boundingBox(),plusSvg=await addVocabulary.locator('svg').boundingBox();
  assert(Math.abs(plusBox.x+plusBox.width/2-plusSvg.x-plusSvg.width/2)<1);
  assert(Math.abs(plusBox.y+plusBox.height/2-plusSvg.y-plusSvg.height/2)<1);
  const motion=await nav.evaluate(async()=>{
   document.querySelector('.vocabulary-view .add').click();
   await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
   const panel=document.querySelector('.vocabulary-entry-drawer');const start=panel.getBoundingClientRect().x;
   await new Promise(resolve=>setTimeout(resolve,340));return {start,end:panel.getBoundingClientRect().x};
  });
  assert(motion.start>motion.end+20,JSON.stringify(motion));assert.equal(await word.evaluate(node=>node===document.activeElement),true);
  const fittedDrawer=await entryPanel.evaluate(node=>{
   const panel=node.getBoundingClientRect(),save=node.querySelector('button[type="submit"]').getBoundingClientRect();
   return {bottomSpace:panel.bottom-save.bottom,scrollHeight:node.scrollHeight,clientHeight:node.clientHeight};
  });
  assert(fittedDrawer.bottomSpace>=20&&fittedDrawer.bottomSpace<=30,JSON.stringify(fittedDrawer));
  assert(fittedDrawer.scrollHeight<=fittedDrawer.clientHeight+1,JSON.stringify(fittedDrawer));
  await word.fill('');await translation.fill('');assert(await entryPanel.getByRole('button',{name:'Save',exact:true}).isDisabled());
  await word.fill('   ');await translation.fill('translation');assert(await entryPanel.getByRole('button',{name:'Save',exact:true}).isDisabled());
  await word.fill('한'.repeat(257));assert(await entryPanel.getByRole('button',{name:'Save',exact:true}).isDisabled());
  await word.fill('단어');await pronunciation.fill('🙂'.repeat(256));assert(!await entryPanel.getByRole('button',{name:'Save',exact:true}).isDisabled());
  await pronunciation.fill('p'.repeat(257));assert(await entryPanel.getByRole('button',{name:'Save',exact:true}).isDisabled());
  await pronunciation.fill('');await translation.fill('e'.repeat(2001));assert(await entryPanel.getByRole('button',{name:'Save',exact:true}).isDisabled());
  check('Vocabulary works without a notes folder; centered + and real right-to-left drawer motion; panel fits fields and Save without unused bottom space; required fields and Unicode character limits agree with API');

  const exactWord='  한글'.normalize('NFD')+' 단어  ',exactPronunciation=' han-geul\t단어 ',exactEnglish='  Plain <script>window.__vocabularyUnsafe=true</script>\n![image](https://example.invalid/vocabulary.png)  ';
  await word.fill(exactWord);await pronunciation.fill(exactPronunciation);await translation.fill(exactEnglish);
  let compositionSaves=0;const countComposition=request=>{if(request.method()==='POST'&&new URL(request.url()).pathname==='/api/language/vocabulary')compositionSaves++;};nav.on('request',countComposition);
  await word.evaluate(node=>{node.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true}));node.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',isComposing:true,bubbles:true,cancelable:true}));node.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',keyCode:229,bubbles:true,cancelable:true}));node.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true,data:'단어'}));});
  await nav.waitForTimeout(80);assert.equal(compositionSaves,0);nav.off('request',countComposition);
  await entryPanel.getByRole('button',{name:'Save',exact:true}).focus();await nav.keyboard.press('Tab');
  assert.equal(await entryPanel.getByRole('button',{name:'Close new vocabulary',exact:true}).evaluate(node=>document.activeElement===node),true);
  await nav.keyboard.press('Shift+Tab');assert.equal(await entryPanel.getByRole('button',{name:'Save',exact:true}).evaluate(node=>document.activeElement===node),true);
  await entryPanel.getByRole('button',{name:'Save',exact:true}).click();
  await nav.waitForTimeout(340);await vocabulary.getByRole('cell',{name:exactWord.trim(),exact:true}).waitFor();
  const entry=(await vocabularyList()).data.items[0];assert.equal(entry.korean,exactWord);assert.equal(entry.pronunciation,exactPronunciation);assert.equal(entry.english,exactEnglish);
  const exactRow=vocabulary.locator(`[data-vocabulary-id="${entry.id}"]`);
  assert.deepEqual(await exactRow.locator('td').allTextContents(),[exactWord,exactPronunciation,exactEnglish]);
  assert.equal(await nav.evaluate(()=>window.__vocabularyUnsafe),undefined);assert.equal(await exactRow.locator('img,script').count(),0);
  assert.equal(await addVocabulary.evaluate(node=>node===document.activeElement),true);
  check('manual fields round-trip as exact inert Unicode/whitespace; IME Enter never saves unfinished text; drawer focus traps and acknowledged Save adds a table row and returns to +');

  await openEntry();await word.fill('남겨 둘 단어');await translation.fill('Keep this entry');
  await word.press('Escape');await vocabularyDecision.waitFor();await vocabularyDecision.getByRole('button',{name:'Keep editing',exact:true}).click();
  assert.equal(await word.inputValue(),'남겨 둘 단어');assert.equal(await translation.inputValue(),'Keep this entry');assert.equal(await word.evaluate(node=>document.activeElement===node),true);
  await navSections.getByRole('button',{name:'Practice',exact:true}).click();await vocabularyDecision.waitFor();await vocabularyDecision.press('Escape');
  assert.equal(await navSections.getByRole('button',{name:'Vocabulary',exact:true}).getAttribute('aria-current'),'page');
  await nav.locator('.vocabulary-shade').click({position:{x:12,y:12}});await vocabularyDecision.waitFor();await discardEntry();
  await openEntry();assert.equal(await word.inputValue(),'');await closeEmptyEntry();
  assert.equal((await vocabularyList()).data.total,initialVocabulary+1);
  check('dirty Escape, outside click and section navigation offer Keep editing/Discard; Keep preserves all fields/focus and Discard leaves stored entries unchanged');

  await openEntry();await word.fill('모듈 이동');await translation.fill('Module navigation');
  await nav.getByRole('button',{name:'Open Keivotos menu'}).click();await nav.locator('.app-drawer').getByRole('button',{name:'Files',exact:true}).click();
  await vocabularyDecision.waitFor();await vocabularyDecision.getByRole('button',{name:'Keep editing',exact:true}).click();
  assert.equal(await word.inputValue(),'모듈 이동');await nav.getByRole('button',{name:'Open Keivotos menu'}).click();await nav.locator('.app-drawer').getByRole('button',{name:'Files',exact:true}).click();await vocabularyDecision.waitFor();await discardEntry();
  await nav.locator('.language-surface').waitFor({state:'detached'});await nav.waitForTimeout(220);
  await nav.getByRole('button',{name:'Open Keivotos menu'}).click();await nav.locator('.app-drawer').getByRole('button',{name:'Language',exact:true}).click();
  await nav.getByRole('navigation',{name:'Language sections'}).getByRole('button',{name:'Vocabulary',exact:true}).click();await vocabulary.getByRole('cell',{name:exactWord.trim(),exact:true}).waitFor();
  check('suite module navigation uses the vocabulary dirty guard; confirmed discard changes modules and saved rows remain on return');

  await openEntry();await word.fill('실패 뒤 재시도');await pronunciation.fill('manual');await translation.fill('Retry after failure');
  const failEntry=async route=>route.request().method()==='POST'?route.fulfill({status:503,json:{detail:'Generated vocabulary save failure'}}):route.continue();
  await nav.route('**/api/language/vocabulary',failEntry);await entryPanel.getByRole('button',{name:'Save',exact:true}).click();await entryPanel.getByRole('alert').waitFor();
  assert.equal(await word.inputValue(),'실패 뒤 재시도');assert.equal(await pronunciation.inputValue(),'manual');assert.equal(await translation.inputValue(),'Retry after failure');
  assert.equal(await word.evaluate(node=>node.readOnly),true);assert.equal((await vocabularyList()).data.total,initialVocabulary+1);
  await nav.unroute('**/api/language/vocabulary',failEntry);await entryPanel.getByRole('button',{name:'Retry save',exact:true}).click();await vocabulary.getByRole('cell',{name:'Retry after failure',exact:true}).waitFor();await nav.waitForTimeout(320);
  check('failed Save retains all submitted fields and protects the submitted text; explicit Retry save acknowledges one entry');

  await openEntry();await word.fill('응답 손실');await translation.fill('Lost acknowledgement');
  const retryPayloads=[];let loseOnce=true;
  const loseAck=async route=>{
   if(route.request().method()!=='POST')return route.continue();retryPayloads.push(route.request().postDataJSON());
   const response=await route.fetch();assert(response.ok());if(loseOnce){loseOnce=false;await route.abort('failed');}else await route.fulfill({response});
  };
  await nav.route('**/api/language/vocabulary',loseAck);await entryPanel.getByRole('button',{name:'Save',exact:true}).click();await entryPanel.getByRole('alert').waitFor();
  const committed=(await vocabularyList()).data.items.find(item=>item.english==='Lost acknowledgement');assert(committed);
  await entryPanel.getByRole('button',{name:'Retry save',exact:true}).click();await vocabulary.getByRole('cell',{name:'Lost acknowledgement',exact:true}).waitFor();await nav.waitForTimeout(320);
  assert.equal(retryPayloads.length,2);assert.deepEqual(retryPayloads[0],retryPayloads[1]);assert.equal((await vocabularyList()).data.items.filter(item=>item.english==='Lost acknowledgement').length,1);
  assert.equal(await vocabulary.locator(`[data-vocabulary-id="${committed.id}"]`).count(),1);await nav.unroute('**/api/language/vocabulary',loseAck);
  check('a lost committed response preserves the entry; Retry sends the exact same UUID/fields and adds only the original stored row');

  let releaseSave,reachedSave;const heldSave=new Promise(resolve=>releaseSave=resolve),saveReached=new Promise(resolve=>reachedSave=resolve);let pendingSaveCount=0;
  const holdEntry=async route=>{
   if(route.request().method()!=='POST')return route.continue();pendingSaveCount++;const response=await route.fetch();reachedSave();await heldSave;await route.fulfill({response});
  };
  await nav.route('**/api/language/vocabulary',holdEntry);await openEntry();await word.fill('기다리는 저장');await translation.fill('Pending save');await entryPanel.getByRole('button',{name:'Save',exact:true}).click();await saveReached;
  assert(await word.isDisabled());assert(await entryPanel.getByRole('button',{name:'Saving…',exact:true}).isDisabled());assert(await entryPanel.getByRole('button',{name:'Close new vocabulary',exact:true}).isDisabled());
  await navSections.getByRole('button',{name:'Practice',exact:true}).click();await nav.waitForTimeout(80);assert.equal(await navSections.getByRole('button',{name:'Vocabulary',exact:true}).getAttribute('aria-current'),'page');
  releaseSave();await vocabulary.getByRole('cell',{name:'Pending save',exact:true}).waitFor();await nav.waitForTimeout(320);assert.equal(pendingSaveCount,1);await nav.unroute('**/api/language/vocabulary',holdEntry);
  check('pending Save blocks duplicate requests, field changes, closing and navigation until its actual acknowledgement');

  // A late list response must not erase a newly acknowledged entry.
  let releaseList,reachedList;const heldList=new Promise(resolve=>releaseList=resolve),listReached=new Promise(resolve=>reachedList=resolve);let holdListOnce=true;
  const staleList=async route=>{
   if(route.request().method()!=='GET'||!holdListOnce)return route.continue();holdListOnce=false;
   const response=await route.fetch();reachedList();await heldList;await route.fulfill({response}).catch(()=>{});
  };
  await nav.route('**/api/language/vocabulary?*',staleList);await vocabulary.getByRole('button',{name:'Refresh vocabulary',exact:true}).click();await listReached;
  await openEntry();await word.fill('늦은 목록');await translation.fill('Newer than delayed list');await entryPanel.getByRole('button',{name:'Save',exact:true}).click();await vocabulary.getByRole('cell',{name:'Newer than delayed list',exact:true}).waitFor();
  releaseList();await nav.waitForTimeout(340);assert.equal(await vocabulary.getByRole('cell',{name:'Newer than delayed list',exact:true}).count(),1);await nav.unroute('**/api/language/vocabulary?*',staleList);
  check('stale list completion cannot overwrite a newly acknowledged table row');

  for(let i=0;i<53;i++)assert.equal((await vocabularyApi('/api/language/vocabulary','POST',{request_id:crypto.randomUUID(),korean:i===0?'긴 단어'.repeat(50):`생성 단어 ${i}`,pronunciation:i===0?'manual '.repeat(35):'',english:i===0?'Long translation '.repeat(90):`Generated translation ${i}`})).status,200);
  await vocabulary.getByRole('button',{name:'Refresh vocabulary',exact:true}).click();await vocabulary.getByRole('cell',{name:'Generated translation 52',exact:true}).waitFor();
  assert.equal(await vocabulary.locator('tbody tr').count(),50);await vocabulary.getByRole('button',{name:'Load more vocabulary',exact:true}).click();
  const totalVocabulary=(await vocabularyList()).data.total;
  await nav.waitForFunction(total=>document.querySelectorAll('.vocabulary-view tbody tr').length===total,totalVocabulary);
  const rowIds=await vocabulary.locator('tbody tr').evaluateAll(nodes=>nodes.map(node=>node.dataset.vocabularyId));assert.equal(new Set(rowIds).size,totalVocabulary);
  const failList=async route=>route.fulfill({status:503,json:{detail:'Generated vocabulary listing failure'}});
  await nav.route('**/api/language/vocabulary?*',failList);await vocabulary.getByRole('button',{name:'Refresh vocabulary',exact:true}).click();await vocabulary.getByRole('alert').waitFor();assert.equal(await vocabulary.locator('tbody tr').count(),totalVocabulary);
  await nav.unroute('**/api/language/vocabulary?*',failList);await vocabulary.getByRole('button',{name:'Retry vocabulary',exact:true}).click();await nav.waitForFunction(()=>!document.querySelector('.vocabulary-view [role=alert]'));
  assert.equal(await vocabulary.locator('tbody tr').count(),totalVocabulary);
  check('paginated table loads all saved rows without duplicate IDs; failed refresh retains rows and Retry restores the current loaded prefix');

  for(const viewport of [{width:360,height:700},{width:1280,height:260}]){
   await nav.setViewportSize(viewport);if(viewport.width<700&&await navGrip.getAttribute('aria-expanded')==='true'){await navGrip.click();await nav.waitForTimeout(330);}await openEntry();await word.fill('화면');await translation.fill('Viewport');
   await entryPanel.getByRole('button',{name:'Save',exact:true}).scrollIntoViewIfNeeded();const panelBox=await entryPanel.boundingBox(),saveBox=await entryPanel.getByRole('button',{name:'Save',exact:true}).boundingBox();
   assert(panelBox.x>=0&&panelBox.x+panelBox.width<=viewport.width);assert(saveBox.y>=0&&saveBox.y+saveBox.height<=viewport.height);
   await entryPanel.getByRole('button',{name:'Close new vocabulary',exact:true}).scrollIntoViewIfNeeded();await entryPanel.getByRole('button',{name:'Close new vocabulary',exact:true}).click();await vocabularyDecision.waitFor();await discardEntry();
   assert.equal(await nav.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  }
  await nav.setViewportSize({width:1280,height:850});
  await nav.emulateMedia({reducedMotion:'reduce'});await openEntry();assert(Number.parseFloat(await entryPanel.evaluate(node=>getComputedStyle(node).transitionDuration))<.001);await closeEmptyEntry();await nav.emulateMedia({reducedMotion:'no-preference'});
  await nav.evaluate(()=>document.documentElement.dataset.motion='reduced');await openEntry();assert(Number.parseFloat(await entryPanel.evaluate(node=>getComputedStyle(node).transitionDuration))<.001);await closeEmptyEntry();await nav.evaluate(()=>delete document.documentElement.dataset.motion);
  await openEntry();await entryPanel.getByRole('button',{name:'Close new vocabulary',exact:true}).click();await nav.waitForTimeout(60);await addVocabulary.click();await nav.waitForTimeout(340);assert.equal(await entryPanel.evaluate(node=>Math.abs(new DOMMatrixReadOnly(getComputedStyle(node).transform).m41)<1),true);await closeEmptyEntry();
  await nav.screenshot({path:path.join(config.output,'language-vocabulary-table.png')});await openEntry();await nav.screenshot({path:path.join(config.output,'language-vocabulary-drawer.png')});await closeEmptyEntry();
  check('compact table and drawer fit narrow/short windows; save stays reachable; system/app reduced motion and rapid drawer reversals settle correctly');

  const vocabularyFresh=await context.newPage();vocabularyFresh.on('pageerror',error=>report.errors.push(error.message));await vocabularyFresh.goto(config.url);
  await vocabularyFresh.getByRole('button',{name:'Open Keivotos menu'}).click();await vocabularyFresh.locator('.app-drawer').getByRole('button',{name:'Language',exact:true}).click();
  const freshGrip=vocabularyFresh.locator('[data-sidebar-owner="language"] [data-module-sidebar-grip]');
  if(await freshGrip.getAttribute('aria-expanded')==='false'){await freshGrip.click();await vocabularyFresh.waitForTimeout(330);}
  await vocabularyFresh.getByRole('navigation',{name:'Language sections'}).getByRole('button',{name:'Vocabulary',exact:true}).click();
  await vocabularyFresh.getByRole('cell',{name:'Generated translation 52',exact:true}).waitFor();await vocabularyFresh.close();
  assert.equal((await vocabularyApi('/api/suite/modules/language/disable','POST')).status,200);assert.equal((await vocabularyList()).status,409);
  assert.equal((await vocabularyApi('/api/suite/modules/language/enable','POST')).status,200);assert.equal((await vocabularyList()).data.total,totalVocabulary);
  assert.equal(fs.readFileSync(path.join(notesA,navName),'utf8'),navSaved);await nav.close({runBeforeUnload:false});
  check('fresh page reload and disable/re-enable retain saved vocabulary; original Markdown bytes remain unchanged');
  }
  assert.deepEqual(report.errors,[]);
 }finally{
  fs.writeFileSync(path.join(config.output,'report.json'),JSON.stringify(report,null,2));
  await context.tracing.stop({path:path.join(config.output,'trace.zip')});await browser.close();
 }
})().catch(error=>{console.error(error);process.exitCode=1;});

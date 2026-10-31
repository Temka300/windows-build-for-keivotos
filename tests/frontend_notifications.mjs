import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {build} from '../frontend/node_modules/vite/dist/node/index.js';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const result=await build({root:path.join(root,'frontend'),configFile:false,logLevel:'silent',
  build:{write:false,minify:false,lib:{entry:path.join(root,'frontend/src/lib/notifications.ts'),formats:['es']}}});
const chunk=(Array.isArray(result)?result[0]:result).output.find(item=>item.type==='chunk');
const temporary=await fs.mkdtemp(path.join(os.tmpdir(),'keivotos-notification-contract-'));
const realNow=Date.now,realSetTimeout=globalThis.setTimeout,realClearTimeout=globalThis.clearTimeout;
let now=10000,sequence=0,items=[],timers=new Map(),unsubscribe;
function advance(ms){
  const end=now+ms;
  while(true){
    const due=[...timers].filter(([,timer])=>timer.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];
    if(!due)break;
    now=due[1].at;timers.delete(due[0]);due[1].run();
  }
  now=end;
}
try{
  const file=path.join(temporary,'notifications.mjs');await fs.writeFile(file,chunk.code);
  Date.now=()=>now;
  globalThis.setTimeout=(run,delay)=>{const id=++sequence;timers.set(id,{at:now+delay,run});return id;};
  globalThis.clearTimeout=id=>timers.delete(id);
  const {notifications,notify,updateNotification,dismissNotification,pauseNotification,resumeNotification}=await import(pathToFileURL(file).href);
  unsubscribe=notifications.subscribe(value=>items=value);
  const id=notify('Downloading',{id:'download',destination:'downloads',progress:10});
  assert.equal(items[0].destination,'downloads');
  advance(1000);pauseNotification(id,'pointer');pauseNotification(id,'pointer');
  advance(10000);
  updateNotification(id,{message:'Downloading more',tone:'info',progress:30});
  assert.equal(items[0].destination,'downloads');assert.equal(items[0].progress,30);
  pauseNotification(id,'focus');resumeNotification(id,'pointer');advance(10000);assert.equal(items.length,1);
  resumeNotification(id,'focus');advance(4999);assert.equal(items.length,1);advance(1);assert.equal(items.length,0);
  updateNotification(id,{message:'Late update',tone:'success',destination:'downloads'});assert.equal(items.length,0);
  console.log('PASS: independent hover/focus holds, exact remaining expiry and destination-preserving progress without revival');
  notify('Start',{id,destination:'downloads'});advance(5000);
  updateNotification(id,{message:'Still downloading',tone:'info',progress:80});advance(1000);assert.equal(items.length,0);
  for(let index=0;index<6;index++)notify('Notice '+index,{id:'notice-'+index,destination:index%2?'downloads':undefined});
  assert.deepEqual(items.map(item=>item.id),['notice-1','notice-2','notice-3','notice-4','notice-5']);
  assert.equal(timers.size,5);
  pauseNotification('notice-1','pointer');dismissNotification('notice-1');
  updateNotification('notice-1',{message:'Late',tone:'info',destination:'downloads'});resumeNotification('notice-1','pointer');
  assert.equal(items.length,4);assert.equal(timers.size,4);
  notify('Ordinary replacement',{id:'notice-3'});assert.equal(items.find(item=>item.id==='notice-3').destination,undefined);
  notify('Unsupported destination',{id:'unsupported',destination:'https://example.invalid'});
  assert.equal(items.find(item=>item.id==='unsupported').destination,undefined);
  advance(6000);assert.equal(items.length,0);assert.equal(timers.size,0);
  console.log('PASS: five-notice bound, dismissal, ordinary replacements, unsupported destinations and unchanged expiry');
  const metadata=notify('Refreshing metadata…',{id:'music',pending:true,progress:0});
  advance(60000);assert.equal(items.length,1);assert.equal(timers.size,0);
  updateNotification(metadata,{message:'Refreshing metadata… 39/50 tracks',tone:'info',pending:true,progress:78});
  pauseNotification(metadata,'pointer');advance(10000);resumeNotification(metadata,'pointer');advance(60000);
  assert.equal(items[0].progress,78);assert.equal(items.length,1);
  notify('Metadata refreshed for 50 tracks.',{id:metadata,tone:'success'});
  advance(5999);assert.equal(items.length,1);advance(1);assert.equal(items.length,0);
  const dismissed=notify('Reading music details…',{id:'dismissed-music',pending:true});
  dismissNotification(dismissed);updateNotification(dismissed,{message:'Late progress',tone:'info',pending:true,progress:90});
  assert.equal(items.length,0);assert.equal(timers.size,0);
  console.log('PASS: retained Music progress, normal completion expiry and dismissed progress never revived');
  const resumed=notify('Metadata refresh stopped.',{id:'resumed'});advance(1000);
  updateNotification(resumed,{message:'Refreshing metadata…',tone:'info',pending:true,progress:10});
  advance(10000);assert.equal(items.length,1);assert.equal(timers.size,0);
  notify('Done',{id:resumed,tone:'success'});advance(6000);assert.equal(items.length,0);
  console.log('PASS: active progress cancels an older expiry timer without changing ordinary notice timing');


} finally {
  unsubscribe?.();Date.now=realNow;globalThis.setTimeout=realSetTimeout;globalThis.clearTimeout=realClearTimeout;
  await fs.rm(temporary,{recursive:true,force:true});
}

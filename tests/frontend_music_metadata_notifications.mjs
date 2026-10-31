import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const require=createRequire(import.meta.url);
const ts=require('../frontend/node_modules/typescript');
function load(file,imports,globals={}){
  const source=fs.readFileSync(path.join(root,file),'utf8');
  const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
  const context={exports:{},require:name=>{assert(name in imports,'Unexpected import '+name);return imports[name];},AbortController,Date,Map,Set,URL,...globals};
  vm.runInNewContext(code,context,{filename:file});return context.exports;
}
function writable(value){
  let listeners=[];
  return {subscribe(fn){listeners.push(fn);fn(value);return ()=>listeners=listeners.filter(item=>item!==fn);},
    set(next){if(next===value&&typeof next!=='object')return;value=next;listeners.slice().forEach(fn=>fn(value));},
    update(fn){this.set(fn(value));}};
}
function get(store){let value;store.subscribe(next=>value=next)();return value;}
class ApiError extends Error{constructor(status,message='Generated failure'){super(message);this.status=status;}}
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const settle=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
function harness(){
  let now=Date.now(),timerId=0,timers=new Map();
  const enabledModules=writable([]),folderRescanChanges=writable(0),suiteModules=writable([]),notifications=writable([]);
  const state={calls:[],reads:0,refreshes:[],clears:0,opens:0,status:async()=>({items:[]}),registry:async()=>[{id:'files',enabled:true},{id:'ost',enabled:true}],refresh:async()=>state.job()};
  state.job=(values={})=>({id:'job',source_id:'source',label:'Generated audio',state:'working',progress:20,processed:2,total:10,failed:0,discover:true,created_at:new Date(now).toISOString(),...values});
  class ClockDate extends Date{static now(){return now;}}
  const notify=(message,options={})=>{const item={id:options.id,message,...options};notifications.update(rows=>[...rows.filter(row=>row.id!==item.id),item]);};
  const api=load('frontend/src/modules/ost/metadataNotifications.ts',{
    'svelte/store':{get,writable},'../../lib/suiteStores':{enabledModules,folderRescanChanges,suiteModules},
    '../../lib/http':{ApiError},'../../lib/suiteApi':{suiteApi:{listModules:()=>{state.reads++;return state.registry();}}},
    '../../lib/notifications':{notifications,notify,updateNotification(id,values){notifications.update(rows=>rows.map(row=>row.id===id?{id,...values}:row));}},
    './api':{ostApi:{metadataJobs(signal){state.calls.push({at:now,signal});return state.status(signal);},refreshMetadata(source,parent){state.refreshes.push({source,parent});return state.refresh();}}},
    './libraryState':{clearMusicLibraryState(){state.clears++;},enableMusicLibraryState(){state.opens++;}},
  },{Date:ClockDate,setTimeout(fn,delay){const id=++timerId;timers.set(id,{at:now+delay,fn});return id;},clearTimeout(id){timers.delete(id);}});
  const stop=api.observeMusicMetadata();
  return {api,state,enabledModules,folderRescanChanges,suiteModules,notifications,stop,
    async enable(){enabledModules.set(['files','ost']);await settle();},
    async scan(){folderRescanChanges.update(value=>value+1);await settle();},
    async advance(ms){const until=now+ms;let steps=0;while(true){const next=[...timers].filter(([,task])=>task.at<=until).sort((a,b)=>a[1].at-b[1].at)[0];if(!next)break;assert(++steps<1000,'Runaway timer');now=next[1].at;timers.delete(next[0]);next[1].fn();await settle();}now=until;await settle();},
    pending:()=>timers.size};
}
let checks=0;
async function test(name,run){await run();checks++;console.log('PASS: '+name);}
await test('disabled discovery is inert; enable checks once and idle stays silent',async()=>{
  const h=harness();await h.advance(60000);await h.scan();assert.equal(h.state.calls.length,0);
  await h.enable();assert.equal(h.state.calls.length,1);await h.advance(60000);assert.equal(h.state.calls.length,1);assert.equal(h.pending(),0);
  h.enabledModules.set(['files','ost']);await settle();assert.equal(h.state.calls.length,1);h.stop();
});
await test('active jobs progress to completion and stop; later rescans wake discovery',async()=>{
  const h=harness();let job=h.state.job();h.state.status=async()=>({items:[job]});await h.enable();assert.equal(get(h.notifications)[0].progress,20);
  await h.advance(500);assert.equal(h.state.calls.length,2);
  job={...job,state:'completed',processed:10,progress:100};await h.advance(500);assert.match(get(h.notifications)[0].message,/refreshed for 10/);
  const count=h.state.calls.length;await h.advance(30000);assert.equal(h.state.calls.length,count);
  await h.scan();assert.equal(h.state.calls.length,count+1);await h.advance(30000);assert.equal(h.state.calls.length,count+1);h.stop();
});
await test('accepted refresh cancels a stale discovery and cannot lose the new job',async()=>{
  const h=harness(),stale=deferred();h.state.status=()=>stale.promise;await h.enable();const first=h.state.calls[0];
  h.state.status=async()=>({items:[h.state.job()]});await h.api.startMetadataRefresh('source','Album');await settle();assert(first.signal.aborted);
  stale.resolve({items:[]});await settle();assert.equal(get(h.api.metadataOperations)[0].id,'job');assert.equal(get(h.notifications)[0].pending,true);
  await h.advance(500);assert.equal(h.state.calls.length,3);h.stop();
});
await test('a rescan supersedes an in-flight read and an old failure cannot restart it',async()=>{
  const h=harness(),stale=deferred();h.state.status=()=>stale.promise;await h.enable();h.state.status=async()=>({items:[]});await h.scan();
  stale.reject(new ApiError(503));await settle();await h.advance(30000);assert.equal(h.state.calls.length,2);assert.equal(h.state.reads,0);h.stop();
});
await test('409 stops immediately and reconciles a stale enabled-module list once',async()=>{
  const h=harness();h.state.status=async()=>{throw new ApiError(409);};h.state.registry=async()=>[{id:'files',enabled:true},{id:'ost',enabled:false}];
  await h.enable();await h.advance(60000);assert.equal(h.state.calls.length,1);assert.equal(h.state.reads,1);assert.deepEqual([...get(h.enabledModules)],['files']);assert.equal(h.state.clears,1);h.stop();
});
await test('409 during maintenance stops even if the registry still says enabled',async()=>{
  const h=harness();await h.enable();h.state.status=async()=>({items:[h.state.job()]});await h.scan();h.state.status=async()=>{throw new ApiError(409);};
  await h.advance(500);assert.equal(h.state.reads,1);assert.equal(get(h.api.metadataOperations).length,0);assert.equal(get(h.notifications)[0].pending,undefined);assert.match(get(h.notifications)[0].message,/progress unavailable/);
  const count=h.state.calls.length;await h.advance(60000);assert.equal(h.state.calls.length,count);
  h.state.status=async()=>({items:[]});await h.scan();assert.equal(h.state.calls.length,count+1);h.stop();
});
await test('a failed reconciliation is bounded and a stale registry reply cannot overwrite a new owner session',async()=>{
  const h=harness(),late=deferred();h.state.status=async()=>{throw new ApiError(409);};h.state.registry=()=>late.promise;await h.enable();
  h.enabledModules.set(['files']);h.state.status=async()=>({items:[]});await h.enable();late.resolve([{id:'files',enabled:true},{id:'ost',enabled:false}]);await settle();assert(get(h.enabledModules).includes('ost'));
  h.state.status=async()=>{throw new ApiError(409);};h.state.registry=async()=>{throw new TypeError('offline');};await h.scan();const count=h.state.calls.length;await h.advance(60000);assert.equal(h.state.calls.length,count);assert.equal(h.state.reads,2);h.stop();
});
for(const failure of [new TypeError('offline'),new ApiError(503),new ApiError(429)]){
 await test('temporary '+failure.constructor.name+'/'+(failure.status??'network')+' uses three increasing retries then stops',async()=>{
  const h=harness();h.state.status=async()=>{throw failure;};await h.enable();await h.advance(999);assert.equal(h.state.calls.length,1);await h.advance(1);await h.advance(2000);await h.advance(4000);
  assert.deepEqual(h.state.calls.slice(1).map((call,i)=>call.at-h.state.calls[i].at),[1000,2000,4000]);await h.advance(60000);assert.equal(h.state.calls.length,4);assert.equal(h.pending(),0);h.stop();
 });
}
await test('a successful response resets the temporary-failure budget',async()=>{
 const h=harness();let failed=true;h.state.status=async()=>{if(failed)throw new ApiError(500);return {items:[h.state.job()]};};await h.enable();failed=false;await h.advance(1000);failed=true;await h.advance(500);await h.advance(1000);assert.equal(h.state.calls.length,4);h.stop();
});
await test('permanent errors stop without retries and missing jobs release their pending notice',async()=>{
 const h=harness();h.state.status=async()=>({items:[h.state.job()]});await h.enable();h.state.status=async()=>({items:[]});await h.advance(500);assert.match(get(h.notifications)[0].message,/stopped/);
 h.state.status=async()=>{throw new ApiError(404);};await h.scan();const count=h.state.calls.length;await h.advance(60000);assert.equal(h.state.calls.length,count);h.stop();
});
await test('disable and observer disposal abort reads/timers and ignore late acceptance',async()=>{
 const h=harness(),accepted=deferred(),read=deferred();h.state.status=()=>read.promise;await h.enable();h.state.refresh=()=>accepted.promise;
 const pending=h.api.startMetadataRefresh('source','Album');h.enabledModules.set(['files']);assert(h.state.calls[0].signal.aborted);await h.enable();
 accepted.resolve(h.state.job());await pending;assert.equal(get(h.api.metadataOperations).length,0);read.resolve({items:[]});await settle();h.stop();await h.scan();await h.advance(60000);assert.equal(h.pending(),0);assert.equal(h.state.calls.length,2);
});
await test('dismissed progress is not resurrected by completion or a monitoring failure',async()=>{
 const h=harness();let job=h.state.job();h.state.status=async()=>({items:[job]});await h.enable();h.notifications.set([]);job={...job,state:'completed'};await h.advance(500);assert.equal(get(h.notifications).length,0);
 job={...job,id:'other',state:'working'};await h.scan();h.notifications.set([]);h.state.status=async()=>{throw new ApiError(409);};await h.advance(500);assert.equal(get(h.notifications).length,0);h.stop();
});
await test('real Files/suite clients emit only after successful scans and preserve results/errors',async()=>{
 const folderRescanChanges=writable(0),calls=[];let ok=true,gate;
 const diagnosticFetch=async(url,options)=>{calls.push({url,options});if(gate)await gate;return {ok,status:ok?200:409,json:async()=>ok?{base:{added:1},module:{},source_id:'source'}:{detail:'Conflict'}};};
 const common={'./diagnostics':{diagnosticFetch},'./suiteStores':{folderRescanChanges},'./http':{apiError:async()=>new ApiError(409)},'./product':{VERSION:'test'}};
 const files=load('frontend/src/lib/filesApi.ts',common),suite=load('frontend/src/lib/suiteApi.ts',common);
 const wait=deferred();gate=wait.promise;const pending=files.filesApi.scanSource('one/two');await settle();assert.equal(get(folderRescanChanges),0);wait.resolve();gate=null;
 assert.equal((await pending).base.added,1);assert.equal(get(folderRescanChanges),1);assert.equal((await suite.suiteApi.rescanFolder('source')).source_id,'source');assert.equal(get(folderRescanChanges),2);
 assert.match(calls[0].url,/one%2Ftwo\/scan$/);assert.equal(calls[0].options.method,'POST');assert.match(calls[1].url,/folders\/source\/rescan$/);
 ok=false;await assert.rejects(files.filesApi.scanSource('source'));await assert.rejects(suite.suiteApi.rescanFolder('source'));assert.equal(get(folderRescanChanges),2);
});
console.log(`PASS: ${checks} Music metadata observer/client regression groups`);

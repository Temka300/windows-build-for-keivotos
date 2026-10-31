import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {build} from '../frontend/node_modules/vite/dist/node/index.js';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..'),frontend=path.join(root,'frontend');
const entry=path.join(frontend,'src/modules/video/watchProgress.ts');
const result=await build({root:frontend,configFile:false,logLevel:'silent',build:{write:false,minify:false,lib:{entry,formats:['es']}}});
const chunk=(Array.isArray(result)?result[0]:result).output.find(item=>item.type==='chunk');
const temporary=await fs.mkdtemp(path.join(os.tmpdir(),'keivotos-video-watch-'));
const originalSet=globalThis.setTimeout,originalClear=globalThis.clearTimeout;
let timers=new Map(),timerId=0;
const microtask=()=>new Promise(resolve=>setImmediate(resolve));
const tick=async()=>{const pending=[...timers.values()];timers.clear();for(const callback of pending)callback();await microtask();};
const observation=position=>({position,duration:100,ended:false});
const subject={source_id:'source',path:'episode.mp4'};
function fixture(saved=null){
 const calls=[],confirmed=[],failures=[];let reject=false;
 const transport={read:async()=>({revision:'a'.repeat(64),generation:2,position:saved}),begin:async(item,revision,expected)=>{calls.push({begin:true,item,expected});return {generation:expected+1,revision};},save:async(value,keepalive)=>{calls.push({...value,keepalive});if(reject)throw Error('offline');return {...value,updated_at:'confirmed'};}};
 return {transport,calls,confirmed,failures,setReject:value=>reject=value};
}
try{
 globalThis.setTimeout=callback=>{const id=++timerId;timers.set(id,callback);return id;};globalThis.clearTimeout=id=>timers.delete(id);
 const file=path.join(temporary,'watch.mjs');await fs.writeFile(file,chunk.code);const {openWatch}=await import(pathToFileURL(file).href);
 const open=f=>openWatch(subject,f.transport,()=>true,value=>f.confirmed.push(value),error=>f.failures.push(error));
 {
  const f=fixture({...observation(39),ended:false}),session=await open(f);
  assert.equal(session.initialPosition,39);assert.equal(f.calls.length,1);assert.equal(timers.size,0);
  session.observe(observation(40));session.observe(observation(41));assert.equal(f.calls.length,1);await tick();
  assert.equal(f.calls.length,2);assert.equal(f.calls[1].position,41);assert.equal(f.calls[1].generation,3);assert.equal(f.confirmed.length,1);
  session.dispose();
 }
 console.log('PASS: lookup/session do not write positions; observations coalesce to their immutable subject and generation');
 {
  const f=fixture({...observation(100),ended:true}),session=await open(f);assert.equal(session.initialPosition,0);assert.equal(f.confirmed.length,0);
  session.observe({...observation(100),ended:true});await session.flush();assert.equal(f.confirmed.at(-1).ended,true);assert.equal(timers.size,0);session.dispose();
 }
 console.log('PASS: finished episodes start at zero without clearing completion and explicit flush drains the pending save');
 {
  const f=fixture(),session=await open(f);session.observe(observation(12));await session.flush();f.setReject(true);
  session.observe(observation(15));assert.equal(await session.flush(),false);assert.equal(f.confirmed.at(-1).position,12);
  const failed=f.calls.at(-1);session.observe(observation(19));await tick();assert.equal(f.calls.at(-1),failed);
  f.setReject(false);assert.equal(await session.retry(),true);assert.deepEqual(f.calls.at(-2),failed);assert.equal(f.confirmed.at(-1).position,19);
  assert(f.calls.at(-1).sequence>failed.sequence);session.dispose();
 }
 console.log('PASS: failed saves retain acknowledgement and retry the exact idempotent write before newer observations');
 {
  const f=fixture();let resolve;f.transport.save=value=>new Promise(done=>{resolve=()=>done({...value,updated_at:'confirmed'});f.calls.push({...value});});
  const session=await open(f);session.observe(observation(1));const flushed=session.flush();session.observe(observation(2));
  assert.equal(f.calls.filter(x=>!x.begin).length,1);resolve();await microtask();assert.equal(f.calls.filter(x=>!x.begin).length,2);resolve();await flushed;
  assert.deepEqual(f.confirmed.map(x=>x.position),[1,2]);session.dispose();
 }
 console.log('PASS: only one ordinary save is in flight and newer observations drain in order');
 {
  const f=fixture();let owns=true;f.transport.read=async()=>{owns=false;return {revision:'x',generation:0,position:null};};
  assert.equal(await openWatch(subject,f.transport,()=>owns,()=>{},()=>{}),null);assert.equal(f.calls.length,0);
  f.transport.read=async()=>{throw Error('unavailable');};await assert.rejects(open(f));assert.equal(f.calls.length,0);
 }
 console.log('PASS: cancelled or failed lookup cannot create a session or overwrite unknown saved state');
 {
  const f=fixture(),session=await open(f);for(const value of [observation(NaN),observation(-1),observation(101),{position:1,duration:0,ended:false}])session.observe(value);
  await tick();assert.equal(f.calls.length,1);session.observe(observation(3));session.dispose();await microtask();
  assert.equal(f.calls.at(-1).keepalive,true);const count=f.calls.length;session.observe(observation(4));await tick();assert.equal(f.calls.length,count);
 }
 console.log('PASS: invalid observations are ignored and disposal sends a bounded keepalive without later observations');
 {
  const f=fixture();let resolveFirst;let calls=0;
  f.transport.save=async value=>{if(++calls===1)return new Promise(done=>resolveFirst=()=>done({...value,updated_at:'late'}));return {...value,updated_at:'newer'};};
  const session=await open(f);session.observe(observation(1));const ordinary=session.flush();session.observe(observation(2));
  await session.flush(true);assert.deepEqual(f.confirmed.map(value=>value.position),[2]);resolveFirst();await ordinary;
  assert.deepEqual(f.confirmed.map(value=>value.position),[2]);session.dispose();
 }
 console.log('PASS: a delayed ordinary acknowledgement cannot replace a newer keepalive acknowledgement');
 assert.equal(timers.size,0);
}finally{globalThis.setTimeout=originalSet;globalThis.clearTimeout=originalClear;await fs.rm(temporary,{recursive:true,force:true});}

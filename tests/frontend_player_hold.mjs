import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {build} from '../frontend/node_modules/vite/dist/node/index.js';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..'),frontend=path.join(root,'frontend');
const entry=path.join(frontend,'__player_hold_contract__.ts');
const code=`export * from './src/lib/playerSpaceHold';
export * from './src/lib/playbackPreferences';
export {get} from 'svelte/store';`;
const result=await build({root:frontend,configFile:false,logLevel:'silent',plugins:[{name:'hold-contract',resolveId:id=>id===entry?id:null,load:id=>id===entry?code:null}],build:{write:false,minify:false,lib:{entry,formats:['es']}}});
const chunk=(Array.isArray(result)?result[0]:result).output.find(item=>item.type==='chunk');
const temporary=await fs.mkdtemp(path.join(os.tmpdir(),'keivotos-player-hold-'));
const originalSet=globalThis.setTimeout,originalClear=globalThis.clearTimeout,originalStorage=globalThis.localStorage;
let now=0,sequence=0,timers=new Map();
const stored=new Map([['keivotos:player-hold-space','"bad"'],['keivotos:player-hold-speed-multiplier','"fast"'],['keivotos:video-playback-speed','1.25'],['keivotos:youtube-playback-speed','2']]);
function advance(ms){const end=now+ms;while(true){const due=[...timers].filter(([,timer])=>timer.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];if(!due)break;now=due[1].at;timers.delete(due[0]);due[1].run();}now=end;}
try{
 globalThis.setTimeout=(run,delay)=>{const id=++sequence;timers.set(id,{at:now+delay,run});return id;};globalThis.clearTimeout=id=>timers.delete(id);
 globalThis.localStorage={getItem:key=>stored.get(key)??null,setItem:(key,value)=>stored.set(key,value)};
 const file=path.join(temporary,'hold.mjs');await fs.writeFile(file,chunk.code);
 const {createSpaceHold,isSpaceKey,SPACE_HOLD_DELAY_MS,HOLD_SPEED_MULTIPLIERS,normalizeHoldSpeedMultiplier,holdSpaceEnabled,holdSpeedMultiplier,speedFor,get}=await import(pathToFileURL(file).href);
 const make=()=>{const calls=[];return {calls,gesture:createSpaceHold({tap:()=>calls.push('tap'),boost:()=>calls.push('boost'),restore:()=>calls.push('restore')})};};
 assert.equal(SPACE_HOLD_DELAY_MS,250);
 {
  const {calls,gesture}=make();assert(gesture.press(true));advance(249);assert.deepEqual(calls,[]);assert(gesture.release());assert.deepEqual(calls,['tap']);advance(1000);assert.equal(timers.size,0);assert.equal(gesture.pending,false);
 }
 {
  const {calls,gesture}=make();gesture.press(true);advance(250);assert.equal(gesture.boosted,true);assert.deepEqual(calls,['boost']);gesture.release();assert.deepEqual(calls,['boost','restore']);assert.equal(gesture.release(),false);advance(500);assert.equal(timers.size,0);
 }
 console.log('PASS: short tap and exact hold threshold, one restore and no delayed tap');
 {
  const {calls,gesture}=make();gesture.press(true);advance(100);gesture.cancel();advance(300);assert.equal(gesture.release(),false);assert.deepEqual(calls,[]);gesture.press(true);gesture.release();assert.deepEqual(calls,['tap']);
 }
 {
  const {calls,gesture}=make();gesture.press(true);advance(250);gesture.cancel();gesture.cancel();gesture.release();assert.deepEqual(calls,['boost','restore']);gesture.press(true);advance(249);gesture.release(false);advance(1000);assert.deepEqual(calls,['boost','restore']);assert.equal(timers.size,0);
 }
 console.log('PASS: cancellation before/after boost, cancelled release, repeated cleanup and new gesture ownership');
 {
  const {calls,gesture}=make();assert.equal(gesture.press(true,true),false);assert.equal(gesture.release(),false);assert.deepEqual(calls,[]);gesture.press(true);advance(100);for(let i=0;i<25;i++){assert.equal(gesture.press(true,true),false);assert.equal(gesture.press(true,false),false);}advance(150);assert.deepEqual(calls,['boost']);gesture.release();assert.deepEqual(calls,['boost','restore']);
 }
 {
  const {calls,gesture}=make();gesture.press(false);assert.deepEqual(calls,['tap']);assert.equal(gesture.press(false,true),false);assert.equal(gesture.release(),false);advance(1000);assert.deepEqual(calls,['tap']);assert.equal(timers.size,0);
 }
 console.log('PASS: repeat-only/duplicate keydown suppression, stable threshold and disabled immediate play/pause');
 {
  const calls=[];const gesture=createSpaceHold({tap:()=>calls.push('tap'),boost:()=>{throw new Error('fixture boost failed');},restore:()=>calls.push('restore')});gesture.press(true);assert.throws(()=>advance(250),/fixture boost failed/);assert.equal(gesture.pending,false);assert.equal(timers.size,0);assert.deepEqual(calls,['restore']);gesture.press(true);gesture.release();assert.deepEqual(calls,['restore','tap']);
 }
 assert(isSpaceKey({key:' ',code:'Space'}));assert(isSpaceKey({key:'Spacebar'}));assert(!isSpaceKey({key:'k',code:'KeyK'}));
 console.log('PASS: failed activation drains its gesture/timer and legacy/physical Space keys are recognized');
 assert.equal(get(holdSpaceEnabled),true);assert.equal(get(holdSpeedMultiplier),1.5);
 for(const value of HOLD_SPEED_MULTIPLIERS)assert.equal(normalizeHoldSpeedMultiplier(value),value);
 for(const value of [null,undefined,false,true,0,-1,1,3,NaN,Infinity,'1.5',{},[]])assert.equal(normalizeHoldSpeedMultiplier(value),1.5);
 holdSpaceEnabled.set(false);holdSpeedMultiplier.set(2);assert.equal(stored.get('keivotos:player-hold-space'),'false');assert.equal(stored.get('keivotos:player-hold-speed-multiplier'),'2');
 assert.equal(get(speedFor('video')),1.25);assert.equal(get(speedFor('youtube')),2);assert.equal(stored.has('keivotos:ost-playback-speed'),false);
 const restored=await import(pathToFileURL(file).href+'?saved-preferences');assert.equal(restored.get(restored.holdSpaceEnabled),false);assert.equal(restored.get(restored.holdSpeedMultiplier),2);assert.equal(restored.get(restored.speedFor('video')),1.25);assert.equal(restored.get(restored.speedFor('youtube')),2);
 assert.equal(timers.size,0);
 console.log('PASS: validated hold defaults/options and persistence preserve independent normal Video/YouTube speeds and Music');
}finally{globalThis.setTimeout=originalSet;globalThis.clearTimeout=originalClear;if(originalStorage===undefined)delete globalThis.localStorage;else globalThis.localStorage=originalStorage;await fs.rm(temporary,{recursive:true,force:true});}

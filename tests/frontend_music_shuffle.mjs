import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {build} from '../frontend/node_modules/vite/dist/node/index.js';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..'),frontend=path.join(root,'frontend');
const entry=path.join(frontend,'__music_shuffle_contract__.ts');
const code=`export * from './src/modules/ost/shuffle'; export * from './src/lib/playbackPreferences'; export {get} from 'svelte/store';`;
const result=await build({root:frontend,configFile:false,logLevel:'silent',plugins:[{name:'shuffle-contract',resolveId:id=>id===entry?id:null,load:id=>id===entry?code:null}],build:{write:false,minify:false,lib:{entry,formats:['es']}}});
const chunk=(Array.isArray(result)?result[0]:result).output.find(item=>item.type==='chunk');
const temporary=await fs.mkdtemp(path.join(os.tmpdir(),'keivotos-music-shuffle-')),originalStorage=globalThis.localStorage;
const stored=new Map([['keivotos:ost-shuffle','"bad"'],['keivotos:video-playback-speed','1.75'],['keivotos:youtube-playback-speed','1.25'],['keivotos:ost-volume-percent','17'],['keivotos:ost-loop-mode','"all"'],['keivotos:ost-auto-next','false']]);
try{
 globalThis.localStorage={getItem:key=>stored.get(key)??null,setItem:(key,value)=>stored.set(key,value)};
 const file=path.join(temporary,'shuffle.mjs');await fs.writeFile(file,chunk.code);
 const module=await import(pathToFileURL(file).href),{createShuffleQueue,MAX_SHUFFLE_HISTORY,get}=module;
 {
  const queue=createShuffleQueue(()=>.99999);queue.reset(null,[]);assert.equal(queue.move(1,true),null);assert.equal(queue.canPrevious,false);assert.equal(queue.canNext,false);
  queue.reset('A',['A','A']);assert.equal(queue.move(1),null);assert.equal(queue.move(1,true),null);assert.equal(queue.move(-1,true),null);assert.equal(queue.current,'A');
 }
 console.log('PASS: empty/single pools and beginning-of-history never fabricate navigation');
 {
  let sampled=0;const queue=createShuffleQueue(()=>{sampled++;return .99999;});queue.reset('A',['A','B','C','C']);
  const played=['A',queue.move(1),queue.move(1)];assert.deepEqual(new Set(played),new Set(['A','B','C']));assert.equal(queue.move(1),null);assert.equal(queue.canNext,false);
  const samples=sampled;assert.equal(queue.move(-1),played[1]);assert.equal(queue.move(-1),played[0]);assert.equal(queue.move(-1,true),null);
  assert.equal(queue.move(1),played[1]);assert.equal(queue.move(1),played[2]);assert.equal(queue.move(1),null);assert.equal(sampled,samples);
 }
 console.log('PASS: without-replacement rounds and actual Previous/forward history consume each choice once');
 {
  const queue=createShuffleQueue(()=>.37);queue.reset('A',['A','B','C','D']);
  let current='A';for(let round=0;round<30;round++){
   const group=[current];for(let i=0;i<3;i++){const next=queue.move(1,true);assert.notEqual(next,current);group.push(next);current=next;}
   assert.equal(new Set(group).size,4);
  }
 }
 console.log('PASS: Loop All starts fresh complete rounds without repeating the just-played track');
 {
  const queue=createShuffleQueue(()=>.99999);queue.reset('A',['A','B','C']);assert.equal(queue.move(1),'C');assert.equal(queue.move(1),'B');
  queue.reconcile('B',['A','B','D']);assert.equal(queue.move(-1),'A');assert.equal(queue.move(1),'B');assert.equal(queue.move(1),'D');assert.equal(queue.move(1),null);
  queue.reconcile('D',['D']);assert.equal(queue.canPrevious,false);assert.equal(queue.move(-1),null);assert.equal(queue.move(1,true),null);
 }
 {
  const ids=Array.from({length:10000},(_,index)=>String(index)),queue=createShuffleQueue(()=>.99999);queue.reset('0',ids.slice(0,5000));queue.reconcile('0',ids);const visited=new Set(['0']);let next;while((next=queue.move(1))!==null){assert(!visited.has(next));visited.add(next);}assert.equal(visited.size,10000);
 }
 console.log('PASS: availability changes skip removed history and add new eligible identities only once');
 {
  const first=JSON.stringify(['root-a','same.wav']),second=JSON.stringify(['root-b','same.wav']);
  const ids=[first,second];const queue=createShuffleQueue(()=>0);queue.reset(first,ids);assert.equal(queue.move(1),second);assert.deepEqual(ids,[first,second]);
  queue.reset('new',['new','other']);assert.equal(queue.canPrevious,false);assert.equal(queue.move(1),'other');assert(![first,second].includes(queue.current));
  queue.reset('new',['new','other']);assert.equal(queue.current,'new');assert.equal(queue.canPrevious,false);
 }
 console.log('PASS: source/path identity, queue resets and new rounds retain their independent context');
 {
  const queue=createShuffleQueue(()=>0);queue.reset('A',['A','B']);for(let i=0;i<600;i++)assert.notEqual(queue.move(1,true),null);
  assert.equal(queue.historyLength,MAX_SHUFFLE_HISTORY);let previous=0;while(queue.canPrevious){assert.notEqual(queue.move(-1),null);previous++;}assert.equal(previous,MAX_SHUFFLE_HISTORY-1);
 }
 console.log('PASS: temporary played history remains bounded through repeated looping');
 assert.equal(get(module.musicShuffle),false);assert.equal(stored.get('keivotos:ost-shuffle'),'false');module.musicShuffle.set(true);assert.equal(stored.get('keivotos:ost-shuffle'),'true');
 const restored=await import(pathToFileURL(file).href+'?restored');assert.equal(restored.get(restored.musicShuffle),true);
 assert.equal(restored.get(restored.speedFor('video')),1.75);assert.equal(restored.get(restored.speedFor('youtube')),1.25);assert.equal(restored.get(restored.volumeFor('ost')),17);assert.equal(restored.get(restored.loopModeFor('ost')),'all');assert.equal(restored.get(restored.autoNextFor('ost')),false);
 console.log('PASS: remembered on/off preference preserves all existing owner playback preferences');
 for(const [index,value] of [null,1,0,'true',{},[],false].entries()){
  stored.set('keivotos:ost-shuffle',JSON.stringify(value));const m=await import(pathToFileURL(file).href+'?malformed='+index);assert.equal(m.get(m.musicShuffle),false);
 }
 stored.set('keivotos:ost-shuffle','{invalid');const bad=await import(pathToFileURL(file).href+'?invalid-json');assert.equal(bad.get(bad.musicShuffle),false);
 delete globalThis.localStorage;const without=await import(pathToFileURL(file).href+'?without-storage');assert.equal(without.get(without.musicShuffle),false);
 console.log('PASS: missing/malformed/unavailable saved storage safely defaults shuffle off');
}finally{
 if(originalStorage===undefined)delete globalThis.localStorage;else globalThis.localStorage=originalStorage;
 assert(path.resolve(temporary).startsWith(path.resolve(os.tmpdir())+path.sep)&&path.basename(temporary).startsWith('keivotos-music-shuffle-'));
 await fs.rm(temporary,{recursive:true,force:true});
}

import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {build} from '../frontend/node_modules/vite/dist/node/index.js';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const result=await build({root:path.join(root,'frontend'),configFile:false,logLevel:'silent',build:{write:false,minify:false,lib:{entry:path.join(root,'frontend/src/modules/ost/libraryState.ts'),formats:['es']}}});
const dir=await fs.mkdtemp(path.join(os.tmpdir(),'music-state-'));
try{
 const chunk=(Array.isArray(result)?result[0]:result).output.find(row=>row.type==='chunk');
 const file=path.join(dir,'state.mjs');await fs.writeFile(file,chunk.code);
 const state=await import(pathToFileURL(file).href);
 assert.equal(state.musicLibraryState(),null);
 const snapshot={tracks:[{source_id:'one',path:'a.wav',music:{title:'Publisher title'}}],positions:Array.from({length:80},(_,i)=>['folder'+i,{count:i,scroll:i,focus:'',selected:''}])};
 state.saveMusicLibraryState(snapshot);snapshot.tracks[0].music.title='Changed caller';
 const first=state.musicLibraryState();assert.equal(first.tracks[0].music.title,'Publisher title');assert.equal(first.positions.length,64);
 first.tracks.length=0;assert.equal(state.musicLibraryState().tracks.length,1);
 assert(!('queue' in state.musicLibraryState()));assert(!('audio' in state.musicLibraryState()));
 state.clearMusicLibraryState();state.saveMusicLibraryState(snapshot);assert.equal(state.musicLibraryState(),null);
 state.enableMusicLibraryState();state.saveMusicLibraryState(snapshot);assert.equal(state.musicLibraryState().tracks.length,1);
 console.log('PASS: retained isolated list snapshots, bounded positions, no audio and disable-before-unmount guard');
}finally{await fs.rm(dir,{recursive:true,force:true});}

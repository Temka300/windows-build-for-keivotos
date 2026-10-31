import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {build} from '../frontend/node_modules/vite/dist/node/index.js';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..'),frontend=path.join(root,'frontend');
const temporary=await fs.mkdtemp(path.join(os.tmpdir(),'keivotos-path-contract-'));
try{
 const entry=path.join(frontend,'__path_contract__.ts');
 const result=await build({root:frontend,configFile:false,logLevel:'silent',plugins:[{name:'paths',resolveId:id=>id===entry?id:null,load:id=>id===entry?"export {normalizedPath} from './src/lib/suiteApi';":null}],build:{write:false,minify:false,lib:{entry,formats:['es']}}});
 const code=(Array.isArray(result)?result[0]:result).output.find(item=>item.type==='chunk').code;
 const file=path.join(temporary,'paths.mjs');await fs.writeFile(file,code);const {normalizedPath:normalize}=await import(pathToFileURL(file).href);
 assert.equal(normalize('/home/Artist/音楽/ExactName.flac'),'/home/Artist/音楽/ExactName.flac');
 assert.notEqual(normalize('/home/Artist'),normalize('/home/artist'));
 assert.notEqual(normalize('/tmp/Σ'),normalize('/tmp/σ'));
 assert.equal(normalize('/tmp//Case/'),normalize('/tmp/Case'));
 assert.equal(normalize('D:\\Folder\\音楽\\'),normalize('d:/folder/音楽'));
 assert.equal(normalize('\\\\Server\\Share\\Folder'),normalize('//server/share/folder/'));
 assert.equal(normalize('\\\\?\\C:\\Folder'),normalize('//?/c:/folder'));
 console.log('PASS: POSIX case/Unicode identity, separator normalization and Windows drive/UNC/device case compatibility');
}finally{await fs.rm(temporary,{recursive:true,force:true});}

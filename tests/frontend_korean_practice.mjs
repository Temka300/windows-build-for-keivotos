import assert from 'node:assert/strict';
import {mkdtemp,rm,writeFile} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {build} from '../frontend/node_modules/vite/dist/node/index.js';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const temporary=await mkdtemp(path.join(os.tmpdir(),'keivotos-korean-practice-'));
try {
  const built=await build({root:path.join(root,'frontend'),configFile:false,logLevel:'silent',
    build:{write:false,minify:false,lib:{entry:path.join(root,'frontend/src/modules/language/koreanPractice.ts'),formats:['es']}}});
  const chunk=(Array.isArray(built)?built[0]:built).output.find(item=>item.type==='chunk');
  const file=path.join(temporary,'practice.mjs');await writeFile(file,chunk.code);
  const {question,accepts,nextQuestion,validPreferences}=await import(pathToFileURL(file).href);
  // Independently enumerate every finite written form, including units and exceptional spellings.
  const ones=['','일','이','삼','사','오','육','칠','팔','구'];
  const tens=['','십','이십','삼십','사십','오십','육십','칠십','팔십','구십'];
  const sino=Array.from({length:99},(_,i)=>tens[Math.floor((i+1)/10)]+ones[(i+1)%10]);
  const native='하나 둘 셋 넷 다섯 여섯 일곱 여덟 아홉 열 열하나 열둘 열셋 열넷 열다섯 열여섯 열일곱 열여덟 열아홉 스물'.split(' ');
  const months='일월 이월 삼월 사월 오월 유월 칠월 팔월 구월 시월 십일월 십이월'.split(' ');
  const hours='한 두 세 네 다섯 여섯 일곱 여덟 아홉 열 열한 열두'.split(' ').map(n=>n+' 시');
  const expected={sino,native,months,dates:sino.slice(0,31).map(n=>n+'일'),hours,minutes:sino.slice(0,59).map(n=>n+' 분')};
  let finite=0;
  for(const [category,answers] of Object.entries(expected))for(const [index,korean] of answers.entries()){
    const value=index+1,k=question(category,value,'korean'),e=question(category,value,'english');
    assert.equal(k.answer,korean);assert.equal(e.prompt,korean);assert(accepts(k,korean.normalize('NFD')));
    assert(accepts(k,'  '+korean+'  '));assert(accepts(e,String(value)));assert(accepts(e,e.answer));
    for(const alias of e.aliases){assert(accepts(e,alias));assert(accepts(e,alias.toUpperCase().replace(/-/g,'  ')));}
    assert(!accepts(k,String(value)));assert(!accepts(e,korean));assert(!accepts(e,String(value+1)));
    assert(!accepts(k,korean+'요'));assert(!accepts(e,'answer '+value));assert(!accepts(e,'x'.repeat(257)));
    finite++;
  }
  assert.equal(finite,233);
  for(const [category,value,bad] of [['months',6,'육월'],['months',10,'십월'],['hours',1,'하나 시'],['hours',2,'둘 시'],['hours',3,'셋 시'],['hours',4,'넷 시'],['hours',11,'열하나 시'],['hours',12,'열둘 시'],['sino',16,'심뉵'],['native',20,'스무'],['native',3,'삼'],['sino',3,'셋']])assert(!accepts(question(category,value,'korean'),bad),bad);
  assert(accepts(question('hours',11,'korean'),'열한시'));assert(accepts(question('minutes',25,'korean'),'이십오분'));
  assert(!accepts(question('minutes',25,'korean'),'이 십 오 분'));
  assert(accepts(question('sino',17,'english'),'SEVENTEEN'));
  assert(accepts(question('sino',25,'english'),'twenty-five'));assert(accepts(question('sino',25,'english'),'twenty five'));
  assert(accepts(question('months',6,'english'),'June'));assert(!accepts(question('months',6,'english'),'July'));
  assert(accepts(question('dates',21,'english'),'twenty-first day'));assert(accepts(question('dates',21,'english'),'21st'));
  assert(accepts(question('hours',3,'english'),"three o'clock"));assert(accepts(question('hours',3,'english'),'3:00'));assert(!accepts(question('hours',3,'english'),'3:25'));
  assert(accepts(question('minutes',25,'english'),'twenty-five minutes'));assert(!accepts(question('minutes',25,'english'),'25 hours'));
  for(const [category,answers] of Object.entries(expected)){
    const settings={category,answer_language:'korean',minimum:1,maximum:answers.length};assert(validPreferences(settings));
    assert.equal(nextQuestion(settings,undefined,()=>0).value,1);assert.equal(nextQuestion(settings,undefined,()=>1).value,answers.length);
    const previous=question(category,1,'korean');assert.notEqual(nextQuestion(settings,previous,()=>0).value,1);
    assert(!validPreferences({...settings,maximum:answers.length+1}));assert(!validPreferences({...settings,minimum:0}));
    assert(!validPreferences({...settings,minimum:2,maximum:1}));assert(!validPreferences({...settings,minimum:1.5}));
    assert.equal(nextQuestion({...settings,minimum:1,maximum:1},previous).value,1);
    assert.throws(()=>question(category,0,'korean'),RangeError);assert.throws(()=>question(category,answers.length+1,'korean'),RangeError);
  }
  console.log(`PASS: all ${finite} finite questions in both directions, written exceptions, bounded aliases, wrong systems, ranges and non-repeating selection`);
} finally {await rm(temporary,{recursive:true,force:true});}

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
  const cases=[
    ['month_date',74,'삼월 십오일','March 15',['3/15','03/15','March fifteenth','march fifteen'],['15/3','April 15','15']],
    ['month_date',59,'이월 이십팔일','February 28',['2/28','February twenty-eighth'],['February 29','2/29']],
    ['month_date',60,'삼월 일일','March 1',['3/1','March first'],['2/29','3/2']],
    ['month_date',152,'유월 일일','June 1',['6/1','June first'],['6/2']],
    ['month_date',274,'시월 일일','October 1',['10/1','October first'],['10/2']],
    ['hour_minute',146,'세 시 이십오 분','3:25',['03:25','three twenty-five','three hours twenty-five minutes'],['3:26','25','25:3']],
    ['hour_minute',121,'세 시','3:00',["three o'clock",'03:00'],['3:01','3']],
    ['hour_minute',126,'세 시 오 분','3:05',['three oh five','three zero five'],['3:50']],
    ['hour_minute',720,'열두 시 오십구 분','12:59',['twelve fifty-nine'],['13:59','12:60']],
    ['date_time',52706,'삼월 십오일 세 시 이십오 분','March 15, 3:25',['3/15 3:25','March fifteenth, three twenty-five'],['March 15','3:25','15/3 3:25','March 15 3:26']],
    ['date_time',262800,'십이월 삼십일일 열두 시 오십구 분','December 31, 12:59',['12/31 12:59','December thirty-first twelve fifty-nine'],['December 32 12:59']],
  ];
  for(const [category,index,korean,english,good,bad] of cases){
    const k=question(category,index,'korean'),e=question(category,index,'english');
    assert.equal(k.answer,korean);assert.equal(e.prompt,korean);assert.equal(e.answer,english);
    assert(accepts(k,korean));assert(accepts(k,korean.normalize('NFD')));assert(accepts(k,korean.replace(/ /g,'')));
    assert(accepts(e,english));for(const alias of good)assert(accepts(e,alias),alias);
    for(const alias of bad)assert(!accepts(e,alias),alias);
    assert(!accepts(k,korean.replace('세 시','셋 시'))||!korean.includes('세 시'));
    assert(!accepts(k,korean+'요'));assert(!accepts(k,korean.replace('십오','십 오'))||!korean.includes('십오'));
    assert(!accepts(e,String(index)));assert(!accepts(e,'x'.repeat(257)));
  }
  assert(accepts(question('date_time',52706,'korean'),'삼 월 십오 일 세시 이십오분'));
  assert(!accepts(question('month_date',152,'korean'),'육월 일일'));
  assert(!accepts(question('month_date',274,'korean'),'십 월 일일'));
  let dates=0,times=0;
  const lengths=[31,28,31,30,31,30,31,31,30,31,30,31];
  for(let m=1;m<=12;m++)for(let d=1;d<=lengths[m-1];d++){
    dates++;assert.equal(question('month_date',dates,'english').prompt,months[m-1]+' '+sino[d-1]+'일');
  }
  for(let h=1;h<=12;h++)for(let m=0;m<60;m++){
    times++;assert.equal(question('hour_minute',times,'english').prompt,hours[h-1]+(m?' '+sino[m-1]+' 분':''));
  }
  assert.equal(dates,365);assert.equal(times,720);
  for(const [category,maximum] of [['month_date',365],['hour_minute',720],['date_time',262800]]){
    const p={category,answer_language:'english',minimum:1,maximum};assert(validPreferences(p));
    assert.equal(nextQuestion(p,undefined,()=>0).value,1);assert.equal(nextQuestion(p,undefined,()=>1).value,maximum);
    assert.equal(nextQuestion(p,question(category,maximum,'english'),()=>1).value,1);
    assert(!validPreferences({...p,maximum:maximum+1}));assert.throws(()=>question(category,maximum+1,'korean'),RangeError);
  }
  console.log(`PASS: all ${finite} finite questions in both directions, written exceptions, bounded aliases, wrong systems, ranges and non-repeating selection`);
  console.log('PASS: all 365 dates/720 times, combined date/time boundaries, digits/words, Korean units, component order and bounded non-repeating generation');
} finally {await rm(temporary,{recursive:true,force:true});}

// Finite, independently authored written-number practice. No network or datasets.
export const categories = [
  {id:'sino', label:'Sino-Korean numbers', maximum:99},
  {id:'native', label:'Native Korean numbers', maximum:20},
  {id:'months', label:'Months', maximum:12},
  {id:'dates', label:'Days of the month', maximum:31},
  {id:'hours', label:'Hours', maximum:12},
  {id:'minutes', label:'Minutes', maximum:59},
] as const;
export type Category = typeof categories[number]['id'];
export type AnswerLanguage = 'korean'|'english';
export interface StudyPreferences {category:Category; answer_language:AnswerLanguage; minimum:number; maximum:number;}
export const defaultPreferences:StudyPreferences = {category:'sino',answer_language:'english',minimum:1,maximum:99};
export interface Question {category:Category; value:number; answerLanguage:AnswerLanguage; prompt:string; answer:string; aliases:string[];}
const sinoUnits = ['', '일','이','삼','사','오','육','칠','팔','구'];
const native = ['', '하나','둘','셋','넷','다섯','여섯','일곱','여덟','아홉','열',
  '열하나','열둘','열셋','열넷','열다섯','열여섯','열일곱','열여덟','열아홉','스물'];
const hours = ['', '한','두','세','네','다섯','여섯','일곱','여덟','아홉','열','열한','열두'];
const monthNames = ['', 'January','February','March','April','May','June','July','August','September','October','November','December'];
const englishSmall = ['zero','one','two','three','four','five','six','seven','eight','nine','ten',
  'eleven','twelve','thirteen','fourteen','fifteen','sixteen','seventeen','eighteen','nineteen'];
const englishTens = ['', '', 'twenty','thirty','forty','fifty','sixty','seventy','eighty','ninety'];
const ordinals = ['', 'first','second','third','fourth','fifth','sixth','seventh','eighth','ninth','tenth',
  'eleventh','twelfth','thirteenth','fourteenth','fifteenth','sixteenth','seventeenth','eighteenth','nineteenth','twentieth',
  'twenty-first','twenty-second','twenty-third','twenty-fourth','twenty-fifth','twenty-sixth','twenty-seventh','twenty-eighth','twenty-ninth','thirtieth','thirty-first'];

function sino(value:number):string {
  return (value>=10 ? (value>=20 ? sinoUnits[Math.floor(value/10)] : '')+'십' : '')+sinoUnits[value%10];
}
function words(value:number):string {
  return value<20 ? englishSmall[value] : englishTens[Math.floor(value/10)]+(value%10 ? '-'+englishSmall[value%10] : '');
}
function ordinal(value:number):string {
  return value+(value>=11&&value<=13 ? 'th' : ({1:'st',2:'nd',3:'rd'}[value%10]||'th'));
}
export function validPreferences(p:StudyPreferences):boolean {
  const category=categories.find(c=>c.id===p.category);
  return !!category&&(p.answer_language==='korean'||p.answer_language==='english')&&
    Number.isInteger(p.minimum)&&Number.isInteger(p.maximum)&&p.minimum>=1&&p.minimum<=p.maximum&&p.maximum<=category.maximum;
}
export function question(category:Category, value:number, answerLanguage:AnswerLanguage):Question {
  const maximum=categories.find(c=>c.id===category)?.maximum;
  if(!maximum||!Number.isInteger(value)||value<1||value>maximum||(answerLanguage!=='korean'&&answerLanguage!=='english'))throw new RangeError('Invalid practice question');
  const number=String(value),word=words(value);
  let korean=sino(value),english=number,aliases=[number,word];
  if(category==='native')korean=native[value];
  if(category==='months'){
    korean=(value===6 ? '유' : value===10 ? '시' : sino(value))+'월';english=monthNames[value];
    aliases.push(english,english.slice(0,3));
  }
  if(category==='dates'){
    korean+='일';english=ordinal(value)+' day';
    aliases.push(ordinal(value),ordinals[value],english,ordinals[value]+' day','day '+number);
  }
  if(category==='hours'){
    korean=hours[value]+' 시';english=number+' o’clock';
    aliases.push(english,word+" o'clock",number+':00');
  }
  if(category==='minutes'){
    korean+=' 분';const unit=value===1 ? ' minute' : ' minutes';english=number+unit;
    aliases.push(english,word+unit);
  }
  if(answerLanguage==='korean')return {category,value,answerLanguage,prompt:english,answer:korean,aliases:[korean]};
  return {category,value,answerLanguage,prompt:korean,answer:english,aliases};
}
function normalized(value:string, q:Question):string {
  const text=value.normalize('NFC').trim().replace(/\s+/gu,' ');
  if(q.answerLanguage==='english')return text.toLowerCase().replace(/[’‘]/gu,"'").replace(/-/gu,' ').replace(/\s+/gu,' ');
  // Only the boundary before a category's unit may be spaced; number spelling is exact.
  const unit={sino:'',native:'',months:'월',dates:'일',hours:'시',minutes:'분'}[q.category];
  return unit ? text.replace(new RegExp(' ?'+unit+'$','u'),unit) : text;
}
export function accepts(q:Question, answer:string):boolean {
  return answer.length<=256&&q.aliases.some(alias=>normalized(alias,q)===normalized(answer,q));
}
export function nextQuestion(p:StudyPreferences, previous?:Question, random=Math.random):Question {
  if(!validPreferences(p))throw new RangeError('Invalid practice settings');
  const count=p.maximum-p.minimum+1;
  let value=p.minimum+Math.min(count-1,Math.max(0,Math.floor(random()*count)));
  if(count>1&&previous?.category===p.category&&value===previous.value)value=p.minimum+(value-p.minimum+1)%count;
  return question(p.category,value,p.answer_language);
}

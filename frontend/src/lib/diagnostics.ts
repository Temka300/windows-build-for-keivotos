// Fixed diagnostic facts only. Never send exception text, stacks, URLs or bodies.
type Component = 'suite'|'files'|'danbooru'|'youtube'|'twitter'|'video'|'manga'|'ost'|'language'|'unknown';
type Reason = 'network'|'decode'|'unsupported_format'|'unsupported_codec'|'playback'|'fullscreen'|'reader';
type Code = 'script_error'|'unhandled_rejection'|'request_failed'|'media_failed'|'reader_failed';
type ErrorType = 'Error'|'TypeError'|'RangeError'|'ReferenceError'|'SyntaxError'|'URIError'|'DOMException'|'NetworkError';
interface Facts {component?:Component; operation_id?:string; error_type?:ErrorType; status?:number; line?:number; column?:number; reason?:Reason;}
let enabled=false;
let actionId:string|undefined;
const recent=new Map<string,number>();
let sent:number[]=[];
const identityPattern=/^(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$/;

function errorType(value:unknown):ErrorType {
  try {
    const name=(value as Error)?.name;
    return ['Error','TypeError','RangeError','ReferenceError','SyntaxError','URIError','DOMException','NetworkError'].includes(name)?name as ErrorType:'Error';
  } catch { return 'Error'; }
}
function componentFor(path:string):Component {
  const name=path.split('/')[2];
  return ['danbooru','youtube','twitter','video','manga','ost','language'].includes(name)?name as Component:
    path.startsWith('/api/files')?'files':'suite';
}

export function reportDiagnostic(code:Code, facts:Facts={}):void {
  if(!enabled)return;
  const now=Date.now(),key=[code,facts.component,facts.status,facts.reason,facts.error_type].join(':');
  if(now-(recent.get(key)??0)<2000)return;
  sent=sent.filter(time=>now-time<60000);
  if(sent.length>=20)return;
  for(const [name,time] of recent)if(now-time>60000)recent.delete(name);
  if(recent.size>=32)recent.delete(recent.keys().next().value!);
  recent.set(key,now);sent.push(now);
  // Explicitly pick fields even if a caller accidentally supplies extra properties.
  const body={
    code,component:facts.component??'suite',
    operation_id:facts.operation_id&&identityPattern.test(facts.operation_id)?facts.operation_id:actionId??crypto.randomUUID(),
    error_type:facts.error_type??'Error',status:facts.status,
    line:Number.isInteger(facts.line)&&facts.line!>=0?facts.line:undefined,
    column:Number.isInteger(facts.column)&&facts.column!>=0?facts.column:undefined,
    reason:facts.reason,
  };
  try {
    void fetch('/api/diagnostics/frontend',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body),keepalive:true}).catch(()=>{});
  } catch { /* Reporting failure must not alter the initiating action. */ }
}

export async function diagnosticFetch(input:RequestInfo|URL, init?:RequestInit):Promise<Response> {
  if(!enabled)return fetch(input,init);
  let url:URL;
  try {url=new URL(typeof input==='object'&&'url' in input?input.url:String(input),window.location.origin);}
  catch {return fetch(input,init);}
  if(url.origin!==window.location.origin||!url.pathname.startsWith('/api/')||url.pathname==='/api/diagnostics/frontend')return fetch(input,init);
  const operationId=actionId??crypto.randomUUID();
  const headers=new Headers(init?.headers??(input instanceof Request?input.headers:undefined));
  headers.set('X-Keivotos-Operation-ID',operationId);
  try {
    const response=await fetch(input,{...init,headers});
    if(!response.ok)reportDiagnostic('request_failed',{component:componentFor(url.pathname),
      operation_id:response.headers.get('X-Keivotos-Operation-ID')??operationId,status:response.status});
    return response;
  } catch(error) {
    if(!(error instanceof DOMException&&error.name==='AbortError'))
      reportDiagnostic('request_failed',{component:componentFor(url.pathname),operation_id:operationId,error_type:errorType(error)});
    throw error;
  }
}

export function installDiagnostics():()=>void {
  if(enabled)return ()=>{};
  enabled=true;
  const error=(event:ErrorEvent)=>reportDiagnostic('script_error',{error_type:errorType(event.error),line:event.lineno,column:event.colno});
  const rejection=(event:PromiseRejectionEvent)=>{
    if(event.reason instanceof DOMException&&event.reason.name==='AbortError')return;
    reportDiagnostic('unhandled_rejection',{error_type:errorType(event.reason)});
  };
  window.addEventListener('error',error);
  window.addEventListener('unhandledrejection',rejection);
  return ()=>{
    enabled=false;recent.clear();sent=[];
    window.removeEventListener('error',error);
    window.removeEventListener('unhandledrejection',rejection);
  };
}


export function diagnosticAction<T>(action:(operationId:string)=>T):T {
  const previous=actionId;
  const identity=crypto.randomUUID();
  actionId=identity;
  try {return action(identity);}
  finally {actionId=previous;}
}

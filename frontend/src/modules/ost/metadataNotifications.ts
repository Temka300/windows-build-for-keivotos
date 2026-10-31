import {get,writable} from 'svelte/store';
import {enabledModules,folderRescanChanges,suiteModules} from '../../lib/suiteStores';
import {suiteApi} from '../../lib/suiteApi';
import {ApiError} from '../../lib/http';
import {notify,notifications,updateNotification} from '../../lib/notifications';
import {ostApi,type MetadataJob} from './api';
import {clearMusicLibraryState,enableMusicLibraryState} from './libraryState';
export const metadataOperations=writable<MetadataJob[]>([]);
export const metadataChanges=writable(0);
export const metadataActive=(job:MetadataJob)=>['requested','started','working'].includes(job.state);
const seen=new Map<string,string>();
let enabled=false,started=0,timer:ReturnType<typeof setTimeout>|undefined,request:AbortController|null=null;
let generation=0,ownerGeneration=0,failures=0;
const noticeId=(id:string)=>'ost-metadata-'+id;
function observe(job:MetadataJob){
  const prior=seen.get(job.id),active=metadataActive(job),id=noticeId(job.id);
  const recent=Date.parse(job.created_at)>=started-1000;
  if(!active&&!prior&&!recent)return;
  if(prior===job.state&&!active)return;
  const activity=job.discover?'Refreshing metadata':'Reading music details';
  const count=job.total===null?'':` ${job.processed}/${job.total} tracks · ${Math.round(job.progress)}%`;
  const message=`${activity}…${count}`;
  if(active){
    const values={message,tone:'info' as const,progress:job.total===null?undefined:job.progress,pending:true};
    if(prior)updateNotification(id,values);else notify(message,{id,...values});
  }else{
    // Dismissal is respected; completion never resurrects a dismissed operation.
    if(!prior||get(notifications).some(item=>item.id===id)){
      const completed=job.state==='completed';
      const text=completed?`Metadata refreshed for ${job.processed} tracks.`:
        job.state==='completed-with-warnings'?`Metadata refreshed: ${job.processed} tracks, ${job.failed} could not be read. Use Refresh metadata to retry.`:
        'Metadata refresh stopped. Use Refresh metadata to retry.';
      notify(text,{id,tone:completed?'success':'error'});
    }
    metadataChanges.update(value=>value+1);
  }
  seen.set(job.id,job.state);
  if(seen.size>128)seen.delete(seen.keys().next().value!);
}
function stopMissing(ids:Set<string>){
  for(const [id,state] of seen){
    if(!['requested','started','working'].includes(state)||ids.has(id))continue;
    if(get(notifications).some(item=>item.id===noticeId(id)))notify('Metadata refresh stopped.',{id:noticeId(id)});
    seen.set(id,'unavailable');metadataChanges.update(value=>value+1);
  }
}
function cancelPoll(){
  generation++;clearTimeout(timer);timer=undefined;request?.abort();request=null;
}
function unavailable(){
  metadataOperations.set([]);
  for(const [id,state] of seen){
    if(!['requested','started','working'].includes(state))continue;
    if(get(notifications).some(item=>item.id===noticeId(id)))
      notify('Metadata progress unavailable. Use Refresh metadata to check again.',{id:noticeId(id),tone:'error'});
    seen.set(id,'unavailable');
  }
}
async function reconcile(version:number){
  try{
    const modules=await suiteApi.listModules();
    if(!enabled||version!==generation)return;
    suiteModules.set(modules);enabledModules.set(modules.filter(module=>module.enabled).map(module=>module.id));
  }catch{ /* One state read only; a conflict must not create another retry loop. */ }
  if(enabled&&version===generation)unavailable();
}
async function poll(version:number){
  if(!enabled||version!==generation)return;
  const current=new AbortController();request=current;
  let delay:number|undefined;
  try{
    const result=await ostApi.metadataJobs(current.signal);
    if(!enabled||current.signal.aborted||version!==generation)return;
    failures=0;
    metadataOperations.set(result.items);result.items.forEach(observe);stopMissing(new Set(result.items.map(job=>job.id)));
    if(result.items.some(metadataActive))delay=500;
  }catch(error){
    if(!enabled||current.signal.aborted||version!==generation)return;
    if(error instanceof ApiError&&error.status===409){await reconcile(version);return;}
    const temporary=!(error instanceof ApiError)||error.status===408||error.status===429||error.status>=500;
    if(temporary&&failures<3)delay=1000*2**failures++;
    else unavailable();
  }finally{
    if(request===current)request=null;
    if(enabled&&!current.signal.aborted&&version===generation&&delay!==undefined)
      timer=setTimeout(()=>{timer=undefined;void poll(version);},delay);
  }
}
function wake(){
  if(!enabled)return;
  cancelPoll();failures=0;void poll(generation);
}
export async function startMetadataRefresh(sourceId:string,parent:string){
  const owner=ownerGeneration;
  const job=await ostApi.refreshMetadata(sourceId,parent);
  if(enabled&&owner===ownerGeneration){
    // Acceptance is newer than any status request already in flight.
    metadataOperations.update(rows=>[job,...rows.filter(row=>row.id!==job.id)]);observe(job);
    wake();
  }
  return job;
}
export function observeMusicMetadata(){
  const stopEnabled=enabledModules.subscribe(ids=>{
    const next=ids.includes('ost');if(next===enabled)return;enabled=next;
    ownerGeneration++;cancelPoll();
    if(enabled){enableMusicLibraryState();started=Date.now();wake();}
    else{metadataOperations.set([]);stopMissing(new Set());seen.clear();clearMusicLibraryState();}
  });
  let rescans=get(folderRescanChanges);
  const stopRescans=folderRescanChanges.subscribe(value=>{
    if(value===rescans)return;rescans=value;wake();
  });
  return ()=>{stopEnabled();stopRescans();enabled=false;ownerGeneration++;cancelPoll();metadataOperations.set([]);stopMissing(new Set());seen.clear();clearMusicLibraryState();};
}

import {get,post} from '../../lib/http';

export interface Capture {
  source_id:string;source_name:string;path:string;post_id:string;capture_id:string;
  source_url:string;captured_at:string;completed_at:string;posted_at:string|null;raw_text:string;
  author:{id:string|null;name:string;handle:string};author_key:string;
  avatar_path:string;video_path:string|null;photos:{index:number;path:string;name:string}[];available:boolean;unavailable_assets:string[];
  files:Record<string,{size:number;sha256:string}>;
}
export interface ArchivePage {items:Capture[];total:number;next_offset:number|null;invalid:number;}
export interface TwitterFolder {source_id:string;name:string;path:string;available:boolean;}
export interface Preferences {source_id:string;saved_source_id:string;available:boolean;dependencies:{ready:boolean;missing:string[]};}
export interface Download {
  id:string;request_id:string;url:string;post_id:string;source_id:string;
  status:'queued'|'running'|'complete'|'failed'|'interrupted'|'cancelled';
  phase:string;progress:number;attempt:number;capture_id:string;path:string;error:string;
  created_at:string;updated_at:string;
}
export const captureKey=(item:Capture)=>item.source_id+':'+item.path;
export const avatarUnavailable=(item:Capture)=>item.unavailable_assets.includes(item.avatar_path.slice(item.avatar_path.lastIndexOf('/')+1));
export function postUrl(value:string):string|null {
  const raw=value.trim();
  if(!raw||raw.length>2048||/[\x00-\x20\\]/.test(raw))return null;
  try {
    const url=new URL(raw);
    if(url.protocol!=='https:'||!['x.com','www.x.com','twitter.com','www.twitter.com'].includes(url.hostname)||url.username||url.password||url.port&&url.port!=='443')return null;
    if(!/^\/(?:[A-Za-z0-9_]{1,64}|i\/web)\/status\/[1-9][0-9]{0,21}\/?$/.test(url.pathname))return null;
    return 'https://x.com'+url.pathname.replace(/\/$/,'');
  }catch{return null;}
}
export const twitterApi={
  folders:(signal?:AbortSignal)=>get<TwitterFolder[]>('/twitter/folders',undefined,signal),
  settings:(signal?:AbortSignal)=>get<Preferences>('/twitter/settings',undefined,signal),
  saveSettings:(source_id:string)=>post<Preferences>('/twitter/settings',{source_id}),
  library:(offset=0,q='',author='',signal?:AbortSignal)=>get<ArchivePage>('/twitter/library',{offset,limit:120,q,author},signal),
  capture:(source_id:string,path:string,signal?:AbortSignal)=>get<Capture>('/twitter/capture',{source_id,path},signal),
  mediaUrl:(item:Capture,asset:'avatar'|'video'|'photo',photo=1)=>'/api/twitter/media?'+new URLSearchParams({source_id:item.source_id,path:item.path,asset,...(asset==='photo'?{photo:String(photo)}:{})}),
  job:(id:string,signal?:AbortSignal)=>get<Download>(`/twitter/downloads/${encodeURIComponent(id)}`,undefined,signal),
  download:(url:string,source_id:string,request_id:string)=>post<Download>('/twitter/downloads',{url,source_id,request_id}),
};

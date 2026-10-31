import {diagnosticFetch} from '../../lib/diagnostics';
import {apiError} from '../../lib/http';
import {get, post} from '../../lib/http';
export interface VideoItem { source_id:string; path:string; name:string; ext:string; size:number; mtime:number; }
export interface VideoEpisode extends VideoItem { section:string; }
export interface VideoTitle extends VideoItem { kind:'series'|'video'; cover:VideoItem; episode_count:number; }
import type {PlaybackFailure} from '../../lib/playback';
import type {LibraryCategory,CategoryItem,CategoryMembership} from '../registry';
export interface WatchSubject {source_id:string;path:string;}
export interface WatchPosition {revision:string;position:number;duration:number;ended:boolean;generation:number;sequence:number;updated_at:string;}
export interface WatchRead {revision:string;generation:number;position:WatchPosition|null;}
export interface WatchUpdate extends WatchSubject {revision:string;generation:number;sequence:number;position:number;duration:number;ended:boolean;}
async function watchPost<T>(path:string,body:unknown,keepalive=false):Promise<T>{
  const response=await diagnosticFetch('/api/video/progress'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),keepalive,signal:AbortSignal.timeout(5000)});
  if(!response.ok)throw await apiError(response);return response.json();
}
export const videoDetailFields=['summary','tags'] as const;
export type VideoDetailField=typeof videoDetailFields[number];
export type VideoFields=Partial<Record<VideoDetailField,string|string[]|null>>;
export interface VideoDetailSubject {source_id:string;path:string;kind:'series'|'season'|'video';}
export interface VideoDetails {subject:VideoDetailSubject;fields:VideoFields;local_fields:VideoFields;overrides:VideoFields;version:number;source_revision:string;}
export interface VideoDetailSave extends VideoDetailSubject {fields:VideoFields;version:number;source_revision:string;request_id:string;}
export const videoApi = {
  details:(subject:VideoDetailSubject,signal?:AbortSignal)=>get<VideoDetails>('/video/metadata',{...subject},signal),
  refreshDetails:(subject:VideoDetailSubject)=>post<VideoDetails>('/video/metadata/refresh',subject),
  saveDetails:(payload:VideoDetailSave)=>post<VideoDetails>('/video/metadata/overrides',payload),
  watch:{
    read:(item:WatchSubject)=>watchPost<WatchRead>('/read',item),
    begin:(item:WatchSubject,revision:string,expected_generation:number)=>watchPost<{generation:number;revision:string}>('/session',{...item,revision,expected_generation}),
    positions:(items:WatchSubject[])=>watchPost<{items:(WatchSubject&{position:WatchPosition|null})[]}>('/positions',{items}),
    save:(payload:WatchUpdate,keepalive=false)=>watchPost<WatchPosition>('',payload,keepalive),
  },
  categories: {
    list:(offset=0,signal?:AbortSignal)=>get<{items:LibraryCategory[];total:number}>('/video/categories',{offset,limit:120},signal),
    create:(name:string,request_id:string)=>post<LibraryCategory>('/video/categories',{name,request_id}),
    membership:(item:CategoryItem,signal?:AbortSignal)=>get<CategoryMembership>('/video/category-membership',{source_id:item.source_id,path:item.path},signal),
    saveMembership:(item:CategoryItem,category_ids:string[],version:number,request_id:string)=>post<CategoryMembership>('/video/category-membership',{source_id:item.source_id,path:item.path,category_ids,version,request_id}),
  },
  library:(q:string, offset=0, signal?:AbortSignal,category?:string)=>get<{items:VideoItem[];total:number}>('/video/library',{q,offset,limit:120,...(category?{category}:{})},signal),
  catalog:(q:string,offset=0,signal?:AbortSignal,category?:string)=>get<{items:VideoTitle[];total:number}>('/video/catalog',{q,offset,limit:120,...(category?{category}:{})},signal),
  episodes:(title:VideoTitle,q:string,offset=0,signal?:AbortSignal,category?:string)=>get<{items:VideoEpisode[];total:number}>('/video/episodes',{source_id:title.source_id,path:title.path,kind:title.kind,q,offset,limit:120,...(category?{category}:{})},signal),
  random:(q:string,item:VideoItem,signal?:AbortSignal,category?:string,title?:VideoTitle)=>get<{item:VideoItem|null;offset:number|null;total:number}>('/video/random',{q,current_source_id:item.source_id,current_path:item.path,...(category?{category}:{}),...(title?.kind==='series'?{series_source_id:title.source_id,series_path:title.path}:{})},signal),
  error:(item:VideoItem, reason:PlaybackFailure)=>post('/video/playback-error',{source_id:item.source_id,path:item.path,reason}),
};

import {get,post} from '../../lib/http';
import type {LibraryCategory,CategoryItem,CategoryMembership} from '../registry';
export interface Chapter {path:string; kind:'folder'|'archive'; name:string;}
export interface Manga {source_id:string; path:string; name:string; cover:Chapter; cover_revision?:string|null;}
export interface PageMetadata {count:number; revision:string; revisions:string[];}
export const mangaDetailFields=['summary','tags'] as const;
export type MangaDetailField=typeof mangaDetailFields[number];
export interface MangaFields {summary?:string|null;tags?:string[]|null;}
export interface MangaDetailSubject {source_id:string;path:string;}
export interface MangaDetails {subject:MangaDetailSubject;fields:MangaFields;local_fields:MangaFields;overrides:MangaFields;version:number;source_revision:string;}
export interface MangaDetailSave extends MangaDetailSubject {fields:MangaFields;version:number;source_revision:string;request_id:string;}

export interface ReadingPosition {source_id:string;series_path:string;path:string;kind:'folder'|'archive';revision:string;page_count:number;page:number;mode:'paged'|'vertical';anchor:number;generation:number;sequence:number;updated_at:string;}
export interface ReadingState {generation:number;position:ReadingPosition|null;}
export const progressIdentity=(manga:Manga,chapter:Chapter,revision:string)=>({source_id:manga.source_id,series_path:manga.path,path:chapter.path,kind:chapter.kind,revision});

/** Keep a small image window alive so native image requests can reuse it. */
export function createPagePrefetch(){
  type Entry={image:HTMLImageElement;bytes:number;ready:boolean};
  const retained=new Map<string,Entry>(),attempted=new Set<string>();
  const budget=32*1024*1024;
  let adjacent:string[]=[],visibleReady=false,active:string|null=null,bytes=0;
  function release(url:string){
    const entry=retained.get(url);if(!entry)return;
    retained.delete(url);bytes-=entry.bytes;
    entry.image.onload=null;entry.image.onerror=null;entry.image.removeAttribute('src');
    if(active===url)active=null;
  }
  function pump(){
    if(!visibleReady||active)return;
    const url=adjacent.find(candidate=>!attempted.has(candidate));if(!url)return;
    attempted.add(url);active=url;
    const image=new Image(),entry:Entry={image,bytes:0,ready:false};retained.set(url,entry);
    image.fetchPriority='low';image.decoding='async';
    const finish=(ready:boolean)=>{
      // A late load/decode after cancellation cannot start work for an old window.
      if(retained.get(url)!==entry)return;
      if(ready)entry.ready=true;else release(url);
      active=null;pump();
    };
    image.onerror=()=>finish(false);
    image.onload=()=>{
      const size=image.naturalWidth*image.naturalHeight*4;
      if(!size||bytes+size>budget){finish(false);return;}
      entry.bytes=size;bytes+=size;
      void image.decode().then(()=>finish(true),()=>finish(false));
    };
    image.src=url;
  }
  return {
    update(current:string,neighbors:string[],ready:boolean){
      adjacent=[...new Set(neighbors)].filter(candidate=>candidate!==current).slice(0,3);
      visibleReady=ready;
      const wanted=new Set([current,...adjacent]);
      for(const old of retained.keys())if(!wanted.has(old)||(!ready&&old!==current&&!retained.get(old)!.ready)){
        release(old);attempted.delete(old);
      }
      for(const old of attempted)if(!wanted.has(old))attempted.delete(old);
      if(ready)attempted.add(current);
      pump();
    },
    clear(){
      adjacent=[];visibleReady=false;
      for(const url of retained.keys())release(url);
      attempted.clear();
    },
  };
}
export const mangaApi = {
  details:(subject:MangaDetailSubject,signal?:AbortSignal)=>get<MangaDetails>('/manga/metadata',{source_id:subject.source_id,path:subject.path},signal),
  refreshDetails:(subject:MangaDetailSubject)=>post<MangaDetails>('/manga/metadata/refresh',{source_id:subject.source_id,path:subject.path}),
  saveDetails:(payload:MangaDetailSave)=>post<MangaDetails>('/manga/metadata/overrides',payload),
  categories: {
    list:(offset=0,signal?:AbortSignal)=>get<{items:LibraryCategory[];total:number}>('/manga/categories',{offset,limit:120},signal),
    create:(name:string,request_id:string)=>post<LibraryCategory>('/manga/categories',{name,request_id}),
    membership:(item:CategoryItem,signal?:AbortSignal)=>get<CategoryMembership>('/manga/category-membership',{source_id:item.source_id,path:item.path},signal),
    saveMembership:(item:CategoryItem,category_ids:string[],version:number,request_id:string)=>post<CategoryMembership>('/manga/category-membership',{source_id:item.source_id,path:item.path,category_ids,version,request_id}),
  },
  progress:(manga:Manga,chapter:Chapter,revision:string)=>post<ReadingState>('/manga/progress/read',progressIdentity(manga,chapter,revision)),
  beginProgress:(manga:Manga,chapter:Chapter,revision:string,expected_generation:number)=>post<{generation:number}>('/manga/progress/session',{...progressIdentity(manga,chapter,revision),expected_generation}),
  saveProgress:(position:Omit<ReadingPosition,'page_count'|'updated_at'>)=>post<ReadingPosition>('/manga/progress',position),
  seriesProgress:(manga:Manga,offset=0,signal?:AbortSignal)=>get<{items:ReadingPosition[];total:number}>('/manga/progress/series',{source_id:manga.source_id,series_path:manga.path,offset,limit:120},signal),
  library:(q:string,offset=0,signal?:AbortSignal,category?:string)=>get<{items:Manga[];total:number}>('/manga/library',{q,offset,...(category?{category}:{})},signal),
  chapters:(manga:Manga,signal?:AbortSignal)=>get<{items:Chapter[]}>('/manga/chapters',{source_id:manga.source_id,path:manga.path},signal),
  pages:(manga:Manga,chapter:Chapter,signal?:AbortSignal)=>get<PageMetadata>('/manga/pages',{source_id:manga.source_id,path:chapter.path,kind:chapter.kind,metadata:true},signal),
  page:(manga:Manga,chapter:Chapter,page=0,cover=false,revision?:string,representation:'original'|'webp'='original')=>{
    const parameters=new URLSearchParams({source_id:manga.source_id,path:chapter.path,kind:chapter.kind,page:String(page),cover:String(cover),representation:cover?'webp':representation});
    const version=cover?manga.cover_revision:revision;
    if(version)parameters.set('revision',version);
    return '/api/manga/page?'+parameters;
  },
};

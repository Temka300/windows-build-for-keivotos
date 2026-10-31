import {get,post} from '../../lib/http';
import type {AudioMetadata} from '../../lib/filesApi';

export interface OstSource{source_id:string;name:string;path:string;available:boolean;cover_version:string;}
export interface OstItem{source_id:string;path:string;name:string;ext:string;is_dir:boolean;size:number|null;mtime:number;available:boolean;cover_version:string;audio_metadata:AudioMetadata|null;}
export interface OstDirectory{source_id:string;parent:string;items:OstItem[];total:number;}
export interface OstTrack extends OstItem{source_name:string;parent:string;}
export interface MusicFacts{title:string;artists:string[];album:string;album_artists:string[];track:number|null;disc:number|null;duration:number|null;status:AudioMetadata['status'];}
export interface MusicTrack extends OstTrack{music:MusicFacts;disc_label?:string;}
export interface MusicAlbum{id:string;source_id:string;name:string;artists:string[];anchor:string;pending:boolean;total:number;covers:{path:string;version:string}[];}
export interface MusicAlbums{items:MusicAlbum[];total:number;}
export interface AlbumTracks{album:MusicAlbum;items:MusicTrack[];total:number;}
export interface OstTracks{items:OstTrack[];total:number;}
export function musicTitle(item:OstItem):string{return (item as MusicTrack).music?.title??item.audio_metadata?.display_title??item.name.replace(/\.[^.]+$/,'');}
export function musicDuration(value:number|null|undefined):string{if(value==null||!Number.isFinite(value))return '—';const seconds=Math.max(0,Math.floor(value));return `${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`;}
export function musicFacts(item:OstItem,metadata:AudioMetadata):MusicFacts{
  const texts=(name:string)=>(metadata.tags[name]??[]).map(value=>value.trim()).filter(Boolean);
  const number=(name:string)=>{const value=Number(texts(name)[0]?.split('/')[0]);return Number.isInteger(value)&&value>0?value:null;};
  return {title:metadata.display_title,artists:texts('artist'),album:texts('album')[0]??'',album_artists:texts('albumartist'),track:number('track'),disc:number('disc'),duration:metadata.duration,status:metadata.status};
}
export interface MetadataJob{id:string;source_id:string;label:string;state:string;progress:number;processed:number;total:number|null;failed:number;discover:boolean;created_at:string;}
export const ostApi={
  refreshMetadata:(sourceId:string,parent='')=>post<MetadataJob>('/ost/metadata-refresh',{source_id:sourceId,parent}),
  metadataJobs:(signal?:AbortSignal)=>get<{items:MetadataJob[]}>('/ost/metadata-jobs',undefined,signal),
  metadata:(sourceId:string,path:string,signal?:AbortSignal)=>get<AudioMetadata>('/ost/metadata',{source_id:sourceId,path},signal),
  folders:(signal?:AbortSignal)=>get<OstSource[]>('/ost/folders',undefined,signal),
  library:(sourceId:string,parent='',offset=0,signal?:AbortSignal)=>get<OstDirectory>('/ost/library',{source_id:sourceId,parent,offset,limit:120},signal),
  tracks:(offset=0,signal?:AbortSignal,q='')=>get<{items:MusicTrack[];total:number}>('/ost/tracks',{offset,limit:120,q},signal),
  albums:(offset=0,signal?:AbortSignal,q='')=>get<MusicAlbums>('/ost/albums',{offset,limit:120,q},signal),
  albumTracks:(sourceId:string,anchor:string,offset=0,signal?:AbortSignal,prepare=false)=>get<AlbumTracks>('/ost/album-tracks',{source_id:sourceId,anchor,offset,limit:120,prepare},signal),
  coverUrl:(sourceId:string,path:string,version:string,size=300)=>{
    const url=new URL('/api/ost/cover',window.location.origin);
    url.searchParams.set('source_id',sourceId);url.searchParams.set('path',path);
    url.searchParams.set('size',String(size));url.searchParams.set('v',version);
    return url.toString();
  },
};

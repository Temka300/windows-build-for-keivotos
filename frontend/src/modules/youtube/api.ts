import type {PlaybackFailure} from '../../lib/playback';
import {get,post} from '../../lib/http';
export interface Download {id:string;url:string;source_id:string;title:string;status:string;progress:number;path:string;thumbnail:string;error:string;retry_of?:string;superseded_by?:string;failure_phase?:string;video?:string;}
export interface EngineStatus {ready:boolean;missing:string[];folders:{id:string;name:string}[];}
export interface SaveLocation {mode:'default'|'custom';path:string;source_id:string;default_path:string;}
export interface Playlist {id:string;title:string;created_at:string;item_count:number;revision:number;deleted_at:string;}
export interface PlaylistItem {job_id:string;position:number;available:boolean;unavailable_reason:string;job:Download|null;}
export interface PlaylistMembership {version:number;playlist_ids:string[];}
export const youtubeApi={
 playlists:(offset=0,signal?:AbortSignal,limit=60)=>get<{items:Playlist[];total:number}>('/youtube/playlists',{offset,limit},signal),
 createPlaylist:(title:string,request_id:string)=>post<Playlist>('/youtube/playlists',{title,request_id}),
 renamePlaylist:(id:string,title:string,revision:number,request_id:string)=>post<Playlist>(`/youtube/playlists/${encodeURIComponent(id)}/rename`,{title,revision,request_id}),
 archivePlaylist:(id:string,revision:number,item_count:number,request_id:string)=>post<Playlist>(`/youtube/playlists/${encodeURIComponent(id)}/archive`,{revision,item_count,request_id}),
 playlistItems:(id:string,offset=0,signal?:AbortSignal,limit=60)=>get<{playlist:Playlist;items:PlaylistItem[];total:number}>(`/youtube/playlists/${encodeURIComponent(id)}/items`,{offset,limit},signal),
 membership:(jobId:string,signal?:AbortSignal)=>get<PlaylistMembership>(`/youtube/videos/${encodeURIComponent(jobId)}/playlists`,undefined,signal),
 saveMembership:(jobId:string,playlist_ids:string[],version:number)=>post<PlaylistMembership>(`/youtube/videos/${encodeURIComponent(jobId)}/playlists`,{playlist_ids,version}),
 playbackError:(id:string,reason:PlaybackFailure)=>post('/youtube/playback-error',{id,reason}),
 settings:()=>get<SaveLocation>('/youtube/settings'),
 saveSettings:(mode:'default'|'custom',path?:string)=>post<SaveLocation>('/youtube/settings',{mode,path}),
 status:()=>get<EngineStatus>('/youtube/status'),
 library:(q:string,offset=0,signal?:AbortSignal,limit=60)=>get<{items:Download[];total:number}>('/youtube/library',{q,offset,limit},signal),
 download:(url:string,video:string,audio:string)=>post<Download>('/youtube/downloads',{url,video,audio}),
 cancel:(id:string)=>post<Download>(`/youtube/downloads/${id}/cancel`),
 retry:(id:string)=>post<Download>(`/youtube/downloads/${id}/retry`),
};

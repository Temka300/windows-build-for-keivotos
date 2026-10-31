import type {OstSource,OstItem,MusicTrack,MusicAlbum} from './api';
export type Position={count:number;scroll:number;focus:string;selected:string};
export type Section='folders'|'library'|'albums';
export interface MusicLibraryState{
  sources:OstSource[];source:OstSource|null;parent:string;items:OstItem[];total:number;selected:string;
  section:Section;tracks:MusicTrack[];trackTotal:number;trackOffset:number;
  albums:MusicAlbum[];albumTotal:number;album:MusicAlbum|null;albumItems:MusicTrack[];albumTrackTotal:number;
  searchDraft:string;searchQuery:string;queries:{library:string;albums:string};positions:[string,Position][];
}
// Browser-session view memory, separate from audio/queues and persistent metadata.
// No timers, media nodes or in-flight requests survive a surface's destruction.
let saved:MusicLibraryState|null=null,enabled=true;
export function enableMusicLibraryState(){enabled=true;}
export function musicLibraryState(){return saved?structuredClone(saved):null;}
export function saveMusicLibraryState(value:MusicLibraryState){if(enabled)saved=structuredClone({...value,positions:value.positions.slice(-64)});}
export function clearMusicLibraryState(){saved=null;enabled=false;}

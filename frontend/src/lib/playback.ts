export interface PlaybackMedia {src:string;ext:string;}
export type PlaybackFailure = 'unsupported_format' | 'network' | 'decode' | 'unsupported_codec' | 'playback' | 'fullscreen';

export interface PlaybackObservation {position:number;duration:number;ended:boolean;}
export interface PlaybackPosition {
  initialPosition:number;
  observe:(value:PlaybackObservation)=>void;
  flush:(keepalive?:boolean)=>Promise<boolean>;
}

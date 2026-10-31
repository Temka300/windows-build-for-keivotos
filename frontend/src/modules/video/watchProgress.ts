import type {PlaybackObservation,PlaybackPosition} from '../../lib/playback';
import type {WatchSubject,WatchRead,WatchUpdate,WatchPosition} from './api';
export interface WatchTransport {
  read:(item:WatchSubject)=>Promise<WatchRead>;
  begin:(item:WatchSubject,revision:string,expected:number)=>Promise<{generation:number;revision:string}>;
  save:(value:WatchUpdate,keepalive?:boolean)=>Promise<WatchPosition>;
}
export interface WatchSession extends PlaybackPosition {retry:()=>Promise<boolean>;dispose:()=>void;}
// One immutable subject/session owns its serialized writes. UI changes cannot retarget them.
export async function openWatch(item:WatchSubject,transport:WatchTransport,owns:()=>boolean,
  acknowledged:(value:WatchPosition)=>void,failed:(error:unknown|null)=>void):Promise<WatchSession|null>{
  const subject={source_id:item.source_id,path:item.path};
  const saved=await transport.read(subject);if(!owns())return null;
  const session=await transport.begin(subject,saved.revision,saved.generation);if(!owns())return null;
  let sequence=0,lastAcknowledged=0,pending:WatchUpdate|null=null,retryValue:WatchUpdate|null=null;
  let busy:Promise<boolean>|null=null,timer:ReturnType<typeof setTimeout>|undefined,disposed=false,blocked=false;
  function confirm(value:WatchPosition){if(value.generation===session.generation&&value.sequence>lastAcknowledged){lastAcknowledged=value.sequence;acknowledged(value);}}
  const clear=()=>{if(timer!==undefined){clearTimeout(timer);timer=undefined;}};
  async function drain():Promise<boolean>{
    if(busy)return busy;
    busy=(async()=>{
      while((retryValue||pending)&&!blocked){
        const value=retryValue??pending!;if(value===pending)pending=null;retryValue=null;
        try{const confirmed=await transport.save(value);confirm(confirmed);failed(null);}
        catch(error){retryValue=value;blocked=true;failed(error);return false;}
      }
      return !blocked;
    })();
    try{return await busy;}finally{busy=null;}
  }
  async function flush(keepalive=false):Promise<boolean>{
    clear();
    if(keepalive){
      // Termination is best effort. A later sequence may arrive first; the server rejects older writes.
      const value=pending??retryValue;
      if(value){try{const confirmed=await transport.save(value,true);confirm(confirmed);return true;}catch{return false;}}
    }
    return drain();
  }
  return {
    initialPosition:saved.position&&!saved.position.ended?saved.position.position:0,
    observe(value:PlaybackObservation){
      if(disposed||!Number.isFinite(value.position)||!Number.isFinite(value.duration)||value.duration<=0||value.position<0||value.position>value.duration)return;
      pending={...subject,revision:session.revision,generation:session.generation,sequence:++sequence,...value};
      if(!blocked&&timer===undefined)timer=setTimeout(()=>{timer=undefined;void drain();},3000);
    },
    flush,
    async retry(){blocked=false;return flush();},
    dispose(){disposed=true;clear();void flush(true);},
  };
}

<script lang="ts">
  import {onMount,onDestroy,tick} from 'svelte';
  import AppDrawer from '../../components/AppDrawer.svelte';
  import ModuleLocationMenu from '../../components/ModuleLocationMenu.svelte';
  import ModuleSidebarDock from '../../components/ModuleSidebarDock.svelte';
  import {settingsOpen} from '../../lib/suiteStores';
  import {notify} from '../../lib/notifications';
  import {musicLibraryState,saveMusicLibraryState,type Position,type Section} from './libraryState';
  import {metadataOperations,metadataChanges,metadataActive,startMetadataRefresh} from './metadataNotifications';
  import {ostApi,musicTitle,type OstSource,type OstItem,type OstTrack,type MusicTrack,type MusicAlbum} from './api';
  import AudioPlayer from './AudioPlayer.svelte';
  import {musicShuffle} from '../../lib/playbackPreferences';
  import {createShuffleQueue} from './shuffle';
  import OstSidebar from './OstSidebar.svelte';
  import AlbumView from './AlbumView.svelte';
  import MusicTrackRow from './MusicTrackRow.svelte';
  let drawer=false,loading=false,sourceLoading=true,error='',sourceError='',notice='';
  let sources:OstSource[]=[];
  let source:OstSource|null=null,parent='',items:OstItem[]=[],total=0,selected='';
  let section:Section='library',tracks:MusicTrack[]=[],trackTotal=0,trackOffset=0,trackLoading=false,trackError='',trackRetryMore=false;
  let albums:MusicAlbum[]=[],albumTotal=0,album:MusicAlbum|null=null,albumItems:MusicTrack[]=[],albumTrackTotal=0,albumLoading=false,albumError='';
  let albumRequest:AbortController|null=null;
  let searchDraft='',searchQuery='',searchComposing=false;
  let refreshingFolder=false,refreshSubmitting=false;
  $: refreshingFolder=refreshSubmitting||$metadataOperations.some(metadataActive);
  let searchTimer:ReturnType<typeof setTimeout>|undefined;
  const queries={library:'',albums:''};
  let trackRequest:AbortController|null=null;
  let main:HTMLElement;
  let request:AbortController|null=null,sourceRequest:AbortController|null=null;
  let alive=true;
  let navigationSerial=0;
  let retryMore=false;
  let missingArt=new Set<string>();
  const positions=new Map<string,Position>();
  const saved=musicLibraryState();
  let viewReady=!saved;
  if(saved){
    ({sources,source,parent,items,total,selected,section,tracks,trackTotal,trackOffset,albums,albumTotal,album,albumItems,albumTrackTotal,searchDraft,searchQuery}=saved);
    Object.assign(queries,saved.queries);for(const [key,value] of saved.positions)positions.set(key,value);
    section='library';album=null;searchDraft=queries.library;searchQuery=searchDraft;
  }
  function saveView(){
    remember();saveMusicLibraryState({sources,source,parent,items,total,selected,section,tracks,trackTotal,trackOffset,
      albums,albumTotal,album,albumItems,albumTrackTotal,searchDraft,searchQuery,queries,positions:[...positions]});
  }
  let locationMenu:{x:number;y:number;label:string;sourceId:string;relativePath:string;anchor:HTMLElement;unavailableReason:string;isFolder:boolean}|null=null;
  let locationMenuSerial=0;
  type Queue={kind:'folders'|'library'|'album';source:OstSource;parent:string;tracks:OstItem[];offset:number;total:number;stale?:boolean;query?:string;anchor?:string;albumName?:string};
  let queue:Queue|null=null,playingTrack:OstItem|null=null;
  let playerExpanded=false,playbackNavigating=false,playbackNotice='';
  let playbackRequest:AbortController|null=null;
  let playerReturn:Position|undefined,playerAnchor:HTMLElement|null=null;
  function identity(item:OstItem){return JSON.stringify([item.source_id,item.path]);}
  const shuffleOrder=createShuffleQueue();
  let shuffleQueue:Queue|null=null,shuffleMode=false,shuffleState={previous:false,next:false};
  $: updateShuffle(queue,playingTrack,$musicShuffle);
  function shuffleChanged(enabled:boolean){
    if(enabled===shuffleMode)return;
    shuffleMode=enabled;playbackRequest?.abort();playbackRequest=null;playbackNavigating=false;playbackNotice='';shuffleQueue=null;
  }
  function updateShuffle(target:Queue|null,item:OstItem|null,enabled:boolean){
    shuffleChanged(enabled);
    if(!enabled||!target||!item){shuffleOrder.reset(null,[]);shuffleQueue=null;shuffleState={previous:false,next:false};return;}
    const ids=target.tracks.filter(row=>!row.is_dir&&row.available).map(identity);
    if(shuffleQueue!==target){shuffleOrder.reset(identity(item),ids);shuffleQueue=target;}
    else shuffleOrder.reconcile(identity(item),ids);
    shuffleState={previous:shuffleOrder.canPrevious,next:shuffleOrder.canNext||target.offset<target.total||!!target.stale};
  }
  $: playingIndex=queue&&playingTrack?queue.tracks.findIndex(item=>identity(item)===identity(playingTrack!)):-1;
  $: hasPrevious=$musicShuffle?shuffleState.previous:playingIndex>0||!!queue?.stale;
  $: hasNext=$musicShuffle?shuffleState.next:!!queue&&(playingIndex<queue.tracks.length-1||queue.offset<queue.total||queue.stale);
  $: canWrap=$musicShuffle?!!queue&&!!playingTrack&&(queue.tracks.some(row=>row.available&&identity(row)!==identity(playingTrack!))||queue.offset<queue.total||!!queue.stale):!!queue?.tracks.length;
  $: playingSource=sources.find(owner=>owner.source_id===playingTrack?.source_id);
  $: playingParent=queue?.kind!=='folders'?playingTrack?.path.split('/').slice(0,-1).join('/'):queue?.parent;
  $: folderLabel=queue?.kind==='album'?queue.albumName??'':queue?.kind==='library'?(playingTrack as MusicTrack|null)?.music?.album??'':queue?`${playingSource?.name??queue.source.name}${playingParent?' / '+playingParent:''}`:'';
  $: playingDetails=playingTrack?(section==='library'?tracks:albumItems).find(item=>identity(item)===identity(playingTrack!))??playingTrack:null;
  $: displayTitle=playingDetails?(queue?.kind==='folders'?playingDetails.name:musicTitle(playingDetails)):'';
  $: parts=parent?parent.split('/'):[];
  $: backLabel=parent?(parts.length>1?parts[parts.length-2]:source?.name):'Music';
  function key(owner:OstSource|null,path:string){return owner?`${owner.source_id}:${path}`:'roots';}
  function viewKey(){return section==='folders'?key(source,parent):section==='library'?`library:${searchQuery}`:album?`album:${album.source_id}:${album.anchor}`:`albums:${searchQuery}`;}
  function remember(){
    if(!main?.isConnected)return;
    const active=document.activeElement as HTMLElement|null;
    positions.set(viewKey(),{count:section==='library'?tracks.length:section==='albums'?(album?albumItems.length:albums.length):items.length,scroll:main?.scrollTop??0,
      focus:active?.dataset.ostFocus??'',selected});
    if(positions.size>64)positions.delete(positions.keys().next().value!);
  }
  async function restore(position?:Position,signal?:AbortSignal,serial=navigationSerial,focusLibrary=true){
    await tick();
    if(!alive||signal?.aborted||serial!==navigationSerial)return;
    const anchor=position?.focus?[...main.querySelectorAll<HTMLElement>('[data-ost-focus]')]
      .find(node=>node.dataset.ostFocus===position.focus):null;
    if(anchor)anchor.focus({preventScroll:true});
    else if(focusLibrary)main?.focus({preventScroll:true});
    main.scrollTop=position?.scroll??0;
  }
  async function load(more=false,wanted=0,position?:Position,focusLibrary=true){
    if(!source)return;
    request?.abort();const current=new AbortController();request=current;
    const owner=source,folder=parent,scroll=main?.scrollTop??0;
    loading=true;error='';retryMore=more;
    try{
      let next:OstItem[]=[],result;
      do{
        result=await ostApi.library(owner.source_id,folder,more?items.length:next.length,current.signal);
        if(!alive||current.signal.aborted)return;
        next=[...next,...result.items];
      }while(!more&&next.length<wanted&&next.length<result.total&&result.items.length>0);
      items=more?[...items,...next]:next;total=result.total;
      if(position)selected=position.selected;
      await tick();
      if(!alive||current.signal.aborted)return;
      if(more)main.scrollTop=scroll;
      else if(position)await restore(position,current.signal,navigationSerial,focusLibrary);
    }catch(caught){if(alive&&!current.signal.aborted)error=caught instanceof Error?caught.message:'Could not load this folder.';}
    finally{if(alive&&request===current)loading=false;}
  }
  async function navigate(owner:OstSource|null,path=''){
    const serial=++navigationSerial;
    remember();request?.abort();locationMenu=null;
    source=owner;parent=path;items=[];total=0;selected='';error='';notice='';missingArt=new Set();
    const position=positions.get(key(owner,path))??{count:0,scroll:0,focus:'',selected:''};
    await restore(undefined,undefined,serial);
    if(!alive||serial!==navigationSerial)return;
    if(owner)await load(false,position.count,position);
    else{loading=false;await restore(position,undefined,serial);}
  }
  async function loadTracks(more=false,wanted=0,position?:Position,focusLibrary=true,hydrate=true){
    trackRequest?.abort();const current=new AbortController();trackRequest=current;
    const serial=navigationSerial,scroll=main?.scrollTop??0,query=searchQuery;
    trackLoading=true;trackError='';trackRetryMore=more;
    try{
      let next:MusicTrack[]=[],offset=more?trackOffset:0,result;
      do{
        result=await ostApi.tracks(offset,current.signal,query);
        if(!alive||current.signal.aborted||section!=='library'||searchQuery!==query)return;
        offset+=result.items.length;next=[...next,...result.items];
      }while(!more&&next.length<wanted&&offset<result.total&&result.items.length>0);
      tracks=[...new Map((more?[...tracks,...next]:next).map(item=>[identity(item),item])).values()];trackTotal=result.total;trackOffset=offset;
      await tick();if(!alive||current.signal.aborted)return;
      if(more)main.scrollTop=scroll;
      else if(position)await restore(position,current.signal,serial,focusLibrary);
    }catch(caught){if(alive&&!current.signal.aborted)trackError=caught instanceof Error?caught.message:'Could not load the Music library.';}
    finally{if(alive&&trackRequest===current)trackLoading=false;}
  }
  async function selectSection(next:Section){
    if(next===section&&next==='albums'&&album){void backAlbums();return;}
    if(next===section&&!playerExpanded)return;
    if(!playerExpanded)remember();
    clearTimeout(searchTimer);albumRequest?.abort();request?.abort();trackRequest?.abort();loading=false;trackLoading=false;albumLoading=false;
    locationMenu=null;playerExpanded=false;section=next;
    searchDraft=next==='folders'?'':queries[next];searchQuery=searchDraft;album=null;
    const serial=++navigationSerial;
    const position=positions.get(viewKey());
    await restore(undefined,undefined,serial);
    if(!alive||serial!==navigationSerial)return;
    if(next==='library')await loadTracks(false,position?.count??tracks.length,position);
    else if(next==='albums')await loadAlbums(false,position?.count??albums.length,position);
    else if(source)await load(false,position?.count??items.length,position);
    else await restore(position,undefined,serial);
  }
  function back(){void navigate(source&&parent?source:null,parent?parts.slice(0,-1).join('/'):'');}
  async function refreshSources(){
    if(!viewReady)return;
    sourceRequest?.abort();const current=new AbortController();sourceRequest=current;
    sourceLoading=true;sourceError='';
    try{
      const result=await ostApi.folders(current.signal);
      if(!alive||current.signal.aborted)return;
      if(queue&&queue.kind!=='folders'&&JSON.stringify(sources)!==JSON.stringify(result))queue.stale=true;
      sources=result;
      const owners=new Map(result.map(row=>[row.source_id,row]));
      tracks=tracks.filter(row=>owners.has(row.source_id)).map(row=>({...row,available:row.available&&!!owners.get(row.source_id)?.available}));
      albums=albums.filter(row=>owners.has(row.source_id));
      albumItems=albumItems.filter(row=>owners.has(row.source_id)).map(row=>({...row,available:row.available&&!!owners.get(row.source_id)?.available}));
      if(queue&&playingTrack&&!result.some(row=>row.source_id===playingTrack?.source_id&&row.available)){
        playbackRequest?.abort();queue=null;playingTrack=null;playerExpanded=false;playbackNavigating=false;
      }
      if(section==='library'){
        if(source){
          const fresh=result.find(row=>row.source_id===source?.source_id);
          if(fresh)source=fresh;
          else{source=null;parent='';items=[];total=0;selected='';}
        }
        const active=document.activeElement as HTMLElement|null;
        void loadTracks(false,tracks.length,{count:tracks.length,scroll:main?.scrollTop??0,focus:active?.dataset.ostFocus??'',selected},false);
      }else if(section==='albums'){
        const active=document.activeElement as HTMLElement|null;
        const position={count:album?albumItems.length:albums.length,scroll:main?.scrollTop??0,focus:active?.dataset.ostFocus??'',selected};
        if(album){
          if(!result.some(row=>row.source_id===album?.source_id&&row.available)){await backAlbums();notice='This album is no longer available.';}
          else void loadAlbum(false,position.count,position,true);
        }else void loadAlbums(false,position.count,position,false);
      }else if(source){
        const fresh=result.find(row=>row.source_id===source?.source_id);
        if(fresh){
          source=fresh;const active=document.activeElement as HTMLElement|null;
          void load(false,items.length,{count:items.length,scroll:main?.scrollTop??0,focus:active?.dataset.ostFocus??'',selected},false);
        }
        else{await navigate(null);notice='This folder is no longer assigned to Music.';}
      }
    }catch(caught){if(alive&&!current.signal.aborted)sourceError=caught instanceof Error?caught.message:'Could not load Music folders.';}
    finally{if(alive&&sourceRequest===current)sourceLoading=false;}
  }
  async function settingsClosed(){
    await tick();
    if(!alive||$settingsOpen)return;
    void refreshSources();
  }
  function openLocation(event:MouseEvent|KeyboardEvent,label:string,sourceId:string,path:string,available:boolean,isFolder=false){
    event.preventDefault();event.stopPropagation();
    const anchor=event.currentTarget as HTMLElement,rect=anchor.getBoundingClientRect();
    locationMenu={x:event instanceof MouseEvent?event.clientX:rect.left+8,y:event instanceof MouseEvent?event.clientY:rect.bottom,
      label,sourceId,relativePath:path,anchor,unavailableReason:available?'':'This local item is unavailable.',isFolder};
    locationMenuSerial++;
  }
  function locationKey(event:KeyboardEvent,label:string,sourceId:string,path:string,available:boolean,isFolder=false){
    if(event.key==='ContextMenu'||event.shiftKey&&event.key==='F10')openLocation(event,label,sourceId,path,available,isFolder);
  }
  function folderActions(menu:NonNullable<typeof locationMenu>){
    return menu.isFolder?[{label:'Refresh metadata',disabled:refreshingFolder,
      reason:refreshingFolder?'A folder refresh is already running.':'',onSelect:()=>void refreshFolder(menu)}]:[];
  }
  async function refreshFolder(menu:NonNullable<typeof locationMenu>){
    if(refreshingFolder)return;
    refreshSubmitting=true;menu.anchor?.focus({preventScroll:true});
    try{await startMetadataRefresh(menu.sourceId,menu.relativePath);}
    catch(caught){notify(caught instanceof Error?caught.message:'Could not refresh metadata.',{tone:'error'});}
    finally{refreshSubmitting=false;}
  }
  function artKey(sourceId:string,path:string,version:string){return `${sourceId}:${path}:${version}`;}
  function artFailed(id:string){missingArt=new Set([...missingArt,id]);}
  function expandPlayer(event?:MouseEvent){
    if(playerExpanded)return;
    remember();playerReturn=positions.get(viewKey());
    playerAnchor=(event?.currentTarget as HTMLElement|null)??document.activeElement as HTMLElement|null;
    playerExpanded=true;
  }
  async function browsePlayer(){
    playerExpanded=false;await tick();
    if(!alive)return;
    await restore(playerReturn);
    if(playerAnchor?.isConnected)playerAnchor.focus({preventScroll:true});
    playerAnchor=null;
  }
  async function extendQueue(target:Queue,signal:AbortSignal,expectedTotal?:number){
    while(target.offset<target.total){
      const result=target.kind==='library'?await ostApi.tracks(target.offset,signal,target.query):target.kind==='album'?await ostApi.albumTracks(target.source.source_id,target.anchor!,target.offset,signal):await ostApi.library(target.source.source_id,target.parent,target.offset,signal);
      if(!alive||signal.aborted||queue!==target)return;
      if(expectedTotal!==undefined&&(result.total!==expectedTotal||!result.items.length))throw new Error('Music queue changed. Try Next again.');
      if(!result.items.length){target.offset=result.total;target.total=result.total;break;}
      target.offset+=result.items.length;target.total=result.total;
      const additions=result.items.filter(item=>!item.is_dir&&item.available&&!target.tracks.some(old=>identity(old)===identity(item)));
      target.tracks=[...target.tracks,...additions];
      queue=target;
      if(additions.length)return;
    }
  }
  async function navigatePlayback(direction:-1|1,wrap:boolean){
    if(!queue||!playingTrack||playbackNavigating)return false;
    const target=queue,currentTrack=playingTrack,shuffled=$musicShuffle;
    playbackRequest?.abort();const pending=new AbortController();playbackRequest=pending;
    playbackNavigating=true;playbackNotice='';
    try{
      if(target.kind!=='folders'&&target.stale){
        let refreshed:OstItem[]=[],offset=0,result;
        do{
          result=target.kind==='album'?await ostApi.albumTracks(target.source.source_id,target.anchor!,offset,pending.signal):await ostApi.tracks(offset,pending.signal,target.query);
          if(!alive||pending.signal.aborted||queue!==target||playingTrack!==currentTrack)return false;
          refreshed=[...refreshed,...result.items.filter(item=>item.available)];offset+=result.items.length;
        }while(offset<result.total&&result.items.length&&!refreshed.some(item=>identity(item)===identity(currentTrack)));
        target.tracks=refreshed;target.offset=offset;target.total=result.total;target.stale=false;queue=target;
      }
      let index=target.tracks.findIndex(item=>identity(item)===identity(currentTrack));
      if(index<0)return false;
      if(shuffled){
        const bound=target.total;
        while(target.offset<bound){
          const offset=target.offset;await extendQueue(target,pending.signal,bound);
          if(!alive||pending.signal.aborted||queue!==target||playingTrack!==currentTrack||$musicShuffle!==shuffled)return false;
          if(target.offset<=offset)throw new Error('Could not load another song. Try Next again.');
        }
        updateShuffle(target,currentTrack,true);
        const id=shuffleOrder.move(direction,wrap),next=target.tracks.find(row=>row.available&&identity(row)===id);
        if(!next)return false;
        playingTrack=next;
        if(target.kind==='folders'&&source?.source_id===target.source.source_id&&parent===target.parent)selected=next.path;
        updateShuffle(target,next,true);return true;
      }
      if(direction===1&&index===target.tracks.length-1&&target.offset<target.total)await extendQueue(target,pending.signal);
      if(direction===-1&&index===0&&wrap)while(target.offset<target.total){await extendQueue(target,pending.signal);if(pending.signal.aborted||queue!==target)return false;}
      if(!alive||pending.signal.aborted||queue!==target||playingTrack!==currentTrack)return false;
      index+=direction;
      if(index<0||index>=target.tracks.length){if(!wrap)return false;index=(index+target.tracks.length)%target.tracks.length;}
      const next=target.tracks[index];
      if(!next||identity(next)===identity(currentTrack))return false;
      playingTrack=next;
      if(target.kind==='folders'&&source?.source_id===target.source.source_id&&parent===target.parent)selected=next.path;
      return true;
    }catch(caught){
      if(alive&&!pending.signal.aborted&&queue===target)playbackNotice=caught instanceof Error?caught.message:'Could not load another song. Try Next again.';
      return false;
    }finally{if(playbackRequest===pending)playbackNavigating=false;}
  }
  function choose(item:OstItem,event:MouseEvent){
    if(!item.available||!source)return;
    if(item.is_dir)void navigate(source,item.path);
    else{
      playbackRequest?.abort();playbackNavigating=false;playbackNotice='';selected=item.path;
      queue={kind:'folders',source,parent,tracks:items.filter(row=>!row.is_dir&&row.available),offset:items.length,total};
      playingTrack=item;expandPlayer(event);
    }
  }
  function chooseTrack(item:OstTrack,event:MouseEvent){
    const owner=sources.find(row=>row.source_id===item.source_id&&row.available);
    if(!item.available||!owner)return;
    playbackRequest?.abort();playbackNavigating=false;playbackNotice='';
    queue={kind:'library',source:owner,parent:'',tracks:tracks.filter(row=>row.available),offset:trackOffset,total:trackTotal,query:searchQuery};
    playingTrack=item;expandPlayer(event);
  }
  async function loadAlbums(more=false,wanted=0,position?:Position,focusLibrary=true,hydrate=true){
    albumRequest?.abort();const current=new AbortController();albumRequest=current;
    const serial=navigationSerial,query=searchQuery,scroll=main?.scrollTop??0;
    albumLoading=true;albumError='';
    try{
      let next:MusicAlbum[]=[],result;
      do{
        result=await ostApi.albums(more?albums.length:next.length,current.signal,query);
        if(!alive||current.signal.aborted||section!=='albums'||album||query!==searchQuery)return;
        next=[...next,...result.items];
      }while(!more&&next.length<wanted&&next.length<result.total&&result.items.length);
      albums=more?[...albums,...next]:next;albumTotal=result.total;
      await tick();if(!alive||current.signal.aborted)return;
      if(more)main.scrollTop=scroll;else if(position)await restore(position,current.signal,serial,focusLibrary);
    }catch(caught){if(alive&&!current.signal.aborted)albumError=caught instanceof Error?caught.message:'Could not load albums.';}
    finally{if(albumRequest===current)albumLoading=false;}
  }
  async function openAlbum(item:MusicAlbum){
    remember();trackRequest?.abort();albumRequest?.abort();locationMenu=null;
    album=item;albumItems=[];albumTrackTotal=item.total;playerExpanded=false;++navigationSerial;
    const position=positions.get(viewKey());await restore(undefined,undefined,navigationSerial);
    await loadAlbum(false,position?.count??0,position,true);
  }
  async function loadAlbum(more=false,wanted=0,position?:Position,hydrate=true){
    if(!album)return;
    albumRequest?.abort();const current=new AbortController();albumRequest=current;
    const target=album,serial=navigationSerial,scroll=main?.scrollTop??0;
    albumLoading=true;albumError='';
    try{
      let next:MusicTrack[]=[],result;
      do{
        result=await ostApi.albumTracks(target.source_id,target.anchor,more?albumItems.length:next.length,current.signal);
        if(!alive||current.signal.aborted||section!=='albums')return;
        next=[...next,...result.items];
      }while(!more&&next.length<wanted&&next.length<result.total&&result.items.length);
      // Anchor identity stays the user's selected track even if sorting changes.
      album={...result.album,anchor:target.anchor};albumItems=more?[...albumItems,...next]:next;albumTrackTotal=result.total;
      await tick();if(!alive||current.signal.aborted)return;
      if(more)main.scrollTop=scroll;else if(position)await restore(position,current.signal,serial,false);
    }catch(caught){if(alive&&!current.signal.aborted)albumError=caught instanceof Error?caught.message:'Could not load this album.';}
    finally{if(albumRequest===current)albumLoading=false;}
  }
  async function backAlbums(){
    remember();albumRequest?.abort();album=null;albumItems=[];albumLoading=false;albumError='';
    playerExpanded=false;const serial=++navigationSerial,position=positions.get(viewKey());
    await restore(position,undefined,serial);if(!alive||serial!==navigationSerial)return;
    await loadAlbums(false,position?.count??albums.length,position);
  }
  function chooseAlbumTrack(item:MusicTrack,event:MouseEvent){
    const owner=sources.find(row=>row.source_id===item.source_id&&row.available);
    if(!item.available||!owner||!album)return;
    playbackRequest?.abort();playbackNavigating=false;playbackNotice='';
    queue={kind:'album',source:owner,parent:'',anchor:album.anchor,albumName:album.name,tracks:albumItems.filter(row=>row.available),offset:albumItems.length,total:albumTrackTotal};
    playingTrack=item;expandPlayer(event);
  }
  function scheduleSearch(){clearTimeout(searchTimer);if(!searchComposing)searchTimer=setTimeout(()=>void applySearch(),250);}
  async function applySearch(){
    clearTimeout(searchTimer);if(searchComposing)return;
    const query=searchDraft.trim();if(query===searchQuery&&section!=='folders'&&!album)return;
    remember();albumRequest?.abort();request?.abort();trackRequest?.abort();
    section=section==='albums'?'albums':'library';album=null;playerExpanded=false;locationMenu=null;
    searchQuery=query;queries[section]=query;const serial=++navigationSerial,position=positions.get(viewKey());
    const restoreTo=position??{count:0,scroll:0,focus:'',selected:''};
    if(section==='library')await loadTracks(false,restoreTo.count,restoreTo,false);
    else await loadAlbums(false,restoreTo.count,restoreTo,false);
    if(!alive||serial!==navigationSerial)return;
  }
  onMount(()=>{
    let previous=false;
    const stop=settingsOpen.subscribe(open=>{if(previous&&!open)void settingsClosed();previous=open;});
    const stopShuffle=musicShuffle.subscribe(shuffleChanged);
    let observed=-1;
    const stopMetadata=metadataChanges.subscribe(value=>{if(observed>=0&&value!==observed)void refreshSources();observed=value;});
    void (async()=>{
      if(saved)await restore(positions.get(viewKey()),undefined,navigationSerial,false);
      viewReady=true;if(alive)void refreshSources();
    })();
    return()=>{stop();stopShuffle();stopMetadata();};
  });
  onDestroy(()=>{saveView();alive=false;clearTimeout(searchTimer);albumRequest?.abort();request?.abort();sourceRequest?.abort();trackRequest?.abort();playbackRequest?.abort();});
</script>

<svelte:window on:focus={()=>{if(!$settingsOpen)void refreshSources();}} />
<div class="ost-surface">
  <header>
    <button class="menu" aria-label="Open Keivotos menu" title="Open Keivotos menu" on:click={()=>drawer=true}>☰</button><h1>Music</h1>
    <div class="music-search flex-1 max-w-xl mx-4"><input type="text" bind:value={searchDraft} aria-label="Search music" placeholder="Search music..." maxlength="512"
      class="w-full bg-[#1e1e2e] border border-[#2a2a3a] rounded-lg px-3 py-1.5 text-sm text-gray-200 placeholder-gray-500 outline-none focus:border-purple-500 transition-colors"
      on:input={scheduleSearch} on:compositionstart={()=>{searchComposing=true;clearTimeout(searchTimer);}} on:compositionend={()=>{searchComposing=false;scheduleSearch();}}
      on:keydown={event=>{if(!event.isComposing&&event.key==='Enter')void applySearch();else if(!event.isComposing&&event.key==='Escape'&&searchDraft){event.stopPropagation();searchDraft='';void applySearch();}}}/></div>
  </header>
  <div class="ost-body">
  <ModuleSidebarDock owner="ost" label="Music"><OstSidebar {section} on:select={event=>selectSection(event.detail)}/></ModuleSidebarDock>
  <div class="ost-content">
  {#if section==='folders'&&source}
      <div class="location" class:player-hidden={playerExpanded}>
        <button class="back" aria-label={`Back to ${backLabel}`} title={`Back to ${backLabel}`} on:click={back}>
          <svg width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="m15 18-6-6 6-6"/></svg>
        </button>
        <nav aria-label="Music location">
          <button on:click={()=>navigate(source)} title={source.name}>{source.name}</button>
          {#each parts as part,i}<span aria-hidden="true">/</span><button on:click={()=>navigate(source,parts.slice(0,i+1).join('/'))} title={part}>{part}</button>{/each}
        </nav>
      </div>
  {/if}
  <main bind:this={main} on:scroll={remember} on:focusin={remember} aria-label="Music library" tabindex="-1" aria-busy={loading||sourceLoading||trackLoading||albumLoading} class:player-hidden={playerExpanded}>
    {#if notice}<p role="status">{notice}</p>{/if}
    {#if sourceError}<p role="alert">{sourceError} <button on:click={refreshSources}>Retry folders</button></p>{/if}
    {#if error}<p role="alert">{error} <button on:click={()=>load(retryMore,items.length)}>Retry</button></p>{/if}
    {#if section==='library'}
      {#if trackError}<p role="alert">{trackError} <button on:click={()=>loadTracks(trackRetryMore,tracks.length)}>Retry library</button></p>{/if}
      {#if trackLoading&&!tracks.length}<p role="status">Loading Music tracks…</p>{/if}
      <ul aria-label="All Music tracks" class="entries">
        {#each tracks as item (`${identity(item)}:${item.cover_version}`)}
          <li><MusicTrackRow {item} active={!!playingTrack&&identity(playingTrack)===identity(item)} on:choose={event=>chooseTrack(event.detail.item,event.detail.event)}
            on:location={event=>openLocation(event.detail.event,musicTitle(event.detail.item),item.source_id,item.path,item.available)}/></li>
        {/each}
      </ul>
      {#if !trackLoading&&!trackError&&!tracks.length}<p role="status">{searchQuery?'No music matches this search.':sources.length?'No supported Music tracks in assigned folders.':'Assign folders to the Music role in Settings → Files → Folders.'}</p>{/if}
      {#if trackOffset<trackTotal}<button class="more" disabled={trackLoading} on:click={()=>loadTracks(true)}>{trackLoading?'Loading…':'Load more'}</button>{/if}
      <p class="count" aria-live="polite">{tracks.length} / {trackTotal} tracks</p>
    {:else if section==='albums'}
      {#if album}
        <AlbumView {album} items={albumItems} currentIdentity={playingTrack?identity(playingTrack):''} loading={albumLoading} error={albumError} total={albumTrackTotal}
          on:back={backAlbums} on:more={()=>loadAlbum(true)} on:retry={()=>loadAlbum(false,albumItems.length,undefined,true)}
          on:choose={event=>chooseAlbumTrack(event.detail.item,event.detail.event)}
          on:location={event=>openLocation(event.detail.event,musicTitle(event.detail.item),event.detail.item.source_id,event.detail.item.path,event.detail.item.available)}/>
      {:else}
        <h2 class="albums-heading">Albums</h2>
        {#if albumError}<p role="alert">{albumError} <button on:click={()=>loadAlbums(false,albums.length)}>Retry albums</button></p>{/if}
        {#if albumLoading&&!albums.length}<p role="status">Loading albums…</p>{/if}
        <ul aria-label="Music albums" class="albums-grid">
          {#each albums as item (item.id)}
            {@const cover=item.covers.find(cover=>!missingArt.has(artKey(item.source_id,cover.path,cover.version)))}
            <li><button class="album-tile" data-ost-album={item.id} data-ost-source={item.source_id} data-ost-anchor={item.anchor} data-ost-focus={`album:${item.source_id}:${item.anchor}`}
              aria-label={`Open album ${item.name}`} title={item.name} on:click={()=>openAlbum(item)}
              on:contextmenu={event=>openLocation(event,item.name,item.source_id,item.covers[1].path,!!sources.find(source=>source.source_id===item.source_id)?.available,true)}
              on:keydown={event=>locationKey(event,item.name,item.source_id,item.covers[1].path,!!sources.find(source=>source.source_id===item.source_id)?.available,true)}>
              <span class="album-cover"><span aria-hidden="true">♫</span>{#if cover}{#key artKey(item.source_id,cover.path,cover.version)}<img alt="" loading="lazy" decoding="async" src={ostApi.coverUrl(item.source_id,cover.path,cover.version)} on:error={()=>{if(cover)artFailed(artKey(item.source_id,cover.path,cover.version));}}/>{/key}{/if}</span>
              <span class="album-title">{item.name}</span>
            </button></li>
          {/each}
        </ul>
        {#if !albumLoading&&!albumError&&!albums.length}<p>{searchQuery?'No albums match this search.':'No albums in assigned folders.'}</p>{/if}
        {#if albums.length<albumTotal}<button class="more" disabled={albumLoading} on:click={()=>loadAlbums(true)}>{albumLoading?'Loading…':'Load more'}</button>{/if}
      {/if}
    {:else if !source}
      {#if sourceLoading&&!sources.length}<p role="status">Loading Music folders…</p>{/if}
      {#if sources.length}
        <ul aria-label="Music folders" class="entries">
          {#each sources as root (root.source_id)}
            <li><button class="entry" data-ost-source={root.source_id} data-ost-entry-path="" data-ost-focus={root.source_id}
              aria-label={`Open Music folder ${root.name}`} aria-disabled={!root.available} title={`${root.name}\n${root.path}`}
              on:click={()=>{if(root.available)void navigate(root);}}
              on:contextmenu={event=>openLocation(event,root.name,root.source_id,'',root.available,true)}
              on:keydown={event=>locationKey(event,root.name,root.source_id,'',root.available,true)}>
              <span class="art"><span aria-hidden="true">📁</span></span>
              <span class="details"><span class="name">{root.name}</span>{#if !root.available}<small>Folder unavailable</small>{/if}</span>
              <span class="kind" aria-hidden="true">›</span>
            </button></li>
          {/each}
        </ul>
      {:else if !sourceLoading&&!sourceError}<p>Assign folders to the Music role in Settings → Files → Folders.</p>{/if}
    {:else}
      {#if loading&&!items.length}<p role="status">Loading this folder…</p>{/if}
      <ul aria-label="Music folder contents" class="entries">
        {#each items as item (`${item.path}:${item.cover_version}`)}
          {@const id=artKey(item.source_id,item.path,item.cover_version)}
          <li><button class="entry" class:selected={selected===item.path} data-ost-source={item.source_id} data-ost-entry-path={item.path} data-ost-focus={item.path}
            aria-label={`${item.is_dir?'Open folder':'Play track'} ${item.name}`} aria-disabled={!item.available}
            aria-pressed={item.is_dir?undefined:selected===item.path} title={item.name} on:click={event=>choose(item,event)}
            on:contextmenu={event=>openLocation(event,item.name,item.source_id,item.path,item.available,item.is_dir)}
            on:keydown={event=>locationKey(event,item.name,item.source_id,item.path,item.available,item.is_dir)}>
            <span class="art"><span aria-hidden="true">{item.is_dir?'📁':'♫'}</span>{#if !item.is_dir&&item.available&&!missingArt.has(id)}<img alt="" loading="lazy" decoding="async" width="52" height="52" src={ostApi.coverUrl(item.source_id,item.path,item.cover_version)} on:error={()=>artFailed(id)}/>{/if}</span>
            <span class="details"><span class="name">{item.name}</span>{#if !item.available||!item.is_dir}<small>{!item.available?'File unavailable':item.ext.toUpperCase()}</small>{/if}</span>
            {#if item.is_dir}<span class="kind" aria-hidden="true">›</span>{/if}
          </button></li>
        {/each}
      </ul>
      {#if !loading&&!error&&!items.length}<p role="status">No folders or supported audio files here.</p>{/if}
      {#if items.length<total}<button class="more" disabled={loading} on:click={()=>load(true)}>{loading?'Loading…':'Load more'}</button>{/if}
      <p class="count" aria-live="polite">{items.length} / {total} items</p>
    {/if}
  </main>
  {#if playingTrack}<AudioPlayer track={playingTrack} {folderLabel} {displayTitle} expanded={playerExpanded}
    {hasPrevious} {hasNext} {canWrap} navigating={playbackNavigating} navigationNotice={playbackNotice}
    onNavigate={navigatePlayback} onExpand={expandPlayer} onBrowse={browsePlayer}/>{/if}
  </div>
  </div>
</div>
{#if drawer}<AppDrawer on:close={()=>drawer=false} />{/if}
{#if locationMenu}{#key locationMenuSerial}<ModuleLocationMenu {...locationMenu} actions={folderActions(locationMenu)} on:close={()=>locationMenu=null}/>{/key}{/if}

<style>
  .ost-surface{display:flex;flex:1;min-height:0;min-width:0;flex-direction:column;background:#0f0f14;color:#e0e0e8;font-family:system-ui,sans-serif}
  .ost-body{display:flex;position:relative;flex:1;min-height:0;min-width:0;overflow:hidden}.ost-content{display:flex;flex:1;flex-direction:column;min-height:0;min-width:0}
  header{display:flex;align-items:center;gap:16px;min-height:60px;padding:8px 16px;border-bottom:1px solid #292936}
  .menu{width:36px;height:36px;flex:none;border-radius:8px;background:#1e1e2e;font-size:16px}
  h1{font-size:18px;font-weight:600}main:focus{outline:none}
  .music-search{min-width:0}.albums-heading{font-size:20px;font-weight:600;margin:0 0 20px}.albums-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:24px;max-width:1000px;list-style:none;margin:0;padding:0}.album-tile{display:block;width:100%;min-width:0;text-align:left;border-radius:12px}.album-cover{display:grid;place-items:center;position:relative;aspect-ratio:1;width:100%;overflow:hidden;border-radius:12px;background:#20202d;color:#858599;font-size:44px}.album-cover img{position:absolute;width:100%;height:100%;object-fit:cover}.album-title{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;line-clamp:2;overflow:hidden;overflow-wrap:anywhere;font-size:14px;line-height:1.45;margin-top:10px}.album-tile:hover .album-title{color:var(--accent)}
  main{flex:1;min-height:0;overflow:auto;padding:20px 24px 28px;scrollbar-gutter:stable}
  main.player-hidden,.location.player-hidden{display:none}
  .location{display:flex;flex:none;align-items:center;gap:12px;padding:12px 24px 0;min-width:0}
  .back{display:grid;place-items:center;flex-shrink:0;width:40px;height:40px;border:0;background:transparent;border-radius:999px;color:#eee;transition:width .18s ease,background-color .18s ease}
  .back:hover,.back:focus-visible{width:56px;background:#33333b}
  nav{display:flex;flex-wrap:wrap;align-items:center;gap:6px;min-width:0;font-size:12px;color:#858599}
  nav button{max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#b6b6c6}nav button:hover{color:#fff}
  .entries{margin:0;list-style:none;padding:0;max-width:1000px}.entries li{border-bottom:1px solid #242431}
  .entry{display:flex;align-items:center;gap:14px;width:100%;min-width:0;min-height:76px;padding:12px 8px;text-align:left;border-radius:10px;transition:background-color .15s ease}
  .entry:hover,.entry:focus-visible{background:#1c1c29}.entry.selected{background:#202033}.entry[aria-disabled='true']{opacity:.55;cursor:not-allowed}
  .art{position:relative;display:grid;place-items:center;flex:none;width:52px;height:52px;overflow:hidden;border-radius:10px;background:#20202d;color:#858599;font-size:25px}
  .art img{position:absolute;width:100%;height:100%;object-fit:cover}.details{flex:1;min-width:0}
  .name{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;line-clamp:2;overflow:hidden;overflow-wrap:anywhere;font-size:14px;line-height:1.45}
  small{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:4px;color:#858599;font-size:11px}
  .kind{color:#858599;font-size:24px}.more{margin-top:16px;padding:8px 14px;border:1px solid #393946;border-radius:10px;font-size:13px}
  .more:hover{background:#242431}
  button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  p{font-size:13px;color:#b6b6c6}p button{text-decoration:underline}.count{font-size:11px;margin-top:16px;color:#858599}
  @media(max-width:480px){main{padding:16px 12px 24px}.location{padding:12px 12px 0}header{gap:10px;padding:8px 12px}.entry{gap:10px}.music-search{margin:0}.albums-grid{gap:16px;grid-template-columns:repeat(auto-fill,minmax(132px,1fr))}}
  :global(html[data-motion='reduced']) .back,:global(html[data-motion='reduced']) .entry{transition:none}
  @media(prefers-reduced-motion:reduce){.back,.entry{transition:none}}
</style>

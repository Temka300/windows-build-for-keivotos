<script lang="ts">
 import {onMount,onDestroy,tick} from 'svelte';
 import {slide} from 'svelte/transition';
 import MediaPlayer from '../../components/MediaPlayer.svelte';
 import AppDrawer from '../../components/AppDrawer.svelte';
 import ModuleLocationMenu from '../../components/ModuleLocationMenu.svelte';
 import ModuleSidebarDock from '../../components/ModuleSidebarDock.svelte';
 import YouTubeSidebar from './YouTubeSidebar.svelte';
 import PlaylistsView from './PlaylistsView.svelte';
 import PlaylistCreateDialog from './PlaylistCreateDialog.svelte';
 import PlaylistMembershipDialog from './PlaylistMembershipDialog.svelte';
 import PlaylistQueuePanel from './PlaylistQueuePanel.svelte';
 import PlaylistContextMenu from './PlaylistContextMenu.svelte';
 import PlaylistRenameDialog from './PlaylistRenameDialog.svelte';
 import PlaylistDeleteDialog from './PlaylistDeleteDialog.svelte';
 import {ApiError} from '../../lib/http';
 import {filesApi} from '../../lib/filesApi';
 import {moduleSidebarPreferences} from '../../lib/moduleSidebarPreferences';
 import {notify,updateNotification,dismissNotification} from '../../lib/notifications';
 import {youtubeApi,type Download,type EngineStatus,type Playlist,type PlaylistItem} from './api';
 let drawer=false,query='',video='best',audio='best',optionsOpen=false;
 let section:'videos'|'playlists'='videos',createOpen=false,createAnchor:HTMLButtonElement|null=null,playlistsView:PlaylistsView;
 function chooseSection(next:'videos'|'playlists'){managementRequest?.abort();playlistMenu=null;abandonPlaylist();if(window.matchMedia('(max-width:700px)').matches)moduleSidebarPreferences('youtube').open.set(false);section=next;optionsOpen=false;clearTimeout(debounce);request?.abort();loading=false;if(next==='videos')void load(false,true);}
 function createPlaylist(anchor:HTMLButtonElement){createAnchor=anchor;createOpen=true;}
 async function closeCreate(){createOpen=false;await tick();if(createAnchor?.isConnected)createAnchor.focus();}
 async function created(playlist:Playlist){playlistsView?.acceptCreated(playlist);notify('Playlist created: '+playlist.title+'.',{tone:'success'});await closeCreate();}
 let status:EngineStatus|null=null,items:Download[]=[],total=0,error='',busy=false,loading=false;
 let request:AbortController|null=null,timer:ReturnType<typeof setTimeout>,debounce:ReturnType<typeof setTimeout>,alive=true;
 let selected:Download|null=null,returnFocus:HTMLElement|null=null,navigating=false,navigationNotice='';

 let activePlaylist:Playlist|null=null,playlistRows:PlaylistItem[]=[],playlistTotal=0,playlistLoading=false,playlistError='';
 let playlistRequest:AbortController|null=null,playlistNavigation:AbortController|null=null,playlistEpoch=0;
 let panelOpen=false,panelAnchor:HTMLButtonElement|null=null,playlistListScroll=0,playlistReturnId='';
 let playlistMenu:{x:number;y:number;playlist:Playlist;anchor:HTMLElement}|null=null,playlistMenuSerial=0;
 let management: {kind:'rename'|'delete';playlist:Playlist;anchor:HTMLElement}|null=null;
 let managementRequest:AbortController|null=null;
 function openPlaylistMenu(event:MouseEvent|KeyboardEvent,playlist:Playlist){
  event.preventDefault();event.stopPropagation();locationMenu=null;
  const current=event.currentTarget as HTMLElement,anchor=event.target instanceof Element?(event.target.closest<HTMLElement>('button,a')??current):current,rect=anchor.getBoundingClientRect();
  playlistMenu={playlist,anchor,x:event instanceof MouseEvent?event.clientX:rect.left+8,y:event instanceof MouseEvent?event.clientY:rect.bottom};playlistMenuSerial++;
 }
 function playlistMenuKey(event:KeyboardEvent){if(activePlaylist&&(event.key==='ContextMenu'||event.shiftKey&&event.key==='F10'))openPlaylistMenu(event,activePlaylist);}
 async function beginManagement(kind:'rename'|'delete',playlist:Playlist,anchor:HTMLElement){
  managementRequest?.abort();const current=new AbortController();managementRequest=current;
  try{const result=await youtubeApi.playlistItems(playlist.id,0,current.signal,1);if(alive&&!current.signal.aborted)management={kind,playlist:result.playlist,anchor};}
  catch(e){if(alive&&!current.signal.aborted){notify(e instanceof Error?e.message:'Could not load playlist.',{tone:'error'});anchor?.focus({preventScroll:true});}}
 }
 async function closeManagement(){const anchor=management?.anchor;management=null;await tick();if(anchor?.isConnected)anchor.focus({preventScroll:true});else document.querySelector<HTMLButtonElement>('.playlists-view .add,.playlist-back')?.focus({preventScroll:true});}
 async function playlistChanged(playlist:Playlist){
  const archived=!!playlist.deleted_at;
  if(activePlaylist?.id===playlist.id){
   playlistRequest?.abort();playlistNavigation?.abort();playlistEpoch++;navigating=false;
   if(archived){if(selected)await close();abandonPlaylist();}
   else activePlaylist=playlist;
  }
  await tick();playlistsView?.acceptChanged(playlist);
  notify((archived?'Playlist deleted: ':'Playlist renamed: ')+playlist.title+'.',{tone:'success'});
  await closeManagement();
 }
 function refreshPlaylist(){if(alive&&activePlaylist&&!management)void loadPlaylist();}
 function abandonPlaylist(){playlistEpoch++;playlistRequest?.abort();playlistNavigation?.abort();activePlaylist=null;playlistRows=[];playlistTotal=0;playlistError='';playlistLoading=false;panelOpen=false;}
 async function openPlaylist(playlist:Playlist){
  playlistListScroll=document.querySelector('.playlists-view')?.scrollTop??0;playlistReturnId=playlist.id;
  abandonPlaylist();activePlaylist=playlist;optionsOpen=false;await loadPlaylist();
 }
 async function backToPlaylists(){
  abandonPlaylist();await tick();
  const view=document.querySelector<HTMLElement>('.playlists-view');if(view)view.scrollTop=playlistListScroll;
  view?.querySelector<HTMLButtonElement>('[data-playlist-id="'+playlistReturnId+'"] button')?.focus({preventScroll:true});
 }
 async function loadPlaylist(more=false){
  const context=activePlaylist;if(!context||navigating)return;
  playlistRequest?.abort();const current=new AbortController();playlistRequest=current;playlistLoading=true;playlistError='';
  const epoch=playlistEpoch;
  try{
   const result=!more&&selected?await playlistSnapshot(context,current.signal):await youtubeApi.playlistItems(context.id,more?playlistRows.length:0,current.signal);
   if(!alive||current.signal.aborted||epoch!==playlistEpoch||activePlaylist!==context)return;
   playlistRows=[...new Map((more?[...playlistRows,...result.items]:result.items).map(row=>[row.job_id,row])).values()];
   playlistTotal=result.total;activePlaylist=result.playlist;
   if(selected){const currentMember=playlistRows.find(row=>row.job_id===selected?.id);navigationNotice=!currentMember?'This video is no longer in this playlist.':!currentMember.available?currentMember.unavailable_reason:'';}
  }catch(e){if(alive&&!current.signal.aborted&&epoch===playlistEpoch){playlistError=e instanceof Error?e.message:'Could not load playlist videos.';if(e instanceof ApiError&&e.status===404){playlistRows=[];playlistTotal=0;navigationNotice='This playlist is unavailable. Return to playlists.';}}}
  finally{if(playlistRequest===current)playlistLoading=false;}
 }
 async function playlistSnapshot(context:Playlist,signal:AbortSignal):Promise<{playlist:Playlist;items:PlaylistItem[];total:number}>{
  const first=await youtubeApi.playlistItems(context.id,0,signal,120),rows=[...first.items],bound=first.total;
  // One finite traversal of the count acknowledged by the first page.
  // Membership changes during continuation require an explicit retry.
  const requests=Math.ceil(bound/120);
  for(let page=1;page<requests&&rows.length<bound;page++){
   const next=await youtubeApi.playlistItems(context.id,page*120,signal,120);
   if(next.total!==bound||next.playlist.revision!==first.playlist.revision)throw new Error('This playlist changed while loading. Try again.');
   if(!next.items.length)throw new Error('Could not load the remaining playlist videos. Try again.');
   rows.push(...next.items);
  }
  const unique=[...new Map(rows.map(row=>[row.job_id,row])).values()].sort((a,b)=>a.position-b.position);
  if(unique.length!==bound)throw new Error('This playlist changed while loading. Try again.');
  return {playlist:first.playlist,items:unique,total:bound};
 }
 async function playlistAdjacent(direction:-1|1,wrap=false):Promise<boolean>{
  const context=activePlaylist,currentVideo=selected;if(!context||!currentVideo||navigating)return false;
  playlistRequest?.abort();playlistNavigation?.abort();const current=new AbortController();playlistNavigation=current;
  const epoch=playlistEpoch;navigating=true;playlistLoading=false;navigationNotice='';
  try{
   const result=await playlistSnapshot(context,current.signal);
   if(!alive||current.signal.aborted||epoch!==playlistEpoch||selected!==currentVideo||activePlaylist?.id!==context.id)return false;
   playlistRows=result.items;playlistTotal=result.total;activePlaylist=result.playlist;playlistError='';
   const index=playlistRows.findIndex(row=>row.job_id===currentVideo.id);
   if(index<0){navigationNotice='This video is no longer in this playlist.';return false;}
   for(let step=1;step<=playlistRows.length;step++){
    let next=index+direction*step;
    if(!wrap&&(next<0||next>=playlistRows.length))break;
    next=(next+playlistRows.length*step)%playlistRows.length;
    const row=playlistRows[next];
    if(row.available&&row.job){
     if(row.job_id===currentVideo.id)return false;
     open(row.job);return true;
    }
   }
   navigationNotice='No other playable videos are available in this playlist.';return false;
  }catch(e){
   if(alive&&!current.signal.aborted&&epoch===playlistEpoch&&selected===currentVideo){
    playlistError=e instanceof Error?e.message:'Could not load playlist videos.';navigationNotice=playlistError;
   }
   return false;
  }finally{if(playlistNavigation===current)navigating=false;}
 }
 async function choosePlaylistItem(item:PlaylistItem){
  const context=activePlaylist;if(!context||!item.available||navigating)return;
  const origin=!selected?document.activeElement as HTMLElement:undefined;
  playlistRequest?.abort();playlistNavigation?.abort();const current=new AbortController();playlistNavigation=current;
  const epoch=playlistEpoch;navigating=true;navigationNotice='';
  try{
   const result=await playlistSnapshot(context,current.signal);
   if(!alive||current.signal.aborted||epoch!==playlistEpoch||activePlaylist?.id!==context.id)return;
   playlistRows=result.items;playlistTotal=result.total;activePlaylist=result.playlist;playlistError='';
   const fresh=playlistRows.find(row=>row.job_id===item.job_id);
   if(!fresh?.available||!fresh.job){navigationNotice=fresh?.unavailable_reason||'This video is no longer in this playlist.';return;}
   open(fresh.job,origin);
  }catch(e){if(alive&&!current.signal.aborted&&epoch===playlistEpoch){playlistError=e instanceof Error?e.message:'Could not load playlist videos.';navigationNotice=playlistError;}}
  finally{if(playlistNavigation===current)navigating=false;}
 }
 async function dismissPanel(){panelOpen=false;await tick();if(panelAnchor?.isConnected)panelAnchor.focus();}
 function dismissPanelForEscape(){if(!panelOpen)return false;void dismissPanel();return true;}
 $: playlistIndex=selected?playlistRows.findIndex(row=>row.job_id===selected?.id):-1;
 $: queuePrevious=activePlaylist?playlistIndex>0:selectedIndex>0;
 $: queueNext=activePlaylist?playlistIndex>=0&&(playlistIndex<playlistRows.length-1||playlistRows.length<playlistTotal):selectedIndex>=0&&(selectedIndex<playable.length-1||items.length<total);
 $: queueWrap=activePlaylist?playlistTotal>1:playable.length>1||items.length<total;

 let locationMenu:{x:number;y:number;label:string;sourceId:string|null;relativePath:string|null;anchor:HTMLElement;unavailableReason:string}|null=null;
 let locationMenuSerial=0,membershipVideo:Download|null=null,membershipAnchor:HTMLElement|null=null;
 let locationVideo:Download|null=null;
 $: addReason=locationVideo?.status!=='complete'?'This download has no completed local file.':locationVideo&&(locationVideo.video==='audio'||isAudio(locationVideo))?'Audio-only downloads cannot be added to video playlists.':'';
 function chooseMembership(){if(!locationVideo||addReason)return;membershipVideo=locationVideo;}
 async function closeMembership(saved=false){if(saved)notify('Playlists updated for '+(membershipVideo?.title||'YouTube video')+'.',{tone:'success'});membershipVideo=null;await tick();membershipAnchor?.focus();}
 let changing=new Set<string>();
 function openLocation(event:MouseEvent|KeyboardEvent,item:Download){
  event.preventDefault();event.stopPropagation();
  const current=event.currentTarget as HTMLElement;
  const anchor=event.target instanceof HTMLElement?(event.target.closest<HTMLElement>('button,a')??current):current;
  const rect=anchor.getBoundingClientRect(),complete=item.status==='complete'&&!!item.source_id&&!!item.path;
  locationVideo=item;membershipAnchor=anchor;
  locationMenu={x:event instanceof MouseEvent?event.clientX:rect.left+8,y:event instanceof MouseEvent?event.clientY:rect.top+rect.height,
   label:item.title||'YouTube video',sourceId:complete?item.source_id:null,relativePath:complete?item.path:null,anchor,
   unavailableReason:complete?'':'This download has no completed local file.'};
  locationMenuSerial++;
 }
 function locationKey(event:KeyboardEvent,item:Download){if(event.key==='ContextMenu'||event.shiftKey&&event.key==='F10')openLocation(event,item);}
 const isAudio=(item:Download)=>['mp3','m4a','aac','ogg','opus','wav','flac'].includes(item.path.split('.').pop()?.toLowerCase()||'');
 $: playable=items.filter(item=>item.status==='complete'&&!isAudio(item));
 $: selectedIndex=selected?playable.findIndex(item=>item.id===selected?.id):-1;
 $: playback=selected?{src:filesApi.fileUrl(selected.source_id,selected.path),ext:selected.path.split('.').pop()?.toLowerCase()||''}:null;
 function open(item:Download,origin?:HTMLElement){if(!selected)returnFocus=origin??document.activeElement as HTMLElement;selected=item;navigationNotice='';optionsOpen=false;}
 async function close(){playlistNavigation?.abort();navigating=false;selected=null;panelOpen=false;await tick();if(returnFocus?.isConnected)returnFocus.focus({preventScroll:true});}
 async function adjacent(direction:-1|1,wrap=false):Promise<boolean>{
  if(activePlaylist)return playlistAdjacent(direction,wrap);
  if(!selected||navigating)return false;
  const current=selected;
  let index=selectedIndex+direction;
  if(index<0&&!wrap)return false;
  navigating=true;navigationNotice='';
  try{
   let choices=playable;
   while((direction===1&&index>=choices.length||direction===-1&&index<0&&wrap)&&items.length<total){
    const count=items.length;await load(true);
    choices=items.filter(item=>item.status==='complete'&&!isAudio(item));
    if(items.length<=count)throw new Error('Could not load the next video.');
   }
   if(wrap&&index<0)index=choices.length-1;
   if(wrap&&index>=choices.length)index=0;
   if(alive&&selected===current&&choices[index]){open(choices[index]);return true;}
   return false;
  }finally{navigating=false;}
 }
 let motion=200;
 function escape(event:KeyboardEvent){if(event.key==='Escape')optionsOpen=false;}
 function isVideoUrl(value:string){
  try{
   const url=new URL(value.trim());
   if(url.protocol!=='https:'||url.username||url.password||(url.port&&url.port!=='443'))return false;
   let id='';
   if(url.hostname==='youtu.be')id=url.pathname.replace(/^\/+|\/+$/g,'');
   else if(['youtube.com','www.youtube.com','m.youtube.com','music.youtube.com'].includes(url.hostname)){
    if(url.pathname==='/watch')id=url.searchParams.get('v')||'';
    else if(/^\/(shorts|live|embed)\//.test(url.pathname))id=url.pathname.split('/')[2]||'';
   }
   return /^[A-Za-z0-9_-]{11}$/.test(id);
  }catch{return false;}
 }
 $: urlMode=isVideoUrl(query);
 $: searchQuery=urlMode?'':query;
 const active=(item:Download)=>['queued','downloading','merging'].includes(item.status);
 const noticedJobs=new Map<string,string>();
 const downloadNoticeId=(item:Download)=>'youtube-download-'+item.id;
 function announceDownload(item:Download){
  const title=item.title||item.url||'YouTube video';
  const message=(item.status==='queued'?'Download queued: ':item.status==='merging'?'Finishing download: ':'Downloading: ')+title;
  const values={message,tone:'info' as const,progress:item.progress};
  if(noticedJobs.has(item.id))updateNotification(downloadNoticeId(item),values);
  else notify(message,{id:downloadNoticeId(item),tone:'info',progress:item.progress});
  noticedJobs.set(item.id,item.status);
  if(noticedJobs.size>256)noticedJobs.delete(noticedJobs.keys().next().value!);
 }
 function observeDownloads(rows:Download[]){
  for(const item of rows){
   if(active(item)){announceDownload(item);continue;}
   if(!noticedJobs.delete(item.id))continue;
   const title=item.title||item.url||'YouTube video';
   if(item.status==='complete')notify('Download complete: '+title+'.',{id:downloadNoticeId(item),tone:'success'});
   else if(item.status==='failed')notify('Download failed: '+title+'. '+item.error,{id:downloadNoticeId(item),tone:'error'});
  }
 }
 async function engine(){try{status=await youtubeApi.status();}catch(e){error=String(e);}}
 async function load(more=false,refresh=false){
   request?.abort();const current=new AbortController();request=current;loading=true;
   const searched=searchQuery,target=refresh?Math.max(60,items.length):60;
   try{
    let offset=more?items.length:0,rows:Download[]=[],count=0;
    do{
     const result=await youtubeApi.library(searched,offset,current.signal,Math.min(120,target-rows.length));
     if(!alive||current.signal.aborted)return;
     rows.push(...result.items);count=result.total;offset+=result.items.length;
     if(!result.items.length)break;
    }while(!more&&refresh&&rows.length<target&&offset<count);
    // Refresh the entire loaded prefix: retaining an old tail revives failures
    // that another retry/cancellation has already removed from the server.
    items=[...new Map((more?[...items,...rows]:rows).filter(row=>row.status!=='cancelled'&&!row.superseded_by).map(row=>[row.id,row])).values()];
    total=count;
    observeDownloads(items);
   }
   catch(e){if(!current.signal.aborted&&alive)error=e instanceof Error?e.message:'Could not load downloads.';}
   finally{if(current===request)loading=false;}
 }
 function input(){request?.abort();clearTimeout(debounce);debounce=setTimeout(()=>void load(),200);}
 async function start(){
  busy=true;error='';
  try{const item=await youtubeApi.download(query.trim(),video,audio);if(!alive)return;announceDownload(item);query='';optionsOpen=false;items=[item,...items];total++;await load();}
  catch(e){error=e instanceof Error?e.message:'Could not start download.';}
  finally{busy=false;}
 }
 async function action(item:Download,retry=false){
  if(changing.has(item.id))return;
  changing=new Set(changing).add(item.id);error='';
  try{
   const updated=await(retry?youtubeApi.retry(item.id):youtubeApi.cancel(item.id));
   if(!alive)return;
   if(retry){noticedJobs.delete(item.id);dismissNotification(downloadNoticeId(item));announceDownload(updated);}
   else {noticedJobs.delete(item.id);notify('Download canceled: '+(item.title||item.url||'YouTube video')+'.',{id:downloadNoticeId(item)});}
   request?.abort();
   const removed=items.filter(row=>row.id===item.id||row.id===updated.id).length;
   items=items.filter(row=>row.id!==item.id&&row.id!==updated.id);
   const visible=retry&&updated.status!=='cancelled'&&!updated.superseded_by&&updated.title.toLowerCase().includes(searchQuery.toLowerCase());
   if(visible)items=[updated,...items];
   total=Math.max(0,total-removed+(visible?1:0));
   await load(false,true);
  }
  catch(e){if(alive)error=e instanceof Error?e.message:'Could not update download.';}
  finally{changing=new Set([...changing].filter(id=>id!==item.id));}
 }
 async function poll(){if(section==='videos'&&!selected&&items.some(active))await load(false,true);if(alive)timer=setTimeout(poll,1000);}
 onMount(()=>{motion=document.documentElement.dataset.motion==='reduced'||window.matchMedia('(prefers-reduced-motion: reduce)').matches?0:200;void engine();void load();timer=setTimeout(poll,1000);});
 onDestroy(()=>{alive=false;managementRequest?.abort();playlistRequest?.abort();playlistNavigation?.abort();request?.abort();clearTimeout(timer);clearTimeout(debounce);});
</script>
<svelte:window on:keydown={escape} on:focus={refreshPlaylist}/>
<div class="youtube-surface" inert={!!selected||createOpen||!!membershipVideo||!!management}>
 <header><button class="menu" aria-label="Open Keivotos menu" on:click={()=>drawer=true}>☰</button><span class="module-title">YouTube</span>{#if section==='videos'}<div class="search-controls"><div class="options-anchor"><button class="options-toggle" aria-label="Download options" title="Download options" aria-expanded={optionsOpen} aria-controls="download-options" on:click={()=>optionsOpen=!optionsOpen}><svg class:opened={optionsOpen} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="m6 15 6-6 6 6"/></svg></button>
 {#if optionsOpen}
 <div id="download-options" class="download-options" transition:slide={{duration:motion}}>
 <label>Video<select aria-label="Video quality" bind:value={video}><option value="best">Best</option><option value="1080">1080p</option><option value="720">720p</option><option value="480">480p</option><option value="audio">Audio only</option></select></label>
 <label>Audio<select aria-label="Audio quality" bind:value={audio}><option value="best">Best</option><option value="192">Up to 192 kbps</option><option value="128">Up to 128 kbps</option></select></label>
 {#if status&&!status.ready}<p role="status">Missing: {status.missing.join(', ')}. <button on:click={engine}>Check again</button></p>{/if}
 </div>{/if}</div><input aria-label="Search downloads or paste a YouTube URL" placeholder="Search or paste a YouTube URL…" bind:value={query} on:input={input}/><div class="download-reveal" class:visible={urlMode} inert={!urlMode}><button class="download" aria-hidden={!urlMode} tabindex={urlMode?0:-1} disabled={busy||!urlMode||!status?.ready} on:click={start}>{busy?'Starting…':'Download'}</button></div></div>{/if}</header>
 <div class="youtube-body">
 <ModuleSidebarDock owner="youtube" label="YouTube"><YouTubeSidebar {section} on:select={(event)=>chooseSection(event.detail)}/></ModuleSidebarDock>
 {#if section==='videos'}
 <main>
  {#if error}<p role="alert">{error}</p>{/if}
  {#if !items.length&&!loading&&!error}<p class="empty">{searchQuery?'No videos match your search.':'No videos yet.'}</p>{/if}
  <div class="download-grid" aria-busy={loading}>
   {#each items as item (item.id)}
    <!-- svelte-ignore a11y_no_noninteractive_tabindex a11y_no_noninteractive_element_interactions -->
    <article data-job-id={item.id} role="group" aria-label={item.title||'YouTube video'} tabindex="0" on:contextmenu={event=>openLocation(event,item)} on:keydown={event=>locationKey(event,item)}>
     <div class="thumbnail">
      {#if item.status==='complete'}
       {#if isAudio(item)}
       <a href={filesApi.fileUrl(item.source_id,item.path)} target="_blank" rel="noreferrer" aria-label={`Open ${item.title}`}>
        {#if item.thumbnail}<img alt="" loading="lazy" src={filesApi.thumbnailUrl(item.source_id,item.thumbnail,600,item.id)} on:error={e=>(e.currentTarget as HTMLImageElement).style.display='none'}/>{/if}
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m8 5 11 7-11 7Z"/></svg>
       </a>
       {:else}
       <button class="play-download" on:click={()=>open(item)} aria-label={`Open ${item.title}`}>
        {#if item.thumbnail}<img alt="" loading="lazy" src={filesApi.thumbnailUrl(item.source_id,item.thumbnail,600,item.id)} on:error={e=>(e.currentTarget as HTMLImageElement).style.display='none'}/>{/if}
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m8 5 11 7-11 7Z"/></svg>
       </button>
       {/if}
      {/if}
      {#if active(item)}<button class="job-action" disabled={changing.has(item.id)} aria-label={`Cancel ${item.title||'download'}`} title="Cancel download" on:click={()=>action(item)}>×</button>
      {:else if item.status!=='complete'}<button class="job-action" disabled={changing.has(item.id)} aria-label={`Retry ${item.title||'download'}`} title="Retry download" on:click={()=>action(item,true)}>↻</button>{/if}
     </div>
     {#if active(item)}<div class="progress" role="progressbar" aria-label={item.title||'Download'} aria-valuenow={Math.round(item.progress)} aria-valuemin="0" aria-valuemax="100"><div class:pending={item.status==='queued'||item.status==='merging'} style={`width:${Math.max(2,item.progress)}%`}></div></div>{/if}
     <div class="download-title" title={item.title||'YouTube video'}>{item.title||'YouTube video'}</div>
     {#if !active(item)&&item.status!=='complete'}<p class="job-error">{item.status==='cancelled'?'Cancelled':item.error||'Interrupted. Retry to download again.'}</p>{/if}
    </article>
   {/each}
  </div>
  {#if items.length<total}<button class="more" disabled={loading} on:click={()=>load(true)}>Load more</button>{/if}
 </main>
 {:else if activePlaylist}
 <!-- svelte-ignore a11y_no_noninteractive_tabindex a11y_no_noninteractive_element_interactions -->
 <main class="playlist-detail" tabindex="0" on:contextmenu={event=>activePlaylist&&openPlaylistMenu(event,activePlaylist)} on:keydown={playlistMenuKey}>
  <header class="playlist-heading"><button type="button" class="playlist-back" aria-label="Back to playlists" on:click={backToPlaylists}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15.5 5-7 7 7 7"/></svg></button><h1>{activePlaylist.title}</h1><span>{playlistTotal} {playlistTotal===1?'video':'videos'}</span></header>
  {#if playlistError}<p role="alert">{playlistError} <button on:click={()=>loadPlaylist()}>Retry playlist</button></p>{/if}
  {#if playlistLoading&&!playlistRows.length}<p role="status">Loading playlist videos…</p>{:else if !playlistRows.length&&!playlistError}<p class="empty">No videos in this playlist. Use Add to playlists on a completed video.</p>{/if}
  <div class="download-grid" aria-busy={playlistLoading}>
   {#each playlistRows as row(row.job_id)}
    <article class="playlist-video" data-playlist-job-id={row.job_id}>
     <div class="thumbnail"><button class="play-download" type="button" disabled={!row.available||playlistLoading||navigating} aria-label={'Open '+(row.job?.title||'unavailable video')} title={row.unavailable_reason||row.job?.title||''} on:click={()=>choosePlaylistItem(row)}>
      {#if row.available&&row.job?.thumbnail}<img alt="" loading="lazy" src={filesApi.thumbnailUrl(row.job.source_id,row.job.thumbnail,600,row.job.id)}/>{/if}
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m8 5 11 7-11 7Z"/></svg>
     </button></div>
     <div class="download-title">{row.job?.title||'Unavailable video'}</div>{#if !row.available}<p class="job-error">{row.unavailable_reason}</p>{/if}
    </article>
   {/each}
  </div>
  {#if playlistRows.length<playlistTotal}<button class="more" disabled={playlistLoading||navigating} on:click={()=>loadPlaylist(true)}>Load more playlist videos</button>{/if}
 </main>
 {:else}<PlaylistsView bind:this={playlistsView} on:create={(event)=>createPlaylist(event.detail)} on:open={event=>openPlaylist(event.detail)} on:context={event=>openPlaylistMenu(event.detail.event,event.detail.playlist)}/>{/if}
 </div>
</div>
{#if createOpen}<PlaylistCreateDialog on:close={closeCreate} on:created={(event)=>created(event.detail)}/>{/if}
{#if playlistMenu}
 {#key playlistMenuSerial}<PlaylistContextMenu {...playlistMenu} on:close={()=>playlistMenu=null} on:rename={event=>beginManagement('rename',event.detail,playlistMenu!.anchor)} on:delete={event=>beginManagement('delete',event.detail,playlistMenu!.anchor)}/>{/key}
{/if}
{#if management}
 {#if management.kind==='rename'}<PlaylistRenameDialog playlist={management.playlist} on:close={closeManagement} on:saved={event=>playlistChanged(event.detail)}/>
 {:else}<PlaylistDeleteDialog playlist={management.playlist} on:close={closeManagement} on:saved={event=>playlistChanged(event.detail)}/>{/if}
{/if}
{#if membershipVideo}{#key membershipVideo.id}<PlaylistMembershipDialog video={membershipVideo} on:close={()=>closeMembership()} on:saved={()=>closeMembership(true)}/>{/key}{/if}
{#if drawer}<AppDrawer on:close={()=>drawer=false}/>{/if}
{#if locationMenu}
 {#key locationMenuSerial}<ModuleLocationMenu {...locationMenu} actions={[{label:"Add to playlists",disabled:!!addReason,reason:addReason,onSelect:chooseMembership}]} on:close={()=>locationMenu=null}/>{/key}
{/if}
{#if selected && playback}
 <MediaPlayer media={playback} owner="youtube" label="YouTube player" backLabel={activePlaylist?'Back to playlist':'Back to downloads'}
  hasPrevious={queuePrevious} hasNext={queueNext} canWrap={queueWrap}
  {navigating} {navigationNotice} hasPanel={!!activePlaylist} panelActive={panelOpen} onDismissPanel={dismissPanelForEscape}
  mayReplayCurrent={()=>!activePlaylist||playlistRows.some(row=>row.job_id===selected?.id&&row.available)}
  onClose={close} onNavigate={adjacent}
  onFailure={(_media,reason)=>youtubeApi.playbackError(selected!.id,reason)}>
   <svelte:fragment slot="transport">{#if activePlaylist}<button bind:this={panelAnchor} class="playlist-toggle" type="button" aria-label="Playlist videos" aria-expanded={panelOpen} title="Playlist videos" on:click={()=>panelOpen?dismissPanel():panelOpen=true}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M3 6h13M3 11h13M3 16h8m5-3 5 3-5 3Z"/></svg></button>{/if}</svelte:fragment>
   <svelte:fragment slot="panel">{#if activePlaylist}<PlaylistQueuePanel open={panelOpen} playlist={activePlaylist} current={selected} items={playlistRows} total={playlistTotal} loading={playlistLoading} error={playlistError} {navigating} on:close={dismissPanel} on:choose={event=>choosePlaylistItem(event.detail)} on:more={()=>loadPlaylist(true)} on:refresh={()=>loadPlaylist()}/>{/if}</svelte:fragment>
 </MediaPlayer>
{/if}
<style>
 .playlist-heading{padding:0 0 20px;border:0;min-height:0;gap:12px}.playlist-heading h1{font-size:20px;font-weight:600;overflow-wrap:anywhere}.playlist-heading span{font-size:13px;color:#aaa}.playlist-back{display:grid;place-items:center;flex:none;width:32px;height:32px;border-radius:8px;border:1px solid #303040}.playlist-back svg{display:block;width:20px;height:20px}.playlist-toggle{display:grid;place-items:center;width:40px;height:40px;flex:none;border-radius:50%;color:#eee}.playlist-toggle svg{width:24px;height:24px}.playlist-toggle:hover,.playlist-toggle[aria-expanded='true']{background:#ffffff20}.playlist-toggle:focus-visible,.playlist-back:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
 @media(max-width:600px){.playlist-toggle{width:32px;height:36px}}@media(max-width:400px){.playlist-toggle{width:28px}}
 .youtube-surface{display:flex;flex:1;min-height:0;min-width:0;flex-direction:column;background:#0f0f14;color:#e0e0e8}
 header{display:flex;align-items:center;gap:16px;min-height:60px;border-bottom:1px solid #2a2a3a;padding:8px 16px}
 .menu{width:36px;height:36px;border-radius:8px;background:#1e1e2e}.module-title{font-size:18px;font-weight:600}
 .search-controls{display:flex;align-items:center;gap:10px;flex:1;min-width:0;max-width:640px;margin:auto}
 header input{min-width:0;flex:1;border:1px solid #2a2a3a;border-radius:8px;padding:8px 12px;background:#1a1a24;font-size:14px}
 .options-anchor{position:relative;flex-shrink:0}.options-toggle{display:grid;place-items:center;width:40px;height:36px;border:0;border-radius:20px;background:#26262d;color:#ddd}.options-toggle svg{width:20px;height:20px;transition:transform .2s ease}.options-toggle svg.opened{transform:rotate(180deg)}
 .download-options{position:absolute;left:-16px;top:46px;z-index:30;width:350px;max-width:calc(100vw - 92px);padding:16px;display:grid;grid-template-columns:1fr 1fr;gap:14px;border-radius:16px;background:#191920;box-shadow:0 12px 32px #0008}
 label{display:flex;flex-direction:column;gap:8px;min-width:0;font-size:13px;color:#ddd}select{border:0;border-radius:20px;background:#26262d;padding:9px 12px;color:#ddd;font-size:13px;min-width:0;width:100%}
 .download-reveal{width:110px;flex-shrink:0;overflow:hidden;clip-path:inset(0 100% 0 0);opacity:0;transform:translateX(-8px);pointer-events:none;transition:clip-path .22s ease,opacity .22s ease,transform .22s ease}
 .download-reveal.visible{clip-path:inset(0);opacity:1;transform:translateX(0);pointer-events:auto}
 .download{width:100%;background:var(--accent);color:#111118;padding:9px 12px;border-radius:20px;font-size:13px;font-weight:600}.download:disabled{opacity:.4}.download-options p{grid-column:1/-1;font-size:12px;color:#aaa;padding:0 8px}
 @media(max-width:700px){.module-title{display:none}}
 @media(max-width:400px){header{gap:8px;padding-inline:8px}.search-controls{gap:6px}.download-reveal{width:92px}.download{padding-inline:6px}}
 .youtube-body{display:flex;position:relative;flex:1;min-width:0;min-height:0;overflow:hidden}
 main{overflow:auto;flex:1;padding:24px;min-width:0;min-height:0}.empty{color:#9ca3af;font-size:14px}.download-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(260px,100%),1fr));gap:24px 18px}
 article{min-width:0}.thumbnail{aspect-ratio:16/9;background:#1a1a24;border-radius:10px;position:relative;overflow:hidden}.thumbnail a,.thumbnail .play-download{display:grid;place-items:center;width:100%;height:100%}.thumbnail img{width:100%;height:100%;object-fit:cover;position:absolute}.thumbnail svg{width:28px;height:28px;fill:none;stroke:#ddd;stroke-width:1.5;position:relative;opacity:.7}
 .job-action{position:absolute;right:8px;top:8px;background:#292933;width:28px;height:28px;border-radius:50%;font-size:20px;display:grid;place-items:center}.download-title{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;line-clamp:2;height:2.7em;margin-top:8px;overflow:hidden;overflow-wrap:anywhere;line-height:1.35;font-size:14px}.job-error{font-size:12px;color:#aaa;margin-top:4px}
 .progress{height:6px;overflow:hidden;border-radius:999px;background:#20202b;margin-top:8px}.progress>div{height:100%;border-radius:999px;background:var(--accent);transition:width .2s ease}.pending{opacity:.65}.more{display:block;margin:24px auto;padding:8px 12px}
 button:focus-visible,input:focus-visible,select:focus-visible,a:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
 :global(html[data-motion='reduced']) .progress>div{transition:none}
 :global(html[data-motion='reduced']) .options-toggle svg{transition:none}
 :global(html[data-motion='reduced']) .download-reveal{transition:none}
 @media(prefers-reduced-motion:reduce){.options-toggle svg,.download-reveal{transition:none}}
</style>

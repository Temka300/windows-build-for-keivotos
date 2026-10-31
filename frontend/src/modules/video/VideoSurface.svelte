<script lang="ts">
  import {onMount,onDestroy,tick} from 'svelte';
  import AppDrawer from '../../components/AppDrawer.svelte';
  import {ApiError} from '../../lib/http';
  import {filesApi} from '../../lib/filesApi';
  import {settingsOpen,settingsInitialSection} from '../../lib/suiteStores';
  import {videoApi,type VideoItem,type VideoEpisode,type VideoTitle,type WatchPosition,type VideoDetails} from './api';
  import {openWatch,type WatchSession} from './watchProgress';
  import type {PlaybackMedia} from '../../lib/playback';
  import VideoInfo from './VideoInfo.svelte';
  import MediaPlayer from '../../components/MediaPlayer.svelte';
  import ModuleLocationMenu from '../../components/ModuleLocationMenu.svelte';
  import LibraryCategoryStrip from '../../components/LibraryCategoryStrip.svelte';
  import CategoryMembershipDialog from '../../components/CategoryMembershipDialog.svelte';
  import {libraryCategoriesRefresh,type CategoryMembership} from '../registry';
  let category='',categoryReady=false,categoryStrip:LibraryCategoryStrip,libraryElement:HTMLElement;
  const categoryPositions=new Map<string,{scroll:number;count:number}>();
  const detailPositions=new Map<string,{items:VideoEpisode[];total:number;scroll:number}>();
  let membershipItem:{item:VideoItem;anchor:HTMLElement}|null=null;
  let drawer=false,query='',searchQuery='',searchComposing=false,searchPending=false,loadedSearchKey='';
  let items:VideoTitle[]=[],total=0,loading=false,error='';
  let info:VideoTitle|null=null,infoComponent:VideoInfo,episodeRows:VideoEpisode[]=[],episodeTotal=0,episodeLoading=false,episodeError='',episodeScroll=0;
  let playback:PlaybackMedia|null=null,watchSession:WatchSession|null=null,watchNotice='',watchSerial=0;
  let positions:Record<string,WatchPosition|null>={},positionsError='',positionsSerial=0;
  const watchKey=(item:VideoItem)=>JSON.stringify([item.source_id,item.path]);
  let selected:VideoItem|null=null,queue:VideoItem[]=[],queueTotal=0,notice='',navigating=false;
  let request:AbortController|null=null,detailRequest:AbortController|null=null,queueRequest:AbortController|null=null,randomRequest:AbortController|null=null;
  let debounce:ReturnType<typeof setTimeout>,alive=true,refreshSerial=0;
  let returnFocus:HTMLElement|null=null,playbackFocus:HTMLElement|null=null;
  let locationMenu:{x:number;y:number;label:string;sourceId:string;relativePath:string;anchor:HTMLElement;item:VideoItem|null}|null=null,locationMenuSerial=0;
  const sameVideo=(a:VideoItem,b:VideoItem)=>a.source_id===b.source_id&&a.path===b.path;
  const detailKey=(title:VideoTitle)=>JSON.stringify([category,searchQuery,title.source_id,title.kind,title.path]);
  $: selectedIndex=selected?queue.findIndex(item=>sameVideo(item,selected!)):-1;

  $: canRandom=!!selected&&(queue.some(item=>!sameVideo(item,selected!)&&['mp4','m4v','webm'].includes(item.ext.toLowerCase()))||queue.length<queueTotal);
  function acknowledged(item:VideoItem,value:WatchPosition){
    const key=watchKey(item),old=positions[key];
    if(alive&&(!old||value.generation>old.generation||value.generation===old.generation&&value.sequence>old.sequence))positions={...positions,[key]:value};
  }
  export async function beforeNavigate(_slug:string){return !info||await infoComponent?.beforeNavigate();}
  async function metadataChanged(event:CustomEvent<VideoDetails>){
    detailPositions.clear();
    void load(false,true);if(info)void loadEpisodes(false,true);
  }
  async function filterTag(event:CustomEvent<string>){
    if(!await beforeNavigate('video'))return;
    await closeInfo();query=Array.from(event.detail).slice(0,256).join('');search();await tick();document.querySelector<HTMLInputElement>('.video-surface header input')?.focus();
  }
  function stopWatch(){watchSerial++;watchSession?.dispose();watchSession=null;watchNotice='';}
  async function selectVideo(item:VideoItem,owns:()=>boolean):Promise<boolean>{
    await watchSession?.flush();if(!owns())return false;
    stopWatch();const serial=watchSerial;
    const current=()=>alive&&serial===watchSerial&&owns();
    let next:WatchSession|null=null;
    try{if(['mp4','m4v','webm'].includes(item.ext.toLowerCase()))next=await openWatch(item,videoApi.watch,current,value=>acknowledged(item,value),caught=>{if(alive&&serial===watchSerial)watchNotice=caught?'Progress was not saved.':'';});}
    catch{if(current())watchNotice='Progress is unavailable.';}
    if(!current()){next?.dispose();return false;}
    watchSession=next;selected=item;playback={src:filesApi.fileUrl(item.source_id,item.path),ext:item.ext};return true;
  }
  async function retryWatch(){
    const item=selected;if(!item)return;
    if(watchSession){await watchSession.retry();return;}
    const serial=watchSerial,current=()=>alive&&serial===watchSerial&&selected===item;
    try{const next=await openWatch(item,videoApi.watch,current,value=>acknowledged(item,value),caught=>{if(current())watchNotice=caught?'Progress was not saved.':'';});
      if(current()){watchSession=next;watchNotice='';}else next?.dispose();
    }catch{if(current())watchNotice='Progress is unavailable.';}
  }
  async function loadPositions(){
    const title=info,serial=++positionsSerial,rows=[...episodeRows],before=positions;if(!title)return;
    try{
      const next:Record<string,WatchPosition|null>={};
      for(let offset=0;offset<rows.length;offset+=120){
        const result=await videoApi.watch.positions(rows.slice(offset,offset+120));
        if(!alive||serial!==positionsSerial||info!==title)return;
        for(const item of result.items)next[JSON.stringify([item.source_id,item.path])]=item.position;
      }
      const merged={...positions};
      for(const [key,value] of Object.entries(next)){
        const old=positions[key];
        if(old===before[key]||value&&old&&(value.generation>old.generation||value.generation===old.generation&&value.sequence>old.sequence))merged[key]=value;
      }
      positions=merged;positionsError='';
    }catch{if(alive&&serial===positionsSerial&&info===title)positionsError='Progress is unavailable.';}
  }
  function cancelPlaybackRequests(){randomRequest?.abort();randomRequest=null;queueRequest?.abort();queueRequest=null;navigating=false;}
  async function load(more=false,keepCount=false,wantedCount?:number){
    if(!categoryReady||searchComposing||searchPending)return;
    const key=JSON.stringify([searchQuery,category]);
    if(more&&loadedSearchKey!==key)return;
    request?.abort();const current=new AbortController();request=current;loading=true;error='';
    try{
      const wanted=wantedCount??(keepCount?items.length:0),scope=category,search=searchQuery;
      let next:VideoTitle[]=[],result:{items:VideoTitle[];total:number};
      do{
        result=await videoApi.catalog(search,more?items.length:next.length,current.signal,scope);
        if(current.signal.aborted||!alive)return;
        next=[...next,...result.items];
      }while(!more&&next.length<wanted&&next.length<result.total&&result.items.length>0);
      items=more?[...items,...next]:next;total=result.total;loadedSearchKey=key;
    }catch(caught){if(!current.signal.aborted&&alive)error=caught instanceof Error?caught.message:'Could not load videos.';}
    finally{if(request===current&&alive)loading=false;}
  }
  function search(){
    request?.abort();request=null;clearTimeout(debounce);loading=true;searchPending=true;
    if(searchComposing)return;
    debounce=setTimeout(()=>{if(!alive||searchComposing)return;searchQuery=query.trim();searchPending=false;void load();},200);
  }
  function beginSearchComposition(){searchComposing=true;search();}
  function endSearchComposition(){searchComposing=false;search();}
  function folders(){settingsInitialSection.set('roots');settingsOpen.set(true);}
  async function categoryChanged(event:CustomEvent<{category:string}>){
    const chosen=event.detail.category,changed=chosen!==category;
    if(changed){await watchSession?.flush();stopWatch();positionsSerial++;categoryPositions.set(category,{scroll:libraryElement?.scrollTop??0,count:items.length});cancelPlaybackRequests();detailRequest?.abort();info=null;selected=null;}
    const position=changed?categoryPositions.get(chosen):undefined;
    clearTimeout(debounce);category=chosen;categoryReady=true;
    if(!searchComposing){searchQuery=query.trim();searchPending=false;}
    if(changed){items=[];total=0;}
    await load(false,!changed,position?.count);
    if(changed&&category===chosen&&alive){await tick();if(libraryElement)libraryElement.scrollTop=position?.scroll??0;}
  }
  function rememberInfo(){
    if(!info||episodeError)return;
    detailPositions.set(detailKey(info),{items:episodeRows,total:episodeTotal,scroll:episodeScroll});
    if(detailPositions.size>32)detailPositions.delete(detailPositions.keys().next().value!);
  }
  async function loadEpisodes(more=false,keepCount=false){
    const title=info;if(!title)return;
    detailRequest?.abort();const current=new AbortController();detailRequest=current;episodeLoading=true;episodeError='';
    const key=detailKey(title),scope=category,search=searchQuery,wanted=keepCount?episodeRows.length:0;
    try{
      let next:VideoEpisode[]=[],result:{items:VideoEpisode[];total:number};
      do{
        result=await videoApi.episodes(title,search,more?episodeRows.length:next.length,current.signal,scope);
        if(!alive||current.signal.aborted||info!==title||detailKey(title)!==key)return;
        next=[...next,...result.items];
      }while(!more&&next.length<wanted&&next.length<result.total&&result.items.length>0);
      episodeRows=more?[...episodeRows,...next]:next;episodeTotal=result.total;
      if(selected&&title.kind==='series'){queue=episodeRows;queueTotal=episodeTotal;}
      if(selected&&episodeTotal===0){queue=[];queueTotal=0;}
      rememberInfo();void loadPositions();
    }catch(caught){
      if(!current.signal.aborted&&alive&&info===title){
        episodeError=caught instanceof Error?caught.message:'Could not load episodes.';
        if(caught instanceof ApiError&&caught.status===404){detailPositions.delete(key);episodeRows=[];episodeTotal=0;await closeInfo();void load(false,true);}
      }
    }
    finally{if(detailRequest===current&&alive)episodeLoading=false;}
  }
  async function openTitle(title:VideoTitle){
    if(searchPending||searchComposing||loadedSearchKey!==JSON.stringify([searchQuery,category]))return;
    returnFocus=document.activeElement as HTMLElement;info=title;episodeError='';positionsError='';
    const cached=detailPositions.get(detailKey(title));episodeRows=cached?.items??[];episodeTotal=cached?.total??title.episode_count;episodeScroll=cached?.scroll??0;
    await loadEpisodes(false,!!cached);
  }
  async function closeInfo(){
    if(!await beforeNavigate('video'))return;
    await watchSession?.flush();stopWatch();positionsSerial++;rememberInfo();detailRequest?.abort();cancelPlaybackRequests();selected=null;playback=null;info=null;
    await tick();if(returnFocus?.isConnected)returnFocus.focus({preventScroll:true});else categoryStrip?.focusSelected();
  }
  async function queuePage(title:VideoTitle,offset:number,signal:AbortSignal){
    return title.kind==='series'?videoApi.episodes(title,searchQuery,offset,signal,category):videoApi.library(searchQuery,offset,signal,category);
  }
  async function playEpisode(item:VideoEpisode){
    const title=info;if(!title||navigating)return;
    cancelPlaybackRequests();const current=new AbortController();queueRequest=current;navigating=true;episodeError='';
    playbackFocus=document.activeElement as HTMLElement;
    try{
      let next:VideoItem[]=title.kind==='series'?[...episodeRows]:[],count=title.kind==='series'?episodeTotal:0;
      while(!next.some(row=>sameVideo(row,item))){
        const page=await queuePage(title,next.length,current.signal);
        if(!alive||current.signal.aborted||info!==title)return;
        const previous=next.length;next=[...next,...page.items];count=page.total;
        if(next.length===previous||next.length>=count&&!next.some(row=>sameVideo(row,item)))throw new Error('Video list changed. Try again.');
      }
      if(!alive||current.signal.aborted||info!==title)return;
      queue=next;queueTotal=count;await selectVideo(next.find(row=>sameVideo(row,item))!,()=>alive&&!current.signal.aborted&&info===title);notice='';
    }catch(caught){if(alive&&!current.signal.aborted&&info===title)episodeError=caught instanceof Error?caught.message:'Could not open video.';}
    finally{if(queueRequest===current){queueRequest=null;navigating=false;}}
  }
  async function loadQueueMore(signal:AbortSignal){
    const title=info;if(!title)return;
    const page=await queuePage(title,queue.length,signal);
    if(!alive||signal.aborted||info!==title)return;
    if(page.total!==queueTotal||page.items.length===0)throw new Error('Video list changed. Try again.');
    queue=[...queue,...page.items];
    if(title.kind==='series'){episodeRows=queue as VideoEpisode[];episodeTotal=queueTotal;rememberInfo();}
    else if(items.length<total){await load(true);}
  }
  async function adjacent(direction:-1|1,wrap=false):Promise<boolean>{
    if(!selected||navigating||selectedIndex<0)return false;
    const chosen=selected;let next=selectedIndex+direction;
    if(wrap&&next<0)next=queueTotal-1;if(wrap&&next>=queueTotal)next=0;
    if(next<0||next>=queueTotal)return false;
    const current=new AbortController();queueRequest=current;navigating=true;
    try{
      while(next>=queue.length){await loadQueueMore(current.signal);if(!alive||current.signal.aborted||selected!==chosen)return false;}
      if(alive&&!current.signal.aborted&&selected===chosen&&queue[next]){const moved=await selectVideo(queue[next],()=>alive&&!current.signal.aborted&&selected===chosen);notice='';return moved;}
      return false;
    }catch(caught){if(alive&&!current.signal.aborted&&selected===chosen)notice=caught instanceof Error?caught.message:'Could not load another video.';return false;}
    finally{if(queueRequest===current){queueRequest=null;navigating=false;}}
  }
  async function randomVideo():Promise<boolean>{
    if(!selected||navigating)return false;
    const chosen=selected,title=info,scope=category,search=searchQuery,current=new AbortController();randomRequest=current;navigating=true;notice='';
    const owns=()=>alive&&!current.signal.aborted&&randomRequest===current&&selected===chosen&&info===title&&category===scope&&searchQuery===search;
    try{
      const result=await videoApi.random(search,chosen,current.signal,scope,title??undefined);
      if(!owns())return false;
      if(!result.item||result.offset===null){notice='No other videos available.';return false;}
      if(result.total!==queueTotal)throw new Error('Video list changed. Try again.');
      while(result.offset>=queue.length){await loadQueueMore(current.signal);if(!owns())return false;}
      const fresh=queue[result.offset];
      if(!fresh||!sameVideo(fresh,result.item)||sameVideo(fresh,chosen))throw new Error('Video list changed. Try again.');
      return await selectVideo(fresh,owns);
    }catch(caught){if(owns())notice=caught instanceof Error?caught.message:'Could not load another video.';return false;}
    finally{if(randomRequest===current){randomRequest=null;navigating=false;}}
  }
  async function close(){
    cancelPlaybackRequests();await watchSession?.flush();stopWatch();const previous=selected;selected=null;playback=null;
    await tick();if(playbackFocus?.isConnected)playbackFocus.focus({preventScroll:true});else infoComponent?.focusItem(previous??undefined);
  }
  function openLocation(event:MouseEvent|KeyboardEvent,item:VideoItem,group=false){
    event.preventDefault();event.stopPropagation();const anchor=event.currentTarget as HTMLElement,rect=anchor.getBoundingClientRect();
    locationMenu={x:event instanceof MouseEvent?event.clientX:rect.left+8,y:event instanceof MouseEvent?event.clientY:rect.bottom,
      label:item.name,sourceId:item.source_id,relativePath:item.path,anchor,item:group?null:item};locationMenuSerial++;
  }
  function locationKey(event:KeyboardEvent,item:VideoTitle){if(event.key==='ContextMenu'||event.shiftKey&&event.key==='F10')openLocation(event,item,item.kind==='series');}
  function categoryActions(){const menu=locationMenu;return menu?.item?[{label:'Add to categories',onSelect:()=>membershipItem={item:menu.item!,anchor:menu.anchor}}]:[];}
  async function closeMembership(){
    const anchor=membershipItem?.anchor;membershipItem=null;await tick();
    if(anchor?.isConnected)anchor.focus({preventScroll:true});else if(info)infoComponent?.focusItem();else categoryStrip?.focusSelected();
  }
  function membershipSaved(event:CustomEvent<CategoryMembership>){
    const saved=membershipItem?.item;if(!saved)return;
    const keep=category==='default'?event.detail.category_ids.length===0:!category||event.detail.category_ids.includes(category);
    if(!keep){
      const contained=episodeRows.some(item=>sameVideo(item,saved));episodeRows=episodeRows.filter(item=>!sameVideo(item,saved));
      if(contained)episodeTotal=Math.max(0,episodeTotal-1);
      const remove=items.find(item=>item.source_id===saved.source_id&&(item.kind==='video'?item.path===saved.path:contained&&info?.source_id===item.source_id&&info?.path===item.path&&episodeTotal===0));
      if(remove){items=items.filter(item=>item!==remove);total=Math.max(0,total-1);}
    }
    detailPositions.clear();void closeMembership();libraryCategoriesRefresh.update(value=>value+1);void load(false,true);if(info)void loadEpisodes(false,true);
  }
  async function refreshQueue(){
    const title=info,chosen=selected;if(!title||!chosen||navigating||title.kind!=='video')return;
    const wanted=queue.length,current=new AbortController();queueRequest?.abort();queueRequest=current;
    try{
      let next:VideoItem[]=[],page:{items:VideoItem[];total:number};
      do{
        page=await queuePage(title,next.length,current.signal);
        if(!alive||current.signal.aborted||info!==title||selected!==chosen)return;
        next=[...next,...page.items];
      }while(next.length<wanted&&next.length<page.total&&page.items.length>0);
      queue=next;queueTotal=page.total;
    }catch(caught){if(alive&&!current.signal.aborted&&selected===chosen)notice=caught instanceof Error?caught.message:'Could not refresh videos.';}
    finally{if(queueRequest===current)queueRequest=null;}
  }
  async function refreshVisible(){
    if(!alive||$settingsOpen)return;
    const serial=++refreshSerial,target=info;
    try{
      if(target){
        const registered=await filesApi.listSources();
        if(!alive||serial!==refreshSerial||info!==target)return;
        if(!registered.some(source=>source.source_id===target.source_id&&source.visible&&source.role==='video')){
          episodeError='Video is unavailable.';detailPositions.clear();await closeInfo();
        }else{await loadEpisodes(false,true);infoComponent?.refreshSavedDetails();}
      }
      if(alive){void load(false,true);if(info===target)void refreshQueue();}
    }catch(caught){if(alive&&serial===refreshSerial)error=caught instanceof Error?caught.message:'Could not refresh videos.';}
  }
  onMount(()=>{
    const focus=()=>void refreshVisible();window.addEventListener('focus',focus);
    let wasOpen=false;const unsubscribe=settingsOpen.subscribe(open=>{if(wasOpen&&!open)void refreshVisible();wasOpen=open;});
    return()=>{window.removeEventListener('focus',focus);unsubscribe();};
  });
  onDestroy(()=>{stopWatch();positionsSerial++;alive=false;refreshSerial++;clearTimeout(debounce);request?.abort();detailRequest?.abort();cancelPlaybackRequests();detailPositions.clear();});
</script>

<div class="video-surface" inert={!!info||!!selected||!!membershipItem}>
  <header>
    <button aria-label="Open Keivotos menu" title="Open Keivotos menu" on:click={()=>drawer=true}>☰</button>
    <span class="module-title">Video</span>
    <input aria-label="Search videos" placeholder="Search videos…" bind:value={query} on:input={search} on:compositionstart={beginSearchComposition} on:compositionend={endSearchComposition}/>
  </header>
  <LibraryCategoryStrip owner="video" bind:this={categoryStrip} on:change={categoryChanged}/>
  <main bind:this={libraryElement}>
    {#if error}<p role="alert">{error} <button on:click={()=>load()}>Retry</button></p>{/if}
    {#if categoryReady&&!loading&&!error&&!items.length}<p class="empty">{#if query}No videos found.{:else if category}No videos in this category.{:else}<button on:click={folders}>Add Video folders in Settings</button>{/if}</p>{/if}
    <div class="video-grid" aria-busy={loading}>
      {#each items as item (`${item.source_id}/${item.path}`)}
        <button class="video-tile" title={item.name} disabled={searchPending||searchComposing||loadedSearchKey!==JSON.stringify([searchQuery,category])} on:click={()=>openTitle(item)} on:contextmenu={event=>openLocation(event,item,item.kind==='series')} on:keydown={event=>locationKey(event,item)}>
          <span class="thumbnail"><span aria-hidden="true">▷</span><img loading="lazy" alt="" src={filesApi.thumbnailUrl(item.cover.source_id,item.cover.path,600,`${item.cover.mtime}:${item.cover.size}`)} on:error={event=>(event.currentTarget as HTMLImageElement).style.display='none'}/></span>
          <span class="video-name">{item.name}</span>
        </button>
      {/each}
    </div>
    {#if items.length<total}<button class="more" disabled={loading||searchPending||searchComposing||loadedSearchKey!==JSON.stringify([searchQuery,category])} on:click={()=>load(true)}>{loading?'Loading…':'Load more'}</button>{/if}
  </main>
</div>
{#if drawer}<AppDrawer {beforeNavigate} on:close={()=>drawer=false}/>{/if}
{#if info}
  {#key detailKey(info)}<VideoInfo bind:this={infoComponent} title={info} items={episodeRows} total={episodeTotal} loading={episodeLoading} error={episodeError} {positions} progressError={positionsError} inactive={!!selected||!!membershipItem} preparing={navigating&&!selected} bind:scrollTop={episodeScroll}
    on:metadata={metadataChanged} on:tag={filterTag} on:progressRetry={loadPositions} on:close={closeInfo} on:play={event=>playEpisode(event.detail)} on:more={()=>loadEpisodes(true)} on:retry={()=>loadEpisodes(false,true)} on:location={event=>openLocation(event.detail.event,event.detail.item)}/>{/key}
{/if}
{#if locationMenu}{#key locationMenuSerial}<ModuleLocationMenu x={locationMenu.x} y={locationMenu.y} label={locationMenu.label} sourceId={locationMenu.sourceId} relativePath={locationMenu.relativePath} anchor={locationMenu.anchor} actions={categoryActions()} on:close={()=>locationMenu=null}/>{/key}{/if}
{#if membershipItem}<CategoryMembershipDialog client={videoApi.categories} item={membershipItem.item} label={membershipItem.item.name} on:close={closeMembership} on:saved={membershipSaved}/>{/if}
{#if selected&&playback}
  <MediaPlayer media={playback} owner="video" backLabel="Back to episodes" hasPrevious={selectedIndex>0} hasNext={selectedIndex>=0&&selectedIndex<queueTotal-1} canWrap={selectedIndex>=0&&queueTotal>1}
    progress={watchSession??undefined} progressNotice={watchNotice} onRetryProgress={retryWatch} {navigating} navigationNotice={notice} onClose={close} onNavigate={adjacent} {canRandom} onRandom={randomVideo} onFailure={(_media,reason)=>videoApi.error(selected!,reason)}/>
{/if}

<style>
 .video-surface{display:flex;flex:1;min-height:0;flex-direction:column;background:#0f0f14;color:#e0e0e8}
 header{display:flex;align-items:center;gap:16px;min-height:60px;border-bottom:1px solid #2a2a3a;padding:8px 16px}
 header button{width:36px;height:36px;border-radius:8px;background:#1e1e2e}
 .module-title{font-size:18px;font-weight:600}
 header input{max-width:640px;width:100%;margin-left:auto;margin-right:auto;border:1px solid #2a2a3a;border-radius:8px;padding:8px 12px;background:#1a1a24;font-size:14px}
 main{overflow:auto;flex:1;padding:20px;min-height:0}
 .video-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(260px,100%),1fr));gap:24px 18px}
 .video-tile{text-align:left;min-width:0}
 .thumbnail{display:grid;place-items:center;position:relative;aspect-ratio:16/9;background:#1a1a24;border-radius:10px;overflow:hidden;color:#858599}
 .thumbnail img{position:absolute;width:100%;height:100%;object-fit:cover}
 .video-name{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;line-clamp:2;height:2.7em;margin-top:8px;overflow:hidden;overflow-wrap:anywhere;line-height:1.35;font-size:14px}
 .empty{text-align:center;color:#9292a5;padding:40px}
 .empty button,.more{padding:8px 12px;border:1px solid #303040;border-radius:8px}
 .more{display:block;margin:24px auto}
 button:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
 button:disabled{opacity:.45}
</style>

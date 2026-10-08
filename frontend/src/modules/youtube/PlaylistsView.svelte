<script lang="ts">
  import {createEventDispatcher,onMount,onDestroy} from 'svelte';
  import {youtubeApi,type Playlist} from './api';
  const dispatch=createEventDispatcher<{create:HTMLButtonElement;open:Playlist}>();
  let items:Playlist[]=[],total=0,error='',loading=true,alive=true,request:AbortController|null=null;
  async function load(more=false,refresh=false){
    request?.abort();const current=new AbortController();request=current;loading=true;error='';
    const target=refresh?Math.max(60,items.length):60;
    try{
      let offset=more?items.length:0,rows:Playlist[]=[],count=0;
      do{
        const result=await youtubeApi.playlists(offset,current.signal,Math.min(120,target-rows.length));
        if(!alive||current.signal.aborted)return;
        rows.push(...result.items);count=result.total;offset+=result.items.length;
        if(!result.items.length)break;
      }while(!more&&refresh&&rows.length<target&&offset<count);
      items=[...new Map((more?[...items,...rows]:rows).map(item=>[item.id,item])).values()];total=count;
    }catch(e){if(alive&&!current.signal.aborted)error=e instanceof Error?e.message:'Could not load playlists.';}
    finally{if(current===request)loading=false;}
  }
  export function acceptCreated(playlist:Playlist){
    request?.abort();if(!items.some(item=>item.id===playlist.id))total++;
    items=[playlist,...items.filter(item=>item.id!==playlist.id)];loading=false;error='';void load(false,true);
  }
  onMount(()=>{void load();});onDestroy(()=>{alive=false;request?.abort();});
</script>
<svelte:window on:focus={()=>void load(false,true)}/>
<section class="playlists-view" aria-label="Playlists">
  <header><h1>Playlists</h1><button class="add" type="button" aria-label="New playlist" title="New playlist" on:click={(event)=>dispatch('create',event.currentTarget)}>+</button></header>
  {#if error}<p role="alert">{error} <button class="retry" on:click={()=>load(false,true)}>Retry</button></p>{/if}
  {#if loading&&!items.length}<p role="status">Loading playlists…</p>{:else if !items.length&&!error}<p class="empty">No playlists yet.</p>{/if}
  <div class="playlist-list" aria-busy={loading}>
    {#each items as playlist(playlist.id)}<article class="playlist-row" data-playlist-id={playlist.id}><button class="open-playlist" type="button" aria-label={'Open playlist '+playlist.title} on:click={()=>dispatch('open',playlist)}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M3 6h13M3 11h13M3 16h8m5-3 5 3-5 3Z"/></svg><div><h2 title={playlist.title}>{playlist.title}</h2><p>{playlist.item_count} {playlist.item_count===1?'video':'videos'}</p></div></button></article>{/each}
  </div>
  {#if items.length<total}<button class="more" disabled={loading} on:click={()=>load(true)}>Load more playlists</button>{/if}
</section>
<style>
  .playlists-view{padding:24px;flex:1;min-width:0;overflow:auto}header{display:flex;align-items:center;gap:12px;margin-bottom:24px}h1{font-size:20px;font-weight:600}
  .add{display:grid;place-items:center;width:32px;height:32px;border:1px solid #303040;border-radius:8px;background:#1d1d29;color:#d8b4fe;font-size:23px}.add:hover{background:#a855f71a;border-color:#c084fc59}
  .playlist-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(260px,100%),1fr));gap:12px}.playlist-row{min-width:0;border:1px solid #303040;border-radius:10px;background:#15151e}.open-playlist{display:flex;align-items:center;gap:14px;width:100%;text-align:left;padding:16px;border-radius:10px}.open-playlist:hover{background:#ffffff08}
  .playlist-row svg{width:28px;height:28px;flex-shrink:0;color:#9ca3af}.playlist-row div{min-width:0}h2{font-size:14px;font-weight:500;overflow-wrap:anywhere}p{font-size:13px;color:#9ca3af}.playlist-row p{margin-top:5px}.retry{color:#e9d5ff;text-decoration:underline}.more{display:block;margin:24px auto;padding:8px 12px}
  button:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
</style>

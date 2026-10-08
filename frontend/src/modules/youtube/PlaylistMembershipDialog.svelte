<script lang="ts">
  import {createEventDispatcher,onMount,onDestroy,tick} from 'svelte';
  import {ApiError} from '../../lib/http';
  import {youtubeApi,type Download,type Playlist,type PlaylistMembership} from './api';
  import PlaylistCreateDialog from './PlaylistCreateDialog.svelte';

  export let video:Download;
  const dispatch=createEventDispatcher<{close:void;saved:PlaylistMembership}>();
  let dialog:HTMLDivElement,createButton:HTMLButtonElement;
  let items:Playlist[]=[],total=0,selected=new Set<string>(),version=0;
  let loading=true,busy=false,creating=false,conflict=false,error='',alive=true;
  let request:AbortController|null=null,failure:'load'|'save'|null=null,failedMore=false;
  const identity=video.id;

  function controls(){return [...dialog.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled)')];}
  async function focusFirst(){await tick();if(alive&&!creating)(controls()[0]??dialog).focus();}
  function close(){if(!busy&&!creating)dispatch('close');}
  function keys(event:KeyboardEvent){
    if(creating)return;
    if(event.key==='Escape'){event.preventDefault();event.stopPropagation();close();}
    if(event.key==='Tab'){
      const nodes=controls(),first=nodes[0],last=nodes[nodes.length-1];
      if(!first){event.preventDefault();dialog.focus();return;}
      if(event.shiftKey&&(document.activeElement===first||document.activeElement===dialog)){event.preventDefault();last.focus();}
      else if(!event.shiftKey&&(document.activeElement===last||document.activeElement===dialog)){event.preventDefault();first.focus();}
    }
  }
  async function load(more=false){
    if(busy)return;
    request?.abort();const current=new AbortController();request=current;loading=true;error='';failure=null;failedMore=more;
    try{
      const offset=more?items.length:0;
      const [list,membership]=await Promise.all([
        youtubeApi.playlists(offset,current.signal),
        more?Promise.resolve(null):youtubeApi.membership(identity,current.signal)
      ]);
      if(!alive||current.signal.aborted)return;
      items=[...new Map((more?[...items,...list.items]:list.items).map(item=>[item.id,item])).values()];
      total=list.total;
      if(membership){selected=new Set(membership.playlist_ids);version=membership.version;conflict=false;}
    }catch(e){if(alive&&!current.signal.aborted){failure='load';error=e instanceof Error?e.message:'Could not load playlists.';}}
    finally{if(current===request){loading=false;await focusFirst();}}
  }
  function toggle(id:string,checked:boolean){
    if(busy||loading||conflict)return;
    selected=new Set(selected);
    if(checked)selected.add(id);else selected.delete(id);
  }
  async function save(){
    if(busy||loading||conflict||error||selected.size>256)return;
    busy=true;error='';failure=null;
    const pending=[...selected],expected=version;
    try{
      const result=await youtubeApi.saveMembership(identity,pending,expected);
      if(alive)dispatch('saved',result);
    }catch(e){
      if(alive){failure='save';conflict=e instanceof ApiError&&e.status===409;error=e instanceof Error?e.message:'Could not save playlists.';}
    }finally{if(alive){busy=false;await focusFirst();}}
  }
  function retry(){if(failure==='save'){error='';void save();}else void load(failedMore);}
  async function created(playlist:Playlist){
    items=[playlist,...items.filter(item=>item.id!==playlist.id)];total++;
    creating=false;await focusFirst();
  }
  async function closeCreate(){creating=false;await tick();if(alive)createButton?.focus();}
  onMount(()=>{dialog.focus();void load();});onDestroy(()=>{alive=false;request?.abort();});
</script>
<svelte:window on:keydown={keys}/>
<div class="playlist-membership-backdrop" inert={creating}>
  <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
  <div class="playlist-membership-dialog" role="dialog" aria-modal="true" aria-label="Add to playlists" tabindex="-1" bind:this={dialog}>
    <header><h2>Add to playlists</h2><button type="button" aria-label="Close playlist selection" disabled={busy} on:click={close}>×</button></header>
    <p class="video-title" title={video.title}>{video.title||'YouTube video'}</p>
    {#if loading&&!items.length}<p role="status">Loading playlists…</p>{/if}
    {#if error}<p class="error" role="alert">{error}</p>
      {#if conflict}<p>Your selections are still shown. Reload saved selection before trying again.</p><button class="retry" disabled={busy||loading} on:click={()=>load()}>Reload saved selection</button>
      {:else}<button class="retry" disabled={busy||loading} on:click={retry}>Retry</button>{/if}
    {/if}
    {#if !loading&&!error&&!items.length}<p>No playlists yet.</p><button bind:this={createButton} type="button" class="create" on:click={()=>creating=true}>Create playlist</button>{/if}
    <div class="playlist-choices" aria-busy={loading}>
      {#each items as playlist(playlist.id)}
        <label class="playlist-choice"><input type="checkbox" checked={selected.has(playlist.id)} disabled={busy||loading||conflict} on:change={event=>toggle(playlist.id,event.currentTarget.checked)}/><span>{playlist.title}</span></label>
      {/each}
    </div>
    {#if items.length<total}<button class="more" type="button" disabled={busy||loading} on:click={()=>load(true)}>Load more playlists</button>{/if}
    {#if selected.size>256}<p class="error" role="alert">Choose up to 256 playlists.</p>{/if}
    <footer><button type="button" disabled={busy} on:click={close}>Cancel</button><button class="save" type="button" disabled={busy||loading||conflict||!!error||selected.size>256} on:click={save}>{busy?'Saving…':'Save'}</button></footer>
  </div>
</div>
{#if creating}<PlaylistCreateDialog on:close={closeCreate} on:created={event=>created(event.detail)}/>{/if}
<style>
  .playlist-membership-backdrop{position:fixed;inset:0;z-index:110;display:flex;align-items:center;justify-content:center;background:#0008;padding:16px}
  .playlist-membership-dialog{width:400px;max-width:100%;max-height:calc(100dvh - 32px);overflow:auto;padding:20px;border:1px solid #303040;border-radius:14px;background:#14141c;color:#e5e7eb;box-shadow:0 20px 60px #0008}
  header{display:flex;align-items:center;justify-content:space-between;gap:12px}h2{font-size:16px;font-weight:600}header button{width:28px;height:28px;border-radius:7px;color:#9ca3af;font-size:22px}
  .video-title{margin:10px 0 16px;overflow-wrap:anywhere;color:#bbb;font-size:13px}.playlist-choices{display:flex;flex-direction:column;gap:6px;max-height:320px;overflow:auto}
  .playlist-choice{display:flex;align-items:center;gap:12px;border:1px solid #303040;border-radius:8px;padding:11px;cursor:pointer;font-size:14px}.playlist-choice span{min-width:0;overflow-wrap:anywhere}.playlist-choice:has(input:checked){background:#a855f71a;border-color:#c084fc59}input{accent-color:var(--accent);flex-shrink:0}
  p{font-size:13px;color:#9ca3af}.error{color:#fca5a5;margin-bottom:8px}.retry{color:#e9d5ff;text-decoration:underline;margin:10px 0}.more{display:block;margin:12px auto}.create{margin-top:12px}
  footer{display:flex;justify-content:flex-end;gap:12px;margin-top:20px}footer button,.create{padding:8px 16px;border:1px solid #303040;border-radius:8px;font-size:13px}.save,.create{background:#a855f726;color:#f3e8ff;border-color:#c084fc26}.save:hover,.create:hover{background:#a855f740}
  button:disabled{opacity:.4}button:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
</style>

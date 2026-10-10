<script lang="ts">
 import {createEventDispatcher,onMount,onDestroy,tick} from 'svelte';
 import {youtubeApi,type Playlist} from './api';
 import {ApiError} from '../../lib/http';
 export let playlist:Playlist;
 const dispatch=createEventDispatcher<{close:void;saved:Playlist}>();
 let owner=playlist,busy=false,error='',conflict=false,alive=true,token=crypto.randomUUID();
 let dialog:HTMLDivElement,cancel:HTMLButtonElement;
 onMount(()=>cancel.focus());onDestroy(()=>{alive=false;});
 function keys(event:KeyboardEvent){
  if(event.key==='Escape'){event.preventDefault();event.stopPropagation();if(!busy)dispatch('close');}
  if(event.key==='Tab'){const nodes=[...dialog.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')],first=nodes[0],last=nodes.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}}
 }
 async function reload(){busy=true;error='';try{const value=await youtubeApi.playlistItems(owner.id,0,undefined,1);if(alive){owner=value.playlist;token=crypto.randomUUID();conflict=false;}}catch(e){if(alive)error=e instanceof Error?e.message:'Could not reload playlist.';}finally{if(alive){busy=false;await tick();cancel.focus();}}}
 async function save(){
  if(busy||conflict)return;busy=true;error='';
  try{const result=await youtubeApi.archivePlaylist(owner.id,owner.revision,owner.item_count,token);if(alive)dispatch('saved',result);}
  catch(e){if(alive){conflict=e instanceof ApiError&&[404,409].includes(e.status);error=e instanceof Error?e.message:'Could not delete playlist.';}}
  finally{if(alive){busy=false;await tick();cancel.focus();}}
 }
</script>
<svelte:window on:keydown={keys}/>
<div class="playlist-dialog-backdrop">
 <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
 <div class="playlist-delete-dialog" role="dialog" aria-modal="true" aria-label="Delete playlist" tabindex="-1" bind:this={dialog}>
  <header><h2>Delete playlist</h2><button type="button" aria-label="Close delete playlist" disabled={busy} on:click={()=>dispatch('close')}>×</button></header>
  <p class="confirmation">Delete “{owner.title}” with {owner.item_count} {owner.item_count===1?'video':'videos'}?</p><p>The playlist will be hidden. Its history and video memberships are retained. Your videos and thumbnails stay in place.</p>
  {#if error}<p role="alert">{error}</p>{/if}
  {#if conflict}<p>Reload the saved title and video count before confirming.</p><button class="reload" type="button" disabled={busy} on:click={reload}>Reload playlist</button>{/if}
  <footer><button bind:this={cancel} type="button" disabled={busy} on:click={()=>dispatch('close')}>Cancel</button><button class="delete" type="button" disabled={busy||conflict} on:click={save}>{busy?'Deleting…':'Delete playlist'}</button></footer>
 </div>
</div>
<style>
 .playlist-dialog-backdrop{position:fixed;inset:0;z-index:110;display:flex;align-items:center;justify-content:center;background:#0008;padding:16px}.playlist-delete-dialog{width:400px;max-width:100%;max-height:calc(100vh - 32px);overflow:auto;border:1px solid #303040;border-radius:14px;background:#14141c;color:#e5e7eb;box-shadow:0 20px 60px #0008;padding:20px}header{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px}h2{font-size:16px;font-weight:600}header button{width:28px;height:28px;border-radius:7px;color:#9ca3af;font-size:22px}p{font-size:13px;color:#aaa;overflow-wrap:anywhere;margin-top:12px}.confirmation{color:#e5e7eb}p[role='alert']{color:#fca5a5}.reload{margin-top:10px;color:var(--accent-light);text-decoration:underline}footer{display:flex;justify-content:flex-end;gap:10px;margin-top:20px}footer button{border-radius:8px;padding:8px 16px;border:1px solid #303040;font-size:13px}.delete{background:#7f1d1d40;color:#fca5a5;border-color:#fca5a533}button:disabled{opacity:.4}button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
</style>

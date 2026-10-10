<script lang="ts">
 import {createEventDispatcher,onMount,onDestroy,tick} from 'svelte';
 import {youtubeApi,type Playlist} from './api';
 import {ApiError} from '../../lib/http';
 export let playlist:Playlist;
 const dispatch=createEventDispatcher<{close:void;saved:Playlist}>();
 let owner=playlist,title=playlist.title,busy=false,error='',conflict=false,alive=true,token=crypto.randomUUID(),submitted:string|null=null;
 let dialog:HTMLDivElement,input:HTMLInputElement;
 onMount(()=>{input.focus();input.select();});onDestroy(()=>{alive=false;});
 function keys(event:KeyboardEvent){
  if(event.key==='Escape'){event.preventDefault();event.stopPropagation();if(!busy)dispatch('close');}
  if(event.key==='Tab'){const nodes=[...dialog.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled)')],first=nodes[0],last=nodes.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}}
 }
 async function reload(){busy=true;error='';try{const value=await youtubeApi.playlistItems(owner.id,0,undefined,1);if(alive){owner=value.playlist;token=crypto.randomUUID();submitted=null;conflict=false;}}catch(e){if(alive)error=e instanceof Error?e.message:'Could not reload playlist.';}finally{if(alive){busy=false;await tick();input.focus();}}}
 async function save(){
  const value=title.trim();if(busy||conflict||!value)return;
  if(submitted!==null&&submitted!==value)token=crypto.randomUUID();submitted=value;busy=true;error='';
  try{const result=await youtubeApi.renamePlaylist(owner.id,value,owner.revision,token);if(alive)dispatch('saved',result);}
  catch(e){if(alive){conflict=e instanceof ApiError&&[404,409].includes(e.status);error=e instanceof Error?e.message:'Could not rename playlist.';}}
  finally{if(alive){busy=false;await tick();input.focus();}}
 }
</script>
<svelte:window on:keydown={keys}/>
<div class="playlist-dialog-backdrop">
 <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
 <div class="playlist-rename-dialog" role="dialog" aria-modal="true" aria-label="Rename playlist" tabindex="-1" bind:this={dialog}>
  <header><h2>Rename playlist</h2><button type="button" aria-label="Close rename playlist" disabled={busy} on:click={()=>dispatch('close')}>×</button></header>
  <form on:submit|preventDefault={save}><label for="playlist-rename-title">Title</label><input id="playlist-rename-title" bind:this={input} bind:value={title} maxlength="120" autocomplete="off" disabled={busy} required/>
   {#if error}<p role="alert">{error}</p>{/if}
   {#if conflict}<p>Your entered title is retained. Reload the saved playlist before trying again.</p><button class="reload" type="button" disabled={busy} on:click={reload}>Reload playlist</button>{/if}
   <footer><button type="button" disabled={busy} on:click={()=>dispatch('close')}>Cancel</button><button class="save" type="submit" disabled={busy||conflict||!title.trim()}>{busy?'Saving…':'Save'}</button></footer>
  </form>
 </div>
</div>
<style>
 .playlist-dialog-backdrop{position:fixed;inset:0;z-index:110;display:flex;align-items:center;justify-content:center;background:#0008;padding:16px}.playlist-rename-dialog{width:360px;max-width:100%;max-height:calc(100vh - 32px);overflow:auto;border:1px solid #303040;border-radius:14px;background:#14141c;color:#e5e7eb;box-shadow:0 20px 60px #0008;padding:20px}header{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px}h2{font-size:16px;font-weight:600}header button{width:28px;height:28px;border-radius:7px;color:#9ca3af;font-size:22px}label{display:block;font-size:13px;color:#aaa;margin-bottom:7px}input{width:100%;border:1px solid #303040;border-radius:8px;background:#0d0d13;padding:9px 11px;font-size:14px}footer{display:flex;justify-content:flex-end;gap:10px;margin-top:20px}footer button{border-radius:8px;padding:8px 16px;border:1px solid #303040;font-size:13px}.save{background:color-mix(in srgb,var(--accent) 15%,transparent);color:var(--accent-light)}p{font-size:13px;margin-top:12px;color:#aaa;overflow-wrap:anywhere}p[role='alert']{color:#fca5a5}.reload{margin-top:10px;color:var(--accent-light);text-decoration:underline}button:disabled{opacity:.4}button:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
</style>

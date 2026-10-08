<script lang="ts">
  import {createEventDispatcher,onMount,onDestroy,tick} from 'svelte';
  import {youtubeApi,type Playlist} from './api';
  const dispatch=createEventDispatcher<{created:Playlist;close:void}>();
  let dialog:HTMLDivElement,input:HTMLInputElement,title='',error='',busy=false,alive=true;
  let token=crypto.randomUUID(),submittedTitle:string|null=null;
  onMount(()=>input.focus());onDestroy(()=>{alive=false;});
  function close(){if(!busy)dispatch('close');}
  function keys(event:KeyboardEvent){
    if(event.key==='Escape'){event.preventDefault();event.stopPropagation();close();}
    if(event.key==='Tab'){
      const controls=[...dialog.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled)')];
      if(!controls.length){event.preventDefault();dialog.focus();return;}
      const first=controls[0],last=controls[controls.length-1];
      if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
      else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
    }
  }
  async function save(){
    const value=title.trim();if(busy||!value)return;
    if(submittedTitle!==null&&submittedTitle!==value)token=crypto.randomUUID();
    submittedTitle=value;busy=true;error='';
    try{const playlist=await youtubeApi.createPlaylist(value,token);if(alive)dispatch('created',playlist);}
    catch(e){if(alive)error=e instanceof Error?e.message:'Could not create playlist.';}
    finally{if(alive){busy=false;if(error){await tick();if(alive)input.focus();}}}
  }
</script>
<svelte:window on:keydown={keys}/>
<div class="playlist-dialog-backdrop">
  <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
  <div class="playlist-create-dialog" role="dialog" aria-modal="true" aria-label="New playlist" tabindex="-1" bind:this={dialog}>
    <header><h2>New playlist</h2><button type="button" aria-label="Close new playlist" disabled={busy} on:click={close}>×</button></header>
    <form on:submit|preventDefault={save}>
      <label for="playlist-title">Title</label><input id="playlist-title" bind:this={input} bind:value={title} maxlength="120" disabled={busy} autocomplete="off" required/>
      {#if error}<p role="alert">{error}</p>{/if}
      <footer><button class="create" type="submit" disabled={busy||!title.trim()}>{busy?'Creating…':'Create'}</button></footer>
    </form>
  </div>
</div>
<style>
  .playlist-dialog-backdrop{position:fixed;inset:0;z-index:110;display:flex;align-items:center;justify-content:center;background:#0008;padding:16px}
  .playlist-create-dialog{width:360px;max-width:100%;border:1px solid #303040;border-radius:14px;background:#14141c;color:#e5e7eb;box-shadow:0 20px 60px #0008;padding:20px}
  header{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px}h2{font-size:16px;font-weight:600}header button{width:28px;height:28px;border-radius:7px;color:#9ca3af;font-size:22px}header button:hover{background:#ffffff08}
  label{display:block;font-size:13px;color:#aaa;margin-bottom:7px}input{width:100%;border:1px solid #303040;border-radius:8px;background:#0d0d13;padding:9px 11px;font-size:14px}
  footer{display:flex;justify-content:flex-end;margin-top:20px}.create{border-radius:8px;padding:8px 16px;background:#a855f726;color:#f3e8ff;border:1px solid #c084fc26;font-size:13px;font-weight:600}.create:hover{background:#a855f740}
  button:disabled{opacity:.4}p{color:#fca5a5;font-size:13px;margin-top:12px}button:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
</style>

<script lang="ts">
 import {createEventDispatcher,onMount,tick} from 'svelte';
 import type {Playlist} from './api';
 export let playlist:Playlist;
 export let x:number;export let y:number;export let anchor:HTMLElement;
 const dispatch=createEventDispatcher<{close:void;rename:Playlist;delete:Playlist}>();
 let menu:HTMLDivElement,left=x,top=y;
 onMount(async()=>{await tick();const bounds=menu.getBoundingClientRect();left=Math.max(8,Math.min(x,innerWidth-bounds.width-8));top=Math.max(8,Math.min(y,innerHeight-bounds.height-8));menu.querySelector<HTMLButtonElement>('button')?.focus();});
 function close(restore=false){dispatch('close');if(restore)queueMicrotask(()=>anchor?.isConnected&&anchor.focus({preventScroll:true}));}
 function keys(event:KeyboardEvent){
  if(event.key==='Escape'||event.key==='Tab'){event.preventDefault();event.stopPropagation();close(true);return;}
  if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){
   event.preventDefault();event.stopPropagation();const buttons=[...menu.querySelectorAll<HTMLButtonElement>('button')],index=buttons.findIndex(n=>n===document.activeElement);
   buttons[event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+(event.key==='ArrowUp'?-1:1)+buttons.length)%buttons.length].focus();
  }
 }
 function choose(action:'rename'|'delete'){dispatch(action,playlist);close();}
</script>
<svelte:window on:click={()=>close()} on:contextmenu={()=>close()} on:resize={()=>close(true)} on:scroll|capture={()=>close(true)}/>
<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<div bind:this={menu} class="playlist-context-menu" role="menu" aria-label={'Playlist actions for '+playlist.title} tabindex="-1" style={`left:${left}px;top:${top}px`} on:click|stopPropagation on:contextmenu|stopPropagation|preventDefault on:keydown={keys}>
 <div class="menu-title" title={playlist.title}>{playlist.title}</div>
 <button type="button" role="menuitem" on:click={()=>choose('rename')}>Rename playlist</button>
 <button type="button" role="menuitem" on:click={()=>choose('delete')}>Delete playlist</button>
</div>
<style>
 .playlist-context-menu{position:fixed;z-index:140;width:208px;max-width:calc(100vw - 16px);border-radius:8px;border:1px solid #2a2a3a;background:#1e1e2e;padding:4px 0;box-shadow:0 16px 32px #0007}.menu-title{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:6px 12px;font-size:10px;font-weight:600;color:#777;text-transform:uppercase;letter-spacing:.05em}button{display:block;width:100%;padding:6px 12px;text-align:left;font-size:14px;color:#e5e7eb}button:hover,button:focus{background:#2a2a3a;outline:none}button:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
</style>

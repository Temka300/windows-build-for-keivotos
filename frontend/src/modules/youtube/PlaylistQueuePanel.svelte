<script lang="ts">
  import {createEventDispatcher,tick} from 'svelte';
  import type {Download,Playlist,PlaylistItem} from './api';
  import {filesApi} from '../../lib/filesApi';
  export let open=false;
  export let playlist:Playlist;
  export let current:Download;
  export let items:PlaylistItem[]=[];
  export let total=0;
  export let loading=false;
  export let error='';
  export let navigating=false;
  const dispatch=createEventDispatcher<{close:void;choose:PlaylistItem;more:void;refresh:void}>();
  let panel:HTMLElement,closeButton:HTMLButtonElement;
  $: currentMember=items.find(item=>item.job_id===current.id);
  $: if(open)void activate();
  async function activate(){await tick();if(open)closeButton?.focus({preventScroll:true});}
  function keys(event:KeyboardEvent){
    if(event.key==='Escape'){
      if(document.fullscreenElement)return;
      event.preventDefault();event.stopPropagation();dispatch('close');return;
    }
    if(event.key==='Tab'){
      const controls=[...panel.querySelectorAll<HTMLElement>('button:not(:disabled)')],first=controls[0],last=controls.at(-1);
      if(event.shiftKey&&(document.activeElement===first||document.activeElement===panel)){event.preventDefault();last?.focus();}
      else if(!event.shiftKey&&(document.activeElement===last||document.activeElement===panel)){event.preventDefault();first?.focus();}
    }
    event.stopPropagation();
  }
</script>
<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<aside bind:this={panel} class="playlist-queue-panel" class:open inert={!open} aria-hidden={!open} aria-label="Playlist videos" tabindex="-1" on:keydown={keys}>
  <header><h2 title={playlist.title}>{playlist.title}</h2><button bind:this={closeButton} type="button" aria-label="Close playlist videos" on:click={()=>dispatch('close')}>×</button></header>
  <div class="current-video" aria-current="true">
    {#if current.thumbnail&&currentMember?.available}<img alt="" src={filesApi.thumbnailUrl(current.source_id,current.thumbnail,600,current.id)}/>{/if}
    <p class="playing-label">Currently playing</p><h3>{current.title||'YouTube video'}</h3>
    {#if currentMember&&!currentMember.available}<p class="unavailable">{currentMember.unavailable_reason}</p>{/if}
    {#if !loading&&items.length>=total&&!currentMember}<p class="unavailable">This video is no longer in this playlist.</p>{/if}
  </div>
  {#if error}<p class="error" role="alert">{error}</p>{/if}
  {#if loading}<p role="status">Loading playlist videos…</p>{/if}
  <div class="queue-members" aria-busy={loading}>
    {#each items as item(item.job_id)}
      <button class="queue-member" class:playing={item.job_id===current.id} aria-current={item.job_id===current.id?'true':undefined} type="button" data-playlist-job-id={item.job_id} disabled={!item.available||navigating||loading} title={item.unavailable_reason||item.job?.title||'Unavailable video'} on:click={()=>dispatch('choose',item)}>
        <span class="queue-thumbnail">{#if item.available&&item.job?.thumbnail}<img alt="" loading="lazy" src={filesApi.thumbnailUrl(item.job.source_id,item.job.thumbnail,300,item.job.id)}/>{/if}</span>
        <span class="queue-copy"><span class="queue-title">{item.job?.title||'Unavailable video'}</span>{#if !item.available}<span class="unavailable">{item.unavailable_reason}</span>{/if}</span>
      </button>
    {/each}
  </div>
  {#if !loading&&!items.length&&!error}<p>No videos in this playlist.</p>{/if}
  {#if items.length<total}<button type="button" class="more" disabled={loading||navigating} on:click={()=>dispatch('more')}>Load more videos</button>{/if}
  <footer><button type="button" disabled={loading||navigating} on:click={()=>dispatch('refresh')}>{error?'Retry playlist':'Refresh playlist'}</button></footer>
</aside>
<style>
  .playlist-queue-panel{position:absolute;z-index:3;right:16px;top:64px;bottom:116px;width:320px;max-width:calc(100% - 32px);padding:16px;display:flex;flex-direction:column;gap:12px;border:1px solid #363641;border-radius:12px;background:#15151ff5;color:#e5e7eb;box-shadow:0 16px 40px #0008;overflow:auto;transform:translateX(calc(100% + 32px));visibility:hidden;pointer-events:none;transition:transform .28s cubic-bezier(.4,0,.2,1),visibility 0s .28s}
  .playlist-queue-panel.open{transform:translateX(0);visibility:visible;pointer-events:auto;transition-delay:0s}
  header{display:flex;align-items:center;justify-content:space-between;gap:12px;flex:none}h2{min-width:0;overflow-wrap:anywhere;font-size:14px;font-weight:600}header button{width:28px;height:28px;flex:none;border-radius:7px;font-size:22px;color:#bbb}
  .current-video{flex:none;padding-bottom:12px;border-bottom:1px solid #30303d}.current-video img{width:100%;max-height:150px;aspect-ratio:16/9;object-fit:cover;border-radius:7px;margin-bottom:8px}.playing-label{font-size:11px;color:var(--accent);margin-bottom:4px}h3{font-size:13px;overflow-wrap:anywhere}
  .queue-members{display:flex;flex-direction:column;gap:8px;flex:1;min-height:72px;overflow:auto}.queue-member{display:flex;align-items:center;gap:10px;padding:7px;border:1px solid #30303d;border-radius:8px;text-align:left;flex:none;background:#1b1b26}.queue-member:hover:not(:disabled){background:#ffffff12;border-color:#c084fc55}
  .queue-thumbnail{width:76px;aspect-ratio:16/9;flex:none;background:#272733;border-radius:5px;overflow:hidden}.queue-thumbnail img{width:100%;height:100%;object-fit:cover}.queue-copy{display:flex;flex-direction:column;gap:4px;min-width:0}.queue-title{font-size:12px;overflow-wrap:anywhere}.unavailable{font-size:11px;color:#aaa}
  .queue-member.playing,.queue-member.playing:hover:not(:disabled){border-color:var(--accent);background:color-mix(in srgb,var(--accent) 15%,#1b1b26);box-shadow:inset 3px 0 var(--accent)}.queue-member.playing .queue-title{color:var(--accent-light)}
  p{font-size:12px;color:#aaa}.error{color:#fca5a5}.more,footer button{padding:7px 12px;border-radius:7px;border:1px solid #363641;font-size:12px;flex:none}footer{flex:none;text-align:right}button:disabled{opacity:.5}button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  @media(max-width:700px){.playlist-queue-panel{top:72px;bottom:176px}.current-video img{max-height:100px}}
  :global(html[data-motion='reduced']) .playlist-queue-panel{transition:none}
  @media(prefers-reduced-motion:reduce){.playlist-queue-panel{transition:none}}
</style>

<script lang="ts">
  import {createEventDispatcher} from 'svelte';
  import {ostApi,musicTitle,musicDuration,type MusicTrack} from './api';
  export let item:MusicTrack;
  export let active=false;
  export let number:number|null=null;
  export let covers:{path:string;version:string}[]=[];
  const dispatch=createEventDispatcher<{choose:{item:MusicTrack;event:MouseEvent};location:{item:MusicTrack;event:MouseEvent|KeyboardEvent}}>();
  let artIndex=0,version='';
  $: candidates=[{path:item.path,version:item.cover_version},...covers.filter(cover=>cover.path!==item.path)];
  $: nextVersion=JSON.stringify([item.source_id,candidates]);
  $: if(version!==nextVersion){version=nextVersion;artIndex=0;}
  $: cover=candidates[artIndex];
  $: title=musicTitle(item);
  function location(event:MouseEvent|KeyboardEvent){
    if(event instanceof KeyboardEvent&&event.key!=='ContextMenu'&&!(event.shiftKey&&event.key==='F10'))return;
    dispatch('location',{item,event});
  }
</script>
<button class="entry" class:selected={active} class:numbered={number!==null} data-ost-source={item.source_id} data-ost-entry-path={item.path} data-ost-focus={JSON.stringify([item.source_id,item.path])}
  aria-label={`Play track ${title}`} aria-disabled={!item.available} aria-pressed={active} title={title}
  on:click={event=>{if(item.available)dispatch('choose',{item,event});}} on:contextmenu={location} on:keydown={location}>
  {#if number!==null}<span class="number" aria-label={`Track ${number}`}>{number}</span>{/if}
  <span class="art"><span aria-hidden="true">♫</span>{#if item.available&&cover}{#key `${nextVersion}:${artIndex}`}<img alt="" loading="lazy" decoding="async" width="52" height="52" src={cover?ostApi.coverUrl(item.source_id,cover.path,cover.version):undefined} on:error={()=>artIndex++}/>{/key}{/if}</span>
  <span class="details"><span class="name">{title}</span>{#if !item.available}<small>File unavailable</small>{/if}</span>
  <span class="format">{item.ext.toUpperCase()}</span><span class="duration">{musicDuration(item.music?.duration??item.audio_metadata?.duration)}</span>
</button>
<style>
  .entry{display:grid;grid-template-columns:52px minmax(0,1fr) auto 48px;align-items:center;gap:14px;width:100%;min-width:0;min-height:76px;padding:12px 8px;text-align:left;border-radius:10px;transition:background-color .15s ease}
  .entry.numbered{grid-template-columns:24px 52px minmax(0,1fr) auto 48px}.entry:hover,.entry:focus-visible{background:#1c1c29}.entry.selected{background:#202033}.entry[aria-disabled='true']{opacity:.55;cursor:not-allowed}
  .art{position:relative;display:grid;place-items:center;width:52px;height:52px;overflow:hidden;border-radius:10px;background:#20202d;color:#858599;font-size:25px}.art img{position:absolute;width:100%;height:100%;object-fit:cover}
  .details{min-width:0}.name{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;line-clamp:2;overflow:hidden;overflow-wrap:anywhere;font-size:14px;line-height:1.45}
  .format{font-size:10px;color:#b6b6c6;border:1px solid #393946;border-radius:5px;padding:2px 5px}.duration{justify-self:end;color:#a3a3b5;font-size:12px;font-variant-numeric:tabular-nums;white-space:nowrap}.number{font-size:12px;color:#858599;text-align:right;font-variant-numeric:tabular-nums}small{display:block;margin-top:4px;color:#858599;font-size:11px}
  button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  @media(max-width:480px){.entry{gap:8px;grid-template-columns:40px minmax(0,1fr) auto 40px}.entry.numbered{grid-template-columns:18px 40px minmax(0,1fr) auto 40px}.art{width:40px;height:40px}.format{padding:2px 3px;font-size:9px}}
  :global(html[data-motion='reduced']) .entry{transition:none}@media(prefers-reduced-motion:reduce){.entry{transition:none}}
</style>

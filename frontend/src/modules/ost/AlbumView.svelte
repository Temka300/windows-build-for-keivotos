<script lang="ts">
  import {createEventDispatcher} from 'svelte';
  import {ostApi,type MusicAlbum,type MusicTrack} from './api';
  import MusicTrackRow from './MusicTrackRow.svelte';
  export let album:MusicAlbum;
  export let items:MusicTrack[];
  export let currentIdentity='';
  export let loading=false;
  export let error='';
  export let total=0;
  const dispatch=createEventDispatcher<{back:void;more:void;retry:void;choose:{item:MusicTrack;event:MouseEvent};location:{item:MusicTrack;event:MouseEvent|KeyboardEvent}}>();
  let coverIndex=0,coverKey='';
  $: nextCoverKey=JSON.stringify([album.id,album.covers]);
  $: if(coverKey!==nextCoverKey){coverKey=nextCoverKey;coverIndex=0;}
  $: cover=album.covers[coverIndex];
</script>
<section class="album-view" aria-label={`Album ${album.name}`}>
  <button class="back" aria-label="Back to Albums" title="Back to Albums" on:click={()=>dispatch('back')}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg></button>
  <div class="album-heading">
    <div class="cover"><span aria-hidden="true">♫</span>{#if cover}{#key `${nextCoverKey}:${coverIndex}`}<img alt="" src={cover?ostApi.coverUrl(album.source_id,cover.path,cover.version,600):undefined} on:error={()=>coverIndex++}/>{/key}{/if}</div>
    <div class="album-name"><h2>{album.name}</h2>{#if album.artists.length}<p>{album.artists.join(' / ')}</p>{/if}<p>{total} tracks</p></div>
  </div>
  {#if error}<p role="alert">{error} <button on:click={()=>dispatch('retry')}>Retry album</button></p>{/if}
  {#if loading&&!items.length}<p role="status">Reading album…</p>{/if}
  <ol class="tracks" aria-label="Album tracks">
    {#each items as item,i (`${item.source_id}:${item.path}`)}
      {#if item.disc_label&&(i===0||item.disc_label!==items[i-1].disc_label)}<li class="divider" role="presentation"><h3>{item.disc_label}</h3></li>{/if}
      <li><MusicTrackRow {item} covers={album.covers} number={item.music.track??i+1} active={currentIdentity===JSON.stringify([item.source_id,item.path])} on:choose on:location/></li>
    {/each}
  </ol>
  {#if items.length<total}<button class="more" disabled={loading} on:click={()=>dispatch('more')}>{loading?'Loading…':'Load more'}</button>{/if}
  {#if !loading&&!error&&!items.length}<p>No supported tracks in this album.</p>{/if}
</section>
<style>
  .album-view{max-width:1000px}.back{display:grid;place-items:center;width:40px;height:40px;border-radius:999px;transition:width .18s ease,background-color .18s ease;margin-bottom:18px}.back:hover,.back:focus-visible{width:56px;background:#33333b}.back svg{width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
  .album-heading{display:flex;align-items:center;gap:24px;margin-bottom:24px;min-width:0}.cover{position:relative;display:grid;place-items:center;flex:none;width:180px;aspect-ratio:1;border-radius:14px;overflow:hidden;background:#20202d;color:#858599;font-size:50px}.cover img{position:absolute;width:100%;height:100%;object-fit:cover}.album-name{min-width:0}h2{font-size:25px;font-weight:600;overflow-wrap:anywhere;line-height:1.25}.album-name p{color:#858599;font-size:13px;margin-top:9px;overflow-wrap:anywhere}.tracks{list-style:none;margin:0;padding:0}.tracks>li:not(.divider){border-bottom:1px solid #242431}.divider{padding:22px 8px 8px;color:#b6b6c6}.divider h3{font-size:13px;font-weight:600;overflow-wrap:anywhere}.more{margin-top:16px;padding:8px 14px;border:1px solid #393946;border-radius:10px;font-size:13px}p[role]{font-size:13px;color:#b6b6c6}p button{text-decoration:underline}button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  @media(max-width:480px){.album-heading{gap:16px}.cover{width:112px}h2{font-size:20px}.album-name p{font-size:12px}}
  :global(html[data-motion='reduced']) .back{transition:none}@media(prefers-reduced-motion:reduce){.back{transition:none}}
</style>

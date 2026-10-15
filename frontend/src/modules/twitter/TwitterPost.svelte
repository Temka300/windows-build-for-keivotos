<script lang="ts">
  import {onDestroy} from 'svelte';
  import {twitterApi,captureKey,avatarUnavailable,type Capture} from './api';
  export let item:Capture;
  export let detail=false;
  export let onOpen:(item:Capture,event:MouseEvent)=>void;
  export let onAuthor:(item:Capture,event:MouseEvent)=>void;
  let video:HTMLVideoElement|undefined,mediaError=false,avatarError=false;
  let photoErrors=new Set<number>();
  $: date=item.posted_at??item.captured_at;
  $: label=new Date(date).toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'});
  onDestroy(()=>{if(video){video.pause();video.removeAttribute('src');video.load();}});
</script>
<article class:detail data-twitter-post={captureKey(item)}>
  <button class="avatar" aria-label={`Posts by ${item.author.name}`} on:click={event=>onAuthor(item,event)}>
    <span aria-hidden="true">{item.author.name.slice(0,1)}</span>
    {#if !avatarError&&!avatarUnavailable(item)}<img alt="" loading="lazy" src={twitterApi.mediaUrl(item,'avatar')} on:error={()=>avatarError=true}/>{/if}
  </button>
  <div class="content">
    <div class="byline"><button class="author" on:click={event=>onAuthor(item,event)}><strong>{item.author.name}</strong><span>@{item.author.handle}</span></button><span aria-hidden="true">·</span><button class="date" aria-label={`Open saved post ${item.post_id}`} title={`Captured ${new Date(item.captured_at).toLocaleString()}`} on:click={event=>onOpen(item,event)}><time datetime={date}>{label}</time></button></div>
    {#if detail}<p class="text">{item.raw_text}</p>{:else}<button class="text open-post" aria-label={`Read saved post ${item.post_id}`} on:click={event=>onOpen(item,event)}>{item.raw_text}</button>{/if}
    {#if item.photos?.length}
      <div class="photos" class:single={item.photos.length===1} class:three={item.photos.length===3}>
        {#each item.photos as photo (photo.index)}
          {#if item.unavailable_assets.includes(photo.name)||photoErrors.has(photo.index)}
            <p class="unavailable" role="status">Saved photo {photo.index} is unavailable.</p>
          {:else}
            <a class="photo" href={twitterApi.mediaUrl(item,'photo',photo.index)} target="_blank" rel="noreferrer" aria-label={`Open photo ${photo.index} of ${item.photos.length} from saved post ${item.post_id}`}>
              <img alt={`Photo ${photo.index} of ${item.photos.length} from @${item.author.handle}`} loading="lazy" src={twitterApi.mediaUrl(item,'photo',photo.index)} on:error={()=>photoErrors=new Set([...photoErrors,photo.index])}/>
            </a>
          {/if}
        {/each}
      </div>
    {:else if item.video_path&&!item.unavailable_assets.includes('video.mp4')&&!mediaError}
      <!-- svelte-ignore a11y_media_has_caption -->
      <video bind:this={video} controls playsinline preload="none" src={twitterApi.mediaUrl(item,'video')} on:error={()=>mediaError=true}></video>
    {:else if item.video_path}<p class="unavailable" role="status">Saved video is unavailable.</p>{/if}
    {#if detail}<div class="capture-facts"><span>Captured {new Date(item.captured_at).toLocaleString()}</span><span>{item.source_name}</span><a href={item.source_url} target="_blank" rel="noreferrer">Original post ↗</a></div>{/if}
  </div>
</article>
<style>
  article{display:flex;align-items:flex-start;gap:12px;padding:18px 20px;border-bottom:1px solid #ffffff14;min-width:0}article:not(.detail):hover{background:#ffffff03}.avatar{position:relative;display:grid;place-items:center;flex:none;width:44px;height:44px;border-radius:50%;background:#262631;overflow:hidden;font-size:20px}.avatar img{position:absolute;width:100%;height:100%;object-fit:cover}.content{flex:1;min-width:0}.byline{display:flex;align-items:baseline;flex-wrap:wrap;gap:6px;font-size:14px;color:#90909f}.author{display:flex;align-items:baseline;flex-wrap:wrap;gap:6px;text-align:left;min-width:0}.author strong{color:#eeeef3;font-size:15px;overflow-wrap:anywhere}.author:hover strong{text-decoration:underline}.author span{overflow-wrap:anywhere}.date{font-size:13px}.date:hover{text-decoration:underline}.text{display:block;white-space:pre-wrap;overflow-wrap:anywhere;text-align:left;font-size:15px;line-height:1.55;margin:5px 0 13px;width:100%}.open-post{cursor:pointer}video{display:block;width:100%;max-height:640px;aspect-ratio:16/9;object-fit:contain;background:#08080b;border:1px solid #ffffff18;border-radius:16px}.unavailable{margin:12px 0;border:1px solid #30303c;border-radius:16px;padding:24px;color:#aaaab9;font-size:13px}.capture-facts{display:flex;flex-wrap:wrap;gap:9px 16px;border-top:1px solid #ffffff14;padding-top:14px;margin-top:16px;font-size:12px;color:#90909f}.capture-facts a{color:var(--accent)}button:focus-visible,a:focus-visible{outline:2px solid var(--accent);outline-offset:3px;border-radius:4px}@media(max-width:480px){article{padding:14px 12px;gap:9px}.avatar{width:36px;height:36px}.byline{font-size:12px}.text{font-size:14px}}
  .photos{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:2px;border:1px solid #ffffff18;border-radius:16px;overflow:hidden;background:#08080b}.photos.single{grid-template-columns:1fr}.photo{display:block;min-width:0;min-height:0;overflow:hidden}.photo img{display:block;width:100%;height:100%;max-height:420px;aspect-ratio:1;object-fit:cover}.photos.single img{aspect-ratio:auto;height:auto;object-fit:contain}.photos.three .photo:first-child{grid-row:span 2}.photos.three .photo:first-child img{aspect-ratio:auto;height:100%}.photos .unavailable{margin:0;border:0;border-radius:0}.detail .photos{display:flex;flex-direction:column}.detail .photo img{height:auto;max-height:none;aspect-ratio:auto;object-fit:contain}.photo:focus-visible{outline-offset:-3px}
</style>

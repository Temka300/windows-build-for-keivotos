<script lang="ts">
  import {twitterApi,avatarUnavailable,type Capture} from './api';
  export let items:Capture[]=[];
  export let onOpen:(item:Capture,event:MouseEvent)=>void;
  $: authors=[...new Map([...items].reverse().map(item=>[item.author_key,item])).values()].sort((a,b)=>b.capture_id.localeCompare(a.capture_id));
</script>
<div class="authors" aria-label="Saved authors">
  {#each authors as item (item.author_key)}
    <button class="author" data-twitter-author={item.author_key} on:click={event=>onOpen(item,event)}>
      <span class="avatar">{item.author.name.slice(0,1)}{#if !avatarUnavailable(item)}<img alt="" loading="lazy" src={twitterApi.mediaUrl(item,'avatar')}/>{/if}</span>
      <span class="name"><strong>{item.author.name}</strong><span>@{item.author.handle}</span></span>
      <span aria-hidden="true">›</span>
    </button>
  {/each}
</div>
<style>
  .author{width:100%;display:flex;align-items:center;gap:12px;text-align:left;padding:18px 20px;border-bottom:1px solid #ffffff14}.author:hover{background:#ffffff04}.avatar{position:relative;display:grid;place-items:center;width:46px;height:46px;flex:none;border-radius:50%;overflow:hidden;background:#262631;font-size:20px}.avatar img{position:absolute;width:100%;height:100%;object-fit:cover}.name{display:flex;flex:1;min-width:0;flex-direction:column;gap:4px}.name strong{overflow-wrap:anywhere;font-size:15px}.name>span{color:#90909f;font-size:14px;overflow-wrap:anywhere}.author:focus-visible{outline:2px solid var(--accent);outline-offset:-3px}
</style>

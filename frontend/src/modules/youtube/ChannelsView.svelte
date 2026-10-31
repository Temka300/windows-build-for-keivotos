<script lang="ts">
 import {createEventDispatcher} from 'svelte';
 import type {Channel} from './api';
 export let items:Channel[]=[],total=0,loading=false,pending=false,error='',query='';
 let unavailable=new Set<string>();
 const dispatch=createEventDispatcher<{open:{channel:Channel;anchor:HTMLElement};more:void;retry:void}>();
</script>
<div class="channels-view" aria-busy={loading}>
 {#if error}<p role="alert">{error} <button class="retry" on:click={()=>dispatch('retry')}>Retry</button></p>{/if}
 {#if !items.length&&!loading&&!error}<p class="empty">{query?'No channels match your search.':'No channels yet.'}</p>{/if}
 <div class="channels-grid">
  {#each items as channel(channel.id)}
   <button class="channel-card" data-channel-id={channel.id} aria-label={'Open channel '+channel.name} title={channel.name} on:click={event=>dispatch('open',{channel,anchor:event.currentTarget})}>
    <span class="logo">
     {#if channel.avatar_id&&!unavailable.has(channel.avatar_id)}{#key channel.avatar_id}<img src={'/api/youtube/channel-art/'+encodeURIComponent(channel.avatar_id)} loading="lazy" alt="" on:error={()=>unavailable=new Set(unavailable).add(channel.avatar_id!)}/>{/key}{/if}
     <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4 22v-3a8 8 0 0 1 16 0v3"/></svg>
    </span><span class="name">{channel.name}</span>
   </button>
  {/each}
 </div>
 {#if items.length<total}<button class="more" disabled={loading||pending} on:click={()=>dispatch('more')}>Load more channels</button>{/if}
</div>
<style>
 .channels-view{min-width:0}.channels-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:24px 16px}.channel-card{display:flex;flex-direction:column;align-items:center;gap:14px;min-width:0;padding:20px 12px;border-radius:12px}.channel-card:hover{background:#ffffff06}.channel-card:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}.logo{position:relative;display:grid;place-items:center;width:96px;height:96px;border-radius:50%;overflow:hidden;background:#292934;color:#777}.logo img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}.logo img~svg{visibility:hidden}.logo svg{width:44px;height:44px}.name{width:100%;min-height:2.7em;line-height:1.35;font-size:14px;text-align:center;overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:2;line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.empty{color:#888;padding:32px 0}.more{margin-top:20px;border:1px solid #303040;border-radius:8px;padding:8px 16px}.more:disabled{opacity:.5}.retry{text-decoration:underline}p[role='alert']{color:#e8b6bb;margin-bottom:16px}
 @media(max-width:400px){.channels-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px 4px}.channel-card{padding:16px 6px}.logo{width:80px;height:80px}}
</style>

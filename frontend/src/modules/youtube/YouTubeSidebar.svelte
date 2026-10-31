<script lang="ts">
  import {createEventDispatcher} from 'svelte';
  export let section:'videos'|'playlists';
  const dispatch=createEventDispatcher<{select:'videos'|'playlists'}>();
</script>
<nav class="youtube-sidebar" aria-label="YouTube sections">
  <button class:active={section==='videos'} aria-current={section==='videos'?'page':undefined} on:click={()=>dispatch('select','videos')}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4 22v-3a8 8 0 0 1 16 0v3"/></svg>Channels</button>
  <button class:active={section==='playlists'} aria-current={section==='playlists'?'page':undefined} on:click={()=>dispatch('select','playlists')}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M3 6h13M3 11h13M3 16h8m5-3 5 3-5 3Z"/></svg>Playlists</button>
</nav>
<style>
  .youtube-sidebar{height:100%;overflow-y:auto;padding:20px 12px;border-right:1px solid #ffffff0d}
  button{display:flex;align-items:center;gap:12px;width:100%;padding:11px 12px;border-radius:9px;color:#9ca3af;text-align:left;font-size:14px}
  button+button{margin-top:5px}button:hover{background:#ffffff08;color:#e5e7eb}.active{background:#a855f712;color:#e9d5ff}.active:hover{background:#a855f71c}svg{width:20px;height:20px;flex-shrink:0}
  button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
</style>

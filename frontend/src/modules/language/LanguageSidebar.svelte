<script lang="ts">
  import {createEventDispatcher} from 'svelte';
  export let section:'markdown'|'practice'|'vocabulary';
  export let busy=false;
  const dispatch=createEventDispatcher<{select:'markdown'|'practice'|'vocabulary'}>();
</script>
<nav class="language-sidebar" aria-label="Language sections">
  <button class:active={section==='markdown'} aria-current={section==='markdown'?'page':undefined} disabled={busy} on:click={()=>dispatch('select','markdown')}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Zm0 0v6h6M8 13h8m-8 4h6"/></svg>Markdown</button>
  <button class:active={section==='practice'} aria-current={section==='practice'?'page':undefined} disabled={busy} on:click={()=>dispatch('select','practice')}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 4H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2h-4M9 2h6v4H9Zm-2 9 2 2 3-3m3 2h3M7 17h11"/></svg>Practice</button>
  <button class:active={section==='vocabulary'} aria-current={section==='vocabulary'?'page':undefined} disabled={busy} on:click={()=>dispatch('select','vocabulary')}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5c-3-2-6-2-9-1v15c3-1 6-1 9 1m0-15c3-2 6-2 9-1v15c-3-1-6-1-9 1Zm-6 3h3m-3 4h3m6-4h3m-3 4h3"/></svg>Vocabulary</button>
</nav>
<style>
  .language-sidebar{height:100%;overflow-y:auto;padding:20px 12px;border-right:1px solid #ffffff0d}
  button{display:flex;align-items:center;gap:12px;width:100%;padding:11px 12px;border-radius:9px;color:#9ca3af;text-align:left;font-size:14px}
  button+button{margin-top:5px}button:hover{background:#ffffff08;color:#e5e7eb}.active{background:color-mix(in srgb,var(--accent) 8%,transparent);color:var(--accent-light)}.active:hover{background:color-mix(in srgb,var(--accent) 12%,transparent)}button:disabled{cursor:default}
  svg{width:20px;height:20px;flex-shrink:0;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
  button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
</style>

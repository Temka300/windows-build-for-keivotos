<script lang="ts">
  import {createEventDispatcher} from 'svelte';
  export let section:'folders'|'library';
  const dispatch=createEventDispatcher<{select:'folders'|'library'}>();
</script>
<nav class="ost-sidebar" aria-label="OST sections">
  <button class:active={section==='folders'} aria-current={section==='folders'?'page':undefined} on:click={()=>dispatch('select','folders')}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/></svg>Folders</button>
  <button class:active={section==='library'} aria-current={section==='library'?'page':undefined} on:click={()=>dispatch('select','library')}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4v16M8 4v16M12 4v16m4-15 4 14"/></svg>Library</button>
</nav>
<style>
  .ost-sidebar{height:100%;overflow-y:auto;padding:20px 12px;border-right:1px solid #ffffff0d}
  button{display:flex;align-items:center;gap:12px;width:100%;padding:11px 12px;border-radius:9px;color:#9ca3af;text-align:left;font-size:14px}
  button+button{margin-top:5px}button:hover{background:#ffffff08;color:#e5e7eb}.active{background:color-mix(in srgb,var(--accent) 12%,transparent);color:var(--accent)}.active:hover{background:color-mix(in srgb,var(--accent) 18%,transparent)}svg{width:20px;height:20px;flex-shrink:0;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
  button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
</style>

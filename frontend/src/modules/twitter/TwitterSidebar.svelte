<script lang="ts">
  export let section:'posts'|'authors';
  export let onSelect:(section:'posts'|'authors')=>void;
</script>
<nav class="twitter-sidebar" aria-label="Twitter sections">
  <button class:active={section==='posts'} aria-current={section==='posts'?'page':undefined} on:click={()=>onSelect('posts')}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 3h14v18l-7-4-7 4Z"/></svg>Saved posts</button>
  <button class:active={section==='authors'} aria-current={section==='authors'?'page':undefined} on:click={()=>onSelect('authors')}><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/></svg>Authors</button>
</nav>
<style>
  .twitter-sidebar{height:100%;overflow-y:auto;padding:22px 12px;border-right:1px solid #ffffff0d}
  button{display:flex;align-items:center;gap:16px;width:100%;padding:13px 16px;border-radius:999px;color:#d8d8e1;text-align:left;font-size:17px}
  button+button{margin-top:8px}button:hover{background:#ffffff08}.active{color:var(--accent);font-weight:650;background:color-mix(in srgb,var(--accent) 10%,transparent)}
  svg{width:23px;height:23px;flex:none;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
  button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
</style>

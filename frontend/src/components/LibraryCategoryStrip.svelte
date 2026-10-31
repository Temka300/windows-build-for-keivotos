<script lang="ts">
  import {createEventDispatcher,onMount,onDestroy,tick} from 'svelte';
  import {allLibraryCategories,moduleUi,libraryCategoriesRefresh,type LibraryCategory} from '../modules/registry';
  import {readStoredValue} from '../lib/persistedStore';
  import {settingsOpen} from '../lib/suiteStores';
  export let owner:string;
  export let presentation:'plain'|'underline'='plain';
  const dispatch=createEventDispatcher<{change:{category:string}}> ();
  let categories:LibraryCategory[]=[],selected='',ready=false,error='',alive=true,request:AbortController|null=null;
  let strip:HTMLElement;
  export function focusSelected(){strip?.querySelector<HTMLButtonElement>('button[aria-pressed="true"]')?.focus({preventScroll:true});}
  export async function refresh(force=false){
    request?.abort();const current=new AbortController();request=current;error='';
    const client=moduleUi(owner).categories;if(!client)return;
    try{
      const values=await allLibraryCategories(client,current.signal);
      if(!alive||current.signal.aborted)return;
      const stored=readStoredValue('keivotos:'+owner+'-category');
      const choice=ready?selected:typeof stored==='string'?stored:'';
      const next=values.length?(choice==='default'||values.some(item=>item.id===choice)?choice:'default'):'';
      const changed=!ready||next!==selected||JSON.stringify(categories.map(item=>[item.id,item.name]))!==JSON.stringify(values.map(item=>[item.id,item.name]));
      categories=values;ready=true;
      if(force||changed)select(next);
    }catch(e){if(alive&&!current.signal.aborted)error=e instanceof Error?e.message:'Could not load categories.';}
  }
  function select(identity:string){
    selected=identity;
    try{localStorage.setItem('keivotos:'+owner+'-category',JSON.stringify(identity));}catch{/* Keep filtering usable when browser storage is unavailable. */}
    dispatch('change',{category:identity});
  }
  async function keys(event:KeyboardEvent){
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
    event.preventDefault();
    const choices=['default',...categories.map(item=>item.id)],index=choices.indexOf(selected);
    const next=event.key==='Home'?0:event.key==='End'?choices.length-1:(index+(event.key==='ArrowRight'?1:-1)+choices.length)%choices.length;
    select(choices[next]);await tick();
    const button=strip.querySelector<HTMLButtonElement>('button[aria-pressed="true"]');
    button?.focus({preventScroll:true});button?.scrollIntoView({block:'nearest',inline:'nearest'});
  }
  onMount(()=>{
    const stop=libraryCategoriesRefresh.subscribe(()=>void refresh(true));
    const focus=()=>{if(!document.hidden&&!$settingsOpen)void refresh();};window.addEventListener('focus',focus);
    return()=>{stop();window.removeEventListener('focus',focus);};
  });
  onDestroy(()=>{alive=false;request?.abort();});
</script>

{#if categories.length}
  <nav class="library-categories" class:manga-underline={presentation==='underline'} aria-label={`${owner==='video'?'Video':'Manga'} categories`} bind:this={strip}>
    {#each [{id:'default',name:'Default'},...categories] as item (item.id)}
      <button type="button" aria-pressed={selected===item.id} on:click={()=>select(item.id)} on:keydown={keys}>{item.name}</button>
    {/each}
  </nav>
{/if}
{#if error}<p class="category-error" role="alert">{error} <button type="button" on:click={()=>refresh(true)}>Retry categories</button></p>{/if}

<style>
  .library-categories{display:flex;gap:24px;flex:none;overflow-x:auto;min-width:0;padding:14px 20px 10px;scrollbar-width:thin}
  .library-categories button{flex:none;white-space:nowrap;font-size:14px;background:none;border:0;padding:2px 0;color:#9ca3af}
  .library-categories button[aria-pressed=true]{color:var(--accent)}button:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
  .library-categories.manga-underline{padding:10px 20px 8px}
  .manga-underline button{position:relative;padding:7px 0 10px}
  .manga-underline button:not([aria-pressed=true]):hover{color:inherit}
  .manga-underline button::after{content:'';position:absolute;inset:auto 0 1px;height:2px;border-radius:2px;background:var(--accent);transform:scaleX(0);transition:transform 130ms ease}
  .manga-underline button[aria-pressed=true]::after{transform:scaleX(1)}
  @media(pointer:coarse){.manga-underline button{min-height:44px}}
  @media(prefers-reduced-motion:reduce){.manga-underline button::after{transition:none}}
  .category-error{flex:none;padding:12px 20px;font-size:13px;color:#fca5a5}.category-error button{margin-left:8px;color:var(--accent)}
</style>

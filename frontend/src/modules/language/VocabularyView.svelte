<script lang="ts">
  import {onDestroy,tick} from 'svelte';
  import {languageApi,type VocabularyEntry} from './api';
  import VocabularyEntryDrawer from './VocabularyEntryDrawer.svelte';
  export let active=true;
  let items:VocabularyEntry[]=[],total=0,loading=false,error='',entryOpen=false,wasActive=false,alive=true,failedMore=false;
  let request:AbortController|null=null,addButton:HTMLButtonElement,entryDrawer:VocabularyEntryDrawer;
  $: if(active&&!wasActive){wasActive=true;void load(false,true);}else if(!active&&wasActive){wasActive=false;request?.abort();loading=false;}
  export function hasDraft(){return entryDrawer?.hasDraft()??false;}
  export async function beforeNavigate(){return await entryDrawer?.beforeNavigate()??true;}
  async function load(more=false,refresh=false){
    request?.abort();const current=new AbortController();request=current;loading=true;error='';failedMore=more;
    try{
      let offset=more?items.length:0,count=0,rows:VocabularyEntry[]=[];
      const target=more?offset+50:refresh?Math.max(50,items.length):50;
      do{
        const page=await languageApi.vocabulary(offset,50,current.signal);
        if(!alive||current.signal.aborted||!active)return;
        rows.push(...page.items);count=page.total;offset+=page.items.length;
        if(!page.items.length)break;
      }while(!more&&refresh&&offset<target&&offset<count);
      items=[...new Map((more?[...items,...rows]:rows).map(item=>[item.id,item])).values()];total=count;
    }catch(caught){if(alive&&!current.signal.aborted)error=caught instanceof Error?caught.message:'Could not load vocabulary.';}
    finally{if(current===request)loading=false;}
  }
  async function closeEntry(){entryOpen=false;await tick();if(alive&&active)addButton?.focus();}
  async function saved(entry:VocabularyEntry){
    request?.abort();if(!items.some(item=>item.id===entry.id))total++;
    items=[entry,...items.filter(item=>item.id!==entry.id)];loading=false;error='';
    await closeEntry();if(alive&&active)void load(false,true);
  }
  onDestroy(()=>{alive=false;request?.abort();});
</script>

<section class="vocabulary-view" aria-label="Vocabulary">
  <div class="vocabulary-table-host" inert={entryOpen}>
    <header><h1>Vocabulary</h1><button class="add" type="button" aria-label="New vocabulary entry" title="New vocabulary entry" bind:this={addButton} on:click={()=>entryOpen=true}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg></button></header>
    <div class="vocabulary-table-scroll" aria-busy={loading}>
      <table><caption class="sr-only">Saved vocabulary</caption><colgroup><col class="korean-column"/><col class="pronunciation-column"/><col/></colgroup><thead><tr><th scope="col">Korean word</th><th scope="col">Pronunciation</th><th scope="col">English translation</th></tr></thead><tbody>
        {#each items as entry(entry.id)}<tr data-vocabulary-id={entry.id}><td lang="ko">{entry.korean}</td><td>{entry.pronunciation}</td><td lang="en">{entry.english}</td></tr>{/each}
      </tbody></table>
      {#if !loading&&!error&&!items.length}<p class="empty">No vocabulary yet.</p>{/if}
      {#if error}<div class="list-error"><p role="alert">{error}</p><button type="button" on:click={()=>load(failedMore,!failedMore)}>Retry vocabulary</button></div>{/if}
      <footer>
        {#if loading}<span role="status">Loading vocabulary…</span>{/if}
        {#if items.length<total}<button type="button" disabled={loading} on:click={()=>load(true)}>Load more vocabulary</button>{/if}
        <button type="button" disabled={loading} on:click={()=>load(false,true)}>Refresh vocabulary</button>
      </footer>
    </div>
  </div>
  <VocabularyEntryDrawer bind:this={entryDrawer} open={entryOpen} on:close={closeEntry} on:saved={event=>void saved(event.detail)}/>
</section>
<style>
  .vocabulary-view{position:relative;display:flex;flex:1;min-width:0;min-height:0;overflow:hidden}.vocabulary-table-host{display:flex;flex-direction:column;flex:1;min-width:0;min-height:0;padding:24px}
  header{display:flex;align-items:center;gap:12px;margin-bottom:24px;flex:none}h1{font-size:20px;font-weight:600}.add{display:grid;place-items:center;width:32px;height:32px;border:1px solid #303040;border-radius:8px;background:#1d1d29;color:var(--accent-light)}.add:hover{background:color-mix(in srgb,var(--accent) 10%,transparent);border-color:var(--accent)}.add svg{display:block;width:20px;height:20px}
  .vocabulary-table-scroll{flex:1;min-width:0;min-height:0;overflow:auto;scrollbar-gutter:stable}table{width:100%;table-layout:fixed;border-collapse:collapse;font-size:14px}.korean-column,.pronunciation-column{width:28%}th,td{padding:12px;text-align:left;border-bottom:1px solid #30303d;overflow-wrap:anywhere;vertical-align:top}th{font-size:13px;font-weight:500;color:#aaa;background:#17171f;position:sticky;top:0}td{white-space:pre-wrap;color:#ddd}
  .empty{padding:24px 12px;color:#aaa;font-size:13px}.list-error{padding:16px 12px}.list-error p{color:#fca5a5;font-size:13px;overflow-wrap:anywhere}.list-error button{margin-top:8px;text-decoration:underline;font-size:13px}footer{display:flex;justify-content:flex-end;align-items:center;flex-wrap:wrap;gap:12px;padding:20px 0;font-size:13px;color:#aaa}footer button{padding:8px 12px;border:1px solid #363641;border-radius:8px;color:#ddd}button:disabled{opacity:.45}button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  @media(max-width:500px){.vocabulary-table-host{padding:16px}th,td{padding:9px 6px;font-size:12px}.korean-column,.pronunciation-column{width:30%}}
</style>

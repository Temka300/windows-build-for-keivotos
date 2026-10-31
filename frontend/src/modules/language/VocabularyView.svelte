<script lang="ts">
  import {onDestroy,tick} from 'svelte';
  import {languageApi,type VocabularyEntry,type VocabularyMutation} from './api';
  import {ApiError} from '../../lib/http';
  import VocabularyEntryDrawer from './VocabularyEntryDrawer.svelte';
  export let active=true;
  let items:VocabularyEntry[]=[],total=0,loading=false,error='',entryOpen=false,wasActive=false,alive=true,failedMore=false;
  let request:AbortController|null=null,addButton:HTMLButtonElement,entryDrawer:VocabularyEntryDrawer;
  let editing:VocabularyEntry|null=null,anchor:HTMLElement|null=null;
  let deleting:VocabularyEntry|null=null,deleteRevision:number|null=null,deleteBusy=false,deleteReading=false,deleteError='',deleteConflict=false;
  let deletePayload:VocabularyMutation|null=null,deleteRead:AbortController|null=null,deleteDialog:HTMLDivElement,cancelDelete:HTMLButtonElement;
  $: if(active&&!wasActive){wasActive=true;void load(false,true);}else if(!active&&wasActive){wasActive=false;request?.abort();loading=false;}
  export function hasDraft(){return !!deletePayload||deleteBusy||(entryDrawer?.hasDraft()??false);}
  export async function beforeNavigate(){if(deleting)return false;return await entryDrawer?.beforeNavigate()??true;}
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
  function openEntry(entry:VocabularyEntry|null,button:HTMLElement){editing=entry;anchor=button;entryOpen=true;}
  function returnFocus(){if(alive&&active){if(anchor?.isConnected)anchor.focus();else addButton?.focus();}}
  async function closeEntry(){entryOpen=false;await tick();returnFocus();}
  async function saved(entry:VocabularyEntry){
    request?.abort();if(!items.some(item=>item.id===entry.id))total++;
    items=editing?items.map(item=>item.id===entry.id?entry:item):[entry,...items.filter(item=>item.id!==entry.id)];loading=false;error='';
    await closeEntry();if(alive&&active)void load(false,true);
  }
  async function chooseDelete(entry:VocabularyEntry,button:HTMLElement){
    deleting=entry;anchor=button;deletePayload=null;deleteRevision=null;deleteError='';deleteConflict=false;
    await tick();cancelDelete?.focus();await readDelete();
  }
  async function readDelete(){
    if(!deleting)return;deleteRead?.abort();const current=new AbortController();deleteRead=current;
    const id=deleting.id;deleteReading=true;deleteError='';deleteRevision=null;
    try{const latest=await languageApi.vocabularyEntry(id,current.signal);
      if(!alive||current.signal.aborted||deleting?.id!==id)return;
      if(latest.archived)throw new Error('This entry was already deleted. Cancel and refresh Vocabulary.');
      deleting=latest;deleteRevision=latest.revision;deletePayload=null;deleteConflict=false;
    }catch(caught){if(alive&&!current.signal.aborted)deleteError=caught instanceof Error?caught.message:'Could not load this entry.';}
    finally{if(current===deleteRead)deleteReading=false;}
  }
  async function closeDelete(){if(deleteBusy)return;deleting=null;deleteRead?.abort();deletePayload=null;await tick();returnFocus();if(alive&&active)void load(false,true);}
  async function remove(){
    if(!deleting||deleteBusy||deleteReading||deleteRevision===null||deleteConflict)return;
    const id=deleting.id;deletePayload??={request_id:crypto.randomUUID(),revision:deleteRevision};
    deleteBusy=true;deleteError='';await tick();deleteDialog?.focus();
    try{await languageApi.archiveVocabulary(id,deletePayload);
      if(!alive)return;request?.abort();const index=items.findIndex(row=>row.id===id),next=items[index+1]??items[index-1];
      items=items.filter(row=>row.id!==id);total=Math.max(0,total-1);loading=false;error='';deleting=null;deletePayload=null;
      await tick();if(active){const button=next?document.querySelector<HTMLButtonElement>(`[data-vocabulary-id="${next.id}"] button`):null;if(button)button.focus();else addButton?.focus();void load(false,true);}
    }catch(caught){if(alive){deleteConflict=caught instanceof ApiError&&caught.status===409;deleteError=caught instanceof Error?caught.message:'Could not delete this entry. Retry to confirm it.';}}
    finally{if(alive)deleteBusy=false;}
  }
  function deleteKeys(event:KeyboardEvent){
    if(!deleting)return;
    if(event.key==='Escape'){event.preventDefault();event.stopPropagation();void closeDelete();return;}
    if(event.key!=='Tab')return;
    const controls=[...deleteDialog.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')],first=controls[0],last=controls.at(-1);
    if(!first){event.preventDefault();deleteDialog.focus();}
    else if(event.shiftKey&&(document.activeElement===first||document.activeElement===deleteDialog)){event.preventDefault();last?.focus();}
    else if(!event.shiftKey&&(document.activeElement===last||document.activeElement===deleteDialog)){event.preventDefault();first.focus();}
  }
  onDestroy(()=>{alive=false;request?.abort();deleteRead?.abort();});
</script>

<section class="vocabulary-view" aria-label="Vocabulary">
  <div class="vocabulary-table-host" inert={entryOpen||!!deleting}>
    <header><h1>Vocabulary</h1><button class="add" type="button" aria-label="New vocabulary entry" title="New vocabulary entry" bind:this={addButton} on:click={event=>openEntry(null,event.currentTarget)}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg></button></header>
    <div class="vocabulary-table-scroll" aria-busy={loading}>
      <table><caption class="sr-only">Saved vocabulary</caption><colgroup><col class="korean-column"/><col class="pronunciation-column"/><col/><col class="actions-column"/></colgroup><thead><tr><th scope="col">Korean word</th><th scope="col">Pronunciation</th><th scope="col">English translation</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead><tbody>
        {#each items as entry(entry.id)}<tr data-vocabulary-id={entry.id}><td lang="ko">{entry.korean}</td><td>{entry.pronunciation}</td><td lang="en">{entry.english}</td><td><div class="row-actions"><button type="button" aria-label={'Edit '+entry.korean} on:click={event=>openEntry(entry,event.currentTarget)}>Edit</button><button type="button" aria-label={'Delete '+entry.korean} on:click={event=>void chooseDelete(entry,event.currentTarget)}>Delete</button></div></td></tr>{/each}
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
  <VocabularyEntryDrawer bind:this={entryDrawer} open={entryOpen} entry={editing} on:close={closeEntry} on:saved={event=>void saved(event.detail)}/>
  {#if deleting}
   <div class="delete-overlay">
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <div role="dialog" aria-modal="true" aria-label="Delete vocabulary" class="delete-dialog" tabindex="-1" bind:this={deleteDialog} on:keydown={deleteKeys}>
     <h2>Delete vocabulary</h2><p>Delete <strong>{deleting.korean}</strong> from Vocabulary? Its saved history will be kept.</p>
     {#if deleteReading}<p role="status">Loading saved entry…</p>{/if}
     {#if deleteError}<p role="alert" class="delete-error">{deleteError}</p>{/if}
     <footer><button type="button" bind:this={cancelDelete} disabled={deleteBusy} on:click={()=>void closeDelete()}>Cancel</button>
      {#if deleteError&&(deleteConflict||deleteRevision===null)}<button type="button" disabled={deleteBusy||deleteReading} on:click={()=>void readDelete()}>Reload saved entry</button>{/if}
      <button type="button" disabled={deleteBusy||deleteReading||deleteRevision===null||deleteConflict} on:click={()=>void remove()}>{deleteBusy?'Deleting…':deleteError?'Retry delete':'Delete'}</button></footer>
    </div>
   </div>
  {/if}
</section>
<style>
  .vocabulary-view{position:relative;display:flex;flex:1;min-width:0;min-height:0;overflow:hidden}.vocabulary-table-host{display:flex;flex-direction:column;flex:1;min-width:0;min-height:0;padding:24px}
  header{display:flex;align-items:center;gap:12px;margin-bottom:24px;flex:none}h1{font-size:20px;font-weight:600}.add{display:grid;place-items:center;width:32px;height:32px;border:1px solid #303040;border-radius:8px;background:#1d1d29;color:var(--accent-light)}.add:hover{background:color-mix(in srgb,var(--accent) 10%,transparent);border-color:var(--accent)}.add svg{display:block;width:20px;height:20px}
  .vocabulary-table-scroll{flex:1;min-width:0;min-height:0;overflow:auto;scrollbar-gutter:stable}table{width:100%;table-layout:fixed;border-collapse:collapse;font-size:14px}.korean-column,.pronunciation-column{width:28%}th,td{padding:12px;text-align:left;border-bottom:1px solid #30303d;overflow-wrap:anywhere;vertical-align:top}th{font-size:13px;font-weight:500;color:#aaa;background:#17171f;position:sticky;top:0}td{white-space:pre-wrap;color:#ddd}
  .empty{padding:24px 12px;color:#aaa;font-size:13px}.list-error{padding:16px 12px}.list-error p{color:#fca5a5;font-size:13px;overflow-wrap:anywhere}.list-error button{margin-top:8px;text-decoration:underline;font-size:13px}footer{display:flex;justify-content:flex-end;align-items:center;flex-wrap:wrap;gap:12px;padding:20px 0;font-size:13px;color:#aaa}footer button{padding:8px 12px;border:1px solid #363641;border-radius:8px;color:#ddd}button:disabled{opacity:.45}button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  @media(max-width:500px){.vocabulary-table-host{padding:16px}th,td{padding:9px 6px;font-size:12px}.korean-column,.pronunciation-column{width:30%}}
  .actions-column{width:118px}.row-actions{display:flex;gap:6px}.row-actions button{font-size:12px;padding:4px 6px;border:1px solid #363641;border-radius:6px}.row-actions button:hover{color:var(--accent-light);border-color:var(--accent)}
  .delete-overlay{position:fixed;inset:0;z-index:150;display:grid;place-items:center;padding:16px;background:#000b}.delete-dialog{width:min(440px,100%);max-height:calc(100dvh - 32px);overflow:auto;padding:24px;border:1px solid #383848;border-radius:14px;background:#17171f;box-shadow:0 24px 70px #0008}.delete-dialog h2{font-size:16px;font-weight:600}.delete-dialog p{margin-top:12px;font-size:14px;white-space:pre-wrap;overflow-wrap:anywhere}.delete-dialog footer{padding-bottom:0}.delete-dialog strong{font-weight:600}.delete-error{color:#fca5a5}
  @media(max-width:500px){.actions-column{width:64px}.row-actions{flex-direction:column;gap:6px}}
</style>

<script lang="ts">
  import {createEventDispatcher,onMount,onDestroy,tick} from 'svelte';
  import {allLibraryCategories,type CategoryClient,type CategoryItem,type CategoryMembership,type LibraryCategory} from '../modules/registry';
  export let client:CategoryClient;
  export let item:CategoryItem;
  export let label:string;
  const dispatch=createEventDispatcher<{close:void;saved:CategoryMembership}>();
  let dialog:HTMLDivElement,categories:LibraryCategory[]=[],selected:string[]=[],version=0;
  let loading=true,loaded=false,busy=false,error='',alive=true,request:AbortController|null=null;
  let token=crypto.randomUUID(),submitted:string|null=null;
  async function load(){
    request?.abort();const current=new AbortController();request=current;loading=true;loaded=false;error='';
    try{
      const [values,membership]=await Promise.all([allLibraryCategories(client,current.signal),client.membership(item,current.signal)]);
      if(!alive||current.signal.aborted)return;
      categories=values;selected=membership.category_ids;version=membership.version;token=crypto.randomUUID();submitted=null;loaded=true;
    }catch(e){if(alive&&!current.signal.aborted)error=e instanceof Error?e.message:'Could not load categories.';}
    finally{if(alive&&request===current){loading=false;await tick();dialog?.focus();}}
  }
  function close(){if(!busy)dispatch('close');}
  function toggle(identity:string){selected=selected.includes(identity)?selected.filter(id=>id!==identity):[...selected,identity];}
  function keys(event:KeyboardEvent){
    if(event.key==='Escape'){event.preventDefault();event.stopPropagation();close();}
    if(event.key==='Tab'){
      const controls=[...dialog.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled)')],first=controls[0],last=controls[controls.length-1];
      if(!first){event.preventDefault();dialog.focus();return;}
      if(!dialog.contains(document.activeElement)||document.activeElement===dialog){event.preventDefault();(event.shiftKey?last:first).focus();}
      else if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
      else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
    }
  }
  async function save(){
    if(busy||loading||!loaded)return;
    const chosen=[...selected].sort(),signature=JSON.stringify(chosen);
    if(submitted!==null&&submitted!==signature)token=crypto.randomUUID();
    submitted=signature;busy=true;error='';await tick();if(alive)dialog.focus();
    try{
      const result=await client.saveMembership(item,chosen,version,token);
      if(alive)dispatch('saved',result);
    }catch(e){if(alive)error=e instanceof Error?e.message:'Could not save categories.';}
    finally{if(alive){busy=false;await tick();dialog?.focus();}}
  }
  onMount(()=>{dialog.focus();void load();});
  onDestroy(()=>{alive=false;request?.abort();});
</script>

<div class="category-membership-backdrop">
  <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
  <div class="category-membership-dialog" role="dialog" aria-modal="true" aria-label="Add to categories" tabindex="-1" bind:this={dialog} on:keydown={keys}>
    <h2>Add to categories</h2><p class="item-label">{label}</p>
    <p>Items without a category appear in Default.</p>
    {#if loading}<p role="status">Loading categories…</p>
    {:else if categories.length}<div class="choices">{#each categories as category (category.id)}<label><input type="checkbox" checked={selected.includes(category.id)} disabled={busy} on:change={()=>toggle(category.id)}/><span>{category.name}</span></label>{/each}</div>
    {:else if !error}<p>Create categories in Settings first.</p>{/if}
    {#if error}<p class="error" role="alert">{error}</p><button type="button" class="retry" disabled={busy} on:click={load}>Reload saved selection</button>{/if}
    <footer><button type="button" disabled={busy} on:click={close}>Cancel</button><button type="button" class="save" disabled={busy||loading||!loaded} on:click={save}>{busy?'Saving…':'Save'}</button></footer>
  </div>
</div>

<style>
  .category-membership-backdrop{position:fixed;inset:0;z-index:145;background:#0009;display:flex;align-items:center;justify-content:center;padding:16px}
  .category-membership-dialog{width:400px;max-width:100%;max-height:100%;overflow:auto;padding:22px;border:1px solid #303040;border-radius:14px;background:#14141c;color:#e5e7eb;box-shadow:0 20px 60px #0008}
  h2{font-size:16px;font-weight:600}.item-label{font-size:14px;color:#ddd;overflow-wrap:anywhere;margin-top:10px}p{font-size:12px;color:#9ca3af;margin-top:10px}.choices{max-height:45vh;overflow-y:auto;margin-top:18px}
  label{display:flex;align-items:center;gap:10px;padding:8px 2px;font-size:14px}label span{overflow-wrap:anywhere}input{accent-color:var(--accent);flex:none}footer{display:flex;justify-content:flex-end;gap:10px;margin-top:20px}footer button{padding:8px 16px;font-size:13px;border-radius:8px}.save{color:var(--accent);background:var(--accent-bg);border:1px solid var(--accent-border)}.error{color:#fca5a5}.retry{color:var(--accent);font-size:12px;padding:8px 0}button:disabled{opacity:.4}button:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
</style>

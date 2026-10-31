<script lang="ts">
  import {getContext,onDestroy,tick} from 'svelte';
  import {enabledModules,suiteModules} from '../lib/suiteStores';
  import {SETTINGS_SESSION,type SettingsSession} from '../lib/settingsSession';
  import {moduleUi,libraryCategoriesRefresh,type LibraryCategory} from '../modules/registry';
  const session=getContext<SettingsSession>(SETTINGS_SESSION);
  let selected='video',loadedKey='',items:LibraryCategory[]=[],created:LibraryCategory[]=[],total=0;
  let loading=false,loadError='',controller:AbortController|null=null,sequence=0,alive=true;
  let open=false,name='',error='',busy=false,composing=false,token='',submitted:string|null=null;
  let dialog:HTMLDivElement,input:HTMLInputElement,addButton:HTMLButtonElement;
  $: owners=$suiteModules.filter(owner=>Boolean(moduleUi(owner.slug).categories));
  $: if(owners.length&&!owners.some(owner=>owner.slug===selected))selected=owners[0].slug;
  $: owner=owners.find(candidate=>candidate.slug===selected);
  $: available=Boolean(owner&&$enabledModules.includes(selected));
  $: loadKey=selected+':'+available;
  $: if(loadKey!==loadedKey){loadedKey=loadKey;void load(false);}
  $: visible=[...items,...created.filter(item=>!items.some(other=>other.id===item.id))];
  $: valid=Boolean(name.trim())&&Array.from(name.trim().normalize('NFC')).length<=100;
  const unregister=session.register('library-categories',{dismissOverlay(){
    if(!open)return false;
    void close();return true;
  }});
  onDestroy(()=>{alive=false;sequence++;controller?.abort();unregister();});

  async function load(more:boolean){
    controller?.abort();controller=new AbortController();const request=++sequence;
    const client=moduleUi(selected).categories;
    if(!more){items=[];created=[];total=0;}
    loadError='';loading=false;
    if(!available||!client)return;
    loading=true;
    try{
      const result=await client.list(more?items.length:0,controller.signal);
      if(!alive||request!==sequence)return;
      items=more?[...items,...result.items]:result.items;total=result.total;
    }catch(e){if(alive&&request===sequence)loadError=e instanceof Error?e.message:'Could not load categories.';}
    finally{if(alive&&request===sequence)loading=false;}
  }
  async function show(){
    if(!available)return;
    name='';error='';token=crypto.randomUUID();submitted=null;open=true;composing=false;
    await tick();if(alive&&open)input.focus();
  }
  async function close(){
    if(busy)return;
    open=false;await tick();if(alive)addButton?.focus();
  }
  function keys(event:KeyboardEvent){
    if(event.key==='Escape'){event.preventDefault();event.stopPropagation();void close();}
    if(event.key==='Enter'&&(composing||event.isComposing||event.keyCode===229))event.preventDefault();
    if(event.key==='Tab'){
      const controls=[...dialog.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled)')];
      const first=controls[0],last=controls[controls.length-1];
      if(!first){event.preventDefault();dialog.focus();return;}
      if(!dialog.contains(document.activeElement)||document.activeElement===dialog){event.preventDefault();(event.shiftKey?last:first).focus();}
      else if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
      else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
    }
  }
  async function save(){
    const client=moduleUi(selected).categories,value=name.trim().normalize('NFC');
    if(busy||composing||!valid||!available||!client)return;
    if(submitted!==null&&submitted!==value)token=crypto.randomUUID();
    submitted=value;error='';busy=true;
    await tick();if(alive)dialog.focus();
    try{
      const category=await client.create(value,token);
      if(!alive)return;
      if(!visible.some(item=>item.id===category.id)){created=[...created,category];total++;}
      libraryCategoriesRefresh.update(value=>value+1);
      busy=false;await close();
    }catch(e){if(alive)error=e instanceof Error?e.message:'Could not create category.';}
    finally{if(alive){busy=false;await tick();if(error&&open)input.focus();}}
  }
</script>

<section id="setting-library-categories" class="categories-settings" aria-label="Library categories">
  <header>
    <div class="owner-tabs" role="group" aria-label="Category module">
      {#each owners as choice}<button type="button" aria-pressed={selected===choice.slug} on:click={()=>selected=choice.slug}>{choice.name}</button>{/each}
    </div>
    <button class="add-category" type="button" aria-label="Create new category" title="Create new category" disabled={!available||loading||Boolean(loadError)} bind:this={addButton} on:click={show}>
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>
    </button>
  </header>
  {#if !owner}<p>Video and Manga categories are unavailable.</p>
  {:else if !available}<p>Enable {owner.name} in Modules to create categories.</p>
  {:else}
    {#if visible.length}<ul aria-label={`${owner.name} categories`}>{#each visible as category (category.id)}<li>{category.name}</li>{/each}</ul>
    {:else if !loading&&!loadError}<p>No categories yet.</p>{/if}
    {#if loading}<p role="status">Loading categories…</p>{/if}
    {#if loadError}<p role="alert">{loadError}</p><button class="retry" type="button" on:click={()=>load(items.length>0)}>Retry</button>
    {:else if items.length<total-created.filter(item=>!items.some(other=>other.id===item.id)).length&&!loading}<button class="retry" type="button" on:click={()=>load(true)}>Load more</button>{/if}
  {/if}
</section>

{#if open}
  <div class="category-backdrop" role="presentation" on:click|stopPropagation use:session.portal>
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <div class="category-dialog" role="dialog" aria-modal="true" aria-label="Create new category" tabindex="-1" bind:this={dialog} on:keydown={keys}>
      <h2>Create new category</h2>
      <form on:submit|preventDefault={save}>
        <label for="category-name">Category name</label>
        <input id="category-name" bind:this={input} bind:value={name} maxlength="1024" autocomplete="off" required disabled={busy} on:compositionstart={()=>composing=true} on:compositionend={()=>composing=false}/>
        {#if !valid&&name.trim()}<p class="error" role="alert">Use 100 characters or fewer.</p>{/if}
        {#if error}<p class="error" role="alert">{error}</p>{/if}
        <footer><button type="button" disabled={busy} on:click={close}>Cancel</button><button class="create" type="submit" disabled={busy||!valid||!available}>{busy?'Creating…':'Create'}</button></footer>
      </form>
    </div>
  </div>
{/if}

<style>
  .categories-settings{max-width:48rem;margin:0 auto;color:#e5e7eb}header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:18px}.owner-tabs{display:flex;gap:18px}.owner-tabs button{padding:6px 0;font-size:14px;color:#9ca3af}.owner-tabs button[aria-pressed=true]{color:var(--accent)}.add-category{display:grid;place-items:center;width:32px;height:32px;border-radius:8px}.add-category:hover{background:#ffffff08}ul{margin:0;padding:0;list-style:none}li{padding:10px 0;font-size:14px;overflow-wrap:anywhere}p{font-size:13px;color:#9ca3af;margin-top:10px}.retry{padding:8px 0;font-size:13px;color:var(--accent)}
  .category-backdrop{position:fixed;inset:0;z-index:110;display:flex;align-items:center;justify-content:center;background:#0008;padding:16px}.category-dialog{width:380px;max-width:100%;max-height:100%;overflow:auto;border:1px solid #303040;border-radius:14px;background:#14141c;color:#e5e7eb;padding:20px;box-shadow:0 20px 60px #0008}h2{font-size:16px;font-weight:600;margin-bottom:20px}label{display:block;font-size:13px;color:#aaa;margin-bottom:8px}input{width:100%;border:1px solid #303040;border-radius:8px;background:#0d0d13;padding:9px 11px;font-size:14px}footer{display:flex;justify-content:flex-end;gap:10px;margin-top:20px}footer button{border-radius:8px;padding:8px 16px;font-size:13px}.create{background:var(--accent-bg);color:var(--accent);border:1px solid var(--accent-border)}.error{color:#fca5a5}button:disabled{opacity:.4}button:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
</style>

<script lang="ts">
  import {getContext} from 'svelte';
  import {suiteDataApi as api,type CollectionFolder,type CollectionPreview} from '../lib/suiteDataApi';
  import {SETTINGS_SESSION,type SettingsSession} from '../lib/settingsSession';
  export let toolRunning=false;
  const session=getContext<SettingsSession>(SETTINGS_SESSION);
  let folders:CollectionFolder[]=[];
  let selected:string[]=[];
  let loaded=false,busy=false;
  let mode:'export'|'import'='export';
  let directory='',destination='';
  let personal=false;
  let preview:CollectionPreview|null=null;
  let requestId='',message='',error='';
  let rebuildReady=false;
  $: blocked=busy||toolRunning;
  function invalidate(){preview=null;requestId='';message='';error='';}
  async function load(){
    if(loaded)return;
    busy=true;error='';
    try{folders=await api.collectionSources();loaded=true;}
    catch(caught){error=caught instanceof Error?caught.message:'Could not read folders.';}
    finally{busy=false;}
  }
  async function choose(kind:'directory'|'destination'){
    if(blocked)return;
    busy=true;error='';
    try{
      const initial=directory||destination||(await api.getStorageConfiguration()).suite_home;
      const path=await session.pickDirectory(initial);
      if(!path)return;
      invalidate();
      if(kind==='directory')directory=path;
      else destination=path.replace(/[\\/]+$/,'')+'/Keivotos collection';
    }catch(caught){error=caught instanceof Error?caught.message:'Could not choose a folder.';}
    finally{busy=false;}
  }
  function select(id:string){
    selected=selected.includes(id)?selected.filter(value=>value!==id):[...selected,id];
    invalidate();
  }
  async function review(){
    if(blocked)return;
    busy=true;preview=null;message='';error='';
    try{
      preview=mode==='export'?await api.previewCollectionExport(selected,personal):
        await api.previewCollectionImport(directory,destination,personal);
      requestId=crypto.randomUUID();
    }catch(caught){error=caught instanceof Error?caught.message:'Could not review this collection.';}
    finally{busy=false;}
  }
  async function transfer(){
    if(blocked||!preview)return;
    busy=true;error='';message='';
    try{
      const result=mode==='export'?await api.exportCollection(selected,destination,personal,requestId):
        await api.importCollection(directory,destination,personal,requestId);
      message=(mode==='export'?'Exported ':'Imported ')+result.files+' files in '+result.sources+' folders.';
      preview=null;
      if(mode==='import'){loaded=false;await load();}
    }catch(caught){error=caught instanceof Error?caught.message:'Collection transfer failed. You can retry.';}
    finally{busy=false;}
  }
  async function rebuild(){
    if(blocked||!rebuildReady)return;
    busy=true;error='';message='';
    try{await api.rebuildIndex();message='Library index rebuilt.';rebuildReady=false;}
    catch(caught){error=caught instanceof Error?caught.message:'Could not rebuild the index.';}
    finally{busy=false;}
  }
</script>

<section class="recovery-card" aria-label="Rebuild library index">
  <div class="heading"><h3>Rebuild library index</h3>
    <button disabled={blocked} on:click={()=>rebuildReady=!rebuildReady}>{rebuildReady?'Cancel':'Review rebuild'}</button></div>
  {#if rebuildReady}
    <p>Recreate the library’s searchable information from available files and saved metadata. Personal records and original files are preserved.</p>
    <button disabled={blocked} on:click={rebuild}>Rebuild index</button>
  {/if}
</section>
<details class="recovery-card" on:toggle={(event)=>{if(event.currentTarget.open)void load();}}>
  <summary>Transfer collections</summary>
  <div class="transfer-body">
    <div class="heading" role="group" aria-label="Collection operation">
      <button aria-pressed={mode==='export'} disabled={blocked} on:click={()=>{mode='export';invalidate();}}>Export</button>
      <button aria-pressed={mode==='import'} disabled={blocked} on:click={()=>{mode='import';invalidate();}}>Import</button>
    </div>
    {#if mode==='export'}
      <p>Copy selected folders with their original names, files and saved metadata.</p>
      {#if !loaded}<button disabled={blocked} on:click={load}>Retry folders</button>
      {:else if !folders.length}<p>No available folders.</p>
      {:else}<fieldset disabled={blocked}><legend>Folders to export</legend>
        {#each folders as folder}<label class="folder"><input type="checkbox" checked={selected.includes(folder.source_id)} on:change={()=>select(folder.source_id)}/><span>{folder.display_name}<small>{folder.role}</small></span></label>{/each}
      </fieldset>{/if}
    {:else}
      <label for="collection-source">Exported collection folder</label>
      <div class="path"><input id="collection-source" bind:value={directory} disabled={blocked} on:input={invalidate}/><button disabled={blocked} on:click={()=>choose('directory')}>Choose folder</button></div>
    {/if}
    <label for="collection-destination">New destination folder</label>
    <div class="path"><input id="collection-destination" bind:value={destination} disabled={blocked} on:input={invalidate}/><button disabled={blocked} on:click={()=>choose('destination')}>Choose parent</button></div>
    <label class="personal"><input type="checkbox" bind:checked={personal} disabled={blocked} on:change={invalidate}/>Include associated personal records</label>
    <button disabled={blocked||!destination||(mode==='export'?!selected.length:!directory)} on:click={review}>Review collection</button>
    {#if preview}
      <div class="preview" aria-label="Collection preview"><p>{preview.files} files · {preview.sources.length} folders</p>
        {#each preview.sources as source}<p>{source.name} <small>{source.owner}</small></p>{/each}
        {#if preview.personal_records!==undefined}<p>{preview.personal_records} associated personal records</p>{/if}
        <button disabled={blocked} on:click={transfer}>{mode==='export'?'Export collection':'Import collection'}</button>
      </div>
    {/if}
  </div>
</details>
{#if busy}<p role="status">Working…</p>{/if}
{#if message}<p role="status" class="success">{message}</p>{/if}
{#if error}<p role="alert" class="error">{error}</p>{/if}

<style>
  .recovery-card{border:1px solid #2d2f3f;border-radius:12px;background:#0f1018;color:#ececf3;padding:16px;font-size:14px}
  .heading,.path{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.heading{justify-content:space-between}
  h3,summary{font-weight:600;font-size:15px}summary{cursor:pointer}.transfer-body{display:flex;flex-direction:column;gap:12px;padding-top:16px}
  p,small{font-size:12px;color:#a2a4b6;line-height:1.6}section p{margin:12px 0}
  button{border:1px solid #35374a;border-radius:7px;background:#1c1d2b;padding:8px 12px;color:#dddde8;font-size:12px;align-self:flex-start}
  button[aria-pressed=true]{border-color:var(--accent);color:var(--accent)}button:disabled{opacity:.4;cursor:default}
  input:not([type=checkbox]){background:#15151e;border:1px solid #35374a;border-radius:7px;padding:8px;min-width:0;flex:1;color:#ececf3}
  input[type=checkbox]{accent-color:var(--accent)}fieldset{border:1px solid #35374a;border-radius:7px;padding:10px;max-height:210px;overflow:auto}
  .folder,.personal{display:flex;align-items:center;gap:10px;padding:5px 0}.folder span{min-width:0;overflow-wrap:anywhere}.folder small{display:block}
  .path input{min-width:150px}.preview{border-top:1px solid #35374a;padding-top:12px}.preview button{margin-top:10px}
  .success{color:#86efac}.error{color:#fca5a5}button:focus-visible,input:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
  @media(max-width:500px){.path{align-items:stretch;flex-direction:column}}
</style>

<script lang="ts">
  import {getContext,onDestroy} from 'svelte';
  import {SETTINGS_SESSION,type SettingsSession} from '../../lib/settingsSession';
  import {persistentStorageKey} from '../../lib/product';
  import {twitterApi,type Preferences,type TwitterFolder} from './api';
  export let selectedSection:string;
  export let query='';
  const session=getContext<SettingsSession>(SETTINGS_SESSION);
  let preferences:Preferences|null=null,folders:TwitterFolder[]=[],loaded=false,loading=false,busy=false,error='',pending=false,alive=true;
  let request:AbortController|null=null;
  $: if(selectedSection==='twitter-capture'&&!loaded)void load();
  function pendingRequest(){
    try{return !!sessionStorage.getItem(persistentStorageKey('twitter-pending-request'));}catch{return false;}
  }
  async function load(){
    loaded=true;loading=true;request?.abort();const current=new AbortController();request=current;
    try{
      const [saved,registered]=await Promise.all([twitterApi.settings(current.signal),twitterApi.folders(current.signal)]);
      if(!alive||current.signal.aborted)return;preferences=saved;folders=registered;pending=pendingRequest();error='';
    }catch(e){if(alive&&!current.signal.aborted)error=e instanceof Error?e.message:'Could not load the save folder.';}
    finally{if(request===current)loading=false;}
  }
  async function save(sourceId:string){
    if(busy||pendingRequest())return;busy=true;error='';session.setBusy('twitter',true);
    try{const saved=await twitterApi.saveSettings(sourceId);if(alive)preferences=saved;}
    catch(e){if(alive)error=e instanceof Error?e.message:'Could not save the folder.';}
    finally{busy=false;session.setBusy('twitter',false);}
  }
  const unregister=session.register('twitter',{reloadFolders:load});
  onDestroy(()=>{alive=false;request?.abort();unregister();});
</script>
{#if !query&&selectedSection==='twitter-capture'}
<section id="setting-twitter-destination" class="destination">
  <label for="twitter-destination">Save to</label>
  <select id="twitter-destination" disabled={loading||busy||pending||!preferences} value={preferences?.source_id??''} on:change={event=>save((event.currentTarget as HTMLSelectElement).value)}>
    <option value="" disabled>Choose a Twitter folder</option>
    {#each folders as folder (folder.source_id)}<option value={folder.source_id} disabled={!folder.available}>{folder.name}{folder.available?'':' (unavailable)'}</option>{/each}
  </select>
  {#if preferences&&!preferences.available}<p>Assign a visible Twitter folder in Files > Folders.</p>{/if}
  {#if preferences&&!preferences.dependencies.ready}<p>Missing: {preferences.dependencies.missing.join(', ')}</p>{/if}
  {#if pending}<p>Confirm the pending download request before changing its save folder.</p>{/if}
  {#if loading}<p role="status">Loading save folder…</p>{/if}
  {#if error}<p role="alert">{error}</p><button disabled={busy} on:click={load}>Retry</button>{/if}
</section>
{/if}
<style>
  .destination{display:grid;gap:12px;max-width:816px;margin:auto;padding:20px;border:1px solid #2d2f3f;border-radius:12px;background:#0f1018}label{font-size:14px;color:#ececf3}select{width:100%;min-width:0;padding:9px 10px;border:1px solid #34343f;background:#101016;border-radius:10px;font-size:13px}p{font-size:12px;line-height:1.5;color:#aaaab9;overflow-wrap:anywhere}button{justify-self:start;color:var(--accent);font-size:13px}button:focus-visible,select:focus-visible{outline:2px solid var(--accent);outline-offset:3px}select:disabled{opacity:.5}
</style>

<script lang="ts">
  import type {Preferences,TwitterFolder} from './api';
  export let preferences:Preferences|null=null;
  export let folders:TwitterFolder[]=[];
  export let busy=false;
  export let error='';
  export let onSave:(sourceId:string)=>void;
  export let onManage:()=>void;
</script>
<div class="destination">
  <label for="twitter-destination">Save to</label>
  <select id="twitter-destination" disabled={busy||!preferences} value={preferences?.source_id??''} on:change={event=>onSave((event.currentTarget as HTMLSelectElement).value)}>
    <option value="" disabled>Choose a Twitter folder</option>
    {#each folders as folder (folder.source_id)}<option value={folder.source_id} disabled={!folder.available}>{folder.name}{folder.available?'':' (unavailable)'}</option>{/each}
  </select>
  {#if preferences&&!preferences.available}<p>Choose a visible Twitter folder in Files settings.</p>{/if}
  {#if preferences&&!preferences.dependencies.ready}<p>Missing: {preferences.dependencies.missing.join(', ')}</p>{/if}
  {#if error}<p role="alert">{error}</p>{/if}
  <button class="manage" on:click={onManage}>Folders settings</button>
</div>
<style>
  .destination{display:grid;gap:10px}label{font-size:13px;color:#aaaab9}select{width:100%;min-width:0;padding:9px 10px;border:1px solid #34343f;background:#101016;border-radius:10px;font-size:13px}p{font-size:12px;line-height:1.5;color:#aaaab9;overflow-wrap:anywhere}.manage{justify-self:start;color:var(--accent);font-size:12px}.manage:hover{text-decoration:underline}button:focus-visible,select:focus-visible{outline:2px solid var(--accent);outline-offset:3px}select:disabled{opacity:.5}
</style>

<script lang="ts">
  import { settingsStyle, type SettingsStyle } from '../lib/appearance';
  import SettingsSwitch from '../components/SettingsSwitch.svelte';
  import { settingsError } from '../lib/http';
  import { onMount } from 'svelte';
  import { suiteDataApi } from '../lib/suiteDataApi';
  import type { DiagnosticsPreferences } from '../lib/suiteApiTypes';
  let preferences: DiagnosticsPreferences | null = null;
  let busy = false;
  let error = '';
  let notice = '';
  let loading = false;
  async function load() {
    loading = true; error = '';
    try { preferences = await suiteDataApi.getDiagnostics(); await loadPolicy(); }
    catch (caught) { error = settingsError(caught, 'Diagnostic preferences'); }
    loading = false;
  }

  let policyInfo: import('../lib/suiteApiTypes').DiagnosticPolicy | null = null;
  let limits = {retention_days:14,total_mib:256,rotation_mib:16};
  let clock = Date.now();
  $: debugRemaining = policyInfo?.debug.active && policyInfo.debug.expires_at
    ? Math.max(0,Math.ceil(policyInfo.debug.expires_at-clock/1000)) : 0;
  async function loadPolicy() {
    policyInfo=await suiteDataApi.getDiagnosticPolicy();
    limits={...policyInfo.limits};clock=Date.now();
  }
  onMount(()=>{
    const timer=setInterval(()=>{
      if(policyInfo?.debug.active){
        clock=Date.now();
        if(policyInfo.debug.expires_at && policyInfo.debug.expires_at<=clock/1000){
          policyInfo={...policyInfo,debug:{active:false,expires_at:null,remaining_seconds:0}};
          void loadPolicy().catch(caught=>{error=settingsError(caught,'Logging');});
        }
      }
    },1000);
    return ()=>clearInterval(timer);
  });
  async function saveLimits() {
    if(busy)return;
    if(!Object.values(limits).every(Number.isInteger)){error='Enter whole numbers for log limits.';return;}
    busy=true;error='';notice='';
    try {
      policyInfo=await suiteDataApi.configureDiagnosticPolicy({...limits});
      limits={...policyInfo.limits};notice='Log limits saved.';
    }catch(caught){error=settingsError(caught,'Log limits');}
    finally{busy=false;}
  }
  async function debugLogging() {
    if(busy)return;
    busy=true;error='';notice='';
    try {
      policyInfo=await suiteDataApi.setDiagnosticDebug(!policyInfo?.debug.active);
      clock=Date.now();
    }catch(caught){error=settingsError(caught,'Debug logging');}
    finally{busy=false;}
  }

  onMount(() => {void load();});
  async function toggle(key: keyof DiagnosticsPreferences) {
    if (!preferences || busy) return;
    const previous = preferences;
    preferences = { ...preferences, [key]: !preferences[key] };
    busy = true; error = ''; notice = '';
    try { preferences = await suiteDataApi.configureDiagnostics(preferences); }
    catch (e) { preferences = previous; error = settingsError(e, 'Diagnostics'); }
    finally { busy = false; }
  }
  async function open(target: 'data' | 'logs' | 'runtime' | 'access') {
    busy = true; error = ''; notice = '';
    try { await suiteDataApi.openDiagnostics(target); notice = 'Opened with your desktop application.'; }
    catch (e) { error = settingsError(e, 'Diagnostics'); }
    finally { busy = false; }
  }
</script>

<div class="mx-auto max-w-3xl space-y-4" id="setting-diagnostics">
  {#if error}<p role="alert" class="text-sm text-red-300">{error} <button class="ml-2 underline" disabled={loading || busy} on:click={load}>Retry</button></p>{/if}
  {#if notice}<p role="status" class="text-sm text-gray-400">{notice}</p>{/if}
  <section class="space-y-4 rounded-xl border border-[#292938] bg-[#111118] p-4">
    <div class="flex flex-wrap gap-2">
      {#each [{ target: 'data' as const, label: 'Open data folder' }, { target: 'logs' as const, label: 'Open logs folder' }, { target: 'runtime' as const, label: 'Open application log' }, { target: 'access' as const, label: 'Open request log' }] as item}
        <button type="button" disabled={busy} on:click={() => open(item.target)} class="rounded-lg border border-[#303040] px-3 py-2 text-xs text-gray-200 hover:bg-[#1a1a23] disabled:opacity-50">{item.label}</button>
      {/each}
    </div>
  </section>
  <section id="setting-settings-style" class="flex items-center justify-between gap-5 rounded-xl border border-[#292938] bg-[#111118] px-4 py-3">
    <label for="settings-style" class="text-sm text-gray-200">Settings style</label>
    <select id="settings-style" class="rounded-lg border border-[#303040] bg-[#0d0d13] px-3 py-2 text-xs text-gray-200" value={$settingsStyle} on:change={event => settingsStyle.set(event.currentTarget.value as SettingsStyle)}><option value="modern">Modern</option><option value="legacy">Legacy</option></select>
  </section>

  {#if policyInfo}
    <section aria-label="Log limits" class="space-y-4 rounded-xl border border-[#292938] bg-[#111118] p-4">
      <div><div class="text-sm font-medium text-gray-200">Log limits</div><p class="mt-1 text-xs text-gray-500">Only application diagnostic logs are cleaned up. Existing older logs are kept.</p></div>
      <form class="space-y-3" on:submit|preventDefault={saveLimits}>
        <div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <label class="space-y-1 text-xs text-gray-400">Keep logs (days)<input aria-label="Keep logs (days)" type="number" required min="1" max="365" step="1" bind:value={limits.retention_days} disabled={busy} class="block w-full rounded-lg border border-[#303040] bg-[#0d0d13] px-3 py-2 text-gray-200" /></label>
          <label class="space-y-1 text-xs text-gray-400">Total log space (MiB)<input aria-label="Total log space (MiB)" type="number" required min="16" max="4096" step="1" bind:value={limits.total_mib} disabled={busy} class="block w-full rounded-lg border border-[#303040] bg-[#0d0d13] px-3 py-2 text-gray-200" /></label>
          <label class="space-y-1 text-xs text-gray-400">Rotate each file (MiB)<input aria-label="Rotate each file (MiB)" type="number" required min="1" max="64" step="1" bind:value={limits.rotation_mib} disabled={busy} class="block w-full rounded-lg border border-[#303040] bg-[#0d0d13] px-3 py-2 text-gray-200" /></label>
        </div>
        <button type="submit" disabled={busy} class="rounded-lg border border-[#303040] px-3 py-2 text-xs text-gray-200 hover:bg-[#1a1a23] disabled:opacity-50">Save log limits</button>
      </form>
      <div class="flex flex-wrap items-center justify-between gap-3 border-t border-[#292938] pt-3">
        <div><div class="text-sm text-gray-200">Temporary debug logging</div><p class="mt-1 text-xs text-gray-500">{#if debugRemaining>0}Stops in {Math.floor(debugRemaining/60)}:{String(debugRemaining%60).padStart(2,'0')}{:else}Detailed logging stops automatically after 30 minutes.{/if}</p></div>
        <button type="button" disabled={busy} on:click={debugLogging} class="rounded-lg border border-[#303040] px-3 py-2 text-xs text-gray-200 hover:bg-[#1a1a23] disabled:opacity-50">{policyInfo.debug.active?'Stop debug logging':'Start debug logging'}</button>
      </div>
      {#if policyInfo.status.fallback || policyInfo.status.buffered || policyInfo.status.dropped}
        <p role="status" class="text-xs text-amber-300">{policyInfo.status.fallback?'Using fallback logs. ':''}Buffered: {policyInfo.status.buffered} · Dropped: {policyInfo.status.dropped}</p>
      {/if}
    </section>
  {/if}

  {#if preferences}
    <section class="divide-y divide-[#22222e] overflow-hidden rounded-xl border border-[#292938] bg-[#111118]">
      {#each [{ key: 'verbose_logging' as const, label: 'Verbose logging', description: 'Also show successful read requests in the console and application log. Applies immediately.' }, { key: 'show_experimental_modules' as const, label: 'Show experimental modules', description: 'Show experimental choices in Modules. Enabled modules stay visible.' }] as option}
        <div class="flex items-center justify-between gap-5 px-4 py-3">
          <div><div class="text-sm font-medium text-gray-200">{option.label}</div><p class="mt-1 text-xs text-gray-500">{option.description}</p></div>
          <SettingsSwitch label={option.label} checked={preferences[option.key]} disabled={busy} on:click={() => toggle(option.key)} />
        </div>
      {/each}
    </section>
  {/if}
</div>

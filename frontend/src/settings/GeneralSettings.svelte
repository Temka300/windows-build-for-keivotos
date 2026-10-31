<script lang="ts">
  import { accentStyle, accents, type AccentStyle } from '../lib/appearance';
  import { notificationPosition, type NotificationPosition } from '../lib/notifications';
  import ModulesSettings from './ModulesSettings.svelte';
  import AdvancedSettings from './AdvancedSettings.svelte';
  import { getContext } from 'svelte';
  import { SETTINGS_SESSION, type SettingsSession } from '../lib/settingsSession';
  import { compactSegmentClass } from '../lib/settingsControls';
  import { SUITE_NAME } from '../lib/product';
  import { hideControlsSeconds, holdSpaceEnabled, holdSpeedMultiplier, HOLD_SPEED_MULTIPLIERS } from '../lib/playbackPreferences';
  import SettingsSwitch from '../components/SettingsSwitch.svelte';
  import { enabledModules, interfaceScale, motionPreference, startupModule, suiteModules } from '../lib/suiteStores';
  import type { InterfaceScale, MotionPreference, StartupModule } from '../lib/suiteStores';
  import BackupRestoreSettings from '../components/BackupRestoreSettings.svelte';
  import ThumbnailCacheSettings from '../components/ThumbnailCacheSettings.svelte';
  import AboutSettings from './AboutSettings.svelte';
  import CategoriesSettings from './CategoriesSettings.svelte';
  export let selectedSection: string;
  export let query = '';
  const session = getContext<SettingsSession>(SETTINGS_SESSION);
  const busyOwners = session.busyOwners;
  $: toolRunning = $busyOwners.size > 0;
  $: startupModuleOptions = [
    ...$suiteModules.filter(module => module.is_base || $enabledModules.includes(module.id))
      .map(module => ({ value: module.slug as StartupModule, label: module.name })),
    { value: 'last' as StartupModule, label: 'Last used' },
  ];
  const motionOptions: { value: MotionPreference; label: string }[] = [
    { value: 'system', label: 'System' },
    { value: 'full', label: 'Full' },
    { value: 'reduced', label: 'Reduced' },
  ];

  const interfaceScaleOptions: { value: InterfaceScale; label: string }[] = [
    { value: 'default', label: 'Default' },
    { value: 'comfortable', label: 'Comfortable' },
  ];
  const notificationPositions: { value: NotificationPosition; label: string }[] = [
    { value: 'bottom-right', label: 'Bottom right' }, { value: 'bottom-left', label: 'Bottom left' },
    { value: 'top-right', label: 'Top right' }, { value: 'top-left', label: 'Top left' },
  ];

</script>

{#if !query}
{#if selectedSection === 'modules'}
  <ModulesSettings />
{:else if selectedSection === 'general-advanced'}
  <AdvancedSettings />
{:else if selectedSection === 'about'}
  <AboutSettings />
{:else if selectedSection === 'categories'}
  <CategoriesSettings />
{:else if selectedSection === 'appearance'}
          <div class="mx-auto max-w-3xl space-y-4">
            <section class="overflow-hidden rounded-xl border border-[#292938] bg-[#111118]">
              <div class="divide-y divide-[#22222e]">
                <div id="setting-accent-style" class="flex items-center justify-between gap-5 px-4 py-3">
                  <label for="accent-style" class="text-sm font-medium text-gray-200">Accent style</label>
                  <select id="accent-style" class="rounded-lg border border-[#303040] bg-[#0d0d13] px-3 py-2 text-xs text-gray-200" value={$accentStyle} on:change={event => accentStyle.set(event.currentTarget.value as AccentStyle)}>
                    {#each Object.entries(accents) as [value, accent]}<option {value}>{accent.name}</option>{/each}
                  </select>
                </div>
                <div id="setting-motion" class="flex items-center justify-between gap-5 px-4 py-3">
                  <div class="text-sm font-medium text-gray-200">Interface motion</div>
                  <div class="flex divide-x divide-[#303040] overflow-hidden rounded-lg border border-[#303040]">{#each motionOptions as option}<button class={compactSegmentClass($motionPreference === option.value)} type="button" on:click={() => motionPreference.set(option.value)}>{option.label}</button>{/each}</div>
                </div>
                <div id="setting-interface-scale" class="flex items-center justify-between gap-5 px-4 py-3">
                  <div class="text-sm font-medium text-gray-200">Interface scale</div>
                  <div class="flex divide-x divide-[#303040] overflow-hidden rounded-lg border border-[#303040]">{#each interfaceScaleOptions as option}<button class={compactSegmentClass($interfaceScale === option.value)} type="button" on:click={() => interfaceScale.set(option.value)}>{option.label}</button>{/each}</div>
                </div>
                <div id="setting-notification-position" class="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
                  <label for="notification-position" class="text-sm font-medium text-gray-200">Notification position</label>
                  <select id="notification-position" class="rounded-lg border border-[#303040] bg-[#0d0d13] px-3 py-2 text-xs text-gray-200" value={$notificationPosition} on:change={event => notificationPosition.set(event.currentTarget.value as NotificationPosition)}>
                    {#each notificationPositions as option}<option value={option.value}>{option.label}</option>{/each}
                  </select>
                </div>
              </div>
            </section>
          </div>
{:else if selectedSection === 'startup'}
          <div class="mx-auto max-w-3xl space-y-4">
            <section class="overflow-hidden rounded-xl border border-[#292938] bg-[#111118]">
              <div class="divide-y divide-[#22222e]">
                <div id="setting-startup-module" class="flex items-center justify-between gap-5 px-4 py-3">
                  <div>
                    <div class="text-sm font-medium text-gray-200">Startup destination</div>
                    <div class="mt-0.5 text-xs text-gray-500">Which surface opens when {SUITE_NAME} launches.</div>
                  </div>
                  <select class="w-40 shrink-0 rounded-lg border border-[#303040] bg-[#0d0d13] px-3 py-2 text-xs text-gray-200 outline-none focus:border-purple-400/60" value={$startupModule} on:change={(event) => startupModule.set((event.currentTarget as HTMLSelectElement).value as StartupModule)}>
                    {#each startupModuleOptions as option}<option value={option.value}>{option.label}</option>{/each}
                  </select>
                </div>
              </div>
            </section>
          </div>
{:else if selectedSection === 'storage'}
          <div class="mx-auto max-w-3xl space-y-4">
            <ThumbnailCacheSettings />
          </div>
{:else if selectedSection === 'player'}
          <div class="mx-auto max-w-3xl space-y-4">
            <section class="overflow-hidden rounded-xl border border-[#292938] bg-[#111118]">
              <div id="setting-hide-controls-seconds" class="flex min-w-0 flex-col items-start gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-5 px-2 py-3 sm:px-4">
                <label for="hide-controls-seconds" class="text-sm font-medium text-gray-200">Hide controls after</label>
                <select id="hide-controls-seconds" class="max-w-full rounded-lg border border-[#303040] bg-[#0d0d13] px-1 py-2 text-xs sm:px-3 text-gray-200" value={$hideControlsSeconds} on:change={event => hideControlsSeconds.set(Number(event.currentTarget.value))}>
                  {#each Array.from({length:10},(_,index)=>index+1) as seconds}<option value={seconds}>{seconds} {seconds===1?'second':'seconds'}</option>{/each}
                </select>
              </div>
              <div id="setting-hold-space" class="flex min-w-0 flex-col items-start gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-5 border-t border-[#292938] px-2 py-3 sm:px-4">
                <div><div class="text-sm font-medium text-gray-200">Hold Space to speed up</div><p class="mt-1 text-xs text-gray-500">Video and YouTube · release to restore your chosen speed</p></div>
                <SettingsSwitch label="Hold Space to speed up" checked={$holdSpaceEnabled} on:click={()=>holdSpaceEnabled.set(!$holdSpaceEnabled)}/>
              </div>
              <div id="setting-hold-speed-multiplier" class="flex min-w-0 flex-col items-start gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-5 border-t border-[#292938] px-2 py-3 sm:px-4">
                <label for="hold-speed-multiplier" class="text-sm font-medium text-gray-200">Hold speed multiplier</label>
                <select id="hold-speed-multiplier" class="max-w-full rounded-lg border border-[#303040] bg-[#0d0d13] px-1 py-2 text-xs sm:px-3 text-gray-200 disabled:opacity-50" value={$holdSpeedMultiplier} disabled={!$holdSpaceEnabled} on:change={event=>holdSpeedMultiplier.set(Number(event.currentTarget.value) as typeof $holdSpeedMultiplier)}>
                  {#each HOLD_SPEED_MULTIPLIERS as multiplier}<option value={multiplier}>{multiplier}×</option>{/each}
                </select>
              </div>
            </section>
          </div>
{:else if selectedSection === 'backup'}
          <div class="mx-auto max-w-3xl space-y-4">
            <BackupRestoreSettings {toolRunning} />
          </div>

{/if}
{/if}

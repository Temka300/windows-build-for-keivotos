<script lang="ts">
  import { settingsStyle } from '../../lib/appearance';
  import { getContext, onMount, onDestroy, tick } from 'svelte';
  import { get } from 'svelte/store';
  import { SETTINGS_SESSION, type SettingsSession } from '../../lib/settingsSession';
  import { iconPath, formatByteCount, compactSegmentClass } from '../../lib/settingsControls';
  import { SUITE_NAME } from '../../lib/product';
  import { suiteModules } from '../../lib/suiteStores';
  import { filesApi, type AttachmentStore, type SourceInfo } from '../../lib/filesApi';
  import { suiteApi, type FolderBatchResult } from '../../lib/suiteApi';
  import ManageFoldersDialog from '../../components/ManageFoldersDialog.svelte';
  import PathAutocomplete from '../../components/PathAutocomplete.svelte';
  export let selectedSection: string;
  export let query = '';
  const session = getContext<SettingsSession>(SETTINGS_SESSION);
  const busyOwners = session.busyOwners;
  $: toolRunning = $busyOwners.size > 0;
  onMount(() => { void loadAttachmentStore(); });
  $: if (selectedSection === 'roots') void loadRoleSources();
  let attachmentStore: AttachmentStore | null = null;
  let attachmentBusy = false;
  let attachmentError = '';
  let attachmentPathDraft = '';
  let attachmentModeView: 'managed' | 'folder' = 'managed';

  async function loadAttachmentStore() {
    try {
      attachmentStore = await filesApi.getAttachmentStore();
      attachmentModeView = attachmentStore.mode;
      if (attachmentStore.mode === 'folder') attachmentPathDraft = attachmentStore.path;
    } catch (error) {
      attachmentError = error instanceof Error ? error.message : String(error);
    }
  }

  // Managed mode: attachments hidden with the suite data (deduped, backed up).
  async function useManagedAttachmentStore() {
    if (attachmentBusy) return;
    attachmentBusy = true;
    attachmentError = '';
    try {
      attachmentStore = await filesApi.setAttachmentStore(null);
      attachmentPathDraft = '';
      attachmentModeView = 'managed';
    } catch (error) {
      attachmentError = error instanceof Error ? error.message : String(error);
    } finally {
      attachmentBusy = false;
    }
  }

  // Folder mode: attachments stored visibly inside the chosen folder.
  async function saveAttachmentFolder(path?: string) {
    const target = (path ?? attachmentPathDraft).trim();
    if (!target || attachmentBusy) return;
    attachmentBusy = true;
    attachmentError = '';
    try {
      attachmentStore = await filesApi.setAttachmentStore(target);
      attachmentPathDraft = attachmentStore.path;
      attachmentModeView = 'folder';
    } catch (error) {
      attachmentError = error instanceof Error ? error.message : String(error);
    } finally {
      attachmentBusy = false;
    }
  }

  // Copy attachments made in managed mode into the chosen visible folder and
  // repoint them, so previously hidden ones become browsable in Files.
  let attachmentMigrateMessage = '';
  async function migrateAttachmentsToFolder() {
    if (attachmentBusy) return;
    attachmentBusy = true;
    attachmentError = '';
    attachmentMigrateMessage = '';
    try {
      const result = await filesApi.migrateAttachments();
      attachmentMigrateMessage = result.migrated
        ? `Moved ${result.migrated} attachment${result.migrated === 1 ? '' : 's'} into this folder${result.skipped ? ` (${result.skipped} already here)` : ''}. Re-scan the folder to see them in Files.`
        : 'Nothing to move — all attachments are already in this folder.';
    } catch (error) {
      attachmentError = error instanceof Error ? error.message : String(error);
    } finally {
      attachmentBusy = false;
    }
  }

  async function browseForAttachmentFolder() {
    if (attachmentBusy) return;
    attachmentBusy = true;
    attachmentError = '';
    try {
      const pickedPath = await session.pickDirectory(attachmentPathDraft);
      if (pickedPath) attachmentPathDraft = pickedPath;
    } catch (error) {
      attachmentError = error instanceof Error ? error.message : String(error);
    } finally {
      attachmentBusy = false;
    }
  }

  // Folder roles (FILES → Folders). Reuses the suite folder-role manager so each
  // root can be assigned to Files or a module (adopt/release) from Settings.
  let showRoleManager = false;
  let roleSources: SourceInfo[] = [];
  let roleSourcesLoaded = false;
  let rescanBusyId: string | null = null;
  let rescanError = '';
  let rescanMessage = '';

  async function loadRoleSources(force = false): Promise<void> {
    if (roleSourcesLoaded && !force) return;
    roleSources = await filesApi.listSources();
    roleSourcesLoaded = true;
    if (get(suiteModules).length === 0) {
      try {
        suiteModules.set(await suiteApi.listModules());
      } catch {
        // The role dropdown falls back to slugs if the registry is unavailable.
      }
    }
  }

  function moduleLabelForRole(role: string): string {
    if (role === 'base' || role === 'files') return 'Files';
    return get(suiteModules).find((module) => module.slug === role)?.name ?? role;
  }

  function openRoleManager() {
    void loadRoleSources(true);
    showRoleManager = true;
  }

  function roleFoldersSaved(event: CustomEvent<FolderBatchResult>) {
    roleSources = event.detail.sources;
    showRoleManager = false;
    void loadRoleSources(true); // refetch so per-folder counts are populated
    void session.reloadFolders();
    session.refreshImages();
  }

  // Re-scan one folder through its owning module (Files → base scan, a module →
  // its own sync). A module sync runs in the background as a tool, so hand it to
  // the existing tool poller; a base scan finishes synchronously.
  async function rescanRoleFolder(source: SourceInfo) {
    if (rescanBusyId) return;
    rescanBusyId = source.source_id;
    rescanError = '';
    rescanMessage = '';
    try {
      const result = await suiteApi.rescanFolder(source.source_id);
      const toolId = result.module?.active_tool_id;
      if (typeof toolId === 'string' && toolId) {
        session.startToolPolling(source.role, toolId);
        rescanMessage = `Re-scan started for ${source.display_name} — running in the background.`;
      } else {
        rescanMessage = `Re-scanned ${source.display_name}.`;
        session.refreshImages();
      }
    } catch (error) {
      rescanError = error instanceof Error ? error.message : String(error);
    } finally {
      rescanBusyId = null;
    }
  }

  // Remove = the suite "forget": stop tracking the folder and drop its index
  // rows, but never touch the media or sidecars on disk. Works for any role.
  async function removeRoleFolder(source: SourceInfo) {
    if (rescanBusyId) return;
    rescanError = '';
    rescanMessage = '';
    try {
      const preview = await suiteApi.previewFolderForget(source.source_id);
      const indexed = preview.base_files + preview.module_files;
      const confirmed = window.confirm(
        `Remove “${preview.display_name}” from ${SUITE_NAME}?\n\n` +
        `${indexed} indexed entries will be dropped. The folder itself and ` +
        `${preview.sidecars_preserved} sidecars stay on disk — nothing is deleted.`
      );
      if (!confirmed) return;
      const result = await suiteApi.applyFolderChanges([
        {
          source_id: source.source_id,
          path: source.path,
          display_name: source.display_name,
          role: source.role === 'base' ? 'files' : source.role,
          visible: source.visible,
          forget: true,
        },
      ]);
      roleSources = result.sources;
      rescanMessage = `Removed ${source.display_name}. Your files were not touched.`;
      void loadRoleSources(true); // refetch so remaining counts are populated
      void session.reloadFolders();
      session.refreshImages();
    } catch (error) {
      rescanError = error instanceof Error ? error.message : String(error);
    }
  }

  let editingSource: SourceInfo | null = null;
  let editPath = '';
  let editError = '';
  let editBusy = false;
  let editBrowsing = false;
  let editDialog: HTMLDialogElement;
  let editInput: HTMLInputElement;
  let editReturnFocus: HTMLElement | null = null;
  let editGeneration = 0;
  let editComposing = false;

  async function editRoleFolder(source: SourceInfo) {
    if (rescanBusyId || editBusy) return;
    editReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const menu = editReturnFocus?.closest('details');
    if (menu) {
      editReturnFocus = menu.querySelector('summary');
      menu.removeAttribute('open');
    }
    editingSource = source;
    editPath = source.path;
    editError = '';
    editComposing = false;
    ++editGeneration;
    await tick();
    editDialog.showModal();
    editInput.focus();
    editInput.select();
  }

  function closePathEditor() {
    if (editBusy || editBrowsing) return;
    ++editGeneration;
    editDialog?.close();
    editingSource = null;
    void tick().then(() => { if (editReturnFocus?.isConnected) editReturnFocus.focus(); });
  }

  async function browseEditPath() {
    if (editBusy || editBrowsing) return;
    const generation = editGeneration;
    editBrowsing = true;
    await tick();
    editDialog.focus();
    try {
      const path = await session.pickDirectory(editPath);
      if (generation === editGeneration && path) { editPath = path; editError = ''; }
    } catch (error) {
      if (generation === editGeneration) editError = error instanceof Error ? error.message : String(error);
    } finally {
      if (generation === editGeneration) editBrowsing = false;
    }
  }

  async function saveEditPath() {
    if (!editingSource || editBusy || editBrowsing || editComposing || !editPath.trim()) return;
    const source = editingSource;
    const generation = editGeneration;
    editBusy = true;
    editError = '';
    await tick();
    editDialog.focus();
    try {
      const result = await suiteApi.relocateFolder(source.source_id, editPath.trim());
      if (generation !== editGeneration) return;
      roleSources = roleSources.map(row => row.source_id === source.source_id
        ? { ...row, source_id: result.source_id, path: editPath.trim() } : row);
      rescanMessage = `Updated ${source.display_name}. Files were not moved.`;
      session.refreshImages();
      void session.reloadFolders();
      void loadRoleSources(true).catch(() => { rescanError = 'The folder was updated. Reopen Folders to refresh its counts.'; });
      editBusy = false;
      closePathEditor();
    } catch (error) {
      if (generation === editGeneration) editError = error instanceof Error ? error.message : String(error);
    } finally {
      if (generation === editGeneration) editBusy = false;
    }
  }

  function editKeydown(event: KeyboardEvent) {
    event.stopPropagation();
    if (event.key !== 'Tab') return;
    const controls = Array.from(editDialog.querySelectorAll<HTMLElement>('input:not([disabled]), button:not([disabled])'));
    if (!controls.length) { event.preventDefault(); return; }
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }

  onDestroy(() => { ++editGeneration; editDialog?.close(); });

</script>

{#if !query}
{#if selectedSection === 'roots'}
          <div class="mx-auto max-w-3xl space-y-4">
            <section id="setting-folder-roles" class="overflow-visible rounded-xl border border-[#292938] bg-[#111118]">
              <div class="border-b border-[#242432] p-4">
                <div class="flex items-center justify-between gap-4">
                  <div class="flex items-center gap-2"><h4 class="text-sm font-semibold text-gray-200">Folders</h4><span class="rounded-full bg-cyan-500/10 px-2.5 py-1 text-[11px] text-cyan-300">{roleSources.length}</span></div>
                  <button class="rounded-lg border border-[#2a2a3a] bg-[#1e1e2e] px-3 py-1.5 text-xs text-gray-300 transition-colors hover:border-cyan-500/50 hover:text-white" type="button" on:click={openRoleManager}>Manage folders…</button>
                </div>
                <p class="mt-1 text-xs leading-relaxed text-gray-500">{#if $settingsStyle === 'modern'}Add folders and choose which module uses them.{:else}Folders registered with Files. Each is owned by Files or handed to a module; use <span class="text-gray-400">Manage folders…</span> to add, assign a role, rename, or hide one.{/if}</p>
                {#if rescanMessage}<p class="mt-2 text-xs text-green-400">{rescanMessage}</p>{/if}
                {#if rescanError}<p class="mt-2 text-xs text-red-400">{rescanError}</p>{/if}
              </div>
              <div class="space-y-2 p-3">
                {#if !roleSourcesLoaded}<div class="rounded-xl border border-dashed border-[#303040] px-4 py-8 text-center text-sm text-gray-500">Loading folders…</div>{:else if roleSources.length === 0}<div class="rounded-xl border border-dashed border-[#303040] px-4 py-8 text-center text-sm text-gray-500">No folders registered yet. Use Manage folders to add one.</div>{/if}
                {#each roleSources as source (source.source_id)}
                  <div class="flex items-center gap-3 rounded-xl border border-white/5 bg-[#0d0d13] px-3 py-2.5 transition-colors hover:border-cyan-300/15">
                    <span class="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-cyan-500/10 text-cyan-300"><svg class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d={iconPath('folder')} /></svg></span>
                    <div class="min-w-0 flex-1"><div class="flex items-baseline gap-2"><span class="truncate text-sm font-semibold text-gray-200">{source.display_name}</span><span class="shrink-0 rounded-full bg-cyan-500/10 px-2 py-0.5 text-[10px] text-cyan-300">{moduleLabelForRole(source.role)}</span><span class="shrink-0 rounded-full bg-white/[0.04] px-2 py-0.5 text-[10px] text-gray-500">{(source.entry_count ?? 0).toLocaleString()} items</span>{#if !source.visible}<span class="shrink-0 text-[10px] text-gray-600">hidden</span>{/if}</div><div class="mt-0.5 truncate text-xs text-gray-600" title={source.path}>{source.path}</div></div>
                    <button class="shrink-0 rounded-lg border border-[#2a2a3a] px-3 py-1.5 text-xs text-gray-300 transition-colors hover:border-cyan-500/50 hover:text-white disabled:opacity-40" type="button" title="Re-index this folder" disabled={rescanBusyId !== null || toolRunning} on:click={() => rescanRoleFolder(source)}>{rescanBusyId === source.source_id ? 'Re-scanning…' : 'Re-scan'}</button>
                    <details class="group relative shrink-0">
                      <summary class="grid h-8 w-8 cursor-pointer list-none place-items-center rounded-lg text-gray-500 hover:bg-white/5 hover:text-gray-200" title="Folder actions" aria-label="Folder actions">•••</summary>
                      <div class="absolute right-0 top-9 z-20 w-40 overflow-hidden rounded-lg border border-[#303040] bg-[#191920] p-1 shadow-xl shadow-black/50">
                        <button class="w-full rounded-md px-3 py-2 text-left text-xs text-cyan-200 hover:bg-cyan-500/10" type="button" disabled={toolRunning || rescanBusyId !== null} on:click={() => editRoleFolder(source)}>Edit path…</button>
                        <button class="w-full rounded-md px-3 py-2 text-left text-xs text-red-300 hover:bg-red-500/10" type="button" on:click={() => removeRoleFolder(source)}>Remove…</button>
                      </div>
                    </details>
                  </div>
                {/each}
              </div>
            </section>
          </div>
{:else if selectedSection === 'files'}
          <div class="mx-auto max-w-3xl space-y-4">
            <section id="setting-attachment-location" class="rounded-xl border border-[#292938] bg-[#111118] p-4">
              <div class="flex items-start gap-3">
                <span class="mt-0.5 grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-purple-500/10 text-purple-300"><svg class="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d={iconPath('image')} /></svg></span>
                <div class="min-w-0 flex-1">
                  <h3 class="text-sm font-semibold text-gray-100">Attachment storage</h3>
                  <p class="mt-0.5 text-xs leading-relaxed text-gray-500">Where images and videos you attach to a file's origin note are saved. Existing attachments stay where they are — this changes only where new ones go.</p>
                  {#if attachmentStore}
                    <div class="mt-3 flex divide-x divide-[#303040] overflow-hidden rounded-lg border border-[#303040]">
                      <button class={compactSegmentClass(attachmentModeView === 'managed')} type="button" on:click={() => (attachmentModeView = 'managed')} disabled={attachmentBusy}>{$settingsStyle === 'modern' ? 'Default' : 'Managed'}</button>
                      <button class={compactSegmentClass(attachmentModeView === 'folder')} type="button" on:click={() => (attachmentModeView = 'folder')} disabled={attachmentBusy}>Choose a folder</button>
                    </div>

                    {#if attachmentModeView === 'managed'}
                      <div class="mt-3 rounded-lg border border-[#2a2a3a] bg-[#0d0d14] px-3 py-2.5">
                        <p class="text-xs leading-relaxed text-gray-400">Stored with {SUITE_NAME}'s own data — hidden from browsing, de-duplicated, and covered by the app backup. Recommended.</p>
                        <div class="mt-1.5 break-all font-mono text-[11px] text-gray-600">{attachmentStore.default}\.keivotos\attachments</div>
                      </div>
                      {#if attachmentStore.mode !== 'managed'}
                        <div class="mt-3 flex items-center gap-2">
                          <button type="button" class="rounded-lg bg-purple-500/20 px-3 py-1.5 text-xs font-semibold text-purple-100 transition-colors hover:bg-purple-500/30 disabled:opacity-40" on:click={useManagedAttachmentStore} disabled={attachmentBusy}>Use managed storage</button>
                          <span class="text-[11px] text-gray-500">Currently saving to a folder.</span>
                        </div>
                      {:else}
                        <p class="mt-2 text-[11px] text-green-400">Managed storage is active.</p>
                      {/if}
                    {:else}
                      <p class="mt-3 text-xs leading-relaxed text-gray-500">Pick a folder inside your library. New attachments are saved there as browsable files (under an <span class="font-mono text-gray-400">Attachments</span> subfolder), so they show up in Files.</p>
                      <div class="mt-2">
                        <PathAutocomplete bind:value={attachmentPathDraft} disabled={attachmentBusy} on:submit={(event) => saveAttachmentFolder(event.detail)} />
                      </div>
                      <div class="mt-2 flex flex-wrap items-center gap-2">
                        <button type="button" class="rounded-lg bg-purple-500/20 px-3 py-1.5 text-xs font-semibold text-purple-100 transition-colors hover:bg-purple-500/30 disabled:opacity-40" on:click={() => saveAttachmentFolder()} disabled={attachmentBusy || !attachmentPathDraft.trim()}>Save folder</button>
                        <button type="button" class="rounded-lg border border-[#2a2a3a] px-3 py-1.5 text-xs text-gray-400 transition-colors hover:text-white disabled:opacity-40" on:click={browseForAttachmentFolder} disabled={attachmentBusy}>Browse…</button>
                        {#if attachmentStore.mode === 'folder'}<span class="text-[11px] text-green-400">Saving to this folder now.</span>{/if}
                      </div>
                      {#if attachmentStore.mode === 'folder'}
                        <div class="mt-3 rounded-lg border border-[#2a2a3a] bg-[#0d0d14] px-3 py-2.5">
                          <p class="text-xs leading-relaxed text-gray-500">Attachments you added while on <span class="text-gray-400">Managed</span> stay hidden. Move them into this folder so they show up in Files too.</p>
                          <div class="mt-2 flex flex-wrap items-center gap-2">
                            <button type="button" class="rounded-lg border border-[#2a2a3a] bg-[#1e1e2e] px-3 py-1.5 text-xs text-gray-300 transition-colors hover:border-purple-500/50 hover:text-white disabled:opacity-40" on:click={migrateAttachmentsToFolder} disabled={attachmentBusy}>Move existing attachments here</button>
                          </div>
                          {#if attachmentMigrateMessage}<p class="mt-2 text-[11px] text-green-400">{attachmentMigrateMessage}</p>{/if}
                        </div>
                      {/if}
                    {/if}
                  {:else}
                    <p class="mt-3 text-xs text-gray-600">Loading…</p>
                  {/if}
                  {#if attachmentError}<p class="mt-2 text-[11px] text-red-300">{attachmentError}</p>{/if}
                </div>
              </div>
            </section>
          </div>

{/if}
{/if}

<div class="contents" use:session.portal>
  <dialog bind:this={editDialog} aria-labelledby="edit-folder-path-title" tabindex="-1"
    class="m-auto w-[min(560px,92vw)] rounded-xl border border-[#303040] bg-[#14141c] p-5 text-gray-200 shadow-2xl"
    on:click|stopPropagation
    on:cancel={(event) => { event.preventDefault(); closePathEditor(); }}
    on:keydown={editKeydown}>
    <form on:submit|preventDefault={saveEditPath}>
      <h3 id="edit-folder-path-title" class="text-sm font-semibold">Edit folder path</h3>
      <p class="mt-2 text-xs text-gray-400">Choose the current location of {editingSource?.display_name ?? 'this folder'}. Your files will stay where they are.</p>
      <label class="mt-4 block text-xs text-gray-400" for="edit-folder-path">Folder path</label>
      <div class="mt-2 flex gap-2">
        <input id="edit-folder-path" bind:this={editInput} bind:value={editPath} disabled={editBusy || editBrowsing}
          class="min-w-0 flex-1 rounded border border-[#303040] bg-[#0d0d13] px-3 py-2 text-xs outline-none focus:border-cyan-400"
          autocomplete="off" spellcheck="false" on:input={() => { editError = ''; }}
          on:compositionstart={() => { editComposing = true; }} on:compositionend={() => { editComposing = false; }} />
        <button type="button" class="rounded border border-[#303040] px-3 py-2 text-xs disabled:opacity-40" disabled={editBusy || editBrowsing} on:click={browseEditPath}>{editBrowsing ? 'Browsing…' : 'Browse…'}</button>
      </div>
      {#if editError}<p role="alert" class="mt-3 text-xs text-red-300">{editError}</p>{/if}
      <div class="mt-5 flex justify-end gap-2">
        <button type="button" class="rounded px-3 py-2 text-xs hover:bg-white/5 disabled:opacity-40" disabled={editBusy || editBrowsing} on:click={closePathEditor}>Cancel</button>
        <button type="submit" class="rounded bg-cyan-500/15 px-4 py-2 text-xs text-cyan-200 disabled:opacity-40" disabled={editBusy || editBrowsing || !editPath.trim() || toolRunning}>{editBusy ? 'Saving…' : 'Save'}</button>
      </div>
    </form>
  </dialog>
  {#if showRoleManager}
    <ManageFoldersDialog
      sources={roleSources}
      modules={$suiteModules}
      on:saved={roleFoldersSaved}
      on:close={() => (showRoleManager = false)}
    />
  {/if}

</div>

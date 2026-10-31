<script lang="ts">
  import {onMount, onDestroy, tick} from 'svelte';
  import AppDrawer from './AppDrawer.svelte';
  import {downloadsApi, type DownloadHistoryItem, type DownloadControlState} from '../lib/downloadsApi';
  import {notify} from '../lib/notifications';

  let items: DownloadHistoryItem[] = [];
  let cursor: string | null = null;
  let drawer = false;
  let live = true;
  let loading = false;
  let loadingMore = false;
  let paused: DownloadControlState = {paused: false, revision: 0};
  let changingPause = false;
  let pendingPause: {value: boolean; key: string} | null = null;
  let busyId: string | null = null;
  let menu: DownloadHistoryItem | null = null;
  let menuLeft = 0;
  let menuTop = 0;
  let menuButton: HTMLButtonElement | null = null;
  let deleteButton: HTMLButtonElement;
  let scroll: HTMLElement;
  let dialog: HTMLDialogElement;
  let keepButton: HTMLButtonElement;
  let confirmation: {item: DownloadHistoryItem; key: string; count: number} | null = null;
  let failedThumbs = new Set<string>();
  let controller: AbortController | null = null;
  let timer: ReturnType<typeof setInterval>;
  const deleteKeys = new Map<string, {key: string; version: string | null}>();

  function ordered(values: DownloadHistoryItem[]) {
    return values.sort((a, b) => (b.date_at ?? '').localeCompare(a.date_at ?? '') || b.id.localeCompare(a.id));
  }
  function dayLabel(value: string | null) {
    if (!value) return 'Unknown date';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return 'Unknown date';
    const parts = new Intl.DateTimeFormat(undefined, {year: 'numeric', month: '2-digit', day: '2-digit'}).formatToParts(date);
    return ['year', 'month', 'day'].map(type => parts.find(part => part.type === type)?.value ?? '').join('-');
  }
  $: days = (() => {
    const groups = new Map<string, Map<string, {owner: string; name: string; rows: DownloadHistoryItem[]}>>();
    for (const item of items) {
      const day = dayLabel(item.date_at);
      if (!groups.has(day)) groups.set(day, new Map());
      const modules = groups.get(day)!;
      if (!modules.has(item.owner)) modules.set(item.owner, {owner: item.owner, name: item.module_name, rows: []});
      modules.get(item.owner)!.rows.push(item);
    }
    return [...groups].map(([date, modules]) => ({date, modules: [...modules.values()].sort((a, b) => a.name.localeCompare(b.name))}));
  })();

  function applyControl(value: DownloadControlState) {
    if (value.revision >= paused.revision) paused = value;
  }
  async function refresh() {
    if (!live || loading || loadingMore) return;
    loading = true;
    const request = new AbortController();
    controller?.abort();
    controller = request;
    try {
      const [page, control] = await Promise.all([downloadsApi.history({}, request.signal), downloadsApi.control(request.signal)]);
      const first = new Map(page.items.map(item => [item.id, item]));
      const oldActive = items.filter(item => (item.state === 'working' || item.state === 'queued') && !first.has(item.id));
      const changed = await Promise.all(oldActive.map(async item => [item.id, await downloadsApi.item(item.id, request.signal)] as const));
      if (!live || request.signal.aborted) return;
      const values = new Map(items.map(item => [item.id, item]));
      for (const [id, item] of changed) {if (item) values.set(id, item); else values.delete(id);}
      for (const item of page.items) values.set(item.id, item);
      if (items.length <= page.items.length) cursor = page.next_cursor;
      items = ordered([...values.values()]);
      applyControl(control);
      if (menu && !items.some(item => item.id === menu?.id && item.can_delete)) closeMenu(false);
    } catch (error) {
      if (!request.signal.aborted && live) console.error('Downloads refresh failed:', error);
    } finally {if (controller === request) {loading = false; void tick().then(loadVisible);}}
  }
  async function more() {
    if (!live || loadingMore || loading || !cursor) return;
    loadingMore = true;
    const request = new AbortController();
    controller = request;
    try {
      const page = await downloadsApi.history({after: cursor}, request.signal);
      if (!live || request.signal.aborted) return;
      const values = new Map(items.map(item => [item.id, item]));
      for (const item of page.items) values.set(item.id, item);
      items = ordered([...values.values()]);
      cursor = page.next_cursor;
    } catch (error) {if (!request.signal.aborted && live) console.error('Downloads paging failed:', error);}
    finally {loadingMore = false; if (live) void tick().then(loadVisible);}
  }
  function loadVisible() {
    if (live && scroll && scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 400) void more();
  }
  function onScroll() {
    closeMenu(false);
    loadVisible();
  }
  function closeMenu(focus = true) {
    menu = null;
    if (focus && menuButton?.isConnected) menuButton.focus();
  }
  async function openMenu(event: MouseEvent, item: DownloadHistoryItem) {
    const button = event.currentTarget as HTMLButtonElement;
    if (menu?.id === item.id) {closeMenu(); return;}
    menuButton = button;
    const rect = button.getBoundingClientRect();
    menuLeft = Math.max(8, Math.min(window.innerWidth - 148, rect.right - 140));
    menuTop = window.innerHeight - rect.bottom > 56 ? rect.bottom + 4 : rect.top - 48;
    menu = item;
    await tick();
    deleteButton?.focus();
  }
  function onPointer(event: PointerEvent) {
    if (menu && !(event.target instanceof Element && event.target.closest('[data-download-menu]'))) closeMenu(false);
  }
  function onKey(event: KeyboardEvent) {
    if (!menu) return;
    if (event.key === 'Escape') {event.preventDefault(); closeMenu();}
    else if (event.key === 'Tab') closeMenu(false);
    else if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {event.preventDefault(); deleteButton?.focus();}
  }
  async function changePause() {
    if (changingPause) return;
    changingPause = true;
    const action = pendingPause ?? {value: !paused.paused, key: crypto.randomUUID()};
    pendingPause = action;
    try {
      const result = await downloadsApi.pause(action.value, action.key);
      if (!live) return;
      applyControl(result);
      pendingPause = null;
      void refresh();
    } catch (error) {
      console.error('Downloads pause action failed:', error);
      notify('Could not change the download pause state.', {tone: 'error'});
    } finally {if (live) changingPause = false;}
  }
  async function remove(item: DownloadHistoryItem, key?: string, confirmed = false) {
    if (busyId) return;
    closeMenu(false);
    busyId = item.id;
    const previous = deleteKeys.get(item.id);
    const requestId = key ?? (previous?.version === item.control_version ? previous.key : crypto.randomUUID());
    deleteKeys.set(item.id, {key: requestId, version: item.control_version});
    try {
      const result = await downloadsApi.remove(item, requestId, confirmed);
      if (!live) return;
      if (result.requires_confirmation) {
        confirmation = {item, key: requestId, count: result.file_count};
        await tick();
        if (!dialog.open) dialog.showModal();
        keepButton?.focus();
      } else if (result.deleted) {
        items = items.filter(row => row.id !== item.id);
        deleteKeys.delete(item.id);
        confirmation = null;
        dialog?.close();
        scroll?.focus({preventScroll: true});
      }
    } catch (error) {
      console.error('Downloads delete action failed:', error);
      notify('The unfinished download could not be deleted.', {tone: 'error'});
      void refresh();
    } finally {if (live) busyId = null;}
  }
  function keepFiles() {
    if (confirmation) deleteKeys.delete(confirmation.item.id);
    confirmation = null;
    dialog.close();
    scroll?.focus({preventScroll: true});
    void refresh();
  }
  function failThumbnail(url: string) {failedThumbs = new Set([...failedThumbs, url]);}
  onMount(() => {
    void refresh();
    timer = setInterval(() => {if (document.visibilityState !== 'hidden') void refresh();}, 1000);
  });
  onDestroy(() => {
    live = false;
    controller?.abort();
    clearInterval(timer);
    dialog?.close();
  });
</script>

<svelte:window on:pointerdown={onPointer} on:keydown={onKey} on:resize={() => closeMenu(false)} />

<div class="downloads-view flex h-full min-h-0 flex-col">
  <header class="flex h-[53px] shrink-0 items-center gap-3 border-b border-[#292937] px-4">
    <button type="button" class="menu-button" aria-label="Open Keivotos menu" title="Keivotos menu" on:click={() => drawer = true}>
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true"><path stroke-width="1.8" stroke-linecap="round" d="M5 6h14M5 12h14M5 18h14"/></svg>
    </button>
    <h1 class="min-w-0 flex-1 text-base font-semibold text-gray-100">Downloads</h1>
    <button class="pause-button" type="button" on:click={changePause} disabled={changingPause || !!busyId} aria-pressed={paused.paused}>
      <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        {#if paused.paused}<path d="m8 5 11 7-11 7z"/>{:else}<path d="M6 5h4v14H6zm8 0h4v14h-4z"/>{/if}
      </svg>
      <span>{paused.paused ? 'Resume' : 'Pause all'}</span>
    </button>
  </header>
  <!-- svelte-ignore a11y_no_noninteractive_tabindex (The scrollable history must be keyboard reachable when all rows are completed.) -->
  <main class="downloads-scroll min-h-0 flex-1 overflow-y-auto px-4 py-3 sm:px-6" aria-label="Downloads" tabindex="0" bind:this={scroll} on:scroll={onScroll}>
    {#each days as day (day.date)}
      <section class="download-day" aria-label={day.date}>
        <h2>{day.date}</h2>
        {#each day.modules as group (group.owner)}
          <section class="download-group" aria-label={group.name}>
            <hr />
            <h3>{group.name}</h3>
            <ul>
              {#each group.rows as item (item.id)}
                <li class="download-row" class:busy={busyId === item.id} data-download-id={item.id} data-state={item.state}>
                  <div class="download-thumbnail">
                    {#if item.thumbnail_url && !failedThumbs.has(item.thumbnail_url)}
                      <img src={item.thumbnail_url} alt="" loading="lazy" on:error={() => item.thumbnail_url && failThumbnail(item.thumbnail_url)} />
                    {:else}
                      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true"><path stroke-width="1.4" d="M4 4h16v16H4zM4 16l5-5 4 4 3-3 4 4"/><circle cx="15" cy="8" r="1.2"/></svg>
                    {/if}
                  </div>
                  <div class="download-body">
                    <div class="download-label">
                      <span class="download-name" title={item.name}>{item.name}</span>
                      {#if item.progress !== null}<span class="download-percent">{Math.floor(item.progress)}%</span>{/if}
                    </div>
                    <div class="download-track" role="progressbar" aria-label={item.name} aria-valuemin="0" aria-valuemax="100" aria-valuenow={item.progress ?? undefined} aria-valuetext={item.progress === null ? 'Progress unavailable' : undefined}>
                      <div class="download-fill" class:indeterminate={item.progress === null && item.state === 'working'} style:width={item.progress === null ? '32%' : String(item.progress) + '%'}></div>
                    </div>
                  </div>
                  {#if item.can_delete && item.control_version}
                    <button class="download-more" type="button" data-download-menu aria-label={'Actions for ' + item.name} title="Actions" aria-haspopup="menu" aria-expanded={menu?.id === item.id} disabled={!!busyId || changingPause} on:click={(event) => openMenu(event, item)}>
                      <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg>
                    </button>
                  {/if}
                </li>
              {/each}
            </ul>
          </section>
        {/each}
      </section>
    {/each}
  </main>
</div>

{#if menu}
  <div class="download-actions" role="menu" aria-label={'Actions for ' + menu.name} data-download-menu style:left={menuLeft + 'px'} style:top={menuTop + 'px'}>
    <button type="button" role="menuitem" bind:this={deleteButton} on:click={() => menu && remove(menu)}>Delete</button>
  </div>
{/if}

<dialog class="delete-confirmation" bind:this={dialog} on:cancel|preventDefault={keepFiles} aria-labelledby="download-delete-title">
  {#if confirmation}
    <h2 id="download-delete-title">Delete unfinished download?</h2>
    <p>{confirmation.count} unfinished files.</p>
    <div class="confirmation-actions">
      <button type="button" bind:this={keepButton} on:click={keepFiles}>Keep files</button>
      <button type="button" class="destructive" disabled={!!busyId} on:click={() => confirmation && remove(confirmation.item, confirmation.key, true)}>Delete</button>
    </div>
  {/if}
</dialog>

{#if drawer}<AppDrawer on:close={() => drawer = false} />{/if}

<style>
  .menu-button,.download-more { display:grid; place-items:center; width:32px; height:32px; border-radius:6px; color:#9ca3af; flex-shrink:0; }
  .menu-button:hover,.download-more:hover { color:#ede9fe; background:rgba(167,139,250,.09); }
  button:focus-visible { outline:2px solid var(--accent,#a78bfa); outline-offset:2px; }
  button:disabled { cursor:default; opacity:.5; }
  .pause-button { display:flex; align-items:center; gap:7px; border:1px solid #343442; border-radius:6px; padding:6px 10px; font-size:12px; color:#d1d5db; }
  .pause-button:hover:not(:disabled) { background:#1c1c28; border-color:#555168; }
  .downloads-scroll:focus { outline:none; }
  .download-day { margin:0 0 24px; }
  .download-day>h2 { margin:0 0 14px; font-size:12px; font-weight:500; color:#9ca3af; }
  .download-group { margin:0 0 18px; }
  .download-group hr { border:0; border-top:1px solid #2b2b38; }
  .download-group h3 { margin:7px 0 5px; font-size:12px; font-weight:500; color:#c4bed4; }
  .download-row { display:flex; align-items:center; gap:10px; min-width:0; height:50px; padding:5px 0; }
  .download-row.busy { opacity:.6; }
  .download-thumbnail { display:grid; place-items:center; height:38px; width:38px; flex-shrink:0; overflow:hidden; border-radius:4px; background:#1c1c27; color:#626276; }
  .download-thumbnail img { width:100%; height:100%; object-fit:cover; }
  .download-body { min-width:0; flex:1; }
  .download-label { display:flex; align-items:center; gap:10px; min-width:0; }
  .download-percent { flex-shrink:0; color:#9ca3af; font-size:11px; line-height:19px; font-variant-numeric:tabular-nums; }
  .download-name { min-width:0; flex:1; display:block; font-size:13px; line-height:19px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; color:#dedbe8; }
  .download-track { position:relative; overflow:hidden; height:3px; margin-top:5px; border-radius:2px; background:#282837; }
  .download-fill { height:100%; border-radius:inherit; background:var(--accent,#a78bfa); transition:width 180ms ease; }
  .download-fill.indeterminate { animation:download-pulse 1.4s ease-in-out infinite; }
  .download-actions { position:fixed; width:140px; padding:4px; z-index:95; border:1px solid #373748; border-radius:7px; background:#1b1b27; box-shadow:0 7px 20px #0007; }
  .download-actions button { width:100%; padding:7px 10px; text-align:left; color:#fca5a5; border-radius:4px; font-size:12px; }
  .download-actions button:hover,.download-actions button:focus-visible { background:#ef44441a; }
  .delete-confirmation { width:min(340px,calc(100vw - 32px)); padding:20px; color:#e5e7eb; background:#171721; border:1px solid #3b394b; border-radius:10px; }
  .delete-confirmation::backdrop { background:#0009; }
  .delete-confirmation h2 { font-weight:600; font-size:14px; }
  .delete-confirmation p { margin-top:9px; font-size:13px; color:#aaa6b8; }
  .confirmation-actions { display:flex; justify-content:flex-end; gap:8px; margin-top:20px; }
  .confirmation-actions button { padding:6px 10px; border:1px solid #434054; border-radius:6px; font-size:12px; }
  .confirmation-actions .destructive { color:#fca5a5; border-color:#9f4141; }
  @keyframes download-pulse { 0%,100% {transform:translateX(-110%);} 50% {transform:translateX(310%);} }
  :global(html[data-motion='reduced']) .download-fill { animation:none; transition:none; }
  @media (prefers-reduced-motion:reduce) { :global(html:not([data-motion='full'])) .download-fill { animation:none; transition:none; } }
</style>

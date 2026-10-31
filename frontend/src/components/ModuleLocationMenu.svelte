<script lang="ts">
  import { createEventDispatcher, onDestroy, onMount, tick } from 'svelte';
  import { filesApi } from '../lib/filesApi';
  import { showInFiles } from '../modules/registry';

  export let x: number;
  export let y: number;
  export let label: string;
  export let sourceId: string | null;
  export let relativePath: string | null;
  export let anchor: HTMLElement;
  export let unavailableReason = '';
  export let beforeShow: (() => boolean | Promise<boolean>) | undefined = undefined;
  export let actions: {label:string;disabled?:boolean;reason?:string;onSelect:()=>void}[] = [];

  const dispatch = createEventDispatcher<{ close: void }>();
  let menuEl: HTMLDivElement;
  let showButton: HTMLButtonElement;

  let left = x;
  let top = y;
  let checking = true;
  let reason = unavailableReason;
  let actionError = '';
  let revealing = false;
  let alive = true;
  $: available = !checking && !reason;

  onMount(async () => {
    await tick();
    const bounds = menuEl.getBoundingClientRect();
    left = Math.max(8, Math.min(x, window.innerWidth - bounds.width - 8));
    top = Math.max(8, Math.min(y, window.innerHeight - bounds.height - 8));
    menuEl.focus();
    if (!reason) {
      try { reason = await filesApi.locationUnavailableReason(sourceId, relativePath) ?? ''; }
      catch (error) { reason = error instanceof Error ? error.message : 'Could not check this location.'; }
    }
    if (!alive) return;
    checking = false;
    await tick();
    if (alive) enabledItems()[0]?.focus();
  });
  onDestroy(() => { alive = false; });

  function close(restoreFocus = false): void {
    const origin = anchor;
    dispatch('close');
    if (restoreFocus) queueMicrotask(() => origin?.focus({ preventScroll: true }));
  }

  async function chooseShow(): Promise<void> {
    if (!available || !sourceId || relativePath === null) return;
    const destination = { sourceId, relativePath };
    const guard = beforeShow;
    const origin = anchor;
    close();
    if (guard && !await guard()) {
      origin?.focus({ preventScroll: true });
      return;
    }
    showInFiles(destination.sourceId, destination.relativePath);
  }

  async function chooseReveal(): Promise<void> {
    if (!available || !sourceId || relativePath === null || revealing) return;
    revealing = true;
    actionError = '';
    try {
      await filesApi.revealFile(sourceId, relativePath);
      close(true);
    } catch (error) {
      actionError = error instanceof Error ? error.message : 'Could not open this location in Explorer.';
    } finally {
      revealing = false;
    }
  }

  function enabledItems(): HTMLButtonElement[] {
    return [...menuEl.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')];
  }

  function chooseAction(action: (typeof actions)[number]): void {
    if (!available || action.disabled) return;
    close();
    action.onSelect();
  }

  function onKey(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      event.preventDefault(); event.stopPropagation(); close(true); return;
    }
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault(); event.stopPropagation();
      const items = enabledItems();
      if (!items.length) return;
      const current = items.findIndex(item => item === document.activeElement);
      if (event.key === 'Home') items[0].focus();
      else if (event.key === 'End') items[items.length - 1].focus();
      else if (current < 0) items[event.key === 'ArrowUp' ? items.length - 1 : 0].focus();
      else items[(current + (event.key === 'ArrowUp' ? -1 : 1) + items.length) % items.length].focus();
    }
  }
</script>

<svelte:window on:click={() => close()} on:contextmenu={() => close()} on:resize={() => close(true)} on:scroll|capture={() => close(true)} />

<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<div
  bind:this={menuEl}
  class="fixed z-[140] w-52 max-w-[calc(100vw-16px)] rounded-lg border border-[#2a2a3a] bg-[#1e1e2e] py-1 shadow-xl"
  style="left: {left}px; top: {top}px;"
  role="menu"
  aria-label={`Location for ${label}`}
  tabindex="-1"
  on:click|stopPropagation
  on:contextmenu|stopPropagation|preventDefault
  on:keydown={onKey}
>
  <div class="truncate px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wide text-gray-500" title={label}>{label}</div>
  <button bind:this={showButton} type="button" role="menuitem" disabled={!available} title={reason || 'Show in Files'}
    class="flex w-full px-3 py-1.5 text-left text-sm text-gray-200 hover:bg-[#2a2a3a] focus:bg-[#2a2a3a] disabled:cursor-not-allowed disabled:opacity-40"
    on:click={chooseShow}>Show in Files</button>
  <button type="button" role="menuitem" disabled={!available || revealing} title={reason || 'Open in Explorer'}
    class="flex w-full px-3 py-1.5 text-left text-sm text-gray-200 hover:bg-[#2a2a3a] focus:bg-[#2a2a3a] disabled:cursor-not-allowed disabled:opacity-40"
    on:click={chooseReveal}>Open in Explorer</button>
  {#each actions as action}
    <button type="button" role="menuitem" disabled={!available || action.disabled} title={action.reason || reason || action.label}
      class="flex w-full px-3 py-1.5 text-left text-sm text-gray-200 hover:bg-[#2a2a3a] focus:bg-[#2a2a3a] disabled:cursor-not-allowed disabled:opacity-40"
      on:click={() => chooseAction(action)}>{action.label}</button>
  {/each}
  {#if checking}<p class="px-3 py-1 text-xs text-gray-400" role="status">Checking local file…</p>
  {:else if reason}<p class="px-3 py-1 text-xs text-amber-200" role="status">{reason}</p>{/if}
  {#if actionError}<p class="px-3 py-1 text-xs text-red-300" role="alert">{actionError}</p>{/if}
</div>

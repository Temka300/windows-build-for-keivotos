<script lang="ts">
  import { onMount } from 'svelte';
  import { fly } from 'svelte/transition';
  import { flip } from 'svelte/animate';
  import { motionPreference } from '../lib/suiteStores';
  import { notifications, notificationPosition, notify, dismissNotification, pauseNotification, resumeNotification } from '../lib/notifications';
  export let openDownloads: ((prepare: () => Promise<void>) => Promise<boolean>) | undefined = undefined;
  export let blocked = false;
  let host: HTMLElement;
  let marker: Comment;
  let systemReduced = false;
  let hovered: string | null = null;
  let focused: string | null = null;
  let navigating = false;
  $: duration = $motionPreference === 'reduced' || ($motionPreference === 'system' && systemReduced) ? 0 : 180;
  function relocate() {
    const fullscreen = document.fullscreenElement;
    if (fullscreen) fullscreen.append(host);
    else marker?.parentNode?.insertBefore(host, marker.nextSibling);
  }
  onMount(() => {
    marker = document.createComment('notification overlay');
    host.before(marker);
    const media = window.matchMedia('(prefers-reduced-motion: reduce)');
    const motion = () => systemReduced = media.matches;
    motion(); relocate();
    media.addEventListener('change', motion); document.addEventListener('fullscreenchange', relocate);
    return () => { media.removeEventListener('change', motion); document.removeEventListener('fullscreenchange', relocate); marker.remove(); };
  });
  function enter(id: string) { hovered = id; pauseNotification(id, 'pointer'); }
  function leave(id: string) { if (hovered === id) hovered = null; resumeNotification(id, 'pointer'); }
  function focus(id: string) { focused = id; pauseNotification(id, 'focus'); }
  function blur(event: FocusEvent, id: string) {
    if (!(event.relatedTarget instanceof Node) || !(event.currentTarget as HTMLElement).contains(event.relatedTarget)) {
      if (focused === id) focused = null;
      resumeNotification(id, 'focus');
    }
  }
  async function goToDownloads() {
    if (blocked || navigating || !openDownloads) return;
    navigating = true;
    try {
      await openDownloads(async () => {
        if (document.fullscreenElement) await document.exitFullscreen();
        // Restore before the old viewer is destroyed, including browsers whose
        // fullscreenchange event is delivered after exitFullscreen resolves.
        relocate();
      });
    } catch (error) {
      console.error('Could not open Downloads:', error);
      notify('Could not open Downloads.', {tone: 'error'});
    } finally { navigating = false; }
  }
  function keys(event: KeyboardEvent, id: string) {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); dismissNotification(id); }
    else if (['Enter', ' ', 'ArrowUp', 'ArrowDown'].includes(event.key)) event.stopPropagation();
  }
</script>

<section bind:this={host} class="notification-overlay" class:populated={$notifications.length > 0} class:top={$notificationPosition.startsWith('top')} class:left={$notificationPosition.endsWith('left')} class:reduced={duration === 0} class:navigating
  style={'--notice-duration:' + duration + 'ms'} aria-label="Notifications" data-position={$notificationPosition}>
  <ol aria-label="Action messages">
    {#each $notifications as notification (notification.id)}
      <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
      <li class="action-notification" class:error={notification.tone === 'error'}
        class:download={notification.destination === 'downloads'} class:expanded={notification.destination === 'downloads' && (hovered === notification.id || focused === notification.id)}
        data-notification-id={notification.id} data-download-notification={notification.destination === 'downloads' ? '' : undefined}
        animate:flip={{ duration }} in:fly={{ x: $notificationPosition.endsWith('left') ? -20 : 20, duration }}
        on:mouseenter={() => enter(notification.id)} on:mouseleave={() => leave(notification.id)}
        on:focusin={() => focus(notification.id)} on:focusout={event => blur(event, notification.id)}
        on:keydown={event => keys(event, notification.id)}>
        <div class="notification-card">
          <div class="notification-copy"><p role="status">{notification.message}</p>
            {#if notification.progress !== undefined}<div class="notification-progress" aria-hidden="true"><span style={'width:' + notification.progress + '%'}></span></div>{/if}
          </div>
          <button class="notification-dismiss" type="button" aria-label="Dismiss notification" on:click={() => dismissNotification(notification.id)}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg></button>
          {#if notification.destination === 'downloads' && openDownloads}
            <div class="notification-action" inert={hovered !== notification.id && focused !== notification.id} aria-hidden={hovered !== notification.id && focused !== notification.id}>
              <div class="notification-action-inner">
                <button class="notification-downloads" type="button" disabled={blocked || navigating} on:click={goToDownloads}>Downloads <span aria-hidden="true">→</span></button>
              </div>
            </div>
          {/if}
        </div>
      </li>
    {/each}
  </ol>
</section>
<style>
  .notification-overlay{position:fixed;z-index:210;right:20px;bottom:20px;width:min(360px,calc(100vw - 32px));pointer-events:none;max-height:calc(100dvh - 32px)}
  .notification-overlay.top{top:20px;bottom:auto}.notification-overlay.left{left:20px;right:auto}
  ol{display:flex;flex-direction:column;gap:8px;list-style:none;margin:-4px;padding:6px;max-height:calc(100dvh - 24px);overflow-y:auto;pointer-events:none}.populated ol{pointer-events:auto}.top ol{flex-direction:column-reverse}
  .action-notification{min-width:0;pointer-events:auto}
  .notification-card{display:grid;grid-template-columns:minmax(0,1fr) 24px;align-items:start;column-gap:12px;padding:12px;border:1px solid color-mix(in srgb,var(--accent) 35%,#303040);border-radius:10px;background:#191923f5;box-shadow:0 8px 24px #0006;color:#e5e7eb;min-width:0;transform-origin:right bottom;transition:transform var(--notice-duration) ease}
  .left .notification-card{transform-origin:left bottom}.top .notification-card{transform-origin:right top}.top.left .notification-card{transform-origin:left top}
  .download.expanded .notification-card{transform:scale(1.015)}
  .notification-copy{min-width:0}p{font-size:13px;line-height:1.5;margin:0;overflow-wrap:anywhere;max-height:18vh;overflow:auto}.error .notification-card{border-color:#f8717180}.error p{color:#fca5a5}
  .notification-dismiss{display:grid;place-items:center;width:24px;height:24px;flex:none;border-radius:5px;color:#aaa}.notification-dismiss:hover{background:#ffffff14;color:#fff}
  button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.8}
  .notification-progress{height:3px;margin-top:8px;background:#ffffff12;border-radius:3px;overflow:hidden}.notification-progress span{display:block;height:100%;background:var(--accent)}
  .notification-action{grid-column:1/-1;display:grid;grid-template-rows:0fr;opacity:0;visibility:hidden;transition:grid-template-rows var(--notice-duration) ease,opacity var(--notice-duration) ease,visibility var(--notice-duration)}
  .notification-action-inner{min-height:0;overflow:hidden}
  .expanded .notification-action{grid-template-rows:1fr;opacity:1;visibility:visible}
  .notification-downloads{display:flex;align-items:center;gap:8px;margin-top:8px;padding:5px 8px;border-radius:5px;border:1px solid #ffffff20;font-size:12px;line-height:16px;color:#ddd8ec;background:#ffffff05}
  .notification-downloads:hover:not(:disabled){color:#fff;border-color:var(--accent);background:#ffffff0d}.notification-downloads:disabled{opacity:.5;cursor:default}
  .reduced .notification-card,.reduced .download.expanded .notification-card{transform:none}
  .navigating{z-index:90}
  @media(max-width:600px){.notification-overlay{right:16px;bottom:16px}.notification-overlay.top{top:16px}.notification-overlay.left{left:16px}}
</style>

<script lang="ts">
  import { onMount } from 'svelte';
  import { fly } from 'svelte/transition';
  import { flip } from 'svelte/animate';
  import { motionPreference } from '../lib/suiteStores';
  import { notifications, notificationPosition, dismissNotification, pauseNotification, resumeNotification } from '../lib/notifications';
  let host: HTMLElement;
  let systemReduced = false;
  $: duration = $motionPreference === 'reduced' || ($motionPreference === 'system' && systemReduced) ? 0 : 180;
  onMount(() => {
    const marker = document.createComment('notification overlay');
    host.before(marker);
    const media = window.matchMedia('(prefers-reduced-motion: reduce)');
    const motion = () => systemReduced = media.matches;
    const relocate = () => {
      const fullscreen = document.fullscreenElement;
      if (fullscreen) fullscreen.append(host);
      else marker.parentNode?.insertBefore(host, marker.nextSibling);
    };
    motion(); relocate();
    media.addEventListener('change', motion); document.addEventListener('fullscreenchange', relocate);
    return () => { media.removeEventListener('change', motion); document.removeEventListener('fullscreenchange', relocate); marker.remove(); };
  });
  function keys(event: KeyboardEvent, id: string) {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); dismissNotification(id); }
    else if (['Enter', ' ', 'ArrowUp', 'ArrowDown'].includes(event.key)) event.stopPropagation();
  }
</script>

<section bind:this={host} class="notification-overlay" class:populated={$notifications.length > 0} class:top={$notificationPosition.startsWith('top')} class:left={$notificationPosition.endsWith('left')} aria-label="Notifications" data-position={$notificationPosition}>
  <ol aria-label="Action messages">
    {#each $notifications as notification (notification.id)}
      <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
      <li class="action-notification" class:error={notification.tone === 'error'} data-notification-id={notification.id}
        animate:flip={{ duration }} in:fly={{ x: $notificationPosition.endsWith('left') ? -20 : 20, duration }}
        on:mouseenter={() => pauseNotification(notification.id, 'pointer')} on:mouseleave={() => resumeNotification(notification.id, 'pointer')}
        on:focusin={() => pauseNotification(notification.id, 'focus')}
        on:focusout={event => { if (!(event.relatedTarget instanceof Node) || !event.currentTarget.contains(event.relatedTarget)) resumeNotification(notification.id, 'focus'); }}
        on:keydown={event => keys(event, notification.id)}>
        <div class="notification-copy"><p role="status">{notification.message}</p>
          {#if notification.progress !== undefined}<div class="notification-progress" aria-hidden="true"><span style={'width:' + notification.progress + '%'}></span></div>{/if}
        </div>
        <button type="button" aria-label="Dismiss notification" on:click={() => dismissNotification(notification.id)}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg></button>
      </li>
    {/each}
  </ol>
</section>
<style>
  .notification-overlay{position:fixed;z-index:210;right:20px;bottom:20px;width:min(360px,calc(100vw - 32px));pointer-events:none;max-height:calc(100dvh - 32px)}
  .notification-overlay.top{top:20px;bottom:auto}.notification-overlay.left{left:20px;right:auto}
  ol{display:flex;flex-direction:column;gap:8px;list-style:none;margin:0;padding:2px;max-height:calc(100dvh - 32px);overflow-y:auto;pointer-events:none}.populated ol{pointer-events:auto}.top ol{flex-direction:column-reverse}
  .action-notification{display:flex;align-items:flex-start;gap:12px;padding:12px;border:1px solid color-mix(in srgb,var(--accent) 35%,#303040);border-radius:10px;background:#191923f5;box-shadow:0 8px 24px #0006;pointer-events:auto;color:#e5e7eb;min-width:0}
  .notification-copy{min-width:0;flex:1}p{font-size:13px;line-height:1.5;margin:0;overflow-wrap:anywhere;max-height:18vh;overflow:auto}.error{border-color:#f8717180}.error p{color:#fca5a5}
  button{display:grid;place-items:center;width:24px;height:24px;flex:none;border-radius:5px;color:#aaa}button:hover{background:#ffffff14;color:#fff}button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.8}
  .notification-progress{height:3px;margin-top:8px;background:#ffffff12;border-radius:3px;overflow:hidden}.notification-progress span{display:block;height:100%;background:var(--accent)}
  @media(max-width:600px){.notification-overlay{right:16px;bottom:16px}.notification-overlay.top{top:16px}.notification-overlay.left{left:16px}}
</style>

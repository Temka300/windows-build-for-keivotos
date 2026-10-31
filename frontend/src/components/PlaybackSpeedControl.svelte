<script lang="ts">
 import {tick} from 'svelte';
 import {PLAYBACK_SPEEDS,type PlaybackSpeed} from '../lib/playbackPreferences';
 export let value:PlaybackSpeed=1;
 export let disabled=false;
 export let open=false;
 export let onChange:(value:PlaybackSpeed)=>void;
 let container:HTMLDivElement,trigger:HTMLButtonElement,picker:HTMLDivElement;
 let left=8,top=8,positioned=false;
 const label=(speed:number)=>speed===1?'1.0x':speed.toString()+'x';
 function close(focus=false){open=false;if(focus)trigger?.focus();}
 function position(){
  if(!open||!picker||!trigger)return;
  const anchor=trigger.getBoundingClientRect(),box=picker.getBoundingClientRect();
  left=Math.max(8,Math.min(innerWidth-box.width-8,anchor.right-box.width));
  const above=anchor.top-8,below=innerHeight-anchor.bottom-8;
  top=Math.max(8,Math.min(innerHeight-box.height-8,
   above>=box.height||above>=below?anchor.top-box.height-8:anchor.bottom+8));
  positioned=true;
 }
 async function show(){positioned=false;await tick();if(!open)return;position();picker?.querySelector<HTMLButtonElement>('[aria-pressed="true"]')?.focus();}
 $: if(open)void show();
 $: if(disabled)open=false;
 function outside(event:PointerEvent){if(open&&event.target instanceof Node&&!container.contains(event.target))close();}
 function keys(event:KeyboardEvent){
  if(!open)return;
  if(event.key==='Escape'){event.preventDefault();event.stopPropagation();close(true);return;}
  const buttons=[...picker.querySelectorAll<HTMLButtonElement>('button')];
  const index=buttons.indexOf(document.activeElement as HTMLButtonElement);
  if(index<0)return;
  let next:number;
  if(event.key==='Home')next=0;
  else if(event.key==='End')next=buttons.length-1;
  else if(['ArrowRight','ArrowDown','ArrowLeft','ArrowUp'].includes(event.key))next=(index+(['ArrowRight','ArrowDown'].includes(event.key)?1:-1)+buttons.length)%buttons.length;
  else return;
  event.preventDefault();event.stopPropagation();buttons[next]?.focus();
 }
 function choose(speed:PlaybackSpeed){onChange(speed);close(true);}
</script>
<svelte:window on:pointerdown={outside} on:resize={position}/>
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="speed-control" bind:this={container} on:keydown={keys}>
 <button class="speed-trigger" type="button" aria-label="Playback speed" title={'Playback speed: '+label(value)} aria-expanded={open} {disabled} bind:this={trigger} on:click={()=>open=!open}>
  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 18a9 9 0 1 1 16 0M12 3v2M5.6 5.6 7 7m11.4-1.4L17 7M3 12h2m14 0h2m-9 0 4-4"/><circle cx="12" cy="12" r="1.5"/></svg>
 </button>
 {#if open}
  <div class="speed-picker" role="group" aria-label="Playback speeds" bind:this={picker} style={`left:${left}px;top:${top}px;visibility:${positioned?'visible':'hidden'}`}>
   {#each PLAYBACK_SPEEDS as speed}<button type="button" class:selected={speed===value} aria-pressed={speed===value} on:click={()=>choose(speed)}>{label(speed)}</button>{/each}
  </div>
 {/if}
</div>
<style>
 .speed-control{display:flex;flex:none;align-items:center;color:#eee}
 .speed-trigger{display:grid;place-items:center;flex:none;width:40px;height:40px;padding:0;border:0;border-radius:50%;background:transparent;color:inherit;cursor:pointer;transition:background-color .15s ease}
 .speed-trigger:hover:not(:disabled),.speed-trigger[aria-expanded='true']{background:#ffffff20}.speed-trigger:disabled{opacity:.45;cursor:default}
 svg{width:24px;height:24px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
 .speed-picker{position:fixed;z-index:1;display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:4px;width:min(370px,calc(100vw - 16px));max-height:calc(100dvh - 16px);overflow:auto;padding:8px;border:1px solid #454550;border-radius:10px;background:#181820;box-shadow:0 6px 24px #0007}
 .speed-picker button{min-width:0;height:36px;border:1px solid transparent;border-radius:6px;background:transparent;color:#eee;font-size:12px;cursor:pointer}.speed-picker button:hover{background:#ffffff18}.speed-picker button.selected{color:var(--accent);border-color:var(--accent);background:color-mix(in srgb,var(--accent) 12%,transparent)}
 button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
 @media(max-width:600px){.speed-trigger{width:32px;height:36px}svg{width:20px;height:20px}}
 @media(max-width:400px){.speed-trigger{width:28px}.speed-picker{grid-template-columns:repeat(4,minmax(0,1fr));width:min(260px,calc(100vw - 16px))}}
 :global(html[data-motion='reduced']) .speed-trigger{transition:none}@media(prefers-reduced-motion:reduce){.speed-trigger{transition:none}}
</style>

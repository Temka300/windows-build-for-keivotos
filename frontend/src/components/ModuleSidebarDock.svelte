<script lang="ts">
  import {onMount} from 'svelte';
  import {moduleSidebarPreferences,type ModuleSidebarOwner} from '../lib/moduleSidebarPreferences';
  export let owner:ModuleSidebarOwner;
  export let label:string;
  const {open,position}=moduleSidebarPreferences(owner);
  let dock:HTMLDivElement,grip:HTMLButtonElement;
  let pointer:number|null=null,startY=0,dragging=false,suppressClick=false,revealed=true,introReady=false;
  let revealTimer:ReturnType<typeof setTimeout>|null=null,clickTimer:ReturnType<typeof setTimeout>|null=null;
  let introFrame:number|null=null;
  function reveal(duration=1000) {
    revealed=true;if(revealTimer!==null)clearTimeout(revealTimer);
    revealTimer=setTimeout(()=>{revealed=false;revealTimer=null;},duration);
  }
  onMount(()=>{
    reveal(1600);
    introFrame=requestAnimationFrame(()=>{introFrame=requestAnimationFrame(()=>{introReady=true;introFrame=null;});});
    return ()=>{
      if(revealTimer!==null)clearTimeout(revealTimer);
      if(clickTimer!==null)clearTimeout(clickTimer);
      if(introFrame!==null)cancelAnimationFrame(introFrame);
    };
  });
  function start(event:PointerEvent) {
    if(event.button!==0)return;
    reveal(1800);pointer=event.pointerId;startY=event.clientY;dragging=false;
    grip.setPointerCapture(event.pointerId);
  }
  function move(event:PointerEvent) {
    if(event.pointerId!==pointer)return;
    if(Math.abs(event.clientY-startY)>=4)dragging=true;
    const bounds=dock.getBoundingClientRect();
    if(dragging&&bounds.height>0)position.set(Math.min(90,Math.max(10,(event.clientY-bounds.top)/bounds.height*100)));
  }
  function finish(event:PointerEvent) {
    if(event.pointerId!==pointer)return;
    const moved=dragging;pointer=null;dragging=false;
    if(grip.hasPointerCapture(event.pointerId))grip.releasePointerCapture(event.pointerId);
    if(moved){suppressClick=true;clickTimer=setTimeout(()=>{suppressClick=false;clickTimer=null;},0);}
    reveal(900);
  }
  function cancel(event:PointerEvent) {if(event.pointerId===pointer){pointer=null;dragging=false;}}
  function toggle() {if(!suppressClick){reveal(1100);open.update(value=>!value);}}
  function reposition(event:KeyboardEvent) {
    if(!['ArrowUp','ArrowDown','Home','End'].includes(event.key))return;
    event.preventDefault();reveal();
    position.update(value=>event.key==='Home'?10:event.key==='End'?90:Math.min(90,Math.max(10,value+(event.key==='ArrowUp'?-5:5))));
  }
</script>
<div bind:this={dock} class="module-sidebar-dock" class:is-open={$open&&introReady} data-sidebar-owner={owner}>
  <div id={`${owner}-sidebar-panel`} class="module-sidebar-panel" aria-hidden={!($open&&introReady)} inert={!($open&&introReady)}><slot/></div>
  <div class="module-sidebar-grip-track">
  <button bind:this={grip} class="module-sidebar-grip" class:is-dragging={dragging} class:is-revealed={revealed}
    style={`top:${$position}%`} type="button" data-module-sidebar-grip
    title={`Drag to reposition; click to ${$open?'hide':'show'} the ${label} sidebar`}
    aria-label={`${$open?'Hide':'Show'} ${label} sidebar`} aria-expanded={$open} aria-controls={`${owner}-sidebar-panel`}
    on:pointerdown={start} on:pointermove={move} on:pointerup={finish} on:pointercancel={cancel} on:lostpointercapture={cancel}
    on:keydown={reposition} on:click={toggle}>
    <span class="module-sidebar-grip-visual" aria-hidden="true"><span class="dots"><span></span><span></span><span></span></span><svg class:closed={!$open} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="m9 5 7 7-7 7"/></svg></span>
  </button>
  </div>
</div>
<style>
  .module-sidebar-dock{--sidebar-width:min(16rem,calc(100vw - 3rem));position:relative;z-index:30;height:100%;width:0;flex-shrink:0;transition:width 280ms cubic-bezier(.2,.8,.2,1)}
  .module-sidebar-dock.is-open{width:var(--sidebar-width)}
  .module-sidebar-panel{position:absolute;inset:0 auto 0 0;width:var(--sidebar-width);height:100%;overflow:hidden;transform:translate3d(-100%,0,0);transition:transform 280ms cubic-bezier(.2,.8,.2,1);background:#111118}
  .is-open .module-sidebar-panel{transform:translate3d(0,0,0)}
  .module-sidebar-grip-track{position:absolute;left:100%;top:0;z-index:20;height:100%;width:40px;pointer-events:none}
  .module-sidebar-grip{position:absolute;left:0;width:40px;height:64px;transform:translateY(-50%);border:0;background:transparent;padding:0;cursor:grab;touch-action:none;pointer-events:auto}
  .module-sidebar-grip.is-dragging{cursor:grabbing}
  .module-sidebar-grip-visual{position:absolute;left:0;top:50%;display:flex;align-items:center;justify-content:center;gap:2px;width:28px;height:48px;box-sizing:border-box;padding:0 4px;border:1px solid #ffffff1a;border-left:0;border-radius:0 16px 16px 0;background:linear-gradient(90deg,#111118,#1d1d29);box-shadow:5px 0 16px #0005;color:#6b7280;opacity:0;transform:translate3d(-8px,-50%,0) scale(.9);transform-origin:left center;transition:opacity 140ms ease-out,transform 180ms cubic-bezier(.2,.8,.2,1),background-color 140ms ease,border-color 140ms ease,color 140ms ease}
  .dots{display:flex;flex-direction:column;gap:2px;opacity:.35}.dots span{height:2px;width:2px;border-radius:50%;background:currentColor}.module-sidebar-grip svg{width:16px;height:16px;flex-shrink:0;transition:transform 300ms}.closed{transform:rotate(180deg)}
  .module-sidebar-grip:hover .module-sidebar-grip-visual,.module-sidebar-grip:focus-visible .module-sidebar-grip-visual,.is-revealed .module-sidebar-grip-visual,.is-dragging .module-sidebar-grip-visual{opacity:1;transform:translate3d(0,-50%,0) scale(1)}
  .module-sidebar-grip:hover .module-sidebar-grip-visual,.module-sidebar-grip:focus-visible .module-sidebar-grip-visual{color:#e9d5ff;border-color:#ffffff26}
  .module-sidebar-grip.is-dragging .module-sidebar-grip-visual{color:#e9d5ff;border-color:#c084fc59;transform:translate3d(0,-50%,0) scale(1.05)}
  .module-sidebar-grip:focus-visible{outline:2px solid var(--accent);outline-offset:-5px;border-radius:10px}
  @media(max-width:700px){.module-sidebar-dock.is-open{width:0}.module-sidebar-grip-track{left:0;transition:left 280ms cubic-bezier(.2,.8,.2,1)}.is-open .module-sidebar-grip-track{left:var(--sidebar-width)}}
  :global(html[data-motion='reduced']) .module-sidebar-dock,:global(html[data-motion='reduced']) .module-sidebar-panel,:global(html[data-motion='reduced']) .module-sidebar-grip-track,:global(html[data-motion='reduced']) .module-sidebar-grip-visual,:global(html[data-motion='reduced']) .module-sidebar-grip svg{transition:none}
  @media(prefers-reduced-motion:reduce){.module-sidebar-dock,.module-sidebar-panel,.module-sidebar-grip-track,.module-sidebar-grip-visual,.module-sidebar-grip svg{transition:none}}
</style>

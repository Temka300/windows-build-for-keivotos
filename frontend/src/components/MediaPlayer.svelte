<script lang="ts">
  import {onMount,onDestroy,tick} from 'svelte';
  import {reportDiagnostic,diagnosticAction} from '../lib/diagnostics';
  import type {PlaybackMedia,PlaybackFailure,PlaybackPosition} from '../lib/playback';
  import {hideControlsSeconds,loopModeFor,autoNextFor,volumeFor,speedFor,normalizePlaybackSpeed,type LoopMode,type VideoPlaybackOwner,type PlaybackSpeed} from '../lib/playbackPreferences';
  import {createSpaceHold,isSpaceKey} from '../lib/playerSpaceHold';
  import {holdSpaceEnabled,holdSpeedMultiplier,type HoldSpeedMultiplier} from '../lib/playbackPreferences';
  import SettingsSwitch from './SettingsSwitch.svelte';
  import VolumeControl from './VolumeControl.svelte';
  import PlaybackSpeedControl from './PlaybackSpeedControl.svelte';
  export let media:PlaybackMedia;
  export let owner:VideoPlaybackOwner;
  export let label='Video player';
  export let backLabel='Back to videos';
  export let hasPrevious=false;
  export let hasNext=false;
  export let canWrap=false;
  export let canRandom=false;
  export let onRandom:()=>Promise<boolean>=async()=>false;
  export let navigating=false;
  export let navigationNotice='';
  export let progress:PlaybackPosition|undefined=undefined;
  export let progressNotice='';
  export let onRetryProgress:()=>Promise<unknown>=async()=>{};
  export let panelActive=false;
  export let hasPanel=false;
  export let onDismissPanel:()=>boolean=()=>false;
  export let mayReplayCurrent:()=>boolean=()=>true;
  export let onClose:()=>void;
  export let onNavigate:(direction:-1|1,wrap:boolean)=>Promise<boolean>;
  export let onFailure:(item:PlaybackMedia,reason:PlaybackFailure)=>Promise<unknown>;
  let player:HTMLVideoElement;
  let observedNode:HTMLVideoElement|null=null,activeProgress:PlaybackPosition|undefined;
  let restoring=false,explicitSeek=false,actualPlayback=false,playbackAdvanced=false,observedTime=0;
  function observe(ended=player?.ended??false){
    if(!activeProgress||!player||player!==observedNode||restoring||player.seeking||!Number.isFinite(player.duration)||player.duration<=0)return;
    activeProgress.observe({position:ended?player.duration:Math.min(player.duration,Math.max(0,player.currentTime)),duration:player.duration,ended});
  }
  function timeUpdated(event:Event){
    if(event.currentTarget!==observedNode)return;
    position=player.currentTime;
    if(actualPlayback&&!player.paused&&!restoring&&!player.seeking&&Math.abs(position-observedTime)>.025){playbackAdvanced=true;observedTime=position;observe();}
  }
  function metadata(event:Event){
    applySpeed();if(event.currentTarget!==observedNode||!activeProgress)return;
    const saved=activeProgress.initialPosition;observedTime=0;
    if(Number.isFinite(saved)&&saved>0&&Number.isFinite(player.duration)&&saved<player.duration){restoring=true;player.currentTime=saved;position=saved;observedTime=saved;}
  }
  function seeked(event:Event){
    if(event.currentTarget!==observedNode)return;
    if(restoring){restoring=false;explicitSeek=false;observedTime=player.currentTime;return;}
    if(explicitSeek){explicitSeek=false;observedTime=player.currentTime;observe();void activeProgress?.flush();}
  }
  function flushProgress(keepalive=false){return activeProgress?.flush(keepalive)??Promise.resolve(true);}
  function blurred(){cancelSpace();if(playbackAdvanced)observe();void flushProgress();}
  function leaving(){cancelSpace();if(playbackAdvanced)observe();void flushProgress(true);}

  let viewer:HTMLDivElement;
  let playing=false,position=0,duration=0,failure='',notice='';
  let reported=new Set<PlaybackFailure>();
  let alive=true,mounted=false;
  let controlsVisible=true,pointerOverControls=false,pointerInteracting=false,keyboardActive=false;
  let idleSeconds=5;
  let idleTimer:ReturnType<typeof setTimeout>|undefined;
  let stopPreference:()=>void=()=>{};
  let stopLoop:()=>void=()=>{};
  let stopAutoNext:()=>void=()=>{};
  let stopVolume:()=>void=()=>{};
  let stopSpeed:()=>void=()=>{};
  let speed:PlaybackSpeed=1,speedOpen=false;
  let holdEnabled=true,holdMultiplier:HoldSpeedMultiplier=1.5,holdBoosted=false;
  let stopHoldEnabled:()=>void=()=>{},stopHoldMultiplier:()=>void=()=>{};
  const spaceHold=createSpaceHold({tap:()=>{void togglePlay();},boost:startSpaceBoost,restore:restoreSpaceBoost});
  let loopMode:LoopMode='off';
  let autoNext=false,endBusy=false;
  $: nextHint=navigating?'Loading…':hasNext||(loopMode==='all'&&canWrap)?'Next video':'No next video';
  let volume=100;
  const loopOrder:LoopMode[]=['off','all','one'];
  function stamp(seconds:number){
    const whole=Math.max(0,Math.floor(Number.isFinite(seconds)?seconds:0));
    const minutes=Math.floor(whole/60),tail=String(whole%60).padStart(2,'0');
    return minutes<60?`${minutes}:${tail}`:`${Math.floor(minutes/60)}:${String(minutes%60).padStart(2,'0')}:${tail}`;
  }
  function cycleLoop(){loopModeFor(owner).set(loopOrder[(loopOrder.indexOf(loopMode)+1)%loopOrder.length]);reveal();}
  function toggleAutoNext(){autoNextFor(owner).set(!autoNext);reveal();}
  async function navigate(direction:-1|1){
    cancelSpace();
    try{await onNavigate(direction,loopMode==='all');}
    catch{notice='Could not load another video. Try again.';controlsVisible=true;clearIdle();}
  }
  async function randomVideo(){
    if(navigating||!canRandom)return;
    cancelSpace();
    try{await onRandom();}
    catch{notice='Could not load another video. Try again.';controlsVisible=true;clearIdle();}
  }
  async function replay(){
    if(!player||failure)return;
    explicitSeek=false;playbackAdvanced=false;observedTime=0;player.currentTime=0;
    try{await player.play();}
    catch{failure='Could not replay this video. See Logs.';controlsVisible=true;clearIdle();void report('playback');}
  }
  async function ended(event:Event){
    if(event.currentTarget!==observedNode)return;
    cancelSpace();playing=false;controlsVisible=true;clearIdle();
    if(endBusy||failure)return;
    const item=media,node=player;endBusy=true;
    try{
      observe(true);await flushProgress();
      if(!alive||media!==item||player!==node)return;
      if(loopMode==='one'){await replay();return;}
      if(!autoNext)return;
      const moved=await onNavigate(1,loopMode==='all');
      if(alive&&media===item&&!moved&&loopMode==='all'&&mayReplayCurrent())await replay();
    }catch{if(alive&&media===item)notice='Could not load the next video. Try again.';}
    finally{if(media===item)endBusy=false;}
  }
  function clearIdle(){if(idleTimer!==undefined){clearTimeout(idleTimer);idleTimer=undefined;}}
  function scheduleIdle(){
    clearIdle();
    if(!playing||failure||notice||navigationNotice||panelActive||speedOpen||spaceHold.pending||pointerOverControls||keyboardActive)return;
    idleTimer=setTimeout(()=>{controlsVisible=false;idleTimer=undefined;if(viewer?.contains(document.activeElement))viewer.focus();},idleSeconds*1000);
  }
  function reveal(){controlsVisible=true;scheduleIdle();}
  function pointerMove(event:PointerEvent){
    keyboardActive=false;
    if(panelActive||speedOpen||spaceHold.pending){reveal();return;}
    pointerOverControls=event.target instanceof Element&&!!event.target.closest('button,input');
    const bounds=viewer.getBoundingClientRect();
    const atEdge=event.clientX>=bounds.left&&event.clientX<=bounds.right&&event.clientY>=bounds.top&&event.clientY<=bounds.bottom&&
      (bounds.right-event.clientX<=4||bounds.bottom-event.clientY<=4);
    const focusedControl=viewer.contains(document.activeElement)&&document.activeElement!==viewer;
    if(atEdge&&playing&&!failure&&!notice&&!navigationNotice&&!pointerOverControls&&!pointerInteracting&&!focusedControl){
      clearIdle();controlsVisible=false;return;
    }
    reveal();
  }
  function pointerDown(event:PointerEvent){
    keyboardActive=false;
    pointerInteracting=event.target instanceof Element&&!!event.target.closest('button,input');
    if(pointerInteracting){controlsVisible=true;clearIdle();}
  }
  function pointerRelease(){if(pointerInteracting){pointerInteracting=false;scheduleIdle();}}
  function controlFocus(event:FocusEvent){if(event.target!==viewer){cancelSpace();if(keyboardActive){controlsVisible=true;clearIdle();}}}
  function controlBlur(){void tick().then(()=>{if(!alive)return;if(document.activeElement!==viewer)cancelSpace();if(!viewer?.contains(document.activeElement)||document.activeElement===viewer){keyboardActive=false;scheduleIdle();}});}
  $: if(navigationNotice){cancelSpace();controlsVisible=true;clearIdle();}
  $: if(panelActive){cancelSpace();speedOpen=false;controlsVisible=true;clearIdle();}else if(mounted)scheduleIdle();
  $: if(speedOpen){cancelSpace();controlsVisible=true;clearIdle();}else if(mounted)scheduleIdle();
  $: if(mounted) void open(media);
  $: if(mounted&&observedNode===player)activeProgress=progress;
  async function report(reason: PlaybackFailure) {
    if (!media || reported.has(reason)) return;
    reported.add(reason);
    const item = media;
    try {await diagnosticAction(operation_id=>{
      reportDiagnostic('media_failed',{component:owner,reason,operation_id});
      return onFailure(item, reason);
    });}
    catch { if (alive && media === item) failure += ' Could not write to Logs.'; }
  }
  function mediaError() {
    cancelSpace();
    if (!media || !player?.error || player.error.code === 1) return;
    failure = 'This video cannot play here. See Logs.';
    clearIdle();controlsVisible=true;
    void report(player.error.code === 2 ? 'network' : player.error.code === 3 ? 'decode' : 'unsupported_codec');
  }
  async function open(item: PlaybackMedia) {
    cancelSpace();
    player?.pause();void flushProgress();observedNode=null;activeProgress=undefined;
    restoring=false;explicitSeek=false;actualPlayback=false;playbackAdvanced=false;observedTime=0;
    playing = false; position = 0; duration = 0; failure = ''; notice = ''; reported = new Set();
    clearIdle();controlsVisible=true;keyboardActive=false;pointerOverControls=false;pointerInteracting=false;speedOpen=false;
    endBusy=false;
    await tick(); if (!alive || media !== item) return;
    if(panelActive)enabledControls(viewer.querySelector<HTMLElement>('[data-player-panel]')??viewer)[0]?.focus();else viewer?.focus();
    observedNode=player;activeProgress=progress;
    player.volume=volume/100;
    applySpeed();if(player.readyState>=1)metadata({currentTarget:player} as unknown as Event);
    if (!['mp4','m4v','webm'].includes(item.ext)) {
      failure = 'This format cannot play here. See Logs.'; void report('unsupported_format'); return;
    }
    try {await player.play();}
    catch (caught) {
      // A browser autoplay policy is not a broken file: Play remains available.
      if (caught instanceof DOMException && (caught.name === 'NotAllowedError' || caught.name === 'AbortError')) return;
      if (media !== item) return;
      failure = 'This video cannot play here. See Logs.'; void report('playback');
    }
  }
  async function togglePlay() {
    if (!player || failure) return;
    if (!player.paused) player.pause();
    else try {await player.play();} catch {failure = 'Could not play this video. See Logs.'; void report('playback');}
  }
  async function fullscreen() {
    try {if (document.fullscreenElement) await document.exitFullscreen(); else await viewer.requestFullscreen();}
    catch {notice = 'Fullscreen is unavailable. See Logs.';controlsVisible=true;clearIdle();void report('fullscreen');}
  }
  async function close() {
    const item=media,node=player,savedProgress=activeProgress;
    cancelSpace();player?.pause();
    if(document.fullscreenElement===viewer)await document.exitFullscreen().catch(()=>{});
    if(savedProgress)await savedProgress.flush();
    if(alive&&media===item&&player===node)onClose();
  }
  function enabledControls(root:HTMLElement=viewer):HTMLElement[]{
    return [...root.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled)')].filter(node=>!node.closest('[inert]'));
  }
  function playerKeys(event: KeyboardEvent) {
    if(!isSpaceKey(event))cancelSpace();
    if(event.key==='Escape'&&speedOpen){
      event.preventDefault();event.stopPropagation();speedOpen=false;
      viewer.querySelector<HTMLButtonElement>('[aria-label="Playback speed"]')?.focus();return;
    }
    if(event.key==='Tab'&&panelActive){
      event.preventDefault();keyboardActive=true;controlsVisible=true;clearIdle();
      const controls=enabledControls(viewer.querySelector<HTMLElement>('[data-player-panel]')??viewer);
      (event.shiftKey?controls.at(-1):controls[0])?.focus();return;
    }
    if(event.key==='Tab'&&!controlsVisible){event.preventDefault();keyboardActive=true;controlsVisible=true;clearIdle();void tick().then(()=>{const controls=enabledControls();(event.shiftKey?controls.at(-1):controls[0])?.focus();});return;}
    if(event.key==='Tab'||event.key.startsWith('Arrow')){keyboardActive=true;controlsVisible=true;clearIdle();}
    if (event.key === 'Escape' && !document.fullscreenElement) {
      if(onDismissPanel()){event.preventDefault();event.stopPropagation();return;}
      void close(); return;
    }
    const target=event.target instanceof HTMLElement?event.target:null;
    const editing=target?.isContentEditable||target?.closest('button,input,textarea,select,a,[contenteditable],[role="textbox"]');
    if(isSpaceKey(event)&&(editing||event.isComposing||event.keyCode===229||event.altKey||event.ctrlKey||event.metaKey||event.shiftKey))cancelSpace();
    if(!editing&&!event.isComposing&&event.keyCode!==229&&!event.altKey&&!event.ctrlKey&&!event.metaKey&&!event.shiftKey&&player&&!failure){
      if(isSpaceKey(event)){
        event.preventDefault();keyboardActive=true;controlsVisible=true;clearIdle();
        spaceHold.press(holdEnabled,event.repeat);
        return;
      }
      if(event.key==='ArrowLeft'||event.key==='ArrowRight'){
        event.preventDefault();
        if(Number.isFinite(duration)&&duration>0){
          explicitSeek=true;player.currentTime=Math.min(duration,Math.max(0,player.currentTime+(event.key==='ArrowRight'?5:-5)));
          position=player.currentTime;
        }
        return;
      }
    }
    if (event.key !== 'Tab') return;
    const controls = enabledControls();
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && (document.activeElement === first || document.activeElement === viewer)) {event.preventDefault(); last?.focus();}
    else if (!event.shiftKey && document.activeElement === last) {event.preventDefault(); first?.focus();}
  }
  function seek(event: Event) {
    if (player && Number.isFinite(duration)){explicitSeek=true;player.currentTime = Number((event.currentTarget as HTMLInputElement).value);}
  }
  function setVolume(value:number){volumeFor(owner).set(value);reveal();}
  function writeRate(rate:number){
    if(!player)return false;
    try{if(player.playbackRate!==rate)player.playbackRate=rate;return player.playbackRate===rate;}
    catch{return false;}
  }
  function applySpeed(){
    if(!writeRate(holdBoosted?speed*holdMultiplier:speed)&&holdBoosted){holdBoosted=false;writeRate(speed);}
  }
  function startSpaceBoost(){
    if(!alive||!player||player.paused||player.ended||failure||panelActive||speedOpen)return;
    holdBoosted=true;applySpeed();controlsVisible=true;clearIdle();
  }
  function restoreSpaceBoost(){holdBoosted=false;applySpeed();}
  function cancelSpace(){const owned=spaceHold.pending||holdBoosted;spaceHold.cancel();if(holdBoosted)restoreSpaceBoost();if(owned){keyboardActive=false;scheduleIdle();}}
  function spaceReleased(event:KeyboardEvent){
    if(!isSpaceKey(event)||!spaceHold.pending)return;
    event.preventDefault();
    const target=event.target instanceof HTMLElement?event.target:null;
    const editing=target?.isContentEditable||target?.closest('button,input,textarea,select,a,[contenteditable],[role="textbox"]');
    const allowTap=!!(alive&&player&&!failure&&target&&viewer?.contains(target)&&!editing&&!document.hidden&&!event.isComposing&&event.keyCode!==229&&!event.altKey&&!event.ctrlKey&&!event.metaKey&&!event.shiftKey);
    spaceHold.release(allowTap);keyboardActive=false;scheduleIdle();
  }
  function paused(event?:Event){if(event&&event.currentTarget!==observedNode)return;cancelSpace();playing=false;controlsVisible=true;clearIdle();if(!event||event.currentTarget===observedNode){if(playbackAdvanced)observe();void flushProgress();}}
  function pageHidden(){if(document.hidden)blurred();}
  function setSpeed(value:PlaybackSpeed){cancelSpace();speedFor(owner).set(value);reveal();}
  function speedChanged(){
    if(!player)return;
    if(holdBoosted){if(player.playbackRate!==speed*holdMultiplier){cancelSpace();applySpeed();}return;}
    const actual=normalizePlaybackSpeed(player.playbackRate);
    if(actual!==speed)speedFor(owner).set(actual);
    applySpeed();
  }
  function volumeChanged(){
    if(!player)return;
    const actual=Math.round(player.volume*100);
    if(actual!==volume)volumeFor(owner).set(actual);
  }
  onMount(()=>{stopPreference=hideControlsSeconds.subscribe(seconds=>{idleSeconds=seconds;scheduleIdle();});stopLoop=loopModeFor(owner).subscribe(value=>loopMode=value);stopAutoNext=autoNextFor(owner).subscribe(value=>autoNext=value);stopVolume=volumeFor(owner).subscribe(value=>{volume=value;if(player&&player.volume!==value/100)player.volume=value/100;});stopSpeed=speedFor(owner).subscribe(value=>{if(value!==speed)cancelSpace();speed=value;applySpeed();});stopHoldEnabled=holdSpaceEnabled.subscribe(value=>{cancelSpace();holdEnabled=value;});stopHoldMultiplier=holdSpeedMultiplier.subscribe(value=>{cancelSpace();holdMultiplier=value;});mounted=true;});
  onDestroy(()=>{leaving();observedNode=null;activeProgress=undefined;cancelSpace();alive=false;clearIdle();stopPreference();stopLoop();stopAutoNext();stopVolume();stopSpeed();stopHoldEnabled();stopHoldMultiplier();player?.pause();if(document.fullscreenElement===viewer)void document.exitFullscreen().catch(()=>{});});
</script>
  <svelte:window on:pointerup={pointerRelease} on:pointercancel={pointerRelease} on:keyup={spaceReleased} on:blur={blurred} on:pagehide={leaving}/>
  <svelte:document on:visibilitychange={pageHidden} on:fullscreenchange={cancelSpace}/>
  <!-- svelte-ignore a11y_no_noninteractive_tabindex a11y_no_noninteractive_element_interactions -->
  <div class="video-viewer" class:cursor-hidden={!controlsVisible} bind:this={viewer} role="dialog" aria-modal="true" aria-label={label} tabindex="-1" on:keydown={playerKeys} on:pointerdown={pointerDown} on:pointermove={pointerMove} on:touchstart={reveal} on:focusin={controlFocus} on:focusout={controlBlur}>
    {#key media.src}
      <!-- svelte-ignore a11y_media_has_caption -->
      <video bind:this={player} src={['mp4','m4v','webm'].includes(media.ext) ? media.src : undefined} playsinline preload="metadata" on:error={mediaError} on:play={() => {playing=true;scheduleIdle();}} on:pause={paused} on:ended={ended} on:playing={(event)=>{if(event.currentTarget===observedNode)actualPlayback=true;}} on:timeupdate={timeUpdated} on:seeked={seeked} on:durationchange={() => duration = Number.isFinite(player.duration) ? player.duration : 0} on:volumechange={volumeChanged} on:loadedmetadata={metadata} on:ratechange={speedChanged}></video>
    {/key}
    <button class="back" class:controls-hidden={!controlsVisible} aria-label={backLabel} tabindex={controlsVisible?0:-1} on:pointerenter={() => {pointerOverControls=true;clearIdle();}} on:pointerleave={() => {pointerOverControls=false;scheduleIdle();}} on:click={close}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m14 5-7 7 7 7" /></svg></button>
    {#if progressNotice}<p class="progress-error" role="alert">{progressNotice} <button on:click={()=>onRetryProgress()}>Retry</button></p>{/if}
    {#if failure || notice || navigationNotice}<p class="playback-error" role="alert">{failure || notice || navigationNotice}</p>{/if}
    <div class="player-controls" role="group" aria-label="Playback controls" class:controls-hidden={!controlsVisible} inert={!controlsVisible} on:pointerenter={() => {pointerOverControls=true;clearIdle();}} on:pointerleave={() => {pointerOverControls=false;scheduleIdle();}}>
      <input aria-label="Seek" type="range" min="0" max={duration || 0} step="0.1" value={position} style={`--progress:${duration ? position / duration * 100 : 0}%`} disabled={!duration || !!failure} on:input={seek} />
      <div class="control-row">
        <button aria-label="Previous video" disabled={navigating || !(hasPrevious||(loopMode==='all'&&canWrap))} on:click={() => navigate(-1)}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 5v14M19 5 8 12l11 7Z" /></svg></button>
        <button aria-label={playing ? 'Pause' : 'Play'} disabled={!!failure} on:click={togglePlay}><svg viewBox="0 0 24 24" aria-hidden="true">{#if playing}<path d="M8 5v14M16 5v14" />{:else}<path d="m7 4 13 8-13 8Z" />{/if}</svg></button>
        <div class="tooltip-control">
          <button aria-label="Next video" aria-describedby="player-next-tooltip" disabled={navigating || !(hasNext||(loopMode==='all'&&canWrap))} on:click={() => navigate(1)}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 5v14M5 5l11 7-11 7Z" /></svg></button>
          <span id="player-next-tooltip" class="player-tooltip" role="tooltip">{nextHint}</span>
        </div>
        <VolumeControl value={volume} disabled={!!failure} onChange={setVolume}/>
        <span class="playback-time" aria-label="Playback time" title="Current time / total duration">{stamp(position)} / {stamp(duration)}</span>
        <div class="secondary-controls">
          <div class="tooltip-control">
            <button aria-label="Random video" aria-describedby="player-random-tooltip" disabled={navigating||!canRandom} on:click={randomVideo}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h3c5 0 7 12 12 12h3m-4-4 4 4-4 4M3 18h3c2 0 3-2 4-4m4-4c1-2 2-4 4-4h3m-4-4 4 4-4 4"/></svg></button>
            <span id="player-random-tooltip" class="player-tooltip" role="tooltip">{navigating?'Loading…':canRandom?'Random video':'No other videos'}</span>
          </div>
          <PlaybackSpeedControl value={speed} disabled={!!failure} bind:open={speedOpen} onChange={setSpeed}/>
          <div class="auto-next tooltip-control">
            <SettingsSwitch label="Auto next" describedBy="player-auto-next-tooltip" checked={autoNext} on:click={toggleAutoNext}/>
            <span id="player-auto-next-tooltip" class="player-tooltip" role="tooltip">{autoNext?'Turn auto-next off':'Turn auto-next on'}</span>
          </div>
          <button class="loop-button" class:off={loopMode==='off'} class:all={loopMode==='all'} class:one={loopMode==='one'} aria-label={loopMode==='one'?'Loop one':loopMode==='all'?'Loop all':'Loop off'} title={loopMode==='one'?'Loop one':loopMode==='all'?'Loop all':'Loop off'} aria-pressed={loopMode!=='off'} on:click={cycleLoop}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17 2l3 3-3 3"/><path d="M3 11V9a4 4 0 0 1 4-4h13"/><path d="M7 22l-3-3 3-3"/><path d="M21 13v2a4 4 0 0 1-4 4H4"/></svg>{#if loopMode==='one'}<span class="loop-one-mark">1</span>{/if}</button>
          <button class="fullscreen" aria-label="Fullscreen" on:click={fullscreen}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 4H4v5m11-5h5v5M4 15v5h5m11-5v5h-5" /></svg></button>
          {#if $$slots.transport&&hasPanel}<div class="owner-transport"><slot name="transport"/></div>{/if}
        </div>
      </div>
    </div>
    {#if $$slots.panel&&hasPanel}<div class="owner-panel" data-player-panel inert={!panelActive}><slot name="panel"/></div>{/if}
  </div>
<style>
 .video-viewer{position:fixed;inset:0;z-index:110;background:black;display:grid;place-items:center;overflow:hidden}
 .video-viewer.cursor-hidden,.video-viewer.cursor-hidden video{cursor:none}
 .owner-transport{display:flex;flex:none;align-items:center}.owner-panel{position:absolute;inset:0;pointer-events:none}
 video{width:100%;height:100%;max-height:100dvh;object-fit:contain}
 .back{position:absolute;top:16px;left:16px;width:40px;height:40px;display:grid;place-items:center;border:0;background:transparent;border-radius:999px;color:#eee;transition:width .18s ease,background-color .18s ease,opacity .18s ease}
 .back:hover,.back:focus-visible{width:56px;background:#33333b}
 .back.controls-hidden,.player-controls.controls-hidden{opacity:0;pointer-events:none}
 .player-controls{transition:opacity .18s ease}
 .video-viewer svg{width:24px;height:24px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
 .player-controls{position:absolute;bottom:0;left:0;right:0;display:flex;flex-direction:column;gap:8px;background:linear-gradient(transparent,#000b);padding:32px 24px 16px}
 .control-row{display:flex;flex-wrap:wrap;align-items:center;gap:12px}
 .playback-time{flex:none;white-space:nowrap;color:#eee;font-size:12px;font-variant-numeric:tabular-nums}
 .secondary-controls{margin-left:auto;display:flex;flex:none;align-items:center;gap:12px}
 .player-controls button{display:grid;place-items:center;flex:none;width:40px;height:40px;border:0;background:transparent;color:#eee;border-radius:50%;transition:background-color .15s ease}
 .player-controls button:hover:not(:disabled){background:#ffffff20}
 .tooltip-control{position:relative;display:flex;flex:none;align-items:center}
 .player-tooltip{position:absolute;bottom:calc(100% + 8px);left:50%;transform:translateX(-50%);z-index:2;visibility:hidden;pointer-events:none;white-space:nowrap;max-width:calc(100vw - 24px);padding:6px 8px;border-radius:6px;background:#282831;color:#eee;font-size:12px;line-height:1.3;box-shadow:0 2px 8px #0006;transition:visibility 0s}
 .tooltip-control:hover>.player-tooltip{visibility:visible;transition-delay:.3s}
 .tooltip-control:focus-within>.player-tooltip{visibility:visible;transition-delay:0s}
 .auto-next{display:flex;align-items:center}
 .auto-next :global(.settings-switch){width:38px;height:22px}
 .auto-next :global(.settings-switch span){width:16px;height:16px}
 .auto-next :global(.settings-switch.checked span){transform:translateX(16px)}
 .loop-button{position:relative}
 .loop-button.off{color:#8a8a92}
 .loop-button.all,.loop-button.one{color:#fff}
 .loop-one-mark{position:absolute;top:7px;left:8px;font-size:10px;line-height:1;font-weight:800;color:#fff;pointer-events:none;z-index:2;text-shadow:-1px 0 0 #111,1px 0 0 #111,0 -1px 0 #111,0 1px 0 #111,-1px -1px 0 #111,1px -1px 0 #111,-1px 1px 0 #111,1px 1px 0 #111}
 .loop-one-mark::before{content:"";position:absolute;z-index:-1;left:-2px;right:-2px;top:5px;height:5px;border-radius:3px;background:#151515}
 .player-controls>input{appearance:none;width:100%;height:20px;margin:0;padding:0;border:0;border-radius:0;background:transparent;cursor:pointer}
 .player-controls>input::-webkit-slider-runnable-track{height:3px;border:0;background:linear-gradient(to right,var(--accent) var(--progress),#ffffff55 var(--progress))}
 .player-controls>input::-moz-range-track{height:3px;border:0;background:linear-gradient(to right,var(--accent) var(--progress),#ffffff55 var(--progress))}
 .player-controls>input::-webkit-slider-thumb{appearance:none;width:11px;height:11px;margin-top:-4px;border:0;border-radius:50%;background:var(--accent)}
 .player-controls>input::-moz-range-thumb{width:11px;height:11px;border:0;border-radius:50%;background:var(--accent)}
 @media(max-width:600px){.player-controls{padding-inline:12px}.control-row,.secondary-controls{gap:3px}.playback-time{font-size:11px}.control-row button{width:32px;height:36px}}
 @media(max-width:400px){.control-row button{width:28px}}
 :global(html[data-motion='reduced']) .back,:global(html[data-motion='reduced']) .player-controls,:global(html[data-motion='reduced']) .player-controls button{transition:none}
 @media(prefers-reduced-motion:reduce){.back,.player-controls,.player-controls button{transition:none}}
 :global(html[data-motion='reduced']) .player-tooltip{transition:none}
 @media(prefers-reduced-motion:reduce){.player-tooltip{transition:none}}
 .progress-error{position:absolute;top:20px;max-width:calc(100% - 160px);color:#e5e5eb;background:#171720;padding:8px 12px;border-radius:8px;font-size:13px}.progress-error button{text-decoration:underline}
 .playback-error{position:absolute;color:#e5e5eb;background:#171720;padding:12px 18px;border-radius:8px}
 button:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
 button:disabled{opacity:.45}
</style>

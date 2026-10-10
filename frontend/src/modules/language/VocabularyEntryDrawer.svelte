<script lang="ts">
  import {createEventDispatcher,onDestroy,tick} from 'svelte';
  import {languageApi,type VocabularyCreate,type VocabularyEntry} from './api';
  export let open=false;
  const dispatch=createEventDispatcher<{close:void;saved:VocabularyEntry}>();
  let dialog:HTMLDivElement,wordInput:HTMLInputElement,decisionDialog:HTMLDivElement;
  let korean='',pronunciation='',english='',error='',busy=false,wasOpen=false,alive=true;
  let submitted:VocabularyCreate|null=null;
  let decision:{resolve:(leave:boolean)=>void}|null=null;
  let returnFocus:HTMLElement|null=null;
  $: dirty=!!(korean||pronunciation||english||submitted);
  $: invalid=!korean.trim()||!english.trim()||[...korean].length>256||[...pronunciation].length>256||[...english].length>2000;
  $: if(open&&!wasOpen){
    wasOpen=true;korean='';pronunciation='';english='';error='';submitted=null;
    void tick().then(()=>requestAnimationFrame(()=>{
      if(alive&&open){dialog?.getBoundingClientRect();wordInput?.focus({preventScroll:true});}
    }));
  }else if(!open)wasOpen=false;

  export function hasDraft(){return open&&(dirty||busy);}
  export async function beforeNavigate():Promise<boolean>{
    if(!open)return true;
    if(busy||decision)return false;
    if(dirty){
      const leave=await new Promise<boolean>(resolve=>{
        returnFocus=document.activeElement instanceof HTMLElement?document.activeElement:null;
        decision={resolve};void tick().then(()=>{if(alive)decisionDialog?.focus();});
      });
      if(!alive||!leave)return false;
    }
    dispatch('close');return true;
  }
  function choose(leave:boolean){
    const pending=decision;decision=null;pending?.resolve(leave);
    if(!leave)void tick().then(()=>{if(alive&&open)(returnFocus&&dialog.contains(returnFocus)?returnFocus:wordInput)?.focus();});
  }
  function trap(event:KeyboardEvent,root:HTMLElement){
    if(event.key!=='Tab')return;
    const controls=[...root.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),textarea:not(:disabled)')];
    const first=controls[0],last=controls[controls.length-1];
    if(!first){event.preventDefault();root.focus();}
    else if(event.shiftKey&&(document.activeElement===first||document.activeElement===root)){event.preventDefault();last.focus();}
    else if(!event.shiftKey&&(document.activeElement===last||document.activeElement===root)){event.preventDefault();first.focus();}
  }
  function keys(event:KeyboardEvent){
    if(!open||event.defaultPrevented)return;
    const root=decision?decisionDialog:dialog;
    if(!(event.target instanceof Node)||!root?.contains(event.target))return;
    if(event.key==='Escape'){
      event.preventDefault();event.stopPropagation();if(decision)choose(false);else void beforeNavigate();
    }else if(event.key==='Enter'&&(event.isComposing||event.keyCode===229))event.preventDefault();
    else trap(event,root);
  }
  async function save(){
    if(busy||invalid||decision)return;
    submitted??={request_id:crypto.randomUUID(),korean,pronunciation,english};
    const payload=submitted;busy=true;error='';
    try{
      const entry=await languageApi.createVocabulary(payload);
      if(alive&&open)dispatch('saved',entry);
    }catch(caught){
      if(alive){error=caught instanceof Error?caught.message:'Could not save your entry. Retry saving it.';await tick();dialog?.focus();}
    }finally{if(alive)busy=false;}
  }
  onDestroy(()=>{alive=false;decision?.resolve(false);});
</script>

<svelte:window on:keydown={keys}/>
<div class="vocabulary-drawer-layer" class:open aria-hidden={!open}>
  <button class="vocabulary-shade" type="button" aria-label="Close vocabulary entry" tabindex="-1" disabled={!open||busy||!!decision} on:click={()=>void beforeNavigate()}></button>
  <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
  <div class="vocabulary-entry-drawer" class:open inert={!open||!!decision} role="dialog" aria-label="New vocabulary" tabindex="-1" bind:this={dialog}>
    <header><h2>New vocabulary</h2><button class="close" type="button" aria-label="Close new vocabulary" disabled={busy} on:click={()=>void beforeNavigate()}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg></button></header>
    <form on:submit|preventDefault={save}>
      <label for="vocabulary-korean">Korean word</label>
      <input id="vocabulary-korean" lang="ko" autocomplete="off" bind:this={wordInput} bind:value={korean} disabled={busy} readonly={!!submitted} required aria-invalid={[...korean].length>256}/>
      {#if [...korean].length>256}<p class="error">Use up to 256 characters for the Korean word.</p>{/if}
      <label for="vocabulary-pronunciation">Pronunciation</label>
      <input id="vocabulary-pronunciation" autocomplete="off" bind:value={pronunciation} disabled={busy} readonly={!!submitted} aria-invalid={[...pronunciation].length>256}/>
      {#if [...pronunciation].length>256}<p class="error">Use up to 256 characters for pronunciation.</p>{/if}
      <label for="vocabulary-english">English translation</label>
      <textarea id="vocabulary-english" lang="en" rows="4" bind:value={english} disabled={busy} readonly={!!submitted} required aria-invalid={[...english].length>2000}></textarea>
      {#if [...english].length>2000}<p class="error">Use up to 2000 characters for the English translation.</p>{/if}
      {#if error}<p class="error" role="alert">{error}</p><p class="kept-entry">Your entry is kept. Retry save to confirm it.</p>{/if}
      <footer><button class="save" type="submit" disabled={busy||invalid}>{busy?'Saving…':error?'Retry save':'Save'}</button></footer>
    </form>
  </div>
</div>
{#if decision}
  <div class="vocabulary-decision-overlay">
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <div class="vocabulary-decision" role="dialog" aria-modal="true" aria-label="Unsaved vocabulary" tabindex="-1" bind:this={decisionDialog}>
      <h2>Unsaved vocabulary</h2><p>Discard this draft?</p>
      <footer><button type="button" on:click={()=>choose(false)}>Keep editing</button><button type="button" on:click={()=>choose(true)}>Discard</button></footer>
    </div>
  </div>
{/if}
<style>
  .vocabulary-drawer-layer{position:absolute;inset:0;z-index:110;pointer-events:none}.vocabulary-drawer-layer.open{pointer-events:auto}
  .vocabulary-shade{position:absolute;inset:0;background:#0005;opacity:0;transition:opacity .28s}.open>.vocabulary-shade{opacity:1}
  .vocabulary-entry-drawer{position:absolute;right:16px;top:16px;width:400px;max-width:calc(100% - 32px);max-height:calc(100% - 32px);overflow:auto;padding:24px;border:1px solid #363641;border-radius:12px;background:#17171f;color:#e5e7eb;box-shadow:0 16px 40px #0008;transform:translateX(calc(100% + 32px));transition:transform .28s cubic-bezier(.4,0,.2,1)}.vocabulary-entry-drawer.open{transform:translateX(0)}
  header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:24px}h2{font-size:16px;font-weight:600}.close{display:grid;place-items:center;width:28px;height:28px;border-radius:7px;color:#aaa}.close svg{display:block;width:20px;height:20px}.close:hover{background:#ffffff08;color:#fff}
  label{display:block;margin:18px 0 7px;font-size:13px;color:#bbb}input,textarea{display:block;width:100%;padding:10px 11px;border:1px solid #363641;border-radius:8px;background:#101017;color:#eee;font-size:14px}textarea{resize:vertical;min-height:90px;max-height:300px}input[aria-invalid='true'],textarea[aria-invalid='true']{border-color:#fca5a5}
  footer{display:flex;justify-content:flex-end;gap:8px;margin-top:24px}.save{padding:8px 16px;border-radius:8px;border:1px solid var(--accent);background:color-mix(in srgb,var(--accent) 16%,transparent);color:var(--accent-light);font-size:13px}.save:hover:not(:disabled){background:color-mix(in srgb,var(--accent) 25%,transparent)}button:disabled{opacity:.45}
  .error{margin-top:8px;color:#fca5a5;font-size:13px;overflow-wrap:anywhere}.kept-entry{margin-top:8px;color:#aaa;font-size:13px}button:focus-visible,input:focus-visible,textarea:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  .vocabulary-decision-overlay{position:fixed;inset:0;z-index:150;display:grid;place-items:center;padding:16px;background:#000b}.vocabulary-decision{width:min(400px,100%);padding:24px;border:1px solid #383848;border-radius:14px;background:#17171f;box-shadow:0 24px 70px #0008}.vocabulary-decision p{margin-top:12px;color:#bbb;font-size:14px}.vocabulary-decision button{padding:8px 12px;border:1px solid #444455;border-radius:8px;font-size:13px}
  :global(html[data-motion='reduced']) .vocabulary-drawer-layer,:global(html[data-motion='reduced']) .vocabulary-shade,:global(html[data-motion='reduced']) .vocabulary-entry-drawer{transition:none}
  @media(prefers-reduced-motion:reduce){.vocabulary-drawer-layer,.vocabulary-shade,.vocabulary-entry-drawer{transition:none}}
  @media(max-width:500px){.vocabulary-entry-drawer{padding:18px}}
</style>

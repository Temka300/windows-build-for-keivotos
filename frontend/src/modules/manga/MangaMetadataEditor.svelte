<script lang="ts">
  import {createEventDispatcher,onMount,onDestroy,tick} from 'svelte';
  import {ApiError} from '../../lib/http';
  import {mangaApi,mangaDetailFields,type MangaDetailField,type MangaDetails,type MangaFields,type MangaDetailSave} from './api';
  export let details:MangaDetails;
  export let label:string;
  let base=details,draft:MangaFields=structuredClone(details.overrides),pending:MangaDetailSave|null=null,saving=false,error='',conflict=false,alive=true;
  let dialog:HTMLDivElement,decision:((value:'save'|'keep'|'discard')=>void)|null=null;
  const dispatch=createEventDispatcher<{saved:MangaDetails;close:void}>();
  const labels:Record<MangaDetailField,string>={summary:'Description',tags:'Tags'};
  const ordered=(value:MangaFields)=>JSON.stringify(Object.fromEntries(mangaDetailFields.filter(field=>Object.hasOwn(value,field)).map(field=>[field,value[field]])));
  $: dirty=ordered(draft)!==ordered(base.overrides);
  function value(field:MangaDetailField,choices:MangaFields,reference:MangaDetails){const val=Object.hasOwn(choices,field)?choices[field]:reference.local_fields[field];return Array.isArray(val)?val.join('\n'):val??'';}
  function change(field:MangaDetailField,text:string){draft=field==='tags'?{...draft,tags:text.trim()?text.split('\n').map(tag=>tag.trim()).filter(Boolean):null}:{...draft,summary:text.trim()?text:null};if(!conflict)error='';}
  function inherit(field:MangaDetailField){const next={...draft};delete next[field];draft=next;if(!conflict)error='';}
  async function save():Promise<boolean>{
    if(saving||conflict)return false;
    if(!dirty&&!pending)return true;
    pending=pending??{source_id:base.subject.source_id,path:base.subject.path,fields:structuredClone(draft),version:base.version,source_revision:base.source_revision,request_id:crypto.randomUUID()};
    saving=true;error='';
    try{const result=await mangaApi.saveDetails(pending);if(!alive)return false;base=result;draft=structuredClone(result.overrides);pending=null;conflict=false;dispatch('saved',result);return true;}
    catch(caught){if(alive){conflict=caught instanceof ApiError&&caught.status===409;if(conflict)pending=null;error=conflict?'Details changed. Reload before saving.':'Details were not saved. Retry.';}return false;}
    finally{if(alive)saving=false;}
  }
  async function guard():Promise<boolean>{
    if(saving||decision)return false;
    if(!dirty&&!pending)return true;
    const previous=document.activeElement as HTMLElement;
    const result=await new Promise<'save'|'keep'|'discard'>(resolve=>decision=resolve);decision=null;await tick();
    if(previous?.isConnected)previous.focus({preventScroll:true});
    if(!alive||result==='keep')return false;
    if(result==='discard')return true;
    return save();
  }
  export async function beforeNavigate(){return guard();}
  async function close(){if(await guard()&&alive)dispatch('close');}
  async function reload(){
    if(!await guard()||!alive)return;
    saving=true;error='';
    try{const result=await mangaApi.details(base.subject);if(alive){base=result;draft=structuredClone(base.overrides);pending=null;conflict=false;}}
    catch{if(alive)error='Details could not be loaded. Retry Reload.';}
    finally{if(alive)saving=false;}
  }
  function controls(){return Array.from(dialog.querySelectorAll<HTMLElement>('button:not(:disabled),textarea:not(:disabled)')).filter(node=>!node.closest('[inert]'));}
  function keys(event:KeyboardEvent){
    if(event.key==='Escape'){event.preventDefault();event.stopPropagation();if(decision)decision('keep');else void close();return;}
    if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='s'){event.preventDefault();if(!decision)void save();return;}
    if(event.key!=='Tab')return;
    const buttons=decision?Array.from(dialog.querySelectorAll<HTMLElement>('.decision button:not(:disabled)')):controls();
    if(event.shiftKey&&(document.activeElement===buttons[0]||document.activeElement===dialog)){event.preventDefault();buttons.at(-1)?.focus();}
    else if(!event.shiftKey&&(document.activeElement===buttons.at(-1)||document.activeElement===dialog)){event.preventDefault();buttons[0]?.focus();}
  }
  function unload(event:BeforeUnloadEvent){if(dirty||pending||saving){event.preventDefault();event.returnValue='';}}
  $: if(decision)void tick().then(()=>dialog?.querySelector<HTMLButtonElement>('[data-keep-editing]')?.focus());
  onMount(async()=>{await tick();dialog?.querySelector<HTMLTextAreaElement>('textarea')?.focus();});
  onDestroy(()=>{alive=false;decision?.('keep');decision=null;});
</script>
<svelte:window on:beforeunload={unload}/>
<div class="metadata-editor-scrim">
  <!-- svelte-ignore a11y_no_noninteractive_tabindex a11y_no_noninteractive_element_interactions -->
  <div class="metadata-editor" role="dialog" aria-modal="true" aria-label="Edit manga details" tabindex="-1" bind:this={dialog} on:keydown={keys}>
    <div class="editor-content" inert={!!decision}>
      <h2>Edit details</h2><p class="subject" title={label}>{label}</p>
      <span id="manga-detail-list-hint" class="sr-only">One tag per line</span>
      <div class="fields">
        {#each mangaDetailFields as field}
          <div class="field-heading"><label for={'manga-detail-'+field}>{labels[field]}</label><button type="button" disabled={saving||!!pending||!Object.hasOwn(draft,field)} on:click={()=>inherit(field)}>Use local</button></div>
          <textarea id={'manga-detail-'+field} rows={field==='summary'?4:2} maxlength={field==='summary'?16384:65599} aria-describedby={field==='tags'?'manga-detail-list-hint':undefined} disabled={saving||!!pending} value={value(field,draft,base)} on:input={event=>change(field,event.currentTarget.value)}></textarea>
        {/each}
      </div>
      {#if error}<p role="alert">{error} {#if conflict}<button type="button" disabled={saving} on:click={reload}>Reload</button>{/if}</p>{/if}
      <div class="actions"><button type="button" disabled={saving} on:click={close}>Cancel</button><button type="button" disabled={saving||conflict||(!dirty&&!pending)} on:click={save}>{saving?'Saving…':pending?'Retry':'Save'}</button></div>
    </div>
    {#if decision}<div class="decision" role="alertdialog" aria-modal="true" aria-label="Unsaved manga details"><p>Save your changes?</p><div><button disabled={conflict} on:click={()=>decision?.('save')}>Save</button><button data-keep-editing on:click={()=>decision?.('keep')}>Keep editing</button><button on:click={()=>decision?.('discard')}>Discard changes</button></div></div>{/if}
  </div>
</div>
<style>
 .sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}
 .metadata-editor-scrim{position:fixed;inset:0;z-index:140;background:#000a;display:grid;place-items:center;padding:16px}.metadata-editor{position:relative;background:#19191f;color:#eee;border-radius:14px;width:min(560px,100%);max-height:calc(100dvh - 32px);overflow:auto;padding:24px}h2{font-size:18px;font-weight:600}.subject{font-size:13px;color:#aaa;margin:6px 0 20px;overflow-wrap:anywhere}.fields{display:grid;gap:7px}.field-heading{display:flex;align-items:center;justify-content:space-between;margin-top:8px;gap:12px;font-size:13px}.field-heading button{font-size:11px;color:var(--accent)}textarea{width:100%;min-width:0;box-sizing:border-box;background:#111117;border:1px solid #383840;border-radius:6px;padding:8px 10px;font-size:13px;resize:vertical;min-height:64px}.actions{display:flex;justify-content:flex-end;gap:12px;margin-top:20px}.actions button,.decision button{border:1px solid #3b3b48;padding:8px 12px;border-radius:7px;font-size:13px}.actions button:last-child{color:var(--accent)}[role='alert']{font-size:13px;margin-top:14px;color:#e8b6bb}[role='alert'] button{text-decoration:underline}.decision{position:absolute;inset:0;z-index:1;display:flex;flex-direction:column;justify-content:center;align-items:center;gap:18px;background:#19191ff5;padding:24px;text-align:center}.decision>div{display:flex;flex-wrap:wrap;justify-content:center;gap:10px}button:disabled,textarea:disabled{opacity:.45}button:focus-visible,textarea:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
 @media(pointer:coarse){.field-heading button,.actions button,.decision button{min-height:44px}textarea{font-size:16px}}
</style>

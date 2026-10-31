<script lang="ts">
  import {createEventDispatcher,onMount,onDestroy,tick} from 'svelte';
  import {mangaApi,type Manga,type MangaDetails} from './api';
  export let manga:Manga;
  export let inactive=false;
  let details:MangaDetails|null=null,loading=false,error='',failedRefresh=false,expanded=false,clipped=false;
  let description:HTMLParagraphElement,editButton:HTMLButtonElement,alive=true,serial=0;
  let request:AbortController|null=null;
  const dispatch=createEventDispatcher<{edit:MangaDetails;metadata:MangaDetails;tag:string}>();
  $: hasDetails=!!details&&(!!details.fields.summary||!!details.fields.tags?.length);
  $: summary=details?.fields.summary??'';
  $: {summary;expanded;void measure();}
  async function measure(){await tick();if(description&&!expanded)clipped=description.scrollHeight>description.clientHeight+1;}
  export function acceptDetails(value:MangaDetails){
    if(value.subject.source_id!==manga.source_id||value.subject.path!==manga.path)return;
    request?.abort();serial++;loading=false;error='';details=value;expanded=false;
  }
  export function focusEdit(){editButton?.focus({preventScroll:true});}
  export function refreshSavedDetails(){if(!inactive&&!loading)void readDetails();}
  async function restoreFocus(previous:Element|null){
    await tick();if(alive&&!inactive&&previous instanceof HTMLElement&&previous.isConnected&&document.activeElement===document.body)previous.focus({preventScroll:true});
  }
  async function readDetails(){
    request?.abort();const current=new AbortController(),token=++serial;request=current;
    loading=true;error='';failedRefresh=false;const previous=document.activeElement;
    try{const result=await mangaApi.details(manga,current.signal);if(alive&&token===serial)details=result;}
    catch{if(alive&&token===serial&&!current.signal.aborted)error='Details could not be loaded.';}
    finally{if(alive&&token===serial){loading=false;void restoreFocus(previous);}}
  }
  async function refresh(){
    if(inactive||loading)return;
    request?.abort();const token=++serial;loading=true;error='';failedRefresh=true;const previous=document.activeElement;
    try{const result=await mangaApi.refreshDetails(manga);if(alive&&token===serial){details=result;expanded=false;dispatch('metadata',result);}}
    catch{if(alive&&token===serial)error='Local details could not be refreshed.';}
    finally{if(alive&&token===serial){loading=false;void restoreFocus(previous);}}
  }
  onMount(()=>{void readDetails();const resize=new ResizeObserver(()=>void measure());if(description)resize.observe(description);return()=>resize.disconnect();});
  onDestroy(()=>{alive=false;serial++;request?.abort();});
</script>

<svelte:window on:resize={()=>void measure()}/>
<aside class="manga-details-pane" class:has-details={hasDetails} aria-label="Manga details" aria-busy={loading}>
  <div class="chapter-cover"><img alt={manga.name} src={mangaApi.page(manga,manga.cover,0,true)} on:load={event=>(event.currentTarget as HTMLImageElement).style.visibility=''} on:error={event=>(event.currentTarget as HTMLImageElement).style.visibility='hidden'}/></div>
  <div class="metadata-actions"><button disabled={loading||inactive} on:click={refresh}>{loading?'Loading…':'Refresh'}</button><button bind:this={editButton} disabled={!details||loading||inactive} on:click={()=>details&&dispatch('edit',details)}>Edit</button></div>
  {#if error}<p role="alert">{error} <button disabled={loading||inactive} on:click={()=>failedRefresh?refresh():readDetails()}>Retry</button></p>{/if}
  {#if hasDetails}
    <div class="metadata-details">
      {#if details?.fields.tags?.length}<div class="detail-tags" aria-label="Tags">{#each details.fields.tags as tag}<button disabled={inactive} on:click={()=>dispatch('tag',tag)}>{tag}</button>{/each}</div>{/if}
      {#if summary}<p class="detail-summary" class:expanded bind:this={description}>{summary}</p>{#if expanded||clipped}<button class="more-description" aria-expanded={expanded} on:click={()=>expanded=!expanded}>{expanded?'Less':'More'}</button>{/if}{/if}
    </div>
  {/if}
</aside>

<style>
 .manga-details-pane{height:100%;min-width:0;min-height:0;overflow-y:auto;overflow-x:hidden;overscroll-behavior:contain;padding:0 2px;scrollbar-width:thin}
 .chapter-cover{width:100%;max-width:230px;aspect-ratio:2/3;max-height:calc(100% - 36px);min-height:0;margin:0 auto;background:#24242d;border-radius:6px;overflow:hidden}.chapter-cover img{width:100%;height:100%;object-fit:contain}.has-details .chapter-cover{max-width:140px;max-height:none}
 .metadata-actions{display:flex;justify-content:center;gap:12px;padding:7px 0 9px;font-size:12px;color:#aaa}.metadata-actions button{padding:3px 0}.metadata-actions button:hover{color:#eee}
 .metadata-details{min-width:0;font-size:13px;line-height:1.45;overflow-wrap:anywhere;padding-bottom:4px}.detail-tags{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}.detail-tags button{max-width:100%;overflow-wrap:anywhere;text-align:left;background:color-mix(in srgb,var(--accent) 13%,#19191f);color:var(--accent);border-radius:6px;padding:4px 8px;font-size:12px}
 .detail-summary{margin:0;white-space:pre-wrap;color:#aaa;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:3;line-clamp:3;overflow:hidden}.detail-summary.expanded{display:block;line-clamp:unset;-webkit-line-clamp:unset}.more-description{font-size:12px;color:var(--accent);padding:5px 0 0}
 [role='alert']{font-size:12px;line-height:1.5;overflow-wrap:anywhere;color:#e8b6bb;margin:4px 0 10px}[role='alert'] button{text-decoration:underline}button:disabled{opacity:.45}button:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
 @media(max-width:560px){.chapter-cover{max-width:110px}.has-details .chapter-cover{max-width:90px}.metadata-actions{gap:8px}}
 @media(pointer:coarse){.metadata-actions button,.detail-tags button,.more-description{min-height:44px}}
</style>

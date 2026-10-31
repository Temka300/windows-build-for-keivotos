<script lang="ts">
  import {createEventDispatcher,onMount,onDestroy,tick} from 'svelte';
  import {ApiError} from '../../lib/http';
  import {youtubeApi,type Download,type SavedDetails} from './api';
  export let item:Download;
  let value:SavedDetails|null=null,loading=true,error='',alive=true,request:AbortController|null=null;
  let dialog:HTMLDivElement;
  const dispatch=createEventDispatcher<{close:void}>();
  const count=(number:number)=>new Intl.NumberFormat().format(number);
  function date(stamp:string){const value=new Date(stamp);return`${value.getFullYear()}/${String(value.getMonth()+1).padStart(2,'0')}/${String(value.getDate()).padStart(2,'0')}`;}
  async function read(){
    request?.abort();const current=new AbortController();request=current;loading=true;error='';
    try{const result=await youtubeApi.details(item.id,current.signal);if(alive&&!current.signal.aborted)value=result;}
    catch(caught){if(alive&&!current.signal.aborted){value=null;if(caught instanceof ApiError&&[404,409].includes(caught.status))dispatch('close');else error='Saved details could not be loaded.';}}
    finally{if(alive&&request===current){loading=false;await tick();if(alive&&document.activeElement===document.body)dialog?.focus({preventScroll:true});}}
  }
  function keys(event:KeyboardEvent){
    if(event.key==='Escape'){event.preventDefault();event.stopPropagation();dispatch('close');return;}
    if(event.key!=='Tab')return;
    const controls=Array.from(dialog.querySelectorAll<HTMLElement>('button:not(:disabled),a[href]'));
    if(event.shiftKey&&(document.activeElement===controls[0]||document.activeElement===dialog)){event.preventDefault();controls.at(-1)?.focus();}
    else if(!event.shiftKey&&(document.activeElement===controls.at(-1)||document.activeElement===dialog)){event.preventDefault();controls[0]?.focus();}
  }
  function focus(){if(!loading)void read();}
  onMount(async()=>{void read();await tick();dialog?.focus();});
  onDestroy(()=>{alive=false;request?.abort();});
</script>
<svelte:window on:focus={focus}/>
<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<div class="details-scrim" on:click={event=>{if(event.target===event.currentTarget)dispatch('close');}}>
  <!-- svelte-ignore a11y_no_noninteractive_tabindex a11y_no_noninteractive_element_interactions -->
  <div class="youtube-details" role="dialog" aria-modal="true" aria-label="YouTube details" tabindex="-1" bind:this={dialog} on:keydown={keys}>
    <header><h2>{item.title||'YouTube video'}</h2>{#if value?.captured_at}<p class="capture-date">Captured {date(value.captured_at)}</p>{/if}</header>
    <div class="details-body" aria-busy={loading}>
      {#if error}<p role="alert">{error} <button disabled={loading} on:click={read}>Retry</button></p>{/if}
      {#if loading&&!value}<p class="muted" role="status">Loading…</p>{/if}
      {#if value}
        {#if value.like_count!==null||value.dislike_count!==null}<div class="counts" aria-label="Captured ratings">{#if value.like_count!==null}<span>{count(value.like_count)} likes</span>{/if}{#if value.dislike_count!==null}<span>{count(value.dislike_count)} dislikes</span>{/if}</div>{/if}
        {#if value.description_available}<p class="description">{value.description}</p>
        {:else if value.description_status==='unavailable'}<p class="muted">Saved description is unavailable.</p>
        {:else}<p class="muted">No saved description.</p>{/if}
      {/if}
    </div>
    <footer>{#if value?.description_available}<a href={youtubeApi.descriptionUrl(item.id)} download>Download description</a>{/if}<button on:click={()=>dispatch('close')}>Close</button></footer>
  </div>
</div>
<style>
 .details-scrim{position:fixed;inset:0;z-index:140;background:#000a;display:grid;place-items:center;padding:16px}.youtube-details{display:flex;flex-direction:column;min-width:0;width:min(640px,100%);max-height:calc(100dvh - 32px);background:#19191f;color:#eee;border-radius:14px;padding:24px;gap:18px}header{min-width:0}h2{font-size:18px;font-weight:600;overflow-wrap:anywhere;margin:0}.capture-date{font-size:12px;color:#aaa;margin:8px 0 0}.details-body{min-height:0;overflow-y:auto;overscroll-behavior:contain;font-size:14px;line-height:1.5}.counts{display:flex;flex-wrap:wrap;gap:18px;margin-bottom:16px;font-size:13px;color:#c8c8d0}.description{white-space:pre-wrap;overflow-wrap:anywhere;margin:0}.muted{color:#aaa;margin:0}footer{display:flex;flex-wrap:wrap;justify-content:flex-end;align-items:center;gap:12px}footer a,footer button{padding:8px 12px;border:1px solid #3b3b48;border-radius:7px;font-size:13px}footer a{color:var(--accent);text-decoration:none}button:focus-visible,a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}[role='alert']{color:#e8b6bb;overflow-wrap:anywhere}[role='alert'] button{text-decoration:underline}
 @media(max-width:400px){.youtube-details{padding:18px;gap:14px}footer{gap:8px}footer a,footer button{padding:8px}}
 @media(pointer:coarse){footer a,footer button{min-height:44px}}
</style>

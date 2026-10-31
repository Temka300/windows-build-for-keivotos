<script lang="ts">
  import {createEventDispatcher,onMount,onDestroy,tick} from 'svelte';
  import {filesApi} from '../../lib/filesApi';
  import {videoApi,type VideoTitle,type VideoEpisode,type VideoItem,type WatchPosition,type VideoDetailSubject,type VideoDetails} from './api';
  import VideoMetadataEditor from './VideoMetadataEditor.svelte';
  export let title:VideoTitle;
  export let items:VideoEpisode[]=[];
  export let total=0;
  export let loading=false;
  export let error='';
  export let positions:Record<string,WatchPosition|null>={};
  export let progressError='';
  export let inactive=false;
  export let preparing=false;
  export let scrollTop=0;
  let dialog:HTMLDivElement,list:HTMLDivElement,editor:VideoMetadataEditor,editButton:HTMLButtonElement;
  let detailSubject:VideoDetailSubject={source_id:title.source_id,path:title.path,kind:title.kind},detailLabel=title.name;
  let details:VideoDetails|null=null,metadataLoading=false,metadataError='',metadataFailedRefresh=false,editing=false,metadataSerial=0,alive=true;
  let metadataRequest:AbortController|null=null;
  $: hasDetails=!!details&&Object.values(details.fields).some(value=>Array.isArray(value)?value.length:!!value);
  const tagFields=['tags'] as const;
  const detailText=(value:unknown)=>Array.isArray(value)?value.join(', '):String(value??'');
  const detailTags=(value:unknown)=>Array.isArray(value)?value as string[]:[];
  export async function beforeNavigate(){return !editing||await editor?.beforeNavigate();}
  export function refreshSavedDetails(){if(!editing&&!metadataLoading)void readDetails();}
  async function restoreMetadataFocus(){await tick();if(alive&&!inactive&&!editing&&document.activeElement===document.body)dialog?.focus({preventScroll:true});}
  async function readDetails(subject:VideoDetailSubject=detailSubject,label=detailLabel){
    if(!await beforeNavigate())return;
    metadataRequest?.abort();const request=new AbortController(),serial=++metadataSerial;metadataRequest=request;
    const changed=JSON.stringify(subject)!==JSON.stringify(detailSubject);detailSubject=subject;detailLabel=label;if(changed)details=null;
    metadataLoading=true;metadataError='';metadataFailedRefresh=false;
    try{const result=await videoApi.details(subject,request.signal);if(alive&&serial===metadataSerial){details=result;detailLabel=label;}}
    catch{if(alive&&serial===metadataSerial&&!request.signal.aborted)metadataError='Details could not be loaded.';}
    finally{if(alive&&serial===metadataSerial){metadataLoading=false;void restoreMetadataFocus();}}
  }
  async function refreshDetails(){
    metadataRequest?.abort();const serial=++metadataSerial;metadataLoading=true;metadataError='';metadataFailedRefresh=true;
    try{const result=await videoApi.refreshDetails(detailSubject);if(alive&&serial===metadataSerial){details=result;dispatch('metadata',result);}}
    catch{if(alive&&serial===metadataSerial)metadataError='Local details could not be refreshed.';}
    finally{if(alive&&serial===metadataSerial){metadataLoading=false;void restoreMetadataFocus();}}
  }
  async function editorClosed(){editing=false;void readDetails();await tick();editButton?.focus({preventScroll:true});}
  async function saved(event:CustomEvent<VideoDetails>){details=event.detail;detailLabel=detailSubject.path.split('/').at(-1)??title.name;dispatch('metadata',event.detail);await editorClosed();}

  const dispatch=createEventDispatcher<{close:void;play:VideoEpisode;more:void;retry:void;progressRetry:void;metadata:VideoDetails;tag:string;location:{event:MouseEvent|KeyboardEvent;item:VideoItem}}>();
  let previousError='';
  async function restoreRetryFocus(){await tick();if(!inactive&&dialog?.isConnected&&!dialog.contains(document.activeElement))dialog.focus({preventScroll:true});}
  $: {if(previousError&&!error)void restoreRetryFocus();previousError=error;}
  const episodeName=(name:string)=>name.replace(/\.(mp4|m4v|webm|mkv|mov|avi|ogv)$/i,'');
  const coverUrl=()=>filesApi.thumbnailUrl(title.cover.source_id,title.cover.path,600,`${title.cover.mtime}:${title.cover.size}`);
  export function focusItem(item?:VideoItem){
    const row=item&&Array.from(list?.querySelectorAll<HTMLButtonElement>('[data-episode-path]')??[]).find(node=>node.dataset.episodePath===item.path);
    if(row)row.focus({preventScroll:true});else dialog?.focus({preventScroll:true});
  }
  function keys(event:KeyboardEvent){
    if(event.key==='Escape'){event.preventDefault();event.stopPropagation();dispatch('close');return;}
    if(event.key!=='Tab')return;
    const buttons=Array.from(dialog.querySelectorAll<HTMLButtonElement>('button:not(:disabled)'));
    const first=buttons[0],last=buttons.at(-1);
    if(!first){event.preventDefault();dialog.focus();}
    else if(event.shiftKey&&(document.activeElement===first||document.activeElement===dialog)){event.preventDefault();last?.focus();}
    else if(!event.shiftKey&&(document.activeElement===last||document.activeElement===dialog)){event.preventDefault();first.focus();}
  }
  function location(event:MouseEvent|KeyboardEvent,item:VideoEpisode){event.preventDefault();event.stopPropagation();dispatch('location',{event,item});}
  function rowKey(event:KeyboardEvent,item:VideoEpisode){if(event.key==='ContextMenu'||event.shiftKey&&event.key==='F10')location(event,item);}
  onMount(async()=>{void readDetails();await tick();list.scrollTop=scrollTop;dialog.focus({preventScroll:true});});
  onDestroy(()=>{alive=false;metadataSerial++;metadataRequest?.abort();});
</script>

<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<div class="video-info-scrim" inert={inactive||editing} on:click={event=>{if(event.target===event.currentTarget)dispatch('close');}}>
  <!-- svelte-ignore a11y_no_noninteractive_tabindex a11y_no_noninteractive_element_interactions -->
  <div class="video-info" role="dialog" aria-modal="true" aria-label={(title.name)+' episodes'} tabindex="-1" bind:this={dialog} on:keydown={keys}>
    <div class="info-heading">
      <button class="back" aria-label="Back to videos" on:click={()=>dispatch('close')}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m14 5-7 7 7 7"/></svg></button>
      <h2 title={title.name}>{title.name}</h2>
    </div>
    <div class="info-aside">
      <div class="info-cover" class:has-details={hasDetails} class:poster={['jpg','jpeg','png','webp','avif'].includes(title.cover.ext)}><span aria-hidden="true">▷</span>{#key coverUrl()}<img alt="" src={coverUrl()} on:error={event=>(event.currentTarget as HTMLImageElement).style.display='none'}/>{/key}</div>
      <div class="metadata-actions"><button disabled={metadataLoading} on:click={refreshDetails}>{metadataLoading?'Loading…':'Refresh'}</button><button bind:this={editButton} disabled={!details||metadataLoading} on:click={()=>editing=true}>Edit</button></div>
      {#if metadataError}<p class="metadata-error" role="alert">{metadataError} <button disabled={metadataLoading} on:click={()=>metadataFailedRefresh?refreshDetails():readDetails()}>Retry</button></p>{/if}
      {#if details}
        <div class="metadata-details" aria-label="Video details">

          {#if details.fields.summary}<p class="detail-summary">{detailText(details.fields.summary)}</p>{/if}
          {#each tagFields as field}{#if detailTags(details.fields[field]).length}<div class="detail-tag-group"><span class="detail-label">Tags</span><div class="detail-tags">{#each detailTags(details.fields[field]) as tag}<button on:click={()=>dispatch('tag',tag)}>{tag}</button>{/each}</div></div>{/if}{/each}
        </div>
      {/if}
    </div>
    <div class="episode-list" bind:this={list} aria-busy={loading||preparing} on:scroll={()=>scrollTop=list.scrollTop}>
      {#if error}<p role="alert">{error} <button on:click={()=>dispatch('retry')}>Retry</button></p>{/if}
      {#if progressError}<p role="alert">{progressError} <button on:click={()=>dispatch('progressRetry')}>Retry</button></p>{/if}
      {#if loading&&!items.length}<p class="muted">Loading…</p>{/if}
      {#each items as item (`${item.source_id}/${item.path}`)}
        {@const saved=positions[JSON.stringify([item.source_id,item.path])]}
        <button class="episode-row" aria-label={episodeName(item.name)} data-episode-path={item.path} title={episodeName(item.name)} disabled={preparing||!!error} on:click={()=>dispatch('play',item)} on:contextmenu={event=>location(event,item)} on:keydown={event=>rowKey(event,item)}>
          <span>{episodeName(item.name)}</span>
          {#if saved}<span class="episode-progress" role="progressbar" aria-label={episodeName(item.name)+' watch progress'} aria-valuemin="0" aria-valuemax={saved.duration} aria-valuenow={saved.position}><span style={`width:${Math.min(100,saved.position/saved.duration*100)}%`}></span></span>{/if}
        </button>
      {/each}
      {#if items.length<total}<button class="episode-more" disabled={loading||preparing} on:click={()=>dispatch('more')}>{loading?'Loading…':'Load more'}</button>{/if}
    </div>
  </div>
</div>

{#if editing&&details}<VideoMetadataEditor bind:this={editor} {details} label={detailLabel} on:saved={saved} on:close={editorClosed}/>{/if}

<style>
 .video-info-scrim{position:fixed;inset:0;z-index:110;display:grid;place-items:center;background:#000b;padding:24px}
 .video-info{position:relative;display:grid;grid-template-columns:minmax(0,230px) minmax(0,1fr);grid-template-rows:minmax(0,1fr);gap:24px;width:min(760px,100%);height:min(500px,80dvh);max-height:calc(100dvh - 48px);overflow:hidden;padding:88px 24px 24px;background:#19191f;border-radius:14px;color:#eee}
 .info-heading{position:absolute;top:20px;left:24px;right:24px;display:flex;align-items:center;gap:12px;min-width:0}
 h2{font-size:18px;font-weight:600;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .back{flex-shrink:0;display:grid;place-items:center;width:36px;height:36px;border-radius:18px;transition:background .18s}
 .back:hover,.back:focus-visible{background:#ffffff12}
 svg{width:24px;height:24px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
 .info-aside{min-height:0;overflow:auto;overscroll-behavior:contain;padding-right:3px}.info-cover.has-details{max-height:170px}
 .info-cover{position:relative;display:grid;place-items:center;aspect-ratio:16/9;width:100%;max-height:100%;align-self:start;background:#24242d;border-radius:6px;overflow:hidden;color:#9292a5;font-size:32px}
 .info-cover.poster{aspect-ratio:2/3}
 .info-cover img{position:absolute;width:100%;height:100%;object-fit:contain}
 .episode-list{overflow-y:auto;overflow-x:hidden;min-width:0;min-height:0;overscroll-behavior:contain;scrollbar-gutter:stable;padding:4px}
 .episode-row{position:relative;display:block;width:100%;min-height:46px;padding:13px 12px;text-align:left;border-radius:6px;font-size:14px;line-height:20px;overflow-wrap:anywhere}
 .episode-row>span:first-child{min-width:0}.episode-row:hover{background:#ffffff0b}
 .episode-progress{position:absolute;left:8px;right:8px;bottom:0;height:4px;background:#ffffff14;border-radius:2px;overflow:hidden}.episode-progress>span{display:block;height:100%;background:var(--accent)}
 .metadata-actions{display:flex;gap:12px;margin:12px 0;font-size:12px;color:#aaa}.metadata-actions button:hover,.metadata-actions button:focus-visible{color:var(--accent)}.metadata-details{font-size:12px;line-height:1.5;overflow-wrap:anywhere;display:grid;gap:12px}.detail-label{color:#aaa}.detail-summary{white-space:pre-wrap}.detail-label{font-size:11px}.detail-tags{display:flex;flex-wrap:wrap;gap:5px;margin-top:4px}.detail-tags button{border:1px solid #3b3b46;border-radius:5px;padding:2px 6px;max-width:100%;overflow-wrap:anywhere;color:var(--accent);font-size:11px}.metadata-error{font-size:12px;color:#d9a4aa}.metadata-error button{text-decoration:underline}
 .muted{color:#9292a5}.episode-more{display:block;margin:16px auto;padding:8px 12px;border:1px solid #303040;border-radius:8px}
 button:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}button:disabled{opacity:.45}
 [role='alert']{padding:8px;overflow-wrap:anywhere}[role='alert'] button{text-decoration:underline}
 :global(html[data-motion='reduced']) .back{transition:none}
 @media(prefers-reduced-motion:reduce){.back{transition:none}}
 @media(max-width:560px){.video-info-scrim{padding:12px}.video-info{grid-template-columns:minmax(0,100px) minmax(0,1fr);gap:12px;padding:80px 12px 16px;max-height:calc(100dvh - 24px)}.info-heading{left:12px;right:12px;top:16px}.episode-row{padding:13px 8px}h2{font-size:16px}}
</style>

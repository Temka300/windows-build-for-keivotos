<script lang="ts">
  import {onMount,onDestroy,tick} from 'svelte';
  import AppDrawer from '../../components/AppDrawer.svelte';
  import ModuleSidebarDock from '../../components/ModuleSidebarDock.svelte';
  import {moduleSidebarPreferences} from '../../lib/moduleSidebarPreferences';
  import {settingsOpen,settingsInitialSection} from '../../lib/suiteStores';
  import {persistentStorageKey} from '../../lib/product';
  import {notify,updateNotification} from '../../lib/notifications';
  import {ApiError} from '../../lib/http';
  import TwitterSidebar from './TwitterSidebar.svelte';
  import TwitterPost from './TwitterPost.svelte';
  import TwitterAuthorView from './TwitterAuthorView.svelte';
  import {twitterApi,postUrl,captureKey,type Capture,type Download,type Preferences} from './api';

  type Screen={section:'posts'|'authors';author:Capture|null;mode:'feed'|'detail';detail:Capture|null};
  type Frame={screen:Screen;items:Capture[];cursor:number|null;query:string;search:string;scroll:number;focus:{post:string;author:string;label:string};error:string;version:number};
  type Submission={url:string;source_id:string;request_id:string};
  let screen:Screen={section:'posts',author:null,mode:'feed',detail:null};
  let history:Frame[]=[],items:Capture[]=[],cursor:number|null=0,query='',error='',loading=false,detailLoading=false;
  let main:HTMLElement,drawer=false,alive=true,epoch=0,dataVersion=0;
  let search='',searchComposing=false,searchPending=false;
  let preferences:Preferences|null=null,preferenceLoading=true,preferenceError='';
  let activeJobs=new Set<string>(),notifiedJobs=new Set<string>(),jobRevision=0,jobPolling=false,statusUnavailable=false;
  let submission:Submission|null=null,submitting=false,submitError='';
  let archiveRequest:AbortController|null=null,detailRequest:AbortController|null=null,jobRequest:AbortController|null=null,preferenceRequest:AbortController|null=null;
  let debounce:ReturnType<typeof setTimeout>|undefined,timer:ReturnType<typeof setTimeout>|undefined;
  const receiptKey=persistentStorageKey('twitter-pending-request');
  const activeKey=persistentStorageKey('twitter-active-downloads');
  $: url=postUrl(query);
  $: ready=!!preferences?.available&&!!preferences.dependencies.ready&&!preferenceLoading;
  $: setupError=preferenceError||(!preferences?.available?'Choose a save folder in Twitter settings.':!preferences.dependencies.ready?'Missing capture dependencies: '+preferences.dependencies.missing.join(', '):'');
  $: title=screen.mode==='detail'?'Post':screen.author?screen.author.author.name:screen.section==='authors'?'Authors':'Saved posts';

  function rememberSubmission(value:Submission|null){
    submission=value;
    try{if(value)sessionStorage.setItem(receiptKey,JSON.stringify(value));else sessionStorage.removeItem(receiptKey);}catch{/* The current-page receipt remains usable. */}
  }
  function stopReads(){archiveRequest?.abort();detailRequest?.abort();archiveRequest=null;detailRequest=null;clearTimeout(debounce);epoch++;loading=false;detailLoading=false;}
  function remember(){
    const active=document.activeElement as HTMLElement|null;
    const focus={post:active?.closest<HTMLElement>('[data-twitter-post]')?.dataset.twitterPost??'',author:active?.closest<HTMLElement>('[data-twitter-author]')?.dataset.twitterAuthor??'',label:active?.getAttribute('aria-label')??''};
    history=[...history,{screen:{...screen},items,cursor,query,search,scroll:main?.scrollTop??0,focus,error,version:dataVersion}];
  }
  async function focusMain(serial=epoch){await tick();if(alive&&serial===epoch){main?.focus({preventScroll:true});main.scrollTop=0;}}
  function selectSection(section:'posts'|'authors'){
    stopReads();settleSearch();history=[];screen={section,author:null,mode:'feed',detail:null};items=[];cursor=0;error='';
    if(window.matchMedia('(max-width:700px)').matches)moduleSidebarPreferences('twitter').open.set(false);
    void load();void focusMain();
  }
  async function back(){
    const previous=history.at(-1);if(!previous)return;
    stopReads();const serial=epoch;history=history.slice(0,-1);screen=previous.screen;items=previous.items;cursor=previous.cursor;if(!submission)query=previous.query;search=previous.search;searchPending=false;error=previous.error;
    if(previous.version!==dataVersion){
      items=[];
      if(screen.mode==='feed'){
        cursor=0;await load();
        while(alive&&serial===epoch&&cursor!==null&&!error&&cursor<(previous.cursor??Number.MAX_SAFE_INTEGER))await load(true);
      }else if(screen.detail){
        const item=screen.detail;screen={...screen,detail:null};const request=new AbortController();detailRequest=request;
        try{const fresh=await twitterApi.capture(item.source_id,item.path,request.signal);if(alive&&!request.signal.aborted&&serial===epoch)screen={...screen,detail:fresh};}catch(e){if(alive&&!request.signal.aborted&&serial===epoch)error=e instanceof Error?e.message:'Saved post is unavailable.';}
      }
    }
    await tick();if(!alive||serial!==epoch)return;main.scrollTop=previous.scroll;
    const post=[...main.querySelectorAll<HTMLElement>('[data-twitter-post]')].find(node=>node.dataset.twitterPost===previous.focus.post);
    const anchor=previous.focus.author?[...main.querySelectorAll<HTMLElement>('[data-twitter-author]')].find(node=>node.dataset.twitterAuthor===previous.focus.author):post?[...post.querySelectorAll<HTMLElement>('button')].find(node=>node.getAttribute('aria-label')===previous.focus.label):null;
    (anchor??main).focus({preventScroll:true});
  }
  function settleSearch(){
    if(!searchComposing&&!postUrl(query))search=query.trim();
    searchPending=false;
  }
  function input(event?:Event){
    if(event)query=(event.currentTarget as HTMLInputElement).value;
    stopReads();
    // A URL keeps the saved feed in place and never requests X while typing.
    if(postUrl(query)){searchPending=false;return;}
    searchPending=true;loading=true;
    if(searchComposing)return;
    debounce=setTimeout(()=>{
      if(!alive||searchComposing)return;
      settleSearch();if(!screen.author)history=[];
      screen={...screen,mode:'feed',detail:null};void load();
    },180);
  }
  function beginSearchComposition(){searchComposing=true;input();}
  function endSearchComposition(){searchComposing=false;input();}
  async function load(more=false,refresh=false){
    if(screen.mode!=='feed'||searchComposing||searchPending)return;
    if(search.length>256){loading=false;error='Search accepts up to 256 characters.';return;}
    archiveRequest?.abort();const request=new AbortController();archiveRequest=request;
    const serial=epoch,filter=search,author=screen.author?.author_key??'';
    loading=true;error='';
    let offset=more?cursor:0;if(offset===null){loading=false;return;}
    try{
      let result=await twitterApi.library(offset,filter,author,request.signal);
      const bound=result.total,target=refresh?items.length:0;let rows=result.items;
      // Empty filtered pages still advance across admitted candidates. Stop at
      // the first match or the finite count acknowledged by the first page.
      while((!rows.length||rows.length<target)&&result.next_offset!==null&&result.next_offset<bound){
        if(result.next_offset<=offset)throw new Error('Saved posts changed while loading. Retry.');
        offset=result.next_offset;result=await twitterApi.library(offset,filter,author,request.signal);rows=[...rows,...result.items];
      }
      if(!alive||request.signal.aborted||serial!==epoch)return;
      items=[...new Map((more?[...items,...rows]:rows).map(item=>[captureKey(item),item])).values()];cursor=result.next_offset;
    }catch(e){if(alive&&!request.signal.aborted&&serial===epoch)error=e instanceof Error?e.message:'Could not load saved posts.';}
    finally{if(archiveRequest===request)loading=false;}
  }
  async function openPost(item:Capture,event?:MouseEvent){
    remember();stopReads();screen={...screen,mode:'detail',detail:null};detailLoading=true;error='';
    const request=new AbortController();detailRequest=request;const serial=epoch;
    await focusMain();
    if(!alive||request.signal.aborted||serial!==epoch)return;
    try{const detail=await twitterApi.capture(item.source_id,item.path,request.signal);if(alive&&!request.signal.aborted&&serial===epoch)screen={...screen,detail};}
    catch(e){if(alive&&!request.signal.aborted&&serial===epoch)error=e instanceof Error?e.message:'Saved post is unavailable.';}
    finally{if(detailRequest===request)detailLoading=false;}
  }
  function openAuthor(item:Capture,event:MouseEvent){remember();stopReads();settleSearch();screen={section:'posts',mode:'feed',author:item,detail:null};items=[];cursor=0;error='';void load();void focusMain();}
  function rememberActive(){
    try{if(activeJobs.size)sessionStorage.setItem(activeKey,JSON.stringify([...activeJobs]));else sessionStorage.removeItem(activeKey);}catch{/* Current-page tracking remains available. */}
  }
  function observe(job:Download){
    const id='twitter-job-'+job.id;
    if(['queued','running'].includes(job.status)){
      activeJobs.add(job.id);
      if(!notifiedJobs.has(job.id)){notify('Saving post '+job.post_id+'…',{id,progress:job.progress,destination:'downloads'});notifiedJobs.add(job.id);}
      else updateNotification(id,{message:'Saving post '+job.post_id+'…',tone:'info',progress:job.progress,destination:'downloads'});
    }else{
      activeJobs.delete(job.id);notifiedJobs.delete(job.id);
      if(job.status==='complete'){
        notify('Post saved: '+job.post_id+'.',{id,tone:'success',destination:'downloads'});dataVersion++;
        if(screen.mode==='feed'&&screen.section==='posts'&&!screen.author)void load(false,true);
      }else notify('Could not save post '+job.post_id+'. '+(job.error||'Download '+job.status+'.'),{id,tone:'error',destination:'downloads'});
    }
    rememberActive();
  }
  function acceptJob(job:Download){
    jobRevision++;
    // Keep the acknowledged ID even if the initiating surface was unmounted.
    if(!alive){
      try{const ids=JSON.parse(sessionStorage.getItem(activeKey)??'[]');if(Array.isArray(ids))activeJobs=new Set(ids.filter(id=>typeof id==='string'&&/^[0-9a-f]{8}-[0-9a-f-]{27}$/.test(id)).slice(0,100));}catch{/* Keep current-page IDs when storage is unavailable. */}
      // A remounted surface will report terminal outcomes from this receipt.
      activeJobs.add(job.id);rememberActive();return;
    }
    observe(job);
  }
  async function checkActive(){
    if(jobPolling||!activeJobs.size)return;
    const request=new AbortController();jobRequest=request;jobPolling=true;
    const revision=jobRevision,ids=[...activeJobs];
    try{
      const results=await Promise.allSettled(ids.map(id=>twitterApi.job(id,request.signal)));
      if(!alive||request.signal.aborted||revision!==jobRevision)return;
      let unavailable=false;
      for(let index=0;index<results.length;index++){
        const result=results[index];
        if(result.status==='fulfilled')observe(result.value);
        else if(result.reason instanceof ApiError&&result.reason.status===404){
          activeJobs.delete(ids[index]);rememberActive();
          notify('The saved download status is unavailable.',{id:'twitter-job-'+ids[index],tone:'error',destination:'downloads'});
        }else unavailable=true;
      }
      if(unavailable&&!statusUnavailable)notify('Could not check download status. Retrying…',{id:'twitter-status-read',tone:'error',destination:'downloads'});
      statusUnavailable=unavailable;
    }finally{if(jobRequest===request)jobPolling=false;}
  }
  async function refreshPreferences(){
    preferenceRequest?.abort();const request=new AbortController();preferenceRequest=request;preferenceLoading=true;
    try{
      const saved=await twitterApi.settings(request.signal);
      if(!alive||request.signal.aborted)return;preferences=saved;preferenceError='';
    }catch(e){if(alive&&!request.signal.aborted)preferenceError=e instanceof Error?e.message:'Could not load the save folder.';}
    finally{if(preferenceRequest===request)preferenceLoading=false;}
  }
  async function start(){
    if(submitting||!url||(!submission&&!ready))return;
    const body=submission??{url,source_id:preferences!.source_id,request_id:crypto.randomUUID()};
    rememberSubmission(body);submitting=true;submitError='';
    try{
      const job=await twitterApi.download(body.url,body.source_id,body.request_id);acceptJob(job);rememberSubmission(null);
      if(alive){query='';search='';searchPending=false;}
    }catch(e){
      if(e instanceof ApiError&&[400,409,422].includes(e.status))rememberSubmission(null);
      if(alive)submitError=(e instanceof Error?e.message:'Could not confirm the request.')+(submission?' Retry request uses the same saved token.':'');
    }finally{submitting=false;}
  }
  function searchKey(event:KeyboardEvent){if(event.key==='Enter'&&!event.repeat&&!event.isComposing&&event.keyCode!==229&&url){event.preventDefault();void start();}}
  function twitterSettings(){settingsInitialSection.set('twitter-capture');settingsOpen.set(true);}
  async function settingsClosed(){
    await refreshPreferences();if(!alive)return;dataVersion++;
    if(screen.mode==='detail'&&screen.detail){const item=screen.detail;detailRequest?.abort();const request=new AbortController();detailRequest=request;try{const fresh=await twitterApi.capture(item.source_id,item.path,request.signal);if(alive&&!request.signal.aborted)screen={...screen,detail:fresh};}catch(e){if(alive&&!request.signal.aborted){screen={...screen,detail:null};error=e instanceof Error?e.message:'Saved post is unavailable.';}}}
    else void load(false,true);
  }
  onMount(()=>{
    try{const value=JSON.parse(sessionStorage.getItem(receiptKey)??'null');if(value&&postUrl(value.url)===value.url&&typeof value.source_id==='string'&&/^[0-9a-f]{8}-[0-9a-f-]{27}$/.test(value.request_id)){submission=value;query=value.url;submitError='Confirm the previous request with Retry request.';}}catch{/* Ignore an unusable browser receipt. */}
    try{const ids=JSON.parse(sessionStorage.getItem(activeKey)??'[]');if(Array.isArray(ids))activeJobs=new Set(ids.filter(id=>typeof id==='string'&&/^[0-9a-f]{8}-[0-9a-f-]{27}$/.test(id)).slice(0,100));}catch{/* Ignore unusable status tracking. */}
    void refreshPreferences();void load();
    async function poll(){await checkActive();if(alive)timer=setTimeout(poll,1000);}void poll();
    let wasOpen=false;const stop=settingsOpen.subscribe(open=>{if(wasOpen&&!open)void settingsClosed();wasOpen=open;});
    return stop;
  });
  onDestroy(()=>{alive=false;stopReads();clearTimeout(timer);jobRequest?.abort();preferenceRequest?.abort();});
</script>

<div class="twitter-surface">
  <header><button class="menu" aria-label="Open Keivotos menu" on:click={()=>drawer=true}>☰</button><span class="module-title">Twitter</span>
    <div class="search-controls"><input aria-label="Search saved posts or paste a Twitter URL" placeholder="Search or paste a post link…" maxlength="2048" bind:value={query} disabled={submitting||!!submission} on:input={input} on:compositionstart={beginSearchComposition} on:compositionend={endSearchComposition} on:keydown={searchKey}/><div class="download-reveal" class:visible={!!url} inert={!url}><button class="download" aria-hidden={!url} tabindex={url?0:-1} disabled={submitting||!url||!submission&&!ready} on:click={start}>{submitting?'Starting…':submission?'Retry request':'Download'}</button></div></div>
  </header>
  {#if submitError}<p class="submit-error" role="alert">{submitError}</p>{/if}
  {#if url&&!submission&&!preferenceLoading&&setupError}<p class="submit-error" role="status">{setupError} <button on:click={twitterSettings}>Twitter settings</button></p>{/if}
  <div class="twitter-body">
    <ModuleSidebarDock owner="twitter" label="Twitter"><TwitterSidebar section={screen.section} onSelect={selectSection}/></ModuleSidebarDock>
    <div class="columns">
      <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
      <main bind:this={main} tabindex="-1" aria-label="Twitter archive" aria-busy={loading||detailLoading}>
        <div class="feed-heading">{#if history.length}<button class="back" aria-label="Back to saved view" on:click={back}>←</button>{/if}<h1>{title}</h1></div>
        {#if screen.author&&screen.mode==='feed'}<div class="author-heading"><strong>{screen.author.author.name}</strong><span>@{screen.author.author.handle}</span></div>{/if}
        {#if error}<div class="message" role="alert">{error}{#if screen.mode==='feed'} <button on:click={()=>load()}>Retry</button>{/if}</div>{/if}
        {#if screen.mode==='detail'}
          {#if detailLoading}<p class="message" role="status">Loading saved post…</p>{:else if screen.detail}<TwitterPost item={screen.detail} detail onOpen={openPost} onAuthor={openAuthor}/>{/if}
        {:else}
          {#if screen.section==='authors'}<TwitterAuthorView {items} onOpen={openAuthor}/>{:else}{#each items as item (captureKey(item))}<TwitterPost {item} onOpen={openPost} onAuthor={openAuthor}/>{/each}{/if}
          {#if loading}<p class="message" role="status">Loading saved posts…</p>{:else if !error&&!items.length}<div class="empty"><h2>{search?'No saved posts found':'Your saved posts live here'}</h2><p>{search?'Try another search.':'Paste a post link above to save it locally.'}</p></div>{/if}
          {#if cursor!==null}<button class="more" disabled={loading} on:click={()=>load(true)}>Load more</button>{/if}
        {/if}
      </main>
    </div>
  </div>
</div>
{#if drawer}<AppDrawer on:close={()=>drawer=false}/>{/if}

<style>
  .twitter-surface{display:flex;flex:1;min-height:0;min-width:0;flex-direction:column;background:#0f0f14;color:#e7e7ee;font-family:system-ui,sans-serif}header{display:flex;align-items:center;gap:16px;min-height:60px;padding:8px 16px;border-bottom:1px solid #ffffff14;flex:none}.menu{width:36px;height:36px;flex:none;border-radius:8px;background:#1e1e2e;font-size:16px}.module-title{font-size:18px;font-weight:600}.search-controls{display:flex;align-items:center;gap:10px;flex:1;max-width:640px;margin:auto;min-width:0}.search-controls input{flex:1;min-width:0;border:1px solid #ffffff18;background:#1b1b25;border-radius:24px;padding:10px 16px;font-size:14px}.search-controls input:focus{border-color:var(--accent)}.download-reveal{width:110px;flex:none;overflow:hidden;clip-path:inset(0 100% 0 0);opacity:0;transform:translateX(-8px);pointer-events:none;transition:clip-path .22s ease,opacity .22s ease,transform .22s ease}.download-reveal.visible{clip-path:inset(0);opacity:1;transform:translateX(0);pointer-events:auto}.download{width:100%;background:var(--accent);color:#111118;padding:9px 9px;border-radius:20px;font-size:13px;font-weight:600}button:disabled{opacity:.4;cursor:default}.submit-error{flex:none;padding:10px 18px;border-bottom:1px solid #ffffff14;font-size:13px;color:#e6a8ae;overflow-wrap:anywhere}.twitter-body{display:flex;position:relative;flex:1;min-height:0;min-width:0;overflow:hidden}.columns{display:flex;flex:1;min-width:0;min-height:0;max-width:600px;margin-inline:auto;width:100%}main{flex:1;max-width:600px;min-width:0;min-height:0;overflow:auto;border-inline:1px solid #ffffff14;scrollbar-gutter:stable}main:focus{outline:none}.feed-heading{display:flex;gap:16px;align-items:center;position:sticky;top:0;z-index:1;background:#0f0f14ed;backdrop-filter:blur(12px);min-height:62px;padding:12px 20px;border-bottom:1px solid #ffffff14}h1{font-size:20px;font-weight:700;min-width:0;overflow-wrap:anywhere}.back{font-size:23px;flex:none;width:30px;height:30px;border-radius:50%}.back:hover{background:#ffffff12}.author-heading{display:flex;flex-direction:column;padding:22px 20px;border-bottom:1px solid #ffffff14;gap:8px;overflow-wrap:anywhere}.author-heading strong{font-size:22px}.author-heading span{font-size:14px;color:#9292a3}.message{padding:22px 20px;font-size:14px;color:#aaaab9}.message button,.submit-error button{color:var(--accent);text-decoration:underline}.empty{padding:40px 30px}.empty h2{font-size:26px;font-weight:700}.empty p{font-size:14px;line-height:1.6;color:#9292a3;margin-top:12px}.more{display:block;width:100%;padding:18px;color:var(--accent);font-size:14px}.more:hover{background:#ffffff04}button:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
  @media(max-width:700px){header{gap:10px;padding-inline:12px}.module-title{display:none}.search-controls{gap:8px}.feed-heading{padding-inline:14px}.empty{padding:32px 22px}}
  @media(max-width:400px){header{padding-inline:8px;gap:8px}.search-controls{gap:6px}.search-controls input{padding-inline:12px;font-size:12px}.download-reveal{width:92px}.download{padding-inline:4px;font-size:12px}}
  :global(html[data-motion='reduced']) .download-reveal{transition:none}@media(prefers-reduced-motion:reduce){.download-reveal{transition:none}}
</style>

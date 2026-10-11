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
  import Settings from './Settings.svelte';
  import {twitterApi,postUrl,captureKey,type Capture,type Download,type Preferences,type TwitterFolder} from './api';

  type Screen={section:'posts'|'authors';author:Capture|null;mode:'feed'|'detail';detail:Capture|null};
  type Frame={screen:Screen;items:Capture[];cursor:number|null;query:string;scroll:number;focus:{post:string;author:string;label:string};error:string;version:number};
  type Submission={url:string;source_id:string;request_id:string};
  let screen:Screen={section:'posts',author:null,mode:'feed',detail:null};
  let history:Frame[]=[],items:Capture[]=[],cursor:number|null=0,query='',error='',loading=false,detailLoading=false;
  let main:HTMLElement,jobsToggle:HTMLButtonElement,jobsPane:HTMLElement,drawer=false,jobsOpen=false,alive=true,epoch=0,dataVersion=0;
  let preferences:Preferences|null=null,folders:TwitterFolder[]=[],preferenceBusy=false,preferenceError='';
  let jobs:Download[]=[],jobCursor:number|null=0,jobError='',jobsLoading=false,jobRevision=0;
  let changing=new Set<string>(),retryTokens=new Map<string,string>(),seenJobs=new Map<string,string>();
  let submission:Submission|null=null,submitting=false,submitError='';
  let archiveRequest:AbortController|null=null,detailRequest:AbortController|null=null,jobRequest:AbortController|null=null,preferenceRequest:AbortController|null=null;
  let debounce:ReturnType<typeof setTimeout>|undefined,timer:ReturnType<typeof setTimeout>|undefined;
  const receiptKey=persistentStorageKey('twitter-pending-request');
  $: url=postUrl(query);
  $: search=url?'':query.trim();
  $: ready=!!preferences?.available&&!!preferences.dependencies.ready&&!preferenceBusy;
  $: title=screen.mode==='detail'?'Post':screen.author?screen.author.author.name:screen.section==='authors'?'Authors':'Saved posts';

  function rememberSubmission(value:Submission|null){
    submission=value;
    try{if(value)sessionStorage.setItem(receiptKey,JSON.stringify(value));else sessionStorage.removeItem(receiptKey);}catch{/* The current-page receipt remains usable. */}
  }
  function stopReads(){archiveRequest?.abort();detailRequest?.abort();archiveRequest=null;detailRequest=null;clearTimeout(debounce);epoch++;loading=false;detailLoading=false;}
  function remember(){
    const active=document.activeElement as HTMLElement|null;
    const focus={post:active?.closest<HTMLElement>('[data-twitter-post]')?.dataset.twitterPost??'',author:active?.closest<HTMLElement>('[data-twitter-author]')?.dataset.twitterAuthor??'',label:active?.getAttribute('aria-label')??''};
    history=[...history,{screen:{...screen},items,cursor,query,scroll:main?.scrollTop??0,focus,error,version:dataVersion}];
  }
  async function focusMain(serial=epoch){await tick();if(alive&&serial===epoch){main?.focus({preventScroll:true});main.scrollTop=0;}}
  async function toggleJobs(){jobsOpen=!jobsOpen;await tick();if(alive&&jobsOpen)jobsPane.querySelector<HTMLElement>('button')?.focus();}
  async function closeJobs(){jobsOpen=false;await tick();if(alive)jobsToggle?.focus({preventScroll:true});}
  function jobsKey(event:KeyboardEvent){if(event.key==='Escape'&&!event.isComposing&&jobsOpen&&!document.fullscreenElement){event.preventDefault();void closeJobs();}}
  function selectSection(section:'posts'|'authors'){
    stopReads();history=[];screen={section,author:null,mode:'feed',detail:null};items=[];cursor=0;error='';
    if(window.matchMedia('(max-width:700px)').matches)moduleSidebarPreferences('twitter').open.set(false);
    void load();void focusMain();
  }
  async function back(){
    const previous=history.at(-1);if(!previous)return;
    stopReads();const serial=epoch;history=history.slice(0,-1);screen=previous.screen;items=previous.items;cursor=previous.cursor;if(!submission)query=previous.query;error=previous.error;
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
  function input(event:Event){
    query=(event.currentTarget as HTMLInputElement).value;
    clearTimeout(debounce);
    // Entering URL mode keeps the saved feed in place; it never requests X.
    if(postUrl(query)){stopReads();return;}
    stopReads();history=[];screen={section:screen.section,author:null,mode:'feed',detail:null};loading=true;
    debounce=setTimeout(()=>{void load();},180);
  }
  async function load(more=false,refresh=false){
    if(screen.mode!=='feed')return;
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
  function openAuthor(item:Capture,event:MouseEvent){remember();stopReads();screen={section:'posts',mode:'feed',author:item,detail:null};items=[];cursor=0;error='';void load();void focusMain();}
  function observe(values:Download[]){
    let completed=false;
    for(const job of values){
      const before=seenJobs.get(job.id);seenJobs.set(job.id,job.status);
      if(before&&before!==job.status&&job.status==='complete'){
        notify('Post saved: '+job.post_id+'.',{id:'twitter-job-'+job.id,tone:'success'});completed=true;
      }else if(before&&before!==job.status&&job.status==='failed')notify('Could not save post '+job.post_id+'. '+job.error,{id:'twitter-job-'+job.id,tone:'error'});
      else if(['queued','running'].includes(job.status))updateNotification('twitter-job-'+job.id,{message:'Saving post '+job.post_id+'…',tone:'info',progress:job.progress});
    }
    if(completed){dataVersion++;if(screen.mode==='feed'&&screen.section==='posts'&&!screen.author)void load(false,true);}
  }
  function acceptJob(job:Download){
    jobRevision++;jobs=[job,...jobs.filter(item=>item.id!==job.id)].sort((a,b)=>b.created_at.localeCompare(a.created_at)||b.id.localeCompare(a.id));
    seenJobs.set(job.id,job.status);
    if(['queued','running'].includes(job.status))notify('Saving post '+job.post_id+'…',{id:'twitter-job-'+job.id,progress:job.progress});
    else if(job.status==='complete'){dataVersion++;notify('Post saved: '+job.post_id+'.',{id:'twitter-job-'+job.id,tone:'success'});if(screen.mode==='feed')void load(false,true);}
  }
  async function loadJobs(more=false){
    if(jobsLoading)return;const request=new AbortController();jobRequest=request;jobsLoading=true;
    const revision=jobRevision;
    try{
      let offset=more?jobCursor:0;if(offset===null)return;
      let result=await twitterApi.jobs(offset,request.signal),rows=result.items;
      // Reconcile the currently loaded prefix instead of dropping older rows.
      const count=jobs.length;
      if(!more)while(result.next_offset!==null&&rows.length<count){result=await twitterApi.jobs(result.next_offset,request.signal);rows=[...rows,...result.items];}
      if(!alive||request.signal.aborted||revision!==jobRevision)return;
      observe(rows);jobs=[...new Map((more?[...jobs,...rows]:rows).map(job=>[job.id,job])).values()];jobCursor=result.next_offset;jobError='';
    }catch(e){if(alive&&!request.signal.aborted)jobError=e instanceof Error?e.message:'Could not load downloads.';}
    finally{if(jobRequest===request)jobsLoading=false;}
  }
  async function refreshPreferences(){
    preferenceRequest?.abort();const request=new AbortController();preferenceRequest=request;
    try{
      const [saved,registered]=await Promise.all([twitterApi.settings(request.signal),twitterApi.folders(request.signal)]);
      if(!alive||request.signal.aborted)return;preferences=saved;folders=registered;preferenceError='';
    }catch(e){if(alive&&!request.signal.aborted)preferenceError=e instanceof Error?e.message:'Could not load the save folder.';}
  }
  async function saveDestination(sourceId:string){
    if(preferenceBusy||submitting||submission)return;preferenceBusy=true;preferenceError='';
    try{const saved=await twitterApi.saveSettings(sourceId);if(alive)preferences=saved;}
    catch(e){if(alive)preferenceError=e instanceof Error?e.message:'Could not save the folder.';}
    finally{preferenceBusy=false;}
  }
  async function start(){
    if(submitting||!url||(!submission&&!ready))return;
    const body=submission??{url,source_id:preferences!.source_id,request_id:crypto.randomUUID()};
    rememberSubmission(body);submitting=true;submitError='';
    try{
      const job=await twitterApi.download(body.url,body.source_id,body.request_id);rememberSubmission(null);
      if(!alive)return;acceptJob(job);query='';jobsOpen=window.matchMedia('(max-width:1050px)').matches;
    }catch(e){
      if(e instanceof ApiError&&[400,409,422].includes(e.status))rememberSubmission(null);
      if(alive)submitError=(e instanceof Error?e.message:'Could not confirm the request.')+(submission?' Retry request uses the same saved token.':'');
    }finally{submitting=false;}
  }
  async function jobAction(job:Download,retry=false){
    if(changing.has(job.id))return;changing=new Set([...changing,job.id]);jobError='';
    try{
      let result:Download;
      if(retry){const token=retryTokens.get(job.id)??crypto.randomUUID();retryTokens.set(job.id,token);result=await twitterApi.retry(job.id,token);retryTokens.delete(job.id);}
      else result=await twitterApi.cancel(job.id);
      if(alive)acceptJob(result);
    }catch(e){if(alive)jobError=e instanceof Error?e.message:'Could not confirm the download action.';}
    finally{changing=new Set([...changing].filter(id=>id!==job.id));}
  }
  function searchKey(event:KeyboardEvent){if(event.key==='Enter'&&!event.repeat&&!event.isComposing&&event.keyCode!==229&&url){event.preventDefault();void start();}}
  function foldersSettings(){settingsInitialSection.set('folders');settingsOpen.set(true);}
  async function settingsClosed(){
    await refreshPreferences();if(!alive)return;dataVersion++;
    if(screen.mode==='detail'&&screen.detail){const item=screen.detail;detailRequest?.abort();const request=new AbortController();detailRequest=request;try{const fresh=await twitterApi.capture(item.source_id,item.path,request.signal);if(alive&&!request.signal.aborted)screen={...screen,detail:fresh};}catch(e){if(alive&&!request.signal.aborted){screen={...screen,detail:null};error=e instanceof Error?e.message:'Saved post is unavailable.';}}}
    else void load(false,true);
  }
  onMount(()=>{
    try{const value=JSON.parse(sessionStorage.getItem(receiptKey)??'null');if(value&&postUrl(value.url)===value.url&&typeof value.source_id==='string'&&/^[0-9a-f]{8}-[0-9a-f-]{27}$/.test(value.request_id)){submission=value;query=value.url;submitError='Confirm the previous request with Retry request.';}}catch{/* Ignore an unusable browser receipt. */}
    void refreshPreferences();void load();
    async function poll(){await loadJobs();if(alive)timer=setTimeout(poll,1000);}void poll();
    let wasOpen=false;const stop=settingsOpen.subscribe(open=>{if(wasOpen&&!open)void settingsClosed();wasOpen=open;});
    return stop;
  });
  onDestroy(()=>{alive=false;stopReads();clearTimeout(timer);jobRequest?.abort();preferenceRequest?.abort();});
</script>

<svelte:window on:keydown={jobsKey}/>
<div class="twitter-surface">
  <header><button class="menu" aria-label="Open Keivotos menu" on:click={()=>drawer=true}>☰</button><span class="module-title">Twitter</span>
    <div class="search-controls"><input aria-label="Search saved posts or paste a Twitter URL" placeholder="Search or paste a post link…" maxlength="2048" bind:value={query} disabled={submitting||!!submission} on:input={input} on:keydown={searchKey}/><div class="download-reveal" class:visible={!!url} inert={!url}><button class="download" aria-hidden={!url} tabindex={url?0:-1} disabled={submitting||!url||!submission&&!ready} on:click={start}>{submitting?'Starting…':submission?'Retry request':'Download'}</button></div></div>
  </header>
  {#if submitError}<p class="submit-error" role="alert">{submitError}</p>{/if}
  <div class="twitter-body">
    <ModuleSidebarDock owner="twitter" label="Twitter"><TwitterSidebar section={screen.section} onSelect={selectSection}/></ModuleSidebarDock>
    <div class="columns">
      <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
      <main bind:this={main} tabindex="-1" aria-label="Twitter archive" aria-busy={loading||detailLoading}>
        <div class="feed-heading">{#if history.length}<button class="back" aria-label="Back to saved view" on:click={back}>←</button>{/if}<h1>{title}</h1><button class="jobs-toggle" bind:this={jobsToggle} aria-label="Downloads" aria-expanded={jobsOpen} on:click={toggleJobs}>↓</button></div>
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
      <aside class:open={jobsOpen} bind:this={jobsPane} aria-label="Twitter downloads">
        <div class="aside-heading"><h2>Downloads</h2><button class="close-jobs" aria-label="Close downloads" on:click={closeJobs}>×</button></div>
        <Settings {preferences} {folders} busy={preferenceBusy||submitting||!!submission} error={preferenceError} onSave={saveDestination} onManage={foldersSettings}/>
        {#if preferenceError}<button class="refresh" on:click={refreshPreferences}>Retry folder</button>{/if}
        {#if jobError}<p class="job-error" role="alert">{jobError} <button on:click={()=>loadJobs()}>Refresh</button></p>{/if}
        <div class="jobs" aria-busy={jobsLoading}>
          {#each jobs as job (job.id)}
            <div class="job" data-twitter-job={job.id}><div class="job-title"><strong title={job.url}>Post {job.post_id}</strong><span>{job.status==='complete'?'Saved':job.status==='running'?job.phase:job.status}</span></div>
              {#if ['queued','running'].includes(job.status)}<progress max="100" value={job.progress} aria-label={`Saving post ${job.post_id}`}></progress><button disabled={changing.has(job.id)} aria-label={`Cancel post ${job.post_id}`} on:click={()=>jobAction(job)}>Cancel</button>
              {:else if job.status==='complete'}<button aria-label={`View saved post ${job.post_id}`} on:click={()=>void twitterApi.capture(job.source_id,job.path).then(item=>{if(alive)void openPost(item);}).catch(e=>{if(alive)jobError=e.message;})}>View post</button>
              {:else}<p>{job.error}</p><button disabled={changing.has(job.id)} aria-label={`Retry post ${job.post_id}`} on:click={()=>jobAction(job,true)}>Retry</button>{/if}
            </div>
          {/each}
          {#if !jobs.length&&!jobsLoading&&!jobError}<p class="no-jobs">No downloads yet.</p>{/if}
        </div>
        {#if jobCursor!==null}<button class="more" disabled={jobsLoading} on:click={()=>loadJobs(true)}>More downloads</button>{/if}
      </aside>
    </div>
  </div>
</div>
{#if drawer}<AppDrawer on:close={()=>drawer=false}/>{/if}

<style>
  .twitter-surface{display:flex;flex:1;min-height:0;min-width:0;flex-direction:column;background:#0f0f14;color:#e7e7ee;font-family:system-ui,sans-serif}header{display:flex;align-items:center;gap:16px;min-height:60px;padding:8px 16px;border-bottom:1px solid #ffffff14;flex:none}.menu{width:36px;height:36px;flex:none;border-radius:8px;background:#1e1e2e;font-size:16px}.module-title{font-size:18px;font-weight:600}.search-controls{display:flex;align-items:center;gap:10px;flex:1;max-width:640px;margin:auto;min-width:0}.search-controls input{flex:1;min-width:0;border:1px solid #ffffff18;background:#1b1b25;border-radius:24px;padding:10px 16px;font-size:14px}.search-controls input:focus{border-color:var(--accent)}.download-reveal{width:110px;flex:none;overflow:hidden;clip-path:inset(0 100% 0 0);opacity:0;transform:translateX(-8px);pointer-events:none;transition:clip-path .22s ease,opacity .22s ease,transform .22s ease}.download-reveal.visible{clip-path:inset(0);opacity:1;transform:translateX(0);pointer-events:auto}.download{width:100%;background:var(--accent);color:#111118;padding:9px 9px;border-radius:20px;font-size:13px;font-weight:600}button:disabled{opacity:.4;cursor:default}.submit-error{flex:none;padding:10px 18px;border-bottom:1px solid #ffffff14;font-size:13px;color:#e6a8ae;overflow-wrap:anywhere}.twitter-body{display:flex;position:relative;flex:1;min-height:0;min-width:0;overflow:hidden}.columns{display:flex;flex:1;min-width:0;min-height:0;max-width:940px;margin-inline:auto;width:100%}main{flex:1;max-width:600px;min-width:0;min-height:0;overflow:auto;border-inline:1px solid #ffffff14;scrollbar-gutter:stable}main:focus{outline:none}.feed-heading{display:flex;gap:16px;align-items:center;position:sticky;top:0;z-index:1;background:#0f0f14ed;backdrop-filter:blur(12px);min-height:62px;padding:12px 20px;border-bottom:1px solid #ffffff14}h1{font-size:20px;font-weight:700;min-width:0;overflow-wrap:anywhere}.back{font-size:23px;flex:none;width:30px;height:30px;border-radius:50%}.back:hover{background:#ffffff12}.author-heading{display:flex;flex-direction:column;padding:22px 20px;border-bottom:1px solid #ffffff14;gap:8px;overflow-wrap:anywhere}.author-heading strong{font-size:22px}.author-heading span{font-size:14px;color:#9292a3}.message{padding:22px 20px;font-size:14px;color:#aaaab9}.message button,.job-error button{color:var(--accent);text-decoration:underline}.empty{padding:40px 30px}.empty h2{font-size:26px;font-weight:700}.empty p{font-size:14px;line-height:1.6;color:#9292a3;margin-top:12px}.more{display:block;width:100%;padding:18px;color:var(--accent);font-size:14px}.more:hover{background:#ffffff04}aside{flex:0 0 300px;min-height:0;overflow:auto;padding:22px 24px;scrollbar-gutter:stable}.aside-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:20px}.aside-heading h2{font-size:19px;font-weight:700}.jobs{margin-top:22px}.job{padding:16px 0;border-top:1px solid #ffffff14}.job-title{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;font-size:13px}.job-title span{font-size:11px;color:#9292a3;text-transform:capitalize}.job p,.job-error{font-size:12px;color:#aaaab9;overflow-wrap:anywhere;line-height:1.5;margin-top:9px}.job button,.refresh{color:var(--accent);font-size:12px;margin-top:10px}.job progress{display:block;width:100%;height:4px;margin-top:12px;accent-color:var(--accent)}.no-jobs{color:#9292a3;font-size:13px}.close-jobs,.jobs-toggle{display:none}button:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
  @media(max-width:1050px){.columns{max-width:600px}main{max-width:none}.jobs-toggle{display:block;margin-left:auto;font-size:22px;width:32px;height:32px;flex:none}aside{display:none}aside.open{display:block;position:absolute;right:0;top:0;bottom:0;width:min(340px,100%);z-index:5;background:#14141b;border-left:1px solid #ffffff20;box-shadow:-10px 0 40px #0007}.close-jobs{display:block;font-size:24px;width:32px;height:32px}}
  @media(max-width:700px){header{gap:10px;padding-inline:12px}.module-title{display:none}.search-controls{gap:8px}.feed-heading{padding-inline:14px}.empty{padding:32px 22px}}
  @media(max-width:400px){header{padding-inline:8px;gap:8px}.search-controls{gap:6px}.search-controls input{padding-inline:12px;font-size:12px}.download-reveal{width:92px}.download{padding-inline:4px;font-size:12px}aside.open{padding:18px}}
  :global(html[data-motion='reduced']) .download-reveal{transition:none}@media(prefers-reduced-motion:reduce){.download-reveal{transition:none}}
</style>

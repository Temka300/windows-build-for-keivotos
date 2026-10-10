<script lang="ts">
  import {onMount,onDestroy,tick} from 'svelte';
  import {languageApi} from './api';
  import {accepts,categories,defaultPreferences,nextQuestion,validPreferences,type StudyPreferences} from './koreanPractice';

  export let active=true;
  let preferences:StudyPreferences={...defaultPreferences};
  let current=nextQuestion(preferences),answer='',checked:boolean|null=null;
  let input:HTMLInputElement,loading=true,saving=false,composing=false;
  let settingsWarning='',rangeWarning='',loadFailed=false,choicesChanged=false;
  let pending:StudyPreferences|null=null,alive=true;
  const controller=new AbortController();
  $: categoryMaximum=categories.find(c=>c.id===preferences.category)?.maximum||99;
  $: if(active&&!loading)void focusAnswer();

  async function focusAnswer(){await tick();if(alive&&active)input?.focus();}
  function next(){current=nextQuestion(preferences,current);answer='';checked=null;void focusAnswer();}
  function check(){if(loading||rangeWarning||composing||checked!==null||!answer.trim())return;checked=accepts(current,answer);}
  function answerKey(event:KeyboardEvent){
    if(event.key!=='Enter')return;
    if(event.isComposing||event.keyCode===229||composing||event.repeat)return;
    event.preventDefault();check();
  }
  async function load(){
    loading=true;
    try{
      const saved=await languageApi.studyPreferences(controller.signal);
      if(!alive)return;
      if(!validPreferences(saved))throw new Error('Saved practice choices are invalid.');
      preferences=saved;current=nextQuestion(preferences);answer='';checked=null;
      settingsWarning='';loadFailed=false;
    }catch(error){
      if(alive){loadFailed=true;settingsWarning='Could not load saved choices. You can practice with these choices and retry.';}
    }finally{if(alive){loading=false;void focusAnswer();}}
  }
  async function flush(){
    if(saving||!alive)return;
    saving=true;
    try{
      while(pending&&alive){
        const choice=pending;pending=null;
        try{await languageApi.saveStudyPreferences(choice);if(alive){settingsWarning='';loadFailed=false;}}
        catch{if(alive)settingsWarning='These choices could not be saved. Practice still works; retry to keep them after restart.';}
      }
    }finally{if(alive)saving=false;}
  }
  function choose(){
    rangeWarning=validPreferences(preferences)?'':`Choose whole numbers from 1 to ${categoryMaximum}, with From no greater than To.`;
    if(rangeWarning)return;
    choicesChanged=true;next();pending={...preferences};void flush();
  }
  function changeCategory(){
    const maximum=categories.find(c=>c.id===preferences.category)!.maximum;
    preferences={...preferences,minimum:1,maximum};choose();
  }
  function retrySettings(){
    if(loadFailed&&!choicesChanged){void load();return;}
    if(!validPreferences(preferences))return;
    pending={...preferences};void flush();
  }
  onMount(()=>{void load();});
  onDestroy(()=>{alive=false;pending=null;controller.abort();});
</script>

<section class="practice-section" aria-label="Practice">
  <div class="practice-center">
    <div class="practice-card">
      <div class="practice-options">
        <label>Category<select bind:value={preferences.category} on:change={changeCategory} disabled={loading}>
          {#each categories as category}<option value={category.id}>{category.label}</option>{/each}
        </select></label>
        <label>Answer in<select bind:value={preferences.answer_language} on:change={()=>choose()} disabled={loading}>
          <option value="korean">Korean</option><option value="english">English</option>
        </select></label>
      </div>
      <div class="practice-range">
        <label>From<input type="number" min="1" max={categoryMaximum} step="1" bind:value={preferences.minimum} on:change={()=>choose()} disabled={loading}/></label>
        <label>To<input type="number" min="1" max={categoryMaximum} step="1" bind:value={preferences.maximum} on:change={()=>choose()} disabled={loading}/></label>
      </div>
      {#if rangeWarning}<p class="warning" role="alert">{rangeWarning}</p>{/if}
      <p class="practice-system">{categories.find(c=>c.id===current.category)?.label}</p>
      <p class="practice-prompt" lang={current.answerLanguage==='english'?'ko':'en'}>{current.prompt}</p>
      <label class="answer-label" for="korean-practice-answer">Your answer in {current.answerLanguage==='korean'?'Korean':'English'}</label>
      <input id="korean-practice-answer" class="practice-answer" bind:this={input} bind:value={answer}
        lang={current.answerLanguage==='korean'?'ko':'en'} maxlength="256" autocomplete="off" spellcheck="false"
        disabled={loading||!!rangeWarning||checked!==null} on:keydown={answerKey}
        on:compositionstart={()=>composing=true} on:compositionend={()=>composing=false}/>
      <div class="practice-feedback" aria-live="polite" aria-atomic="true">
        {#if checked!==null}<p class:correct={checked}>{checked?'Correct':'Not quite'}<span>Answer: <strong lang={current.answerLanguage==='korean'?'ko':'en'}>{current.answer}</strong></span></p>{/if}
      </div>
      <div class="practice-actions">
        <button on:click={check} disabled={loading||!!rangeWarning||composing||!answer.trim()||checked!==null}>Check</button>
        <button on:click={next} disabled={loading||!!rangeWarning||checked===null}>Next</button>
      </div>
      {#if settingsWarning}<div class="settings-warning" role="status"><p>{settingsWarning}</p><button on:click={retrySettings} disabled={loading||saving||!!rangeWarning}>Retry settings</button></div>{/if}
    </div>
  </div>
</section>

<style>
  .practice-section{display:flex;flex-direction:column;flex:1;min-height:0;overflow:auto;padding:24px;color:#eee}
  .practice-center{flex:1;display:flex;align-items:center;justify-content:center;padding:24px 0}
  .practice-card{width:100%;max-width:480px;border:1px solid #383846;border-radius:12px;background:#191920;padding:24px;box-sizing:border-box}
  .practice-options{display:flex;gap:16px}.practice-options label:first-child{flex:1;min-width:0}label{display:flex;flex-direction:column;gap:7px;font-size:13px;color:#c6c6d3}
  select,input{background:#101016;color:#eee;border:1px solid #383846;border-radius:6px;padding:9px 10px;font:inherit;box-sizing:border-box;min-width:0}
  select:focus-visible,input:focus-visible,button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  .practice-range{display:flex;gap:12px;margin-top:16px}.practice-range label{flex:1;min-width:0}
  .practice-system{font-size:13px;text-align:center;color:#a7a7b6;margin:28px 0 12px}.practice-prompt{font-size:32px;line-height:1.35;text-align:center;overflow-wrap:anywhere;margin:0 0 24px}
  .answer-label{margin-bottom:7px}.practice-answer{width:100%;font-size:18px}
  .practice-feedback{min-height:66px;padding-top:12px;box-sizing:border-box;font-size:14px}.practice-feedback p{margin:0;color:#ddbca1}.practice-feedback p.correct{color:var(--accent-light)}.practice-feedback span{display:block;margin-top:6px;color:#ddd}
  .practice-actions{display:flex;justify-content:flex-end;gap:8px}button{background:#23232e;color:#eee;border:1px solid #3a3a49;border-radius:6px;padding:8px 16px;font:inherit;font-size:14px;cursor:pointer}button:hover:enabled{border-color:var(--accent)}button:disabled,select:disabled,input:disabled{opacity:.55;cursor:default}
  .warning,.settings-warning{font-size:13px;line-height:1.5;color:#ddbca1}.settings-warning{margin-top:16px}.settings-warning p{margin:0 0 8px}
  @media(max-width:500px){.practice-section{padding:16px}.practice-card{padding:18px}.practice-options{flex-wrap:wrap}.practice-options label{width:100%}}
</style>

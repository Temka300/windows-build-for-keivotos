import {get, post, put} from '../../lib/http';
import type {StudyPreferences} from './koreanPractice';

export interface LanguageFolder {source_id:string; name:string;}
export interface LanguageFile {name:string; size:number;}
export interface IndexRefresh {status:"complete"|"failed"; warning:string;}
export interface LanguageDocument {name:string; content:string; revision:string; index_refresh?:IndexRefresh;}
export interface VocabularyCreate {request_id:string; korean:string; pronunciation?:string; english:string;}
export interface VocabularyEntry {id:string; korean:string; pronunciation:string; english:string; created_at:string;}
export interface VocabularyPage {items:VocabularyEntry[]; total:number;}

export const languageApi = {
  vocabulary:(offset=0,limit=50,signal?:AbortSignal)=>get<VocabularyPage>('/language/vocabulary',{offset,limit},signal),
  createVocabulary:(entry:VocabularyCreate)=>post<VocabularyEntry>('/language/vocabulary',entry),
  studyPreferences:(signal?:AbortSignal)=>get<StudyPreferences>('/language/study/preferences',undefined,signal),
  saveStudyPreferences:(preferences:StudyPreferences)=>put<StudyPreferences>('/language/study/preferences',preferences),
  refreshIndex:(source_id:string,name:string,previous_names:string[]=[])=>
    post<IndexRefresh>('/language/document/index',{source_id,name,previous_names}),
  folders:()=>get<LanguageFolder[]>('/language/folders'),
  files:(source_id:string)=>get<{files:LanguageFile[]}>('/language/documents',{source_id}),
  read:(source_id:string,name:string)=>get<LanguageDocument>('/language/document',{source_id,name}),
  create:(source_id:string)=>post<LanguageDocument>('/language/documents',{source_id}),
  rename:(source_id:string,name:string,new_name:string,revision:string)=>
    post<LanguageDocument>('/language/document/rename',{source_id,name,new_name,revision}),
  save:(source_id:string,name:string,content:string,revision:string,session_id:string)=>
    put<LanguageDocument>('/language/document',{source_id,name,content,revision,session_id}),
};

import {get, post, put} from '../../lib/http';

export interface LanguageFolder {source_id:string; name:string;}
export interface LanguageFile {name:string; size:number;}
export interface IndexRefresh {status:"complete"|"failed"; warning:string;}
export interface LanguageDocument {name:string; content:string; revision:string; index_refresh?:IndexRefresh;}

export const languageApi = {
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

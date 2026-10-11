import {writable} from 'svelte/store';
import {persistentStorageKey} from './product';

export type ModuleSidebarOwner = 'youtube' | 'language' | 'ost';
function normalizePosition(value:unknown) {
  return typeof value==='number'&&Number.isFinite(value)?Math.min(90,Math.max(10,value)):50;
}
function persisted<T>(key:string,fallback:T,normalize:(value:unknown)=>T) {
  let value=fallback;
  try {const saved=localStorage.getItem(key);if(saved!==null)value=normalize(JSON.parse(saved));} catch { /* Keep the usable default when storage is unavailable. */ }
  const store=writable(value);
  store.subscribe(current=>{try {localStorage.setItem(key,JSON.stringify(current));} catch { /* UI state remains usable without persistence. */ }});
  return store;
}
function create(owner:ModuleSidebarOwner) {
  return {
    open:persisted(persistentStorageKey(owner+'-sidebar-open'),true,value=>typeof value==='boolean'?value:true),
    position:persisted(persistentStorageKey(owner+'-sidebar-handle-position'),50,normalizePosition),
  };
}
const preferences=new Map<ModuleSidebarOwner,ReturnType<typeof create>>();
export function moduleSidebarPreferences(owner:ModuleSidebarOwner) {
  let value=preferences.get(owner);
  if(!value){value=create(owner);preferences.set(owner,value);}
  return value;
}

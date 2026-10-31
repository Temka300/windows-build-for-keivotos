import { get, post, put } from './http';
import type { DiagnosticPolicy } from './suiteApiTypes';
import type { BackupOptions, DiagnosticsPreferences, UserSetting, StorageConfiguration, BackupComponents, BackupEstimateDetail, BackupEstimate, BackupListItem, BackupConfiguration, BackupResult, BackupManifest, BackupRestoreResult, ThumbnailCacheStatus } from './suiteApiTypes';

export interface StorageUsage {
 data_location:string;total_bytes:number;categories:Record<string,number>;
 modules:{id:string;name:string}[];
 library_files:{categories:{id:string;name:string;bytes:number;files:number;unknown_files:number}[];
  total_bytes:number;total_files:number;unknown_files:number;index_unavailable:boolean};
 thumbnails:ThumbnailCacheStatus & {tier_bytes:Record<string,number>};
 cleanup_on_startup:boolean;unreadable:number;
}
export interface CollectionFolder {source_id:string;path:string;display_name:string;role:string}
export interface CollectionPreview {sources:{id:string;name:string;owner:string}[];files:number;bytes?:number;personal_records?:number}
export interface CollectionResult {status:string;path:string;files:number;sources:number}
export const suiteDataApi = {
 getDiagnosticPolicy:()=>get<DiagnosticPolicy>("/diagnostics/policy"),
 configureDiagnosticPolicy:(limits:DiagnosticPolicy["limits"])=>put<DiagnosticPolicy>("/diagnostics/policy",limits),
 setDiagnosticDebug:(active:boolean)=>post<DiagnosticPolicy>("/diagnostics/debug",{active}),
 collectionSources:()=>get<CollectionFolder[]>('/local-recovery/collections'),
 rebuildIndex:()=>post<{status:string}>('/local-recovery/rebuild-index'),
 previewCollectionExport:(source_ids:string[],include_personal:boolean)=>post<CollectionPreview>('/local-recovery/collections/export-preview',{source_ids,include_personal}),
 exportCollection:(source_ids:string[],destination:string,include_personal:boolean,request_id:string)=>post<CollectionResult>('/local-recovery/collections/export',{source_ids,destination,include_personal,request_id}),
 previewCollectionImport:(directory:string,destination:string,include_personal:boolean)=>post<CollectionPreview>('/local-recovery/collections/import-preview',{directory,destination,include_personal}),
 importCollection:(directory:string,destination:string,include_personal:boolean,request_id:string)=>post<CollectionResult>('/local-recovery/collections/import',{directory,destination,include_personal,request_id}),
 getStorageUsage:()=>get<StorageUsage>('/storage/usage'),
 setCacheStartup:(enabled:boolean)=>put('/storage/cache-preferences',{cleanup_on_startup:enabled}),
getDiagnostics: () => get<DiagnosticsPreferences>('/diagnostics'),
configureDiagnostics: (preferences: DiagnosticsPreferences) => put<DiagnosticsPreferences>('/diagnostics', preferences),
openDiagnostics: (target: 'data' | 'logs' | 'runtime' | 'access' | 'backups') => post<{ status: string }>(`/diagnostics/open/${target}`),
getUserSetting: (key: string) =>
    get<UserSetting>(`/user-settings/${encodeURIComponent(key)}`),

putUserSetting: (key: string, value: string) =>
    put<UserSetting>(`/user-settings/${encodeURIComponent(key)}`, { value }),

getStorageConfiguration: () => get<StorageConfiguration>('/storage'),

getAutomaticBackupStatus: () => get<BackupConfiguration['automatic_status']>('/backups/automatic-status'),

getBackupConfiguration: () => get<BackupConfiguration>('/backups'),



configureBackups: (components: BackupComponents, options?: BackupOptions) =>
    put<BackupConfiguration>('/backups', { components, options }),

estimateBackup: (components: BackupComponents) =>
    post<BackupEstimate>('/backups/estimate', { components }),

createMetadataBackup: (components: BackupComponents) =>
    post<BackupResult>('/backups/create', { components }),

inspectMetadataBackup: (name: string) =>
    get<BackupManifest>(`/backups/${encodeURIComponent(name)}/inspect`),

restoreMetadataBackup: (name: string) =>
    post<BackupRestoreResult>('/backups/restore', { name }),

getThumbnailCache: () => get<ThumbnailCacheStatus>('/thumbnails/cache'),

cleanupThumbnailCache: () =>
    post<ThumbnailCacheStatus & { removed: number; removed_bytes: number }>('/thumbnails/cache/cleanup'),

clearThumbnailCache: () =>
    post<ThumbnailCacheStatus & { removed: number }>('/thumbnails/cache/clear'),

setThumbnailCacheLimit: (limitGb: number) =>
    put<ThumbnailCacheStatus>('/thumbnails/cache/limit', { limit_gb: limitGb }),
};

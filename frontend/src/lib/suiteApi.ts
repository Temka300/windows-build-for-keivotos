import {diagnosticFetch} from './diagnostics';
import { apiError } from './http';
import { VERSION } from './product';
import {folderRescanChanges} from './suiteStores';
// Suite module registry client (V1.1.0). Isolated, like filesApi.ts.

const BASE = '/api/suite';

export interface SuiteModule {
  id: string;
  slug: string;
  name: string;
  enabled: boolean;
  disableable: boolean;
  is_base: boolean;
  api_prefix: string;
  description: string;
  experimental: boolean;
  accepts_folders?: boolean;
}

export interface ModuleStatus {
  id: string;
  state: 'disabled' | 'starting' | 'running' | 'stopping' | 'failed' | 'unavailable';
  error: string | null;
}

export interface FolderChange {
  source_id: string | null;
  path: string;
  display_name: string;
  role: string;
  visible: boolean;
  forget: boolean;
}

export interface ManagedFolder extends FolderChange {
  source_id: string;
  added_at: string | null;
}

export interface FolderBatchResult {
  sources: ManagedFolder[];
  adopted: string[];
  released: string[];
  forgotten: string[];
  operations: Record<string, unknown>[];
}

export interface FolderForgetPreview {
  source_id: string;
  display_name: string;
  base_files: number;
  module_files: number;
  sidecars_preserved: number;
}

export interface FolderRescanResult {
  source_id: string;
  /** Base filesystem index counts (added/updated/…). */
  base: Record<string, number>;
  /** Owning module's rescan result — e.g. {status, active_tool_id} for Danbooru. */
  module: Record<string, unknown>;
}

export interface FolderRelocateResult {
  source_id: string;
  files_updated: number;
}

/** The folder's own name, used as the default label for a newly added source. */
export function displayNameForPath(path: string): string {
  const parts = path.replace(/[\\/]+$/, '').split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

/** Compare absolute paths using their native Windows or POSIX case rules. */
export function normalizedPath(path: string): string {
  const normalized = path.replace(/[\\/]+/g, '/').replace(/\/+$/, '');
  const windowsPath = /^(?:[a-z]:[\\/]|[\\/]{2})/i.test(path);
  return windowsPath ? normalized.toLowerCase() : normalized;
}

async function getJson<T>(path: string): Promise<T> {
  const res = await diagnosticFetch(BASE + path);
  if (!res.ok) throw await apiError(res);
  return res.json();
}

async function post<T>(path: string): Promise<T> {
  const res = await diagnosticFetch(BASE + path, { method: 'POST' });
  if (!res.ok) throw await apiError(res);
  return res.json();
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await diagnosticFetch(BASE + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await apiError(res);
  return res.json();
}

async function putJson<T>(path: string, body: unknown): Promise<T> {
  const res = await diagnosticFetch(BASE + path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await apiError(res);
  return res.json();
}

export interface ReleasePublication {
  version: string;
  tag_name: string;
  published_at: string | null;
}

let releaseRead: Promise<ReleasePublication | null> | undefined;
function installedRelease(): Promise<ReleasePublication | null> {
  return releaseRead ??= getJson<ReleasePublication>('/release').then(value =>
    value.version === VERSION && value.tag_name === `v${VERSION}` ? value : null,
  ).catch(() => null);
}

export const suiteApi = {
  installedRelease,
  moduleStatus: (id: string) => getJson<ModuleStatus>(`/modules/${encodeURIComponent(id)}/status`),
  retryModule: (id: string) => post<ModuleStatus>(`/modules/${encodeURIComponent(id)}/retry`),
  plannedModules: () => getJson<{id:string; name:string; experimental:boolean}[]>('/modules/planned'),
  listModules: () => getJson<SuiteModule[]>('/modules'),
  enableModule: (id: string) => post<SuiteModule>(`/modules/${encodeURIComponent(id)}/enable`),
  disableModule: (id: string) => post<SuiteModule>(`/modules/${encodeURIComponent(id)}/disable`),
  applyFolderChanges: (folders: FolderChange[]) =>
    postJson<FolderBatchResult>('/folders/apply', { folders }),
  previewFolderForget: (sourceId: string) =>
    getJson<FolderForgetPreview>(`/folders/${encodeURIComponent(sourceId)}/forget-preview`),
  rescanFolder: async (sourceId: string) => {
    const result = await post<FolderRescanResult>(`/folders/${encodeURIComponent(sourceId)}/rescan`);
    folderRescanChanges.update(value => value + 1);
    return result;
  },
  relocateFolder: (sourceId: string, path: string) =>
    putJson<FolderRelocateResult>(`/folders/${encodeURIComponent(sourceId)}/path`, { path }),
};

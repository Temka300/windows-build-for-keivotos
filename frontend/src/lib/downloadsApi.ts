import {diagnosticFetch} from './diagnostics';

export interface DownloadHistoryItem {
  id: string;
  owner: string;
  module_name: string;
  name: string;
  state: 'queued' | 'working' | 'completed' | 'failed' | 'cancelled' | 'interrupted';
  progress: number | null;
  started_at: string | null;
  completed_at: string | null;
  date_at: string | null;
  date_source: 'request' | 'transfer' | 'recorded-capture' | 'recorded-manifest' | 'unknown';
  attempt: number;
  can_delete: boolean;
  control_version: string | null;
  thumbnail_url: string | null;
}
export interface DownloadHistoryPage {items: DownloadHistoryItem[]; next_cursor: string | null;}
export interface DownloadControlState {paused: boolean; revision: number;}
export interface DownloadDeleteResult {deleted: boolean; requires_confirmation: boolean; file_count: number;}

class DownloadsError extends Error {
  constructor(public status: number, message: string) {super(message);}
}
async function read<T>(url: string, init: RequestInit = {}): Promise<T> {
  const response = await diagnosticFetch(url, init);
  if (!response.ok) throw new DownloadsError(response.status, 'The download request could not finish.');
  return response.json();
}
const base = '/api/downloads';
export const downloadsApi = {
  history(options: {limit?: number; after?: string; owner?: string} = {}, signal?: AbortSignal): Promise<DownloadHistoryPage> {
    const query = new URLSearchParams();
    if (options.limit !== undefined) query.set('limit', String(options.limit));
    if (options.after) query.set('after', options.after);
    if (options.owner) query.set('owner', options.owner);
    return read(base + '?' + query, {signal});
  },
  async item(id: string, signal?: AbortSignal): Promise<DownloadHistoryItem | null> {
    try {return await read(base + '/' + encodeURIComponent(id), {signal});}
    catch (error) {if (error instanceof DownloadsError && error.status === 404) return null; throw error;}
  },
  control(signal?: AbortSignal): Promise<DownloadControlState> {return read(base + '/control', {signal});},
  pause(paused: boolean, requestId: string): Promise<DownloadControlState> {
    return read(base + (paused ? '/pause' : '/resume'), {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({request_id: requestId})});
  },
  remove(item: DownloadHistoryItem, requestId: string, confirm = false): Promise<DownloadDeleteResult> {
    return read(base + '/' + encodeURIComponent(item.id) + '/delete', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({request_id: requestId, expected_version: item.control_version, confirm})});
  },
};

import { get, writable } from 'svelte/store';
import { persistedWritable } from './persistedStore';
import { persistentStorageKey } from './product';

export type NotificationPosition = 'bottom-right' | 'bottom-left' | 'top-right' | 'top-left';
export type NotificationTone = 'info' | 'success' | 'error';
export interface ActionNotification { id: string; message: string; tone: NotificationTone; progress?: number; destination?: 'downloads'; pending?: boolean; }
export const notificationPosition = persistedWritable<NotificationPosition>(persistentStorageKey('notification-position'), 'bottom-right',
  value => ['bottom-right', 'bottom-left', 'top-right', 'top-left'].includes(value as string) ? value as NotificationPosition : 'bottom-right');

const queue = writable<ActionNotification[]>([]);
export const notifications = { subscribe: queue.subscribe };
const duration = 6000;
let sequence = 0;
const clocks = new Map<string, { timer?: ReturnType<typeof setTimeout>; remaining: number; started: number; holds: Set<string> }>();
function progress(value?: number) { return value !== undefined && Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : undefined; }
function schedule(id: string) {
  const clock = clocks.get(id); if (!clock) return;
  clearTimeout(clock.timer);
  if (clock.holds.size || get(queue).find(item => item.id === id)?.pending) return;
  clock.started = Date.now();
  clock.timer = setTimeout(() => dismissNotification(id), clock.remaining);
}
export function dismissNotification(id: string) {
  clearTimeout(clocks.get(id)?.timer); clocks.delete(id);
  queue.update(items => items.filter(item => item.id !== id));
}
export function notify(message: string, options: { id?: string; tone?: NotificationTone; progress?: number; destination?: 'downloads'; pending?: boolean } = {}) {
  const id = options.id ?? 'notice-' + ++sequence;
  const item = { id, message, pending: options.pending, tone: options.tone ?? 'info', progress: progress(options.progress), destination: options.destination === 'downloads' ? 'downloads' as const : undefined };
  if (get(queue).some(existing => existing.id === id)) {
    queue.update(items => items.map(existing => existing.id === id ? item : existing));
    const clock = clocks.get(id)!; clock.remaining = duration; schedule(id);
    return id;
  }
  if (get(queue).length >= 5) dismissNotification(get(queue)[0].id);
  clocks.set(id, { remaining: duration, started: Date.now(), holds: new Set() });
  queue.update(items => [...items, item]); schedule(id);
  return id;
}
// Progress updates preserve the original expiry and never revive a dismissed notice.
export function updateNotification(id: string, values: Omit<ActionNotification, 'id'>) {
  if (!clocks.has(id)) return;
  if (values.pending) { const clock = clocks.get(id)!; clearTimeout(clock.timer); clock.timer = undefined; }
  queue.update(items => items.map(item => item.id === id ? { id, ...values, progress: progress(values.progress), destination: (values.destination ?? item.destination) === 'downloads' ? 'downloads' as const : undefined } : item));
}
export function pauseNotification(id: string, reason: 'pointer' | 'focus') {
  const clock = clocks.get(id); if (!clock || clock.holds.has(reason)) return;
  if (!clock.holds.size) { clearTimeout(clock.timer); clock.remaining = Math.max(0, clock.remaining - (Date.now() - clock.started)); }
  clock.holds.add(reason);
}
export function resumeNotification(id: string, reason: 'pointer' | 'focus') {
  const clock = clocks.get(id); if (!clock || !clock.holds.delete(reason)) return;
  if (!clock.holds.size) schedule(id);
}

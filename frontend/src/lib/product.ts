export const SUITE_NAME = 'Keivotos';
export const VERSION = '1.2.5';
export const RELEASED_AT = '';
export const DISPLAY_NAME = SUITE_NAME;
export const DEFAULT_PROFILE_NAME = SUITE_NAME;

export function formatReleaseDate(value: string): string | null {
  const parts = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!parts) return null;
  const [, year, month, day, hour, minute, second, offset] = parts;
  const calendar = new Date(`${year}-${month}-${day}T00:00:00Z`);
  if (Number(year) < 1 || !Number.isFinite(calendar.getTime()) || calendar.getUTCFullYear() !== Number(year)
    || calendar.getUTCMonth() + 1 !== Number(month) || calendar.getUTCDate() !== Number(day)
    || Number(hour) > 23 || Number(minute) > 59 || Number(second) > 59
    || (offset !== 'Z' && (Number(offset.slice(1, 3)) > 23 || Number(offset.slice(4)) > 59))) return null;
  const instant = new Date(value);
  if (!Number.isFinite(instant.getTime())) return null;
  const date = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(instant);
  const part = (type: string) => date.find(item => item.type === type)?.value;
  const displayYear = part('year') ?? '';
  if (displayYear.length > 4) return null;
  return `${displayYear.padStart(4, '0')}/${part('month')}/${part('day')}`;
}

export const STORAGE_PREFIX = 'keivotos:';
const LEGACY_STORAGE_PREFIXES = ['danbooru:'];

const persistedSuffixes = new Set([
  'profile-name',
  'startup-view',
  'home-layout',
  'last-view',
  'active-module',
  'active-rating',
  'browse-sort',
  'browse-sort-order',
  'sidebar-open',
  'sidebar-handle-position',
  'fit-mode',
  'image-size',
  'image-page-size',
  'media-autoplay',
  'heart-spam-enabled',
  'artist-notifications-enabled',
  'artist-notification-interval',
  'motion-preference',
  'interface-scale',
  'tag-banner-height',
]);
const persistedSuffixFamilies = ['daily-challenge-v2:', 'home:', 'tag-cover:'];

function migratePersistedStorage(): void {
  if (typeof localStorage === 'undefined') return;
  try {
    const keys = Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index))
      .filter((key): key is string => key !== null);
    for (const key of keys) {
      if (key.startsWith(STORAGE_PREFIX)) continue;
      const legacyPrefix = LEGACY_STORAGE_PREFIXES.find((prefix) => key.startsWith(prefix));
      if (!legacyPrefix) continue;
      const suffix = key.slice(legacyPrefix.length);
      if (!persistedSuffixes.has(suffix) && !persistedSuffixFamilies.some(prefix => suffix.startsWith(prefix))) continue;
      const currentKey = `${STORAGE_PREFIX}${suffix}`;
      if (localStorage.getItem(currentKey) === null) {
        const value = localStorage.getItem(key);
        if (value !== null) localStorage.setItem(currentKey, value);
      }
    }
  } catch {
    // Storage can be unavailable in hardened browser contexts; defaults remain safe.
  }
}

migratePersistedStorage();

export function persistentStorageKey(suffix: string): string {
  return `${STORAGE_PREFIX}${suffix}`;
}

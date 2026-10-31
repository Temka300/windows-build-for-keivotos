import { persistedWritable, normalizeBoolean, normalizeBooleanFalse } from './persistedStore';
import { persistentStorageKey } from './product';

export function normalizeHideControlsSeconds(value: unknown): number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 1 && value <= 10 ? value : 5;
}

export const hideControlsSeconds = persistedWritable(
  persistentStorageKey('player-hide-controls-seconds'),
  5,
  normalizeHideControlsSeconds,
);

export const HOLD_SPEED_MULTIPLIERS = [1.25, 1.5, 2] as const;
export type HoldSpeedMultiplier = typeof HOLD_SPEED_MULTIPLIERS[number];
export function normalizeHoldSpeedMultiplier(value: unknown): HoldSpeedMultiplier {
  return typeof value === 'number' && (HOLD_SPEED_MULTIPLIERS as readonly number[]).includes(value) ? value as HoldSpeedMultiplier : 1.5;
}
export const holdSpaceEnabled = persistedWritable(persistentStorageKey('player-hold-space'), true, normalizeBoolean);
export const holdSpeedMultiplier = persistedWritable(persistentStorageKey('player-hold-speed-multiplier'), 1.5 as HoldSpeedMultiplier, normalizeHoldSpeedMultiplier);

export const musicShuffle = persistedWritable(persistentStorageKey('ost-shuffle'), false, normalizeBooleanFalse);

export type PlaybackOwner = 'video' | 'youtube' | 'ost';
export type VideoPlaybackOwner = 'video' | 'youtube';
export const PLAYBACK_SPEEDS = [0.5,0.75,1,1.25,1.5,1.75,2] as const;
export type PlaybackSpeed = typeof PLAYBACK_SPEEDS[number];
export function normalizePlaybackSpeed(value: unknown): PlaybackSpeed {
  return typeof value==='number' && (PLAYBACK_SPEEDS as readonly number[]).includes(value) ? value as PlaybackSpeed : 1;
}
const speeds = {
  video: persistedWritable(persistentStorageKey('video-playback-speed'), 1 as PlaybackSpeed, normalizePlaybackSpeed),
  youtube: persistedWritable(persistentStorageKey('youtube-playback-speed'), 1 as PlaybackSpeed, normalizePlaybackSpeed),
};
export function speedFor(owner: VideoPlaybackOwner) { return speeds[owner]; }
export type LoopMode = 'off' | 'all' | 'one';
export function normalizeLoopMode(value: unknown): LoopMode {
  return value === 'all' || value === 'one' ? value : 'off';
}

const loopModes = {
  video: persistedWritable(persistentStorageKey('video-loop-mode'), 'off' as LoopMode, normalizeLoopMode),
  youtube: persistedWritable(persistentStorageKey('youtube-loop-mode'), 'off' as LoopMode, normalizeLoopMode),
  ost: persistedWritable(persistentStorageKey('ost-loop-mode'), 'off' as LoopMode, normalizeLoopMode),
};
export function loopModeFor(owner: PlaybackOwner) { return loopModes[owner]; }

function normalizeAutoNext(value: unknown): boolean { return value === true; }
const autoNextModes = {
  video: persistedWritable(persistentStorageKey('video-auto-next'), false, normalizeAutoNext),
  youtube: persistedWritable(persistentStorageKey('youtube-auto-next'), false, normalizeAutoNext),
  ost: persistedWritable(persistentStorageKey('ost-auto-next'), true, normalizeAutoNext),
};
export function autoNextFor(owner: PlaybackOwner) { return autoNextModes[owner]; }

export function normalizeVolumePercent(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? Math.min(100, Math.max(0, Math.round(value))) : 100;
}

const volumes = {
  video: persistedWritable(persistentStorageKey('video-volume-percent'), 100, normalizeVolumePercent),
  youtube: persistedWritable(persistentStorageKey('youtube-volume-percent'), 100, normalizeVolumePercent),
  ost: persistedWritable(persistentStorageKey('ost-volume-percent'), 100, normalizeVolumePercent),
};
export function volumeFor(owner: PlaybackOwner) { return volumes[owner]; }

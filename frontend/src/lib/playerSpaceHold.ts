export const SPACE_HOLD_DELAY_MS = 250;

export function isSpaceKey(event: {key: string; code?: string}): boolean {
  return event.code === 'Space' || event.key === ' ' || event.key === 'Spacebar';
}

// One owned gesture: a cancelled press must never become a delayed tap.
export function createSpaceHold(actions: {tap: () => void; boost: () => void; restore: () => void}) {
  let pressed = false;
  let boosted = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  function finish(tap: boolean) {
    if (timer !== undefined) clearTimeout(timer);
    timer = undefined;
    const restore = boosted;
    pressed = boosted = false;
    if (restore) actions.restore();
    else if (tap) actions.tap();
  }
  return {
    get pending() { return pressed; },
    get boosted() { return boosted; },
    press(enabled: boolean, repeat = false): boolean {
      if (repeat || pressed) return false;
      if (!enabled) { actions.tap(); return true; }
      pressed = true;
      timer = setTimeout(() => {
        timer = undefined;
        if (!pressed) return;
        boosted = true;
        try { actions.boost(); }
        catch (error) { finish(false); throw error; }
      }, SPACE_HOLD_DELAY_MS);
      return true;
    },
    release(allowTap = true): boolean {
      if (!pressed) return false;
      finish(allowTap);
      return true;
    },
    cancel() { finish(false); },
  };
}

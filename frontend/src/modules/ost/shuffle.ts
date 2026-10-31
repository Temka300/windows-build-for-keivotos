export const MAX_SHUFFLE_HISTORY = 256;

// Only this Music queue's transient order is kept here; identities stay opaque.
export function createShuffleQueue(random: () => number = () => Math.random()) {
  let pool = new Set<string>(), seen = new Set<string>();
  let bag: string[] = [], history: string[] = [], cursor = -1;
  function shuffled(values: string[]) {
    for (let index = values.length - 1; index > 0; index--) {
      const pick = Math.floor(random() * (index + 1));
      [values[index], values[pick]] = [values[pick], values[index]];
    }
    return values;
  }
  function current() { return history[cursor] ?? null; }
  function historyIndex(direction: -1 | 1) {
    for (let index = cursor + direction; index >= 0 && index < history.length; index += direction) {
      if (pool.has(history[index]) && history[index] !== current()) return index;
    }
    return -1;
  }
  function reset(item: string | null, identities: readonly string[]) {
    pool = new Set(identities); seen = new Set(item === null ? [] : [item]);
    history = item === null ? [] : [item]; cursor = history.length - 1;
    bag = shuffled([...pool].filter(id => !seen.has(id)));
  }
  function reconcile(item: string, identities: readonly string[]) {
    if (current() !== item) { reset(item, identities); return; }
    pool = new Set(identities);
    const kept = bag.filter(id => pool.has(id) && !seen.has(id));
    const pending = new Set(kept);
    const additions = [...pool].filter(id => !seen.has(id) && !pending.has(id));
    if (kept.length !== bag.length || additions.length) bag = shuffled([...kept, ...additions]);
  }
  function move(direction: -1 | 1, wrap = false): string | null {
    const item = current(); if (item === null) return null;
    const stored = historyIndex(direction);
    if (stored >= 0) { cursor = stored; return history[cursor]; }
    if (direction === -1) return null;
    if (!bag.length && wrap) {
      seen = new Set([item]); bag = shuffled([...pool].filter(id => id !== item));
    }
    const next = bag.pop(); if (next === undefined) return null;
    seen.add(next);
    history = [...history.slice(0, cursor + 1), next].slice(-MAX_SHUFFLE_HISTORY);
    cursor = history.length - 1;
    return next;
  }
  return {
    reset, reconcile, move,
    get canPrevious() { return cursor >= 0 && historyIndex(-1) >= 0; },
    get canNext() { return cursor >= 0 && (historyIndex(1) >= 0 || bag.length > 0); },
    get current() { return current(); },
    get historyLength() { return history.length; },
  };
}

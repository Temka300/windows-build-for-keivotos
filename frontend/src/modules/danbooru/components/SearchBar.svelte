<script lang="ts">
  import { onDestroy } from 'svelte';
  import { activeTags, activeCollectionId, browseTagSelection, viewMode } from '../stores';
  import { danbooruApi as api } from '../api';
  import { type TagInfo } from '../apiTypes';

  let inputValue = '';
  let searchInput: HTMLInputElement;
  let suggestions: TagInfo[] = [];
  let showSuggestions = false;
  let highlightedSuggestionIndex = -1;
  let debounceTimer: ReturnType<typeof setTimeout>;
  let blurTimer: ReturnType<typeof setTimeout>;
  let suggestionRequest: AbortController | null = null;
  let suggestionSerial = 0, alive = true, composing = false, focused = false;
  onDestroy(() => { alive = false; cancelSuggestions(); clearTimeout(blurTimer); });

  const tagPrefixes = new Set(['artist', 'character', 'copyright', 'general', 'meta', 'unknown']);
  const idPrefixes = new Set(['id', 'post', 'post_id', 'danbooru', 'danbooru_id', 'danbooru_post_id']);
  const filenamePrefixes = new Set(['filename', 'file', 'name']);
  const shapePrefixes = new Set(['shape', 'aspect', 'aspect_ratio', 'preset']);
  const shapeTerms = new Set([
    'vertical', 'portrait', 'horizontal', 'landscape', 'wide',
    'phone', 'phones', 'phone_sized', 'phone_size', 'phone_wallpaper', 'phone_wallpapers',
    'mobile', 'mobile_sized', 'mobile_wallpaper',
    'banner', 'banners', 'banner_sized', 'banner_size', 'wide_banner', 'header', 'headers',
    'logo', 'logos', 'logo_sized', 'logo_size', 'icon', 'icons', 'avatar', 'avatars', 'square',
  ]);

  function normalizeDimensionTerms(value: string): string {
    return value.replace(/(\d+)\s*[xX\u00d7]\s*(\d+)/g, '$1x$2');
  }

  function normalizeSearchTerms(value: string): string {
    return normalizeDimensionTerms(value)
      .replace(/\b(phone|mobile|banner|logo)\s+(sized?|wallpapers?)\b/gi, '$1_$2')
      .replace(/\bwide\s+banner\b/gi, 'wide_banner');
  }

  function normalizeShapeTerm(value: string): string {
    return value.trim().toLowerCase().replace(/[\s-]+/g, '_');
  }

  function pendingSuggestionQuery(term: string): string | null {
    let value = term.trim();
    if (value.startsWith('-')) value = value.slice(1);
    if (!value) return null;
    if (/^#\d*$/.test(value)) return null;
    if (/.+\.(?:jpe?g|png|webp|gif|jfif|mp4|webm)$/i.test(value)) return null;
    if (shapeTerms.has(normalizeShapeTerm(value))) return null;
    if (/^(>=|<=|>|<|=)?\d+[xX\u00d7]\d+$/.test(normalizeDimensionTerms(value))) return null;

    if (value.includes(':')) {
      const [prefix, rest] = value.split(':', 2);
      if (idPrefixes.has(prefix.toLowerCase())) return null;
      if (filenamePrefixes.has(prefix.toLowerCase())) return null;
      if (shapePrefixes.has(prefix.toLowerCase())) return null;
      if (!tagPrefixes.has(prefix.toLowerCase())) return null;
      value = rest;
    }

    return value.length >= 1 ? value : null;
  }

  function termForSuggestion(rawTerm: string, tagName: string): string {
    let value = rawTerm.trim();
    const isExclude = value.startsWith('-');
    if (isExclude) value = value.slice(1);

    if (value.includes(':')) {
      const [prefix] = value.split(':', 1);
      if (tagPrefixes.has(prefix.toLowerCase())) {
        return `${isExclude ? '-' : ''}${prefix.toLowerCase()}:${tagName}`;
      }
    }

    return `${isExclude ? '-' : ''}${tagName}`;
  }

  function cancelSuggestions(hide = true) {
    clearTimeout(debounceTimer); suggestionRequest?.abort(); suggestionRequest = null; suggestionSerial++;
    if (hide) { suggestions = []; showSuggestions = false; highlightedSuggestionIndex = -1; }
  }

  function onInput() {
    cancelSuggestions();
    if (composing || !focused) return;
    const value = inputValue, serial = suggestionSerial;
    debounceTimer = setTimeout(async () => {
      const words = normalizeSearchTerms(value).trim().split(/\s+/);
      const query = pendingSuggestionQuery(words[words.length - 1] || '');
      if (!query || !alive || !focused || composing || serial !== suggestionSerial) return;
      const current = new AbortController(); suggestionRequest = current;
      try {
        const result = await api.suggestTags(query, undefined, current.signal);
        if (!alive || !focused || composing || current.signal.aborted || serial !== suggestionSerial || value !== inputValue) return;
        suggestions = result; showSuggestions = result.length > 0;
        highlightedSuggestionIndex = result.length > 0 ? 0 : -1;
      } catch {
        if (!alive || current.signal.aborted || serial !== suggestionSerial) return;
        suggestions = []; showSuggestions = false; highlightedSuggestionIndex = -1;
      }
    }, 250);
  }

  function openSearchResults() {
    if (['home', 'profile', 'collections', 'timelapse', 'challenges'].includes($viewMode)) {
      browseTagSelection.set(null); activeCollectionId.set(null); viewMode.set('gallery');
    }
  }

  function onFocus() { focused = true; clearTimeout(blurTimer); onInput(); }
  function beginComposition() { composing = true; cancelSuggestions(); }
  function endComposition() { composing = false; onInput(); }

  function commitTerms(text: string) {
    const terms = normalizeSearchTerms(text).trim().split(/\s+/).filter(Boolean);
    if (terms.length === 0 || composing) return;
    cancelSuggestions();
    activeTags.update(existing => {
      const combined = [...existing];
      for (const t of terms) {
        if (!combined.includes(t)) combined.push(t);
      }
      return combined;
    });
    openSearchResults();
    inputValue = '';
    suggestions = [];
    showSuggestions = false;
    highlightedSuggestionIndex = -1;
  }

  function selectSuggestion(tag: TagInfo) {
    if (composing) return;
    cancelSuggestions();
    const words = inputValue.trim().split(/\s+/);
    const last = words.pop() || '';
    const pending = words.join(' ');
    if (pending) commitTerms(pending);
    activeTags.update(existing =>
      existing.includes(termForSuggestion(last, tag.name)) ? existing : [...existing, termForSuggestion(last, tag.name)]
    );
    openSearchResults();
    inputValue = '';
    suggestions = [];
    showSuggestions = false;
    highlightedSuggestionIndex = -1;
  }

  function onKeydown(e: KeyboardEvent) {
    if (composing || e.isComposing || e.keyCode === 229) return;
    if (showSuggestions && suggestions.length > 0 && e.key === 'ArrowDown') {
      e.preventDefault();
      highlightedSuggestionIndex = (highlightedSuggestionIndex + 1) % suggestions.length;
      return;
    }

    if (showSuggestions && suggestions.length > 0 && e.key === 'ArrowUp') {
      e.preventDefault();
      highlightedSuggestionIndex = highlightedSuggestionIndex <= 0
        ? suggestions.length - 1
        : highlightedSuggestionIndex - 1;
      return;
    }

    if (e.key === 'Enter') {
      if (showSuggestions && highlightedSuggestionIndex >= 0 && suggestions[highlightedSuggestionIndex]) {
        e.preventDefault();
        selectSuggestion(suggestions[highlightedSuggestionIndex]);
        return;
      }
      commitTerms(inputValue);
    }
    if (e.key === 'Escape') cancelSuggestions();
  }

  function isTextEntryTarget(target: EventTarget | null): boolean {
    if (!(target instanceof HTMLElement)) return false;
    const tagName = target.tagName.toLowerCase();
    return tagName === 'input' || tagName === 'textarea' || tagName === 'select' || target.isContentEditable;
  }

  function onWindowKeydown(e: KeyboardEvent) {
    if (composing || e.isComposing || e.keyCode === 229 || isTextEntryTarget(e.target) || e.ctrlKey || e.altKey || e.metaKey) return;
    if (e.key.length !== 1) return;

    e.preventDefault();
    inputValue += e.key;
    searchInput?.focus();
    requestAnimationFrame(() => {
      const end = searchInput.value.length;
      searchInput.setSelectionRange(end, end);
    });
    onInput();
  }

  function onBlur() {
    focused = false; cancelSuggestions(false); clearTimeout(blurTimer);
    blurTimer = setTimeout(() => { showSuggestions = false; }, 200);
  }

  const categoryColors: Record<string, string> = {
    artist: 'text-red-400',
    character: 'text-green-400',
    copyright: 'text-copyright-400',
    general: 'text-blue-400',
    meta: 'text-yellow-400',
  };
</script>

<svelte:window on:keydown={onWindowKeydown} />

<div class="relative">
  <input
    bind:this={searchInput}
    type="text"
    bind:value={inputValue}
    on:input={onInput}
    on:keydown={onKeydown}
    on:blur={onBlur}
    on:focus={onFocus}
    on:compositionstart={beginComposition}
    on:compositionend={endComposition}
    placeholder="Search tags, filenames, IDs, size, or shape..."
    class="w-full bg-[#1e1e2e] border border-[#2a2a3a] rounded-lg px-3 py-1.5 text-sm text-gray-200 placeholder-gray-500 outline-none focus:border-purple-500 transition-colors"
  />

  {#if showSuggestions}
    <div class="absolute z-50 top-full left-0 right-0 mt-1 bg-[#1e1e2e] border border-[#2a2a3a] rounded-lg shadow-xl max-h-64 overflow-y-auto">
      {#each suggestions as tag, i}
        <button
          class="w-full px-3 py-1.5 text-left text-sm flex justify-between items-center transition-colors {i === highlightedSuggestionIndex ? 'bg-purple-600/20' : 'hover:bg-[#2a2a3a]'}"
          on:mousedown|preventDefault={() => selectSuggestion(tag)}
          on:mouseenter={() => highlightedSuggestionIndex = i}
        >
          <span class={categoryColors[tag.category] || 'text-gray-300'}>{tag.name}</span>
          <span class="text-xs text-gray-500">{tag.category} ({tag.count})</span>
        </button>
      {/each}
    </div>
  {/if}
</div>

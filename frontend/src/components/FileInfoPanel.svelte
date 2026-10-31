<script lang="ts">
  // The "File information" modal, opened from a browse tile's right-click Show
  // info. Two tabs: Properties (system facts) and Origin (the user-authored
  // note). The Origin tab is a progressive editor — an empty note is a single
  // "Add info" button; picking a field type from its menu reveals that field
  // inline, and each field saves on blur. There is no in-app file preview.
  import { createEventDispatcher, onDestroy } from 'svelte';
  import { filesApi, type Annotation, type AnnotationLink, type AnnotationDetail, type AudioMetadata, type ModuleMetadataSection } from '../lib/filesApi';
  import { fileGlyph, linkKindLabel, type Subject } from '../lib/filePreview';

  export let subject: Subject;
  // The owning source's display name, resolved by the parent from the registry.
  export let sourceName = '';

  const dispatch = createEventDispatcher<{ close: void; changed: void; browse: void }>();
  const LINK_KINDS = ['source', 'discussion', 'mirror', 'author', 'other'];

  type FieldKey = 'description' | 'links' | 'author' | 'extra' | 'details';

  let activeTab: 'properties' | 'origin' = 'properties';

  let annotation: Annotation | null = null;
  let loading = false;
  let error = '';
  let actionError = '';
  let loadedKey = '';
  let uploading = false;
  let fileInput: HTMLInputElement;
  let audio: AudioMetadata | null = null, audioLoading = false, audioError = '';
  let audioSerial = 0, alive = true;
  let metadata: ModuleMetadataSection[] = [], metadataLoading = false, metadataError = '';
  let metadataSerial = 0, originSerial = 0, missingCover = false;
  const audioExtensions = new Set(['mp3', 'flac', 'wav', 'ogg', 'oga', 'm4a']);
  onDestroy(() => { alive = false; audioSerial++; metadataSerial++; originSerial++; });

  // One working copy of the editable fields; every field commits the whole draft.
  let draft = { description: '', author: '', extra_info: '', links: [] as AnnotationLink[], details: [] as AnnotationDetail[] };
  let userEdited = false, defaultsUsed = false;
  let lastSaved = '';
  // Empty fields the user has chosen to add but not yet filled — kept visible.
  let added = new Set<FieldKey>();
  let addMenuOpen = false;

  $: key = `${subject.sourceId}::${subject.path}`;
  $: if (key !== loadedKey) {
    loadedKey = key;
    void load(subject);
    void loadAudio(subject);
    void loadMetadata(subject);
  }
  $: typeLabel = subject.isDir ? 'Folder' : subject.ext ? `${subject.ext.toUpperCase()} file` : 'File';
  $: musicCover = metadata.find(section => section.owner === 'ost' && section.artwork);
  $: metadataNotice = metadataError || audioError || (audio?.status === 'busy' ? 'Audio metadata is busy.' :
    audio && audio.status !== 'ready' ? 'Audio metadata is unavailable for this file.' :
    metadata.some(section => section.status === 'unavailable') ? 'Saved metadata is unavailable.' : '');
  $: technicalFacts = fileFacts(metadata, audio);
  $: if (!loading && !error && !metadataLoading && !audioLoading && !userEdited && !annotation?.metadata_applied) {
    fillEmptyFields(metadata, audio);
  }

  function fileFacts(sections: ModuleMetadataSection[], sound: AudioMetadata | null): AnnotationDetail[] {
    const facts = new Map<string, string>();
    for (const section of sections) for (const field of section.fields) {
      if (['Duration', 'Video codec', 'Audio codec'].includes(field.label) && field.value && field.value !== 'none') facts.set(field.label, field.value);
    }
    if (sound) {
      if (sound.duration !== null) facts.set('Duration', audioDuration(sound.duration));
      if (sound.samplerate !== null) facts.set('Sample rate', `${sound.samplerate} Hz`);
      if (sound.bitrate !== null) facts.set('Bitrate', `${Math.round(sound.bitrate)} kbps`);
      if (sound.bitdepth !== null) facts.set('Bit depth', `${sound.bitdepth} bit`);
      if (sound.channels !== null) facts.set('Channels', String(sound.channels));
    }
    return [...facts].map(([label, value]) => ({label, value}));
  }

  function fillEmptyFields(sections: ModuleMetadataSection[], sound: AudioMetadata | null) {
    let description = '', author = '', extra = '';
    const links: AnnotationLink[] = [], details: AnnotationDetail[] = [];
    for (const section of sections) {
      const value = (label: string) => section.fields.find(field => field.label === label)?.value ?? '';
      if (section.owner === 'youtube') {
        author ||= value('Channel');
        for (const label of ['Title', 'Uploaded', 'Downloaded']) if (value(label)) details.push({label, value: value(label)});
      } else if (section.owner === 'twitter') {
        description ||= value('Post text');
        author ||= [value('Author'), value('Handle')].filter(Boolean).join(' ');
        for (const label of ['Posted', 'Captured']) if (value(label)) details.push({label, value: value(label)});
      }
      for (const field of section.fields) if (field.href) {
        links.push({url: field.href, label: field.label === 'Channel URL' ? 'Channel' : field.label === 'Author URL' ? 'Profile' : '',
          kind: ['Channel URL', 'Author URL'].includes(field.label) ? 'author' : 'source'});
      }
    }
    if (sound) {
      author ||= (sound.tags.artist ?? []).join('; ');
      extra ||= (sound.tags.comment ?? []).join('\n');
      for (const name of ['title', 'album', 'albumartist', 'year', 'genre', 'composer', 'track', 'disc']) {
        let value = (sound.tags[name] ?? []).join('; ');
        const total = sound.tags[`${name}_total`]?.join('; ');
        if (['track', 'disc'].includes(name) && total) value = value ? `${value} / ${total}` : `of ${total}`;
        if (value) details.push({label: tagLabel(name), value});
      }
    }
    defaultsUsed ||= Boolean(description || author || extra || links.length || details.length);
    const heldLinks = new Set(draft.links.map(link => link.url.trim()));
    const heldDetails = new Set(draft.details.map(item => item.label.trim().toLocaleLowerCase()));
    draft = {...draft, description: draft.description || description, author: draft.author || author, extra_info: draft.extra_info || extra,
      links: [...draft.links, ...links.filter(link => {if (heldLinks.has(link.url)) return false; heldLinks.add(link.url); return true;})],
      details: [...draft.details, ...details.filter(item => {const label = item.label.toLocaleLowerCase(); if (heldDetails.has(label)) return false; heldDetails.add(label); return true;})].slice(0, 32)};
    // Reading and focus/blur are passive. Only an actual edit saves defaults.
    lastSaved = serialize();
  }

  async function loadMetadata(current: Subject) {
    const serial = ++metadataSerial;
    metadata = []; metadataLoading = true; metadataError = ''; missingCover = false;
    try {
      const result = await filesApi.moduleMetadata(current.sourceId, current.path);
      if (alive && serial === metadataSerial) metadata = result.sections;
    } catch (caught) {
      if (alive && serial === metadataSerial) metadataError = (caught as Error).message;
    } finally {
      if (alive && serial === metadataSerial) metadataLoading = false;
    }
  }

  async function loadAudio(current: Subject) {
    const serial = ++audioSerial;
    audio = null; audioError = ''; audioLoading = false;
    if (current.isDir || !audioExtensions.has(current.ext?.toLowerCase() ?? '')) return;
    audioLoading = true;
    try {
      const result = await filesApi.audioMetadata(current.sourceId, current.path);
      if (alive && serial === audioSerial) audio = result;
    } catch (caught) {
      if (alive && serial === audioSerial) audioError = (caught as Error).message;
    } finally {
      if (alive && serial === audioSerial) audioLoading = false;
    }
  }

  function audioDuration(value: number | null): string {
    if (value === null || !Number.isFinite(value)) return '—';
    const seconds = Math.floor(value);
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
  }

  function tagLabel(name: string): string {
    const labels: Record<string, string> = { title: 'Title', artist: 'Artist', album: 'Album',
      albumartist: 'Album artist', composer: 'Composer', genre: 'Genre', year: 'Year',
      track: 'Track', track_total: 'Total tracks', disc: 'Disc', disc_total: 'Total discs', comment: 'Comment' };
    return labels[name] ?? name;
  }

  $: showDescription = draft.description.trim() !== '' || added.has('description');
  $: showAuthor = draft.author.trim() !== '' || added.has('author');
  $: showExtra = draft.extra_info.trim() !== '' || added.has('extra');
  $: showDetails = draft.details.length > 0 || added.has('details');
  $: showLinks = draft.links.length > 0 || added.has('links');
  $: attachments = annotation?.attachments ?? [];
  $: addOptions = [
    !showDescription ? { key: 'description', label: 'Description' } : null,
    !showLinks ? { key: 'links', label: 'Link' } : null,
    !showAuthor ? { key: 'author', label: 'Author' } : null,
    { key: 'image', label: 'Image / video' },
    !showExtra ? { key: 'extra', label: 'Extra info' } : null,
    !showDetails ? { key: 'details', label: 'Details' } : null,
  ].filter((option): option is { key: string; label: string } => option !== null);

  async function load(current: Subject) {
    const serial = ++originSerial;
    loading = true;
    error = '';
    actionError = '';
    annotation = null;
    userEdited = false; defaultsUsed = false;
    draft = {description: '', author: '', extra_info: '', links: [], details: []};
    closeMenus();
    try {
      const result = await filesApi.getInfo(current.sourceId, current.path);
      if (!alive || serial !== originSerial) return;
      annotation = result;
    } catch (e) {
      if (alive && serial === originSerial) error = (e as Error).message;
    } finally {
      if (alive && serial === originSerial) loading = false;
    }
    if (alive && serial === originSerial) syncDraft();
  }

  function syncDraft() {
    draft = {
      description: annotation?.description ?? '',
      author: annotation?.author ?? '',
      extra_info: annotation?.extra_info ?? '',
      links: (annotation?.links ?? []).map((link) => ({ ...link })),
      details: (annotation?.details ?? []).map(item => ({...item})),
    };
    added = new Set();
    lastSaved = serialize();
  }

  function serialize(): string {
    return JSON.stringify({
      d: draft.description.trim(),
      a: draft.author.trim(),
      e: draft.extra_info.trim(),
      details: draft.details.map(item => ({label: item.label.trim(), value: item.value.trim()})).filter(item => item.label || item.value),
      l: draft.links
        .map((link) => ({ u: link.url.trim(), b: link.label.trim(), k: link.kind }))
        .filter((link) => link.u),
    });
  }

  function onKey(event: KeyboardEvent) {
    if (event.key !== 'Escape') return;
    if (addMenuOpen) {
      closeMenus();
      return;
    }
    dispatch('close');
  }

  function closeMenus() {
    addMenuOpen = false;
  }

  function addField(fieldKey: string) {
    userEdited = true;
    closeMenus();
    if (fieldKey === 'image') {
      fileInput.click();
      return;
    }
    added.add(fieldKey as FieldKey);
    added = added;
    if (fieldKey === 'links' && draft.links.length === 0) addLink();
    if (fieldKey === 'details' && draft.details.length === 0) addDetail();
  }

  function addDetail() {
    userEdited = true;
    draft.details = [...draft.details, {label: '', value: ''}];
  }

  function removeDetail(index: number) {
    userEdited = true;
    draft.details = draft.details.filter((_, i) => i !== index);
    void persist();
  }

  function addLink() {
    userEdited = true;
    draft.links = [...draft.links, { url: '', label: '', kind: 'source' }];
  }

  function removeLink(index: number) {
    userEdited = true;
    draft.links = draft.links.filter((_, i) => i !== index);
    void persist();
  }

  function removeField(fieldKey: FieldKey) {
    userEdited = true;
    if (fieldKey === 'description') draft.description = '';
    else if (fieldKey === 'author') draft.author = '';
    else if (fieldKey === 'extra') draft.extra_info = '';
    else if (fieldKey === 'links') draft.links = [];
    else if (fieldKey === 'details') draft.details = [];
    added.delete(fieldKey);
    added = added;
    void persist();
  }

  async function persist() {
    const links = draft.links
      .map((link) => ({ url: link.url.trim(), label: link.label.trim(), kind: link.kind }))
      .filter((link) => link.url !== '');
    for (const link of links) {
      if (!/^https?:\/\//i.test(link.url)) {
        actionError = `Links must start with http:// or https:// — check: ${link.url}`;
        return;
      }
    }
    const snapshot = serialize();
    if (snapshot === lastSaved) {
      actionError = '';
      return;
    }
    userEdited = true;
    const details = draft.details.map(item => ({label: item.label.trim(), value: item.value.trim()})).filter(item => item.label || item.value);
    if (details.some(item => !item.label || !item.value)) { actionError = 'Give each detail a name and a value.'; return; }
    if (new Set(details.map(item => item.label.toLocaleLowerCase())).size !== details.length) { actionError = 'Each detail needs a different name.'; return; }
    const serial = originSerial;
    actionError = '';
    try {
      const saved = await filesApi.saveInfo({
        source_id: subject.sourceId,
        path: subject.path,
        description: draft.description.trim(),
        author: draft.author.trim(),
        extra_info: draft.extra_info.trim(),
        links, details, metadata_applied: annotation?.metadata_applied || defaultsUsed,
      });
      if (!alive || serial !== originSerial) return;
      annotation = saved;
      lastSaved = snapshot;
      dispatch('changed');
    } catch (e) {
      if (alive && serial === originSerial) actionError = (e as Error).message;
    }
  }

  async function onUploadChange(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    input.value = '';
    if (!file) return;
    uploading = true;
    actionError = '';
    try {
      annotation = await filesApi.uploadAttachment(subject.sourceId, subject.path, file);
      dispatch('changed');
    } catch (e) {
      actionError = (e as Error).message;
    } finally {
      uploading = false;
    }
  }

  async function removeAttachment(attachmentId: number) {
    actionError = '';
    try {
      await filesApi.deleteAttachment(attachmentId);
      annotation = await filesApi.getInfo(subject.sourceId, subject.path);
      dispatch('changed');
    } catch (e) {
      actionError = (e as Error).message;
    }
  }

  // "Open" browses a folder inside Keivotos; a file has no in-app viewer, so it
  // falls back to the OS default app. "Show in folder" is the OS-explorer reveal.
  function openSubject() {
    if (subject.isDir) {
      dispatch('browse');
    } else {
      void openExternally();
    }
  }

  async function openExternally() {
    actionError = '';
    try {
      await filesApi.openFile(subject.sourceId, subject.path);
    } catch (e) {
      actionError = (e as Error).message;
    }
  }

  async function reveal() {
    actionError = '';
    try {
      await filesApi.revealFile(subject.sourceId, subject.path);
    } catch (e) {
      actionError = (e as Error).message;
    }
  }

  async function copyPath() {
    try {
      await navigator.clipboard.writeText(subject.absolutePath);
    } catch {
      // Clipboard can be unavailable; the path is still shown in full.
    }
  }

  function formatSize(bytes: number | null): string {
    if (bytes == null) return '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let n = bytes;
    let i = 0;
    while (n >= 1024 && i < units.length - 1) {
      n /= 1024;
      i += 1;
    }
    return `${i === 0 ? n : n < 10 ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
  }

  function formatDate(mtime: number | null): string {
    if (!mtime) return '—';
    return new Date(mtime * 1000).toLocaleString();
  }
</script>

<svelte:window on:keydown={onKey} />

<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<div
  class="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4"
  role="presentation"
  on:click={() => dispatch('close')}
>
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div
    class="flex h-[560px] max-h-[85vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-[#2a2a3a] bg-[#12121a] shadow-2xl"
    role="dialog"
    aria-modal="true"
    aria-label="File information"
    tabindex="-1"
    on:click|stopPropagation
  >
    <div class="flex items-center gap-2 border-b border-white/5 px-4 py-3">
      <span class="text-lg leading-none">{fileGlyph({ is_dir: subject.isDir, ext: subject.ext })}</span>
      <div class="min-w-0 flex-1">
        <div class="truncate text-sm font-medium text-gray-100" title={subject.name}>{subject.name}</div>
        <div class="text-[11px] text-gray-500">{typeLabel}</div>
      </div>
      <button
        type="button"
        class="grid h-7 w-7 place-items-center rounded-md text-gray-500 transition-colors hover:bg-white/5 hover:text-white"
        title="Close"
        aria-label="Close"
        on:click={() => dispatch('close')}
      >✕</button>
    </div>

    <div class="flex shrink-0 gap-4 border-b border-white/5 px-4">
      <button
        type="button"
        class="-mb-px border-b-2 py-2 text-sm transition-colors {activeTab === 'properties' ? 'border-purple-500 text-white' : 'border-transparent text-gray-400 hover:text-gray-200'}"
        on:click={() => (activeTab = 'properties')}
      >Properties</button>
      <button
        type="button"
        class="-mb-px border-b-2 py-2 text-sm transition-colors {activeTab === 'origin' ? 'border-purple-500 text-white' : 'border-transparent text-gray-400 hover:text-gray-200'}"
        on:click={() => (activeTab = 'origin')}
      >Origin</button>
    </div>

    <div class="flex-1 overflow-y-auto">
      {#if activeTab === 'properties'}
        <dl class="space-y-3 px-5 py-4 text-sm">
          <div class="flex justify-between gap-4">
            <dt class="shrink-0 font-medium text-gray-400">Name</dt>
            <dd class="min-w-0 break-words text-right text-gray-200">{subject.name}</dd>
          </div>
          <div class="flex justify-between gap-4">
            <dt class="shrink-0 font-medium text-gray-400">Type</dt>
            <dd class="text-gray-200">{typeLabel}</dd>
          </div>
          <div class="flex justify-between gap-4">
            <dt class="shrink-0 font-medium text-gray-400">Size</dt>
            <dd class="text-gray-200">{subject.isDir ? '—' : formatSize(subject.size)}</dd>
          </div>
          {#each technicalFacts as fact (fact.label)}
            <div class="flex justify-between gap-4"><dt class="shrink-0 font-medium text-gray-400">{fact.label}</dt><dd class="min-w-0 break-words text-right text-gray-200">{fact.value}</dd></div>
          {/each}
          <div class="flex justify-between gap-4">
            <dt class="shrink-0 font-medium text-gray-400">Modified</dt>
            <dd class="text-gray-200">{formatDate(subject.mtime)}</dd>
          </div>
          <div class="flex justify-between gap-4">
            <dt class="shrink-0 font-medium text-gray-400">MD5</dt>
            <dd class="min-w-0 truncate font-mono text-xs text-gray-400" title={annotation?.content_hash ?? ''}>
              {annotation?.content_hash ?? 'not computed'}
            </dd>
          </div>
          <div class="flex justify-between gap-4">
            <dt class="shrink-0 font-medium text-gray-400">Source</dt>
            <dd class="min-w-0 truncate text-gray-200" title={sourceName}>{sourceName || '—'}</dd>
          </div>
          <div>
            <dt class="mb-0.5 font-medium text-gray-400">Path</dt>
            <dd>
              <button
                type="button"
                class="w-full break-words text-left font-mono text-xs leading-relaxed text-gray-400 hover:text-gray-200"
                title="Click to copy"
                on:click={copyPath}
              >{subject.absolutePath}</button>
            </dd>
          </div>
        </dl>
      {:else if loading}
        <p class="px-4 py-3 text-sm text-gray-500">Loading…</p>
      {:else if error}
        <p class="px-4 py-3 text-xs text-red-300">{error}</p>
      {:else}
        <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
        <div class="px-4 py-3" on:click={closeMenus} on:input={() => userEdited = true}>
          {#if showDescription}
            <div class="mb-3">
              <div class="mb-1 flex items-center justify-between">
                <span class="text-[10px] uppercase tracking-wide text-gray-500">Description</span>
                <button type="button" class="text-gray-600 hover:text-red-300" title="Remove description" aria-label="Remove description" on:click={() => removeField('description')}>✕</button>
              </div>
              <textarea
                rows="3"
                bind:value={draft.description}
                on:blur={persist}
                placeholder="Where is this from? What is it?"
                class="w-full resize-y rounded-lg border border-[#2a2a3a] bg-[#1e1e2e] px-2 py-1.5 text-xs text-gray-200 outline-none placeholder:text-gray-600 focus:border-purple-500"
              ></textarea>
            </div>
          {/if}

          {#if showAuthor}
            <div class="mb-3">
              <div class="mb-1 flex items-center justify-between">
                <span class="text-[10px] uppercase tracking-wide text-gray-500">Author</span>
                <button type="button" class="text-gray-600 hover:text-red-300" title="Remove author" aria-label="Remove author" on:click={() => removeField('author')}>✕</button>
              </div>
              <input
                bind:value={draft.author}
                on:blur={persist}
                placeholder="Creator name"
                class="w-full rounded-lg border border-[#2a2a3a] bg-[#1e1e2e] px-2 py-1.5 text-xs text-gray-200 outline-none placeholder:text-gray-600 focus:border-purple-500"
              />
            </div>
          {/if}

          {#if showLinks}
            <div class="mb-3">
              <div class="mb-1 flex items-center justify-between">
                <span class="text-[10px] uppercase tracking-wide text-gray-500">Links</span>
                <button type="button" class="text-gray-600 hover:text-red-300" title="Remove all links" aria-label="Remove all links" on:click={() => removeField('links')}>✕</button>
              </div>
              {#each draft.links as link, index (index)}
                <div class="mb-2 space-y-1 rounded-lg border border-white/5 bg-black/20 p-2">
                  <div class="flex gap-1.5">
                    <select
                      bind:value={link.kind}
                      on:change={persist}
                      class="shrink-0 rounded border border-[#2a2a3a] bg-[#1e1e2e] px-1 py-1 text-[11px] text-gray-300 outline-none focus:border-purple-500"
                    >
                      {#each LINK_KINDS as kind}
                        <option value={kind}>{linkKindLabel(kind)}</option>
                      {/each}
                    </select>
                    <input
                      bind:value={link.label}
                      on:blur={persist}
                      placeholder="Label (optional)"
                      class="min-w-0 flex-1 rounded border border-[#2a2a3a] bg-[#1e1e2e] px-2 py-1 text-[11px] text-gray-200 outline-none placeholder:text-gray-600 focus:border-purple-500"
                    />
                    <button type="button" class="shrink-0 rounded px-1.5 text-gray-500 hover:text-red-300" title="Remove link" aria-label="Remove link" on:click={() => removeLink(index)}>✕</button>
                  </div>
                  <input
                    bind:value={link.url}
                    on:blur={persist}
                    placeholder="https://…"
                    class="w-full rounded border border-[#2a2a3a] bg-[#1e1e2e] px-2 py-1 text-[11px] text-gray-200 outline-none placeholder:text-gray-600 focus:border-purple-500"
                  />
                </div>
              {/each}
              <button type="button" class="text-[11px] text-purple-300 hover:text-purple-200" on:click={addLink}>＋ Add link</button>
            </div>
          {/if}

          {#if showDetails}
            <div class="mb-3">
              <div class="mb-1 flex items-center justify-between"><span class="text-[10px] uppercase tracking-wide text-gray-500">Details</span><button type="button" class="text-gray-600 hover:text-red-300" aria-label="Remove all details" on:click={() => removeField('details')}>✕</button></div>
              {#each draft.details as detail, index (index)}
                <div class="mb-2 flex items-start gap-1.5">
                  <input aria-label="Detail name" placeholder="Name" maxlength="128" bind:value={detail.label} on:blur={persist} class="w-1/3 min-w-0 rounded border border-[#2a2a3a] bg-[#1e1e2e] px-2 py-1.5 text-xs text-gray-300 outline-none focus:border-purple-500" />
                  <textarea aria-label={detail.label || 'Detail value'} placeholder="Value" rows="1" maxlength="262144" bind:value={detail.value} on:blur={persist} class="min-w-0 flex-1 resize-y rounded border border-[#2a2a3a] bg-[#1e1e2e] px-2 py-1.5 text-xs text-gray-200 outline-none focus:border-purple-500"></textarea>
                  <button type="button" class="px-1 py-1 text-gray-500 hover:text-red-300" aria-label="Remove detail" on:click={() => removeDetail(index)}>✕</button>
                </div>
              {/each}
              {#if draft.details.length < 32}<button type="button" class="text-[11px] text-purple-300 hover:text-purple-200" on:click={addDetail}>＋ Add detail</button>{/if}
            </div>
          {/if}

          {#if attachments.length || (musicCover && !missingCover)}
            <div class="mb-3">
              <div class="mb-1 text-[10px] uppercase tracking-wide text-gray-500">Images / videos</div>
              <div class="grid grid-cols-3 gap-1.5">
                {#if !attachments.length && musicCover && !missingCover}{#key `${key}:${musicCover.artwork_version}`}<img class="h-16 w-full rounded border border-white/5 object-cover" alt="Local music cover" src={filesApi.moduleArtworkUrl(subject.sourceId, subject.path, musicCover.owner, musicCover.artwork_version)} on:error={() => missingCover = true}/>{/key}{/if}
                {#each attachments as attachment (attachment.id)}
                  <div class="group relative overflow-hidden rounded border border-white/5 bg-black/30">
                    {#if attachment.media_type.startsWith('video/')}
                      <!-- svelte-ignore a11y-media-has-caption -->
                      <video src={filesApi.attachmentUrl(attachment.id)} class="h-16 w-full object-cover" muted></video>
                    {:else}
                      <img src={filesApi.attachmentUrl(attachment.id)} alt={attachment.caption || attachment.file_name} class="h-16 w-full object-cover" />
                    {/if}
                    <button type="button" class="absolute right-0.5 top-0.5 hidden h-5 w-5 place-items-center rounded bg-black/70 text-[10px] text-gray-200 group-hover:grid hover:text-red-300" title="Remove attachment" aria-label="Remove attachment" on:click={() => removeAttachment(attachment.id)}>✕</button>
                  </div>
                {/each}
              </div>
            </div>
          {/if}

          {#if showExtra}
            <div class="mb-3">
              <div class="mb-1 flex items-center justify-between">
                <span class="text-[10px] uppercase tracking-wide text-gray-500">Extra info</span>
                <button type="button" class="text-gray-600 hover:text-red-300" title="Remove extra info" aria-label="Remove extra info" on:click={() => removeField('extra')}>✕</button>
              </div>
              <textarea
                rows="2"
                bind:value={draft.extra_info}
                on:blur={persist}
                placeholder="Anything else worth noting…"
                class="w-full resize-y rounded-lg border border-[#2a2a3a] bg-[#1e1e2e] px-2 py-1.5 text-xs text-gray-200 outline-none placeholder:text-gray-600 focus:border-purple-500"
              ></textarea>
            </div>
          {/if}

          <div class="flex items-center gap-2">
            <button
              type="button"
              class="inline-flex items-center gap-1.5 rounded-lg border border-dashed border-[#3a3a4a] px-3 py-1.5 text-xs text-purple-300 transition-colors hover:border-purple-500/60 hover:text-purple-200"
              on:click|stopPropagation={() => (addMenuOpen = !addMenuOpen)}
            >＋ Add info</button>
            {#if uploading}
              <span class="text-[11px] text-gray-500">Uploading…</span>
            {/if}
          </div>

          <!-- Reveals in normal flow (not an absolute dropdown): a floating menu
               is clipped by the body's overflow-y-auto and only shows its first
               couple of items. -->
          {#if addMenuOpen}
            <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
            <div class="mt-1.5 w-48 overflow-hidden rounded-lg border border-[#2a2a3a] bg-[#1e1e2e] py-1" on:click|stopPropagation>
              {#each addOptions as option (option.key)}
                <button type="button" class="flex w-full items-center px-3 py-1.5 text-left text-sm text-gray-300 transition-colors hover:bg-[#2a2a3a] hover:text-white" on:click={() => addField(option.key)}>{option.label}</button>
              {/each}
            </div>
          {/if}

          {#if actionError}
            <p class="mt-2 text-[11px] text-red-300">{actionError}</p>
          {/if}
        </div>
      {/if}
      {#if metadataLoading || audioLoading}<p role="status" class="px-4 pb-3 text-xs text-gray-500">Reading metadata…</p>
      {:else if metadataNotice}<div class="px-4 pb-3 text-xs text-gray-400"><p role="status">{metadataNotice}</p><button class="mt-2 text-purple-300" on:click={() => { void loadMetadata(subject); void loadAudio(subject); }}>Retry metadata</button></div>{/if}

    </div>

    <input bind:this={fileInput} type="file" accept="image/*,video/*" class="hidden" on:change={onUploadChange} />

    <div class="shrink-0 border-t border-white/5 px-4 py-3">
      <div class="flex gap-2">
        <button
          type="button"
          class="flex-1 rounded-lg border border-[#2a2a3a] bg-[#1e1e2e] px-3 py-1.5 text-sm text-gray-300 transition-colors hover:border-purple-500/50 hover:text-white"
          on:click={openSubject}
        >Open</button>
        <button
          type="button"
          class="flex-1 rounded-lg border border-[#2a2a3a] bg-[#1e1e2e] px-3 py-1.5 text-sm text-gray-300 transition-colors hover:border-purple-500/50 hover:text-white"
          on:click={reveal}
        >Show in folder</button>
      </div>
    </div>
  </div>
</div>

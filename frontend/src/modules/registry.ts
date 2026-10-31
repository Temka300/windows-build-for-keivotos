import { activeModule, suiteView } from '../lib/suiteStores';
import { filesLocationRequest } from '../lib/filesStores';
import { writable } from 'svelte/store';

export interface LibraryCategory { id: string; name: string; created_at: string; }
export interface CategoryItem { source_id: string; path: string; }
export interface CategoryMembership { version: number; category_ids: string[]; }
export interface CategoryClient {
  list: (offset?: number, signal?: AbortSignal) => Promise<{items: LibraryCategory[]; total: number}>;
  create: (name: string, requestId: string) => Promise<LibraryCategory>;
  membership: (item: CategoryItem, signal?: AbortSignal) => Promise<CategoryMembership>;
  saveMembership: (item: CategoryItem, selected: string[], version: number, requestId: string) => Promise<CategoryMembership>;
}
export const libraryCategoriesRefresh = writable(0);

export async function allLibraryCategories(client: CategoryClient, signal?: AbortSignal): Promise<LibraryCategory[]> {
  const items: LibraryCategory[] = [];
  while (true) {
    const page = await client.list(items.length, signal);
    items.push(...page.items);
    if (items.length >= page.total) return items;
    if (!page.items.length) throw new Error('Could not load the complete category list. Try again.');
  }
}

export interface DrawerAction {
  id: string;
  label: string;
  iconSrc: string;
  run: () => void;
}

export interface ModuleUiDescriptor {
  slug: string;
  iconSrc: string | null;
  drawerActions: DrawerAction[];
  activate?: () => void;
  categories?: CategoryClient;
}

// Eager discovery preserves synchronous activation and existing store initialization.
// Each installed owner contributes its own actions; the registry names no module.
const contributions = import.meta.glob<ModuleUiDescriptor>('./*/ui.ts', {
  eager: true,
  import: 'default',
});
const UI_DESCRIPTORS: Record<string, ModuleUiDescriptor> = Object.fromEntries(
  Object.values(contributions).map((descriptor) => [descriptor.slug, descriptor]),
);

const FALLBACK_UI: ModuleUiDescriptor = {
  slug: 'unknown',
  iconSrc: null,
  drawerActions: [],
};

export function moduleUi(slug: string): ModuleUiDescriptor {
  return UI_DESCRIPTORS[slug] ?? { ...FALLBACK_UI, slug };
}

export function activateModule(slug: string): void {
  suiteView.set('module');
  moduleUi(slug).activate?.();
  activeModule.set(slug);
}

export function showInFiles(sourceId: string, relativePath: string): void {
  filesLocationRequest.set({ sourceId, relativePath });
  activateModule('files');
}

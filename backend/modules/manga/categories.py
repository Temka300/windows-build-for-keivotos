"""Manga-owned series categories, independent of chapters and reading progress."""
from fastapi import APIRouter, HTTPException, Query, Response
import config
from files_base import sources, serving
from modules.manga import library
from database import get_user_db
from services.library_categories import CategoryStore, CategoryCreate, Category, CategoryList
from services.library_categories import CategoryMembership, CategoryMembershipSave

store = CategoryStore('manga')
router = APIRouter()


@router.get('/api/manga/categories', response_model=CategoryList)
def list_categories(response: Response, offset: int = Query(0, ge=0), limit: int = Query(120, ge=1, le=240)):
    response.headers['Cache-Control'] = 'no-store'
    with get_user_db() as connection:
        return store.listing(connection, offset, limit)


@router.post('/api/manga/categories', response_model=Category)
def create_category(payload: CategoryCreate):
    with get_user_db() as connection:
        return store.create(connection, payload.name, str(payload.request_id))


def require_item(connection, source_id, path):
    source = sources.get_source(connection, source_id)
    if not source or not source.visible or source.role != 'manga':
        raise HTTPException(404, 'Manga is unavailable.')
    try:
        target = serving.resolve_within_source(source.path, path, [config.SUITE_HOME])
        owner = sources.get_source_by_path(connection, target) or sources.nearest_ancestor_source(target, sources.list_sources(connection))
        if not owner or owner.source_id != source_id:
            raise HTTPException(404, 'Manga is unavailable.')
    except (serving.ServeDenied, OSError, ValueError) as exc:
        raise HTTPException(404, 'Manga is unavailable.') from exc
    if not any(item['path'] == path for item in library.catalog(config.FILES_DB_PATH, [source])):
        raise HTTPException(404, 'Manga is unavailable.')


@router.get('/api/manga/category-membership', response_model=CategoryMembership)
def membership(response: Response, source_id: str = Query(..., min_length=1, max_length=256), path: str = Query('', max_length=4096)):
    response.headers['Cache-Control'] = 'no-store'
    with get_user_db() as connection:
        require_item(connection, source_id, path)
        return store.membership(connection, source_id, path)


@router.post('/api/manga/category-membership', response_model=CategoryMembership)
def save_membership(payload: CategoryMembershipSave):
    with get_user_db() as connection:
        return store.set_membership(connection, payload.source_id, payload.path, [str(value) for value in payload.category_ids],
                                    payload.version, str(payload.request_id), require_item)

"""Manga endpoints: indexed ownership plus shared containment policy on every read."""
import logging
from hashlib import sha256
from threading import BoundedSemaphore
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Response
import config
from pydantic import BaseModel, Field
import lifecycle
from suite_modules import require_enabled
from modules.manga import progress
from database import get_user_db
from files_base import sources, serving
from modules.manga import library as engine

router = APIRouter()
logger = logging.getLogger('keivotos.manga')
_render_slots = BoundedSemaphore(2)


def owners():
    with get_user_db() as connection:
        return [s for s in sources.list_sources(connection) if s.role == 'manga' and s.visible]


def find_chapter(source_id, path, kind):
    source = next((s for s in owners() if s.source_id == source_id), None)
    if source and engine.chapter_exists(config.FILES_DB_PATH, source_id, path, kind):
        return source
    raise HTTPException(404, 'Chapter is unavailable')


def failed(source_id, path, exc):
    logger.warning('Manga read failed: source=%r chapter=%r error=%s: %s', source_id, path, type(exc).__name__, exc)
    if isinstance(exc, serving.ServeDenied):
        raise HTTPException(exc.status_code, 'Chapter is unavailable. See Logs.') from exc
    raise HTTPException(422, 'This chapter cannot be opened. See Logs.') from exc


@router.get('/api/manga/library')
def library(q: str = Query('', max_length=256), offset: int = Query(0, ge=0),
            limit: int = Query(60, ge=1, le=120), response: Response = None):
    if response is not None:
        response.headers['Cache-Control'] = 'no-store'
    items = [s for s in engine.catalog(config.FILES_DB_PATH, owners()) if q.strip().casefold() in s['name'].casefold()]
    sources_by_id = {source.source_id: source for source in owners()}
    result = []
    for item in items[offset:offset+limit]:
        cover = item['chapters'][0]
        revision = None
        try:
            metadata = engine.chapter_metadata(sources_by_id[item['source_id']], cover['path'],
                                               cover['kind'], config.FILES_DB_PATH, [config.SUITE_HOME], first_only=True)
            if metadata['revisions']:
                revision = metadata['revisions'][0]
        except Exception as exc:
            logger.warning('Manga cover identity unavailable: %s', exc)
        result.append({'source_id': item['source_id'], 'path': item['path'], 'name': item['name'],
                       'cover': cover, 'cover_revision': revision})
    return {'items': result, 'total': len(items)}


@router.get('/api/manga/chapters')
def chapters(source_id: str = Query(..., max_length=256), path: str = Query('', max_length=4096)):
    for series in engine.catalog(config.FILES_DB_PATH, owners()):
        if series['source_id'] == source_id and series['path'] == path:
            return {'items': series['chapters']}
    raise HTTPException(404, 'Manga is unavailable')


@router.get('/api/manga/pages')
def pages(source_id: str = Query(..., max_length=256), path: str = Query('', max_length=4096),
          kind: Literal['folder','archive'] = 'folder', metadata: bool = False,
          response: Response = None):
    if response is not None:
        response.headers['Cache-Control'] = 'no-store'
    source = find_chapter(source_id, path, kind)
    try:
        if metadata:
            result = engine.chapter_metadata(source, path, kind, config.FILES_DB_PATH, [config.SUITE_HOME])
            count = result['count']
        elif kind == 'archive':
            target = serving.resolve_within_source(source.path, path, [config.SUITE_HOME])
            with engine.open_archive(target, source_id=source_id) as (_, entries):
                count = len(entries)
        else:
            serving.resolve_within_source(source.path, path, [config.SUITE_HOME])
            count = len(engine.folder_pages(config.FILES_DB_PATH, source_id, path))
        if not count:
            raise ValueError('No readable image pages')
        return result if metadata else {'count': count}
    except Exception as exc:
        failed(source_id, path, exc)


@router.get('/api/manga/page')
def page(source_id: str = Query(..., max_length=256), path: str = Query('', max_length=4096),
         kind: Literal['folder','archive'] = 'folder', page: int = Query(0, ge=0), cover: bool = False,
         representation: Literal['webp', 'original'] = 'webp',
         revision: Annotated[str | None, Query(max_length=64, pattern='^[0-9a-f]{64}$')] = None,
         if_none_match: Annotated[str | None, Header()] = None):
    source = find_chapter(source_id, path, kind)
    # Bound simultaneous image decodes independently of retained page bytes.
    with _render_slots:
        try:
            result, current = engine.page_response(source, path, kind, page, config.FILES_DB_PATH,
                                                  [config.SUITE_HOME], cover, representation, revision)
            etag = '"' + sha256(result.content).hexdigest() + '"'
            headers = {'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, no-cache',
                       'ETag': etag, 'X-Manga-Revision': current,
                       'X-Manga-Representation': 'webp' if cover else representation}
            matches = (if_none_match or '').split(',')
            if any(value.strip().removeprefix('W/') in ('*', etag) for value in matches):
                return Response(status_code=304, headers=headers)
            return Response(result.content, media_type=result.media_type, headers=headers)
        except engine.StaleRevision as exc:
            raise HTTPException(409, str(exc), headers={'Cache-Control': 'no-store'}) from exc
        except engine.OriginalUnavailable as exc:
            raise HTTPException(415, str(exc) + '. Open a converted page to read it.',
                                headers={'Cache-Control': 'no-store'}) from exc
        except Exception as exc:
            failed(source_id, path, exc)


# Personal-state mutations share lifecycle coordination with module disable/restore.


class ProgressChapter(BaseModel):
    source_id: str = Field(min_length=1, max_length=256)
    series_path: str = Field(default='', max_length=4096)
    path: str = Field(default='', max_length=4096)
    kind: Literal['folder', 'archive'] = 'folder'
    revision: str = Field(pattern=r'^[0-9a-f]{64}$')


class ProgressSession(ProgressChapter):
    expected_generation: int = Field(ge=0, le=9007199254740990)


class ProgressUpdate(ProgressChapter):
    generation: int = Field(ge=1, le=9007199254740991)
    sequence: int = Field(ge=1, le=9007199254740991)
    page: int = Field(ge=0)
    mode: Literal['paged', 'vertical']
    anchor: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)


def progress_owner(payload):
    try:
        require_enabled('manga')
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    source = find_chapter(payload.source_id, payload.path, payload.kind)
    series = next((item for item in engine.catalog(config.FILES_DB_PATH, [source])
                   if item['path'] == payload.series_path), None)
    if not series or not any(c['path'] == payload.path and c['kind'] == payload.kind for c in series['chapters']):
        raise HTTPException(404, 'Chapter is unavailable in this series')
    try:
        metadata = engine.chapter_metadata(source, payload.path, payload.kind,
                                           config.FILES_DB_PATH, [config.SUITE_HOME])
    except Exception as error:
        failed(payload.source_id, payload.path, error)
    if metadata['revision'] != payload.revision:
        raise HTTPException(409, 'Chapter changed; reopen it before saving progress.')
    if not metadata['count']:
        raise HTTPException(404, 'Chapter has no readable pages')
    return metadata


@router.post('/api/manga/progress/read')
def read_progress(payload: ProgressChapter):
    with lifecycle.module_change_lock:
        progress_owner(payload)
        with get_user_db() as connection:
            return progress.read(connection, payload.source_id, payload.path, payload.kind, payload.revision)


@router.post('/api/manga/progress/session')
def begin_progress(payload: ProgressSession):
    with lifecycle.module_change_lock:
        progress_owner(payload)
        with get_user_db() as connection:
            return progress.begin(connection, payload.source_id, payload.path, payload.kind, payload.expected_generation)


@router.post('/api/manga/progress')
def save_progress(payload: ProgressUpdate):
    with lifecycle.module_change_lock:
        metadata = progress_owner(payload)
        if payload.page >= metadata['count']:
            raise HTTPException(409, 'Observed page is outside this chapter.')
        with get_user_db() as connection:
            return progress.save(connection, payload, metadata['count'])


@router.get('/api/manga/progress/series')
def series_progress(source_id: str = Query(..., max_length=256),
                    series_path: str = Query('', max_length=4096)):
    with lifecycle.module_change_lock:
        try:
            require_enabled('manga')
        except RuntimeError as error:
            raise HTTPException(409, str(error)) from error
        if not any(s.source_id == source_id for s in owners()):
            raise HTTPException(404, 'Manga folder is unavailable')
        with get_user_db() as connection:
            rows = connection.execute("""SELECT * FROM manga_reading_progress
                WHERE source_id=? AND series_path=? ORDER BY updated_at DESC, generation DESC, sequence DESC
                LIMIT 120""", (source_id, series_path)).fetchall()
        # Selection must still pass fresh chapter guards; these are personal records only.
        return {'items': [dict(row) for row in rows]}

"""OST role admission and guarded local discovery; media uses Files routes."""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.responses import FileResponse, Response
from typing import Annotated

import config
from database import get_user_db
from files_base import index, serving, sources
from modules.ost import library as discovery
from modules.ost import albums as music_albums
from services import audio_metadata

router = APIRouter()


def registered_sources():
    with get_user_db() as connection:
        sources.ensure_sources_schema(connection)
        return sources.list_sources(connection)


@router.get('/api/ost/folders')
def folders():
    registered = registered_sources()
    result = []
    with index.open_index(config.FILES_DB_PATH) as connection:
        for source in registered:
            if source.role != 'ost' or not source.visible:
                continue
            forbidden = discovery.forbidden_roots(source, registered, config.SUITE_HOME)
            result.append({'source_id': source.source_id, 'name': source.display_name,
                           'path': source.path,
                           'available': discovery.available(source, '', True, forbidden),
                           'cover_version': discovery.cover_version(connection, source, '', True, forbidden)})
    return result


def require_source(registered, source_id):
    source = next((source for source in registered
                   if source.source_id == source_id and source.role == 'ost' and source.visible), None)
    if source is None:
        raise HTTPException(404, 'Music folder is unavailable')
    return source


@router.get('/api/ost/library')
def library(source_id: str = Query(..., max_length=256),
            parent: str = Query('', max_length=4096),
            offset: int = Query(0, ge=0), limit: int = Query(120, ge=1, le=240)):
    registered = registered_sources()
    source = require_source(registered, source_id)
    try:
        return discovery.directory(config.FILES_DB_PATH, source, registered,
                                   config.SUITE_HOME, parent, offset, limit)
    except serving.ServeDenied as error:
        raise HTTPException(error.status_code, error.detail) from error
    except (OSError, ValueError) as error:
        raise HTTPException(404, 'Music folder is unavailable') from error


@router.get('/api/ost/cover')
def cover(source_id: str = Query(..., max_length=256), path: str = Query('', max_length=4096),
          size: int = Query(300, ge=1, le=1200), v: str | None = Query(None, max_length=128)):
    registered = registered_sources()
    source = require_source(registered, source_id)
    try:
        thumbnail = discovery.cover_file(config.FILES_DB_PATH, source, registered,
                                         config.SUITE_HOME, path, size)
        return FileResponse(thumbnail, media_type='image/webp',
                            headers={'Cache-Control': 'public, max-age=31536000, immutable'})
    except serving.ServeDenied as error:
        raise HTTPException(error.status_code, error.detail) from error
    except (OSError, ValueError) as error:
        raise HTTPException(404, 'Music cover is unavailable') from error


@router.get('/api/ost/tracks')
def tracks(offset: int = Query(0, ge=0), limit: int = Query(120, ge=1, le=240),
           q: Annotated[str, Query(max_length=512)] = '', response: Response = None):
    if response is not None:
        response.headers['Cache-Control'] = 'no-store'
    return discovery.tracks(config.FILES_DB_PATH, registered_sources(),
                            config.SUITE_HOME, offset, limit, q)


@router.get('/api/ost/albums')
def albums(offset: int = Query(0, ge=0), limit: int = Query(120, ge=1, le=240),
           q: Annotated[str, Query(max_length=512)] = '', response: Response = None):
    if response is not None:
        response.headers['Cache-Control'] = 'no-store'
    return music_albums.listing(config.FILES_DB_PATH, registered_sources(), config.SUITE_HOME, offset, limit, q)


@router.get('/api/ost/album-tracks')
def album_tracks(source_id: str = Query(..., max_length=256), anchor: str = Query(..., max_length=4096),
                 offset: int = Query(0, ge=0), limit: int = Query(120, ge=1, le=240), prepare: bool = False,
                 response: Response = None):
    if response is not None:
        response.headers['Cache-Control'] = 'no-store'
    registered = registered_sources()
    source = require_source(registered, source_id)
    try:
        discovery.directory_path(source, '', discovery.forbidden_roots(source, registered, config.SUITE_HOME))
        serving.resolve_served_file(source.path, anchor, discovery.forbidden_roots(source, registered, config.SUITE_HOME))
        return music_albums.tracks(config.FILES_DB_PATH, registered, config.SUITE_HOME,
                                  source_id, anchor, offset, limit, prepare)
    except serving.ServeDenied as error:
        raise HTTPException(error.status_code, error.detail) from error
    except (OSError, ValueError) as error:
        raise HTTPException(404, 'Album is unavailable') from error


@router.get('/api/ost/metadata')
def metadata(response: Response, source_id: str = Query(..., max_length=256),
             path: str = Query(..., max_length=4096)) -> dict:
    response.headers['Cache-Control'] = 'no-store'
    registered = registered_sources()
    source = require_source(registered, source_id)
    try:
        forbidden = discovery.forbidden_roots(source, registered, config.SUITE_HOME)
        result = audio_metadata.read(config.FILES_DB_PATH, source, path, forbidden)
        music_albums.record(config.FILES_DB_PATH, source, path, forbidden, result)
        return result
    except serving.ServeDenied as error:
        raise HTTPException(error.status_code, error.detail) from error
    except (OSError, ValueError) as error:
        raise HTTPException(404, 'Music file is unavailable') from error


class MetadataRefresh(BaseModel):
    source_id: str = Field(min_length=1, max_length=256)
    parent: str = Field(default='', max_length=4096)


@router.post('/api/ost/metadata-refresh')
def refresh_metadata(payload: MetadataRefresh):
    from modules.ost import metadata_jobs
    try:
        return metadata_jobs.request(payload.source_id, payload.parent)
    except serving.ServeDenied as error:
        raise HTTPException(error.status_code, error.detail) from error
    except (OSError, ValueError) as error:
        raise HTTPException(404, 'Music folder is unavailable') from error


@router.get('/api/ost/metadata-jobs')
def metadata_jobs(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    from modules.ost.metadata_jobs import listing
    return {'items': listing()}

"""Optional Twitter archive routes; local media uses guarded shared Files URLs."""
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.responses import FileResponse, Response, StreamingResponse

import config
from database import get_user_db
from files_base import serving, sources
from modules.twitter import archive

router = APIRouter()


def registered_sources():
    with get_user_db() as connection:
        sources.ensure_sources_schema(connection)
        return sources.list_sources(connection)


def require_source(registered, source_id):
    source = next((source for source in registered
                   if source.source_id == source_id and source.role == 'twitter' and source.visible), None)
    if source is None:
        raise HTTPException(404, 'Twitter folder is unavailable')
    return source


@router.get('/api/twitter/folders')
def folders():
    registered = registered_sources()
    return [{'source_id': source.source_id, 'name': source.display_name, 'path': source.path,
             'available': archive.available(source, archive.forbidden_roots(source, registered, config.SUITE_HOME))}
            for source in registered if source.role == 'twitter' and source.visible]


@router.get('/api/twitter/library')
def library(offset: int = Query(0, ge=0), limit: int = Query(120, ge=1, le=240),
            q: str = Query('', max_length=256), author: str = Query('', max_length=80)):
    return archive.listing(config.FILES_DB_PATH, registered_sources(), config.SUITE_HOME, offset, limit, q, author)


@router.get('/api/twitter/capture')
def capture(source_id: str = Query(..., max_length=256), path: str = Query(..., max_length=4096)):
    registered = registered_sources()
    source = require_source(registered, source_id)
    try:
        return archive.capture(source, registered, config.SUITE_HOME, path)
    except serving.ServeDenied as error:
        raise HTTPException(error.status_code, error.detail) from error
    except archive.InvalidCapture as error:
        raise HTTPException(422, str(error)) from error
    except OSError as error:
        raise HTTPException(404, 'Twitter capture is unavailable') from error


@router.get('/api/twitter/media')
def media(request: Request, source_id: str = Query(..., max_length=256),
          path: str = Query(..., max_length=4096), asset: Literal['avatar', 'video'] = Query(...)):
    item = capture(source_id, path)
    name = 'avatar.jpg' if asset == 'avatar' else 'video.mp4'
    if name in item['unavailable_assets']:
        raise HTTPException(404, 'Twitter media is unavailable')
    # Reconcile the owner again after reading metadata; streaming never delegates
    # to the more permissive Files HTTP route or accepts an arbitrary asset path.
    registered = registered_sources()
    source = require_source(registered, source_id)
    try:
        resolved = serving.resolve_served_file(source.path, item[f'{asset}_path'],
                                               archive.forbidden_roots(source, registered, config.SUITE_HOME))
        file_size = resolved.stat().st_size
        if file_size != item['files'][name]['size']:
            raise HTTPException(404, 'Twitter media changed after reading its metadata')
    except serving.ServeDenied as error:
        raise HTTPException(error.status_code, error.detail) from error
    except OSError as error:
        raise HTTPException(404, 'Twitter media is unavailable') from error
    media_type, inline = serving.inline_media_type(resolved)
    headers = {'Accept-Ranges': 'bytes', 'X-Content-Type-Options': 'nosniff',
               'Content-Disposition': serving.content_disposition(resolved.name, inline=inline),
               'Cache-Control': 'private, no-cache'}
    range_header = request.headers.get('range')
    byte_range = serving.parse_range_header(range_header, file_size)
    if range_header and byte_range is None:
        return Response(status_code=416, headers={**headers, 'Content-Range': f'bytes */{file_size}'})
    if byte_range is not None:
        start, end = byte_range
        return StreamingResponse(serving.file_range_iter(resolved, start, end), status_code=206,
                                 media_type=media_type, headers={**headers,
                                     'Content-Length': str(end - start + 1),
                                     'Content-Range': f'bytes {start}-{end}/{file_size}'})
    return FileResponse(resolved, media_type=media_type, headers=headers)

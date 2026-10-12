"""Optional Twitter archive routes; local media uses guarded shared Files URLs."""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

import config
from database import get_user_db
from files_base import serving, sources
from modules.twitter import archive, jobs

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


def _job_call(action, *args):
    try:
        return action(*args)
    except serving.ServeDenied as error:
        raise HTTPException(error.status_code, error.detail) from error
    except (ValueError, OSError) as error:
        raise HTTPException(400, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


class TwitterPreferences(BaseModel):
    source_id: str = Field(max_length=256)


class TwitterDownload(BaseModel):
    url: str = Field(max_length=2048)
    source_id: str = Field(default='', max_length=256)
    request_id: UUID


class TwitterRetry(BaseModel):
    request_id: UUID


@router.get('/api/twitter/settings')
def settings():
    return jobs.preferences()


@router.post('/api/twitter/settings')
def save_settings(payload: TwitterPreferences):
    return _job_call(jobs.save_preferences, payload.source_id)


@router.post('/api/twitter/downloads')
def download(payload: TwitterDownload):
    return jobs.public(_job_call(jobs.create, payload.url, payload.source_id, payload.request_id))


@router.get('/api/twitter/downloads/{job_id}')
def download_status(job_id: UUID):
    item = jobs.get(str(job_id))
    if not item:
        raise HTTPException(404, 'Download not found')
    return jobs.public(item)


@router.post('/api/twitter/downloads/{job_id}/cancel')
def cancel_download(job_id: UUID):
    download_status(job_id)
    return jobs.public(_job_call(jobs.cancel, str(job_id)))


@router.post('/api/twitter/downloads/{job_id}/retry')
def retry_download(job_id: UUID, payload: TwitterRetry):
    download_status(job_id)
    return jobs.public(_job_call(jobs.retry, str(job_id), payload.request_id))

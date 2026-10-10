from fastapi import APIRouter, HTTPException, Query, Path as ApiPath
from pydantic import BaseModel, Field
from typing import Literal, Annotated
from uuid import UUID
import lifecycle
from suite_modules import require_enabled
from modules.youtube import playlists
from database import get_user_db
from files_base import sources, serving
from modules.youtube import jobs
from modules.youtube.engine import dependencies

router=APIRouter()


class Download(BaseModel):
    url:str=Field(max_length=2048)
    source_id:str|None=Field(default=None,max_length=256)
    video:Literal['best','1080','720','480','audio']='best'
    audio:Literal['best','192','128']='best'


def public(item):
    return {k:v for k,v in item.items() if k not in ('root',)}


@router.get('/api/youtube/status')
def status():
    info=dependencies()
    with get_user_db() as db:
        folders=[{'id':s.source_id,'name':s.display_name} for s in sources.list_sources(db) if s.visible and s.role in ('files','video','youtube')]
    return {'ready':info['ready'],'missing':info['missing'],'folders':folders}


@router.get('/api/youtube/library')
def library(q:str=Query('',max_length=256),offset:int=Query(0,ge=0),limit:int=Query(60,ge=1,le=120)):
    with get_user_db() as db:
        where="status != 'cancelled' AND superseded_by = '' AND instr(lower(title),lower(?))>0"
        total=db.execute('SELECT count(*) AS total FROM youtube_downloads WHERE '+where,(q,)).fetchone()['total']
        rows=db.execute('SELECT * FROM youtube_downloads WHERE '+where+' ORDER BY created_at DESC,id LIMIT ? OFFSET ?',(q,limit,offset)).fetchall()
    return {'items':[public(dict(row)) for row in rows],'total':total}


@router.post('/api/youtube/downloads')
def download(payload:Download):
    try:return public(jobs.create(payload.url,payload.source_id,payload.video,payload.audio))
    except serving.ServeDenied as exc:raise HTTPException(exc.status_code,exc.detail) from exc
    except (ValueError,OSError) as exc:raise HTTPException(400,str(exc)) from exc


@router.post('/api/youtube/downloads/{job_id}/cancel')
def cancel(job_id:str):
    try:return public(jobs.cancel(job_id))
    except ValueError as exc:raise HTTPException(404,str(exc)) from exc


@router.post('/api/youtube/downloads/{job_id}/retry')
def retry(job_id:str):
    item=jobs.get(job_id)
    if not item:raise HTTPException(404,'Download not found.')
    if not item['superseded_by'] and item['status'] not in ('failed','cancelled','interrupted'):raise HTTPException(409,'This download cannot be retried.')
    try:return public(jobs.retry(job_id))
    except serving.ServeDenied as exc:raise HTTPException(exc.status_code,exc.detail) from exc
    except (ValueError,OSError) as exc:raise HTTPException(400,str(exc)) from exc


class Preferences(BaseModel):
    mode:Literal['default','custom']
    path:str|None=Field(default=None,max_length=4096)


@router.get('/api/youtube/settings')
def settings():
    return jobs.preferences()


@router.post('/api/youtube/settings')
def save_settings(payload:Preferences):
    try:return jobs.save_preferences(payload.mode,payload.path)
    except (ValueError,OSError) as exc:raise HTTPException(400,str(exc)) from exc


class YouTubePlaybackFailure(BaseModel):
    id:str=Field(max_length=64)
    reason:Literal['unsupported_format','network','decode','unsupported_codec','playback','fullscreen']


@router.post('/api/youtube/playback-error')
def playback_error(payload:YouTubePlaybackFailure):
    item=jobs.get(payload.id)
    if not item or item['status']!='complete':
        raise HTTPException(404,'Completed download not found.')
    try:jobs.destination(item['source_id'],item['root'])
    except (ValueError,OSError,serving.ServeDenied) as exc:
        raise HTTPException(404,'Download folder is unavailable.') from exc
    jobs.logger.warning('Playback failed: job=%s source=%r file=%r reason=%s',
                        item['id'],item['source_id'],item['path'],payload.reason)
    return {'status':'logged'}


class PlaylistCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    request_id: UUID


class PlaylistMembership(BaseModel):
    playlist_ids: list[UUID] = Field(max_length=256)
    version: int = Field(ge=0, le=9007199254740990)


class PlaylistRename(BaseModel):
    title: str = Field(min_length=1,max_length=120)
    revision: int = Field(ge=0,le=9007199254740990)
    request_id: UUID


class PlaylistArchive(BaseModel):
    revision: int = Field(ge=0,le=9007199254740990)
    item_count: int = Field(ge=0)
    request_id: UUID


def _playlist_enabled():
    try:
        require_enabled('youtube')
    except lifecycle.ModuleStartError as error:
        raise HTTPException(503,str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409,str(error)) from error


@router.get('/api/youtube/playlists')
def list_playlists(offset:int=Query(0,ge=0),limit:int=Query(60,ge=1,le=120)):
    with lifecycle.module_change_lock:
        _playlist_enabled()
        with get_user_db() as connection:
            return playlists.listing(connection,offset,limit)


@router.post('/api/youtube/playlists')
def create_playlist(payload:PlaylistCreate):
    with lifecycle.module_change_lock:
        _playlist_enabled()
        with get_user_db() as connection:
            return playlists.create(connection,payload.title,str(payload.request_id))


@router.post('/api/youtube/playlists/{playlist_id}/rename')
def rename_playlist(playlist_id:UUID,payload:PlaylistRename):
    with lifecycle.module_change_lock:
        _playlist_enabled()
        with get_user_db() as connection:
            return playlists.change(connection,str(playlist_id),payload.revision,str(payload.request_id),title=payload.title)


@router.post('/api/youtube/playlists/{playlist_id}/archive')
def archive_playlist(playlist_id:UUID,payload:PlaylistArchive):
    with lifecycle.module_change_lock:
        _playlist_enabled()
        with get_user_db() as connection:
            return playlists.change(connection,str(playlist_id),payload.revision,str(payload.request_id),count=payload.item_count)


@router.get('/api/youtube/playlists/{playlist_id}/items')
def playlist_items(playlist_id:UUID,offset:int=Query(0,ge=0),limit:int=Query(60,ge=1,le=120)):
    with lifecycle.module_change_lock:
        _playlist_enabled()
        with get_user_db() as connection:
            return playlists.items(connection,str(playlist_id),offset,limit)


@router.get('/api/youtube/videos/{job_id}/playlists')
def video_playlists(job_id:Annotated[str,ApiPath(min_length=1,max_length=64)]):
    with lifecycle.module_change_lock:
        _playlist_enabled()
        with get_user_db() as connection:
            playlists.require_video(connection,job_id)
            return playlists.membership(connection,job_id)


@router.post('/api/youtube/videos/{job_id}/playlists')
def save_video_playlists(job_id:Annotated[str,ApiPath(min_length=1,max_length=64)],payload:PlaylistMembership):
    with lifecycle.module_change_lock:
        _playlist_enabled()
        with get_user_db() as connection:
            return playlists.set_membership(connection,job_id,[str(value) for value in payload.playlist_ids],payload.version)

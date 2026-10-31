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
from modules.youtube import channel_art, metadata, details, layout, channels
from fastapi.responses import Response
import random
import config
from pathlib import Path

router=APIRouter()


class SavedDetailsResponse(BaseModel):
    captured:bool
    captured_at:str|None
    description:str|None=Field(max_length=details.MAX_BYTES)
    description_available:bool
    description_status:Literal['absent','unavailable','captured']
    like_count:int|None=Field(ge=0,le=details.MAX_COUNT)
    dislike_count:int|None=Field(ge=0,le=details.MAX_COUNT)
    extractor:str|None
    extractor_version:str|None


def _saved_details(job_id):
    _playlist_enabled()
    item=jobs.get(job_id)
    if not item or item['status']!='complete' or not item.get('path'):
        raise HTTPException(404,'Saved details are unavailable.')
    try:
        source,root=jobs.destination(item['source_id'],item['root'])
        with get_user_db() as connection:
            registered=sources.list_sources(connection)
            forbidden=[config.SUITE_HOME,*[Path(child.path) for child in sources.descendant_sources(source,registered)]]
            serving.resolve_served_file(root,item['path'],forbidden)
            if metadata.preserved_capture(connection,job_id) is not None:
                if metadata.preserved_media(connection,job_id,source_id=source.source_id,
                        source_root=root,forbidden_roots=forbidden,hash_assets=False) is None:
                    raise ValueError('Saved publication is unavailable.')
            elif not jobs.published_output(item,root):
                raise ValueError('Saved publication is unavailable.')
        value,content=details.read(root,item,forbidden)
        with get_user_db() as connection:
            current=sources.get_source(connection,source.source_id)
            if current is None or not current.visible or (current.path,current.role)!=(source.path,source.role):
                raise ValueError('Saved source changed.')
            latest_forbidden=[config.SUITE_HOME,*[Path(child.path) for child in sources.descendant_sources(current,sources.list_sources(connection))]]
            serving.resolve_served_file(root,item['path'],latest_forbidden)
            # Added nested roots must also fence the description, not just media.
            if set(map(str,latest_forbidden))!=set(map(str,forbidden)):
                raise ValueError('Saved source ownership changed.')
        latest=jobs.get(job_id)
        if not latest or latest['status']!='complete' or any(latest.get(key)!=item.get(key)for key in('source_id','root','path','manifest_sha256')):
            raise ValueError('Saved publication changed.')
    except (OSError,ValueError,serving.ServeDenied) as error:
        raise HTTPException(404,'Saved details are unavailable.') from error
    return item,value,content


@router.get('/api/youtube/videos/{job_id}/details',response_model=SavedDetailsResponse)
def saved_details(job_id:Annotated[str,ApiPath(min_length=1,max_length=64)],response:Response):
    response.headers['Cache-Control']='private, no-store'
    with lifecycle.module_change_lock:
        _,value,_=_saved_details(job_id)
        return value


@router.get('/api/youtube/videos/{job_id}/description')
def saved_description(job_id:Annotated[str,ApiPath(min_length=1,max_length=64)]):
    from urllib.parse import quote
    with lifecycle.module_change_lock:
        item,_,content=_saved_details(job_id)
        if content is None:raise HTTPException(404,'No saved description is available.')
        try:filename=layout.name(item['title'] or 'YouTube','.description.txt',120)
        except ImportError:filename='description.txt'
        return Response(content,media_type='text/plain',headers={
            'Content-Disposition':"attachment; filename=description.txt; filename*=UTF-8''"+quote(filename,safe=''),
            'X-Content-Type-Options':'nosniff','Cache-Control':'private, no-store'})


class Download(BaseModel):
    url:str=Field(max_length=2048)
    source_id:str|None=Field(default=None,max_length=256)
    video:Literal['best','1080','720','480','audio']='best'
    audio:Literal['best','192','128']='best'
    request_id:UUID|None=None


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
    result=jobs.operational_state.library(get_user_db,q,offset,limit)
    return {'items':[public(item) for item in result['items']],'total':result['total']}


class ChannelSummary(BaseModel):
    id:str=Field(max_length=24)
    name:str=Field(max_length=512)
    avatar_id:str|None=Field(max_length=64)
    item_count:int=Field(ge=0)


class ChannelPage(BaseModel):
    items:list[ChannelSummary]
    total:int=Field(ge=0)


ChannelKey=Annotated[str,ApiPath(pattern=r'^(?:UC[A-Za-z0-9_-]{22}|unknown)$')]


@router.get('/api/youtube/channels',response_model=ChannelPage)
def channel_list(response:Response,q:str=Query('',max_length=256),offset:int=Query(0,ge=0),limit:int=Query(60,ge=1,le=120)):
    response.headers['Cache-Control']='private, no-store'
    with lifecycle.module_change_lock:
        _playlist_enabled()
        return channels.listing(get_user_db,q,offset,limit)


@router.get('/api/youtube/channels/{channel_key}/videos')
def channel_videos(channel_key:ChannelKey,response:Response,q:str=Query('',max_length=256),offset:int=Query(0,ge=0),limit:int=Query(60,ge=1,le=120)):
    response.headers['Cache-Control']='private, no-store'
    with lifecycle.module_change_lock:
        _playlist_enabled()
        try:result=channels.videos(get_user_db,channel_key,q,offset,limit)
        except ValueError as error:raise HTTPException(404,str(error)) from error
        return {**result,'items':[public(item)for item in result['items']]}


def _random_eligible(item):
    if item['status']!='complete' or item.get('superseded_by'):
        return False
    if item.get('video')=='audio' or Path(item['path']).suffix.lower() not in ('.mp4','.m4v','.webm'):
        return False
    with get_user_db() as connection:
        saved,reason=playlists.video(connection,item['id'])
        if reason or not saved or any(saved.get(key)!=item.get(key)for key in ('source_id','path')):
            return False
        source=sources.get_source(connection,item['source_id'])
        registered=sources.list_sources(connection)
    if source is None or not source.visible or source.role not in ('files','video','youtube'):
        return False
    try:
        serving.resolve_served_file(source.path,item['path'],
            [config.SUITE_HOME,*(Path(child.path)for child in sources.descendant_sources(source,registered))])
    except (serving.ServeDenied,OSError,ValueError):
        return False
    return True


@router.get('/api/youtube/channels/{channel_key}/random')
def channel_random(channel_key:ChannelKey,response:Response,q:str=Query('',max_length=256),current_id:str=Query('',max_length=64)):
    response.headers['Cache-Control']='private, no-store'
    with lifecycle.module_change_lock:
        _playlist_enabled()
        try:
            # The index is within this channel's current title search, matching its player queue.
            for item,position,total in channels.random_candidates(get_user_db,channel_key,q,current_id):
                if not _random_eligible(item):continue
                return {'item':public(item),'offset':position,'total':total}
        except ValueError as error:raise HTTPException(404,str(error)) from error
        return {'item':None,'offset':None,'total':channels.videos(get_user_db,channel_key,q,0,1)['total']}


@router.get('/api/youtube/random')
def random_video(response: Response, q: str = Query('', max_length=256),
                 current_id: str = Query('', max_length=64)):
    response.headers['Cache-Control'] = 'no-store'
    with lifecycle.module_change_lock:
        _playlist_enabled()
        first = library(q, 0, 1)
        total = first['total']
        for offset in random.sample(range(total), total):
            page = first if offset == 0 else library(q, offset, 1)
            if page['total'] != total or len(page['items']) != 1:
                raise HTTPException(409, 'Video list changed. Try again.')
            item = page['items'][0]
            if item['id']==current_id or not _random_eligible(item):continue
            return {'item': item, 'offset': offset, 'total': total}
        return {'item': None, 'offset': None, 'total': total}


@router.get('/api/youtube/channel-art/{job_id}')
def channel_avatar(job_id:Annotated[str,ApiPath(min_length=1,max_length=64)]):
    item=jobs.get(job_id)
    if not item or item['status']!='complete' or not item['avatar']:
        raise HTTPException(404,'Channel profile image is unavailable.')
    try:
        with jobs.get_jobs_db() as operation_db:
            native=operation_db.execute('SELECT id FROM youtube_downloads WHERE id=?',(job_id,)).fetchone()
        if native:
            _,root=jobs.destination(item['source_id'],item['root'])
            if not item['path'] or not jobs.published_output(item,root):raise ValueError('Published output is unavailable')
        else:
            with get_user_db() as db:
                source=sources.get_source(db,item['source_id'])
                if source is None or not source.visible or source.role not in ('files','video','youtube'):
                    raise ValueError('Saved artwork source is unavailable')
                root=serving.resolve_within_source(source.path,'',[config.SUITE_HOME])
                forbidden=[config.SUITE_HOME,*[Path(child.path) for child in sources.descendant_sources(source,sources.list_sources(db))]]
                if metadata.preserved_media(db,job_id,source_id=source.source_id,source_root=root,
                        forbidden_roots=forbidden,hash_assets=False) is None:
                    raise ValueError('Saved artwork publication is unavailable')
        folder=Path(item['path']).parent
        relative=Path(item['avatar'])
        if relative.as_posix() not in {(folder/name).as_posix() for name in metadata.ART_NAMES}:
            raise ValueError('Profile image location changed')
        cursor=root
        for part in relative.parts:
            cursor/=part
            if cursor.is_symlink():raise ValueError('Profile image contains a symbolic link')
        path=serving.resolve_served_file(root,relative.as_posix(),[config.SUITE_HOME])
        with path.open('rb') as handle:content=handle.read(channel_art.MAX_BYTES+1)
        extension=channel_art.image_type(content)
        if path.suffix!='.'+extension:raise ValueError('Profile image format changed')
    except (OSError,ValueError,serving.ServeDenied) as error:
        raise HTTPException(404,'Channel profile image is unavailable.') from error
    return Response(content,media_type={'jpg':'image/jpeg','png':'image/png','webp':'image/webp'}[extension],
                    headers={'X-Content-Type-Options':'nosniff','Cache-Control':'private, no-cache'})


@router.post('/api/youtube/downloads')
def download(payload:Download):
    try:return public(jobs.create(payload.url,payload.source_id,payload.video,payload.audio,payload.request_id))
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
    try:
        with get_user_db() as db:
            captured=metadata.preserved_capture(db,item['id'])
            if captured is not None:
                return metadata.log_preserved_playback_failure(db,item['id'],payload.reason,suite_home=config.SUITE_HOME)
        jobs.destination(item['source_id'],item['root'])
    except (ValueError,OSError,serving.ServeDenied) as exc:
        raise HTTPException(404,'Download folder is unavailable.') from exc
    jobs.logger.warning('Playback failed: job=%s source=%r file=%r reason=%s',
                        item['id'],item['source_id'],item['path'],payload.reason,
                        extra={'event_code':'playback.failed','library_id':item['source_id'],
                               'event_details':{'owner':'youtube','reason':payload.reason,'saved_id':item['id']}})
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

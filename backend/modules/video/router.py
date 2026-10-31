"""Discovery and bounded playback diagnostics; bytes use guarded shared Files routes."""
from contextlib import contextmanager
import logging
import random
from pathlib import Path, PurePosixPath
from uuid import UUID
from typing import Annotated, Literal
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field
import config
from config import FILES_DB_PATH, USER_DB_PATH
from services.search_text import literal_contains
from database import get_user_db
from files_base import index, sources, serving
from services.library_categories import CategoryStore
from modules.video import library as catalog_engine
from modules.video import progress as watch
from modules.video import metadata as details_engine
from suite_modules import require_enabled
import lifecycle

categories = CategoryStore('video')

router = APIRouter()
logger = logging.getLogger("keivotos.video")
VIDEO_EXTENSIONS = ("mp4", "m4v", "webm", "mkv", "mov", "avi", "ogv")


def video_sources():
    with get_user_db() as connection:
        sources.ensure_sources_schema(connection)
        return [source.source_id for source in sources.list_sources(connection)
                if source.role == "video" and source.visible]


@router.get("/api/video/library")
def library(q: str = Query("", max_length=256), offset: int = Query(0, ge=0),
            limit: int = Query(120, ge=1, le=240), category: Annotated[str | None, Query(max_length=36)] = None):
    if category is not None:
        with get_user_db() as connection:
            categories.require_category(connection, category)
    with get_user_db() as connection:
        registered = sources.list_sources(connection)
    with get_user_db() as connection:
        detail_state=details_engine.snapshot(connection,[s.source_id for s in registered if s.role=='video' and s.visible])
    owners = [source.source_id for source in registered if source.role == 'video']
    extensions = ','.join('?' for _ in VIDEO_EXTENSIONS)
    with index.open_index(FILES_DB_PATH) as connection:
        scope, scope_params = index.source_search_scope(connection, registered, source_ids=owners,
                                                        forbidden_paths=[config.SUITE_HOME])
        predicate, folded = literal_contains(connection, 'name', '' if detail_state['source'] or detail_state['personal'] else q.strip())
        where = f"{scope} AND ext IN ({extensions}) AND is_dir=0 AND available=1 AND {predicate}"
        params = [*scope_params, *VIDEO_EXTENSIONS, folded]
        if category is not None:
            connection.execute('ATTACH DATABASE ? AS category_user', (str(USER_DB_PATH),))
            predicate, values = categories.filter_sql(category, alias='files_index')
            where += ' AND ' + predicate
            params.extend(values)

        if (detail_state['source'] or detail_state['personal']) and q.strip():
            rows=connection.execute(f"SELECT source_id,relative_path AS path,name,ext,size,mtime FROM files_index WHERE {where} ORDER BY name COLLATE NOCASE,path,source_id",params).fetchall()
            matched=[dict(row) for row in rows if details_engine.matches(detail_state,row['source_id'],row['path'],'video',q,filename=True)]
            return {'items':matched[offset:offset+limit],'total':len(matched)}
        total = connection.execute(f"SELECT count(*) FROM files_index WHERE {where}", params).fetchone()[0]
        rows = connection.execute(
            f"SELECT source_id,relative_path AS path,name,ext,size,mtime FROM files_index WHERE {where} "
            "ORDER BY name COLLATE NOCASE, path, source_id LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
    return {"items": [dict(row) for row in rows], "total": total}



def title_catalog(q='', category=None, source_id=None, path=None, kind=None):
    with get_user_db() as connection:
        if category is not None:
            categories.require_category(connection, category)
        registered = sources.list_sources(connection)
        detail_state=details_engine.snapshot(connection,[s.source_id for s in registered if s.role=='video' and s.visible])
    return catalog_engine.titles(FILES_DB_PATH, registered, VIDEO_EXTENSIONS, [config.SUITE_HOME],
                                 user_db=USER_DB_PATH, categories=categories, category=category, q=q,
                                 source_id=source_id, title_path=path, kind=kind, metadata_state=detail_state)


@router.get('/api/video/catalog')
def catalog(response: Response, q: str = Query('', max_length=256), offset: int = Query(0, ge=0),
            limit: int = Query(120, ge=1, le=240), category: Annotated[str | None, Query(max_length=36)] = None):
    response.headers['Cache-Control'] = 'no-store'
    groups = title_catalog(q, category)
    return {'items': catalog_engine.summaries(groups[offset:offset+limit]), 'total': len(groups)}


@router.get('/api/video/episodes')
def episodes(response: Response, source_id: str = Query(..., min_length=1, max_length=256),
             path: str = Query(..., min_length=1, max_length=4096), kind: Literal['series', 'video'] = 'series',
             q: str = Query('', max_length=256), offset: int = Query(0, ge=0), limit: int = Query(120, ge=1, le=240),
             category: Annotated[str | None, Query(max_length=36)] = None):
    response.headers['Cache-Control'] = 'no-store'
    for group in title_catalog(q, category, source_id, path, kind):
        if group['source_id'] == source_id and group['path'] == path and group['kind'] == kind:
            return {'items': group['episodes'][offset:offset+limit], 'total': len(group['episodes'])}
    if q.strip() or category is not None:
        if any(group['source_id'] == source_id and group['path'] == path and group['kind'] == kind
               for group in title_catalog('', None, source_id, path, kind)):
            return {'items': [], 'total': 0}
    raise HTTPException(404, 'Video is unavailable.')


@router.get("/api/video/random")
def random_video(response: Response, q: str = Query("", max_length=256),
                 current_source_id: str = Query("", max_length=256),
                 current_path: str = Query("", max_length=4096),
                 category: Annotated[str | None, Query(max_length=36)] = None,
                 series_source_id: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
                 series_path: Annotated[str | None, Query(min_length=1, max_length=4096)] = None):
    response.headers['Cache-Control'] = 'no-store'
    if (series_source_id is None) != (series_path is None):
        raise HTTPException(422, 'Choose a series source and path together.')
    def read_page(offset):
        if series_source_id is not None:
            return episodes(response, series_source_id, series_path, 'series', q, offset, 1, category)
        return library(q, offset, 1, category)
    first = read_page(0)
    total = first['total']
    # A finite random permutation includes every page without loading media.
    for offset in random.sample(range(total), total):
        page = first if offset == 0 else read_page(offset)
        if page['total'] != total or len(page['items']) != 1:
            raise HTTPException(409, 'Video list changed. Try again.')
        item = page['items'][0]
        if (item['source_id'], item['path']) == (current_source_id, current_path):
            continue
        if item['ext'].lower() not in ('mp4', 'm4v', 'webm'):
            continue
        with get_user_db() as connection:
            source = sources.get_source(connection, item['source_id'])
            registered = sources.list_sources(connection)
        if source is None or not source.visible or source.role != 'video':
            continue
        try:
            serving.resolve_served_file(source.path, item['path'],
                [config.SUITE_HOME, *(Path(child.path) for child in sources.descendant_sources(source, registered))])
        except (serving.ServeDenied, OSError, ValueError):
            continue
        return {'item': item, 'offset': offset, 'total': total}
    return {'item': None, 'offset': None, 'total': total}


class PlaybackFailure(BaseModel):
    source_id: str = Field(max_length=256)
    path: str = Field(max_length=4096)
    reason: Literal["unsupported_format", "network", "decode", "unsupported_codec", "playback", "fullscreen"]


@router.post("/api/video/playback-error")
def playback_error(failure: PlaybackFailure):
    if failure.source_id not in video_sources():
        raise HTTPException(404, "Video folder is unavailable")
    # Match a known indexed item; no caller-supplied diagnostic text or file reads.
    with index.open_index(FILES_DB_PATH) as connection:
        row = connection.execute(
            "SELECT name FROM files_index WHERE source_id=? AND relative_path=? AND is_dir=0",
            (failure.source_id, failure.path)).fetchone()
    if row is None or PurePosixPath(failure.path).suffix.lower().lstrip('.') not in VIDEO_EXTENSIONS:
        raise HTTPException(404, "Video is unavailable")
    logger.warning("Playback failed: source=%r file=%r reason=%s", failure.source_id, failure.path, failure.reason,
                   extra={"event_code":"playback.failed","library_id":failure.source_id,
                          "event_details":{"reason":failure.reason,"owner":"video"}})
    return {"status": "logged"}


class VideoWatchSubject(BaseModel):
    source_id: str = Field(min_length=1,max_length=256)
    path: str = Field(min_length=1,max_length=4096)


class VideoWatchSession(VideoWatchSubject):
    revision: str = Field(pattern='^[a-f0-9]{64}$')
    expected_generation: int = Field(ge=0,le=9007199254740990)


class VideoWatchUpdate(VideoWatchSubject):
    revision: str = Field(pattern='^[a-f0-9]{64}$')
    generation: int = Field(ge=1,le=9007199254740991)
    sequence: int = Field(ge=1,le=9007199254740991)
    position: float = Field(ge=0,allow_inf_nan=False)
    duration: float = Field(gt=0,allow_inf_nan=False)
    ended: bool = False


class VideoWatchBatch(BaseModel):
    items: list[VideoWatchSubject] = Field(max_length=120)


def watch_target(connection, subject):
    try:
        require_enabled('video')
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    registered = sources.list_sources(connection)
    source = next((item for item in registered if item.source_id==subject.source_id and item.visible and item.role=='video'),None)
    if source is None: raise HTTPException(404,'Video is unavailable.')
    with index.open_index(FILES_DB_PATH) as files:
        scope,values=index.source_search_scope(files,registered,source_ids=[subject.source_id],forbidden_paths=[config.SUITE_HOME])
        row=files.execute('SELECT ext FROM files_index WHERE '+scope+' AND source_id=? AND relative_path=? AND available=1 AND is_dir=0',
                          [*values,subject.source_id,subject.path]).fetchone()
    if row is None or row['ext'] not in ('mp4','m4v','webm'):
        raise HTTPException(404,'Video is unavailable.')
    try:
        return serving.resolve_served_file(source.path,subject.path,
            [config.SUITE_HOME,*(Path(child.path) for child in sources.descendant_sources(source,registered))])
    except (serving.ServeDenied,OSError,ValueError,RuntimeError) as error:
        raise HTTPException(404,'Video is unavailable.') from error


def watch_revision(connection, subject):
    target=watch_target(connection,subject)
    try:
        current=watch.revision(target)
    except OSError as error:
        raise HTTPException(409,'Video revision is unavailable. Reopen it.') from error
    if watch_target(connection,subject)!=target:
        raise HTTPException(409,'Video changed. Reopen it.')
    return current


def check_watch_revision(connection, subject):
    if watch_revision(connection,subject)!=subject.revision:
        raise HTTPException(409,'Video changed. Reopen it before saving progress.')


@contextmanager
def watch_connection():
    with lifecycle.module_change_lock:
        try: require_enabled('video')
        except RuntimeError as error: raise HTTPException(409,str(error)) from error
        with get_user_db() as connection: yield connection


@router.post('/api/video/progress/read')
def read_watch_position(payload:VideoWatchSubject,response:Response):
    response.headers['Cache-Control']='no-store'
    with watch_connection() as connection:
        current=watch_revision(connection,payload)
        return watch.read(connection,payload.source_id,payload.path,current)


@router.post('/api/video/progress/session')
def begin_watch_session(payload:VideoWatchSession):
    with watch_connection() as connection:
        check_watch_revision(connection,payload)
        return watch.begin(connection,payload,lambda:check_watch_revision(connection,payload))


@router.post('/api/video/progress')
def save_watch_position(payload:VideoWatchUpdate):
    with watch_connection() as connection:
        check_watch_revision(connection,payload)
        return watch.save(connection,payload,lambda:check_watch_revision(connection,payload))


@router.post('/api/video/progress/positions')
def watch_positions(payload:VideoWatchBatch,response:Response):
    response.headers['Cache-Control']='no-store'
    result=[]
    with watch_connection() as connection:
        for subject in payload.items:
            try:
                watch_target(connection,subject)
                exists=connection.execute('SELECT 1 FROM video_watch_progress WHERE source_id=? AND path=? LIMIT 1',
                                           (subject.source_id,subject.path)).fetchone()
                position=watch.read(connection,subject.source_id,subject.path,watch_revision(connection,subject))['position'] if exists else None
                result.append(dict(source_id=subject.source_id,path=subject.path,position=position))
            except HTTPException as error:
                if error.status_code!=404: raise
                result.append(dict(source_id=subject.source_id,path=subject.path,position=None))
    return {'items':result}


class VideoDetailSubject(BaseModel):
    source_id:str=Field(min_length=1,max_length=256)
    path:str=Field(min_length=1,max_length=4096)
    kind:Literal['series','season','video']


class VideoDetailUpdate(VideoDetailSubject):
    fields:dict=Field(max_length=2)
    version:int=Field(ge=0,le=9007199254740990)
    source_revision:str=Field(pattern='^[a-f0-9]{64}$')
    request_id:UUID


def detail_context(connection,source_id):
    return details_engine.context(connection,source_id,index_path=FILES_DB_PATH)


@router.get('/api/video/metadata')
def video_details(response:Response,source_id:str=Query(...,min_length=1,max_length=256),path:str=Query(...,min_length=1,max_length=4096),kind:Literal['series','season','video']='video'):
    response.headers['Cache-Control']='no-store'
    with watch_connection() as connection:
        return details_engine.details(detail_context(connection,source_id),path,kind)


@router.post('/api/video/metadata/refresh')
def refresh_video_details(payload:VideoDetailSubject):
    with watch_connection() as connection:
        ctx=detail_context(connection,payload.source_id)
        try:result=details_engine.refresh(ctx,payload.path,payload.kind)
        except (OSError,ValueError,serving.ServeDenied) as error:raise HTTPException(409,'Local details could not be refreshed. Retry.') from error
        return dict(**details_engine.details(ctx,payload.path,payload.kind),refresh=result)


@router.post('/api/video/metadata/overrides')
def save_video_details(payload:VideoDetailUpdate):
    with watch_connection() as connection:
        try:return details_engine.save_choices(detail_context(connection,payload.source_id),payload.path,payload.kind,payload.fields,payload.version,payload.source_revision,str(payload.request_id))
        except ValueError as error:raise HTTPException(422,str(error)) from error

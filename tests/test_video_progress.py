"""Observed watch-state contracts in generated, isolated Files libraries."""
from contextlib import closing
from pathlib import Path
import json
import math
import os
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from tests.test_video_library import catalog
from modules.video import router, progress
from modules.video.user_schema import publish
from files_base import sources, index
from services import item_catalog, data_migrations


@pytest.fixture
def watched(catalog, monkeypatch):
    monkeypatch.setattr(router,'require_enabled',lambda _:None)
    with catalog[2]() as connection: publish(connection)
    return catalog


def subject(watched, path='film2.mp4'):
    return dict(source_id=watched[0].source_id,path=path)


def read(watched, path='film2.mp4'):
    response=Response();result=router.read_watch_position(router.VideoWatchSubject(**subject(watched,path)),response)
    assert response.headers['cache-control']=='no-store'
    return result


def begin(watched, state=None, path='film2.mp4'):
    state=state or read(watched,path)
    return router.begin_watch_session(router.VideoWatchSession(**subject(watched,path),revision=state['revision'],expected_generation=state['generation']))


def save(watched, session, sequence=1, position=12.5, duration=100.0, ended=False, path='film2.mp4'):
    return router.save_watch_position(router.VideoWatchUpdate(**subject(watched,path),**session,
        sequence=sequence,position=position,duration=duration,ended=ended))


def test_read_and_listing_never_create_observed_positions(watched):
    source, media, connect, db=watched
    before={p.relative_to(media).as_posix():p.read_bytes() for p in media.rglob('*') if p.is_file()}
    assert read(watched)['position'] is None
    router.library('',0,120);router.catalog(Response(),'',0,120,None)
    result=router.watch_positions(router.VideoWatchBatch(items=[router.VideoWatchSubject(**subject(watched))]),Response())
    assert result=={'items':[dict(**subject(watched),position=None)]}
    with connect() as connection:
        assert connection.execute('SELECT count(*) FROM video_watch_progress').fetchone()[0]==0
        assert connection.execute('SELECT count(*) FROM video_watch_sessions').fetchone()[0]==0
    session=begin(watched)
    assert session['generation']==1 and read(watched)['position'] is None
    assert before=={p.relative_to(media).as_posix():p.read_bytes() for p in media.rglob('*') if p.is_file()}


def test_exact_retry_old_sequence_and_newer_session_are_ordered(watched):
    session=begin(watched);first=save(watched,session)
    assert save(watched,session)==first
    with pytest.raises(HTTPException):save(watched,session,position=50)
    newer=save(watched,session,sequence=2,position=30)
    with pytest.raises(HTTPException):save(watched,session,sequence=1)
    state=read(watched);next_session=begin(watched,state)
    with pytest.raises(HTTPException):save(watched,session,sequence=3,position=90)
    assert read(watched)['position']==newer
    saved=save(watched,next_session,position=5)
    assert saved['generation']==2 and saved['position']==5
    with pytest.raises(HTTPException):begin(watched,state)
    assert read(watched)['position']==saved


def test_replaced_revision_retains_history_and_never_applies_old_offset(watched):
    source,media,connect,_=watched
    state=read(watched);session=begin(watched,state);saved=save(watched,session)
    target=media/'film2.mp4';target.rename(media/'preserved-film.fixture');target.write_bytes(b'replacement video revision')
    fresh=read(watched)
    assert fresh['revision']!=state['revision'] and fresh['position'] is None
    with pytest.raises(HTTPException):save(watched,session,sequence=2)
    with pytest.raises(HTTPException):begin(watched,state)
    save(watched,begin(watched,fresh),position=2)
    with connect() as connection:
        rows=connection.execute('SELECT * FROM video_watch_progress ORDER BY revision').fetchall()
        assert len(rows)==2
        assert next(dict(row) for row in rows if row['revision']==saved['revision'])==saved
    assert (media/'preserved-film.fixture').read_bytes()==b'film2.mp4'


def test_same_size_rewrite_with_restored_mtime_has_a_new_revision_and_no_retained_handle(watched):
    path=watched[1]/'film2.mp4';before=path.stat();original=read(watched)
    raw=path.read_bytes();path.write_bytes(b'X'*len(raw));os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
    assert read(watched)['revision']!=original['revision']
    stable=read(watched)['revision'];assert read(watched)['revision']==stable
    path.rename(path.with_suffix('.preserved'))
    assert path.with_suffix('.preserved').read_bytes()==b'X'*len(raw)


@pytest.mark.parametrize('position,duration,ended',[(-1,100,False),(101,100,False),(1,0,False),
    (math.inf,100,False),(1,math.nan,False),(1,100,True)])
def test_invalid_positions_do_not_replace_confirmed_progress(watched,position,duration,ended):
    session=begin(watched);saved=save(watched,session)
    payload=SimpleNamespace(**subject(watched),**session,sequence=2,position=position,duration=duration,ended=ended)
    with watched[2]() as connection:
        with pytest.raises(HTTPException):progress.save(connection,payload,lambda:None)
    assert read(watched)['position']==saved


def test_validation_failure_before_commit_rolls_back_position_and_session(watched):
    session=begin(watched);saved=save(watched,session)
    def changed():raise HTTPException(409,'Changed during write')
    payload=router.VideoWatchUpdate(**subject(watched),**session,sequence=2,position=50,duration=100)
    with pytest.raises(HTTPException):
        with watched[2]() as connection:progress.save(connection,payload,changed)
    assert read(watched)['position']==saved
    start=router.VideoWatchSession(**subject(watched),revision=session['revision'],expected_generation=1)
    with pytest.raises(HTTPException):
        with watched[2]() as connection:progress.begin(connection,start,changed)
    assert read(watched)['generation']==1


@pytest.mark.parametrize('state',['hidden','role','nested','private','missing','unindexed','unsupported','outside'])
def test_fresh_owner_index_and_containment_guards(watched,monkeypatch,state):
    source,media,connect,db=watched;session=begin(watched);saved=save(watched,session)
    path='film2.mp4'
    if state in ('hidden','role','nested'):
        with connect() as connection:
            if state=='hidden':sources.update_source(connection,source.source_id,visible=False)
            elif state=='role':sources.update_source(connection,source.source_id,role='files')
            else:
                child=media/'child';child.mkdir();(media/path).rename(child/path)
                with index.open_index(db) as files:index.scan_source(files,source.source_id,media)
                sources.register_source(connection,child,role='files');path='child/'+path
    elif state=='private':monkeypatch.setattr(router.config,'SUITE_HOME',media)
    elif state=='missing':(media/path).rename(media/'retained.fixture')
    elif state=='unindexed':path='not-indexed.mp4';(media/path).write_bytes(b'new unindexed video')
    elif state=='unsupported':path='Series 10/episode 1.mkv'
    else:path='../outside.mp4'
    with pytest.raises(HTTPException) as failure:read(watched,path)
    assert failure.value.status_code==404
    with connect() as connection:
        assert dict(connection.execute('SELECT * FROM video_watch_progress').fetchone())==saved


def test_duration_and_end_are_observed_facts_and_are_backed_up(watched,tmp_path):
    session=begin(watched);save(watched,session,position=99)
    ended=save(watched,session,sequence=2,position=100,ended=True)
    assert ended['ended']==1 and ended['position']==ended['duration']
    db=Path(router.USER_DB_PATH);backup=tmp_path/'snapshot.sqlite'
    data_migrations.snapshot_user_database(db,backup)
    with closing(sqlite3.connect(backup)) as connection:
        connection.row_factory=sqlite3.Row
        assert dict(connection.execute('SELECT * FROM video_watch_progress').fetchone())==ended
        assert connection.execute('SELECT generation FROM video_watch_sessions').fetchone()[0]==session['generation']


def test_watch_catalog_import_is_separate_idempotent_and_retains_original_rows(watched):
    session=begin(watched);saved=save(watched,session)
    with watched[2]() as connection:
        item_catalog.ensure_catalog_schema(connection);connection.commit();connection.execute('BEGIN')
        assert progress.import_catalog_records(connection)==2
        rows=[tuple(row) for row in connection.execute('SELECT * FROM kv_items ORDER BY id')]
        assert len(rows)==3
        assert progress.import_catalog_records(connection)==2
        assert rows==[tuple(row) for row in connection.execute('SELECT * FROM kv_items ORDER BY id')]
        assert dict(connection.execute('SELECT * FROM video_watch_progress').fetchone())==saved
    from modules.video.transfer import contribution
    value=contribution()
    assert [(i.name,i.version) for i in value.catalog_importers]==[('video.essential-records',1),('video.watch-records',1),('video.metadata-records',1)]
    assert {'video_watch_progress','video_watch_sessions'}<=set(value.personal_tables)


def test_watch_importer_refuses_invalid_schema_without_partial_catalog_state():
    with closing(sqlite3.connect(':memory:')) as db:
        db.executescript("CREATE TABLE video_watch_sessions(source_id TEXT,path TEXT,generation INTEGER,revision TEXT); INSERT INTO video_watch_sessions VALUES('source','video.mp4',1,'invalid')")
        item_catalog.ensure_catalog_schema(db);db.execute('BEGIN')
        with pytest.raises(ValueError):progress.import_catalog_records(db)
        assert db.execute('SELECT count(*) FROM kv_items').fetchone()[0]==0
    with closing(sqlite3.connect(':memory:')) as db:
        item_catalog.ensure_catalog_schema(db);db.execute('BEGIN');assert progress.import_catalog_records(db)==0


def test_disabled_owner_refuses_before_opening_database(watched,monkeypatch):
    def disabled(_):raise RuntimeError('Video is disabled')
    monkeypatch.setattr(router,'require_enabled',disabled)
    monkeypatch.setattr(router,'get_user_db',lambda:pytest.fail('Disabled owner opened storage'))
    with pytest.raises(HTTPException) as error:read(watched)
    assert error.value.status_code==409


def test_watch_routes_validate_http_payloads_and_keep_reads_uncached(watched):
    import asyncio
    from fastapi import FastAPI
    app=FastAPI();app.include_router(router.router)
    def request(path,body):
        async def run():
            messages=[]
            async def receive():return {'type':'http.request','body':json.dumps(body).encode(),'more_body':False}
            async def send(message):messages.append(message)
            await app({'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'POST','scheme':'http',
                'path':'/api/video/progress'+path,'raw_path':('/api/video/progress'+path).encode(),'query_string':b'',
                'root_path':'','headers':[(b'content-type',b'application/json')],'client':('127.0.0.1',1),'server':('test',80)},receive,send)
            start=next(message for message in messages if message['type']=='http.response.start')
            raw=b''.join(message.get('body',b'') for message in messages if message['type']=='http.response.body')
            return start,json.loads(raw)
        return asyncio.run(run())
    start,state=request('/read',subject(watched));assert start['status']==200 and (b'cache-control',b'no-store') in start['headers']
    assert state['position'] is None
    assert request('/positions',{'items':[subject(watched)]*121})[0]['status']==422
    assert request('/session',dict(**subject(watched),revision='bad',expected_generation=0))[0]['status']==422
    assert request('/read',dict(**subject(watched),extra='ignored'))[0]['status']==200
    assert request('/read',dict(source_id=watched[0].source_id,path='../outside.mp4'))[0]['status']==404

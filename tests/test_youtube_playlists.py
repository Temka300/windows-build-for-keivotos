"""Actual isolated SQLite/filesystem playlist operations; no downloader/network."""
import asyncio
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4, UUID
from types import SimpleNamespace
from urllib.parse import urlsplit
import json
import sqlite3
import sys

import pytest
from fastapi import FastAPI, HTTPException
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from files_base import sources
from modules.youtube import _publish, router


@pytest.fixture
def local(tmp_path,monkeypatch):
    root=tmp_path/'generated';root.mkdir()
    user=tmp_path/'user.sqlite'
    @contextmanager
    def connect():
        with sqlite3.connect(user) as connection:
            connection.row_factory=sqlite3.Row
            yield connection
    with connect() as connection:
        sources.ensure_sources_schema(connection);_publish(connection)
        connection.execute('CREATE TABLE preserved(value TEXT)')
        connection.execute("INSERT INTO preserved VALUES ('personal annotation')")
        source=sources.register_source(connection,root)
        for identity in ('first','second','audio','pending'):
            path=identity+('.mp3' if identity=='audio' else '.mp4')
            (root/path).write_bytes(('generated '+identity).encode())
            connection.execute("""INSERT INTO youtube_downloads
                (id,url,source_id,root,video,audio,title,status,path) VALUES (?,?,?,?,?,?,?,?,?)""",
                (identity,'https://www.youtube.com/watch?v=abcdefghijk',source.source_id,str(root),
                 'audio' if identity=='audio' else 'best','best','동영상 '+identity,
                 'queued' if identity=='pending' else 'complete',path))
        connection.commit()
    monkeypatch.setattr(router,'get_user_db',connect)
    monkeypatch.setattr(router,'require_enabled',lambda owner:None)
    monkeypatch.setattr(router.playlists.config,'SUITE_HOME',tmp_path/'home')
    return root,connect,source


def make(title='휴식 Music',token=None):
    return router.create_playlist(router.PlaylistCreate(title=title,request_id=token or uuid4()))


def save(job,ids,version):
    return router.save_video_playlists(job,router.PlaylistMembership(playlist_ids=ids,version=version))


def test_additive_migration_unicode_create_idempotency_and_pagination(local):
    root,connect,source=local
    with connect() as connection:
        before=[tuple(row) for row in connection.execute('SELECT * FROM youtube_downloads')]
        _publish(connection);_publish(connection)
        assert [tuple(row) for row in connection.execute('SELECT * FROM youtube_downloads')]==before
        assert connection.execute('SELECT value FROM preserved').fetchone()[0]=='personal annotation'
    token=uuid4()
    with ThreadPoolExecutor(max_workers=2) as pool:
        created=list(pool.map(lambda unused:make('  공부 음악  ',token),range(2)))
    assert created[0]==created[1] and created[0]['title']=='공부 음악'
    duplicate=make('공부 음악');assert duplicate['id']!=created[0]['id']
    with pytest.raises(HTTPException) as error:
        make('Changed title',token)
    assert error.value.status_code==409
    with pytest.raises(HTTPException):make('   ')
    assert router.list_playlists(0,1)['total']==2
    assert len(router.list_playlists(1,1)['items'])==1
    assert router.list_playlists(100,1)['items']==[]


def test_membership_order_atomic_conflicts_repeat_and_preservation(local):
    root,connect,source=local
    original={p.name:p.read_bytes() for p in root.iterdir()}
    p1=make();p2=make('Another')
    assert router.video_playlists('first')=={'version':0,'playlist_ids':[]}
    result=save('first',[p2['id'],p1['id']],0)
    assert result['version']==1
    assert save('first',[p1['id'],p2['id']],0)==result
    save('second',[p1['id']],0)
    items=router.playlist_items(UUID(p1['id']),0,120)
    assert [item['job_id'] for item in items['items']]==['first','second']
    assert all(item['available'] for item in items['items'])
    assert 'root' not in items['items'][0]['job']
    before=router.video_playlists('first')
    with pytest.raises(HTTPException):save('first',[str(uuid4())],1)
    assert router.video_playlists('first')==before
    with pytest.raises(HTTPException):save('first',[],0)
    assert router.video_playlists('first')==before
    save('first',[],1)
    assert [item['job_id'] for item in router.playlist_items(UUID(p1['id']),0,120)['items']]==['second']
    save('first',[p1['id']],2)
    assert [item['job_id'] for item in router.playlist_items(UUID(p1['id']),0,120)['items']]==['second','first']
    assert {p.name:p.read_bytes() for p in root.iterdir()}==original
    with connect() as connection:
        assert connection.execute('SELECT count(*) FROM youtube_downloads').fetchone()[0]==4
        assert connection.execute('SELECT value FROM preserved').fetchone()[0]=='personal annotation'


@pytest.mark.parametrize('state',['missing','hidden','wrong_role','moved','traversal','absent_record'])
def test_unavailable_members_retained_and_cannot_mutate(local,state,tmp_path):
    root,connect,source=local
    p=make();save('first',[p['id']],0)
    if state=='missing':(root/'first.mp4').rename(root/'preserved-copy.mp4')
    with connect() as connection:
        if state=='hidden':sources.update_source(connection,source.source_id,visible=False)
        elif state=='wrong_role':sources.update_source(connection,source.source_id,role='language')
        elif state=='moved':
            elsewhere=tmp_path/'elsewhere';elsewhere.mkdir()
            connection.execute('UPDATE files_sources SET path=? WHERE source_id=?',(str(elsewhere),source.source_id))
        elif state=='traversal':connection.execute("UPDATE youtube_downloads SET path='../outside.mp4' WHERE id='first'")
        elif state=='absent_record':connection.execute("DELETE FROM youtube_downloads WHERE id='first'")  # disposable row only
        connection.commit()
    item=router.playlist_items(UUID(p['id']),0,120)['items'][0]
    assert not item['available'] and item['unavailable_reason']
    with pytest.raises(HTTPException):save('first',[],1)
    with connect() as connection:
        assert connection.execute('SELECT count(*) FROM youtube_playlist_items').fetchone()[0]==1


def test_audio_unfinished_disabled_and_ownership_rejected(local,monkeypatch):
    p=make()
    for identity in ('audio','pending','absent'):
        with pytest.raises(HTTPException):save(identity,[p['id']],0)
    monkeypatch.setattr(router,'require_enabled',lambda owner:(_ for _ in ()).throw(RuntimeError('not installed or disabled')))
    for operation in (lambda:make(),lambda:router.list_playlists(0,60),lambda:save('first',[p['id']],0)):
        with pytest.raises(HTTPException) as error:operation()
        assert error.value.status_code==409
    with local[1]() as connection:
        assert connection.execute('SELECT count(*) FROM youtube_playlists').fetchone()[0]==1
        assert connection.execute('SELECT count(*) FROM youtube_playlist_items').fetchone()[0]==0


def request(method,path,body=None):
    app=FastAPI();app.include_router(router.router)
    async def run():
        messages=[];content=json.dumps(body or {}).encode();received=False;url=urlsplit(path)
        async def receive():
            nonlocal received
            if received:return {'type':'http.disconnect'}
            received=True;return {'type':'http.request','body':content,'more_body':False}
        async def send(message):messages.append(message)
        await app({'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':method,
                   'scheme':'http','path':url.path,'raw_path':url.path.encode(),'query_string':url.query.encode(),
                   'headers':[(b'content-type',b'application/json')],'client':('127.0.0.1',1),'server':('test',80)},receive,send)
        status=next(m['status'] for m in messages if m['type']=='http.response.start')
        return status,json.loads(b''.join(m.get('body',b'') for m in messages if m['type']=='http.response.body'))
    return asyncio.run(run())


def test_actual_http_create_membership_validation_and_items(local):
    status,p=request('POST','/api/youtube/playlists',{'title':'한국어','request_id':str(uuid4())});assert status==200
    assert request('POST','/api/youtube/playlists',{'title':'Broken','request_id':'invalid'})[0]==422
    assert request('POST','/api/youtube/videos/first/playlists',{'playlist_ids':[p['id']],'version':0})[0]==200
    status,items=request('GET',f"/api/youtube/playlists/{p['id']}/items");assert status==200 and items['items'][0]['job_id']=='first'
    assert request('POST','/api/youtube/videos/first/playlists',{'playlist_ids':[],'version':-1})[0]==422
    assert request('GET','/api/youtube/playlists/not-a-uuid/items')[0]==422


@pytest.mark.parametrize('role',['files','video','youtube'])
def test_membership_accepts_each_supported_visible_destination(local,role):
    with local[1]() as connection:
        sources.update_source(connection,local[2].source_id,role=role)
    p=make()
    assert save('first',[p['id']],0)['version']==1
    assert router.playlist_items(UUID(p['id']),0,60)['items'][0]['available']


def test_simultaneous_sqlite_membership_changes_accept_only_one_version(local):
    p1=make('First choice');p2=make('Second choice')
    def update(identity):
        with local[1]() as connection:
            try:return router.playlists.set_membership(connection,'first',[identity],0)
            except HTTPException as error:return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(update,[p1['id'],p2['id']]))
    assert sum(isinstance(result,dict) for result in results)==1
    assert results.count(409)==1
    saved=router.video_playlists('first');assert saved['version']==1
    assert len(saved['playlist_ids'])==1
    with local[1]() as connection:
        assert connection.execute('SELECT count(*) FROM youtube_playlist_items').fetchone()[0]==1


def test_membership_sql_failure_rolls_back_removal_and_version(local):
    old=make('Keep');first=make('New first');second=make('New second')
    save('first',[old['id']],0)
    with local[1]() as connection:
        connection.execute("CREATE TRIGGER fail_selected BEFORE INSERT ON youtube_playlist_items WHEN NEW.playlist_id='"+second['id']+"' BEGIN SELECT raise(ABORT,'generated write failure'); END")
        connection.commit()
    with pytest.raises(sqlite3.IntegrityError):save('first',[first['id'],second['id']],1)
    assert router.video_playlists('first')=={'version':1,'playlist_ids':[old['id']]}
    with local[1]() as connection:
        assert connection.execute('SELECT count(*) FROM youtube_playlist_items').fetchone()[0]==1
        assert connection.execute('SELECT value FROM preserved').fetchone()[0]=='personal annotation'


@pytest.mark.parametrize('state',['escaping_symlink','forbidden_suite_root','superseded','directory'])
def test_guarded_unavailable_items_retain_relationships(local,state,tmp_path,monkeypatch):
    root,connect,source=local;p=make();save('first',[p['id']],0)
    if state in ('escaping_symlink','directory'):
        (root/'first.mp4').rename(root/'preserved-first.mp4')
        if state=='escaping_symlink':
            outside=tmp_path/'outside.mp4';outside.write_bytes(b'generated outside');(root/'first.mp4').symlink_to(outside)
        else:(root/'first.mp4').mkdir()
    elif state=='forbidden_suite_root':monkeypatch.setattr(router.playlists.config,'SUITE_HOME',root)
    else:
        with connect() as connection:
            connection.execute("UPDATE youtube_downloads SET superseded_by='second' WHERE id='first'");connection.commit()
    item=router.playlist_items(UUID(p['id']),0,60)['items'][0]
    assert not item['available'] and item['unavailable_reason']
    with pytest.raises(HTTPException):save('first',[],1)
    with connect() as connection:
        assert connection.execute('SELECT job_id FROM youtube_playlist_items').fetchone()[0]=='first'
        assert connection.execute('SELECT count(*) FROM youtube_downloads').fetchone()[0]==4


@pytest.mark.parametrize('method,path,body',[
 ('POST','/api/youtube/playlists',{'title':'x'*121,'request_id':str(uuid4())}),
 ('POST','/api/youtube/playlists',{'title':'','request_id':str(uuid4())}),
 ('GET','/api/youtube/playlists?offset=-1',None),
 ('GET','/api/youtube/playlists?limit=121',None),
 ('GET','/api/youtube/playlists/'+str(uuid4())+'/items?limit=0',None),
 ('POST','/api/youtube/videos/first/playlists',{'playlist_ids':[str(uuid4())]*257,'version':0}),
 ('POST','/api/youtube/videos/first/playlists',{'playlist_ids':['invalid'],'version':0}),
 ('POST','/api/youtube/videos/first/playlists',{'playlist_ids':[],'version':9007199254740991}),
 ('GET','/api/youtube/videos/'+('x'*65)+'/playlists',None),
])
def test_http_payload_and_pagination_bounds_precede_mutation(local,method,path,body):
    assert request(method,path,body)[0]==422
    with local[1]() as connection:
        assert connection.execute('SELECT count(*) FROM youtube_playlists').fetchone()[0]==0
        assert connection.execute('SELECT count(*) FROM youtube_playlist_items').fetchone()[0]==0


@pytest.mark.parametrize('state,expected',[('absent',409),('disabled',409),('failed',503)])
def test_all_playlist_routes_enforce_actual_owner_before_storage(local,monkeypatch,state,expected):
    import database,suite_modules,lifecycle
    with local[1]() as connection:
        suite_modules.ensure_schema(connection)
        if state=='failed':suite_modules.set_enabled(connection,'youtube',True)
    monkeypatch.setattr(database,'get_user_db',local[1])
    monkeypatch.setattr(router,'require_enabled',suite_modules.require_enabled)
    monkeypatch.setattr(lifecycle,'module_runtime',SimpleNamespace(status=lambda owner:{'state':'failed'}) if state=='failed' else None)
    if state=='absent':monkeypatch.setattr(suite_modules,'MODULE_REGISTRY',SimpleNamespace(get=lambda owner:None))
    def forbidden_storage():raise AssertionError('Unavailable owner opened playlist storage')
    monkeypatch.setattr(router,'get_user_db',forbidden_storage)
    identity=str(uuid4())
    for method,path,body in [
      ('GET','/api/youtube/playlists',None),
      ('POST','/api/youtube/playlists',{'title':'No mutation','request_id':identity}),
      ('GET','/api/youtube/playlists/'+identity+'/items',None),
      ('GET','/api/youtube/videos/first/playlists',None),
      ('POST','/api/youtube/videos/first/playlists',{'playlist_ids':[identity],'version':0}),
    ]:assert request(method,path,body)[0]==expected


def test_disable_and_reenable_preserve_personal_playlist_state(local,monkeypatch):
    import database,suite_modules,lifecycle
    p=make();save('first',[p['id']],0)
    monkeypatch.setattr(database,'get_user_db',local[1]);monkeypatch.setattr(router,'require_enabled',suite_modules.require_enabled)
    monkeypatch.setattr(lifecycle,'module_runtime',None)
    with local[1]() as connection:suite_modules.ensure_schema(connection)
    assert request('GET','/api/youtube/playlists')[0]==409
    with local[1]() as connection:suite_modules.set_enabled(connection,'youtube',True)
    assert request('GET','/api/youtube/playlists')[1]['items'][0]['id']==p['id']
    assert router.video_playlists('first')=={'version':1,'playlist_ids':[p['id']]}
    with local[1]() as connection:
        suite_modules.set_enabled(connection,'youtube',False);suite_modules.set_enabled(connection,'youtube',True)
    assert router.video_playlists('first')['playlist_ids']==[p['id']]

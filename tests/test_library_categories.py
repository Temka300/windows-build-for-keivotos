"""Real owner-scoped SQLite categories, retry races and guarded HTTP contracts."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, closing
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI, HTTPException, Response

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import database
import lifecycle
import suite_modules
from files_base import sources
from modules.video import categories as video
from modules.manga import categories as manga
from services.library_categories import CategoryStore, CategoryCreate
from services.library_categories import CategoryMembershipSave
from server import module_guard


@pytest.fixture
def local(tmp_path, monkeypatch):
    path = tmp_path / 'user.sqlite'
    @contextmanager
    def connect():
        with closing(sqlite3.connect(path)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            yield connection
    with connect() as connection:
        sources.ensure_sources_schema(connection)
        suite_modules.ensure_schema(connection)
        connection.execute('CREATE TABLE preserved(note TEXT)')
        connection.execute("INSERT INTO preserved VALUES('original annotation')")
        source = sources.register_source(connection, tmp_path / 'missing library')
        connection.commit()
        for owner in ('video', 'manga'):
            suite_modules.set_enabled(connection, owner, True)
            descriptor = suite_modules.MODULE_REGISTRY.require(owner)
            descriptor.publish(connection)
    for target in (database, video, manga):
        monkeypatch.setattr(target, 'get_user_db', connect)
    monkeypatch.setattr(lifecycle, 'module_runtime', None)
    return connect, source, path


def create(owner, name='공부', token=None):
    return owner.create_category(CategoryCreate(name=name, request_id=token or uuid4()))


def listing(owner, offset=0, limit=120):
    return owner.list_categories(Response(), offset, limit)


def request(owner, method='GET', query='', body=None, endpoint='categories'):
    app = FastAPI()
    app.include_router(owner.router, dependencies=[Depends(module_guard('video' if owner is video else 'manga'))])
    async def run():
        messages=[];received=False;url=urlsplit('/api/'+('video' if owner is video else 'manga')+'/'+endpoint+query)
        async def receive():
            nonlocal received
            if received:return {'type':'http.disconnect'}
            received=True
            return {'type':'http.request','body':json.dumps(body or {}).encode(),'more_body':False}
        async def send(message):messages.append(message)
        await app({'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':method,
            'scheme':'http','path':url.path,'raw_path':url.path.encode(),'query_string':url.query.encode(),
            'headers':[(b'content-type',b'application/json')],'client':('127.0.0.1',1),'server':('test',80)},receive,send)
        start=next(message for message in messages if message['type']=='http.response.start')
        data=json.loads(b''.join(message.get('body',b'') for message in messages if message['type']=='http.response.body'))
        return start['status'],data,dict(start['headers'])
    return asyncio.run(run())


def test_additive_owner_schemas_keep_old_progress_annotations_and_exact_memberships(local):
    connect, source, _ = local
    saved = create(video)
    with connect() as connection:
        connection.execute('INSERT INTO video_category_memberships(category_id,source_id,path) VALUES(?,?,?)', (saved['id'], source.source_id, 'same/name.mp4'))
        connection.execute('INSERT INTO video_category_memberships(category_id,source_id,path) VALUES(?,?,?)', (saved['id'], 'other-source', 'same/name.mp4'))
        connection.execute("INSERT INTO manga_reading_sessions VALUES(?,'chapter','folder',7)", (source.source_id,))
        connection.commit()
        before = [tuple(row) for row in connection.execute('SELECT * FROM video_category_memberships')]
        for _ in range(2):
            for owner in ('video', 'manga'):
                suite_modules.MODULE_REGISTRY.require(owner).publish(connection)
        assert [tuple(row) for row in connection.execute('SELECT * FROM video_category_memberships')] == before
        assert connection.execute('SELECT generation FROM manga_reading_sessions').fetchone()[0] == 7
        assert connection.execute('SELECT note FROM preserved').fetchone()[0] == 'original annotation'
        sources.update_source(connection, source.source_id, role='files')
        suite_modules.set_enabled(connection, 'video', False)
        assert [tuple(row) for row in connection.execute('SELECT * FROM video_category_memberships')] == before
    assert listing(manga) == {'items': [], 'total': 0}
    assert listing(video)['items'] == [saved]


def test_same_name_across_modules_and_creation_order_pagination(local):
    a = create(video, '  휴식  ')
    b = create(manga, '휴식')
    c = create(video, '<script>literal name</script>')
    assert a['id'] != b['id'] and a['name'] == '휴식'
    assert listing(video, 0, 1) == {'items': [a], 'total': 2}
    assert listing(video, 1, 1)['items'] == [c]
    assert listing(video, 20, 1)['items'] == []
    assert listing(manga)['items'] == [b]


def test_lost_response_concurrent_retry_is_exact_and_changed_payload_refuses(local):
    token=uuid4()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:create(video, '  Retry  ', token), range(2)))
    assert results[0] == results[1]
    assert create(video, 'Retry', token) == results[0]
    with pytest.raises(HTTPException) as error:create(video, 'Changed', token)
    assert error.value.status_code == 409
    assert listing(video)['total'] == 1
    assert create(manga, 'Different owner', token)['name'] == 'Different owner'


@pytest.mark.parametrize('first,duplicate', [('Shelf',' shelf '), ('Café','Cafe\u0301'), ('Straße','STRASSE')])
def test_normalized_same_module_duplicates_refuse_without_rows(local,first,duplicate):
    create(video, first)
    with pytest.raises(HTTPException) as error:create(video, duplicate)
    assert error.value.status_code == 409
    assert listing(video)['total'] == 1


@pytest.mark.parametrize('name', [' ', '\u3000', 'Default', ' default ', 'DEFAULT', '字'*101])
def test_invalid_and_reserved_names_do_not_mutate(local,name):
    with pytest.raises(HTTPException) as error:create(video, name)
    assert error.value.status_code == 400
    assert listing(video)['total'] == 0


def test_unicode_limit_counts_characters_and_database_failure_rolls_back(local):
    assert create(manga, '😀'*100)['name'] == '😀'*100
    connect,_,_=local
    with connect() as connection:
        connection.execute("CREATE TRIGGER fail_create BEFORE INSERT ON video_categories BEGIN SELECT RAISE(ABORT,'generated failure'); END")
        connection.commit()
    token=uuid4()
    with pytest.raises(HTTPException) as error:create(video, 'Recover', token)
    assert error.value.status_code == 503 and listing(video)['total'] == 0
    with connect() as connection:
        connection.execute('DROP TRIGGER fail_create');connection.commit()
    assert create(video,'Recover',token)['name'] == 'Recover'


@pytest.mark.parametrize('owner', [video,manga])
def test_real_http_contract_bounds_and_no_store(local,owner):
    status,data,headers=request(owner)
    assert status == 200 and data == {'items': [], 'total': 0} and headers[b'cache-control'] == b'no-store'
    token=str(uuid4())
    status,saved,_=request(owner,'POST',body={'name':'Offline','request_id':token})
    assert status == 200
    assert request(owner,'POST',body={'name':'Offline','request_id':token})[1] == saved
    for query in ('?offset=-1','?limit=0','?limit=241'):
        assert request(owner,query=query)[0] == 422
    assert request(owner,'POST',body={'name':'No token','request_id':'bad'})[0] == 422
    assert request(owner,'POST',body={'name':'','request_id':str(uuid4())})[0] == 422
    assert listing(owner)['total'] == 1


@pytest.mark.parametrize('state,status', [('disabled',409),('absent',409),('failed',503)])
@pytest.mark.parametrize('owner', [video,manga])
def test_unavailable_owner_refuses_before_opening_category_storage(local,monkeypatch,owner,state,status):
    connect,_,_=local
    slug='video' if owner is video else 'manga'
    with connect() as connection:
        if state=='disabled':suite_modules.set_enabled(connection,slug,False)
    if state=='absent':monkeypatch.setattr(suite_modules,'MODULE_REGISTRY',SimpleNamespace(get=lambda _:None))
    if state=='failed':monkeypatch.setattr(lifecycle,'module_runtime',SimpleNamespace(status=lambda _:{'state':'failed'}))
    def forbidden():raise AssertionError('Unavailable module opened category storage')
    monkeypatch.setattr(owner,'get_user_db',forbidden)
    assert request(owner)[0] == status
    assert request(owner,'POST',body={'name':'No change','request_id':str(uuid4())})[0] == status


def test_disable_reenable_does_not_lose_category_or_create_receipt(local):
    connect,_,_=local;token=uuid4();saved=create(video,'Keep',token)
    with connect() as connection:suite_modules.set_enabled(connection,'video',False)
    assert request(video)[0] == 409
    with connect() as connection:
        suite_modules.set_enabled(connection,'video',True)
        suite_modules.MODULE_REGISTRY.require('video').publish(connection)
    assert request(video)[1]['items'] == [saved]
    assert create(video,'Keep',token) == saved


def test_table_names_cannot_be_supplied_by_a_caller():
    with pytest.raises(ValueError):CategoryStore('video; DROP TABLE user_settings')


@pytest.fixture
def browsing(local, tmp_path, monkeypatch):
    import config
    from files_base import index
    from modules.video import router as videos
    from modules.manga import router as mangas
    from PIL import Image
    connect,_,user=local
    media=tmp_path/'videos';media.mkdir()
    for name in ('one.mp4','two.webm','other.mp4'):(media/name).write_bytes(b'original video fixture')
    books=tmp_path/'manga'
    for name in ('Book/Chapter 1/1.png','Other/Chapter 1/1.png','1.png'):
        target=books/name;target.parent.mkdir(parents=True,exist_ok=True);Image.new('RGB',(8,8)).save(target)
    db=tmp_path/'files.sqlite'
    with connect() as connection:
        v=sources.register_source(connection,media,role='video')
        m=sources.register_source(connection,books,role='manga')
        connection.commit()
    with index.open_index(db) as connection:
        index.scan_source(connection,v.source_id,media)
        index.scan_source(connection,m.source_id,books)
    monkeypatch.setattr(config,'FILES_DB_PATH',db)
    monkeypatch.setattr(config,'SUITE_HOME',tmp_path/'private')
    monkeypatch.setattr(videos,'FILES_DB_PATH',db)
    monkeypatch.setattr(videos,'USER_DB_PATH',user)
    monkeypatch.setattr(videos,'get_user_db',connect)
    monkeypatch.setattr(mangas,'get_user_db',connect)
    return local,v,m,media,books,db


def save_membership(owner,item,ids,version=0,token=None):
    return owner.save_membership(CategoryMembershipSave(**item,category_ids=ids,version=version,request_id=token or uuid4()))


@pytest.mark.parametrize('owner,source_slot,path',[(video,1,'one.mp4'),(manga,2,'Book'),(manga,2,'')])
def test_multi_membership_default_clear_history_and_exact_retry(browsing,owner,source_slot,path):
    local=browsing[0];source=browsing[source_slot]
    item={'source_id':source.source_id,'path':path}
    a=create(owner,'A');b=create(owner,'B');token=uuid4()
    first=save_membership(owner,item,[a['id'],b['id'],a['id']],token=token)
    assert first=={'version':1,'category_ids':sorted([a['id'],b['id']])}
    assert save_membership(owner,item,[b['id'],a['id']],token=token)==first
    second=save_membership(owner,item,[b['id']],1)
    with pytest.raises(HTTPException) as error:save_membership(owner,item,[a['id'],b['id']],token=token)
    assert error.value.status_code==409
    assert save_membership(owner,item,[],second['version'])=={'version':3,'category_ids':[]}
    with local[0]() as connection:
        rows=connection.execute(f'SELECT active FROM {owner.store.memberships} WHERE source_id=? AND path=?',(source.source_id,path)).fetchall()
        assert len(rows)==2 and all(row['active']==0 for row in rows)
        assert connection.execute(f'SELECT count(*) FROM {owner.store.updates}').fetchone()[0]==3


def test_video_filtering_counts_pagination_search_and_exact_cross_source_identity(browsing,tmp_path):
    from files_base import index
    from modules.video import router as videos
    local,v,_,media,_,db=browsing
    second=tmp_path/'duplicate';second.mkdir();(second/'one.mp4').write_bytes(b'other original')
    with local[0]() as connection:
        duplicate=sources.register_source(connection,second,role='video');connection.commit()
    with index.open_index(db) as connection:index.scan_source(connection,duplicate.source_id,second)
    a=create(video,'Watching');b=create(video,'Other')
    one={'source_id':v.source_id,'path':'one.mp4'}
    save_membership(video,one,[a['id'],b['id']])
    save_membership(video,{'source_id':v.source_id,'path':'two.webm'},[a['id']])
    assert videos.library('',0,120)['total']==4
    assert videos.library('',0,1,a['id'])['total']==2
    assert [row['name'] for row in videos.library('',1,1,a['id'])['items']]==['two.webm']
    default=videos.library('one',0,120,'default')
    assert default['total']==1 and default['items'][0]['source_id']==duplicate.source_id
    assert videos.library('other',0,120,a['id'])['total']==0
    save_membership(video,one,[b['id']],1)
    assert videos.library('one',0,120,'default')['total']==1
    save_membership(video,one,[],2)
    assert videos.library('one',0,120,'default')['total']==2
    with pytest.raises(HTTPException):videos.library('',0,120,str(uuid4()))
    assert (media/'one.mp4').read_bytes()==b'original video fixture'


def test_manga_filters_series_without_changing_chapters_or_progress(browsing):
    from modules.manga import router as mangas
    local,_,m,_,_,_=browsing
    category=create(manga,'Reading');item={'source_id':m.source_id,'path':'Book'}
    chapters=mangas.chapters(m.source_id,'Book')
    with local[0]() as connection:
        connection.execute("INSERT INTO manga_reading_sessions VALUES(?,'Book/Chapter 1','folder',7)",(m.source_id,));connection.commit()
    save_membership(manga,item,[category['id']])
    assert mangas.library('',0,60,category=category['id'])['total']==1
    assert mangas.library('',0,60,category='default')['total']==2
    assert mangas.library('Other',0,60,category=category['id'])['items']==[]
    assert mangas.chapters(m.source_id,'Book')==chapters
    with local[0]() as connection:assert connection.execute('SELECT generation FROM manga_reading_sessions').fetchone()[0]==7


def test_membership_concurrent_conflict_and_retry_receipts(browsing):
    v=browsing[1];item={'source_id':v.source_id,'path':'one.mp4'};a=create(video,'A');b=create(video,'B')
    def attempt(identity):
        try:return save_membership(video,item,[identity])
        except HTTPException as exc:return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,[a['id'],b['id']]))
    assert sum(isinstance(result,dict) for result in results)==1 and 409 in results
    token=uuid4();other={'source_id':v.source_id,'path':'two.webm'}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:save_membership(video,other,[a['id']],token=token),range(2)))
    assert results[0]==results[1]
    with pytest.raises(HTTPException) as error:save_membership(video,item,[a['id']],token=token)
    assert error.value.status_code==409


def test_failed_membership_update_restores_active_rows_versions_and_receipts(browsing):
    connect=browsing[0][0];item={'source_id':browsing[1].source_id,'path':'one.mp4'}
    a=create(video,'A');b=create(video,'B');save_membership(video,item,[a['id']])
    with connect() as connection:
        connection.execute("CREATE TRIGGER fail_member BEFORE INSERT ON video_category_memberships BEGIN SELECT RAISE(ABORT,'fixture failure'); END");connection.commit()
    token=uuid4()
    with pytest.raises(HTTPException) as error:save_membership(video,item,[b['id']],1,token)
    assert error.value.status_code==503
    with connect() as connection:
        assert video.store.membership(connection,**item)=={'version':1,'category_ids':[a['id']]}
        assert connection.execute('SELECT count(*) FROM video_category_updates').fetchone()[0]==1
        connection.execute('DROP TRIGGER fail_member');connection.commit()
    assert save_membership(video,item,[b['id']],1,token)['version']==2


@pytest.mark.parametrize('owner,slot,path',[(video,1,'one.mp4'),(manga,2,'Book')])
@pytest.mark.parametrize('state',['hidden','role','missing','nested','protected','traversal'])
def test_membership_keeps_source_and_path_guards_without_erasing_saved_state(browsing,monkeypatch,owner,slot,path,state):
    import config
    local=browsing[0];source=browsing[slot];item={'source_id':source.source_id,'path':path}
    category=create(owner,'Keep');save_membership(owner,item,[category['id']])
    target=Path(source.path)/path
    with local[0]() as connection:
        if state=='hidden':sources.update_source(connection,source.source_id,visible=False)
        if state=='role':sources.update_source(connection,source.source_id,role='files')
        if state=='nested':
            nested=target if owner is manga else target.parent
            if owner is video:
                # The exact root is already registered: move the generated video
                # into a nested root and keep its stale parent index fact.
                nested=Path(source.path)/'child';nested.mkdir();target.rename(nested/'one.mp4')
                from files_base import index
                with index.open_index(browsing[-1]) as files:
                    files.execute("UPDATE files_index SET relative_path='child/one.mp4',path=? WHERE source_id=? AND relative_path='one.mp4'",(str(nested/'one.mp4'),source.source_id));files.commit()
                item={**item,'path':'child/one.mp4'}
            sources.register_source(connection,nested,role='files')
        connection.commit()
    if state=='missing':target.rename(target.with_name(target.name+'.preserved'))
    if state=='protected':monkeypatch.setattr(config,'SUITE_HOME',Path(source.path))
    if state=='traversal':item={**item,'path':'../outside'}
    with pytest.raises(HTTPException) as error:save_membership(owner,item,[],1)
    assert error.value.status_code==404
    with local[0]() as connection:
        assert owner.store.membership(connection,source.source_id,path)['category_ids']==[category['id']]


@pytest.mark.parametrize('owner,slot,path',[(video,1,'one.mp4'),(manga,2,'Book')])
def test_membership_http_uuid_bounds_foreign_category_and_unavailable_guards(browsing,monkeypatch,owner,slot,path):
    item={'source_id':browsing[slot].source_id,'path':path}
    payload={**item,'category_ids':[],'version':0,'request_id':str(uuid4())}
    assert request(owner,'POST',body=payload,endpoint='category-membership')[0]==200
    status,data,headers=request(owner,query='?source_id='+item['source_id']+'&path='+path,endpoint='category-membership')
    assert status==200 and data=={'version':0,'category_ids':[]} and headers[b'cache-control']==b'no-store'
    for change in [{'category_ids':['bad']},{'category_ids':[str(uuid4())]*257},{'version':-1},{'request_id':'bad'}]:
        assert request(owner,'POST',body={**payload,**change},endpoint='category-membership')[0]==422
    foreign=create(manga if owner is video else video,'Foreign')
    assert request(owner,'POST',body={**payload,'category_ids':[foreign['id']]},endpoint='category-membership')[0]==404
    with browsing[0][0]() as connection:suite_modules.set_enabled(connection,'video' if owner is video else 'manga',False)
    assert request(owner,'POST',body=payload,endpoint='category-membership')[0]==409


def test_old_category_relationship_migration_is_additive_for_plain_sqlite_connections(tmp_path):
    path=tmp_path/'old.sqlite';identity=str(uuid4());token=str(uuid4())
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE video_categories(id TEXT PRIMARY KEY,request_id TEXT UNIQUE,name TEXT,name_key TEXT UNIQUE,created_at TEXT)')
        connection.execute('CREATE TABLE video_category_memberships(category_id TEXT,source_id TEXT,path TEXT,PRIMARY KEY(category_id,source_id,path))')
        connection.execute("INSERT INTO video_categories VALUES(?,?,'Keep','keep','original time')",(identity,token))
        connection.execute("INSERT INTO video_category_memberships VALUES(?,'missing-source','same/path')",(identity,));connection.commit()
        store=CategoryStore('video');store.publish(connection);store.publish(connection)
        assert connection.execute('SELECT * FROM video_categories').fetchone()==(identity,token,'Keep','keep','original time')
        assert connection.execute('SELECT * FROM video_category_memberships').fetchone()==(identity,'missing-source','same/path',1)

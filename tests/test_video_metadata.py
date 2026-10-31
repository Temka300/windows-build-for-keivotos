"""Video metadata uses generated libraries and preserves all original bytes."""
from pathlib import Path
import json
import sqlite3
from uuid import uuid4
import pytest
from fastapi import HTTPException,Response
from tests.test_video_library import catalog
from modules.video import metadata,router
from modules.video.user_schema import publish
from services import item_catalog,item_manifest as portable


@pytest.fixture
def local(catalog,monkeypatch):
    monkeypatch.setattr(router,'require_enabled',lambda _:None)
    with catalog[2]() as db:publish(db)
    return catalog


def subject(local,path='Series 2',kind='series'):return dict(source_id=local[0].source_id,path=path,kind=kind)
def read(local,path='Series 2',kind='series'):
    response=Response();value=router.video_details(response,**subject(local,path,kind));assert response.headers['cache-control']=='no-store';return value

def refresh(local,path='Series 2',kind='series'):return router.refresh_video_details(router.VideoDetailSubject(**subject(local,path,kind)))
def save(local,fields,state=None,path='Series 2',kind='series',token=None):
    state=state or read(local,path,kind)
    return router.save_video_details(router.VideoDetailUpdate(**subject(local,path,kind),fields=fields,version=state['version'],source_revision=state['source_revision'],request_id=token or uuid4()))


def write_details(local):
    media=local[1]
    (media/'Series 2/details.json').write_text(json.dumps(dict(title='새로운 Show',description='<b>plain</b>',author='Author',genre=['Study'],status='1',unknown='retain original')),encoding='utf-8')
    (media/'Series 2/Season 2/season.nfo').write_text('<season><title>Second season</title><language>ko</language><year>2025</year></season>')
    (media/'Series 2/Season 2/episode 2.nfo').write_text('<episodedetails><title>두 번째</title><tag>한국어</tag><plot>episode summary</plot><watched>true</watched></episodedetails>',encoding='utf-8')


def test_reads_do_not_scan_sources_or_write_catalog_state(local,monkeypatch):
    write_details(local)
    with local[2]() as db:before=db.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall()
    assert read(local)['fields']=={}
    def never(*args,**kwargs):pytest.fail('Browsing opened local metadata')
    monkeypatch.setattr(metadata,'read_local',never)
    router.catalog(Response(),'',0,120,None);router.library('',0,120,None)
    with local[2]() as db:assert before==db.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall()


def test_refresh_scope_precedence_search_and_exact_original_preservation(local):
    write_details(local);media=local[1];before={p.relative_to(media).as_posix():p.read_bytes() for p in media.rglob('*') if p.is_file()}
    result=refresh(local);assert result['fields']['summary']=='<b>plain</b>'
    episode=read(local,'Series 2/Season 2/episode 2.mp4','video')
    assert episode['fields']==dict(summary='episode summary',tags=['한국어'])
    assert 'title' not in read(local,'Series 2/episode 0.mp4','video')['fields']
    assert router.catalog(Response(),'한국어',0,120,None)['total']==2
    assert all('display_title' not in row for row in router.library('한국어',0,120,None)['items'])
    assert router.episodes(Response(),local[0].source_id,'Series 2','series','한국어',0,120,None)['items'][0]['path']=='Series 2/Season 2/episode 2.mp4'
    assert refresh(local)['refresh']['refreshed']==0
    assert before=={p.relative_to(media).as_posix():p.read_bytes() for p in media.rglob('*') if p.is_file()}
    captures=list((Path(router.config.SUITE_HOME)/'preserved').rglob('details.json'));assert captures
    assert captures[0].read_bytes()==(media/'Series 2/details.json').read_bytes()


def test_personal_choices_exact_retry_conflict_inheritance_and_retained_history(local):
    write_details(local);refresh(local);state=read(local);token=uuid4()
    saved=save(local,{'summary':'Personal description','tags':['Personal']},state,token=token)
    assert saved['version']==1 and saved['fields']['summary']=='Personal description'
    assert save(local,{'summary':'Personal description','tags':['Personal']},state,token=token)==saved
    with pytest.raises(HTTPException):save(local,{'summary':'Different'},state,token=token)
    with pytest.raises(HTTPException):save(local,{'summary':'Stale'},state)
    child=read(local,'Series 2/Season 2/episode 2.mp4','video');assert child['fields']['tags']==['Personal']
    save(local,{'tags':None},child,'Series 2/Season 2/episode 2.mp4','video')
    assert 'tags' not in read(local,'Series 2/Season 2/episode 2.mp4','video')['fields']
    restored=save(local,{},saved);assert restored['fields']['summary']=='<b>plain</b>'
    with local[2]() as db:
        assert db.execute('SELECT count(*) FROM video_metadata_updates').fetchone()[0]==3
        assert db.execute('SELECT count(*) FROM kv_item_captures WHERE namespace=?',(metadata.PERSONAL,)).fetchone()[0]==3
    assert json.loads((local[1]/'Series 2/details.json').read_text())['title']=='새로운 Show'


def test_changed_local_details_reject_stale_editor_and_preserve_prior_capture(local):
    write_details(local);refresh(local);old=read(local)
    (local[1]/'Series 2/details.json').write_text('{"description":"Changed"}')
    refresh(local)
    with pytest.raises(HTTPException) as caught:save(local,{'summary':'Stale'},old)
    assert caught.value.status_code==409
    with local[2]() as db:
        assert db.execute('SELECT count(*) FROM video_metadata_updates').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM kv_item_captures WHERE namespace=?',(metadata.SOURCE,)).fetchone()[0]>=4
    assert read(local)['fields']['summary']=='Changed'


def test_invalid_refresh_retains_confirmed_details_and_originals(local):
    write_details(local);refresh(local);old=read(local)
    path=local[1]/'Series 2/details.json';path.write_bytes(b'{bad JSON')
    with pytest.raises(HTTPException):refresh(local)
    assert read(local)==old and path.read_bytes()==b'{bad JSON'


@pytest.mark.parametrize('state',['hidden','role','private','unindexed','outside','nested'])
def test_fresh_sources_and_containment_before_read_or_save(local,monkeypatch,state):
    source,media,connect,index_path=local;path='Series 2';kind='series'
    from files_base import sources
    if state in ('hidden','role','nested'):
        with connect() as db:
            if state=='hidden':sources.update_source(db,source.source_id,visible=False)
            elif state=='role':sources.update_source(db,source.source_id,role='files')
            else:sources.register_source(db,media/'Series 2',role='files')
    elif state=='private':monkeypatch.setattr(router.config,'SUITE_HOME',media)
    elif state=='unindexed':path='Unknown';(media/path).mkdir();(media/path/'episode.mp4').write_bytes(b'unknown')
    else:path='../outside'
    with pytest.raises(HTTPException):read(local,path,kind)



def test_failed_acknowledgement_rolls_back_head_and_keeps_previous_capture(local,monkeypatch):
    write_details(local);refresh(local);old=read(local)
    path=local[1]/'Series 2/details.json';path.write_text('{"description":"New capture"}')
    original=metadata._fresh;calls=0
    def changed(*args):
        nonlocal calls
        calls+=1
        if calls==2:raise HTTPException(409,'Changed during acknowledgement')
        return original(*args)
    monkeypatch.setattr(metadata,'_fresh',changed)
    with pytest.raises(HTTPException):refresh(local)
    assert read(local)==old and path.read_text()=='{"description":"New capture"}'


def test_metadata_refresh_never_changes_watch_revision_or_observed_progress(local):
    from modules.video import progress
    path='Series 2/Season 2/episode 2.mp4'
    state=router.read_watch_position(router.VideoWatchSubject(source_id=local[0].source_id,path=path),Response())
    session=router.begin_watch_session(router.VideoWatchSession(source_id=local[0].source_id,path=path,revision=state['revision'],expected_generation=state['generation']))
    payload=router.VideoWatchUpdate(source_id=local[0].source_id,path=path,**session,sequence=1,position=39,duration=100)
    saved=router.save_watch_position(payload)
    write_details(local);refresh(local);save(local,{'tags':['personal tag']})
    current=router.read_watch_position(router.VideoWatchSubject(source_id=local[0].source_id,path=path),Response())
    assert current['revision']==state['revision'] and current['position']==saved


def test_separate_metadata_importer_repeats_without_rewriting_original_rows(local):
    write_details(local);refresh(local);save(local,{'tags':['retained']})
    with local[2]() as db:
        original=[tuple(row) for row in db.execute('SELECT * FROM video_metadata_overrides')]
        db.execute('BEGIN');assert metadata.import_catalog_records(db)==2;db.commit()
        captures=[tuple(row) for row in db.execute('SELECT * FROM kv_item_captures ORDER BY rowid')]
        db.execute('BEGIN');assert metadata.import_catalog_records(db)==2;db.commit()
        assert captures==[tuple(row) for row in db.execute('SELECT * FROM kv_item_captures ORDER BY rowid')]
        assert original==[tuple(row) for row in db.execute('SELECT * FROM video_metadata_overrides')]



def test_metadata_import_rolls_back_new_anchors_if_later_retained_row_is_invalid():
    db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
    try:
        db.executescript(metadata.SCHEMA);item_catalog.ensure_catalog_schema(db)
        db.execute("INSERT INTO video_metadata_overrides(source_id,path,kind,version,fields_json) VALUES('source','a.mp4','video',1,'{}')")
        db.execute("INSERT INTO video_metadata_overrides(source_id,path,kind,version,fields_json) VALUES('source','b.mp4','invalid',1,'{}')");db.commit();db.execute('BEGIN')
        with pytest.raises(ValueError):metadata.import_catalog_records(db)
        assert db.execute('SELECT count(*) FROM kv_items').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM video_metadata_overrides').fetchone()[0]==2
    finally:db.close()


@pytest.mark.parametrize('subject_value',[
    {'path':['bad'],'kind':'video','facts':{}},
    {'path':'../outside.mp4','kind':'video','facts':{}},
    {'path':'unrelated.mp4','kind':'video','facts':{'summary':'Unrelated'}},
    {'path':'video.mp4','kind':'unknown','facts':{}},
    {'path':'video.mp4','kind':'video','facts':{'tags':'invalid'}},
])
def test_native_subjects_reject_invalid_or_unrelated_facts(subject_value):
    value=portable.new(item_id='item',owner='video',item_type='video',library_id='library',assets=[portable.asset('video.mp4')],extensions={'video':{'metadata':{'version':1,'subjects':[subject_value]}}})
    with pytest.raises(ValueError):metadata.native_entries(value)


def test_native_duplicate_subjects_are_not_silently_overwritten():
    entry=dict(path='video.mp4',kind='video',facts={'summary':'First'})
    value=portable.new(item_id='item',owner='video',item_type='video',library_id='library',assets=[portable.asset('video.mp4')],extensions={'video':{'metadata':{'version':1,'subjects':[entry,{**entry,'facts':{'summary':'Second'}}]}}})
    with pytest.raises(ValueError,match='Duplicate'):metadata.native_entries(value)



def test_unused_retained_personal_fields_are_preserved_without_display_or_search(local):
    write_details(local);refresh(local)
    with local[2]() as db:
        db.execute("INSERT INTO video_metadata_overrides(source_id,path,kind,version,fields_json) VALUES(?,?,?,?,?)",(local[0].source_id,'Series 2','series',1,json.dumps({'title':'Retained unsupported title','summary':'Old description'})));db.commit()
    before=read(local);assert before['overrides']=={'summary':'Old description'}
    assert router.library('Retained unsupported title',0,120,None)['total']==0
    save(local,{'tags':['new personal tag']},before)
    with local[2]() as db:stored=json.loads(db.execute('SELECT fields_json FROM video_metadata_overrides').fetchone()[0])
    assert stored['title']=='Retained unsupported title' and stored['tags']==['new personal tag']
    assert set(read(local)['fields'])<=set(('tags','summary'))



def test_two_field_routes_validate_http_requests_and_disabled_owner_before_storage(local,monkeypatch):
    import asyncio
    from urllib.parse import urlencode
    from fastapi import FastAPI
    app=FastAPI();app.include_router(router.router)
    def request(path,body=None,query=None):
        async def run():
            messages=[]
            async def receive():return {'type':'http.request','body':json.dumps(body).encode(),'more_body':False}
            async def send(value):messages.append(value)
            await app({'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'GET' if query is not None else 'POST','scheme':'http',
                'path':'/api/video/metadata'+path,'raw_path':('/api/video/metadata'+path).encode(),'query_string':urlencode(query or {}).encode(),
                'root_path':'','headers':[(b'content-type',b'application/json')],'client':('127.0.0.1',1),'server':('test',80)},receive,send)
            start=next(value for value in messages if value['type']=='http.response.start')
            raw=b''.join(value.get('body',b'') for value in messages if value['type']=='http.response.body')
            return start,json.loads(raw)
        return asyncio.run(run())
    target=subject(local);start,state=request('',query=target)
    assert start['status']==200 and (b'cache-control',b'no-store') in start['headers']
    body=dict(**target,fields={'summary':'Saved description','tags':['HTTP tag']},version=0,source_revision=state['source_revision'],request_id=str(uuid4()))
    assert request('/overrides',{**body,'fields':{'title':'Unsupported'}})[0]['status']==422
    assert request('/overrides',{**body,'fields':{'summary':'a','tags':[],'extra':'b'}})[0]['status']==422
    assert request('/overrides',{**body,'request_id':'not-a-uuid'})[0]['status']==422
    assert request('',query={**target,'kind':'unknown'})[0]['status']==422
    assert request('',query={**target,'path':'../outside'})[0]['status']==404
    start,saved=request('/overrides',body);assert start['status']==200 and saved['fields']==body['fields']
    assert request('/overrides',body)[1]==saved
    def disabled(_):raise RuntimeError('Video disabled')
    monkeypatch.setattr(router,'require_enabled',disabled)
    monkeypatch.setattr(router,'get_user_db',lambda:pytest.fail('Disabled metadata operation opened storage'))
    assert request('',query=target)[0]['status']==409
    assert request('/refresh',target)[0]['status']==409
    assert request('/overrides',body)[0]['status']==409

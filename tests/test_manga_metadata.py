"""Manga facts use generated local metadata; no real profile, media or network."""
from pathlib import Path
from contextlib import closing
import copy,hashlib,json,sqlite3,zipfile
from uuid import uuid4
import pytest
from fastapi import FastAPI,HTTPException,Response
from tests.test_manga_module import library,picture
from modules import manga
from modules.manga import metadata as engine,router,library as chapters,user_schema
from services import item_catalog,item_manifest as portable
from files_base import index,sources


@pytest.fixture
def local(library,monkeypatch):
    source,connect,media,db=library;manga.adopt(source.source_id)
    with connect() as connection:
        user_schema.publish(connection);item_catalog.ensure_catalog_schema(connection)
    monkeypatch.setattr(router,'require_enabled',lambda _:None)
    import suite_modules
    monkeypatch.setattr(suite_modules,'require_enabled',lambda _:None)
    return source,connect,media,db


def read(local,path='Story'):return router.manga_details(Response(),local[0].source_id,path)
def refresh(local,path='Story'):return router.refresh_manga_details(router.MangaDetailSubject(source_id=local[0].source_id,path=path))
def save(local,fields,state=None,path='Story',token=None):
    state=state or read(local,path)
    return router.save_manga_details(router.MangaDetailUpdate(source_id=local[0].source_id,path=path,fields=fields,version=state['version'],source_revision=state['source_revision'],request_id=token or uuid4()))
def tree(root):return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
def xml(summary='원래 설명',tags='이야기, Slice of life'):
    return ('<ComicInfo><Series>Ignored title</Series><Writer>Ignored author</Writer><Summary>'+summary+'</Summary><Genre>'+tags+'</Genre><Future><Unknown>exact</Unknown></Future></ComicInfo>').encode()
def source_xml(local,raw=None):
    p=local[2]/'Story/ComicInfo.xml';p.write_bytes(raw or xml());return p


def test_read_and_search_use_saved_facts_without_metadata_io_or_writes(local,monkeypatch):
    source_xml(local);assert read(local)['fields']=={}
    assert router.library('이야기',0,60)['total']==0
    refresh(local)
    with local[1]() as db:before=db.execute('SELECT COUNT(*) FROM kv_item_captures').fetchone()[0]
    monkeypatch.setattr(engine,'read_local',lambda *a,**k:pytest.fail('browse parsed source metadata'))
    assert read(local)['fields']==dict(summary='원래 설명',tags=['이야기','Slice of life'])
    result=router.library('slice of life',0,60);assert result['total']==1 and result['items'][0]['name']=='Story'
    assert router.library('Ignored title',0,60)['total']==0
    assert router.library('원래',1,1)['items']==[]
    with local[1]() as db:assert db.execute('SELECT COUNT(*) FROM kv_item_captures').fetchone()[0]==before


def test_source_precedence_exact_raw_history_and_explicit_removal(local):
    media=local[2];details=media/'Story/details.json';details.write_text(json.dumps(dict(description='JSON description',genre=['json'],unknown=['preserve'])))
    source=source_xml(local);before=tree(media);state=refresh(local)
    assert state['fields']['summary']=='원래 설명' and 'json' not in state['fields']['tags']
    assert refresh(local)['refresh']['refreshed']==0
    source.write_bytes(xml('새 설명','new'));refresh(local)
    stored=list(router.config.SUITE_HOME.rglob('ComicInfo.xml'));assert {p.read_bytes() for p in stored}=={xml(),xml('새 설명','new')}
    source.unlink();assert refresh(local)['fields']==dict(summary='JSON description',tags=['json'])
    details.unlink();assert refresh(local)['fields']=={}
    with local[1]() as db:assert db.execute('SELECT COUNT(*) FROM kv_item_captures WHERE namespace=?',(engine.SOURCE,)).fetchone()[0]==4
    assert all((media/name).read_bytes()==raw for name,raw in before.items() if name not in ('Story/ComicInfo.xml','Story/details.json'))


@pytest.mark.parametrize('kind',['archive','folder'])
def test_first_natural_chapter_metadata_fallback_is_bounded_and_never_rewrites_media(local,kind):
    media=local[2]
    if kind=='archive':
        path=media/'Story/Chapter 10.cbz'
        with zipfile.ZipFile(path,'w') as archive:
            archive.writestr('1.png',picture());archive.writestr('ComicInfo.xml',xml())
    else:(media/'Story/Chapter 2/ComicInfo.xml').write_bytes(xml())
    before=tree(media);state=refresh(local);assert state['fields']['tags']==['이야기','Slice of life']
    assert tree(media)==before
    retained=[p for p in router.config.SUITE_HOME.rglob('*') if p.is_file()]
    assert any(p.name=='ComicInfo.xml' and p.read_bytes()==xml() for p in retained)
    assert not any(p.suffix=='.cbz' for p in retained)


@pytest.mark.parametrize('raw',[b'<ComicInfo><Summary>',b'<!DOCTYPE x [<!ENTITY z "bad">]><ComicInfo><Summary>&z;</Summary></ComicInfo>',b'<ComicInfo>'+b'<X>'*66+b'</X>'*66+b'</ComicInfo>',b' '*(192*1024+1)],ids=['malformed','entity','depth','oversize'])
def test_invalid_refresh_preserves_confirmed_facts_and_original_bytes(local,raw):
    p=source_xml(local);old=refresh(local);p.write_bytes(raw)
    with pytest.raises(HTTPException) as error:refresh(local)
    assert error.value.status_code==409 and read(local)['fields']==old['fields'] and p.read_bytes()==raw


def test_personal_choices_conflict_retry_clear_inherit_and_source_priority(local):
    source_xml(local);original=refresh(local);token=uuid4()
    chosen=save(local,dict(tags=['personal'],summary='my description'),original,token=token)
    assert chosen['fields']==dict(tags=['personal'],summary='my description')
    assert save(local,dict(tags=['personal'],summary='my description'),original,token=token)==chosen
    with pytest.raises(HTTPException) as conflict:save(local,dict(tags=['other']),original)
    assert conflict.value.status_code==409
    with pytest.raises(HTTPException):save(local,dict(tags=['changed']),original,token=token)
    assert save(local,dict(tags=[],summary=None))['fields']=={'tags':[]}
    assert save(local,{})['fields']==original['fields']
    source_xml(local,xml('new source','new'));refresh(local)
    with pytest.raises(HTTPException):save(local,dict(summary='stale'),original)
    with pytest.raises(HTTPException):save(local,dict(title='No rename'))
    assert chapters.catalog(local[3],[local[0]])[0]['name']=='Story'


def test_failed_head_acknowledgement_rolls_back_and_retains_previous_capture(local,monkeypatch):
    source_xml(local);old=refresh(local);source_xml(local,xml('changed','changed'))
    original=item_catalog.record_capture
    def fail(db,**kwargs):
        if kwargs['namespace']==engine.HEAD:raise RuntimeError('interrupted acknowledgement')
        return original(db,**kwargs)
    monkeypatch.setattr(item_catalog,'record_capture',fail)
    with pytest.raises(RuntimeError):refresh(local)
    assert read(local)['fields']==old['fields']


@pytest.mark.parametrize('state',['hidden','role','nested','linked'])
def test_current_ownership_and_path_guards_precede_read_refresh_and_save(local,state):
    source_xml(local);old=refresh(local)
    with local[1]() as db:
        if state=='hidden':sources.update_source(db,local[0].source_id,visible=False)
        elif state=='role':sources.update_source(db,local[0].source_id,role='files')
        elif state=='nested':sources.register_source(db,local[2]/'Story',role='files')
        else:
            p=local[2]/'Story/ComicInfo.xml';p.unlink()
            try:p.symlink_to(local[2]/'Story/Chapter 2/1.png')
            except OSError as error:
                if getattr(error,'winerror',None)==1314:pytest.skip('Windows symbolic-link privilege unavailable; covered on Linux')
                raise
    operations=[lambda:refresh(local)] if state=='linked' else [lambda:read(local),lambda:refresh(local),lambda:save(local,{'tags':['unsafe']},old)]
    for operation in operations:
        with pytest.raises((HTTPException,ValueError)):operation()


def test_root_series_metadata_and_unchanged_rescan_hook(local):
    media=local[2];(media/'root.cbz').write_bytes((media/'Story/Chapter 10.cbz').read_bytes());(media/'details.json').write_text('{"description":"root description","genre":["root"]}')
    with index.open_index(local[3]) as db:index.scan_source(db,local[0].source_id,media)
    assert refresh(local,'')['fields']==dict(summary='root description',tags=['root'])
    assert engine.after_rescan(local[0].source_id)['manga_metadata']['subjects']==2
    assert router.library('root description',0,60)['total']==1


def test_metadata_importer_is_independent_repeatable_and_retains_original_rows(local):
    source_xml(local);refresh(local);save(local,{'tags':['personal']})
    with local[1]() as db:
        before=[tuple(row) for row in db.execute('SELECT * FROM manga_metadata_overrides')]
        db.execute('BEGIN');assert engine.import_catalog_records(db)==2;assert engine.import_catalog_records(db)==2;db.commit()
        assert [tuple(row) for row in db.execute('SELECT * FROM manga_metadata_overrides')]==before


def test_three_http_routes_validate_input_and_block_disabled_owner(local,monkeypatch):
    import asyncio
    from urllib.parse import urlencode
    app=FastAPI();app.include_router(router.router)
    def request(path,body=None,query=None):
        async def run():
            messages=[]
            async def receive():return {'type':'http.request','body':json.dumps(body).encode(),'more_body':False}
            async def send(value):messages.append(value)
            route='/api/manga/metadata'+path
            await app({'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'GET' if query is not None else 'POST','scheme':'http','path':route,'raw_path':route.encode(),'query_string':urlencode(query or {}).encode(),'root_path':'','headers':[(b'content-type',b'application/json')],'client':('127.0.0.1',1),'server':('test',80)},receive,send)
            start=next(value for value in messages if value['type']=='http.response.start')
            raw=b''.join(value.get('body',b'') for value in messages if value['type']=='http.response.body')
            return start,json.loads(raw)
        return asyncio.run(run())
    target=dict(source_id=local[0].source_id,path='Story');start,state=request('',query=target)
    assert start['status']==200 and (b'cache-control',b'no-store') in start['headers']
    assert request('/refresh',target)[0]['status']==200
    body=dict(**target,fields={'summary':'personal'},version=state['version'],source_revision=state['source_revision'],request_id=str(uuid4()))
    assert request('/overrides',{**body,'fields':{'title':'unsupported'}})[0]['status']==422
    assert request('/overrides',{**body,'request_id':'invalid'})[0]['status']==422
    assert request('',query={**target,'path':'../outside'})[0]['status']==404
    assert request('/overrides',body)[0]['status']==200
    monkeypatch.setattr(router,'require_enabled',lambda _:(_ for _ in ()).throw(RuntimeError('disabled')))
    monkeypatch.setattr(router,'get_user_db',lambda:pytest.fail('disabled metadata opened storage'))
    assert request('',query=target)[0]['status']==409
    assert request('/refresh',target)[0]['status']==409
    assert request('/overrides',body)[0]['status']==409


@pytest.mark.parametrize('case',['oversize','duplicate','unsafe','malformed'])
def test_archive_metadata_limits_retain_saved_facts_without_extraction(local,case,monkeypatch):
    source=source_xml(local);old=refresh(local);source.unlink();archive_path=local[2]/'Story/Chapter 10.cbz'
    with zipfile.ZipFile(archive_path,'w',compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('1.png',picture())
        if case=='oversize':archive.writestr('ComicInfo.xml',b'x'*(192*1024+1))
        elif case=='duplicate':archive.writestr('ComicInfo.xml',xml());archive.writestr('comicinfo.xml',xml())
        elif case=='unsafe':archive.writestr('../ComicInfo.xml',xml())
        else:archive.writestr('ComicInfo.xml',b'<ComicInfo><Summary>')
    before=archive_path.read_bytes()
    original=zipfile.ZipFile.open
    def bounded(self,name,*args,**kwargs):
        entry=self.getinfo(name) if isinstance(name,str) else name
        if case=='oversize' and entry.filename=='ComicInfo.xml':pytest.fail('Oversized metadata body was opened')
        return original(self,name,*args,**kwargs)
    monkeypatch.setattr(zipfile.ZipFile,'open',bounded)
    with pytest.raises(HTTPException):refresh(local)
    assert read(local)['fields']==old['fields'] and archive_path.read_bytes()==before


def test_metadata_refresh_retains_actual_observed_reading_position_and_revision(local):
    source=local[0];path='Story/Chapter 2';kind='folder'
    facts=chapters.chapter_metadata(source,path,kind,local[3],[router.config.SUITE_HOME])
    target=dict(source_id=source.source_id,series_path='Story',path=path,kind=kind,revision=facts['revision'])
    session=router.begin_progress(router.ProgressSession(**target,expected_generation=0))
    router.save_progress(router.ProgressUpdate(**target,generation=session['generation'],sequence=1,page=1,mode='paged',anchor=.5))
    before=router.read_progress(router.ProgressChapter(**target))
    source_xml(local);refresh(local);source_xml(local,xml('new description','new tag'));refresh(local)
    assert router.read_progress(router.ProgressChapter(**target))==before
    assert chapters.chapter_metadata(source,path,kind,local[3],[router.config.SUITE_HOME])['revision']==facts['revision']


def test_input_change_during_refresh_refuses_acknowledgement_and_retains_old_facts(local,monkeypatch):
    source_xml(local);before=refresh(local);original=engine.read_local;calls=[]
    source_xml(local,xml('candidate','candidate'))
    def changing(*args,**kwargs):
        value=original(*args,**kwargs);calls.append(True)
        if len(calls)==1:source_xml(local,xml('changed during read','changed'))
        return value
    monkeypatch.setattr(engine,'read_local',changing)
    with pytest.raises(HTTPException):refresh(local)
    assert read(local)['fields']==before['fields']

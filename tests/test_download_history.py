"""History facts and passive reads on isolated SQLite stores only."""
import base64
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from services import operations, download_history as history
from module_registry import ModuleRegistry
from modules.files import descriptor


def registry(tmp_path):
    return ModuleRegistry((descriptor(tmp_path,'test'),))


def request(db,owner='youtube',job='job'):
    operations.ensure_schema(db)
    operations.request(db,owner,job,kind='download',key=job,payload={'fixture':True})
    return operations.claim(db,owner,job)


def record(db,owner='youtube',job='job',**changes):
    return history.record(db,owner,job,**(dict(file_key='media',name='雪.mp4',module_name='YouTube')|changes))


def page(tmp_path,**changes):
    return history.page(**(dict(jobs_path=tmp_path/'jobs.sqlite',user_path=tmp_path/'user.sqlite',
        registry=registry(tmp_path))|changes))


def test_missing_history_read_creates_no_profile_or_store(tmp_path):
    root=tmp_path/'missing'
    assert page(root)=={'items':[],'next_cursor':None}
    assert not root.exists()


@pytest.mark.parametrize('value', ['2026-01-02','2026-01-02T03:00:00','nonsense',None,True,'2026-02-30T00:00:00Z'])
def test_unknown_or_naive_date_never_gets_a_guessed_timezone(value):
    assert history.timestamp(value) is None


def test_midnight_offsets_preserve_instants():
    assert history.timestamp('2027-01-16T00:00:01+09:00')=='2027-01-15T15:00:01.000000Z'
    assert history.timestamp('2027-01-15T23:59:59-08:00')=='2027-01-16T07:59:59.000000Z'


def test_progress_persistence_and_second_process_read(tmp_path):
    path=tmp_path/'jobs.sqlite'
    with sqlite3.connect(path) as db:
        token=request(db)
        with operations.bind('youtube','job',token):
            item=record(db,at='2027-01-16T00:00:01+09:00',progress=20)
            record(db,at='2027-01-16T00:00:02+09:00',progress=10)
            record(db,at='2027-01-16T00:00:03+09:00',completed=True)
            record(db,at='2028-01-01T00:00:00Z',completed=True)
            operations.transition(db,'youtube','job',state='completed')
    selected=page(tmp_path)['items']
    assert len(selected)==1 and selected[0]['id']==item and selected[0]['progress']==100
    assert selected[0]['completed_at']=='2027-01-15T15:00:03.000000Z'
    script="""import json,sys
sys.path.insert(0,sys.argv[1])
from services.download_history import page
from module_registry import ModuleRegistry
from modules.files import descriptor
from pathlib import Path
p=Path(sys.argv[2])
print(json.dumps(page(jobs_path=p/'jobs.sqlite',user_path=p/'user.sqlite',
 registry=ModuleRegistry((descriptor(p,'test'),)))))"""
    output=subprocess.check_output([sys.executable,'-I','-c',script,str(Path(history.__file__).parents[1]),str(tmp_path)])
    assert json.loads(output)['items']==selected
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT started_at,ended_at FROM kv_download_attempts').fetchone()==(
            '2027-01-15T15:00:01.000000Z','2027-01-15T15:00:03.000000Z')


@pytest.mark.parametrize('terminal',['failed','cancelled','interrupted','completed-with-warnings'])
def test_partial_multifile_completion_and_restart_do_not_erase_siblings(tmp_path,terminal):
    with sqlite3.connect(tmp_path/'jobs.sqlite') as db:
        token=request(db,'twitter')
        with operations.bind('twitter','job',token):
            record(db,'twitter',file_key='photo-1.jpg',completed=True)
            record(db,'twitter',file_key='photo-2.jpg',progress=42)
            operations.transition(db,'twitter','job',state=terminal)
    rows={r['id']:r for r in page(tmp_path)['items']}
    complete=rows[history.identity('twitter','job','photo-1.jpg')]
    incomplete=rows[history.identity('twitter','job','photo-2.jpg')]
    assert complete['state']=='completed' and complete['progress']==100
    assert incomplete['state']==('interrupted' if terminal=='completed-with-warnings' else terminal)
    assert incomplete['progress']==42 and incomplete['completed_at'] is None


def test_retry_links_one_item_and_old_attempt_cannot_change_it(tmp_path):
    with sqlite3.connect(tmp_path/'jobs.sqlite') as db:
        old=request(db)
        with operations.bind('youtube','job',old):
            first=record(db,progress=30)
            operations.transition(db,'youtube','job',state='failed')
        new=request(db,job='replacement')
        history.link(db,'youtube','replacement',previous='job')
        with operations.bind('youtube','replacement',new):
            assert record(db,job='replacement',progress=5)==first
        with operations.bind('youtube','job',old),pytest.raises(operations.StaleAttempt):
            record(db,progress=99)
        with operations.bind('youtube','replacement',new):
            record(db,job='replacement',completed=True)
    assert len(page(tmp_path)['items'])==1
    with sqlite3.connect(tmp_path/'jobs.sqlite') as db:
        assert db.execute('SELECT count(*) FROM kv_download_attempts').fetchone()[0]==2


@pytest.mark.parametrize('value',[float('nan'),float('inf'),-1,101,True,'20'])
def test_invalid_progress_refuses_without_mutating_history(tmp_path,value):
    with sqlite3.connect(tmp_path/'jobs.sqlite') as db:
        request(db)
        with pytest.raises(ValueError):record(db,progress=value)
        assert db.execute('SELECT count(*) FROM kv_download_items').fetchone()[0]==0


def test_owner_write_and_history_rollback_together(tmp_path):
    with sqlite3.connect(tmp_path/'jobs.sqlite') as db:
        request(db)
        db.execute('BEGIN')
        record(db,progress=70)
        db.rollback()
        assert not page(tmp_path)['items']


def test_unknown_progress_and_completed_only_after_verification(tmp_path):
    with sqlite3.connect(tmp_path/'jobs.sqlite') as db:
        request(db)
        record(db,progress=None)
        assert page(tmp_path)['items'][0]['progress'] is None
        record(db,progress=100)
        assert page(tmp_path)['items'][0]['progress']==99
        assert page(tmp_path)['items'][0]['state']=='working'


def test_read_is_passive_and_keeps_absent_owner_facts(tmp_path):
    path=tmp_path/'jobs.sqlite'
    with sqlite3.connect(path) as db:
        request(db,'removed-owner')
        record(db,'removed-owner',module_name='Removed module',completed=True)
    before=path.read_bytes()
    result=page(tmp_path)
    assert result['items'][0]['owner']=='removed-owner'
    assert result['items'][0]['module_name']=='Removed module'
    assert path.read_bytes()==before
    assert sorted(p.name for p in tmp_path.iterdir())==['jobs.sqlite']


def test_paging_with_equal_dates_and_owners_is_complete_and_stable(tmp_path):
    with sqlite3.connect(tmp_path/'jobs.sqlite') as db:
        for owner in ('youtube','twitter'):
            request(db,owner)
            for i in range(35):record(db,owner,file_key=str(i),name=str(i),completed=True,at='2027-01-16T00:00:00Z')
    ids=[];cursor=None
    while True:
        result=page(tmp_path,limit=9,after=cursor)
        ids.extend(r['id'] for r in result['items']);cursor=result['next_cursor']
        if not cursor:break
    assert len(ids)==len(set(ids))==70
    assert len(page(tmp_path,owner='twitter',limit=100)['items'])==35
    with pytest.raises(ValueError):page(tmp_path,owner='twitter',after=page(tmp_path,limit=2)['next_cursor'])


@pytest.mark.parametrize('cursor',['bad!',base64.urlsafe_b64encode(b'{}').decode(),base64.urlsafe_b64encode(b'[true,null,"","id"]').decode()])
def test_cursor_refuses_wrong_shape_or_scope(tmp_path,cursor):
    with pytest.raises(ValueError):page(tmp_path,after=cursor)


def test_schema_migration_additive_and_unknown_version_refuses(tmp_path):
    path=tmp_path/'jobs.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE preserved(value BLOB)');db.execute("INSERT INTO preserved VALUES(x'00ff')");db.commit()
        operations.ensure_schema(db)
        assert db.execute('SELECT value FROM preserved').fetchone()[0]==b'\0\xff'
        db.execute('UPDATE kv_download_schema SET version=99');db.commit()
        with pytest.raises(ValueError):history.ensure_schema(db)
    before=path.read_bytes()
    with pytest.raises(ValueError):page(tmp_path)
    assert path.read_bytes()==before


def test_restore_gate_waits_for_passive_view_then_releases(tmp_path):
    from database_connections import read_existing_databases,exclusive_database_access
    entered=threading.Event();finished=threading.Event()
    def restore():
        entered.set()
        with exclusive_database_access():finished.set()
    with read_existing_databases([tmp_path/'missing']*3):
        worker=threading.Thread(target=restore);worker.start();entered.wait(2)
        assert not finished.wait(.03)
    worker.join(2);assert finished.is_set()


def test_large_history_is_bounded_and_uses_no_media_observations(tmp_path):
    with sqlite3.connect(tmp_path/'jobs.sqlite') as db:
        request(db)
        db.executemany('INSERT INTO kv_download_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            [(f'id-{i:05}','youtube','YouTube','job',str(i),str(i),'job',1,'completed',100,
              '2027-01-16T00:00:00.000000Z','2027-01-16T01:00:00.000000Z') for i in range(10000)])
        db.commit()
    start=time.monotonic();first=page(tmp_path,limit=200)
    assert len(first['items'])==200 and first['next_cursor']
    assert time.monotonic()-start<5
    second=page(tmp_path,limit=200,after=first['next_cursor'])
    assert not {r['id'] for r in first['items']} & {r['id'] for r in second['items']}


def test_api_is_read_only_and_failure_does_not_create_files(tmp_path,monkeypatch):
    from fastapi import FastAPI
    import asyncio
    from routers import downloads
    for name,file in [('JOBS_DB_PATH','jobs.sqlite'),('USER_DB_PATH','user.sqlite'),('INDEX_DB_PATH','index.sqlite')]:
        monkeypatch.setattr(downloads.config,name,tmp_path/file)
    monkeypatch.setattr(downloads.config,'MODULE_REGISTRY',registry(tmp_path))
    app=FastAPI();app.include_router(downloads.router)
    async def call(method,path):
        raw,_,query=path.partition('?')
        messages=[]
        async def receive():return {'type':'http.request','body':b'','more_body':False}
        async def send(message):messages.append(message)
        await app({'type':'http','asgi':{'version':'3.0'},'http_version':'1.1',
            'method':method,'scheme':'http','path':raw,'raw_path':raw.encode(),
            'query_string':query.encode(),'headers':[],'server':('localhost',80),
            'client':('127.0.0.1',1234),'root_path':''},receive,send)
        return messages[0]['status'],b''.join(m.get('body',b'') for m in messages[1:])
    status,body=asyncio.run(call('GET','/api/downloads'))
    assert status==200 and json.loads(body)=={'items':[],'next_cursor':None}
    assert asyncio.run(call('GET','/api/downloads?limit=201'))[0]==422
    assert asyncio.run(call('GET','/api/downloads?after=bad!'))[0]==400
    assert asyncio.run(call('POST','/api/downloads'))[0]==405
    assert not list(tmp_path.iterdir())


def test_physically_absent_owners_keep_recorded_history_without_worker_import(tmp_path):
    import shutil
    backend=Path(history.__file__).parents[1]
    copied=tmp_path/'source/backend'
    shutil.copytree(backend,copied,ignore=shutil.ignore_patterns('__pycache__','danbooru','youtube','twitter'))
    with sqlite3.connect(tmp_path/'jobs.sqlite') as db:
        request(db,'youtube');record(db,completed=True)
    script="""import sys,json
sys.path.insert(0,sys.argv[1])
from module_registry import build_registry
from services.download_history import page
from pathlib import Path
p=Path(sys.argv[2]);r=build_registry(p,'test')
assert all(r.get(o) is None for o in ('danbooru','youtube','twitter'))
result=page(jobs_path=p/'jobs.sqlite',user_path=p/'user.sqlite',registry=r)
assert len(result['items'])==1
assert not any(n.startswith(('modules.youtube','modules.twitter','modules.danbooru')) for n in sys.modules)
print(json.dumps(result))"""
    result=subprocess.check_output([sys.executable,'-I','-c',script,str(copied),str(tmp_path)])
    assert json.loads(result)['items'][0]['owner']=='youtube'


def test_completed_sibling_retains_each_real_retry_attempt_without_redating(tmp_path):
    with sqlite3.connect(tmp_path/'jobs.sqlite') as db:
        first=request(db,'twitter')
        with operations.bind('twitter','job',first):
            record(db,'twitter',file_key='photo',progress=10,at='2027-01-16T00:00:00Z')
            item=record(db,'twitter',file_key='photo',completed=True,at='2027-01-16T00:00:01Z')
            operations.transition(db,'twitter','job',state='failed')
        original=page(tmp_path)['items']
        operations.retry(db,'twitter','job','retry')
        second=operations.claim(db,'twitter','job')
        with operations.bind('twitter','job',second):
            record(db,'twitter',file_key='photo',progress=5,at='2027-01-17T00:00:00Z')
            record(db,'twitter',file_key='photo',completed=True,at='2027-01-17T00:00:02Z')
            operations.transition(db,'twitter','job',state='completed')
        attempts=db.execute('SELECT started_at,ended_at FROM kv_download_attempts WHERE item_id=? ORDER BY attempt',(item,)).fetchall()
    assert page(tmp_path)['items']==original
    assert attempts==[('2027-01-16T00:00:00.000000Z','2027-01-16T00:00:01.000000Z'),
                      ('2027-01-17T00:00:00.000000Z','2027-01-17T00:00:02.000000Z')]


def test_completion_without_a_start_event_keeps_start_unknown(tmp_path):
    with sqlite3.connect(tmp_path/'jobs.sqlite') as db:
        request(db);record(db,completed=True,at='2027-01-16T00:00:00Z')
    item=page(tmp_path)['items'][0]
    assert item['started_at'] is None and item['completed_at']=='2027-01-16T00:00:00.000000Z'


def test_owner_presentation_can_change_its_title_but_not_download_facts(tmp_path):
    with sqlite3.connect(tmp_path/'jobs.sqlite') as db:
        request(db,'files');record(db,'files',name='video.webm',completed=True,at='2027-01-16T00:00:00Z')
    original=page(tmp_path)['items'][0]
    def provider(context):
        assert len(context.current)==1
        context.current[0]['progress']=0
        context.current[0]['completed_at']='2099-01-01T00:00:00Z'
        return [{**context.current[0],'name':'Saved title'}]
    selected=registry(tmp_path).replacing(replace(descriptor(tmp_path,'test'),download_history_provider=provider))
    row=page(tmp_path,registry=selected)['items'][0]
    assert row=={**original,'name':'Saved title'}


def test_presentation_cannot_change_another_owners_title(tmp_path):
    with sqlite3.connect(tmp_path/'jobs.sqlite') as db:
        request(db);record(db,name='Saved title',completed=True)
    original=page(tmp_path)
    selected=registry(tmp_path).replacing(replace(descriptor(tmp_path,'test'),
        download_history_provider=lambda context:[{**context.current[0],'name':'Wrong owner'}]))
    assert page(tmp_path,registry=selected)==original

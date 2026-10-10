"""Generated personal vocabulary only: actual ASGI requests and isolated SQLite."""
from contextlib import closing, contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4
import asyncio
import json
import sqlite3
import subprocess
import sys
import unicodedata

import pytest
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from modules.language import router, user_schema, vocabulary


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / 'user.sqlite'
    @contextmanager
    def connect():
        with closing(sqlite3.connect(path, timeout=0.05)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA journal_mode=WAL')
            yield connection
    with connect() as connection:
        connection.execute('CREATE TABLE precious(value TEXT)')
        connection.execute("INSERT INTO precious VALUES ('original personal data')")
        connection.commit()
        user_schema.publish(connection)
    monkeypatch.setattr(router, 'get_user_db', connect)
    monkeypatch.setattr(router, 'require_enabled', lambda slug: None)
    return path, connect


def payload(**changes):
    return {'request_id': str(uuid4()), 'korean':'안녕', 'pronunciation':'annyeong', 'english':'hello', **changes}


def request(method='GET', data=None, query='', *, drop_reply=False):
    app = FastAPI()
    app.include_router(router.router)
    async def run():
        messages = []
        async def receive():
            return {'type':'http.request','body':json.dumps(data).encode(),'more_body':False}
        async def send(message):
            messages.append(message)
            if drop_reply and message['type']=='http.response.body':
                raise ConnectionError('Generated loss of committed response')
        path='/api/language/vocabulary'
        await app({'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':method,
                   'scheme':'http','path':path,'raw_path':path.encode(),'root_path':'',
                   'query_string':query.encode(),'headers':[(b'content-type',b'application/json')],
                   'client':('127.0.0.1',1),'server':('127.0.0.1',80)},receive,send)
        start=next(m for m in messages if m['type']=='http.response.start')
        body=b''.join(m.get('body',b'') for m in messages if m['type']=='http.response.body')
        return start['status'],json.loads(body),dict(start['headers'])
    return asyncio.run(run())


def test_empty_list_without_notes_folder_or_index_write(store, monkeypatch):
    monkeypatch.setattr(router,'_source',lambda *args:pytest.fail('Vocabulary must not require a notes folder'))
    status,result,headers=request()
    assert status==200 and result=={'items':[],'total':0}
    assert headers[b'cache-control']==b'no-store'
    with store[1]() as connection:
        assert connection.execute('SELECT COUNT(*) FROM language_vocabulary').fetchone()[0]==0
        assert connection.execute('SELECT value FROM precious').fetchone()[0]=='original personal data'


def test_exact_unicode_whitespace_and_duplicate_spellings_are_separate(store):
    data=payload(korean=unicodedata.normalize('NFD','  한글')+'  \n단어 ',pronunciation='  han-geul\t단어  ',
                 english="  hello  world\n'); DROP TABLE precious; -- 🐾  ")
    status,first,_=request('POST',data)
    assert status==200 and first['korean']==data['korean'] and first['pronunciation']==data['pronunciation'] and first['english']==data['english']
    assert first['korean']!=unicodedata.normalize('NFC',first['korean'])
    assert set(first)=={'id','korean','pronunciation','english','created_at'}
    assert first['created_at'].endswith('Z')
    status,second,_=request('POST',{**data,'request_id':str(uuid4())})
    assert status==200 and second['id']!=first['id']
    result=request()[1];assert result['total']==2 and result['items']==[second,first]
    with store[1]() as connection:
        assert connection.execute('SELECT value FROM precious').fetchone()[0]=='original personal data'


def test_repeat_uuid_returns_exact_receipt_without_moving_or_rewriting_entry(store):
    data=payload();first=request('POST',data)[1]
    second=request('POST',payload())[1]
    assert request('POST',{**data,'request_id':data['request_id'].upper()})[:2]==(200,first)
    assert request()[1]['items']==[second,first]
    for changes in ({'korean':data['korean']+' '},{'pronunciation':data['pronunciation']+' '},{'english':'a changed meaning'}):
        assert request('POST',{**data,**changes})[0]==409
    assert request()[1]=={'items':[second,first],'total':2}


def test_optional_pronunciation_is_empty_and_remains_manual(store):
    data=payload();del data['pronunciation']
    status,entry,_=request('POST',data);assert status==200 and entry['pronunciation']==''
    assert request('POST',{**data,'pronunciation':''})[1]==entry


@pytest.mark.parametrize('field,maximum',[('korean',256),('pronunciation',256),('english',2000)])
def test_exact_character_limits_without_unicode_normalization(store,field,maximum):
    data=payload(**{field:'🐾'*maximum})
    status,entry,_=request('POST',data);assert status==200 and entry[field]==data[field]
    assert request('POST',payload(**{field:'🐾'*(maximum+1)}))[0]==422
    assert request()[1]['total']==1


@pytest.mark.parametrize('changes',[
    {'korean':''},{'korean':' \t\n\u3000'},{'english':''},{'english':'\n\u00a0 '},
    {'korean':False},{'korean':7},{'pronunciation':None},{'pronunciation':7},{'english':False},
    {'request_id':'not-a-uuid'},{'request_id':None},{'request_id':42},{'unexpected':'ignore this?'}
])
def test_invalid_payload_never_changes_saved_words(store,changes):
    entry=request('POST',payload())[1]
    assert request('POST',payload(**changes))[0]==422
    assert request()[1]=={'items':[entry],'total':1}


def test_save_token_is_required_and_no_delete_or_update_api_exists(store):
    data=payload();del data['request_id']
    assert request('POST',data)[0]==422
    entry=request('POST',payload())[1]
    assert request('DELETE')[0]==405 and request('PUT',payload())[0]==405
    assert request()[1]=={'items':[entry],'total':1}


@pytest.mark.parametrize('query',['offset=-1','limit=0','limit=101','offset=unknown','limit=NaN'])
def test_pagination_bounds(store,query):
    assert request(query=query)[0]==422
    assert request()[1]=={'items':[],'total':0}


def test_paginated_order_is_insertion_order_even_with_equal_or_earlier_clocks(store,monkeypatch):
    _,connect=store
    time=['2026-01-01T00:00:00.000Z']
    @contextmanager
    def frozen_clock():
        with connect() as connection:
            connection.create_function('strftime',2,lambda *args:time[0])
            yield connection
    monkeypatch.setattr(router,'get_user_db',frozen_clock)
    entries=[request('POST',payload(korean=f'단어 {i}'))[1] for i in range(23)]
    time[0]='2025-01-01T00:00:00.000Z';entries.append(request('POST',payload(korean='마지막'))[1])
    pages=[request(query=f'offset={offset}&limit=7')[1] for offset in (0,7,14,21)]
    assert all(page['total']==24 for page in pages)
    assert [entry for page in pages for entry in page['items']]==list(reversed(entries))
    assert request(query='offset=1000')[1]=={'items':[],'total':24}


def test_list_total_and_rows_share_one_read_snapshot(store):
    path,connect=store
    first=request('POST',payload())[1]
    class DuringCount:
        def __enter__(self):return self
        def __exit__(self,*args):return connection.__exit__(*args)
        def execute(self,sql,*args):
            cursor=connection.execute(sql,*args)
            if sql.startswith('SELECT COUNT'):
                with connect() as writer:
                    vocabulary.create(writer,vocabulary.VocabularyCreate(**payload(korean='추가')))
            return cursor
    with connect() as connection:
        page=vocabulary.listing(DuringCount(),0,50)
    assert page.model_dump(mode='json')=={'items':[first],'total':1}
    assert request()[1]['total']==2


def test_concurrent_identical_save_uuid_creates_one_record(store):
    path,_=store;data=vocabulary.VocabularyCreate(**payload());barrier=Barrier(2)
    def save():
        with closing(sqlite3.connect(path,timeout=5)) as connection:
            connection.row_factory=sqlite3.Row;barrier.wait(timeout=5)
            return vocabulary.create(connection,data)
    with ThreadPoolExecutor(max_workers=2) as executor:
        one,two=[future.result(timeout=10) for future in [executor.submit(save),executor.submit(save)]]
    assert one==two and request()[1]['total']==1


def test_lost_committed_http_acknowledgement_retries_to_one_entry(store):
    data=payload(korean='응답을 잃은 단어')
    with pytest.raises(ConnectionError,match='Generated loss'):
        request('POST',data,drop_reply=True)
    original=request()[1];assert original['total']==1
    assert request('POST',data)[1]==original['items'][0]
    assert request()[1]==original


def test_locked_database_fails_without_writing_and_same_token_can_retry(store):
    path,connect=store;first=request('POST',payload())[1];data=payload(korean='나중에')
    with closing(sqlite3.connect(path)) as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        assert request('POST',data)[0]==503
        assert request()[1]=={'items':[first],'total':1}
        blocker.rollback()
    assert request('POST',data)[0]==200 and request()[1]['total']==2
    with connect() as connection:
        assert connection.execute('SELECT value FROM precious').fetchone()[0]=='original personal data'


@pytest.mark.parametrize('failure',['corrupt','unavailable'])
def test_unavailable_database_preserves_original_store(store,monkeypatch,failure):
    path,connect=store;first=request('POST',payload())[1]
    broken=path.parent/'broken.sqlite'
    if failure=='corrupt':broken.write_bytes(b'generated corrupt SQLite fixture')
    else:broken.mkdir()
    @contextmanager
    def broken_connect():
        with closing(sqlite3.connect(broken)) as connection:yield connection
    with monkeypatch.context() as m:
        m.setattr(router,'get_user_db',broken_connect)
        assert request()[0]==503 and request('POST',payload())[0]==503
    assert request()[1]=={'items':[first],'total':1}


def test_failed_commit_rolls_back_without_losing_previous_entries(store,monkeypatch):
    path,connect=store;first=request('POST',payload())[1];data=payload()
    @contextmanager
    def fail():
        with connect() as connection:
            class Wrapper:
                execute=connection.execute
                rollback=connection.rollback
                def commit(self):raise sqlite3.OperationalError('Generated commit failure')
            yield Wrapper()
    with monkeypatch.context() as m:
        m.setattr(router,'get_user_db',fail);assert request('POST',data)[0]==503
    assert request()[1]=={'items':[first],'total':1}
    assert request('POST',data)[0]==200 and request()[1]['total']==2


def test_actual_process_restart_and_interrupted_creation_are_safe(store):
    path,connect=store;first=request('POST',payload())[1];data=payload(korean='중단 후 재시도')
    common="""import json,sqlite3,sys,os
from contextlib import closing
sys.path.insert(0,sys.argv[2])
from modules.language import vocabulary
with closing(sqlite3.connect(sys.argv[1])) as connection:
 connection.row_factory=sqlite3.Row
"""
    restart=subprocess.run([sys.executable,'-c',common+" print(json.dumps(vocabulary.listing(connection,0,50).model_dump(mode='json')))\n",str(path),str(ROOT/'backend')],capture_output=True,text=True,check=True)
    assert json.loads(restart.stdout)=={'items':[first],'total':1}
    crash=common+""" class Crash:
  execute=connection.execute
  rollback=connection.rollback
  def commit(self):os._exit(73)
 vocabulary.create(Crash(),vocabulary.VocabularyCreate(**json.loads(sys.argv[3])))
"""
    result=subprocess.run([sys.executable,'-c',crash,str(path),str(ROOT/'backend'),json.dumps(data)],capture_output=True,text=True)
    assert result.returncode==73,result.stderr
    assert request()[1]=={'items':[first],'total':1}
    assert request('POST',data)[0]==200 and request()[1]['total']==2


def test_disabled_owner_blocks_reads_and_writes_before_opening_database(store,monkeypatch):
    first=request('POST',payload())[1]
    def disabled(slug):raise RuntimeError('Language is disabled')
    with monkeypatch.context() as m:
        m.setattr(router,'require_enabled',disabled)
        m.setattr(router,'get_user_db',lambda:pytest.fail('Disabled Language accessed user data'))
        assert request()[0]==409 and request('POST',payload())[0]==409
    assert request()[1]=={'items':[first],'total':1}


def test_additive_schema_preserves_old_study_and_unknown_personal_tables(tmp_path):
    from modules.language import study
    with closing(sqlite3.connect(tmp_path/'existing.sqlite')) as connection:
        connection.row_factory=sqlite3.Row
        connection.executescript("""CREATE TABLE language_study_preferences (
          id INTEGER PRIMARY KEY,category TEXT NOT NULL,answer_language TEXT NOT NULL,minimum INTEGER NOT NULL,maximum INTEGER NOT NULL);
          INSERT INTO language_study_preferences VALUES (1,'hours','korean',2,12);
          CREATE TABLE old_personal_annotations(content TEXT);
          INSERT INTO old_personal_annotations VALUES ('preserve every old row');""")
        user_schema.publish(connection)
        word=vocabulary.create(connection,vocabulary.VocabularyCreate(**payload()))
        user_schema.publish(connection)
        assert study.read(connection).model_dump()=={'category':'hours','answer_language':'korean','minimum':2,'maximum':12}
        assert vocabulary.listing(connection,0,50).items==[word]
        assert connection.execute('SELECT content FROM old_personal_annotations').fetchone()[0]=='preserve every old row'

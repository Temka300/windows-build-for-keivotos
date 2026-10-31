"""Isolated request-store contracts; no live profiles, media or download engines."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from services import job_store as store


@pytest.fixture
def connection():
    db = sqlite3.connect(':memory:')
    store.ensure_job_schema(db)
    yield db
    db.close()


def request(db, **changes):
    return store.record_request(db, **(dict(owner='youtube', kind='download',
        request_key='original-uuid', payload={'url': 'https://example.invalid/video',
        'source_id': 'opaque-source', 'options': {'video': 'best', 'audio': None}}) | changes))


def rows(db):
    return db.execute('SELECT * FROM kv_job_requests ORDER BY id').fetchall()


def test_receipt_exact_retry_survives_reopen_and_another_process(tmp_path):
    path = tmp_path / 'jobs.sqlite'
    with sqlite3.connect(path) as db:
        store.ensure_job_schema(db)
        first = request(db, payload={'한국어': ' words\n', 'order': [True, None, 1.5, 3]})
        before = rows(db)
        again = request(db, payload={'order': [True, None, 1.5, 3], '한국어': ' words\n'})
        assert again == first and rows(db) == before
        assert not db.in_transaction
    code = '''import json,sqlite3,sys
sys.path.insert(0,sys.argv[1])
from services.job_store import ensure_job_schema,record_request
with sqlite3.connect(sys.argv[2]) as db:
 ensure_job_schema(db)
 print(json.dumps(record_request(db,**json.loads(sys.argv[3])),ensure_ascii=True))
'''
    arguments = dict(owner=first['owner'], kind=first['kind'], request_key=first['request_key'],
                     payload=first['payload'], request_id=first['id'])
    output = subprocess.check_output([sys.executable, '-I', '-c', code,
        str(Path(store.__file__).resolve().parents[1]), str(path), json.dumps(arguments)], encoding='utf-8')
    assert json.loads(output) == first
    assert datetime.fromisoformat(first['created_at'].replace('Z', '+00:00')).utcoffset().total_seconds() == 0


@pytest.mark.parametrize('change', [dict(kind='retry'), dict(format_version=2),
    dict(payload={'source_id': 'different'}), dict(request_id='changed-id')], ids=['kind','version','input','id'])
def test_changed_request_refuses_without_rewriting_receipt(connection, change):
    first = request(connection)
    before = rows(connection)
    with pytest.raises(store.JobConflict):
        request(connection, **change)
    assert rows(connection) == before and not connection.in_transaction
    assert store.read_request(connection, owner='youtube', request_id=first['id']) == first


def test_owner_scope_and_global_pinned_id_collisions(connection):
    first = request(connection, request_id='original')
    second = request(connection, owner='twitter', request_id='another')
    assert first['request_key'] == second['request_key'] and first['id'] != second['id']
    assert store.read_request(connection, owner='twitter', request_id='original') is None
    assert store.read_request(connection, owner='youtube', request_id='missing') is None
    before = rows(connection)
    with pytest.raises(store.JobConflict):
        request(connection, owner='danbooru', request_id='original')
    with pytest.raises(store.JobConflict):
        request(connection, request_key='different', request_id='original')
    assert rows(connection) == before


@pytest.mark.parametrize('conflict', [False, True], ids=['same-input','conflicting-input'])
def test_concurrent_connections_accept_only_one_receipt(tmp_path, conflict):
    path = tmp_path / 'jobs.sqlite'
    with sqlite3.connect(path) as db:
        store.ensure_job_schema(db)
        db.execute('PRAGMA journal_mode=WAL')
    def submit(number):
        with sqlite3.connect(path, timeout=20) as db:
            store.ensure_job_schema(db)
            try:
                return request(db, payload={'value': number if conflict else 1})
            except store.JobConflict:
                return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(8)))
    accepted = [receipt for receipt in results if receipt is not None]
    assert len(accepted) == (1 if conflict else 8)
    assert len({receipt['id'] for receipt in accepted}) == 1
    with sqlite3.connect(path) as db:
        assert len(rows(db)) == 1 and db.execute('PRAGMA integrity_check').fetchone() == ('ok',)


def test_outer_transaction_and_conflict_savepoint_preserve_caller_writes(connection):
    first = request(connection)
    connection.execute('CREATE TABLE authored(note TEXT)')
    connection.execute("INSERT INTO authored VALUES('pending')")
    created = request(connection, request_key='second')
    with pytest.raises(store.JobConflict):
        request(connection, payload={'changed': True})
    assert connection.in_transaction
    assert connection.execute('SELECT * FROM authored').fetchall() == [('pending',)]
    assert store.read_request(connection, owner='youtube', request_id=created['id']) == created
    connection.rollback()
    assert rows(connection) and len(rows(connection)) == 1
    assert store.read_request(connection, owner='youtube', request_id=first['id']) == first
    assert store.read_request(connection, owner='youtube', request_id=created['id']) is None
    assert connection.execute('SELECT * FROM authored').fetchall() == []


@pytest.mark.parametrize('outer', [False, True], ids=['standalone','caller-transaction'])
def test_database_write_failure_preserves_existing_and_pending_rows(connection, outer):
    first = request(connection)
    connection.execute('CREATE TABLE authored(note TEXT)')
    connection.executescript("CREATE TRIGGER refuse_request BEFORE INSERT ON kv_job_requests "
        "BEGIN SELECT RAISE(ABORT,'injected failure'); END;")
    if outer:
        connection.execute("INSERT INTO authored VALUES('pending')")
    with pytest.raises(sqlite3.IntegrityError, match='injected failure'):
        request(connection, request_key='second')
    assert connection.in_transaction is outer
    assert len(rows(connection)) == 1
    assert store.read_request(connection, owner='youtube', request_id=first['id']) == first
    assert connection.execute('SELECT * FROM authored').fetchall() == ([('pending',)] if outer else [])


@pytest.mark.parametrize('factory', [None, sqlite3.Row, lambda cursor, row: dict(zip([c[0] for c in cursor.description], row))],
                         ids=['tuple','sqlite-row','suite-dict'])
def test_services_preserve_connection_row_factory(factory):
    with sqlite3.connect(':memory:') as db:
        db.row_factory = factory
        store.ensure_job_schema(db)
        first = request(db)
        assert store.read_request(db, owner='youtube', request_id=first['id']) == first
        assert db.row_factory is factory


def test_additive_schema_preserves_legacy_rows_headers_and_connection_settings(tmp_path):
    with sqlite3.connect(tmp_path / 'user-copy.sqlite') as db:
        db.executescript("PRAGMA user_version=73; PRAGMA application_id=12345; "
            "PRAGMA foreign_keys=ON; CREATE TABLE personal(id INTEGER PRIMARY KEY AUTOINCREMENT,note TEXT,raw BLOB);"
            "INSERT INTO personal VALUES(19,'exact 한국어',X'00FF');")
        before = db.execute('SELECT *,typeof(note),typeof(raw) FROM personal').fetchall()
        schema = db.execute("SELECT sql FROM sqlite_master WHERE name='personal'").fetchone()
        store.ensure_job_schema(db)
        first = request(db)
        store.ensure_job_schema(db)
        assert db.execute('SELECT *,typeof(note),typeof(raw) FROM personal').fetchall() == before
        assert db.execute("SELECT sql FROM sqlite_master WHERE name='personal'").fetchone() == schema
        assert db.execute('SELECT * FROM sqlite_sequence').fetchall() == [('personal', 19)]
        for name, expected in [('user_version',73),('application_id',12345),('foreign_keys',1)]:
            assert db.execute('PRAGMA ' + name).fetchone() == (expected,)
        assert store.read_request(db, owner='youtube', request_id=first['id']) == first


def test_schema_refuses_pending_transaction_without_committing(connection):
    connection.execute('CREATE TABLE authored(note TEXT)')
    connection.execute("INSERT INTO authored VALUES('pending')")
    with pytest.raises(ValueError, match='pending transaction'):
        store.ensure_job_schema(connection)
    assert connection.in_transaction
    connection.rollback()
    assert connection.execute('SELECT * FROM authored').fetchall() == []


@pytest.mark.parametrize('damage', ['future-version','missing-version','missing-requests','foreign-table'])
def test_unknown_or_incomplete_schema_is_retained(connection, damage):
    if damage == 'future-version':
        connection.execute('UPDATE kv_job_store_meta SET schema_version=999')
    elif damage == 'missing-version':
        connection.execute('DELETE FROM kv_job_store_meta')
    elif damage == 'missing-requests':
        connection.execute('DROP TABLE kv_job_requests')
    else:
        connection.execute('DROP TABLE kv_job_store_meta')
    connection.commit()
    before = list(connection.iterdump())
    with pytest.raises(ValueError):
        store.ensure_job_schema(connection)
    assert list(connection.iterdump()) == before and not connection.in_transaction


def test_initialization_ddl_failure_rolls_back_every_owned_table():
    with sqlite3.connect(':memory:') as db:
        def deny_second_table(action, name, *unused):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_TABLE and name == 'kv_job_requests' else sqlite3.SQLITE_OK
        db.set_authorizer(deny_second_table)
        with pytest.raises(sqlite3.DatabaseError):
            store.ensure_job_schema(db)
        db.set_authorizer(None)
        assert db.execute('SELECT name FROM sqlite_master').fetchall() == []
        assert not db.in_transaction
        store.ensure_job_schema(db)
        assert request(db)['id']


@pytest.mark.parametrize('change', [dict(owner='Bad Owner'),dict(kind=''),dict(request_key=''),
    dict(request_key='x'*257),dict(request_id=''),dict(request_id='a\0b'),dict(format_version=True),
    dict(format_version=0),dict(format_version=2**31),dict(payload=[]),dict(payload={1:'key'}),
    dict(payload={'value':float('nan')}),dict(payload={'value':float('inf')}),dict(payload={'value':(1,2)}),
    dict(payload={'value':b'bytes'}),dict(payload={'value':'\ud800'}),dict(payload={'value':'x'*store.MAX_PAYLOAD_BYTES})],
    ids=['owner','kind','empty-key','long-key','empty-id','nul-id','bool-version','zero-version','large-version',
         'array','key-type','nan','infinity','tuple','bytes','invalid-unicode','oversized'])
def test_invalid_input_never_changes_existing_receipts(connection, change):
    request(connection)
    before = rows(connection)
    with pytest.raises(ValueError):
        request(connection, **change)
    assert rows(connection) == before and not connection.in_transaction


@pytest.mark.parametrize('shape', ['deep','many','cyclic'])
def test_complexity_bounds_fail_without_database_writes(connection, shape):
    if shape == 'many':
        payload = {'children': [None] * 10001}
    elif shape == 'cyclic':
        payload = {}; payload['self'] = payload
    else:
        payload = {}; child = payload
        for _ in range(34):
            child['next'] = {}; child = child['next']
    with pytest.raises(ValueError, match='complex'):
        request(connection, payload=payload)
    assert rows(connection) == []


@pytest.mark.parametrize('damage', ['json','canonical','hash','oversized','version'], ids=str)
def test_corrupt_receipt_is_not_read_or_silently_replaced(connection, damage):
    first = request(connection)
    if damage == 'hash':
        connection.execute("UPDATE kv_job_requests SET payload_sha256='different'")
    elif damage == 'version':
        connection.execute('UPDATE kv_job_requests SET format_version=?', (2**31,))
    else:
        text = {'json':'{broken','canonical':'{ "unrelated": true }',
                'oversized':'{"large":"' + 'x'*store.MAX_PAYLOAD_BYTES + '"}'}[damage]
        connection.execute('UPDATE kv_job_requests SET payload_json=?,payload_sha256=?',
                           (text,hashlib.sha256(text.encode()).hexdigest()))
    connection.commit()
    before = rows(connection)
    with pytest.raises(ValueError):
        store.read_request(connection, owner='youtube', request_id=first['id'])
    with pytest.raises(ValueError):
        request(connection)
    assert rows(connection) == before


def test_import_has_no_profile_database_network_or_owner_side_effects():
    code = '''import sqlite3,socket,sys
sys.path.insert(0,sys.argv[1])
def refuse(*a,**k): raise AssertionError('unexpected I/O')
sqlite3.connect=refuse
socket.socket=refuse
from services import job_store
assert not any(name in sys.modules for name in ('config','database','module_registry','modules.youtube.jobs','modules.twitter.jobs'))
'''
    subprocess.run([sys.executable,'-I','-c',code,str(Path(store.__file__).resolve().parents[1])],check=True)


# Runtime attempt ownership joins native owner state in one SQLite transaction.
from services import operations as runtime_operations
from services import job_store

@pytest.fixture
def runtime_store(tmp_path):
    path=tmp_path/'operations.sqlite'
    connection=sqlite3.connect(path)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA foreign_keys=ON')
    runtime_operations.ensure_schema(connection)
    connection.execute('CREATE TABLE owner_state(id TEXT PRIMARY KEY,value TEXT)')
    connection.execute("INSERT INTO owner_state VALUES('job','original')")
    connection.commit()
    runtime_operations.request(connection,'owner','job',kind='download',key='first',payload={'url':'local'})
    yield connection,path
    connection.close()

@pytest.mark.parametrize('state',sorted(runtime_operations.TERMINAL))
def test_runtime_outcomes_keep_explicit_end_times(runtime_store,state):
    db,_=runtime_store
    token=runtime_operations.claim(db,'owner','job')
    with runtime_operations.bind('owner','job',token):
        runtime_operations.transition(db,'owner','job',state='working',phase='verifying',progress=80)
        runtime_operations.transition(db,'owner','job',state=state)
    row=runtime_operations.get(db,'owner','job')
    assert row['state']==state and row['started_at'] and row['ended_at']
    assert db.execute('SELECT state,ended_at FROM kv_job_attempts').fetchone()==(state,row['ended_at'])

def test_runtime_stale_attempt_cannot_write_native_owner_state(runtime_store):
    db,_=runtime_store
    old=runtime_operations.claim(db,'owner','job')
    runtime_operations.transition(db,'owner','job',state='interrupted')
    runtime_operations.retry(db,'owner','job','explicit-retry')
    new=runtime_operations.claim(db,'owner','job')
    assert new!=old
    with runtime_operations.bind('owner','job',old):
        with pytest.raises(runtime_operations.StaleAttempt):
            with job_store._write(db):
                runtime_operations.validate(db,'owner','job')
                db.execute("UPDATE owner_state SET value='incorrect'")
    assert db.execute('SELECT value FROM owner_state').fetchone()==('original',)
    assert runtime_operations.get(db,'owner','job')['state']=='started'

def test_runtime_owner_and_progress_rollback_together(runtime_store):
    db,_=runtime_store
    token=runtime_operations.claim(db,'owner','job')
    with runtime_operations.bind('owner','job',token):
        with pytest.raises(RuntimeError):
            with job_store._write(db):
                runtime_operations.validate(db,'owner','job')
                db.execute("UPDATE owner_state SET value='changed'")
                runtime_operations.transition(db,'owner','job',state='working',progress=50)
                raise RuntimeError('interrupted commit')
    assert db.execute('SELECT value FROM owner_state').fetchone()==('original',)
    assert runtime_operations.get(db,'owner','job')['progress']==0

def test_runtime_exact_retry_remains_idempotent_after_later_attempt(runtime_store):
    db,_=runtime_store
    runtime_operations.claim(db,'owner','job')
    runtime_operations.transition(db,'owner','job',state='failed')
    runtime_operations.retry(db,'owner','job','retry-one')
    runtime_operations.claim(db,'owner','job')
    runtime_operations.transition(db,'owner','job',state='failed')
    runtime_operations.retry(db,'owner','job','retry-two')
    runtime_operations.claim(db,'owner','job')
    runtime_operations.retry(db,'owner','job','retry-one')
    assert runtime_operations.get(db,'owner','job')['state']=='started'
    assert db.execute('SELECT count(*) FROM kv_job_attempts').fetchone()==(3,)

def test_runtime_legacy_completion_has_no_fabricated_end_time(runtime_store):
    db,_=runtime_store
    runtime_operations.request(db,'owner','old',kind='download',key='legacy',payload={},
        legacy_state='completed',created_at='2000-01-01')
    row=runtime_operations.get(db,'owner','old')
    assert row['created_at']=='2000-01-01' and row['started_at'] is None and row['ended_at'] is None

@pytest.mark.parametrize('progress',[float('nan'),float('inf'),-1,101,True,'5'])
def test_runtime_rejects_invalid_progress_without_mutation(runtime_store,progress):
    db,_=runtime_store
    with pytest.raises(ValueError):
        runtime_operations.transition(db,'owner','job',progress=progress)
    assert runtime_operations.get(db,'owner','job')['progress']==0


def test_runtime_concurrent_claims_create_one_attempt(runtime_store):
    from concurrent.futures import ThreadPoolExecutor
    db,path=runtime_store
    def claim(_):
        with sqlite3.connect(path,timeout=10) as other:
            try:return runtime_operations.claim(other,'owner','job')
            except runtime_operations.StaleAttempt:return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        tokens=list(pool.map(claim,range(12)))
    assert len([token for token in tokens if token])==1
    assert db.execute('SELECT count(*) FROM kv_job_attempts').fetchone()==(1,)


def test_runtime_restart_reaps_only_recorded_orphan_worker(runtime_store):
    import subprocess,json,os
    db,path=runtime_store
    code="""import os,sys,subprocess,json,sqlite3
sys.path.insert(0,'backend')
from services import operations
db=sqlite3.connect(sys.argv[1])
operations.request(db,'owner','orphan',kind='download',key='orphan',payload={})
operations.claim(db,'owner','orphan')
options={'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**options)
operations.register_process(db,'owner','orphan',child.pid)
print(json.dumps({'pid':child.pid}),flush=True)
os._exit(0)
"""
    supervisor=subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True,text=True,timeout=15)
    assert supervisor.returncode==0,supervisor.stderr
    pid=json.loads(supervisor.stdout)['pid']
    try:
        assert runtime_operations.process_identity(pid) is not None
        runtime_operations.recover_processes(db,'owner')
        assert runtime_operations.process_identity(pid) is None
    finally:
        # A failure must not leave the generated sleeper running.
        if runtime_operations.process_identity(pid) is not None:
            if os.name=='nt':
                subprocess.run(['taskkill','/PID',str(pid),'/T','/F'],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                import signal
                os.killpg(pid,signal.SIGKILL)



def test_runtime_diagnostic_binding_does_not_change_operation_records(runtime_store):
    from hashlib import sha256
    from services.diagnostic_events import current_context, diagnostic_context
    db,_=runtime_store
    token=runtime_operations.claim(db,'owner','job')
    before=list(db.iterdump())
    with diagnostic_context(library_id='library'):
        with runtime_operations.bind('owner','job',token):
            assert current_context()=={'library_id':'library','operation_owner':'owner',
                'operation_id':'job','attempt_id':sha256(token.encode()).hexdigest()[:32]}
            runtime_operations.validate(db,'owner','job')
        assert current_context()=={'library_id':'library'}
    assert current_context()=={} and list(db.iterdump())==before


def test_runtime_failed_nested_binding_restores_both_contexts(runtime_store):
    from services.diagnostic_events import current_context
    db,_=runtime_store
    token=runtime_operations.claim(db,'owner','job')
    with runtime_operations.bind('owner','job',token):
        outer=current_context()
        with pytest.raises(RuntimeError):
            with runtime_operations.bind('other','another','other-token'):
                raise RuntimeError('fixture')
        assert current_context()==outer
        runtime_operations.validate(db,'owner','job')
    assert current_context()=={}

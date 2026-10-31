"""Generated-operation evidence; all configuration is isolated by the test runner."""
import asyncio
from contextlib import contextmanager
from hashlib import sha256
import io
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import patch
from uuid import uuid4
import urllib.error
import urllib.request

from types import SimpleNamespace
from urllib.parse import urlsplit
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from services import operation_diagnostics as diag
from services.diagnostic_events import DiagnosticEventHandler, current_context, diagnostic_context
from services.tool_diagnostics import ToolEvidence, command_details, run
from tests.test_youtube_module import local
from tests.test_twitter_module import catalog as twitter_catalog


class LocalClient:
    """Exercise the real ASGI stack with standard-library fixtures only."""
    def __init__(self, app, origin="http://testserver"):
        self.app,self.origin=app,origin
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    async def get(self,path,**kwargs):return await self.request("GET",path,**kwargs)
    async def post(self,path,**kwargs):return await self.request("POST",path,**kwargs)
    async def request(self,method,path,**kwargs):
        parts=urlsplit(self.origin)
        body=json.dumps(kwargs["json"]).encode() if "json" in kwargs else b""
        headers={"host":parts.netloc,**{key.lower():value for key,value in kwargs.get("headers",{}).items()}}
        if body:headers["content-type"]="application/json"
        sent=False
        finished=asyncio.Event()
        messages=[]
        async def receive():
            nonlocal sent
            if not sent:
                sent=True
                return {"type":"http.request","body":body,"more_body":False}
            await finished.wait()
            return {"type":"http.disconnect"}
        async def send(message):
            messages.append(message)
            if message["type"]=="http.response.body" and not message.get("more_body",False):finished.set()
        await self.app({"type":"http","asgi":{"version":"3.0"},"http_version":"1.1",
            "method":method,"scheme":parts.scheme,"path":path,"raw_path":path.encode(),
            "root_path":"","query_string":b"","headers":[(key.encode(),value.encode()) for key,value in headers.items()],
            "client":("127.0.0.1",1),"server":("127.0.0.1",80)},receive,send)
        start=next(message for message in messages if message["type"]=="http.response.start")
        raw=b"".join(message.get("body",b"") for message in messages if message["type"]=="http.response.body")
        return SimpleNamespace(status_code=start["status"],json=lambda:json.loads(raw),
            headers={key.decode():value.decode() for key,value in start["headers"]})


@pytest.fixture
def evidence(tmp_path):
    from runtime_logging import _SafeFormatter
    path=tmp_path/'events.jsonl'
    saved=DiagnosticEventHandler(path,'fixture-run')
    text=io.StringIO()
    terminal=logging.StreamHandler(text)
    terminal.setFormatter(_SafeFormatter('%(message)s operation=%(operation_id)s'))
    # A filter adds current IDs just as the JSON handler does at the writer.
    class Correlation(logging.Filter):
        def filter(self,record):
            record.operation_id=getattr(record,'operation_id',current_context().get('operation_id',''))
            return True
    terminal.addFilter(Correlation())
    root=logging.getLogger();level=root.level;root.setLevel(logging.INFO)
    root.addHandler(saved);root.addHandler(terminal)
    yield path,text
    root.removeHandler(saved);root.removeHandler(terminal)
    saved.close();terminal.close();root.setLevel(level)


def events(evidence):
    return [json.loads(line) for line in evidence[0].read_text(encoding='utf-8').splitlines()]


def test_phase_counts_duration_nested_identity_and_result_unchanged(evidence):
    identity=uuid4().hex
    @diag.observed('files','scan',library='source_id')
    def scan(source_id):
        diag.count('skipped',reason='unreadable_stat')
        return {'files':2,'directories':1,'unavailable':0}
    with diag.operation('suite','request',identity=identity):
        result=scan('generated-library')
    assert result=={'files':2,'directories':1,'unavailable':0}
    finished=[event for event in events(evidence) if event['event_code']=='operation.phase_finished']
    scan_event=next(event for event in finished if event['details']['phase']=='scan')
    assert scan_event['operation_id']==identity and scan_event['library_id']=='generated-library'
    assert {key:scan_event['details'][key] for key in ('processed','success','skipped','failed')}=={
        'processed':4,'success':3,'skipped':1,'failed':0}
    assert scan_event['details']['reasons']=={'unreadable_stat':1}
    assert scan_event['details']['duration_ms']>=0
    assert scan_event['details']['parent_phase_id'] in {event['details']['phase_id'] for event in finished}
    assert identity in evidence[1].getvalue()
    assert current_context()=={}


@pytest.mark.parametrize('kind,outcome', [('error','failed'),('cancel','cancelled'),('interrupt','cancelled')])
def test_exception_and_cancellation_identity_are_preserved(evidence,kind,outcome):
    error=ValueError('fixture failure') if kind=='error' else asyncio.CancelledError() if kind=='cancel' else InterruptedError()
    @diag.observed('suite','work')
    async def work():
        raise error
    with pytest.raises(type(error)) as caught:asyncio.run(work())
    assert caught.value is error
    final=events(evidence)[-1]
    assert final['details']['outcome']==outcome
    assert final['details']['failed']==(1 if outcome=='failed' else 0)
    assert current_context()=={}


def test_cli_nonzero_result_is_failure_without_changing_exit_value(evidence):
    @diag.observed('danbooru','cli',exit_result=True)
    def cli():return 7
    assert cli()==7
    assert events(evidence)[-1]['details']['outcome']=='failed'


def test_database_lock_connects_terminal_and_file_without_sql_payload(evidence,tmp_path,monkeypatch):
    import database_connections
    path=tmp_path/'lock.sqlite'
    actual=sqlite3.connect
    with actual(path) as connection:
        connection.execute('CREATE TABLE value(id)')
    held=actual(path);held.execute('PRAGMA journal_mode=WAL');held.execute('BEGIN IMMEDIATE')
    monkeypatch.setattr(database_connections.sqlite3,'connect',lambda *a,**kw:actual(*a,timeout=.01,**kw))
    identity=uuid4().hex
    try:
        with diagnostic_context(operation_id=identity):
            with pytest.raises(sqlite3.OperationalError):
                with database_connections.connect_database(path) as connection:
                    connection.execute('INSERT INTO value VALUES(1)')
    finally:held.rollback();held.close()
    errors=[event for event in events(evidence) if event['event_code']=='database.failed']
    assert len(errors)==1 and errors[0]['operation_id']==identity
    assert errors[0]['details']['sqlite_errorcode'] is not None
    assert identity in evidence[1].getvalue()
    with actual(path) as connection:assert connection.execute('SELECT count(*) FROM value').fetchone()[0]==0


def test_unreadable_file_is_counted_without_changing_scan_contract(evidence,tmp_path,monkeypatch):
    from files_base import index
    root=tmp_path/'library';root.mkdir()
    bad=root/'unreadable.txt';bad.write_bytes(b'preserved')
    good=root/'good.txt';good.write_bytes(b'good')
    original=Path.stat
    def stat(path,*args,**kwargs):
        if path==bad:raise PermissionError('fixture unreadable')
        return original(path,*args,**kwargs)
    with index.open_index(tmp_path/'index.sqlite') as db:
        with monkeypatch.context() as local_patch:
            local_patch.setattr(Path,'stat',stat)
            result=index.scan_source(db,'generated-source',root)
    assert set(result)=={'added','updated','files','directories','unavailable'}
    assert result['files']==1 and bad.read_bytes()==b'preserved'
    final=next(event for event in reversed(events(evidence)) if event['event_code']=='operation.phase_finished')
    assert final['library_id']=='generated-source' and final['details']['skipped']==1
    assert final['details']['reasons']=={'unreadable_stat':1}


def test_network_retry_has_separate_attempts_and_final_success(evidence,monkeypatch):
    from modules.danbooru import network
    class Reply:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self):return b'{"ok":true}'
    failure=urllib.error.HTTPError('https://example.test/api?q=private-query',503,'failure',{},None)
    with patch.object(network.urllib.request,'urlopen',side_effect=[failure,Reply()]),patch.object(network.time,'sleep'):
        with diagnostic_context(operation_id='generated-job',attempt_id='parent-attempt'):
            result=network.read_json(urllib.request.Request(failure.url),timeout=1,retries=1,emit=lambda line:None)
    assert result=={'ok':True}
    rows=[row for row in events(evidence) if row['event_code']=='network.attempt_finished']
    assert len(rows)==2 and len({row['attempt_id'] for row in rows})==2
    assert {row['operation_id'] for row in rows}=={'generated-job'}
    assert all(row['details']['parent_attempt_id']=='parent-attempt' for row in rows)
    assert rows[0]['details']['retry'] is True and rows[1]['details']['outcome']=='completed'
    final=events(evidence)[-1]
    assert final['details']['outcome']=='completed' and final['details']['failed']==1 and final['details']['success']==1
    assert 'private-query' not in evidence[0].read_text(encoding='utf-8')


def test_successful_tool_stderr_is_not_failure_and_stdout_stays_machine_readable(evidence):
    result=run('fixture',[sys.executable,'-c',
        "import sys;print('{\"ok\":true}');print('ordinary diagnostic',file=sys.stderr)"],
        capture_output=True,text=True,check=True)
    assert json.loads(result.stdout)=={'ok':True} and result.returncode==0
    row=next(row for row in events(evidence) if row['event_code']=='tool.finished')
    assert row['details']['outcome']=='completed' and row['details']['exit_code']==0
    assert any(item['stream']=='stderr' for item in row['details']['output_head'])
    started=next(row for row in events(evidence) if row['event_code']=='tool.started')
    assert started['details']['tool_version']==sys.version.split()[0]
    assert started['details']['arguments']==['-c','[redacted]']


def test_failed_tool_keeps_original_exception_and_retained_safe_output(evidence):
    with pytest.raises(subprocess.CalledProcessError) as caught:
        run('fixture',[sys.executable,'-c',"import sys;print('password=private-tool',file=sys.stderr);sys.exit(4)"],
            capture_output=True,check=True)
    assert caught.value.returncode==4
    row=events(evidence)[-1]
    assert row['details']['exit_code']==4 and row['details']['outcome']=='failed'
    assert row['severity_text']=='ERROR'
    assert 'private-tool' not in evidence[0].read_text(encoding='utf-8')


def test_tool_bounds_protocol_metadata_and_binary_are_not_recorded(evidence):
    tool=ToolEvidence('fixture',[sys.executable],secrets=('private-key',))
    for index in range(100):tool.output(f'{index} Authorization: Bearer private-key')
    tool.output(json.dumps({'event':'complete','capture':{'description':'private note body'},'title':'private title'}),'stdout',protocol=True)
    tool.output(b'private media bytes','stdout',binary=True)
    tool.finish(0,validation=True)
    tool.finish(1)
    rows=[row for row in events(evidence) if row['event_code']=='tool.finished']
    assert len(rows)==1
    detail=rows[0]['details']
    assert len(detail['output_head'])==8 and len(detail['output_tail'])==8
    assert detail['omitted_lines']==85 and detail['output_validated'] is True
    saved=evidence[0].read_text(encoding='utf-8')
    assert all(value not in saved for value in ('private-key','private note body','private title','private media bytes'))


def test_committed_states_retries_and_restart_preserve_attempts(evidence,local):
    from modules.youtube import jobs
    source,media,_=local
    job=jobs.create('https://youtu.be/abcdefghijk',source.source_id)
    jobs.update(job['id'],status='downloading')
    jobs.update(job['id'],status='failed')
    replacement=jobs.retry(job['id'])
    jobs.update(replacement['id'],status='downloading')
    jobs.recover()
    rows=[row for row in events(evidence) if row['event_code']=='operation.state_committed']
    failed=next(row for row in rows if row['operation_id']==job['id'] and row['details']['state']=='failed')
    interrupted=next(row for row in rows if row['operation_id']==replacement['id'] and row['details']['state']=='interrupted')
    assert failed['attempt_id']!=interrupted['attempt_id']
    assert failed['details']['counts']=={'processed':1,'success':0,'skipped':0,'failed':1}
    assert interrupted['details']['counts']['failed']==0
    assert interrupted['details']['reason']=='unfinished_work_recovered'
    assert jobs.get(job['id'])['status']=='failed'
    assert jobs.get(replacement['id'])['status']=='interrupted'
    retry=next(row for row in events(evidence) if row['event_code']=='operation.retry_requested')
    assert retry['operation_id']==replacement['id']
    assert retry['details']['previous_operation_id']==job['id'] and retry['details']['reason']=='user_requested'
    assert not list(media.iterdir())


def test_failed_owner_transaction_never_logs_a_committed_transition(evidence,local):
    from modules.youtube import jobs
    source,_,_=local
    job=jobs.create('https://youtu.be/abcdefghijk',source.source_id)
    jobs.update(job['id'],status='downloading')
    with jobs.get_jobs_db() as db:
        db.execute("CREATE TRIGGER refuse_update BEFORE UPDATE ON youtube_downloads BEGIN SELECT RAISE(ABORT,'fixture rollback'); END")
        db.commit()
    before=len(events(evidence))
    with pytest.raises(sqlite3.IntegrityError):jobs.update(job['id'],status='failed')
    assert not any(row['event_code']=='operation.state_committed' for row in events(evidence)[before:])
    assert jobs.get(job['id'])['status']=='downloading'


def test_worker_correlation_and_stdout_json_survive_process_boundary(tmp_path):
    home=tmp_path/'home';home.mkdir();original=home/'user.sqlite';original.write_bytes(b'preserved fixture')
    code=r'''
import json,sys
sys.path.insert(0,sys.argv[1])
from services.operation_diagnostics import worker_diagnostics,operation
@worker_diagnostics
def main():
    with operation('fixture','child'):print(json.dumps({'ok':True}))
main()
'''
    identity=uuid4().hex
    with diagnostic_context(operation_id=identity,attempt_id='derived-attempt'):
        environment=diag.worker_environment({**os.environ,'KEIVOTOS_HOME':str(home)})
    result=subprocess.run([sys.executable,'-c',code,str(ROOT/'backend')],env=environment,text=True,capture_output=True,check=True)
    assert json.loads(result.stdout)=={'ok':True}
    paths=list((home/'logs').glob('*/events-0001.jsonl'))
    assert len(paths)==1
    rows=[json.loads(line) for line in paths[0].read_text(encoding='utf-8').splitlines()]
    assert len(rows)==2 and all(row['operation_id']==identity and row['attempt_id']=='derived-attempt' for row in rows)
    assert identity in result.stderr
    assert original.read_bytes()==b'preserved fixture' and not (home/'keivotos.sqlite').exists()


@pytest.fixture
def diagnostic_app():
    from fastapi import FastAPI,HTTPException
    from app_factory import _DiagnosticRequestContext,log_http_error
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from routers import diagnostics
    diagnostics._recent.clear()
    app=FastAPI()
    app.add_middleware(_DiagnosticRequestContext)
    app.add_exception_handler(StarletteHTTPException,log_http_error)
    app.include_router(diagnostics.router)
    @app.get('/api/failure')
    def fail():raise HTTPException(503,'private note body')
    return app


def test_browser_error_matches_backend_operation_without_body_logging(evidence,diagnostic_app):
    identity=str(uuid4())
    async def check():
        async with LocalClient(diagnostic_app) as client:
            response=await client.get('/api/failure',headers={'X-Keivotos-Operation-ID':identity})
            assert response.status_code==503 and response.json()=={'detail':'private note body'}
            assert response.headers['x-keivotos-operation-id']==identity
            ack=await client.post('/api/diagnostics/frontend',json={
                'code':'request_failed','component':'suite','operation_id':identity,'status':503})
            assert ack.json()=={'status':'recorded','operation_id':identity}
    asyncio.run(check())
    rows=[row for row in events(evidence) if row['event_code'] in ('http.request_rejected','frontend.request_failed')]
    assert len(rows)==2 and {row['operation_id'] for row in rows}=={identity}
    assert 'private note body' not in evidence[0].read_text(encoding='utf-8')
    assert identity in evidence[1].getvalue()


@pytest.mark.parametrize('extra', [{'message':'private'},{'body':{'note':'private'}},{'stack':'private'},
    {'code':'arbitrary'},{'component':'private component'},{'operation_id':'../../private'},{'status':700}])
def test_frontend_intake_rejects_freeform_and_invalid_fields(diagnostic_app,evidence,extra):
    async def check():
        async with LocalClient(diagnostic_app) as client:
            result=await client.post('/api/diagnostics/frontend',json={'code':'script_error',**extra})
            assert result.status_code==422
    asyncio.run(check())
    assert not any(row['event_code'].startswith('frontend.') for row in events(evidence))


def test_frontend_intake_rate_is_bounded_and_http_security_stays_active(diagnostic_app,evidence):
    async def check():
        async with LocalClient(diagnostic_app) as client:
            for index in range(60):
                assert (await client.post('/api/diagnostics/frontend',json={'code':'script_error'})).status_code==200
            assert (await client.post('/api/diagnostics/frontend',json={'code':'script_error'})).status_code==429
        from server import app
        async with LocalClient(app,origin='http://localhost') as client:
            assert (await client.post('/api/diagnostics/frontend',headers={'Origin':'https://outside.invalid'},
                                     json={'code':'script_error'})).status_code==403
    asyncio.run(check())
    assert sum(row['event_code']=='frontend.script_error' for row in events(evidence))==60


def test_empty_child_environment_still_pins_the_selected_disposable_profile(tmp_path,monkeypatch):
    monkeypatch.setenv('KEIVOTOS_HOME',str(tmp_path/'selected'))
    with diagnostic_context(operation_id='fixture'):
        selected=diag.worker_environment({})
    assert selected['KEIVOTOS_HOME']==str(tmp_path/'selected')
    assert json.loads(selected['KEIVOTOS_DIAGNOSTIC_CONTEXT'])['operation_id']=='fixture'
    assert diag.worker_environment({},profile=tmp_path/'explicit')['KEIVOTOS_HOME']==str(tmp_path/'explicit')


def test_worker_context_without_explicit_profile_does_not_open_default_storage(monkeypatch):
    monkeypatch.delenv('KEIVOTOS_HOME',raising=False)
    monkeypatch.setenv('KEIVOTOS_DIAGNOSTIC_CONTEXT',json.dumps({'operation_id':'fixture'}))
    @diag.worker_diagnostics
    def fixture():return current_context()
    assert fixture()=={}


def test_zero_exit_without_worker_completion_is_not_success(evidence,local,monkeypatch,tmp_path):
    from modules.youtube import jobs
    source,_,_=local
    fake=tmp_path/'worker-source';fake.mkdir()
    from services import download_controls
    ready=tmp_path/'registered-worker'
    worker="from pathlib import Path\nimport time\nready=Path("+repr(str(ready))+")\nlimit=time.monotonic()+5\nwhile not ready.exists():\n if time.monotonic()>limit:raise RuntimeError('fixture registration timeout')\n time.sleep(.01)\n"
    worker+="print("+repr(json.dumps({"event":"progress","progress":2,"title":"fixture"}))+",flush=True)\n"
    (fake/'app.py').write_text(worker)
    register=download_controls.register_worker
    def registered(*args,**kwargs):
        result=register(*args,**kwargs)
        ready.write_text('registered')
        return result
    monkeypatch.setattr(download_controls,'register_worker',registered)
    monkeypatch.setattr(jobs.config,'CODE_ROOT',fake)
    job=jobs.create('https://youtu.be/abcdefghijk',source.source_id)
    jobs.update(job['id'],status='downloading')
    with pytest.raises(RuntimeError,match='Download failed'):
        asyncio.run(jobs.execute(job))
    finished=[row for row in events(evidence) if row['event_code']=='tool.finished']
    assert finished[-1]['details']['exit_code']==0
    assert finished[-1]['details']['outcome']=='failed'
    assert finished[-1]['details']['output_validated'] is False


def test_worker_diagnostic_storage_failure_does_not_change_owner_result(tmp_path,monkeypatch,capsys):
    import runtime_logging
    monkeypatch.setenv('KEIVOTOS_HOME',str(tmp_path/'home'))
    monkeypatch.setenv('KEIVOTOS_DIAGNOSTIC_CONTEXT',json.dumps({'operation_id':'fixture'}))
    monkeypatch.setattr(runtime_logging,'configure_runtime_logging',lambda:(_ for _ in ()).throw(OSError('private OS path')))
    @diag.worker_diagnostics
    def fixture():return 17
    assert fixture()==17
    assert capsys.readouterr().err=='Keivotos: worker diagnostic storage is unavailable.'+chr(10)


@pytest.mark.parametrize('exception,has_trace',[(ValueError('refused'),False),(RuntimeError('unexpected'),True)])
def test_expected_refusal_and_unexpected_failure_keep_distinct_evidence(evidence,exception,has_trace):
    with pytest.raises(type(exception)):
        with diag.operation('suite','boundary'):
            raise exception
    final=events(evidence)[-1]
    assert final['details']['error_type']==type(exception).__name__
    assert ('exception' in final)==has_trace
    assert final['details']['outcome']=='failed'


def test_multiline_tool_output_retains_tail_and_explicit_truncation(evidence):
    tool=ToolEvidence('fixture',[sys.executable])
    tool.output('long '+('x'*2000)+chr(10)+chr(10).join('line '+str(index) for index in range(30)))
    tool.finish(0)
    detail=events(evidence)[-1]['details']
    assert detail['output_lines']==31 and detail['omitted_lines']==15
    assert detail['truncated_lines']==1
    assert detail['output_tail'][-1]['line']=='line 29'


def test_relative_http_targets_exclude_private_queries_in_both_log_views(evidence):
    logging.getLogger('uvicorn.access').warning(
        '%s - "%s %s HTTP/%s" %d','local','GET','/api/search?q=private-study-note&api_key=private-key','1.1',404)
    saved=evidence[0].read_text(encoding='utf-8')
    assert 'private-study-note' not in saved+evidence[1].getvalue()
    assert 'private-key' not in saved+evidence[1].getvalue()
    assert '/api/search?[redacted]' in events(evidence)[-1]['message']


def test_twitter_retry_records_only_a_new_committed_request(evidence,twitter_catalog):
    from modules.twitter import jobs
    from tests.test_twitter_module import new_job
    job=new_job(twitter_catalog)
    jobs.recover()
    token=uuid4()
    assert jobs.retry(job['id'],token)['status']=='queued'
    assert jobs.retry(job['id'],token)['status']=='queued'
    rows=[row for row in events(evidence) if row['event_code']=='operation.retry_requested']
    assert len(rows)==1 and rows[0]['operation_id']==job['id']
    assert rows[0]['details']=={'reason':'user_requested','state':'requested'}
    assert not list(twitter_catalog[1].rglob('manifest.json'))

"""Generated diagnostic evidence only; runners set KEIVOTOS_HOME before imports."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from services.diagnostic_events import (
    DiagnosticEventHandler, current_context, diagnostic_context, redact_log_text,
)


def read_events(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def record(message="fixture event", level=logging.INFO, **extra):
    result = logging.LogRecord("keivotos.fixture", level, __file__, 1, message, (), None)
    result.__dict__.update(extra)
    return result


@pytest.fixture
def writer(tmp_path):
    path = tmp_path / "events.jsonl"
    handler = DiagnosticEventHandler(path, "fixture-run")
    yield handler, path
    handler.close()


def test_import_has_no_configuration_database_or_network_side_effects(tmp_path):
    code = """
import sys
sys.path.insert(0, sys.argv[1])
class Refuse:
    def find_spec(self, name, *unused):
        if name in ('config','database','sqlite3','runtime_logging') or name.startswith(('modules.', 'urllib.request')):
            raise AssertionError(name)
sys.meta_path.insert(0, Refuse())
import services.diagnostic_events
"""
    home = tmp_path / "home"
    subprocess.run([sys.executable, "-c", code, str(ROOT / "backend")],
                   env={**os.environ, "KEIVOTOS_HOME": str(home)}, check=True)
    assert not home.exists()


@pytest.mark.parametrize("level,text,number", [
    (5, "TRACE", 1), (10, "DEBUG", 5), (20, "INFO", 9),
    (30, "WARNING", 13), (40, "ERROR", 17), (50, "CRITICAL", 21), (60, "CRITICAL", 21),
])
def test_schema_time_severity_and_unicode(writer, level, text, number):
    handler, path = writer
    handler.handle(record("한국어 🎵\nnext line", level, event_code="fixture.saved",
                          event_details={"count": 3, "duration": 1.25, "ready": True}))
    event, = read_events(path)
    assert event["schema_version"] == 1
    assert datetime.fromisoformat(event["timestamp"]).utcoffset().total_seconds() == 0
    assert datetime.fromisoformat(event["observed_timestamp"]) >= datetime.fromisoformat(event["timestamp"])
    assert (event["severity_text"], event["severity_number"]) == (text, number)
    assert event["component"] == "keivotos.fixture"
    assert event["event_code"] == "fixture.saved"
    assert event["message"] == "한국어 🎵\nnext line"
    assert event["details"] == {"count": 3, "duration": 1.25, "ready": True}
    assert len(event["event_id"]) == 32 and event["run_id"] == "fixture-run"
    assert "operation_id" not in event
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_redaction_applies_to_messages_nested_values_and_exception(writer):
    handler, path = writer
    secret = "private-key+42"
    error = RuntimeError(f"Authorization: Bearer hidden-bearer https://u:p@example.test/a?search=private {secret}")
    row = record(f"api_key='{secret}' Cookie: session=private-cookie",
                 diagnostic_secrets=(secret,), event_details={
                     "Authorization": "private-auth", "Cookie": "private-cookie",
                     "nested": {"request_body": {"text": "private study note"},
                                "note_text": "private note", "markdown": "private markdown",
                                "password": "private-password", "credential": "private-credential",
                                "safe": f"failed https://example.test/path?q=hidden {secret}"},
                     "filename": "음악.flac"})
    row.exc_info = (type(error), error, None)
    handler.handle(row)
    saved = path.read_text(encoding="utf-8")
    for value in (secret, "hidden-bearer", "u:p", "search=private", "private-cookie",
                  "private-auth", "private study note", "private note", "private markdown",
                  "private-password", "private-credential", "q=hidden"):
        assert value not in saved
    assert "음악.flac" in saved and "example.test/path" in saved


def test_bad_values_and_cycles_are_bounded_without_calling_private_repr(writer):
    handler, path = writer
    class Private:
        def __str__(self):
            raise AssertionError("must not call")
        __repr__ = __str__
    cycle = {}
    cycle["child"] = cycle
    handler.handle(record("x" * 50000, event_code="unsafe code", event_details={
        "bad": Private(), "bytes": b"private bytes", "nan": float("nan"),
        "huge": 10**500, "cycle": cycle, "many": ["x" * 10000] * 1000,
        "surrogate": "\ud800",
    }))
    event, = read_events(path)
    assert event["event_code"] == "python.log"
    assert event["truncated_values"] > 0
    assert len(path.read_bytes()) < 40000
    assert "private bytes" not in path.read_text(encoding="utf-8")


def test_references_identify_same_exception_object_not_same_message(writer):
    handler, path = writer
    error = ValueError("same wording")
    for value in (error, error, ValueError("same wording")):
        row = record("boundary failed")
        row.exc_info = (type(value), value, None)
        handler.handle(row)
    first, repeat, other = read_events(path)
    assert first["exception"]["message"] == "same wording"
    assert "exception" not in repeat and repeat["exception_repeat_count"] == 1
    assert repeat["exception_id"] == first["exception_id"]
    assert repeat["exception_event_id"] == first["event_id"]
    assert other["exception_id"] != first["exception_id"] and "exception" in other


def test_exception_grouping_has_age_and_capacity_bounds(tmp_path):
    path = tmp_path / "events.jsonl"
    handler = DiagnosticEventHandler(path, "run", exception_limit=2, exception_ttl=10)
    errors = [ValueError(str(index)) for index in range(3)]
    with patch("services.diagnostic_events.time.monotonic", return_value=1):
        for error in errors:
            row = record()
            row.exc_info = (type(error), error, None)
            handler.handle(row)
    assert len(handler._exceptions) == 2
    with patch("services.diagnostic_events.time.monotonic", return_value=12):
        row = record()
        row.exc_info = (type(errors[-1]), errors[-1], None)
        handler.handle(row)
    assert len(handler._exceptions) == 1
    handler.close()
    events = read_events(path)
    assert "exception" in events[-1]
    assert events[-1]["exception_id"] != events[-2]["exception_id"]
    assert handler._exceptions == {}


def test_failed_write_does_not_create_false_exception_reference(writer, capsys):
    handler, path = writer
    error = RuntimeError("password=private-write")
    row = record("secret=private-write")
    row.exc_info = (type(error), error, None)
    with patch.object(handler.stream, "write", side_effect=OSError("private OS path")):
        handler.handle(row)
    assert "private" not in capsys.readouterr().err
    assert len(handler._exceptions) == 0
    handler.handle(row)
    event, = read_events(path)
    assert "exception" in event
    assert "private-write" not in path.read_text(encoding="utf-8")


def test_concurrent_threads_write_complete_unique_records_and_keep_context(writer):
    handler, path = writer
    def work(index):
        with diagnostic_context(operation_id=f"job-{index}", library_id=f"library-{index}"):
            for number in range(60):
                handler.handle(record(f"한국어 {index}/{number}"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, range(8)))
    events = read_events(path)
    assert len(events) == 480
    assert len({event["event_id"] for event in events}) == 480
    for event in events:
        index = event["message"].split()[1].split("/")[0]
        assert event["operation_id"] == f"job-{index}"
        assert event["library_id"] == f"library-{index}"
    assert current_context() == {}


def test_async_contexts_nesting_and_failures_restore_identities(writer):
    handler, path = writer
    async def task(index):
        with diagnostic_context(operation_id=str(index)):
            await asyncio.sleep(0)
            with diagnostic_context(item_id=f"item-{index}"):
                await asyncio.sleep(0)
                handler.handle(record("nested"))
            assert current_context() == {"operation_id": str(index)}
    async def run():
        await asyncio.gather(*(task(index) for index in range(20)))
    asyncio.run(run())
    with pytest.raises(RuntimeError):
        with diagnostic_context(library_id="temporary"):
            raise RuntimeError("fixture")
    assert current_context() == {}
    assert {event["operation_id"] for event in read_events(path)} == {str(index) for index in range(20)}
    for values in ({"invalid": "id"}, {"item_id": ""}, {"item_id": "x"*257}, {"item_id": 3}):
        with pytest.raises(ValueError):
            with diagnostic_context(**values):
                pass


def test_explicit_context_and_ordinary_thread_dont_leak(writer):
    handler, path = writer
    with diagnostic_context(operation_id="parent"):
        thread = threading.Thread(target=lambda: handler.handle(record("separate thread")))
        thread.start()
        thread.join()
        handler.handle(record("override", operation_id="explicit"))
    first, second = read_events(path)
    assert "operation_id" not in first
    assert second["operation_id"] == "explicit"


@pytest.mark.parametrize("status,text,number", [(200,"INFO",9),(404,"WARNING",13),(500,"ERROR",17)])
def test_access_outcomes_use_actual_http_severity(writer, status, text, number):
    handler, path = writer
    row = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "%s %s %s %s %s",
                            ("local", "GET", "/api/sample", "1.1", status), None)
    handler.handle(row)
    event, = read_events(path)
    assert (event["severity_text"], event["severity_number"]) == (text,number)


def test_standard_rotation_keeps_valid_json_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    handler = DiagnosticEventHandler(path, "run", max_bytes=1600, backups=2)
    for index in range(20):
        handler.handle(record("한국어 " * 30, event_details={"index": index}))
    handler.close()
    assert len(list(tmp_path.glob("events.jsonl*"))) == 3
    for file in tmp_path.iterdir():
        assert all(event["run_id"] == "run" for event in read_events(file))
    assert read_events(path)[-1]["details"]["index"] == 19


def test_repeated_configuration_closes_owned_writers_and_preserves_old_text_logs(tmp_path):
    code = """
import json, logging, sys
sys.path.insert(0, sys.argv[1])
import runtime_logging as runtime
import config
first = runtime.configure_runtime_logging()
old = tuple(runtime._owned_handlers)
second = runtime.configure_runtime_logging()
assert first == second
assert all(handler._closed for handler in old)
logging.getLogger('fixture').warning('one saved event', extra={'event_code':'fixture.saved'})
logging.shutdown()
print(json.dumps([str(config.DIAGNOSTIC_LOG_FILE), str(first[0])]))
"""
    result = subprocess.run([sys.executable, "-c", code, str(ROOT / "backend")],
                            env={**os.environ, "KEIVOTOS_HOME": str(tmp_path)},
                            text=True, capture_output=True, check=True)
    diagnostic, runtime = json.loads(result.stdout)
    events = read_events(Path(diagnostic))
    assert len(events) == 1 and events[0]["event_code"] == "fixture.saved"
    assert "one saved event" in Path(runtime).read_text(encoding="utf-8")


def test_processes_use_separate_run_files_without_touching_databases(tmp_path):
    preserved = tmp_path / "user.sqlite"
    preserved.write_bytes(b"not a database: preserved fixture")
    code = """
import json, logging, sys
sys.path.insert(0, sys.argv[1])
from runtime_logging import configure_runtime_logging
from config import DIAGNOSTIC_LOG_FILE
configure_runtime_logging()
for index in range(30):
    logging.getLogger('fixture').info('event %s', index)
logging.shutdown()
print(json.dumps(str(DIAGNOSTIC_LOG_FILE)))
"""
    environment = {**os.environ, "KEIVOTOS_HOME": str(tmp_path)}
    processes = [subprocess.Popen([sys.executable, "-c", code, str(ROOT / "backend")],
                  env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
    paths = []
    for process in processes:
        output, error = process.communicate(timeout=30)
        assert process.returncode == 0, error
        paths.append(Path(json.loads(output)))
    assert len(set(paths)) == 3
    assert len({read_events(path)[0]["run_id"] for path in paths}) == 3
    assert all(len(read_events(path)) == 30 for path in paths)
    assert preserved.read_bytes() == b"not a database: preserved fixture"
    assert not (tmp_path / "state/jobs.sqlite").exists()


@pytest.mark.parametrize("status,level", [(400,logging.WARNING),(500,logging.ERROR)])
def test_http_error_boundary_keeps_response_and_omits_raw_error_details(status, level, caplog):
    from app_factory import log_http_error
    from starlette.requests import Request
    from starlette.exceptions import HTTPException
    request = Request({"type":"http", "method":"POST", "path":"/api/fixture",
                       "headers":[], "scheme":"http", "query_string":b"api_key=private"})
    with caplog.at_level(logging.WARNING, logger="keivotos.requests"):
        response = asyncio.run(log_http_error(request, HTTPException(status, "private note body")))
    assert json.loads(response.body) == {"detail":"private note body"}
    entry = caplog.records[-1]
    assert entry.levelno == level and entry.event_code == "http.request_rejected"
    assert entry.event_details == {"method":"POST","status_code":status}
    assert "private" not in entry.getMessage()


def test_module_failure_boundary_has_stable_code_and_preserves_failed_state(caplog):
    from lifecycle import ModuleRuntime
    runtime = ModuleRuntime.__new__(ModuleRuntime)
    runtime._states, runtime._state_lock = {}, threading.Lock()
    error = RuntimeError("fixture failure")
    with caplog.at_level(logging.ERROR, logger="lifecycle"):
        runtime.fail("fixture", "Startup hooks", error)
    assert runtime.status("fixture") == {"state":"failed","error":"Startup hooks failed; see the runtime log"}
    entry = caplog.records[-1]
    assert entry.event_code == "module.runtime_failed"
    assert entry.event_details == {"module":"fixture","stage":"Startup hooks"}
    assert entry.exc_info[1] is error



def test_details_require_structured_fields_instead_of_freeform_request_body(writer):
    handler,path=writer
    handler.handle(record(event_details='private note body'))
    event,=read_events(path)
    assert event['details']=={} and event['truncated_values']==1
    assert 'private note body' not in path.read_text(encoding='utf-8')


@pytest.mark.parametrize('kind', ['thread','fatal-after-shutdown'])
def test_unhandled_failures_reach_structured_stream_even_after_launcher_cleanup(tmp_path,kind):
    code=r'''
import logging,sys,threading
sys.path.insert(0,sys.argv[1])
from runtime_logging import configure_runtime_logging
configure_runtime_logging()
def fail():
    raise RuntimeError('password=private-fixture')
if sys.argv[2]=='thread':
    worker=threading.Thread(target=fail,name='fixture')
    worker.start()
    worker.join()
    logging.shutdown()
else:
    try:
        fail()
    finally:
        logging.shutdown()
'''
    result=subprocess.run([sys.executable,'-c',code,str(ROOT/'backend'),kind],
        env={**os.environ,'KEIVOTOS_HOME':str(tmp_path)},text=True,capture_output=True)
    assert result.returncode==(0 if kind=='thread' else 1)
    paths=list((tmp_path/'logs').glob('*/events-0001.jsonl'))
    assert len(paths)==1
    event,=read_events(paths[0])
    assert event['event_code']==('runtime.unhandled_thread_exception' if kind=='thread' else 'runtime.unhandled_exception')
    assert event['severity_text']=='CRITICAL' and 'exception' in event
    assert 'private-fixture' not in paths[0].read_text(encoding='utf-8')+result.stderr



def test_legacy_handler_write_failure_never_dumps_raw_arguments(capsys):
    from runtime_logging import _SafeConsoleHandler, _SafeFormatter
    class Broken:
        def write(self,text):
            raise UnicodeEncodeError('ascii','음악',0,1,'fixture console')
        def flush(self):
            pass
    handler=_SafeConsoleHandler(Broken())
    handler.setFormatter(_SafeFormatter('%(message)s'))
    row=record('password=private-write')
    handler.handle(row)
    assert capsys.readouterr().err=='Keivotos: a diagnostic message could not be written.\n'
    handler.close()

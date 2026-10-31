"""Durable operations and attempts; owner mutations join the same transaction."""
from contextlib import contextmanager
from contextvars import ContextVar
import math
from hashlib import sha256
from services.diagnostic_events import diagnostic_context
from uuid import uuid4
from services.job_store import _key, _name, _one, _write, record_request, ensure_job_schema

TERMINAL = frozenset({'completed','completed-with-warnings','failed','cancelled','interrupted'})
STATES = TERMINAL | {'requested','started','working'}
_lease = ContextVar('keivotos_attempt', default=None)
SCHEMA = (
    """CREATE TABLE kv_operations (
        owner TEXT NOT NULL, id TEXT NOT NULL, request_id TEXT NOT NULL,
        state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
        phase TEXT NOT NULL DEFAULT '', progress REAL NOT NULL DEFAULT 0,
        publication TEXT NOT NULL DEFAULT '', created_at TEXT,
        started_at TEXT, ended_at TEXT, PRIMARY KEY(owner,id),
        FOREIGN KEY(request_id) REFERENCES kv_job_requests(id))""",
    """CREATE TABLE kv_job_attempts (
        owner TEXT NOT NULL, operation_id TEXT NOT NULL, number INTEGER NOT NULL,
        token TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
        started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        ended_at TEXT, PRIMARY KEY(owner,operation_id,number),
        FOREIGN KEY(owner,operation_id) REFERENCES kv_operations(owner,id))""",
    """CREATE TABLE kv_operation_schema (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)""",
)

class StaleAttempt(ValueError):
    """An obsolete worker cannot mutate another attempt."""

def rows(connection, sql, args=()):
    import sqlite3
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in cursor.execute(sql,args)]
    finally:
        cursor.close()

def ensure_schema(connection):
    ensure_job_schema(connection)
    with _write(connection):
        present = rows(connection,"SELECT name FROM sqlite_master WHERE name IN ('kv_operations','kv_job_attempts','kv_operation_schema')")
        if present:
            if len(present)!=3 or _one(connection,'SELECT * FROM kv_operation_schema')!={'id':1,'version':1}:
                raise ValueError('Unsupported operation schema')
            connection.execute('SELECT owner,id,request_id,state,attempt,phase,progress,publication,created_at,started_at,ended_at FROM kv_operations LIMIT 0')
            connection.execute('SELECT owner,operation_id,number,token,state,started_at,ended_at FROM kv_job_attempts LIMIT 0')
        else:
            for sql in SCHEMA:
                connection.execute(sql)
            connection.execute('INSERT INTO kv_operation_schema VALUES(1,1)')
    from services.download_history import ensure_schema as ensure_download_history
    ensure_download_history(connection)

def get(connection, owner, identity):
    _name(owner,'owner'); _key(identity,'operation ID')
    return _one(connection,'SELECT * FROM kv_operations WHERE owner=? AND id=?',(owner,identity))

def request(connection, owner, identity, *, kind, key, payload, legacy_state=None, created_at=None):
    _key(identity,'operation ID')
    state=legacy_state or 'requested'
    if state not in STATES:
        raise ValueError('Invalid operation state')
    with _write(connection):
        receipt=record_request(connection,owner=owner,kind=kind,request_key=key,payload=payload)
        prior=get(connection,owner,identity)
        if prior:
            if prior['request_id']!=receipt['id']:
                raise ValueError('Operation already describes another request')
            return prior
        connection.execute('INSERT INTO kv_operations(owner,id,request_id,state,created_at) VALUES(?,?,?,?,?)',
            (owner,identity,receipt['id'],state,created_at if legacy_state else receipt['created_at']))
        return get(connection,owner,identity)

def claim(connection, owner, identity):
    with _write(connection):
        row=get(connection,owner,identity)
        if not row or row['state']!='requested':
            raise StaleAttempt('Operation is not queued')
        number,token=row['attempt']+1,uuid4().hex
        connection.execute('INSERT INTO kv_job_attempts(owner,operation_id,number,token,state) VALUES(?,?,?,?,?)',
            (owner,identity,number,token,'started'))
        connection.execute("UPDATE kv_operations SET attempt=?,state='started',progress=0,phase='',ended_at=NULL,"
            "started_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE owner=? AND id=?",(number,owner,identity))
        return token

def validate(connection, owner, identity, *, token=None):
    selected=_lease.get()
    if token is None and selected is not None:
        if selected[:2]!=(owner,identity):
            raise StaleAttempt('Worker operation identity changed')
        token=selected[2]
    if token is None:
        return
    row=_one(connection,'SELECT a.token,o.state FROM kv_operations o JOIN kv_job_attempts a '
        'ON a.owner=o.owner AND a.operation_id=o.id AND a.number=o.attempt WHERE o.owner=? AND o.id=?',(owner,identity))
    if not row or row['token']!=token or row['state'] in TERMINAL or row['state']=='requested':
        raise StaleAttempt('Worker attempt is no longer active')

@contextmanager
def bind(owner, identity, token):
    previous=_lease.set((owner,identity,token))
    try:
        # Correlate without exposing the database fencing token in diagnostics.
        with diagnostic_context(operation_owner=owner, operation_id=identity,
                                attempt_id=sha256(token.encode()).hexdigest()[:32]):
            yield
    finally:
        _lease.reset(previous)

def transition(connection, owner, identity, *, state=None, phase=None, progress=None, publication=None):
    if state is not None and state not in STATES:
        raise ValueError('Invalid operation state')
    if phase is not None:
        _key(phase,'phase',limit=256)
    if progress is not None and (type(progress) not in (int,float) or not math.isfinite(progress) or not 0<=progress<=100):
        raise ValueError('Invalid operation progress')
    if publication is not None and publication not in ('','output-published','catalog-registered','acknowledged'):
        raise ValueError('Invalid publication step')
    with _write(connection):
        validate(connection,owner,identity)
        old=get(connection,owner,identity)
        if old is None:
            raise ValueError('Unknown operation')
        if old['state'] in TERMINAL and (state is None or state!=old['state']):
            raise StaleAttempt('Retry must be explicitly requested')
        if state=='requested' and old['state']!='requested':
            raise ValueError('Use an explicit retry receipt')
        if state in {'started','working'} and not old['attempt']:
            raise ValueError('Claim an attempt before starting work')
        values={k:v for k,v in dict(state=state,phase=phase,progress=progress,publication=publication).items() if v is not None}
        if not values:
            return old
        connection.execute('UPDATE kv_operations SET '+','.join(k+'=?' for k in values)+' WHERE owner=? AND id=?',
            (*values.values(),owner,identity))
        if state:
            connection.execute('UPDATE kv_job_attempts SET state=? WHERE owner=? AND operation_id=? AND number=?',
                (state,owner,identity,old['attempt']))
            if state in TERMINAL and old['state'] not in TERMINAL:
                ended=_one(connection,"SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now') AS value")['value']
                connection.execute("UPDATE kv_operations SET ended_at=? WHERE owner=? AND id=?",(ended,owner,identity))
                connection.execute("UPDATE kv_job_attempts SET ended_at=? WHERE owner=? AND operation_id=? AND number=?",(ended,owner,identity,old['attempt']))
                from services.download_history import finish_operation
                finish_operation(connection,owner,identity,state,ended)
        return get(connection,owner,identity)

def retry(connection, owner, identity, key):
    with _write(connection):
        row=get(connection,owner,identity)
        if not row:
            raise ValueError('Unknown operation')
        existing=_one(connection,'SELECT id FROM kv_job_requests WHERE owner=? AND request_key=?',(owner,key))
        receipt=record_request(connection,owner=owner,kind='retry',request_key=key,payload={'operation_id':identity})
        if existing:
            return row
        if row['state'] not in TERMINAL-{'completed','completed-with-warnings'}:
            raise ValueError('This operation cannot be retried')
        connection.execute("UPDATE kv_operations SET request_id=?,state='requested',ended_at=NULL WHERE owner=? AND id=?",
            (receipt['id'],owner,identity))
        return get(connection,owner,identity)


PROCESS_SCHEMA="""CREATE TABLE IF NOT EXISTS kv_job_processes (
    token TEXT NOT NULL, owner TEXT NOT NULL, operation_id TEXT NOT NULL,
    pid INTEGER NOT NULL, identity TEXT NOT NULL,
    supervisor_pid INTEGER NOT NULL, supervisor_identity TEXT NOT NULL,
    PRIMARY KEY(token,pid,identity), FOREIGN KEY(token) REFERENCES kv_job_attempts(token))"""


def process_identity(pid):
    """Creation identity, never PID alone; missing processes return None."""
    import os
    from pathlib import Path
    if type(pid) is not int or pid<=0:raise ValueError('Invalid worker PID')
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        api=ctypes.WinDLL('kernel32',use_last_error=True)
        api.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD)
        api.OpenProcess.restype=wintypes.HANDLE
        api.GetProcessTimes.argtypes=(wintypes.HANDLE,*([ctypes.POINTER(wintypes.FILETIME)]*4))
        api.GetProcessTimes.restype=wintypes.BOOL
        api.CloseHandle.argtypes=(wintypes.HANDLE,)
        api.GetExitCodeProcess.argtypes=(wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD))
        api.GetExitCodeProcess.restype=wintypes.BOOL
        handle=api.OpenProcess(0x1000,False,pid)
        if not handle:
            error=ctypes.get_last_error()
            if error in (87,1168):return None
            raise OSError(error,'Cannot identify owned worker')
        try:
            code=wintypes.DWORD()
            if not api.GetExitCodeProcess(handle,ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            if code.value!=259:return None
            times=[wintypes.FILETIME() for _ in range(4)]
            if not api.GetProcessTimes(handle,*(ctypes.byref(t) for t in times)):
                raise ctypes.WinError(ctypes.get_last_error())
            return 'windows:'+str((times[0].dwHighDateTime<<32)|times[0].dwLowDateTime)
        finally:
            api.CloseHandle(handle)
    try:
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        raw=Path(f'/proc/{pid}/stat').read_text()
        fields=raw[raw.rfind(')')+2:].split()
        if fields[0]=='Z':return None
        return 'linux:'+boot+':'+fields[19]
    except FileNotFoundError:
        return None


def register_process(connection,owner,identity,pid):
    import os
    born=process_identity(pid)
    if born is None:return
    with _write(connection):
        validate(connection,owner,identity)
        row=get(connection,owner,identity)
        if not row or row['state'] not in ('started','working'):
            raise StaleAttempt('Worker has no active attempt')
        token=_one(connection,'SELECT token FROM kv_job_attempts WHERE owner=? AND operation_id=? AND number=?',
            (owner,identity,row['attempt']))['token']
        connection.execute(PROCESS_SCHEMA)
        connection.execute('INSERT INTO kv_job_processes VALUES(?,?,?,?,?,?,?)',
            (token,owner,identity,pid,born,os.getpid(),process_identity(os.getpid())))


def recover_processes(connection,owner):
    """Stop only a recorded child whose creation identity still matches."""
    import os,signal,subprocess,time
    if not _one(connection,"SELECT name FROM sqlite_master WHERE name='kv_job_processes'"):return
    recorded=rows(connection,'SELECT * FROM kv_job_processes WHERE owner=?',(owner,))
    for row in recorded:
        if process_identity(row['pid'])!=row['identity']:continue
        supervisor=process_identity(row['supervisor_pid'])
        if row['supervisor_pid']!=os.getpid() and supervisor==row['supervisor_identity']:
            raise RuntimeError('Another running application owns this worker')
        if os.name=='nt':
            result=subprocess.run(['taskkill','/PID',str(row['pid']),'/T','/F'],
                capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=10)
            if result.returncode and process_identity(row['pid'])==row['identity']:
                raise RuntimeError('Interrupted worker could not be stopped')
        else:
            # All integrated workers are launched in their own process session.
            try:
                if os.getpgid(row['pid'])!=row['pid']:
                    raise RuntimeError('Owned worker process group changed')
                os.killpg(row['pid'],signal.SIGKILL)
            except ProcessLookupError:
                pass
        deadline=time.monotonic()+5
        while process_identity(row['pid'])==row['identity']:
            if time.monotonic()>deadline:raise RuntimeError('Interrupted worker did not stop')
            time.sleep(.02)

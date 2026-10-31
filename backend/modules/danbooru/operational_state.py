"""Danbooru owns ingest state and durable, bounded tool snapshots."""
from contextlib import contextmanager
import json
from pathlib import Path
from uuid import uuid4
from services import operations
from services import operation_diagnostics as diagnostics
from services.job_store import _write
from services.job_store_migration import initialize, owner_database, prepare

OWNER='danbooru'
STATUSES={'running':'working','cancelling':'working','done':'completed','partial':'completed-with-warnings',
          'error':'failed','cancelled':'cancelled','interrupted':'interrupted'}
TOOL_SCHEMA="""CREATE TABLE IF NOT EXISTS danbooru_tool_runs (
    id TEXT PRIMARY KEY, tool_id TEXT NOT NULL, snapshot TEXT NOT NULL,
    sequence INTEGER NOT NULL UNIQUE)"""

def database():
    from database import get_jobs_db
    return get_jobs_db()

def setup(connection):
    initialize(connection)
    connection.execute(TOOL_SCHEMA)
    connection.commit()

def begin(tool_id,commands,task):
    identity=uuid4().hex
    with database() as db:
        setup(db)
        with _write(db):
            operations.request(db,OWNER,identity,kind='tool',key=identity,
                payload={'tool_id':tool_id,'stages':len(commands)})
            token=operations.claim(db,OWNER,identity)
            number=operations._one(db,'SELECT coalesce(max(sequence),0)+1 AS next FROM danbooru_tool_runs')['next']
            db.execute('INSERT INTO danbooru_tool_runs VALUES(?,?,?,?)',
                (identity,tool_id,json.dumps(task,ensure_ascii=False),number))
    return identity,token

def persist(identity,token,task):
    with database() as db:
        setup(db)
        with _write(db):
            before=operations.get(db,OWNER,identity)
            operations.validate(db,OWNER,identity,token=token)
            state=STATUSES[task['status']]
            total=task.get('total') or 0
            phase_progress=min(1,max(0,(task.get('progress') or 0)/total)) if total>0 else 0
            stages=max(1,task.get('stage_total') or 1)
            progress=min(100,max(0,((task.get('stage_index') or 1)-1+phase_progress)/stages*100))
            operations.transition(db,OWNER,identity,state=state,phase=task.get('stage') or 'running',
                progress=100 if state in {'completed','completed-with-warnings'} else progress)
            # Console tails have already been redacted by the owner.
            db.execute('UPDATE danbooru_tool_runs SET snapshot=? WHERE id=?',
                (json.dumps(task,ensure_ascii=False),identity))
            after=diagnostics.snapshot(db,OWNER,identity)
    if before["state"]!=after["state"] or before["phase"]!=after["phase"]:
        counts=task.get("result_counts") or {}
        diagnostics.committed(OWNER, identity, after, previous=before["state"], counts={
            "processed":sum(counts.values()),"success":counts.get("matched",0),
            "skipped":counts.get("no_match",0),"failed":counts.get("error",0),
            "partial":counts.get("partial",0)})

def snapshot(tool_id):
    import config
    if not config.JOBS_DB_PATH.exists():
        return None
    with database() as db:
        if not operations._one(db,"SELECT name FROM sqlite_master WHERE name='danbooru_tool_runs'"):
            return None
        row=operations._one(db,'SELECT snapshot FROM danbooru_tool_runs WHERE tool_id=? ORDER BY sequence DESC LIMIT 1',(tool_id,))
        return json.loads(row['snapshot']) if row else None

def recover():
    recovered=[]
    with database() as db:
        setup(db)
        operations.recover_processes(db,OWNER)
        with _write(db):
            for row in operations.rows(db,"SELECT n.id,n.snapshot FROM danbooru_tool_runs n JOIN kv_operations o ON o.owner=? AND o.id=n.id WHERE o.state IN ('requested','started','working')",(OWNER,)):
                task=json.loads(row['snapshot'])
                task.update(status='interrupted',cancellable=False)
                operations.transition(db,OWNER,row['id'],state='interrupted')
                db.execute('UPDATE danbooru_tool_runs SET snapshot=? WHERE id=?',(json.dumps(task,ensure_ascii=False),row['id']))
                recovered.append((row["id"],diagnostics.snapshot(db,OWNER,row["id"])))

    for identity,state in recovered:
        diagnostics.committed(OWNER, identity, state, reason="unfinished_work_recovered")

@contextmanager
def index_connection(data_database=None,user_database=None):
    from database import get_data_db,get_user_db
    data_database=data_database or get_data_db
    user_database=user_database or get_user_db
    import config
    from modules.danbooru.schema import INGEST_SCHEMA
    with owner_database(OWNER,'ingest_state',user_database,source_database=data_database,empty_schema=INGEST_SCHEMA):
        pass
    with data_database() as db:
        db.execute('ATTACH DATABASE ? AS operational',(str(config.JOBS_DB_PATH),))
        try:
            yield db
        finally:
            if db.in_transaction:db.rollback()
            db.execute('DETACH DATABASE operational')

def attach_ingest(connection,jobs_path,user_path):
    """Explicit standalone pipeline paths; default CLI behavior stays compatible."""
    if jobs_path is None:
        return
    import sqlite3
    from contextlib import closing
    if connection.in_transaction:
        connection.commit()
    Path(jobs_path).parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(jobs_path)) as jobs, closing(sqlite3.connect(user_path)) as user:
        jobs.execute('PRAGMA journal_mode=WAL')
        jobs.execute('PRAGMA foreign_keys=ON')
        from modules.danbooru.schema import INGEST_SCHEMA
        prepare(jobs,connection,user,owner=OWNER,table='ingest_state',empty_schema=INGEST_SCHEMA)
    connection.execute('ATTACH DATABASE ? AS operational',(str(jobs_path),))

def ingest_table(connection):
    rows=operations.rows(connection,'PRAGMA database_list')
    return 'operational.ingest_state' if any(row['name']=='operational' for row in rows) else 'ingest_state'

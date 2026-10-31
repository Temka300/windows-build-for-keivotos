"""Gallery-dl's supported progress protocol and explicit operational context."""
from contextlib import contextmanager
from pathlib import Path
import ast
import json
import os
import sqlite3
from uuid import uuid4
from services import download_history as history, operations

PREFIX='KEIVOTOS_GALLERY_'
OUTPUT={
 'start':PREFIX+'START:{0!r}\n',
 'success':PREFIX+'SUCCESS:{0!r}\n',
 'skip':PREFIX+'SKIP:{0!r}\n',
 'progress':PREFIX+'UNKNOWN\n',
 'progress-total':PREFIX+'PROGRESS:{3}\n',
}


class Transfer:
    def __init__(self,operation,destination):
        self.operation=operation;self.destination=Path(destination).resolve();self.current=None;self.code=None;self.source=None;self.worker_pid=None

    def record(self,key,progress=None,completed=False,actual_key=None):
        from files_base.serving import resolve_within_source
        path=resolve_within_source(self.destination,actual_key or key,(),must_exist=completed)
        if completed and (not path.is_file() or path.is_symlink() or path.stat().st_size<=0):
            raise ValueError('Unverified gallery-dl media output')
        if not self.operation:return
        with sqlite3.connect(self.operation['jobs']) as db:
            with operations.bind('danbooru',self.operation['id'],self.operation['token']):
                history.ensure_schema(db)
                item_id=history.record(db,'danbooru',self.operation['id'],file_key=key,
                    name=path.name,module_name='Danbooru',progress=progress,completed=completed)
                if self.source:
                    from services import download_controls
                    relative=path.relative_to(Path(self.source.path)).as_posix()
                    paths=[relative] if completed else [relative,relative+'.part']
                    download_controls.bind_target(db,item_id,source_id=self.source.source_id,root=self.source.path,
                        source_role=self.source.role,paths=paths,artwork=relative if completed else '')

    def wait_allowed(self):
        if not self.operation:return
        import time
        from services.download_controls import paused
        while True:
            with sqlite3.connect(self.operation['jobs']) as db:
                operations.validate(db,'danbooru',self.operation['id'],token=self.operation['token'])
                if not paused(db):return
            time.sleep(.1)

    def register(self,pid):
        if not self.operation:return
        from services.download_controls import register_worker
        with sqlite3.connect(self.operation['jobs']) as db:
            register_worker(db,'danbooru',self.operation['id'],pid)
        self.worker_pid=pid

    def unregister(self):
        if self.operation and self.worker_pid:
            from services.download_controls import unregister_worker
            with sqlite3.connect(self.operation['jobs']) as db:
                unregister_worker(db,'danbooru',self.operation['id'],self.worker_pid)

    def output(self,line):
        if not line.startswith(PREFIX):return False
        if len(line)>32768:raise ValueError('Gallery file event exceeds its limit')
        event,_,value=line[len(PREFIX):].strip().partition(':')
        if event in ('START','SUCCESS','SKIP'):
            path=ast.literal_eval(value)
            if not isinstance(path,str):raise ValueError('Invalid gallery-dl file identity')
            target=Path(path)
            if not target.is_absolute():target=self.destination/target
            key=target.resolve().relative_to(self.destination).as_posix()
            if event=='SKIP':
                self.current=None
                return True  # An archive hit is not a new download/date.
            if event=='SUCCESS':
                self.record(self.current or key,completed=True,actual_key=key)
                self.current=None
            else:
                self.current=key
                self.record(key)
        elif event=='PROGRESS':
            if self.current is None:raise ValueError('Gallery progress has no file')
            self.record(self.current,float(value))
        elif event=='UNKNOWN':
            if self.current:self.record(self.current)
        else:raise ValueError('Invalid gallery-dl progress event')
        return True


@contextmanager
def transfer(args,destination):
    raw=os.environ.get('KEIVOTOS_DANBOORU_OPERATION')
    operation=json.loads(raw) if raw else None
    owned=False
    # Unconfigured standalone CLIs remain standalone. An explicit --jobs-db
    # opts into exactly that operational store, never the ambient live profile.
    if operation is None and getattr(args,'jobs_db',None) is not None:
        path=Path(args.jobs_db)
        if not path.is_file():raise ValueError('Choose an initialized operational database')
        with sqlite3.connect(path) as db:
            if not history.has_table(db,'kv_operational_store'):
                raise ValueError('Choose an initialized operational database')
            operations.ensure_schema(db)
            identity=uuid4().hex
            operations.request(db,'danbooru',identity,kind='download',key=identity,payload={'destination':str(destination),'history_cli':1,'supervisor_pid':os.getpid(),'supervisor_identity':operations.process_identity(os.getpid())})
            token=operations.claim(db,'danbooru',identity)
        operation={'id':identity,'token':token,'jobs':str(path)};owned=True
    tracker=Transfer(operation,destination)
    user_path=getattr(args,'user_db',None)
    if user_path is not None and Path(user_path).is_file():
        from files_base import sources
        with sqlite3.connect(Path(user_path).resolve().as_uri()+'?mode=ro',uri=True) as user:
            user.row_factory=sqlite3.Row
            if history.has_table(user,'files_sources'):
                registered=sources.list_sources(user)
                tracker.source=next((source for source in registered if Path(source.path).resolve()==Path(destination).resolve()),None) or sources.nearest_ancestor_source(destination,registered)
    try:
        yield tracker
    except BaseException as exc:
        if owned:
            with sqlite3.connect(operation['jobs']) as db, operations.bind('danbooru',operation['id'],operation['token']):
                if operations.get(db,'danbooru',operation['id'])['state'] not in operations.TERMINAL:
                    operations.transition(db,'danbooru',operation['id'],state='cancelled' if isinstance(exc,KeyboardInterrupt) else 'failed')
        raise
    else:
        if owned:
            with sqlite3.connect(operation['jobs']) as db, operations.bind('danbooru',operation['id'],operation['token']):
                if operations.get(db,'danbooru',operation['id'])['state'] not in operations.TERMINAL:
                    operations.transition(db,'danbooru',operation['id'],state='completed' if tracker.code==0 else 'failed')


def contribute(context):
    if not history.has_table(context.user,'kv_item_captures'):return []
    # Only an explicit recorded download fact qualifies. Local import/index
    # dates (birthtime/ctime/mtime) never become download history.
    return history.select(context.user,"""
      WITH manifests AS (
        SELECT item_id,history_capture(payload_json,payload_sha256) AS facts,
          row_number() OVER(PARTITION BY item_id ORDER BY recorded_at DESC,capture_key DESC) AS position
        FROM kv_item_captures WHERE namespace='portable.manifest' AND format_version=1
      )
      SELECT history_id('danbooru',m.item_id,a.key) AS id,'danbooru' AS owner,'Danbooru' AS module_name,
        history_name(json_extract(a.value,'$.path')) AS name,'completed' AS state,100 AS progress,
        NULL AS started_at,NULL AS completed_at,history_time(json_extract(m.facts,'$.origin.downloaded_at')) AS date_at,
        CASE WHEN history_time(json_extract(m.facts,'$.origin.downloaded_at')) IS NULL THEN 'unknown' ELSE 'recorded-manifest' END AS date_source,
        0 AS attempt,json_extract(l.payload_json,'$.source_id') AS _source_id,
        CASE WHEN coalesce(json_extract(l.payload_json,'$.root'),'')=''
          THEN json_extract(a.value,'$.path')
          ELSE json_extract(l.payload_json,'$.root')||'/'||json_extract(a.value,'$.path') END AS _media_path
        FROM manifests m JOIN json_each(m.facts,'$.assets') a
        LEFT JOIN kv_item_captures l ON l.item_id=m.item_id AND l.namespace='portable.location'
          AND l.rowid=(SELECT max(x.rowid) FROM kv_item_captures x WHERE x.item_id=m.item_id AND x.namespace='portable.location')
        WHERE m.position=1 AND json_extract(m.facts,'$.item.owner')='danbooru'
          AND json_extract(m.facts,'$.origin.downloaded_at') IS NOT NULL
          AND json_extract(a.value,'$.role')='media'
          AND NOT history_tracked('danbooru','portable:'||m.item_id)
    """,context)


def recover(connection):
    """Only explicit history CLIs whose original supervisor has exited."""
    if not history.has_table(connection,'kv_operations'):return
    for row in operations.rows(connection,"""SELECT o.id,r.payload_json FROM kv_operations o
        JOIN kv_job_requests r ON r.id=o.request_id WHERE o.owner='danbooru'
        AND o.state IN ('started','working') AND r.kind='download'"""):
        payload=json.loads(row['payload_json'])
        if payload.get('history_cli')!=1:continue
        pid=payload.get('supervisor_pid');birth=payload.get('supervisor_identity')
        if type(pid) is not int or not isinstance(birth,str):continue
        if operations.process_identity(pid)!=birth:
            operations.transition(connection,'danbooru',row['id'],state='interrupted')


def controls():
    from services.download_controls import OwnerControls
    return OwnerControls(cancel_active)


def cancel_active(row):
    from modules.danbooru import operational_state,tools
    with operational_state.database() as db:
        op=operations.get(db,'danbooru',row['operation_id'])
        worker=history._one(db,'SELECT * FROM kv_download_workers WHERE owner=? AND operation_id=? AND attempt=?',
            ('danbooru',row['operation_id'],row['attempt']))
        tool=history._one(db,'SELECT tool_id FROM danbooru_tool_runs WHERE id=?',(row['operation_id'],)) if history.has_table(db,'danbooru_tool_runs') else None
        if op is None or op['attempt']!=row['attempt'] or op['state'] not in ('started','working'):
            raise ValueError('Download finished or changed')
    if tool:
        tools.cancel_download_attempt(tool['tool_id'],worker['supervisor'])
    else:
        with operational_state.database() as db:
            operations.transition(db,'danbooru',row['operation_id'],state='cancelled')

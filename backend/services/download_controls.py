"""Suite download coordination; engines and cancellation remain owner-controlled."""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from uuid import UUID
from services import download_history as history, operations
from services.job_store import _one, _write

_LOCK=threading.RLock()
TABLES=('kv_download_gate','kv_download_workers','kv_download_targets','kv_download_deleted','kv_download_actions')
SCHEMA=(
 """CREATE TABLE kv_download_gate(id INTEGER PRIMARY KEY CHECK(id=1),paused INTEGER NOT NULL,revision INTEGER NOT NULL)""",
 """CREATE TABLE kv_download_workers(owner TEXT NOT NULL,operation_id TEXT NOT NULL,attempt INTEGER NOT NULL,
    pid INTEGER NOT NULL,birth TEXT NOT NULL,supervisor INTEGER NOT NULL,supervisor_birth TEXT NOT NULL,
    suspended TEXT NOT NULL DEFAULT '[]',PRIMARY KEY(owner,operation_id,attempt))""",
 """CREATE TABLE kv_download_targets(item_id TEXT PRIMARY KEY,owner TEXT NOT NULL,operation_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,source_id TEXT NOT NULL,root TEXT NOT NULL,source_role TEXT NOT NULL,paths TEXT NOT NULL,
    artwork TEXT NOT NULL DEFAULT '',published INTEGER NOT NULL DEFAULT 0)""",
 """CREATE TABLE kv_download_deleted(item_id TEXT PRIMARY KEY,recorded_at TEXT NOT NULL)""",
 """CREATE TABLE kv_download_actions(request_id TEXT PRIMARY KEY,action TEXT NOT NULL,subject TEXT NOT NULL,
    version TEXT NOT NULL,result TEXT NOT NULL,plan TEXT NOT NULL DEFAULT '')""",
)


@dataclass(frozen=True)
class OwnerControls:
    cancel: object



@contextmanager
def _database(path,*,initialize=False):
    from database_connections import connect_database
    path=Path(path)
    if initialize:path.parent.mkdir(parents=True,exist_ok=True)
    if not path.is_file() and not initialize:raise ValueError('Download history is unavailable')
    with connect_database(path) as db:
        if initialize:
            from services.job_store_migration import initialize as initialize_store
            initialize_store(db)
        yield db


@contextmanager
def _user(path):
    from database_connections import read_existing_databases
    with read_existing_databases((path,)) as (db,):
        yield db


def check_schema(db):
    present=[name for name in TABLES if history.has_table(db,name)]
    if not present:return False
    if len(present)!=len(TABLES):raise ValueError('Incomplete download controls')
    gate=_one(db,'SELECT paused,revision FROM kv_download_gate WHERE id=1')
    if gate is None or gate['paused'] not in (0,1) or type(gate['revision']) is not int or gate['revision']<0:
        raise ValueError('Unsupported download pause state')
    db.execute('SELECT owner,operation_id,attempt,pid,birth,supervisor,supervisor_birth,suspended FROM kv_download_workers LIMIT 0')
    db.execute('SELECT item_id,owner,operation_id,attempt,source_id,root,source_role,paths,artwork,published FROM kv_download_targets LIMIT 0')
    db.execute('SELECT item_id,recorded_at FROM kv_download_deleted LIMIT 0')
    db.execute('SELECT request_id,action,subject,version,result,plan FROM kv_download_actions LIMIT 0')
    return True


def ensure_schema(db):
    if check_schema(db):return
    for sql in SCHEMA:db.execute(sql)
    db.execute('INSERT INTO kv_download_gate VALUES(1,0,0)')


def paused(db):
    if not history.has_table(db,'kv_download_gate'):return False
    row=_one(db,'SELECT paused FROM kv_download_gate WHERE id=1')
    return bool(row and row['paused'])


def state(jobs_path):
    from database_connections import read_existing_databases
    with read_existing_databases((jobs_path,)) as (db,):
        if db is None or not check_schema(db):return {'paused':False,'revision':0}
        result=_one(db,'SELECT paused,revision FROM kv_download_gate WHERE id=1')
        result['paused']=bool(result['paused'])
        return result


def register_worker(db,owner,operation_id,pid):
    from services import process_control
    operations.validate(db,owner,operation_id)
    operation=operations.get(db,owner,operation_id)
    if operation is None or operation['state'] not in ('started','working') or not operation['attempt']:
        raise operations.StaleAttempt('Download attempt is not active')
    birth=operations.process_identity(pid);parent=operations.process_identity(os.getpid())
    if not birth or not parent:raise ValueError('Download child has no process identity')
    history.ensure_schema(db)
    with _write(db):
        prior=_one(db,'SELECT * FROM kv_download_workers WHERE owner=? AND operation_id=? AND attempt=?',
            (owner,operation_id,operation['attempt']))
        if prior and process_control.alive(prior['pid'],prior['birth']):
            if (prior['pid'],prior['birth'])==(pid,birth):return
            raise ValueError('Another child still owns this download attempt')
        db.execute('INSERT OR REPLACE INTO kv_download_workers VALUES(?,?,?,?,?,?,?,?)',
            (owner,operation_id,operation['attempt'],pid,birth,os.getpid(),parent,'[]'))
        if paused(db):
            suspended=process_control.suspend(pid,birth)
            db.execute('UPDATE kv_download_workers SET suspended=? WHERE owner=? AND operation_id=? AND attempt=?',
                (json.dumps(suspended),owner,operation_id,operation['attempt']))

def unregister_worker(db,owner,operation_id,pid):
    if history.has_table(db,'kv_download_workers'):
        from services import process_control
        worker=_one(db,'SELECT * FROM kv_download_workers WHERE owner=? AND operation_id=? AND pid=?',(owner,operation_id,pid))
        if worker and not process_control.alive(worker['pid'],worker['birth']):
            process_control.stop_records(json.loads(worker['suspended']))
        with _write(db):db.execute('DELETE FROM kv_download_workers WHERE owner=? AND operation_id=? AND pid=?',(owner,operation_id,pid))


def recover_workers(db,owner):
    """An exited original supervisor cannot leave a suspended orphan behind."""
    from services import process_control
    if not history.has_table(db,'kv_download_workers'):return
    for worker in operations.rows(db,'SELECT * FROM kv_download_workers WHERE owner=?',(owner,)):
        if operations.process_identity(worker['supervisor'])==worker['supervisor_birth']:continue
        if process_control.alive(worker['pid'],worker['birth']):
            process_control.stop(worker['pid'],worker['birth'])
        process_control.stop_records(json.loads(worker['suspended']))
        unregister_worker(db,owner,worker['operation_id'],worker['pid'])


def bind_target(db,item_id,*,source_id,root,source_role,paths=(),artwork='',published=False):
    """Owner supplies exact intended media paths, never an arbitrary folder sweep."""
    from storage_layout import portable_relative
    row=_one(db,'SELECT * FROM kv_download_items WHERE id=?',(item_id,))
    if row is None:raise ValueError('Unknown download media')
    clean=[]
    for name in paths:
        name=portable_relative(name)
        if len(name)>4096:raise ValueError('Download path exceeds its limit')
        if name not in clean:clean.append(name)
    if len(clean)>32:raise ValueError('Too many media components')
    if artwork:artwork=portable_relative(artwork)
    root=str(Path(root).resolve())
    prior=_one(db,'SELECT * FROM kv_download_targets WHERE item_id=?',(item_id,))
    same=prior and (prior['owner'],prior['operation_id'],prior['attempt'],prior['root'],prior['source_id'])==(
        row['owner'],row['operation_id'],row['attempt'],root,source_id)
    if prior and prior['published'] and not published:return
    if same:clean=list(dict.fromkeys([*json.loads(prior['paths']),*clean]))
    if not artwork and same:artwork=prior['artwork']
    with _write(db):
        db.execute('INSERT INTO kv_download_targets VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET '
            'operation_id=excluded.operation_id,attempt=excluded.attempt,source_id=excluded.source_id,root=excluded.root,'
            'source_role=excluded.source_role,paths=excluded.paths,artwork=excluded.artwork,published=excluded.published',
            (item_id,row['owner'],row['operation_id'],row['attempt'],source_id,root,source_role,json.dumps(clean),artwork,int(published)))


def version(row):
    return sha256(json.dumps([row['id'],row['owner'],row['operation_id'],row['attempt']],separators=(',',':')).encode()).hexdigest()


def _active(db,row):
    if row is None or row['state']!='working':return False
    op=operations.get(db,row['owner'],row['operation_id'])
    return bool(op and op['state'] in {'started','working'} and op['attempt']==row['attempt'])


def _worker(db,row):
    if not history.has_table(db,'kv_download_workers'):return None
    return _one(db,'SELECT * FROM kv_download_workers WHERE owner=? AND operation_id=? AND attempt=?',
                (row['owner'],row['operation_id'],row['attempt']))


def decorate(db,row,registry):
    """Capability fields are passive. Legacy/unavailable owners remain history."""
    from services import process_control
    row=dict(row);row['control_version']=None;row['can_delete']=False;row['thumbnail_url']=None
    target=None
    if db is not None and history.has_table(db,'kv_download_targets'):
        target=_one(db,'SELECT * FROM kv_download_targets WHERE item_id=?',(row['id'],))
        raw=_one(db,'SELECT * FROM kv_download_items WHERE id=?',(row['id'],))
        if target:
            if target['artwork'] or json.loads(target['paths']):row['thumbnail_url']='/api/downloads/'+row['id']+'/thumbnail?v='+sha256(json.dumps([target['paths'],target['artwork'],row['progress'],row['completed_at']]).encode()).hexdigest()[:16]
        descriptor=registry.get(row['owner'])
        if raw and descriptor and descriptor.download_control_provider and _active(db,raw) and target and json.loads(target['paths']) and not target['published']:
            worker=_worker(db,raw)
            if worker and process_control.alive(worker['pid'],worker['birth']):
                row['can_delete']=True;row['control_version']=version(raw)
    if row.get('_source_id') and row.get('_media_path'):row['thumbnail_url']='/api/downloads/'+row['id']+'/thumbnail'
    return row


def _owner(registry,slug):
    descriptor=registry.get(slug)
    if descriptor is None or descriptor.download_control_provider is None:
        raise ValueError('Download owner is unavailable')
    return descriptor.download_control_provider()


def _request(value):
    return str(UUID(str(value)))


def set_paused(*,jobs_path,paused_value,request_id):
    from services import process_control
    request_id=_request(request_id)
    if type(paused_value) is not bool:raise ValueError('Invalid pause state')
    with _LOCK,_database(jobs_path,initialize=True) as db:
        history.ensure_schema(db)
        prior=_one(db,'SELECT * FROM kv_download_actions WHERE request_id=?',(request_id,))
        action='pause' if paused_value else 'resume'
        if prior:
            if prior['action']!=action:raise ValueError('Control request describes another action')
            return json.loads(prior['result'])
        changed=[]
        with _write(db):
            old=paused(db)
            db.execute('UPDATE kv_download_gate SET paused=?,revision=revision+1 WHERE id=1',(int(paused_value),))
            workers=operations.rows(db,'SELECT * FROM kv_download_workers')
        try:
            for worker in workers:
                if not process_control.alive(worker['pid'],worker['birth']):continue
                if operations.process_identity(worker['supervisor'])!=worker['supervisor_birth']:
                    raise ValueError('Download supervisor changed')
                saved=json.loads(worker['suspended'])
                if paused_value and not saved:
                    saved=process_control.suspend(worker['pid'],worker['birth']);changed.append((worker,'pause',saved))
                elif not paused_value and saved:
                    process_control.resume(saved);changed.append((worker,'resume',saved));saved=[]
                with _write(db):db.execute('UPDATE kv_download_workers SET suspended=? WHERE owner=? AND operation_id=? AND attempt=?',
                    (json.dumps(saved),worker['owner'],worker['operation_id'],worker['attempt']))
        except BaseException:
            for worker,action,records in reversed(changed):
                if action=='pause':
                    process_control.resume(records);restored=[]
                else:
                    restored=process_control.suspend(worker['pid'],worker['birth']) if process_control.alive(worker['pid'],worker['birth']) else []
                with _write(db):db.execute('UPDATE kv_download_workers SET suspended=? WHERE owner=? AND operation_id=? AND attempt=?',
                    (json.dumps(restored),worker['owner'],worker['operation_id'],worker['attempt']))
            with _write(db):db.execute('UPDATE kv_download_gate SET paused=? WHERE id=1',(int(old),))
            raise
        result=_one(db,'SELECT paused,revision FROM kv_download_gate WHERE id=1');result['paused']=bool(result['paused'])
        with _write(db):db.execute('INSERT INTO kv_download_actions VALUES(?,?,?,?,?,?)',
            (request_id,action,'','',json.dumps(result),''))
        return result


def _resolve(target,user,relative,*,home,must_exist=True):
    """Fresh Files/root policy plus strict non-linked asset components."""
    from files_base import sources,serving
    from storage_layout import portable_path
    if user is not None:user.row_factory=sqlite3.Row
    registered=sources.list_sources(user) if history.has_table(user,'files_sources') else []
    source=next((s for s in registered if s.source_id==target['source_id'] and s.visible),None)
    if source is None:
        raise ValueError('Download source changed or is unavailable')
    if not target.get('published') and str(Path(source.path).resolve())!=target['root']:
        raise ValueError('Download source changed or is unavailable')
    path=portable_path(Path(source.path).resolve(),relative,(Path(home),),must_exist=must_exist)
    nearest=sources.nearest_ancestor_source(path,registered)
    if nearest is None or nearest.source_id!=source.source_id or not target.get('published') and source.role!=target.get('source_role',source.role):
        raise ValueError('Download target belongs to another source')
    return path



def _unfinished(target,user,relative,*,home):
    # Sidecars, manifests, resume metadata and gallery archives are preserved.
    if Path(relative).name.lower().endswith(('.json','.jsonl','.txt','.sqlite','.sqlite3','.db','.ytdl','.yaml','.yml','.toml')):
        raise ValueError('Metadata cannot be deleted from Downloads')
    return _resolve(target,user,relative,home=home)


def _fingerprint(path):
    value=path.stat()
    return [value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns]


def thumbnail(*,item_id,jobs_path,user_path,home,registry):
    from database_connections import read_existing_databases
    from thumbnails import ensure_thumbnail
    with read_existing_databases((jobs_path,user_path)) as (db,user):
        if db is None or user is None:return None
        if history.has_table(db,'kv_download_targets'):
            target=_one(db,'SELECT * FROM kv_download_targets WHERE item_id=?',(item_id,))
            if target:
                choices=[target['artwork'],*json.loads(target['paths'])]
                for relative in choices:
                    if not relative:continue
                    try:
                        path=_resolve(target,user,relative,home=home)
                        result=ensure_thumbnail(str(path),max_size=200)
                        if result:return result
                    except (OSError,ValueError):continue
    # Legacy contributions resolve their current source through the same guard.
    record=history.page(jobs_path=jobs_path,user_path=user_path,registry=registry,limit=1,item_id=item_id,internal=True)['items']
    if not record:return None
    row=record[0]
    if not row.get('_source_id') or not row.get('_media_path'):return None
    with read_existing_databases((user_path,)) as (user,):
        from files_base import sources
        if user is not None:user.row_factory=sqlite3.Row
        source=sources.get_source(user,row['_source_id']) if user else None
        if source is None:return None
        target=dict(source_id=source.source_id,root=str(Path(source.path).resolve()),owner=row['owner'])
        path=_resolve(target,user,row['_media_path'],home=home)
        return ensure_thumbnail(str(path),max_size=200)



def delete_active(*,item_id,expected_version,request_id,confirm,jobs_path,user_path,home,registry):
    """Drain before preparing exact file identities; never hold owner DB locks."""
    from services import process_control
    request_id=_request(request_id)
    with _LOCK:
        with _database(jobs_path) as db:
            history.ensure_schema(db)
            action=_one(db,'SELECT * FROM kv_download_actions WHERE request_id=?',(request_id,))
            if action:
                if (action['action'],action['subject'],action['version'])!=('delete',item_id,expected_version):
                    raise ValueError('Control request describes another download')
                result=json.loads(action['result'])
                if result.get('deleted') or not confirm:return result
                plan=json.loads(action['plan'])
            else:
                if confirm:raise ValueError('Preview the unfinished file count before confirmation')
                row=_one(db,'SELECT * FROM kv_download_items WHERE id=?',(item_id,))
                target=_one(db,'SELECT * FROM kv_download_targets WHERE item_id=?',(item_id,))
                if not _active(db,row) or version(row)!=expected_version or target is None or target['published']:
                    raise ValueError('Download already finished or changed')
                worker=_worker(db,row)
                if not worker or not process_control.alive(worker['pid'],worker['birth']):
                    raise ValueError('Download is no longer active')
        if action is None:
            # Owner publication and cancellation serialize through the owner's
            # existing lock. The coordinator holds no SQLite/restore read lease.
            _owner(registry,row['owner']).cancel(row)
            if process_control.alive(worker['pid'],worker['birth']):
                process_control.stop(worker['pid'],worker['birth'])
            with _database(jobs_path) as db:
                current=_one(db,'SELECT * FROM kv_download_items WHERE id=?',(item_id,))
                op=operations.get(db,row['owner'],row['operation_id'])
                target=_one(db,'SELECT * FROM kv_download_targets WHERE item_id=?',(item_id,))
                if current['state']=='completed' or target['published'] or op['attempt']!=row['attempt']:
                    raise ValueError('Download finished or changed before deletion')
            files=[]
            with _user(user_path) as user:
                for relative in json.loads(target['paths']):
                    try:path=_unfinished(target,user,relative,home=home)
                    except FileNotFoundError:continue
                    except Exception as exc:
                        from files_base.serving import ServeDenied
                        if isinstance(exc,ServeDenied) and exc.status_code==404:continue
                        raise
                    if not path.is_file():raise ValueError('Unfinished media is not a regular file')
                    files.append({'relative':relative,'identity':_fingerprint(path)})
            plan={'target':target,'files':files,'attempt':row['attempt'],'operation_id':row['operation_id']}
            result={'deleted':False,'requires_confirmation':len(files)>1,'file_count':len(files)}
            with _database(jobs_path) as db,_write(db):
                db.execute('INSERT INTO kv_download_actions VALUES(?,?,?,?,?,?)',
                    (request_id,'delete',item_id,expected_version,json.dumps(result),json.dumps(plan)))
            if len(files)>1 and not confirm:return result
        with _database(jobs_path) as db:
            op=operations.get(db,plan['target']['owner'],plan['operation_id'])
            current=_one(db,'SELECT * FROM kv_download_items WHERE id=?',(item_id,))
            if current['state']=='completed' or op['attempt']!=plan['attempt']:
                raise ValueError('Download changed after confirmation')
        with _user(user_path) as user:
            targets=[]
            for file in plan['files']:
                try:path=_unfinished(plan['target'],user,file['relative'],home=home)
                except Exception as exc:
                    from files_base.serving import ServeDenied
                    if isinstance(exc,FileNotFoundError) or isinstance(exc,ServeDenied) and exc.status_code==404:continue
                    raise
                if _fingerprint(path)!=file['identity']:raise ValueError('Unfinished file changed after confirmation')
                targets.append(path)
            for path in targets:path.unlink()
        result={'deleted':True,'requires_confirmation':False,'file_count':len(plan['files'])}
        with _database(jobs_path) as db,_write(db):
            db.execute('INSERT OR IGNORE INTO kv_download_deleted VALUES(?,?)',(item_id,history.now()))
            db.execute('UPDATE kv_download_actions SET result=? WHERE request_id=?',(json.dumps(result),request_id))
        return result

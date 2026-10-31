"""Per-media operational facts. Reads never initialize an owner or store."""
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import base64
import json
import math
from uuid import NAMESPACE_URL, uuid5
from services.job_store import _key, _name, _one, _write

TABLES = ('kv_download_schema','kv_download_items','kv_download_links','kv_download_attempts')
SCHEMA = (
    'CREATE TABLE kv_download_schema(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL)',
    """CREATE TABLE kv_download_links(owner TEXT NOT NULL,operation_id TEXT NOT NULL,
        lineage TEXT NOT NULL,PRIMARY KEY(owner,operation_id))""",
    """CREATE TABLE kv_download_items(id TEXT PRIMARY KEY,owner TEXT NOT NULL,module_name TEXT NOT NULL,
        lineage TEXT NOT NULL,file_key TEXT NOT NULL,name TEXT NOT NULL,
        operation_id TEXT NOT NULL,attempt INTEGER NOT NULL,state TEXT NOT NULL,
        progress REAL,started_at TEXT,completed_at TEXT,UNIQUE(owner,lineage,file_key))""",
    """CREATE TABLE kv_download_attempts(item_id TEXT NOT NULL,operation_id TEXT NOT NULL,
        attempt INTEGER NOT NULL,state TEXT NOT NULL,progress REAL,started_at TEXT,ended_at TEXT,
        PRIMARY KEY(item_id,operation_id,attempt),FOREIGN KEY(item_id) REFERENCES kv_download_items(id))""",
    "CREATE INDEX kv_download_order ON kv_download_items(coalesce(completed_at,started_at,'') DESC,id DESC)",
    'CREATE INDEX kv_download_operation ON kv_download_items(owner,operation_id)',
)
PUBLIC = ('id','owner','module_name','name','state','progress','started_at',
          'completed_at','date_at','date_source','attempt')


def timestamp(value):
    """Accept timezone-aware saved instants, never guessed local time."""
    if not isinstance(value,str) or len(value)>64:return None
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.utcoffset() is None:return None
        return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
    except (ValueError,OverflowError):return None


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')


def identity(owner,lineage,file_key):
    return str(uuid5(NAMESPACE_URL,json.dumps([owner,lineage,file_key],ensure_ascii=True)))


def has_table(connection,name):
    return connection is not None and connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone() is not None


def check_schema(connection):
    present=[name for name in TABLES if has_table(connection,name)]
    if not present:return False
    if len(present)!=len(TABLES) or _one(connection,'SELECT * FROM kv_download_schema') not in ({'id':1,'version':1},{'id':1,'version':2}):
        raise ValueError('Unsupported download history schema')
    connection.execute('SELECT id,owner,module_name,lineage,file_key,name,operation_id,attempt,state,progress,started_at,completed_at FROM kv_download_items LIMIT 0')
    connection.execute('SELECT owner,operation_id,lineage FROM kv_download_links LIMIT 0')
    connection.execute('SELECT item_id,operation_id,attempt,state,progress,started_at,ended_at FROM kv_download_attempts LIMIT 0')
    if _one(connection,'SELECT version FROM kv_download_schema')['version']==2:
        from services import download_controls
        if not download_controls.check_schema(connection):raise ValueError('Missing download controls')
    return True


def ensure_schema(connection):
    with _write(connection):
        if not check_schema(connection):
            for sql in SCHEMA:connection.execute(sql)
            connection.execute('INSERT INTO kv_download_schema VALUES(1,1)')
        from services import download_controls
        download_controls.ensure_schema(connection)
        connection.execute('UPDATE kv_download_schema SET version=2 WHERE id=1')


def link(connection,owner,operation_id,*,previous=None):
    """A retry points to its predecessor's lineage, not another visible item."""
    _name(owner,'owner');_key(operation_id,'operation ID')
    with _write(connection):
        prior=_one(connection,'SELECT lineage FROM kv_download_links WHERE owner=? AND operation_id=?',(owner,operation_id))
        if prior:return prior['lineage']
        lineage=operation_id
        if previous:
            _key(previous,'previous operation ID')
            parent=_one(connection,'SELECT lineage FROM kv_download_links WHERE owner=? AND operation_id=?',(owner,previous))
            lineage=parent['lineage'] if parent else previous
            connection.execute('INSERT OR IGNORE INTO kv_download_links VALUES(?,?,?)',(owner,previous,lineage))
        connection.execute('INSERT INTO kv_download_links VALUES(?,?,?)',(owner,operation_id,lineage))
        return lineage


def record(connection,owner,operation_id,*,file_key,name,module_name,progress=None,completed=False,at=None,token=None):
    """Validated worker events join the owner's transaction and attempt fence."""
    from services import operations
    _key(file_key,'media key',limit=4096)
    if not isinstance(name,str) or not name.strip() or len(name)>1024 or '\0' in name:
        raise ValueError('Invalid download name')
    _key(module_name,'module name',limit=128)
    if type(completed) is not bool:raise ValueError('Invalid completion flag')
    if progress is not None and (type(progress) not in (int,float) or not math.isfinite(progress) or not 0<=progress<=100):
        raise ValueError('Invalid file progress')
    stamp=now() if at is None else timestamp(at)
    if stamp is None:raise ValueError('Invalid file event time')
    with _write(connection):
        operations.validate(connection,owner,operation_id,token=token)
        operation=operations.get(connection,owner,operation_id)
        if not operation or operation['state'] not in {'started','working'} or not operation['attempt']:
            raise operations.StaleAttempt('Download attempt is not active')
        lineage=link(connection,owner,operation_id)
        item_id=identity(owner,lineage,file_key)
        prior=_one(connection,'SELECT * FROM kv_download_items WHERE id=?',(item_id,))
        # Registration repair/repeated completion cannot re-date completed media.
        if prior and prior['state']=='completed':
            # A real retry can transfer the same media again. Preserve its
            # attempt evidence while retaining the first verified visible item.
            attempt=_one(connection,'SELECT * FROM kv_download_attempts WHERE item_id=? AND operation_id=? AND attempt=?',
                (item_id,operation_id,operation['attempt']))
            if attempt and attempt['state']=='completed':return item_id
            state='completed' if completed else 'working'
            percent=100 if completed else min(progress,99) if progress is not None else None
            connection.execute('''INSERT INTO kv_download_attempts VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(item_id,operation_id,attempt) DO UPDATE SET state=excluded.state,
                progress=excluded.progress,ended_at=excluded.ended_at''',
                (item_id,operation_id,operation['attempt'],state,percent,
                 None if completed else stamp,stamp if completed else None))
            return item_id
        percent=100 if completed else min(progress,99) if progress is not None else None
        same=prior and (prior['operation_id'],prior['attempt'])==(operation_id,operation['attempt'])
        if same and percent is not None and prior['progress'] is not None:percent=max(percent,prior['progress'])
        started=prior['started_at'] if prior and prior['started_at'] else None if completed else stamp
        if completed and started and stamp<started:raise ValueError('Completion precedes download start')
        state='completed' if completed else 'working'
        connection.execute("""INSERT INTO kv_download_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name,operation_id=excluded.operation_id,
            attempt=excluded.attempt,state=excluded.state,progress=excluded.progress,
            completed_at=excluded.completed_at""",
            (item_id,owner,module_name,lineage,file_key,name,operation_id,operation['attempt'],
             state,percent,started,stamp if completed else None))
        connection.execute("""INSERT INTO kv_download_attempts VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(item_id,operation_id,attempt) DO UPDATE SET state=excluded.state,
            progress=excluded.progress,ended_at=excluded.ended_at""",
            (item_id,operation_id,operation['attempt'],state,percent,None if completed else stamp,stamp if completed else None))
        return item_id


def finish_operation(connection,owner,operation_id,state,ended_at):
    """Never invent success or cancel completed sibling files."""
    if not has_table(connection,'kv_download_items'):return
    outcome=state if state in {'failed','cancelled','interrupted'} else 'interrupted'
    connection.execute("UPDATE kv_download_attempts SET state=?,ended_at=? WHERE operation_id=? AND item_id IN "
        "(SELECT id FROM kv_download_items WHERE owner=?) AND state='working'",
        (outcome,ended_at,operation_id,owner))
    connection.execute("UPDATE kv_download_items SET state=? WHERE owner=? AND operation_id=? AND state!='completed'",
                       (outcome,owner,operation_id))


@dataclass(frozen=True)
class ReadContext:
    jobs: object
    user: object
    limit: int
    before: tuple | None
    owner: str | None
    item_id: str | None = None
    current: tuple = ()


def select(connection,sql,context,args=()):
    """Owner SQL supplies a normalized view; each contribution is bounded."""
    if connection is None:return []
    where=['NOT history_hidden(id)'];parameters=list(args)
    if context.item_id:where.append('id=?');parameters.append(context.item_id)
    if context.before:
        where.append("(coalesce(date_at,''),id)<(?,?)");parameters.extend(context.before)
    if context.owner:where.append('owner=?');parameters.append(context.owner)
    query='SELECT * FROM ('+sql+')'
    if where:query+=' WHERE '+' AND '.join(where)
    query+=" ORDER BY coalesce(date_at,'') DESC,id DESC LIMIT ?"
    parameters.append(context.limit+1)
    cursor=connection.execute(query,parameters)
    names=[column[0] for column in cursor.description]
    return [dict(zip(names,row)) if not isinstance(row,dict) else row for row in cursor]


def _cursor(value,owner):
    if value is None:return None
    try:
        if len(value)>1024:raise ValueError()
        version,scope,stamp,item_id=json.loads(base64.urlsafe_b64decode(value+'='*(-len(value)%4)))
        if type(version) is not int or version!=1 or scope!=owner or not isinstance(stamp,str) or stamp and timestamp(stamp)!=stamp:raise ValueError()
        _key(item_id,'history cursor ID',limit=128)
        return stamp,item_id
    except (ValueError,TypeError,UnicodeError):
        raise ValueError('Invalid download history cursor') from None


def page(*,jobs_path,user_path,registry,limit=100,after=None,owner=None,item_id=None,internal=False):
    from database_connections import read_existing_databases
    if type(limit) is not int or not 1<=limit<=200:raise ValueError('Invalid history page size')
    if owner is not None:_name(owner,'owner')
    before=_cursor(after,owner)
    with ExitStack() as stack:
        jobs,user=stack.enter_context(read_existing_databases((jobs_path,user_path)))
        present=check_schema(jobs) if jobs is not None else False
        def tracked(module,operation):
            return bool(present and _one(jobs,'SELECT 1 AS present FROM kv_download_links l JOIN kv_download_items i ON i.owner=l.owner AND i.lineage=l.lineage WHERE l.owner=? AND l.operation_id=? LIMIT 1',(module,operation)))
        def hidden(item_id):
            return bool(has_table(jobs,'kv_download_deleted') and _one(jobs,
                'SELECT 1 AS present FROM kv_download_deleted WHERE item_id=?',(item_id,)))
        def operational(module,operation):
            return bool(has_table(jobs,'kv_operations') and _one(jobs,
                'SELECT 1 AS present FROM kv_operations WHERE owner=? AND id=?',(module,operation)))
        def captured(raw,digest):
            if not isinstance(raw,str) or sha256(raw.encode()).hexdigest()!=digest:
                raise ValueError('Changed preserved download facts')
            return raw
        for connection in (jobs,user):
            if connection is not None:
                connection.create_function('history_time',1,timestamp,deterministic=True)
                connection.create_function('history_id',3,identity,deterministic=True)
                connection.create_function('history_tracked',2,tracked)
                connection.create_function('history_hidden',1,hidden)
                connection.create_function('history_operational',2,operational)
                connection.create_function('history_capture',2,captured,deterministic=True)
                connection.create_function('history_name',1,lambda path:str(path or '').replace('\\','/').rsplit('/',1)[-1],deterministic=True)
        context=ReadContext(jobs,user,limit,before,owner,item_id)
        rows=select(jobs,"SELECT id,owner,module_name,name,state,progress,started_at,completed_at,"
            "coalesce(completed_at,started_at) AS date_at,'transfer' AS date_source,attempt,"
            "operation_id AS _operation_id,lineage AS _lineage FROM kv_download_items",context) if present else []
        context=replace(context,current=tuple(dict(row) for row in rows))
        unique={row['id']:row for row in rows}
        for descriptor,provider in registry.download_history_contributions():
            if owner is not None and owner!=descriptor.slug:continue
            for row in provider(context):
                current=unique.get(row['id'])
                if current is None:unique[row['id']]=row
                elif current['owner']==row['owner']==descriptor.slug:
                    # Owner presentation may supply its saved title, never replace
                    # recorded dates, progress, attempt or completion facts.
                    name=row.get('name')
                    if isinstance(name,str) and name.strip() and len(name)<=1024 and '\0' not in name:
                        unique[row['id']]={**current,'name':name}
        ordered=sorted(unique.values(),key=lambda row:(row['date_at'] or '',row['id']),reverse=True)
        from services.download_controls import decorate
        selected=[]
        for row in ordered[:limit]:
            row=decorate(jobs,row,registry)
            selected.append(row if internal else {key:row[key] for key in (*PUBLIC,'can_delete','control_version','thumbnail_url')})
        cursor=None
        if len(ordered)>limit:
            last=selected[-1]
            cursor=base64.urlsafe_b64encode(json.dumps([1,owner,last['date_at'] or '',last['id']]).encode()).decode().rstrip('=')
        return {'items':selected,'next_cursor':cursor}

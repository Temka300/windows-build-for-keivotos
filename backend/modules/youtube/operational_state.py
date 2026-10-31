"""YouTube's operational rows and preserved completed-library bridge."""
from contextlib import contextmanager
from services import operations
from services.search_text import literal_contains
from services.job_store_migration import owner_database, preserve_sources
from modules.youtube import metadata

OWNER='youtube'
TABLE='youtube_downloads'
STATES={'queued':'requested','downloading':'working','merging':'working','complete':'completed',
        'failed':'failed','cancelled':'cancelled','interrupted':'interrupted'}

def preserve(connection):
    from modules.youtube.user_schema import import_catalog_library
    preserve_sources(connection)
    import_catalog_library(connection)

@contextmanager
def connect(legacy):
    with owner_database(OWNER,TABLE,legacy,preserve=preserve) as db:
        from services.job_store import _write
        missing=operations.rows(db,'SELECT n.* FROM '+TABLE+' n LEFT JOIN kv_operations o ON o.owner=? AND o.id=n.id WHERE o.id IS NULL',(OWNER,))
        if missing:
            with _write(db):
                for row in missing:
                    accept(db,row,legacy=True)
        yield db

def accept(db, row, *, key=None, legacy=False):
    payload={k:row[k] for k in ('url','source_id','root','video','audio')}
    result=operations.request(db,OWNER,row['id'],kind='download',key=key or 'legacy:'+row['id'],payload=payload,
        legacy_state=('requested' if row['status'] in ('queued','downloading','merging','running') else STATES[row['status']]) if legacy else None,created_at=row.get('created_at'))
    from modules.youtube.downloads import associate
    associate(db,row,legacy=legacy)
    return result

def ensure(db, row):
    return operations.get(db,OWNER,row['id']) or accept(db,row,legacy=True)

def validate(db, identity):
    operations.validate(db,OWNER,identity)

def update(db, row, values):
    operation=ensure(db,row)
    validate(db,row['id'])
    status=values.get('status',row['status'])
    state=STATES[status]
    if status=='complete' and values.get('avatar_error',row.get('avatar_error')):
        state='completed-with-warnings'
    if state in {'started','working'} and operation['state']=='requested':
        operations.claim(db,OWNER,row['id'])
    operations.transition(db,OWNER,row['id'],state=state,
        phase=values.get('failure_phase') or ('merging' if status=='merging' else 'download'),
        progress=values.get('progress'),publication='acknowledged' if status=='complete' else
            'output-published' if values.get('path') else None)

def catalog(legacy):
    from services.job_store_migration import catalog_database
    return lambda: catalog_database(legacy)


def saved_job(connection,identity):
    captured=metadata.preserved_capture(connection,identity)
    if captured is None:return None
    row={name:'' for name in metadata.COLUMNS}
    row.update(metadata_version=0,duration_seconds=None,layout_version=0,bundle_path='',manifest_sha256='',retry_of='')
    row.update(captured['payload']['record'])
    row.update(status='complete',progress=100,error='',failure_phase='',superseded_by='')
    return row


@contextmanager
def merged_library(legacy):
    """One authoritative operational/catalog union for bounded owner reads."""
    import config
    with connect(legacy):
        pass
    with legacy() as db:
        # Validate the same immutable capture/membership invariants as item 02.
        metadata.preserved_library(db,'',0,1)
        db.execute('ATTACH DATABASE ? AS operational',(str(config.JOBS_DB_PATH),))
        try:
            columns=operations.rows(db,'PRAGMA operational.table_info(youtube_downloads)')
            names=[column['name'] for column in columns]
            from services.job_store_migration import identifier
            defaults={'status':"'complete'",'progress':'100','error':"''",'failure_phase':"''",'superseded_by':"''"}
            projection=[]
            for column in columns:
                name=column['name']
                expression=defaults.get(name)
                if expression is None:
                    expression="coalesce(json_extract(f.payload_json,'$.record."+name+"'),"+(column['dflt_value'] or "NULL")+")"
                projection.append(expression+' AS '+identifier(name))
            catalog="""SELECT """+','.join(projection)+""" FROM kv_item_captures f
                JOIN kv_item_captures m ON m.item_id=f.item_id AND m.capture_key=f.capture_key AND m.namespace='youtube.library'
                WHERE f.namespace='youtube.download' AND json_extract(m.payload_json,'$.listed')=1
                AND NOT EXISTS(SELECT 1 FROM kv_item_captures r WHERE r.namespace='youtube.retirement'
                    AND r.item_id=f.item_id AND r.capture_key=f.capture_key)
                AND NOT EXISTS(SELECT 1 FROM operational.youtube_downloads n WHERE n.id=f.capture_key)"""
            yield db, "WITH all_items AS (SELECT * FROM operational.youtube_downloads UNION ALL "+catalog+")"
        finally:
            db.execute('DETACH DATABASE operational')


def library(legacy,q,offset,limit):
    """Merge preserved saves and owner jobs in SQL, retaining existing paging."""
    with merged_library(legacy) as (db,prefix):
        predicate,folded=literal_contains(db,'title',q)
        prefix+=", matching AS (SELECT * FROM all_items WHERE status!='cancelled' AND superseded_by='' AND "+predicate+")"
        total=operations._one(db,prefix+' SELECT count(*) AS total FROM matching',(folded,))['total']
        items=operations.rows(db,prefix+' SELECT * FROM matching ORDER BY created_at DESC,id LIMIT ? OFFSET ?',(folded,limit,offset))
        return {'items':items,'total':total}


def request_ack(legacy,key,url,source_id,video,audio):
    from services import item_catalog
    with legacy() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='kv_item_captures'").fetchone():
            return None
        selected=operations._one(db,"SELECT item_id FROM kv_item_captures WHERE namespace='youtube.request' AND capture_key=?",(key,))
        if selected is None:return None
        captured=item_catalog.read_capture(db,item_id=selected['item_id'],namespace='youtube.request',capture_key=key)
        if captured['format_version']!=1:raise ValueError('Unsupported completed request receipt')
        payload=captured['payload']
        intent=payload['intent']
        if any(intent[k]!=v for k,v in dict(url=url,video=video,audio=audio).items()) or source_id is not None and intent['source_id']!=source_id:
            raise ValueError('This request token belongs to another download.')
        result=saved_job(db,payload['id'])
        if result is None:raise ValueError('Completed request evidence is unavailable.')
        return result


def register_ack(connection,receipt,identity):
    from services import item_catalog
    item=item_catalog.item_for_legacy(connection,namespace='youtube.download',legacy_key=identity)
    if item is None:raise ValueError('Saved request has no content identity')
    item_catalog.record_capture(connection,item_id=item['id'],namespace='youtube.request',
        capture_key=receipt['request_key'],format_version=1,payload={'id':identity,'intent':receipt['payload']})

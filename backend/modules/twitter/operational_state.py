"""Twitter keeps preferences/capture facts separate from disposable job history."""
from contextlib import contextmanager
from services import operations
from services.job_store_migration import owner_database, preserve_sources
OWNER='twitter'
TABLE='twitter_downloads'
STATES={'queued':'requested','running':'working','complete':'completed','failed':'failed',
        'cancelled':'cancelled','interrupted':'interrupted'}

def preserve(connection):
    from modules.twitter.user_schema import import_catalog_receipts
    preserve_sources(connection)
    import_catalog_receipts(connection)

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

def accept(db,row,legacy=False):
    result=operations.request(db,OWNER,row['id'],kind='capture',key=row['request_id'],
        payload={k:row[k] for k in ('url','source_id','root')},
        legacy_state=('requested' if row['status'] in ('queued','downloading','merging','running') else STATES[row['status']]) if legacy else None,created_at=row.get('created_at'))
    if not legacy:
        from services import download_history
        download_history.ensure_schema(db)
        download_history.link(db,OWNER,row['id'])
    return result

def ensure(db,row):
    return operations.get(db,OWNER,row['id']) or accept(db,row,legacy=True)

def update(db,row,values):
    operation=ensure(db,row)
    operations.validate(db,OWNER,row['id'])
    status=values.get('status',row['status'])
    if status=='running' and operation['state']=='requested':
        operations.claim(db,OWNER,row['id'])
    operations.transition(db,OWNER,row['id'],state=STATES[status],
        phase=values.get('phase') or row['phase'] or 'accepted',progress=values.get('progress'),
        publication='acknowledged' if status=='complete' else
            'output-published' if values.get('phase')=='registering' else None)

def catalog(legacy):
    from services.job_store_migration import catalog_database
    return lambda: catalog_database(legacy)


def request_ack(legacy,key,url,source_id):
    from services import item_catalog
    with legacy() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='kv_item_captures'").fetchone():return None
        selected=operations._one(db,"SELECT item_id FROM kv_item_captures WHERE namespace='twitter.request' AND capture_key=?",(key,))
        if selected is None:return None
        captured=item_catalog.read_capture(db,item_id=selected['item_id'],namespace='twitter.request',capture_key=key)
        if captured['format_version']!=1:raise ValueError('Unsupported completed capture receipt')
        record=captured['payload']['record']
        if record['url']!=url or source_id is not None and record['source_id']!=source_id:
            raise ValueError('This request token belongs to a different capture')
        return dict(record,status='complete',phase='completed',progress=100,attempt=0,error='',retry_requests='[]',updated_at='')


def register_ack(connection,identity,row):
    from services import item_catalog
    from modules.twitter.user_schema import CATALOG_RECEIPT_FIELDS
    record={key:row[key] for key in CATALOG_RECEIPT_FIELDS}
    keys=[row['request_id'],*('retry:'+key for key in __import__('json').loads(row['retry_requests']))]
    for key in keys:
        item_catalog.record_capture(connection,item_id=identity,namespace='twitter.request',capture_key=key,
            format_version=1,payload={'record':record})

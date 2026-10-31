"""YouTube media history, not temporary audio/video tracks or artwork."""
from services import download_history as history


def associate(db,row,*,legacy=False):
    if legacy:return
    history.ensure_schema(db)
    previous=row.get('retry_of')
    # Legacy predecessors may predate the ledger. Resolve the existing chain.
    ancestors=[];seen={row['id']}
    while previous:
        if previous in seen or len(ancestors)>=100:raise ValueError('Invalid YouTube retry lineage')
        seen.add(previous);ancestors.append(previous)
        old=db.execute('SELECT * FROM youtube_downloads WHERE id=?',(previous,)).fetchone()
        if old is None:raise ValueError('Missing YouTube retry predecessor')
        if not isinstance(old,dict):old=dict(zip([v[1] for v in db.execute('PRAGMA table_info(youtube_downloads)')],old))
        if any(old[key]!=row[key] for key in ('url','source_id','root','video','audio')):
            raise ValueError('Incompatible YouTube retry predecessor')
        previous=old['retry_of']
    parent=None
    for child in reversed(ancestors):
        history.link(db,'youtube',child,previous=parent)
        parent=child
    history.link(db,'youtube',row['id'],previous=ancestors[0] if ancestors else None)


def event(db,job_id,value,*,source_role="files"):
    if set(value)-{'event','name','progress','files','artwork'} or value.get('event')!='file':
        raise ValueError('Invalid YouTube file event')
    history.ensure_schema(db)
    from pathlib import PurePosixPath
    from services import download_controls
    files=value.get('files',[])
    if type(files) is not list or len(files)>16:raise ValueError('Invalid YouTube media references')
    def checked(name):
        if not isinstance(name,str) or '/' in name or '\\' in name or len(name)>255 or not name.startswith(('video.','audio.')):
            raise ValueError('Invalid YouTube media reference')
        if name.lower().endswith(('.json','.txt','.sqlite','.sqlite3','.db','.ytdl')):raise ValueError('Metadata is not unfinished media')
        return name
    files=[checked(name) for name in files]
    artwork=value.get('artwork') or ''
    if artwork:checked(artwork)
    item_id=history.record(db,'youtube',job_id,file_key='media',module_name='YouTube',
                          name=value['name'],progress=value.get('progress'))
    job=history._one(db,'SELECT * FROM youtube_downloads WHERE id=?',(job_id,))
    if job is not None:
        prefix='.keivotos/youtube/'+job_id+'/'
        download_controls.bind_target(db,item_id,source_id=job['source_id'],root=job['root'],source_role=source_role,
            paths=[prefix+name for name in files],artwork=prefix+artwork if artwork else '')
    return item_id


def complete(db,job_id,name,*,path='',thumbnail='',source_role='files'):
    history.ensure_schema(db)
    item_id=history.record(db,'youtube',job_id,file_key='media',module_name='YouTube',name=_title(name) or _title(path.rsplit('/',1)[-1]) or 'YouTube',completed=True)
    if path:
        from services import download_controls
        job=history._one(db,'SELECT * FROM youtube_downloads WHERE id=?',(job_id,))
        download_controls.bind_target(db,item_id,source_id=job['source_id'],root=job['root'],source_role=source_role,
            paths=[path],artwork=thumbnail or path,published=True)
    return item_id


def contribute(context):
    result=presentation(context)+pending(context)
    if history.has_table(context.jobs,'youtube_downloads'):
        result.extend(history.select(context.jobs,"""
          WITH RECURSIVE ancestors(leaf,id,parent,depth) AS (
            SELECT id,id,retry_of,0 FROM youtube_downloads WHERE superseded_by='' AND path<>''
            UNION ALL SELECT a.leaf,p.id,p.retry_of,a.depth+1 FROM ancestors a
              JOIN youtube_downloads p ON p.id=a.parent WHERE a.parent<>'' AND a.depth<100
          ), roots AS (SELECT leaf,id FROM ancestors WHERE parent='')
          SELECT history_id('youtube',r.id,'media') AS id,'youtube' AS owner,'YouTube' AS module_name,
            coalesce(nullif(j.title,''),history_name(j.path)) AS name,
            CASE WHEN j.status='complete' THEN 'completed' ELSE 'interrupted' END AS state,
            CASE WHEN j.status='complete' THEN 100 ELSE NULL END AS progress,
            NULL AS started_at,NULL AS completed_at,history_time(j.captured_at) AS date_at,
            CASE WHEN history_time(j.captured_at) IS NULL THEN 'unknown' ELSE 'recorded-capture' END AS date_source,
            0 AS attempt,j.source_id AS _source_id,coalesce(nullif(j.thumbnail,''),j.path) AS _media_path FROM youtube_downloads j JOIN roots r ON r.leaf=j.id
          WHERE NOT history_tracked('youtube',j.id) AND NOT history_tracked('youtube',r.id)
        """,context))
    if history.has_table(context.user,'kv_item_captures'):
        result.extend(history.select(context.user,"""
          WITH captured AS (
            SELECT capture_key,history_capture(payload_json,payload_sha256) AS facts
            FROM kv_item_captures WHERE namespace='youtube.download' AND format_version=1
          ), records AS (
            SELECT capture_key,json_extract(facts,'$.record') AS record,
              coalesce(json_extract(facts,'$.legacy_job_ids[#-1]'),capture_key) AS root FROM captured
          )
          SELECT history_id('youtube',root,'media') AS id,'youtube' AS owner,'YouTube' AS module_name,
            coalesce(nullif(json_extract(record,'$.title'),''),history_name(json_extract(record,'$.path'))) AS name,'completed' AS state,100 AS progress,
            NULL AS started_at,NULL AS completed_at,history_time(json_extract(record,'$.captured_at')) AS date_at,
            CASE WHEN history_time(json_extract(record,'$.captured_at')) IS NULL THEN 'unknown' ELSE 'recorded-capture' END AS date_source,
            0 AS attempt,json_extract(record,'$.source_id') AS _source_id,coalesce(nullif(json_extract(record,'$.thumbnail'),''),json_extract(record,'$.path')) AS _media_path FROM records WHERE NOT history_tracked('youtube',capture_key)
              AND NOT history_tracked('youtube',root) AND NOT history_operational('youtube',capture_key)
              AND coalesce(json_extract(record,'$.path'),'')<>''
        """,context))
    for row in result:row['name']=_title(row['name']) or 'YouTube'
    return result


def controls():
    from services.download_controls import OwnerControls
    return OwnerControls(cancel_active)


def cancel_active(row):
    from modules.youtube import jobs
    from services import operations
    with jobs._lock:
        current=jobs.get(row['operation_id'])
        with jobs.get_jobs_db() as db:op=operations.get(db,'youtube',row['operation_id'])
        if not current or current['status'] not in ('queued','downloading','merging') or current['path'] or op['attempt']!=row['attempt']:
            raise ValueError('Download finished or changed')
        jobs.cancel(row['operation_id'])


def _title(value):
    return value.replace('\0','')[:1024] if isinstance(value,str) and value.replace('\0','').strip() else ''


def presentation(context):
    """Resolve only this bounded page's display titles; never rewrite saved facts."""
    current=[dict(row) for row in context.current if row['owner']=='youtube']
    if not current:return []
    titles={}
    if history.has_table(context.jobs,'youtube_downloads'):
        ids=list(dict.fromkeys(row['_operation_id'] for row in current))
        marks=','.join('?' for _ in ids)
        from services.operations import rows
        titles={row['id']:_title(row['title']) for row in rows(context.jobs,
            'SELECT id,title FROM youtube_downloads WHERE id IN ('+marks+')',ids)}
    for row in current:
        title=titles.get(row['_operation_id'],'')
        if not title and history.has_table(context.user,'kv_item_captures'):
            saved=history._one(context.user,"""SELECT history_capture(payload_json,payload_sha256) AS facts
                FROM kv_item_captures WHERE namespace='youtube.download' AND format_version=1
                AND capture_key IN (?,?) ORDER BY CASE WHEN capture_key=? THEN 0 ELSE 1 END LIMIT 1""",
                (row['_operation_id'],row['_lineage'],row['_operation_id']))
            if saved:
                import json
                title=_title(json.loads(saved['facts']).get('record',{}).get('title'))
        if title:row['name']=title
    return current


def pending(context):
    """An accepted request is visible before extraction or the first byte event."""
    if not all(history.has_table(context.jobs,name) for name in ('youtube_downloads','kv_operations','kv_download_links')):
        return []
    return history.select(context.jobs,"""SELECT
        history_id('youtube',coalesce(l.lineage,j.id),'media') AS id,
        'youtube' AS owner,'YouTube' AS module_name,coalesce(nullif(j.title,''),j.url) AS name,
        CASE WHEN o.state='requested' THEN 'queued'
             WHEN o.state IN ('started','working') THEN 'working'
             WHEN o.state='completed-with-warnings' THEN 'completed' ELSE o.state END AS state,
        CASE WHEN o.state='requested' THEN 0 ELSE NULL END AS progress,
        NULL AS started_at,NULL AS completed_at,history_time(o.created_at) AS date_at,
        'request' AS date_source,o.attempt AS attempt
        FROM youtube_downloads j JOIN kv_operations o ON o.owner='youtube' AND o.id=j.id
        LEFT JOIN kv_download_links l ON l.owner='youtube' AND l.operation_id=j.id
        WHERE j.path='' AND j.superseded_by='' AND o.created_at IS NOT NULL
          AND o.state NOT IN ('completed','completed-with-warnings')
          AND NOT history_tracked('youtube',j.id)
    """,context)

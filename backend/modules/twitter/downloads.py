"""Twitter history contributes photos/video only, never avatars or metadata."""
import re
from services import download_history as history

MEDIA=re.compile(r'(?:photo-[1-4]\.(?:jpg|jpeg|png|webp)|video\.mp4)\Z')


def event(db,job_id,value):
    if set(value)-{'event','name','progress'} or value.get('event')!='file' or not MEDIA.fullmatch(value.get('name','')):
        raise ValueError('Invalid Twitter file event')
    history.ensure_schema(db)
    item_id=history.record(db,'twitter',job_id,file_key=value['name'].rsplit('.',1)[0],module_name='Twitter',
                          name=value['name'],progress=value.get('progress'))
    _bind(db,item_id,job_id,value['name'])
    return item_id


def complete(db,job_id,files,*,at=None):
    history.ensure_schema(db)
    for name in files:
        if MEDIA.fullmatch(name):
            item_id=history.record(db,'twitter',job_id,file_key=name.rsplit('.',1)[0],module_name='Twitter',name=name,completed=True,at=at)
            _bind(db,item_id,job_id,name,published=True)


def contribute(context):
    if not history.has_table(context.user,'kv_item_captures'):return []
    # Old asset receipts prove media identities, not their download dates.
    return history.select(context.user,"""
      WITH observations AS (
        SELECT item_id,history_capture(payload_json,payload_sha256) AS facts,
          row_number() OVER(PARTITION BY item_id ORDER BY recorded_at DESC,capture_key DESC) AS position
        FROM kv_item_captures WHERE namespace='twitter.asset-observation' AND format_version=1
      ), manifests AS (
        SELECT item_id,history_capture(payload_json,payload_sha256) AS facts,
          row_number() OVER(PARTITION BY item_id ORDER BY recorded_at DESC,capture_key DESC) AS position
        FROM kv_item_captures WHERE namespace='portable.manifest' AND format_version=1
      ), receipts AS (
        SELECT item_id,min(capture_key) AS operation_id FROM kv_item_captures
          WHERE namespace='twitter.download' GROUP BY item_id
      )
      SELECT history_id('twitter',coalesce(r.operation_id,o.item_id),substr(a.key,1,instr(a.key,'.')-1)) AS id,
        'twitter' AS owner,'Twitter' AS module_name,a.key AS name,
        'completed' AS state,100 AS progress,NULL AS started_at,NULL AS completed_at,
        history_time(json_extract(p.facts,'$.origin.captured_at')) AS date_at,
        CASE WHEN history_time(json_extract(p.facts,'$.origin.captured_at')) IS NULL
          THEN 'unknown' ELSE 'recorded-manifest' END AS date_source,0 AS attempt,
        json_extract(o.facts,'$.source_id') AS _source_id,json_extract(a.value,'$.path') AS _media_path
      FROM observations o JOIN json_each(o.facts,'$.files') a
      LEFT JOIN receipts r ON r.item_id=o.item_id
      LEFT JOIN manifests p ON p.item_id=o.item_id AND p.position=1
      WHERE o.position=1 AND (a.key GLOB 'photo-[1-4].*' OR a.key='video.mp4')
        AND NOT history_tracked('twitter',coalesce(r.operation_id,o.item_id))
    """,context)


def _bind(db,item_id,job_id,name,*,published=False):
    from pathlib import PurePosixPath
    from services import download_controls
    job=history._one(db,'SELECT * FROM twitter_downloads WHERE id=?',(job_id,)) if history.has_table(db,'twitter_downloads') else None
    if job and job['path']:
        relative=(PurePosixPath(job['path']).parent/name).as_posix()
        download_controls.bind_target(db,item_id,source_id=job['source_id'],root=job['root'],source_role='twitter',
            paths=[relative],artwork=relative,published=published)


def controls():
    from services.download_controls import OwnerControls
    return OwnerControls(cancel_active)


def cancel_active(row):
    from modules.twitter import jobs
    from services import operations
    with jobs._lock:
        current=jobs.get(row['operation_id'])
        with jobs.get_jobs_db() as db:op=operations.get(db,'twitter',row['operation_id'])
        if not current or current['status'] not in ('queued','running') or current['phase']=='registering' or op['attempt']!=row['attempt']:
            raise ValueError('Capture finished or changed')
        jobs.cancel(row['operation_id'])

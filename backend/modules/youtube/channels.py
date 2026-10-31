"""Bounded local channel views over existing saved facts and operational jobs."""
from contextlib import contextmanager
from pathlib import Path

from files_base import sources
from modules.youtube import metadata, operational_state
from services import operations
from services.search_text import literal_contains

UNKNOWN = 'unknown'


def key(identity):
    return identity if isinstance(identity, str) and metadata.CHANNEL_ID.fullmatch(identity) else UNKNOWN


@contextmanager
def projection(legacy):
    with operational_state.merged_library(legacy) as (db, prefix):
        db.create_function('youtube_channel_key', 1, key, deterministic=True)
        registered = sources.list_sources(db)
        children = {source.source_id:tuple(Path(child.path).resolve().relative_to(Path(source.path).resolve()).as_posix()
                    for child in sources.descendant_sources(source, registered)) for source in registered}
        db.create_function('youtube_owned', 2, lambda identity, path:identity in children and
            not any(path==child or path.startswith(child+'/') for child in children[identity]), deterministic=True)
        prefix += """, visible AS (
            SELECT i.*,youtube_channel_key(i.channel_id) AS channel_key
            FROM all_items i JOIN files_sources s ON s.source_id=i.source_id
            WHERE s.visible=1 AND s.role IN ('files','video','youtube')
            AND i.status!='cancelled' AND i.superseded_by=''
            AND youtube_owned(i.source_id,i.path)
            AND (NOT EXISTS(SELECT 1 FROM operational.youtube_downloads n WHERE n.id=i.id)
                 OR i.root=s.path)
        ), ranked AS (
            SELECT *,row_number() OVER(PARTITION BY channel_key ORDER BY
                (status='complete' AND channel_name!='') DESC,captured_at DESC,created_at DESC,id) AS rank
            FROM visible
        ), heads AS (
            SELECT r.channel_key AS id,
                CASE WHEN r.channel_key='unknown' THEN 'Unknown channel'
                     ELSE coalesce(nullif(r.channel_name,''),'Unknown channel') END AS name,
                CASE WHEN r.channel_key='unknown' THEN NULL ELSE
                    (SELECT a.id FROM visible a WHERE a.channel_key=r.channel_key AND a.status='complete'
                     AND a.avatar!='' ORDER BY a.captured_at DESC,a.created_at DESC,a.id LIMIT 1) END AS avatar_id,
                (SELECT count(*) FROM visible v WHERE v.channel_key=r.channel_key) AS item_count,
                (SELECT max(v.created_at) FROM visible v WHERE v.channel_key=r.channel_key) AS latest
            FROM ranked r WHERE rank=1
        )"""
        yield db, prefix


def listing(legacy, query, offset, limit):
    with projection(legacy) as (db, prefix):
        title, folded = literal_contains(db, 'v.title', query)
        name, _ = literal_contains(db, 'v.channel_name', query)
        # The neutral unknown label is searchable without inventing an identity.
        label, _ = literal_contains(db, 'h.name', query)
        prefix += ', matching AS (SELECT h.* FROM heads h WHERE '+label+' OR EXISTS(SELECT 1 FROM visible v WHERE v.channel_key=h.id AND ('+title+' OR '+name+')))'
        parameters = (folded, folded, folded)
        total = operations._one(db, prefix+' SELECT count(*) AS total FROM matching', parameters)['total']
        rows = operations.rows(db, prefix+' SELECT id,name,avatar_id,item_count FROM matching ORDER BY latest DESC,id LIMIT ? OFFSET ?', (*parameters, limit, offset))
        return dict(items=rows, total=total)


def _head(db, prefix, identity):
    if key(identity) != identity:
        raise ValueError('Channel is unavailable.')
    row = operations._one(db, prefix+' SELECT id,name,avatar_id,item_count FROM heads WHERE id=?', (identity,))
    if row is None:
        if identity == UNKNOWN:
            return dict(id=UNKNOWN,name='Unknown channel',avatar_id=None,item_count=0)
        raise ValueError('Channel is unavailable.')
    return row


def videos(legacy, identity, query, offset, limit):
    with projection(legacy) as (db, prefix):
        head = _head(db, prefix, identity)
        predicate, folded = literal_contains(db, 'title', query)
        prefix += ', matching AS (SELECT * FROM visible WHERE channel_key=? AND '+predicate+')'
        parameters = (identity, folded)
        total = operations._one(db, prefix+' SELECT count(*) AS total FROM matching', parameters)['total']
        rows = operations.rows(db, prefix+' SELECT * FROM matching ORDER BY created_at DESC,id LIMIT ? OFFSET ?', (*parameters, limit, offset))
        for row in rows:
            row.pop('channel_key', None)
        return dict(channel=head, items=rows, total=total)


def random_candidates(legacy, identity, query, current):
    with projection(legacy) as (db, prefix):
        _head(db, prefix, identity)
        predicate, folded = literal_contains(db, 'title', query)
        prefix += ', matching AS (SELECT *,row_number() OVER(ORDER BY created_at DESC,id)-1 AS position,count(*) OVER() AS total FROM visible WHERE channel_key=? AND '+predicate+')'
        sql = prefix+" SELECT * FROM matching WHERE status='complete' AND id!=? AND video!='audio' ORDER BY random()"
        for selected in db.execute(sql, (identity, folded, current)):
            row = dict(selected)
            row.pop('channel_key', None)
            position, total = row.pop('position'), row.pop('total')
            yield row, position, total

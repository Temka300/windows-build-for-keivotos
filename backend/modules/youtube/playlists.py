"""Guarded local video references and atomic personal membership changes."""
from pathlib import Path
from uuid import uuid4
from fastapi import HTTPException
import config
from files_base import sources, serving


def playlist(connection, identity):
    row = connection.execute("""SELECT p.id,p.title,p.created_at,count(i.job_id) AS item_count
        FROM youtube_playlists p LEFT JOIN youtube_playlist_items i ON i.playlist_id=p.id
        WHERE p.id=? GROUP BY p.id""", (identity,)).fetchone()
    if not row:
        raise HTTPException(404, 'Playlist is unavailable.')
    return dict(row)


def listing(connection, offset, limit):
    total = connection.execute('SELECT count(*) AS total FROM youtube_playlists').fetchone()['total']
    rows = connection.execute("""SELECT p.id,p.title,p.created_at,count(i.job_id) AS item_count
        FROM youtube_playlists p LEFT JOIN youtube_playlist_items i ON i.playlist_id=p.id
        GROUP BY p.id ORDER BY p.created_at DESC,p.id LIMIT ? OFFSET ?""", (limit,offset)).fetchall()
    return {'items':[dict(row) for row in rows], 'total':total}


def create(connection, title, request_id):
    title=title.strip()
    if not title or len(title)>120:
        raise HTTPException(400, 'Enter a playlist title of 1–120 characters.')
    connection.execute('BEGIN IMMEDIATE')
    old=connection.execute('SELECT id,title FROM youtube_playlists WHERE request_id=?', (request_id,)).fetchone()
    if old:
        if old['title']!=title:
            raise HTTPException(409, 'This create request already saved another title.')
        return playlist(connection,old['id'])
    identity=str(uuid4())
    connection.execute('INSERT INTO youtube_playlists(id,request_id,title) VALUES (?,?,?)', (identity,request_id,title))
    connection.commit()
    return playlist(connection,identity)


def video(connection, job_id):
    row=connection.execute('SELECT * FROM youtube_downloads WHERE id=?', (job_id,)).fetchone()
    if not row:
        return None,'Download record is unavailable.'
    job=dict(row)
    if job['status']!='complete' or job.get('superseded_by'):
        return job,'This download has no completed local video.'
    if job['video']=='audio' or Path(job['path']).suffix.lower() not in ('.mp4','.webm','.m4v'):
        return job,'This item cannot play as a local video.'
    source=sources.get_source(connection,job['source_id'])
    if source is None or not source.visible or source.role not in ('files','video','youtube'):
        return job,'The registered video folder is unavailable.'
    try:
        root=serving.resolve_within_source(source.path,'',[config.SUITE_HOME])
        if str(root)!=job['root'] or not root.is_dir():
            return job,'The registered video folder changed.'
        target=serving.resolve_within_source(source.path,job['path'],[config.SUITE_HOME])
        if not target.is_file():
            return job,'The local video file is unavailable.'
    except (serving.ServeDenied,OSError,ValueError):
        return job,'The local video file is unavailable.'
    return job,''


def require_video(connection, job_id):
    job,reason=video(connection,job_id)
    if reason:
        raise HTTPException(409 if job else 404,reason)
    return job


def items(connection, identity, offset, limit):
    owner=playlist(connection,identity)
    rows=connection.execute("""SELECT job_id,position FROM youtube_playlist_items
        WHERE playlist_id=? ORDER BY position LIMIT ? OFFSET ?""", (identity,limit,offset)).fetchall()
    result=[]
    for row in rows:
        job,reason=video(connection,row['job_id'])
        result.append({'job_id':row['job_id'],'position':row['position'],'available':not reason,
                       'unavailable_reason':reason,'job':{k:v for k,v in job.items() if k!='root'} if job else None})
    return {'playlist':owner,'items':result,'total':owner['item_count']}


def membership(connection, job_id):
    version=connection.execute('SELECT version FROM youtube_playlist_versions WHERE job_id=?', (job_id,)).fetchone()
    return {'version':version['version'] if version else 0,
            'playlist_ids':[row['playlist_id'] for row in connection.execute(
                'SELECT playlist_id FROM youtube_playlist_items WHERE job_id=? ORDER BY playlist_id', (job_id,))]}


def set_membership(connection, job_id, selected, expected):
    selected=sorted(set(selected))
    connection.execute('BEGIN IMMEDIATE')
    require_video(connection,job_id)
    current=membership(connection,job_id)
    if current['version']!=expected:
        # The one accepted update may have lost its response; repeat is harmless.
        if current['version']==expected+1 and current['playlist_ids']==selected:
            return current
        raise HTTPException(409, 'Playlist membership changed. Reopen the picker before saving.')
    for identity in selected:
        playlist(connection,identity)  # validate every owner before any removal
    before=set(current['playlist_ids']);after=set(selected)
    if before==after:
        return current
    for identity in before-after:
        connection.execute('DELETE FROM youtube_playlist_items WHERE playlist_id=? AND job_id=?', (identity,job_id))
    for identity in after-before:
        position=connection.execute('SELECT coalesce(max(position),-1)+1 AS next FROM youtube_playlist_items WHERE playlist_id=?', (identity,)).fetchone()['next']
        connection.execute('INSERT INTO youtube_playlist_items(playlist_id,job_id,position) VALUES (?,?,?)', (identity,job_id,position))
    connection.execute("""INSERT INTO youtube_playlist_versions VALUES (?,?)
        ON CONFLICT(job_id) DO UPDATE SET version=excluded.version""", (job_id,expected+1))
    connection.commit()
    return membership(connection,job_id)

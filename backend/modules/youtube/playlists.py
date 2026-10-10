"""Guarded local video references and atomic personal membership changes."""
from pathlib import Path
from uuid import uuid4
from fastapi import HTTPException
import config
from files_base import sources, serving


def playlist(connection, identity, archived=False):
    row = connection.execute("""SELECT p.id,p.title,p.created_at,p.revision,p.deleted_at,count(i.job_id) AS item_count
        FROM youtube_playlists p LEFT JOIN youtube_playlist_items i ON i.playlist_id=p.id
        WHERE p.id=? GROUP BY p.id""", (identity,)).fetchone()
    if not row or row['deleted_at'] and not archived:
        raise HTTPException(404, 'Playlist is unavailable.')
    return dict(row)


def listing(connection, offset, limit):
    total = connection.execute("SELECT count(*) AS total FROM youtube_playlists WHERE deleted_at='' ").fetchone()['total']
    rows = connection.execute("""SELECT p.id,p.title,p.created_at,p.revision,p.deleted_at,count(i.job_id) AS item_count
        FROM youtube_playlists p LEFT JOIN youtube_playlist_items i ON i.playlist_id=p.id
        WHERE p.deleted_at='' GROUP BY p.id ORDER BY p.created_at DESC,p.id LIMIT ? OFFSET ?""", (limit,offset)).fetchall()
    return {'items':[dict(row) for row in rows], 'total':total}


def create(connection, title, request_id):
    title=title.strip()
    if not title or len(title)>120:
        raise HTTPException(400, 'Enter a playlist title of 1–120 characters.')
    connection.execute('BEGIN IMMEDIATE')
    old=connection.execute('SELECT id,title FROM youtube_playlists WHERE request_id=?', (request_id,)).fetchone()
    if old:
        original=connection.execute('SELECT title FROM youtube_playlist_history WHERE playlist_id=? AND revision=0', (old['id'],)).fetchone()
        if (original['title'] if original else old['title'])!=title:
            raise HTTPException(409, 'This create request already saved another title.')
        return playlist(connection,old['id'])
    identity=str(uuid4())
    connection.execute('INSERT INTO youtube_playlists(id,request_id,title) VALUES (?,?,?)', (identity,request_id,title))
    connection.execute("""INSERT INTO youtube_playlist_history
        (playlist_id,revision,request_id,operation,previous_title,title) VALUES (?,0,?,'create','',?)""",(identity,request_id,title))
    connection.commit()
    return playlist(connection,identity)


def change(connection, identity, expected, request_id, *, title=None, count=None):
    """A counted archive or rename, with an exact lost-response retry receipt."""
    operation='rename' if title is not None else 'archive'
    if title is not None:
        title=title.strip()
        if not title or len(title)>120:
            raise HTTPException(400,'Enter a playlist title of 1–120 characters.')
    connection.execute('BEGIN IMMEDIATE')
    owner=playlist(connection,identity,archived=True)
    receipt=connection.execute('SELECT * FROM youtube_playlist_history WHERE playlist_id=? AND request_id=?',(identity,request_id)).fetchone()
    if receipt:
        if receipt['operation']!=operation or receipt['revision']!=expected+1 or owner['revision']!=receipt['revision'] or title is not None and receipt['title']!=title or count is not None and owner['item_count']!=count:
            raise HTTPException(409,'This playlist request conflicts with newer saved changes. Reload the playlist.')
        return owner
    if owner['deleted_at']:
        raise HTTPException(409,'This playlist was deleted. Reload the playlist list.')
    if owner['revision']!=expected or count is not None and owner['item_count']!=count:
        raise HTTPException(409,'This playlist changed. Reload its title and video count before saving.')
    next_title=title if title is not None else owner['title']
    connection.execute("""INSERT INTO youtube_playlist_history
        (playlist_id,revision,request_id,operation,previous_title,title) VALUES (?,?,?,?,?,?)""",
        (identity,expected+1,request_id,operation,owner['title'],next_title))
    if operation=='rename':
        connection.execute('UPDATE youtube_playlists SET title=?,revision=revision+1 WHERE id=?',(next_title,identity))
    else:
        connection.execute("UPDATE youtube_playlists SET deleted_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),revision=revision+1 WHERE id=?",(identity,))
        # Invalidate open pickers without deleting the archived relationships.
        for row in connection.execute('SELECT job_id FROM youtube_playlist_items WHERE playlist_id=?',(identity,)).fetchall():
            connection.execute("""INSERT INTO youtube_playlist_versions VALUES (?,1)
                ON CONFLICT(job_id) DO UPDATE SET version=version+1""",(row['job_id'],))
    connection.commit()
    return playlist(connection,identity,archived=True)


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
                """SELECT i.playlist_id FROM youtube_playlist_items i JOIN youtube_playlists p ON p.id=i.playlist_id
                WHERE i.job_id=? AND p.deleted_at='' ORDER BY i.playlist_id""", (job_id,))]}


def set_membership(connection, job_id, selected, expected):
    selected=sorted(set(selected))
    connection.execute('BEGIN IMMEDIATE')
    require_video(connection,job_id)
    for identity in selected:
        playlist(connection,identity)  # reject archived owners even on a repeated payload
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
    for identity in before ^ after:
        connection.execute('UPDATE youtube_playlists SET revision=revision+1 WHERE id=?',(identity,))
    connection.execute("""INSERT INTO youtube_playlist_versions VALUES (?,?)
        ON CONFLICT(job_id) DO UPDATE SET version=excluded.version""", (job_id,expected+1))
    connection.commit()
    return membership(connection,job_id)

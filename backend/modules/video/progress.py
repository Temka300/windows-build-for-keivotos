"""Video-owned observed positions; file revisions and older history are retained."""
import hashlib
import json
import math
import os
import re
import sqlite3
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import HTTPException

MAX_COUNTER = 9007199254740991
SCHEMA = """
CREATE TABLE IF NOT EXISTS video_watch_sessions (
 source_id TEXT NOT NULL, path TEXT NOT NULL, generation INTEGER NOT NULL,
 revision TEXT NOT NULL, PRIMARY KEY(source_id,path)
);
CREATE TABLE IF NOT EXISTS video_watch_progress (
 source_id TEXT NOT NULL, path TEXT NOT NULL, revision TEXT NOT NULL,
 position REAL NOT NULL CHECK(position>=0), duration REAL NOT NULL CHECK(duration>0),
 ended INTEGER NOT NULL CHECK(ended IN (0,1)), generation INTEGER NOT NULL,
 sequence INTEGER NOT NULL, updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 PRIMARY KEY(source_id,path,revision), CHECK(position<=duration)
);
"""


def _windows_changed(path):
    # Query metadata only, with all sharing modes; never retain a file handle.
    # https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_basic_info
    import ctypes
    from ctypes import wintypes
    class Basic(ctypes.Structure):
        _fields_ = [(name, ctypes.c_longlong) for name in ('created','accessed','written','changed')] + [('attributes', wintypes.DWORD)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    query = kernel.GetFileInformationByHandleEx
    query.argtypes = [wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
    query.restype = wintypes.BOOL
    close = kernel.CloseHandle; close.argtypes = [wintypes.HANDLE]; close.restype = wintypes.BOOL
    handle = create(str(path), 0, 7, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        facts = Basic()
        if not query(handle, 0, ctypes.byref(facts), ctypes.sizeof(facts)):
            raise ctypes.WinError(ctypes.get_last_error())
        return facts.changed
    finally:
        close(handle)


def revision(path):
    """Fresh file-system revision, not a content hash or a media metadata scan."""
    before = path.stat()
    changed = _windows_changed(path) if os.name == 'nt' else before.st_ctime_ns
    after = path.stat()
    key = lambda value: (value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,
                         None if os.name == 'nt' else value.st_ctime_ns)
    if key(before) != key(after):
        raise HTTPException(409, 'Video changed. Reopen it.')
    return hashlib.sha256(json.dumps([*key(after), changed], separators=(',', ':')).encode()).hexdigest()


def generation(connection, source_id, path):
    row = connection.execute('SELECT generation FROM video_watch_sessions WHERE source_id=? AND path=?', (source_id,path)).fetchone()
    return row['generation'] if row else 0


def valid_position(row):
    return (all(type(row[key]) in (int,float) and math.isfinite(row[key]) for key in ('position','duration'))
            and 0 <= row['position'] <= row['duration'] and row['duration'] > 0
            and type(row['ended']) is int and row['ended'] in (0,1)
            and (not row['ended'] or abs(row['position']-row['duration']) <= .001)
            and type(row['generation']) is int and 1 <= row['generation'] <= MAX_COUNTER
            and type(row['sequence']) is int and 1 <= row['sequence'] <= MAX_COUNTER)


def read(connection, source_id, path, current_revision):
    row = connection.execute('SELECT * FROM video_watch_progress WHERE source_id=? AND path=? AND revision=?',
                             (source_id,path,current_revision)).fetchone()
    if row is not None and not valid_position(row):
        raise HTTPException(409, 'Saved video progress needs review.')
    return {'revision': current_revision, 'generation': generation(connection,source_id,path),
            'position': dict(row) if row is not None else None}


def begin(connection, payload, validate):
    connection.execute('BEGIN IMMEDIATE')
    current = generation(connection,payload.source_id,payload.path)
    if current != payload.expected_generation or current >= MAX_COUNTER:
        raise HTTPException(409, 'Playback session changed. Reopen this video.')
    connection.execute("""INSERT INTO video_watch_sessions VALUES(?,?,?,?)
        ON CONFLICT(source_id,path) DO UPDATE SET generation=excluded.generation,revision=excluded.revision""",
        (payload.source_id,payload.path,current+1,payload.revision))
    validate()
    connection.commit()
    return {'generation':current+1,'revision':payload.revision}


def save(connection, payload, validate):
    if (not math.isfinite(payload.position) or not math.isfinite(payload.duration)
            or not 0 <= payload.position <= payload.duration or payload.duration <= 0
            or payload.ended and abs(payload.position-payload.duration) > .001):
        raise HTTPException(409, 'Observed position is outside this video.')
    connection.execute('BEGIN IMMEDIATE')
    session = connection.execute('SELECT generation,revision FROM video_watch_sessions WHERE source_id=? AND path=?',
                                 (payload.source_id,payload.path)).fetchone()
    if not session or (session['generation'],session['revision']) != (payload.generation,payload.revision):
        raise HTTPException(409, 'A newer playback session owns this video.')
    old = connection.execute('SELECT * FROM video_watch_progress WHERE source_id=? AND path=? AND revision=?',
                             (payload.source_id,payload.path,payload.revision)).fetchone()
    values = (payload.position,payload.duration,int(payload.ended))
    if old is not None and not valid_position(old):
        raise HTTPException(409, 'Saved video progress needs review.')
    if old and old['generation'] == payload.generation and old['sequence'] >= payload.sequence:
        if old['sequence'] == payload.sequence and tuple(old[key] for key in ('position','duration','ended')) == values:
            validate(); connection.commit(); return dict(old)
        raise HTTPException(409, 'A newer position has already been saved.')
    connection.execute("""INSERT INTO video_watch_progress
        (source_id,path,revision,position,duration,ended,generation,sequence) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(source_id,path,revision) DO UPDATE SET position=excluded.position,duration=excluded.duration,
        ended=excluded.ended,generation=excluded.generation,sequence=excluded.sequence,
        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
        (payload.source_id,payload.path,payload.revision,*values,payload.generation,payload.sequence))
    validate()
    connection.commit()
    return dict(connection.execute('SELECT * FROM video_watch_progress WHERE source_id=? AND path=? AND revision=?',
                                  (payload.source_id,payload.path,payload.revision)).fetchone())


def import_catalog_records(connection):
    """Separate watch-record importer; original tables retain mutable facts."""
    from services import item_catalog
    if not connection.in_transaction:
        raise ValueError('Import Video watch records inside a caller transaction')
    cursor = connection.cursor(); cursor.row_factory = sqlite3.Row
    point = 'video_watch_' + uuid4().hex; connection.execute('SAVEPOINT '+point)
    try:
        present = {row['name'] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        count = 0
        for table, keys, fields in [
            ('video_watch_sessions',('source_id','path'),('source_id','path','generation','revision')),
            ('video_watch_progress',('source_id','path','revision'),('source_id','path','revision','position','duration','ended','generation','sequence'))]:
            if table not in present: continue
            if not set(fields) <= {row['name'] for row in cursor.execute('PRAGMA table_info('+table+')')}:
                raise ValueError('Unsupported Video watch schema')
            if cursor.execute('SELECT 1 FROM '+table+' GROUP BY '+','.join(keys)+' HAVING count(*)>1 LIMIT 1').fetchone():
                raise ValueError('Duplicate Video watch identity')
            for row in cursor.execute('SELECT '+','.join(fields)+' FROM '+table+' ORDER BY '+','.join(keys)):
                if (not isinstance(row['source_id'],str) or not 0<len(row['source_id'])<=256 or '\0' in row['source_id']
                        or not isinstance(row['path'],str) or not 0<len(row['path'])<=4096 or '\0' in row['path']
                        or not isinstance(row['revision'],str) or not re.fullmatch('[a-f0-9]{64}',row['revision'])
                        or type(row['generation']) is not int or not 1<=row['generation']<=MAX_COUNTER
                        or table=='video_watch_progress' and not valid_position(row)):
                    raise ValueError('Invalid Video watch record')
                def anchor(kind, values):
                    encoded=json.dumps(values,ensure_ascii=False,separators=(',',':'))
                    alias='v1:'+hashlib.sha256(encoded.encode()).hexdigest()
                    identity=str(uuid5(NAMESPACE_URL,'urn:keivotos:video:'+kind+':'+encoded))
                    item_catalog.ensure_item(connection,namespace='video.'+kind,legacy_key=alias,owner='video',item_type=kind,item_id=identity)
                    item_catalog.record_capture(connection,item_id=identity,namespace='video.record',capture_key='identity',format_version=1,payload=dict(kind=kind,key=values))
                    return identity
                item=anchor('item',[row['source_id'],row['path']])
                watch=anchor('watch-session' if table.endswith('sessions') else 'watch-position',[row[key] for key in keys])
                item_catalog.relate_items(connection,source_item_id=watch,target_item_id=item,kind='item')
                count+=1
        connection.execute('RELEASE '+point)
        return count
    except BaseException:
        connection.execute('ROLLBACK TO '+point); connection.execute('RELEASE '+point); raise
    finally:
        cursor.close()

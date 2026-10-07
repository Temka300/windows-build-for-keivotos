"""Personal positions with compare-and-swap sessions and retained old revisions."""
from fastapi import HTTPException


def generation(connection, source_id, path, kind):
    row = connection.execute(
        'SELECT generation FROM manga_reading_sessions WHERE source_id=? AND path=? AND kind=?',
        (source_id, path, kind)).fetchone()
    return row['generation'] if row else 0


def read(connection, source_id, path, kind, revision):
    row = connection.execute(
        """SELECT * FROM manga_reading_progress
           WHERE source_id=? AND path=? AND kind=? AND revision=?""",
        (source_id, path, kind, revision)).fetchone()
    exists = connection.execute(
        'SELECT 1 FROM manga_reading_progress WHERE source_id=? AND path=? AND kind=? LIMIT 1',
        (source_id, path, kind)).fetchone()
    return {'generation': generation(connection, source_id, path, kind),
            'position': dict(row) if row else None, 'stale': bool(exists) and row is None}


def begin(connection, source_id, path, kind, expected):
    connection.execute('BEGIN IMMEDIATE')
    current = generation(connection, source_id, path, kind)
    if current != expected:
        raise HTTPException(409, 'Reading session changed; reopen this chapter.')
    connection.execute("""INSERT INTO manga_reading_sessions VALUES (?,?,?,?)
        ON CONFLICT(source_id,path,kind) DO UPDATE SET generation=excluded.generation""",
        (source_id, path, kind, current + 1))
    connection.commit()
    return {'generation': current + 1}


def save(connection, payload, page_count):
    connection.execute('BEGIN IMMEDIATE')
    key = (payload.source_id, payload.path, payload.kind)
    if generation(connection, *key) != payload.generation:
        raise HTTPException(409, 'A newer reading session owns this chapter.')
    old = connection.execute("""SELECT * FROM manga_reading_progress
        WHERE source_id=? AND path=? AND kind=? AND revision=?""", (*key, payload.revision)).fetchone()
    values = (payload.page, payload.mode, payload.anchor)
    if old and old['generation'] == payload.generation and old['sequence'] >= payload.sequence:
        if old['sequence'] == payload.sequence and tuple(old[k] for k in ('page','mode','anchor')) == values:
            return dict(old)
        raise HTTPException(409, 'A newer reading position has already been saved.')
    connection.execute("""INSERT INTO manga_reading_progress
        (source_id,series_path,path,kind,revision,page_count,page,mode,anchor,generation,sequence)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source_id,path,kind,revision) DO UPDATE SET
        page_count=excluded.page_count,page=excluded.page,mode=excluded.mode,anchor=excluded.anchor,
        generation=excluded.generation,sequence=excluded.sequence,
        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
        (payload.source_id,payload.series_path,payload.path,payload.kind,payload.revision,
         page_count,*values,payload.generation,payload.sequence))
    connection.commit()
    return dict(connection.execute("""SELECT * FROM manga_reading_progress
        WHERE source_id=? AND path=? AND kind=? AND revision=?""", (*key,payload.revision)).fetchone())

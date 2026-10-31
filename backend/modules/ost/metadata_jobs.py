"""Explicit local Music metadata jobs over Files identities and suite operations.

No work starts at import, listing or navigation. The active owner's supervised
worker handles accepted rescans/refreshes; leaving a UI never owns its lifetime.
"""
import asyncio
import json
from pathlib import Path
import threading
import time
from uuid import uuid4

import config
from database import get_jobs_db, get_user_db
from files_base import index, sources, serving
from modules.ost import library, albums
from services import audio_metadata, operations
from services.operation_diagnostics import emit
from services.diagnostic_events import diagnostic_context

_KIND = 'music-metadata'
_stop = threading.Event()


def _source(source_id, parent=''):
    with get_user_db() as connection:
        registered = sources.list_sources(connection)
    source = next((row for row in registered if row.source_id == source_id
                   and row.role == 'ost' and row.visible), None)
    if source is None:
        raise serving.ServeDenied(404, 'Music folder is unavailable')
    forbidden = library.forbidden_roots(source, registered, config.SUITE_HOME)
    library.directory_path(source, parent, forbidden)
    return source, registered, forbidden


def _rows(connection):
    return operations.rows(connection,
        'SELECT o.*,r.payload_json FROM kv_operations o JOIN kv_job_requests r '
        'ON r.id=o.request_id WHERE o.owner=? AND r.kind=? '
        "AND (o.state IN ('requested','started','working') OR o.id IN ("
        "SELECT id FROM kv_operations WHERE owner='ost' ORDER BY created_at DESC,id DESC LIMIT 32)) "
        'ORDER BY o.created_at DESC,o.id DESC', ('ost', _KIND))


def _view(row, payload, source):
    try:
        phase = json.loads(row['phase']) if row['phase'] else {}
    except (TypeError, ValueError):
        phase = {}
    return {'id': row['id'], 'source_id': source.source_id,
            'label': Path(payload['parent']).name or source.display_name,
            'state': row['state'], 'progress': row['progress'],
            'processed': phase.get('processed', 0), 'total': phase.get('total'),
            'failed': phase.get('failed', 0), 'discover': payload['discover'],
            'created_at': row['created_at']}


def listing():
    # Never return paths/details belonging to a newly hidden/released source.
    with get_jobs_db() as connection:
        operations.ensure_schema(connection)
        rows = _rows(connection)
    result = []
    for row in rows:
        payload = json.loads(row['payload_json'])
        try:
            source, _, _ = _source(payload['source_id'], payload['parent'])
        except (OSError, ValueError, serving.ServeDenied):
            continue
        result.append(_view(row, payload, source))
    return result


def request(source_id, parent='', *, discover=True):
    source, _, _ = _source(source_id, parent)
    payload = {'source_id': source_id, 'parent': parent,
               'root': str(Path(source.path).resolve()), 'discover': discover}
    with get_jobs_db() as connection:
        operations.ensure_schema(connection)
        connection.execute('BEGIN IMMEDIATE')
        for row in _rows(connection):
            if row['state'] not in operations.TERMINAL and json.loads(row['payload_json']) == payload:
                return _view(row, payload, source)
        identity = uuid4().hex
        row = operations.request(connection, 'ost', identity, kind=_KIND,
                                 key='metadata:' + identity, payload=payload)
        connection.commit()
    with diagnostic_context(operation_owner='ost', operation_id=identity):
        emit('ost', 'metadata.refresh_requested', 'Music metadata refresh requested',
             details={'discover': discover})
    return _view(row, payload, source)


def startup():
    _stop.clear()
    with get_jobs_db() as connection:
        operations.ensure_schema(connection)
        for row in _rows(connection):
            if row['state'] in ('started', 'working'):
                operations.transition(connection, 'ost', row['id'], state='interrupted')
        connection.commit()


def _status(identity, *, state=None, processed=0, total=None, failed=0):
    with get_jobs_db() as connection:
        operations.transition(connection, 'ost', identity, state=state,
            phase=json.dumps(dict(processed=processed, total=total, failed=failed)),
            progress=(100 * processed / total if total else 100 if state in
                      ('completed', 'completed-with-warnings') else 0))
        connection.commit()


def run_next():
    # An idle active owner must not reserve the source-edit lock. Only accepted
    # queued work participates in Files' scan/role-change exclusion boundary.
    with get_jobs_db() as connection:
        operations.ensure_schema(connection)
        row = next((row for row in reversed(_rows(connection))
                    if row['state'] == 'requested'), None)
    if row is None:
        return False
    with index.source_work(wait=False):
        with get_jobs_db() as connection:
            if operations.get(connection, 'ost', row['id'])['state'] != 'requested':
                return False
            token = operations.claim(connection, 'ost', row['id'])
            connection.commit()
        identity = row['id']; payload = json.loads(row['payload_json'])
        processed = failed = 0; total = None
        with operations.bind('ost', identity, token):
            try:
                source, registered, forbidden = _source(payload['source_id'], payload['parent'])
                if str(Path(source.path).resolve()) != payload['root']:
                    raise InterruptedError('Music folder location changed')
                _status(identity, state='working')
                if payload['discover']:
                    # Existing Files discovery/manifest path, without recursively
                    # submitting another full-root metadata operation.
                    from routers.files import _scan_source
                    _scan_source(source.source_id, notify_owner=False)
                    source, registered, forbidden = _source(source.source_id, payload['parent'])
                with index.open_index(config.FILES_DB_PATH) as connection:
                    albums.synchronize(connection, [source])
                    rows = connection.execute('SELECT relative_path FROM files_index '
                        'WHERE source_id=? AND is_dir=0 AND available=1 ORDER BY relative_path',
                        (source.source_id,)).fetchall()
                    prefix = payload['parent'].rstrip('/')
                    paths = [row['relative_path'] for row in rows
                        if (not prefix or row['relative_path'].startswith(prefix + '/'))
                        and Path(row['relative_path']).suffix.lower().lstrip('.') in library.AUDIO_EXTENSIONS
                        and library.available(source, row['relative_path'], False, forbidden)]
                total = len(paths); _status(identity, processed=0, total=total)
                for path in paths:
                    if _stop.is_set():
                        raise InterruptedError('Metadata refresh interrupted')
                    fresh, _, forbidden = _source(source.source_id, payload['parent'])
                    if str(Path(fresh.path).resolve()) != payload['root']:
                        raise InterruptedError('Music folder location changed')
                    result = None
                    for attempt in range(4):
                        result = audio_metadata.read(config.FILES_DB_PATH, fresh, path, forbidden,
                                                     refresh=payload['discover'])
                        if result['status'] != 'busy' or _stop.wait(.1):
                            break
                    if _stop.is_set():
                        raise InterruptedError('Metadata refresh interrupted')
                    checked, _, current_forbidden = _source(source.source_id, payload['parent'])
                    if str(Path(checked.path).resolve()) != payload['root']:
                        raise InterruptedError('Music folder location changed')
                    forbidden = current_forbidden
                    if result['status'] not in ('pending', 'busy', 'unavailable'):
                        albums.record(config.FILES_DB_PATH, fresh, path, forbidden, result)
                    failed += result['status'] not in ('ready',)
                    processed += 1
                    _status(identity, processed=processed, total=total, failed=failed)
                state = 'completed-with-warnings' if failed else 'completed'
                _status(identity, state=state, processed=processed, total=total, failed=failed)
                emit('ost', 'metadata.refresh_finished', 'Music metadata refreshed',
                     details={'processed': processed, 'failed': failed})
            except Exception as error:
                state = 'interrupted' if isinstance(error, InterruptedError) else 'failed'
                _status(identity, state=state, processed=processed, total=total, failed=failed)
                emit('ost', 'metadata.refresh_failed', 'Music metadata refresh stopped',
                     details={'reason': type(error).__name__})
        return True


async def worker():
    while True:
        pending = asyncio.create_task(asyncio.to_thread(run_next))
        try:
            try:
                worked = await asyncio.shield(pending)
            except RuntimeError:
                # An active Files scan owns the shared lock; retry later.
                worked = False
        except asyncio.CancelledError:
            _stop.set()
            await pending
            raise
        await asyncio.sleep(.1 if worked else .5)

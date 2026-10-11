"""Durable UUID receipts and one supervised helper; completion is create-only."""
import asyncio
from datetime import datetime, timezone
import importlib.util
import json
import logging
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from uuid import UUID, uuid4

import config
from database import get_user_db
from files_base import index, serving, sources
from maintenance import exclusive_tool_operation
from modules.twitter import archive

logger = logging.getLogger('keivotos.twitter')
_lock = threading.RLock()
ACTIVE = ('queued', 'running')


def get(job_id):
    with get_user_db() as db:
        row = db.execute('SELECT * FROM twitter_downloads WHERE id=?', (job_id,)).fetchone()
    return dict(row) if row else None


def public(job):
    return {key: value for key, value in job.items() if key not in ('root', 'retry_requests')}


def listing(offset=0, limit=120):
    with get_user_db() as db:
        total = db.execute('SELECT COUNT(*) AS count FROM twitter_downloads').fetchone()['count']
        rows = db.execute('SELECT * FROM twitter_downloads ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?',
                          (limit, offset)).fetchall()
    return {'items': [public(dict(row)) for row in rows], 'total': total,
            'next_offset': offset + len(rows) if offset + len(rows) < total else None}


def destination(source_id, expected_root=None):
    with get_user_db() as db:
        registered = sources.list_sources(db)
    source = next((s for s in registered if s.source_id == source_id and s.visible and s.role == 'twitter'), None)
    if source is None:
        raise ValueError('Choose a visible Twitter folder in Files settings')
    forbidden = archive.forbidden_roots(source, registered, config.SUITE_HOME)
    root = serving.resolve_within_source(source.path, '', forbidden)
    if not root.is_dir() or expected_root is not None and str(root) != expected_root:
        raise ValueError('Twitter destination changed or is unavailable')
    return source, root, forbidden


def preferences():
    with get_user_db() as db:
        row = db.execute('SELECT source_id FROM twitter_preferences WHERE id=1').fetchone()
        selected = row['source_id'] if row else ''
        folders = [s for s in sources.list_sources(db) if s.visible and s.role == 'twitter']
    source = next((s for s in folders if s.source_id == selected), None) if selected else next(iter(folders), None)
    return {'source_id': source.source_id if source else '', 'saved_source_id': selected,
            'available': bool(source and archive.available(source, [config.SUITE_HOME])),
            'dependencies': dependencies()}


def dependencies():
    missing = [name for name in ('yt_dlp', 'PIL', 'imageio_ffmpeg') if importlib.util.find_spec(name) is None]
    return {'ready': not missing, 'missing': missing}


def save_preferences(source_id):
    with _lock, exclusive_tool_operation('saving Twitter preferences', wait=False), get_user_db() as db:
        if source_id:
            destination(source_id)
        db.execute('INSERT INTO twitter_preferences(id,source_id) VALUES(1,?) '
                   'ON CONFLICT(id) DO UPDATE SET source_id=excluded.source_id', (source_id,))
        db.commit()
    return preferences()


def _capacity(db, post_id, source_id):
    if db.execute("SELECT COUNT(*) AS count FROM twitter_downloads WHERE status IN ('queued','running')").fetchone()['count'] >= 20:
        raise ValueError('The Twitter download queue is full')
    if db.execute("SELECT 1 FROM twitter_downloads WHERE post_id=? AND source_id=? AND status IN ('queued','running')",
                  (post_id, source_id)).fetchone():
        raise ValueError('This post is already downloading to that folder')


def create(url, source_id, request_id):
    url, post_id = archive.canonical_url(url)
    request_id = str(UUID(str(request_id)))
    source_id = source_id or preferences()['source_id']
    with _lock, exclusive_tool_operation('queuing a Twitter capture', wait=False), get_user_db() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM twitter_downloads WHERE request_id=?', (request_id,)).fetchone()
        if row:
            item = dict(row)
            if item['url'] != url or item['source_id'] != source_id:
                raise ValueError('This request token belongs to a different capture')
            return item
        _, root, _ = destination(source_id)
        tools = dependencies()
        if not tools['ready']:
            raise ValueError('Missing capture dependencies: ' + ', '.join(tools['missing']))
        _capacity(db, post_id, source_id)
        db.execute('INSERT INTO twitter_downloads(id,request_id,url,post_id,source_id,root) VALUES(?,?,?,?,?,?)',
                   (request_id, request_id, url, post_id, source_id, str(root)))
        db.commit()
    return get(request_id)


def update(job_id, **values):
    allowed = {'status', 'phase', 'progress', 'attempt', 'capture_id', 'path', 'error'}
    if not values or not set(values) <= allowed:
        raise ValueError('Invalid Twitter job fields')
    with _lock, get_user_db() as db:
        db.execute('UPDATE twitter_downloads SET ' + ','.join(key + '=?' for key in values) +
                   ",updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (*values.values(), job_id))
        db.commit()


def cancel(job_id):
    with _lock, exclusive_tool_operation('cancelling a Twitter capture', wait=False):
        item = get(job_id)
        if item is None:
            raise ValueError('Download not found')
        if item['status'] in ACTIVE:
            update(job_id, status='cancelled', error='Capture cancelled; any files are preserved')
    return get(job_id)


def retry(job_id, request_id):
    token = str(UUID(str(request_id)))
    with _lock, exclusive_tool_operation('retrying a Twitter capture', wait=False), get_user_db() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM twitter_downloads WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise ValueError('Download not found')
        item = dict(row)
        receipts = json.loads(item['retry_requests'])
        if token in receipts:
            return item
        if item['status'] not in ('failed', 'interrupted', 'cancelled'):
            raise ValueError('This capture cannot be retried')
        if len(receipts) >= 100:
            raise ValueError('Start a new capture after 100 retry attempts')
        destination(item['source_id'], item['root'])
        _capacity(db, item['post_id'], item['source_id'])
        receipts.append(token)
        db.execute("UPDATE twitter_downloads SET status='queued',error='',retry_requests=?,"
                   "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (json.dumps(receipts), job_id))
        db.commit()
    return get(job_id)


def recover():
    # Startup and shutdown run inside the suite's module transition. No remote
    # work is resumed, including jobs which had not started before shutdown.
    with _lock, get_user_db() as db:
        db.execute("UPDATE twitter_downloads SET status='interrupted',error='Capture interrupted; use Retry. Files are preserved',"
                   "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE status IN ('queued','running')")
        db.commit()


def _folder(root, parts, forbidden, exclusive=False):
    current = root
    for i, part in enumerate(parts):
        current = current / part
        if current.is_symlink():
            raise ValueError('Capture directories cannot contain symbolic links')
        # Fence the existing parent before creating the next fixed component.
        serving.resolve_within_source(root, current.parent.relative_to(root).as_posix(), forbidden)
        try:
            serving.resolve_within_source(root, current.relative_to(root).as_posix(), forbidden)
        except serving.ServeDenied as error:
            if error.status_code != 404:
                raise
        current.mkdir(exist_ok=not (exclusive and i == len(parts) - 1))
        serving.resolve_within_source(root, current.relative_to(root).as_posix(), forbidden)
    return current


def prepare(job):
    with exclusive_tool_operation('starting a Twitter capture', wait=False), _lock:
        current = get(job['id'])
        if current['status'] != 'running':
            raise ValueError('Capture state changed')
        _, root, forbidden = destination(job['source_id'], job['root'])
        now = datetime.now(timezone.utc)
        capture_id = now.strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid4().hex[:8]
        folder = _folder(root, ['captures', job['post_id'], capture_id], forbidden, exclusive=True)
        path = folder.relative_to(root).as_posix() + '/manifest.json'
        update(job['id'], capture_id=capture_id, path=path, attempt=current['attempt'] + 1,
               progress=0, phase='metadata', error='')
        return {**get(job['id']), 'forbidden': [str(p) for p in forbidden],
                'directory_identity': [folder.stat().st_dev, folder.stat().st_ino],
                'captured_at': now.isoformat().replace('+00:00', 'Z')}


def register(job):
    with exclusive_tool_operation('registering a saved Twitter capture', wait=False), _lock:
        current = get(job['id'])
        if current['status'] != 'running':
            raise ValueError('Capture state changed')
        source, root, forbidden = destination(job['source_id'], job['root'])
        with get_user_db() as db:
            registered = sources.list_sources(db)
        item = archive.capture(source, registered, config.SUITE_HOME, job['path'])
        if not item['available'] or item['post_id'] != job['post_id'] or item['source_url'] != job['url']:
            raise ValueError('Saved capture changed or is unavailable')
        with index.open_index(config.FILES_DB_PATH) as db:
            index.scan_source(db, source.source_id, root, excluded_roots=forbidden)
        update(job['id'], status='complete', phase='completed', progress=100, error='')


def publish(spec, files):
    from modules.twitter.acquisition import directory
    with exclusive_tool_operation('publishing a Twitter capture', wait=False), _lock:
        current = get(spec['id'])
        if current['status'] != 'running' or current['capture_id'] != spec['capture_id']:
            raise ValueError('Capture state changed before publication')
        _, root, forbidden = destination(spec['source_id'], spec['root'])
        spec = {**spec, 'forbidden': [str(p) for p in forbidden]}
        folder = directory(spec)
        if not isinstance(files, dict) or set(files) != set(archive.ASSET_LIMITS):
            raise ValueError('Worker did not verify all capture assets')
        for name, maximum in archive.ASSET_LIMITS.items():
            value = files[name]
            target = serving.resolve_served_file(root, folder.relative_to(root).as_posix() + '/' + name, forbidden)
            if (type(value.get('size')) is not int or not 0 < value['size'] <= maximum or
                    not isinstance(value.get('sha256'), str) or not archive.HASH.fullmatch(value['sha256']) or
                    list(archive._identity(target.stat())) != value.get('identity')):
                raise ValueError('Capture asset changed after worker verification')
        manifest = {'schema_version': 1, 'complete': True, 'post_id': spec['post_id'],
                    'capture_id': spec['capture_id'], 'captured_at': spec['captured_at'],
                    'completed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                    'source_url': spec['url'],
                    'files': {name: {'size': value['size'], 'sha256': value['sha256']} for name, value in files.items()}}
        with (folder / 'manifest.json').open('xb') as handle:
            handle.write((json.dumps(manifest) + '\n').encode('utf-8'))
            handle.flush()
            os.fsync(handle.fileno())
        update(spec['id'], phase='registering', progress=98)
        register(get(spec['id']))


async def terminate(process):
    if process.returncode is not None:
        return
    if sys.platform == 'win32':
        killer = await asyncio.create_subprocess_exec('taskkill', '/PID', str(process.pid), '/T', '/F',
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW)
        await killer.wait()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()


async def execute(job):
    spec = prepare(job)
    command = [sys.executable, '--twitter-worker'] if getattr(sys, 'frozen', False) else [sys.executable, str(config.CODE_ROOT / 'app.py'), '--twitter-worker']
    options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {'start_new_session': True}
    launch = asyncio.create_task(asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options))
    try:
        process = await asyncio.shield(launch)
    except asyncio.CancelledError:
        process = await launch
        await terminate(process)
        raise
    result, failure = None, ''
    async def events():
        nonlocal result, failure
        while line := await process.stdout.readline():
            event = json.loads(line)
            if event.get('event') == 'complete':
                result = event['files']
            elif event.get('event') == 'failure':
                failure = str(event.get('error', 'Capture failed'))[:1000]
            elif event.get('event') == 'progress':
                progress = event.get('progress')
                if event.get('phase') not in ('metadata', 'avatar', 'video', 'verifying') or type(progress) not in (int, float) or not math.isfinite(progress):
                    raise ValueError('Invalid worker progress')
                try:
                    with _lock, exclusive_tool_operation('recording Twitter progress', wait=False):
                        current = get(job['id'])
                        if current['status'] == 'running':
                            update(job['id'], phase=event['phase'], progress=max(current['progress'], min(95, progress)))
                except RuntimeError:
                    # Progress is advisory; a concurrent maintenance reservation
                    # must not turn a healthy capture into a failed download.
                    continue
    async def diagnostics():
        while line := await process.stderr.readline():
            logger.info('job=%s %s', job['id'], line.decode('utf-8', 'replace').strip()[:2000])
    tasks = []
    try:
        worker_spec = {key: spec[key] for key in ('url', 'post_id', 'root', 'path', 'capture_id', 'captured_at', 'forbidden', 'directory_identity')}
        process.stdin.write((json.dumps(worker_spec) + '\n').encode('utf-8'))
        await process.stdin.drain()
        process.stdin.close()
        tasks = [asyncio.create_task(events()), asyncio.create_task(diagnostics())]
        started = time.monotonic()
        while process.returncode is None:
            current = get(job['id'])
            if current['status'] != 'running':
                await terminate(process)
                return
            destination(job['source_id'], job['root'])
            if time.monotonic() - started > 1800:
                raise ValueError('Capture timed out; files were preserved')
            if any(task.done() and task.exception() for task in tasks):
                raise ValueError('Capture event stream failed')
            await asyncio.sleep(.1)
        await asyncio.gather(*tasks)
        if process.returncode or result is None:
            raise ValueError(failure or 'Capture failed; see Logs')
        completion = asyncio.create_task(asyncio.to_thread(publish, spec, result))
        try:
            await asyncio.shield(completion)
        except asyncio.CancelledError:
            await completion
            raise
    finally:
        await terminate(process)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_queue():
    try:
        while True:
            try:
                with _lock, exclusive_tool_operation('admitting a Twitter capture', wait=False), get_user_db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    row = db.execute("SELECT * FROM twitter_downloads WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
                    if row:
                        db.execute("UPDATE twitter_downloads SET status='running' WHERE id=?", (row['id'],))
                        db.commit()
            except RuntimeError:
                await asyncio.sleep(.2)
                continue
            if not row:
                await asyncio.sleep(.2)
                continue
            job = dict(row)
            try:
                # A verified manifest survives an index/acknowledgement failure.
                # Retry that integration locally instead of fetching new media.
                _, root, forbidden = destination(job['source_id'], job['root'])
                saved = root / job['path'] if job['path'] else None
                completed = False
                if saved is not None and saved.exists():
                    with get_user_db() as db:
                        registered = sources.list_sources(db)
                    source = next(s for s in registered if s.source_id == job['source_id'])
                    try:
                        archive.capture(source, registered, config.SUITE_HOME, job['path'])
                        completed = True
                    except archive.InvalidCapture:
                        if job['phase'] == 'registering':
                            raise
                if completed:
                    completion = asyncio.create_task(asyncio.to_thread(register, job))
                    try:
                        await asyncio.shield(completion)
                    except asyncio.CancelledError:
                        await completion
                        raise
                else:
                    await execute(job)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if isinstance(error, (ValueError, serving.ServeDenied)):
                    logger.warning('Twitter capture refused: job=%s; files preserved: %s', job['id'], error)
                else:
                    logger.exception('Twitter capture failed: job=%s; files preserved', job['id'])
                with _lock:
                    current = get(job['id'])
                    if current and current['status'] == 'running':
                        update(job['id'], status='failed', error=str(error)[:1000])
    finally:
        recover()

"""Durable UUID receipts and one supervised helper; completion is create-only."""
from services.operation_diagnostics import observed
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
from services import operations
from services import operation_diagnostics as diagnostics
from services.tool_diagnostics import ToolEvidence, command_details
from modules.twitter import operational_state
from files_base import index, serving, sources
from maintenance import exclusive_tool_operation
from modules.twitter import archive
from modules.twitter.helpers import ffmpeg_executable

logger = logging.getLogger('keivotos.twitter')
_lock = threading.RLock()
ACTIVE = ('queued', 'running')



def get_jobs_db():
    return operational_state.connect(get_user_db)

def get(job_id):
    with get_jobs_db() as db:
        row = db.execute('SELECT * FROM twitter_downloads WHERE id=?', (job_id,)).fetchone()
    return dict(row) if row else None


def public(job):
    return {key: value for key, value in job.items() if key not in ('root', 'retry_requests')}


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
    missing = [name for name in ('yt_dlp', 'PIL') if importlib.util.find_spec(name) is None]
    if not ffmpeg_executable():
        missing.append('FFmpeg')
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
    selected_source=source_id
    source_id = source_id or preferences()['source_id']
    with _lock, exclusive_tool_operation('queuing a Twitter capture', wait=False), get_jobs_db() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM twitter_downloads WHERE request_id=?', (request_id,)).fetchone()
        if row:
            item = dict(row)
            if item['url'] != url or selected_source and item['source_id'] != selected_source:
                raise ValueError('This request token belongs to a different capture')
            return item
        acknowledged=operational_state.request_ack(get_user_db,request_id,url,selected_source or None)
        if acknowledged is not None:return acknowledged
        _, root, _ = destination(source_id)
        tools = dependencies()
        if not tools['ready']:
            raise ValueError('Missing capture dependencies: ' + ', '.join(tools['missing']))
        _capacity(db, post_id, source_id)
        db.execute('INSERT INTO twitter_downloads(id,request_id,url,post_id,source_id,root) VALUES(?,?,?,?,?,?)',
                   (request_id, request_id, url, post_id, source_id, str(root)))
        operational_state.accept(db,dict(db.execute('SELECT * FROM twitter_downloads WHERE id=?',(request_id,)).fetchone()))
        db.commit()
    with diagnostics.diagnostic_context(operation_owner="twitter",operation_id=request_id):
        diagnostics.emit("twitter","operation.requested","Capture queued",details={"state":"requested"})
    return get(request_id)


def update(job_id, **values):
    allowed = {'status', 'phase', 'progress', 'attempt', 'capture_id', 'path', 'error'}
    if not values or not set(values) <= allowed:
        raise ValueError('Invalid Twitter job fields')
    with _lock, get_jobs_db() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM twitter_downloads WHERE id=?',(job_id,)).fetchone()
        if not row:raise ValueError('Download not found')
        previous=dict(row)
        changed=any(key in values and values[key]!=previous.get(key) for key in ("status","phase","failure_phase"))
        operational_state.update(db,dict(row),values)
        db.execute('UPDATE twitter_downloads SET ' + ','.join(key + '=?' for key in values) +
                   ",updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (*values.values(), job_id))
        state=diagnostics.snapshot(db,"twitter",job_id) if changed else None
        db.commit()

    if state is not None:
        diagnostics.committed("twitter", job_id, state, previous=previous["status"])


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
    with _lock, exclusive_tool_operation('retrying a Twitter capture', wait=False), get_jobs_db() as db:
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
        operations.retry(db,'twitter',job_id,'retry:'+token)
        receipts.append(token)
        db.execute("UPDATE twitter_downloads SET status='queued',error='',retry_requests=?,"
                   "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (json.dumps(receipts), job_id))
        db.commit()
    with diagnostics.diagnostic_context(operation_owner="twitter",operation_id=job_id):
        diagnostics.emit("twitter","operation.retry_requested","Capture retry queued",
                         details={"reason":"user_requested","state":"requested"})
    return get(job_id)


def recover():
    # Restart/disable never admits remote work implicitly.
    recovered=[]
    with _lock, get_jobs_db() as db:
        operations.recover_processes(db,'twitter')
        db.execute('BEGIN IMMEDIATE')
        for row in operations.rows(db,"SELECT * FROM twitter_downloads WHERE status IN ('queued','running')"):
            values=dict(status='interrupted',error='Capture interrupted; use Retry. Files are preserved')
            operational_state.update(db,row,values)
            db.execute("UPDATE twitter_downloads SET status=?,error=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                (values['status'],values['error'],row['id']))
            recovered.append((row["id"], diagnostics.snapshot(db,"twitter",row["id"])))
        db.commit()

    for identity,state in recovered:
        diagnostics.committed("twitter", identity, state, reason="unfinished_work_recovered")


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


@observed('twitter', 'capture.prepare', identity='job.id', library='job.source_id')
def prepare(job):
    with get_jobs_db() as fence:
        operations.validate(fence,'twitter',job['id'])
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


@observed('twitter', 'capture.register', identity='job.id', library='job.source_id')
def register(job, *, catalog_database=None):
    with get_jobs_db() as fence:
        operations.validate(fence,'twitter',job['id'])
    catalog_database=catalog_database or operational_state.catalog(get_user_db)
    with exclusive_tool_operation('registering a saved Twitter capture', wait=False), _lock:
        current = get(job['id'])
        if current['status'] != 'running':
            raise ValueError('Capture state changed')
        if catalog_database is not None:
            from modules.twitter.user_schema import CATALOG_RECEIPT_FIELDS
            if any(current[name] != job[name] for name in CATALOG_RECEIPT_FIELDS):
                raise ValueError('Capture registration identity changed')
        source, root, forbidden = destination(job['source_id'], job['root'])
        with get_user_db() as db:
            registered = sources.list_sources(db)
        item = archive.capture(source, registered, config.SUITE_HOME, job['path'])
        if not item['available'] or item['post_id'] != job['post_id'] or item['source_url'] != job['url']:
            raise ValueError('Saved capture changed or is unavailable')
        with index.open_index(config.FILES_DB_PATH) as db:
            index.scan_source(db, source.source_id, root, excluded_roots=forbidden)
        if catalog_database is not None:
            # Staged integration only: catalog commit precedes the separate job
            # acknowledgement. A retained manifest permits exact local repair.
            from modules.twitter.user_schema import register_catalog_capture
            with catalog_database() as db:
                if db.in_transaction:
                    raise ValueError('Open a clean catalog registration connection')
                db.execute('BEGIN IMMEDIATE')
                try:
                    source, _, forbidden = destination(job['source_id'], job['root'])
                    with get_user_db() as source_db:
                        registered = sources.list_sources(source_db)
                    result = register_catalog_capture(db, source_id=source.source_id,
                        registered=registered, suite_home=config.SUITE_HOME, path=job['path'], receipt=job)
                    source, _, forbidden = destination(job['source_id'], job['root'])
                    with get_user_db() as source_db:
                        registered = sources.list_sources(source_db)
                    archive.recheck_assets(source, registered, config.SUITE_HOME, result['observation'])
                    from services import item_manifest as portable
                    relative=(Path(job['path']).parent/portable.MANIFEST).as_posix()
                    from storage_layout import portable_path
                    if portable_path(source.path,relative,forbidden,must_exist=False).exists():
                        value,observations=portable.read(source.path,relative,forbidden=forbidden,hash_assets=True)
                        context=portable.Context(source,tuple(registered),config.SUITE_HOME,config.FILES_DB_PATH,db,config.MODULE_REGISTRY)
                        portable.register(value,context,relative,observations=observations,register_owner=False)
                    operational_state.register_ack(db,result['item_id'],job)
                    current = get(job['id'])
                    if (not current or current['status'] != 'running'
                            or any(current[name] != job[name] for name in CATALOG_RECEIPT_FIELDS)):
                        raise ValueError('Capture registration state changed')
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
        with get_jobs_db() as state_db:
            operations.transition(state_db,'twitter',job['id'],state='working',publication='catalog-registered')
        update(job['id'], status='complete', phase='completed', progress=100, error='')


@observed('twitter', 'capture.publish', identity='spec.id', library='spec.source_id')
def publish(spec, files, *, catalog_database=None):
    from modules.twitter.acquisition import directory
    with get_jobs_db() as fence:
        operations.validate(fence,'twitter',spec['id'])
    catalog_database=catalog_database or operational_state.catalog(get_user_db)
    with exclusive_tool_operation('publishing a Twitter capture', wait=False), _lock:
        current = get(spec['id'])
        if current['status'] != 'running' or current['capture_id'] != spec['capture_id']:
            raise ValueError('Capture state changed before publication')
        source, root, forbidden = destination(spec['source_id'], spec['root'])
        spec = {**spec, 'forbidden': [str(p) for p in forbidden]}
        folder = directory(spec)
        limits = archive.asset_limits(files)
        for name, maximum in limits.items():
            value = files[name]
            target = serving.resolve_served_file(root, folder.relative_to(root).as_posix() + '/' + name, forbidden)
            if (type(value.get('size')) is not int or not 0 < value['size'] <= maximum or
                    not isinstance(value.get('sha256'), str) or not archive.HASH.fullmatch(value['sha256']) or
                    list(archive._identity(target.stat())) != value.get('identity')):
                raise ValueError('Capture asset changed after worker verification')
        with (folder / 'tweet.json').open('rb') as handle:
            raw = handle.read(archive.TWEET_LIMIT + 1)
        tweet = json.loads(raw, object_pairs_hook=archive._unique_object)
        if len(raw) > archive.TWEET_LIMIT or archive.tweet_asset_limits(tweet) != limits:
            raise ValueError('Worker photo assets do not match the saved metadata')
        manifest = {'schema_version': tweet['schema_version'], 'complete': True, 'post_id': spec['post_id'],
                    'capture_id': spec['capture_id'], 'captured_at': spec['captured_at'],
                    'completed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                    'source_url': spec['url'],
                    'files': {name: {'size': value['size'], 'sha256': value['sha256']} for name, value in files.items()}}
        raw=(json.dumps(manifest)+'\n').encode('utf-8')
        from modules.twitter.portable import envelope
        from services.item_manifest import write
        from services.item_manifest import collection_identity
        with get_user_db() as collection_db:library_id=collection_identity(collection_db,source)
        write(folder,envelope(folder,{**spec,'portable_library_id':library_id},manifest,raw),forbidden=forbidden)
        # The existing owner completion marker is still published last.
        with (folder / 'manifest.json').open('xb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        from modules.twitter import downloads
        with get_jobs_db() as history_db:
            downloads.complete(history_db,spec['id'],files,at=manifest['completed_at'])
        update(spec['id'], phase='registering', progress=98)
        register(get(spec['id']), catalog_database=catalog_database)


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


@observed('twitter', 'capture.download', identity='job.id', library='job.source_id', false_outcome='cancelled')
async def execute(job):
    spec = prepare(job)
    command = [sys.executable, '--twitter-worker'] if getattr(sys, 'frozen', False) else [sys.executable, str(config.CODE_ROOT / 'app.py'), '--twitter-worker']
    options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {'start_new_session': True}
    launch = asyncio.create_task(asyncio.create_subprocess_exec(*command, env=diagnostics.worker_environment(os.environ, profile=config.SUITE_HOME), stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options))
    try:
        process = await asyncio.shield(launch)
    except asyncio.CancelledError:
        process = await launch
        await terminate(process)
        raise
    evidence=ToolEvidence("twitter", command, destination=spec["path"], packages=("yt-dlp",))
    executable=ffmpeg_executable()
    if executable:diagnostics.emit("twitter", "tool.dependency", "Configured FFmpeg", details=command_details([executable]))
    result, failure = None, ''
    async def events():
        nonlocal result, failure
        while line := await process.stdout.readline():
            evidence.output(line, "stdout", protocol=True)
            event = json.loads(line)
            if event.get('event') == 'complete':
                result = event['files']
            elif event.get('event') == 'file':
                from modules.twitter import downloads
                current=get(job['id'])
                if not current or current['status'] in ('cancelled','interrupted'):continue
                try:
                    with get_jobs_db() as history_db:
                        downloads.event(history_db,job['id'],event)
                except operations.StaleAttempt:
                    current=get(job['id'])
                    if not current or current['status'] in ('cancelled','interrupted'):return
                    raise
            elif event.get('event') == 'failure':
                failure = str(event.get('error', 'Capture failed'))[:1000]
            elif event.get('event') == 'progress':
                progress = event.get('progress')
                if event.get('phase') not in ('metadata', 'avatar', 'video', 'photos', 'verifying') or type(progress) not in (int, float) or not math.isfinite(progress):
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
    async def read_diagnostics():
        while line := await process.stderr.readline():
            evidence.output(line, "stderr")
            logger.info("job=%s %s",job["id"],line.decode("utf-8","replace").strip()[:2000])
    validated=False
    tasks = []
    try:
        worker_spec = {key: spec[key] for key in ('url', 'post_id', 'root', 'path', 'capture_id', 'captured_at', 'forbidden', 'directory_identity')}
        with get_jobs_db() as process_db:
            operations.register_process(process_db,'twitter',job['id'],process.pid)
            from services import download_controls
            download_controls.register_worker(process_db,'twitter',job['id'],process.pid)
        process.stdin.write((json.dumps(worker_spec) + '\n').encode('utf-8'))
        await process.stdin.drain()
        process.stdin.close()
        tasks = [asyncio.create_task(events()), asyncio.create_task(read_diagnostics())]
        elapsed=0.0;last_tick=time.monotonic()
        while process.returncode is None:
            current = get(job['id'])
            if current['status'] != 'running':
                await terminate(process)
                return False
            destination(job['source_id'], job['root'])
            tick=time.monotonic()
            with get_jobs_db() as pause_db:held=download_controls.paused(pause_db)
            if not held:elapsed+=tick-last_tick
            last_tick=tick
            if elapsed > 1800:
                raise ValueError('Capture timed out; files were preserved')
            if any(task.done() and task.exception() for task in tasks):
                raise ValueError('Capture event stream failed')
            await asyncio.sleep(.1)
        await asyncio.gather(*tasks)
        if get(job['id'])['status']!='running':return False
        if process.returncode or result is None:
            raise ValueError(failure or 'Capture failed; see Logs')
        validated=True
        completion = asyncio.create_task(asyncio.to_thread(publish, spec, result))
        try:
            await asyncio.shield(completion)
        except asyncio.CancelledError:
            await completion
            raise
    finally:
        escaped=sys.exc_info()[1]
        await terminate(process)
        from services import download_controls
        with get_jobs_db() as process_db:download_controls.unregister_worker(process_db,'twitter',job['id'],process.pid)
        try:current=get(job["id"])
        except Exception:current=None
        state=current["status"] if current else "interrupted"
        outcome="cancelled" if state=="cancelled" else "interrupted" if state=="interrupted" or isinstance(escaped,asyncio.CancelledError) else "failed" if not validated else None
        evidence.finish(process.returncode, outcome=outcome,
                        reason="user_cancelled" if state=="cancelled" else "shutdown_or_disable" if isinstance(escaped,asyncio.CancelledError) else None, validation=validated)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_queue():
    try:
        while True:
            try:
                with _lock, exclusive_tool_operation('admitting a Twitter capture', wait=False), get_jobs_db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    from services.download_controls import paused
                    row = None if paused(db) else db.execute("SELECT * FROM twitter_downloads WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
                    if row:
                        operational_state.ensure(db,dict(row))
                        token=operations.claim(db,'twitter',row['id'])
                        db.execute("UPDATE twitter_downloads SET status='running' WHERE id=?", (row['id'],))
                        db.commit()
            except RuntimeError:
                await asyncio.sleep(.2)
                continue
            if not row:
                await asyncio.sleep(.2)
                continue
            with operations.bind('twitter',row['id'],token):
                diagnostics.emit("twitter", "operation.attempt_started", "Download attempt started",
                                 details={"attempt_kind":"download"})
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
                except operations.StaleAttempt:
                    logger.info('Obsolete Twitter attempt ignored: job=%s',job['id'])
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

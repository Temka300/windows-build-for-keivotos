"""Read-only TinyTag integration; bounded work and disposable Files cache only.

Listings call cached(), which never opens audio. Explicit reads use at most two
daemon readers, without a work queue. The caller has a hard wait limit; a reader
still inside OS IO keeps its slot until it exits and can never publish late data.
TinyTag also receives byte/operation/deadline budgets and never requests artwork.
"""
from concurrent.futures import Future, TimeoutError
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time

from files_base import index, serving

VERSION = 2
MAX_READ_BYTES = 8 * 1024 * 1024
MAX_OPERATIONS = 32768
MAX_RESULT_BYTES = 128 * 1024
MAX_CACHE_ENTRIES = 4096
READ_TIMEOUT = 2.0
_readers = threading.BoundedSemaphore(2)
_PROPERTIES = ('duration', 'channels', 'bitrate', 'bitdepth', 'samplerate',
               'mime_type', 'is_lossless')


class BudgetExceeded(Exception):
    pass


class _BudgetFile:
    def __init__(self, handle, deadline):
        self.handle = handle
        self.deadline = deadline
        self.bytes = 0
        self.operations = 0

    def _check(self):
        self.operations += 1
        if self.operations > MAX_OPERATIONS or time.monotonic() > self.deadline:
            raise BudgetExceeded()

    def read(self, size=-1):
        self._check()
        if size < 0:
            size = os.fstat(self.handle.fileno()).st_size - self.handle.tell()
        if size > MAX_READ_BYTES - self.bytes:
            raise BudgetExceeded()
        data = self.handle.read(size)
        self.bytes += len(data)
        self._check()
        return data

    def seek(self, offset, whence=0):
        self._check()
        return self.handle.seek(offset, whence)

    def tell(self):
        self._check()
        return self.handle.tell()


def fallback(relative_path, status='pending'):
    return {'status': status, 'tags': {}, 'display_title': Path(relative_path).stem,
            **{name: None for name in _PROPERTIES}}


def _stat_key(stat):
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _snapshot(connection, source, relative_path, forbidden):
    target = serving.resolve_served_file(source.path, relative_path, forbidden)
    facts = _stat_key(target.stat())
    row = connection.execute(
        'SELECT seen_at FROM files_index WHERE source_id=? AND relative_path=?',
        (source.source_id, relative_path)).fetchone()
    revision = row['seen_at'] if row else None
    identity = hashlib.sha256(json.dumps(
        [VERSION, str(Path(source.path).resolve()), str(target), facts],
        ensure_ascii=True).encode()).hexdigest()
    return target, facts, identity


def _cached(connection, source_id, relative_path, identity):
    row = connection.execute(
        'SELECT payload FROM files_audio_metadata WHERE source_id=? AND relative_path=? AND identity=?',
        (source_id, relative_path, identity)).fetchone()
    if row:
        try:
            assert len(row['payload'].encode()) <= MAX_RESULT_BYTES
            result = json.loads(row['payload'])
            assert result['status'] in ('ready', 'invalid', 'limited')
            assert isinstance(result['display_title'], str)
            assert isinstance(result['tags'], dict)
            assert all(isinstance(key, str) and isinstance(values, list)
                       and all(isinstance(value, str) for value in values)
                       for key, values in result['tags'].items())
            assert all(key in result for key in _PROPERTIES)
            assert all(value is None or (isinstance(value, (int, float)) and not isinstance(value, bool)
                                         and math.isfinite(value) and value >= 0)
                       for value in (result[key] for key in ('duration', 'channels', 'bitrate', 'bitdepth', 'samplerate')))
            assert result['mime_type'] is None or isinstance(result['mime_type'], str)
            assert result['is_lossless'] is None or isinstance(result['is_lossless'], bool)
            return result
        except (ValueError, TypeError, KeyError, AssertionError):
            pass
    return None


def cached(connection, source, relative_path, forbidden):
    """Fresh guards/identity and SQLite only; never parse audio during listing."""
    if Path(relative_path).suffix.lower().lstrip('.') not in serving._INLINE_AUDIO:
        return fallback(relative_path, 'unsupported')
    try:
        _, _, identity = _snapshot(connection, source, relative_path, forbidden)
        return _cached(connection, source.source_id, relative_path, identity) or fallback(relative_path)
    except (serving.ServeDenied, OSError, ValueError):
        return fallback(relative_path, 'unavailable')


def _extract(source, relative_path, forbidden, expected):
    from tinytag import TinyTag

    result = fallback(relative_path)
    deadline = time.monotonic() + READ_TIMEOUT
    try:
        target = serving.resolve_served_file(source.path, relative_path, forbidden)
        with target.open('rb') as handle:
            # Resolve again after opening, before the parser sees any bytes.
            current = serving.resolve_served_file(source.path, relative_path, forbidden)
            if _stat_key(os.fstat(handle.fileno())) != expected or _stat_key(current.stat()) != expected:
                return fallback(relative_path, 'unavailable')
            bounded = _BudgetFile(handle, deadline)
            tag = TinyTag.get(filename=str(target), file_obj=bounded, image=False)
            bounded._check()
            raw = tag.as_dict()
            # Keep TinyTag's normalized, multi-value text exactly; filename fallback
            # is presentation only and never becomes a claimed embedded title.
            result['tags'] = {key: values for key, values in raw.items() if isinstance(values, list)}
            result['display_title'] = next((value.strip() for value in result['tags'].get('title', [])
                                            if value.strip()), Path(relative_path).stem)
            for key in _PROPERTIES:
                value = getattr(tag, key)
                if isinstance(value, (int, float)) and (not math.isfinite(value) or value < 0):
                    value = None
                result[key] = value
            result['status'] = 'ready'
            if len(json.dumps(result, ensure_ascii=True).encode()) > MAX_RESULT_BYTES:
                raise BudgetExceeded()
            bounded._check()
    except serving.ServeDenied:
        raise
    except Exception as error:
        # TinyTag wraps reader exceptions inside ParseError.
        cause = error
        while cause.__cause__ is not None:
            cause = cause.__cause__
        result = fallback(relative_path, 'limited' if isinstance(cause, BudgetExceeded) else 'invalid')
    return result


def read(db, source, relative_path, forbidden, *, refresh=False):
    """Read one guarded file on demand. No media/user data is ever written."""
    with index.open_index(db) as connection:
        _, expected, identity = _snapshot(connection, source, relative_path, forbidden)
        if Path(relative_path).suffix.lower().lstrip('.') not in serving._INLINE_AUDIO:
            return fallback(relative_path, 'unsupported')
        warm = _cached(connection, source.source_id, relative_path, identity)
        if warm is not None and not refresh:
            return warm
    permits = _readers
    if not permits.acquire(blocking=False):
        return fallback(relative_path, 'busy')
    future = Future()

    def work():
        try:
            future.set_result(_extract(source, relative_path, forbidden, expected))
        except BaseException as error:
            future.set_exception(error)
        finally:
            permits.release()

    reader = threading.Thread(target=work, name='audio-metadata', daemon=True)
    try:
        reader.start()
    except BaseException:
        permits.release()
        raise
    try:
        result = future.result(timeout=READ_TIMEOUT)
    except TimeoutError:
        # A late reader has no database handle and cannot publish its result.
        from services.operation_diagnostics import failure
        failure("ost","metadata.read_limited",reason="Audio metadata read limit reached",
                details={"reason":"deadline"})
        return fallback(relative_path, 'limited')
    with index.open_index(db) as connection:
        _, _, current = _snapshot(connection, source, relative_path, forbidden)
        if current != identity or result['status'] == 'unavailable':
            return fallback(relative_path, 'unavailable')
        connection.execute(
            'INSERT OR REPLACE INTO files_audio_metadata(source_id,relative_path,identity,payload,updated_at) '
            'VALUES(?,?,?,?,?)', (source.source_id, relative_path, identity,
                                  json.dumps(result, ensure_ascii=True), time.time_ns()))
        connection.execute(
            'DELETE FROM files_audio_metadata WHERE rowid IN '
            '(SELECT rowid FROM files_audio_metadata ORDER BY updated_at DESC,rowid DESC LIMIT -1 OFFSET ?)',
            (MAX_CACHE_ENTRIES,))
        connection.commit()
    return result

"""Verified, non-overwriting SQLite snapshots for staged data transitions.

Callers resolve/guard configured paths and coordinate the maintenance gate.
This module does not select an active database, migrate live profiles at import,
or implement cross-database cutover. Failed candidates remain for recovery.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import tempfile
from typing import Callable

from services import item_catalog


@dataclass(frozen=True)
class DatabaseSnapshot:
    destination: Path
    schema_sha256: str
    table_sha256: dict[str, str]
    table_rows: dict[str, int]


class SnapshotError(RuntimeError):
    def __init__(self, message: str, staging_path: Path):
        super().__init__(message)
        self.staging_path = staging_path


def _identifier(value):
    return '"' + value.replace('"', '""') + '"'


def _cell(value):
    if value is None:
        return b'n'
    if isinstance(value, bytes):
        tag, data = b'b', value
    elif isinstance(value, str):
        tag, data = b's', value.encode('utf-8')
    elif isinstance(value, int):
        tag, data = b'i', str(value).encode('ascii')
    elif isinstance(value, float):
        tag, data = b'f', struct.pack('!d', value)
    else:
        raise ValueError('Unsupported SQLite value')
    return tag + len(data).to_bytes(8, 'big') + data


def _manifest(connection, *, schema_objects=None, table_names=None):
    schema = hashlib.sha256()
    for row in connection.execute('SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name'):
        if schema_objects is not None and (row[0], row[1]) not in schema_objects:
            continue
        for value in row:
            schema.update(_cell(value))
    hashes, counts = {}, {}
    tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for table in tables:
        if table_names is not None and table not in table_names:
            continue
        quoted = _identifier(table)
        columns = [row[1] for row in connection.execute(f'PRAGMA table_info({quoted})')]
        if not columns:
            raise ValueError('Cannot verify a table without readable columns')
        ordering = ','.join(_identifier(column) + ' COLLATE BINARY' for column in columns)
        digest = hashlib.sha256()
        count = 0
        for row in connection.execute(f'SELECT * FROM {quoted} ORDER BY {ordering}'):
            digest.update(b'r')
            for value in row:
                digest.update(_cell(value))
            count += 1
        hashes[table], counts[table] = digest.hexdigest(), count
    return schema.hexdigest(), hashes, counts


def snapshot_user_database(source: Path, destination: Path) -> DatabaseSnapshot:
    """Publish a verified new snapshot, preserving source and existing targets.

    The read transaction includes schema/data fingerprints and backup, so WAL
    writes committed concurrently cannot produce two different source revisions.
    Publication uses a same-directory hard link for atomic create-if-absent;
    unsupported filesystems fail with their verified candidate retained.
    """
    source = Path(source).resolve(strict=True)
    destination = Path(destination).absolute()
    if os.path.lexists(destination):
        raise FileExistsError(destination)
    parent = destination.parent.resolve(strict=True)
    destination = parent / destination.name
    if source == destination:
        raise ValueError('Snapshot destination must differ from its source')
    descriptor, name = tempfile.mkstemp(prefix='.' + destination.name + '.', suffix='.partial', dir=parent)
    os.close(descriptor)
    candidate = Path(name)
    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original:
            original.execute('BEGIN')
            expected = _manifest(original)
            with closing(sqlite3.connect(candidate)) as copied:
                original.backup(copied)
                if copied.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise ValueError('Snapshot failed SQLite integrity verification')
                if _manifest(copied) != expected:
                    raise ValueError('Snapshot differs from its consistent source revision')
        with candidate.open('r+b') as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.link(candidate, destination)
        if os.name != 'nt':
            directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        candidate.unlink()
        return DatabaseSnapshot(destination, *expected)
    except Exception as error:
        raise SnapshotError('Snapshot was not completed; candidate retained: ' + str(error), candidate) from error


@dataclass(frozen=True)
class PreparedEssential:
    attempt_directory: Path
    candidate: Path
    legacy_table_rows: dict[str, int]
    registered_libraries: int
    imported_items: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class CatalogImporter:
    """Trusted owner callback; must retain the caller's transaction/legacy rows."""

    name: str
    version: int
    import_records: Callable[[sqlite3.Connection], int] = field(repr=False, compare=False)


def _configured_importers(importers):
    importers = tuple(importers)
    names = set()
    for importer in importers:
        if (not isinstance(importer, CatalogImporter) or not isinstance(importer.name, str)
                or not re.fullmatch(r'[a-z][a-z0-9_.:-]{0,63}', importer.name)
                or type(importer.version) is not int or not 1 <= importer.version <= 2**31 - 1
                or not callable(importer.import_records) or importer.name in names):
            raise ValueError('Invalid or duplicate catalog importer')
        names.add(importer.name)
    ordered = tuple(sorted(importers, key=lambda importer: importer.name))
    configuration = [dict(name=importer.name, version=importer.version) for importer in ordered]
    return ordered, configuration


class PreparationError(RuntimeError):
    """Preparation cannot continue; all source/candidate evidence is retained."""

    def __init__(self, message: str, attempt_directory: Path):
        super().__init__(message)
        self.attempt_directory = attempt_directory


def _state(connection, **selection):
    schema, hashes, rows = _manifest(connection, **selection)
    return dict(schema_sha256=schema, table_sha256=hashes, table_rows=rows,
                database_header={name: connection.execute('PRAGMA ' + name).fetchone()[0]
                                 for name in ('user_version', 'application_id')})


@contextmanager
def _read_database(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.execute('BEGIN')
        yield connection


@contextmanager
def _attempt_lock(attempt):
    """Kernel-owned lock survives neither process exit nor a crashed owner."""
    path = attempt / '.prepare.lock'
    if path.is_symlink():
        raise PreparationError('Refused symbolic-link preparation lock', attempt)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'r+b') as handle:
        if os.name == 'nt':
            import msvcrt
            acquire = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as error:
            raise PreparationError('Preparation is busy or cannot be locked', attempt) from error
        try:
            yield
        finally:
            release()


def _record_phase(attempt, phase, **facts):
    """Publish one immutable receipt; never replace an earlier phase record."""
    payload = json.dumps(dict(format=1, phase=phase, **facts), sort_keys=True, ensure_ascii=False).encode('utf-8') + b'\n'
    descriptor, name = tempfile.mkstemp(prefix='.' + phase + '.', suffix='.partial', dir=attempt)
    temporary = Path(name)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.link(temporary, attempt / (phase + '.json'))
    if os.name != 'nt':
        directory = os.open(attempt, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    temporary.unlink()


def _receipt(attempt, phase):
    path = attempt / (phase + '.json')
    if path.is_symlink():
        raise PreparationError('Refused symbolic-link preparation receipt', attempt)
    try:
        with path.open('rb') as handle:
            payload = handle.read(4 * 1024 * 1024 + 1)
    except OSError as error:
        raise PreparationError('Cannot read ' + phase + ' preparation receipt', attempt) from error
    if len(payload) > 4 * 1024 * 1024:
        raise PreparationError('Preparation receipt is too large', attempt)
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError) as error:
        raise PreparationError('Invalid ' + phase + ' preparation receipt', attempt) from error
    if not isinstance(value, dict) or type(value.get('format')) is not int or value['format'] != 1 or value.get('phase') != phase:
        raise PreparationError('Unsupported preparation receipt format or phase', attempt)
    return value


def _require_source(source, expected, attempt):
    with _read_database(source) as connection:
        if _state(connection) != expected:
            raise PreparationError('Source changed since the snapshot; prepare a new attempt', attempt)


def _import_registered_sources(connection):
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='files_sources'").fetchone()
    if not exists:
        return 0
    columns = {row[1] for row in connection.execute('PRAGMA table_info(files_sources)')}
    if not {'source_id', 'display_name'} <= columns:
        raise ValueError('Unsupported registered-source schema')
    count = 0
    # No filesystem reads or role/visibility filtering: unavailable/disabled
    # sources remain essential registrations, with their original opaque IDs.
    for source_id, display_name in connection.execute('SELECT source_id,display_name FROM files_sources ORDER BY source_id'):
        item_catalog.ensure_library(connection, namespace='files.source', legacy_key=source_id,
                                    library_id=source_id, kind='linked', display_name=display_name or source_id)
        count += 1
    return count


def _require_owned_additions(connection, baseline_objects, attempt):
    # Partially completed catalog DDL is restartable, but unrelated candidate
    # mutations must not be blessed merely because legacy rows still match.
    with closing(sqlite3.connect(':memory:')) as reference:
        reference.executescript(item_catalog.SCHEMA)
        owned = {(kind, name): sql for kind, name, sql in reference.execute(
            'SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL')}
    for kind, name, sql in connection.execute('SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL'):
        if (kind, name) not in baseline_objects and owned.get((kind, name)) != sql:
            raise PreparationError('Unexpected schema changes in the candidate; preserved attempt needs review', attempt)


def _validate_prepared(source, attempt, configuration):
    snapshot = _receipt(attempt, 'snapshot')
    prepared = _receipt(attempt, 'prepared')
    imported = prepared.get('imported_items', {})
    if (not isinstance(imported, dict) or set(imported) != {entry['name'] for entry in configuration}
            or any(type(count) is not int or count < 0 for count in imported.values())):
        raise PreparationError('Invalid prepared importer counts', attempt)
    _require_source(source, snapshot['manifest'], attempt)
    candidate = attempt / 'essential.sqlite'
    if candidate.is_symlink():
        raise PreparationError('Refused symbolic-link candidate', attempt)
    with _read_database(candidate) as connection:
        if _state(connection) != prepared['manifest']:
            raise PreparationError('Prepared candidate changed; preserved attempt needs review', attempt)
    return PreparedEssential(attempt, candidate, snapshot['manifest']['table_rows'], prepared['registered_libraries'], imported)


def _attempt_path(directory):
    directory = Path(directory).absolute()
    return directory.parent.resolve(strict=True) / directory.name


def _require_intent(source, attempt, configuration):
    intent = _receipt(attempt, 'intent')
    if intent.get('kind') != 'essential-candidate' or intent.get('source') != str(source):
        raise PreparationError('Preparation belongs to a different source', attempt)
    recorded = intent.get('catalog_importers', [])
    if recorded != configuration or any(type(entry['version']) is not int for entry in recorded):
        raise PreparationError('Catalog importer configuration changed; prepare a new attempt', attempt)


def prepare_essential_candidate(source: Path, attempt_directory: Path, *,
                                catalog_importers=()) -> PreparedEssential:
    """Prepare/resume a separate legacy copy with catalog/source aliases.

    The caller supplies a guarded recovery location; no active authority is
    selected. A changed source/importer configuration requires a fresh attempt,
    not replacement of preserved snapshots. Explicitly supplied versioned owner
    importers run in the alias transaction; no optional owner is auto-imported.
    """
    importers, configuration = _configured_importers(catalog_importers)
    source = Path(source).resolve(strict=True)
    attempt = _attempt_path(attempt_directory)
    fresh = not os.path.lexists(attempt)
    if fresh:
        attempt.mkdir(mode=0o700)
    elif attempt.is_symlink() or not attempt.is_dir():
        raise PreparationError('Refused non-directory or symbolic-link attempt', attempt)
    else:
        _require_intent(source, attempt, configuration)
    try:
        with _attempt_lock(attempt):
            if fresh:
                _record_phase(attempt, 'intent', kind='essential-candidate', source=str(source),
                              catalog_importers=configuration)
            else:
                _require_intent(source, attempt, configuration)
            if (attempt / 'prepared.json').exists():
                return _validate_prepared(source, attempt, configuration)
            candidate = attempt / 'essential.sqlite'
            if candidate.is_symlink():
                raise PreparationError('Refused symbolic-link candidate', attempt)
            if not (attempt / 'snapshot.json').exists():
                if not candidate.exists():
                    snapshot_user_database(source, candidate)
                with _read_database(candidate) as connection:
                    baseline = _state(connection)
                    objects = [list(row) for row in connection.execute(
                        'SELECT type,name FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name')]
                # Recover a crash after snapshot publication but before its
                # receipt only when the complete copy still matches the source.
                _require_source(source, baseline, attempt)
                _record_phase(attempt, 'snapshot', manifest=baseline, schema_objects=objects)
            snapshot = _receipt(attempt, 'snapshot')
            _require_source(source, snapshot['manifest'], attempt)
            with closing(sqlite3.connect(candidate.as_uri() + '?mode=rw', uri=True)) as connection:
                expected = snapshot['manifest']
                selection = dict(schema_objects={tuple(row) for row in snapshot['schema_objects']},
                                 table_names=set(expected['table_rows']))
                if _state(connection, **selection) != expected:
                    raise PreparationError('Candidate legacy records changed; preserved attempt needs review', attempt)
                _require_owned_additions(connection, selection['schema_objects'], attempt)
                item_catalog.ensure_catalog_schema(connection)
                with connection:
                    connection.execute('BEGIN IMMEDIATE')
                    libraries = _import_registered_sources(connection)
                    imported = {}
                    for importer in importers:
                        count = importer.import_records(connection)
                        if type(count) is not int or count < 0 or not connection.in_transaction:
                            raise ValueError('Catalog importer must return a count and retain the transaction')
                        imported[importer.name] = count
                    if _state(connection, **selection) != expected:
                        raise PreparationError('Catalog preparation changed legacy records', attempt)
                if _state(connection, **selection) != expected:
                    raise PreparationError('Catalog preparation changed legacy records', attempt)
                final = _state(connection)
            _require_source(source, expected, attempt)
            _record_phase(attempt, 'prepared', manifest=final, registered_libraries=libraries, imported_items=imported)
            return _validate_prepared(source, attempt, configuration)
    except PreparationError:
        raise
    except Exception as error:
        raise PreparationError('Preparation failed; evidence retained: ' + str(error), attempt) from error


def validate_essential_candidate(source: Path, attempt_directory: Path, *,
                                 catalog_importers=()) -> PreparedEssential:
    """Recheck a ready candidate and its source without activating either."""
    _, configuration = _configured_importers(catalog_importers)
    source = Path(source).resolve(strict=True)
    attempt = _attempt_path(attempt_directory)
    try:
        if attempt.is_symlink() or not attempt.is_dir():
            raise PreparationError('Refused non-directory or symbolic-link attempt', attempt)
        _require_intent(source, attempt, configuration)
        with _attempt_lock(attempt):
            return _validate_prepared(source, attempt, configuration)
    except PreparationError:
        raise
    except Exception as error:
        raise PreparationError('Preparation validation failed; evidence retained: ' + str(error), attempt) from error

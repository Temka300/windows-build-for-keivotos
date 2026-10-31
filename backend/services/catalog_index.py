"""Explicit, owner-composed index preparation; never selects live authority."""
from __future__ import annotations

from services.operation_diagnostics import observed
from contextlib import closing
from dataclasses import dataclass, field
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
from typing import Callable
from uuid import uuid4

from database_connections import exclusive_database_access
from services.data_migrations import _attempt_lock, _manifest, _record_phase, _receipt
from services.item_catalog import _write

ARCHIVE_TABLES = ('kv_index_archive_meta', 'kv_index_archives', 'kv_index_archive_rows')
ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv_index_archive_meta (
    id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kv_index_archives (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, version INTEGER NOT NULL,
    definitions TEXT NOT NULL, manifest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv_index_archive_rows (
    archive_id TEXT NOT NULL, table_name TEXT NOT NULL, position INTEGER NOT NULL,
    payload TEXT NOT NULL, PRIMARY KEY(archive_id,table_name,position),
    FOREIGN KEY(archive_id) REFERENCES kv_index_archives(id));
"""
INDEX_SCHEMA = """
CREATE TABLE kv_index_meta (
    id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL,
    generation TEXT NOT NULL, selection TEXT NOT NULL);
CREATE TABLE kv_index_owners (
    owner TEXT PRIMARY KEY, version INTEGER NOT NULL, tables_json TEXT NOT NULL);
"""


@dataclass(frozen=True)
class IndexOwner:
    slug: str
    tables: tuple[str, ...]
    initialize: Callable
    rebuild: Callable
    version: int = 1
    requires: tuple[str, ...] = ()
    legacy_tables: tuple[str, ...] = ()
    source_config: tuple[tuple[str, str], ...] = ()
    observe: Callable | None = None
    preserve_result: bool = False


@dataclass
class IndexBuild:
    connection: sqlite3.Connection
    essential: sqlite3.Connection
    sources: tuple
    suite_home: Path
    cancelled: Callable
    generation: str
    observations: dict = field(default_factory=dict)

    def check(self):
        if self.cancelled():
            raise InterruptedError('Index preparation cancelled; evidence retained')


@dataclass(frozen=True)
class PreparedIndex:
    candidate: Path
    attempt: Path
    generation: str
    archives: dict[str, str]
    owners: tuple[str, ...]


def quoted(name):
    if not isinstance(name, str) or not name or '\0' in name:
        raise ValueError('Invalid index identifier')
    return '"' + name.replace('"', '""') + '"'


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _pack(row):
    def cell(value):
        if isinstance(value, bytes):
            return ['b', base64.b64encode(value).decode('ascii')]
        if isinstance(value, float):
            return ['f', struct.pack('!d', value).hex()]
        return ['n' if value is None else 'i' if isinstance(value, int) else 's', value]
    return _json([cell(value) for value in row])


def _unpack(payload):
    result = []
    for kind, value in json.loads(payload):
        if kind == 'b':
            value = base64.b64decode(value, validate=True)
        elif kind == 'f':
            value = struct.unpack('!d', bytes.fromhex(value))[0]
        elif not ((kind == 'n' and value is None) or
                  (kind == 'i' and type(value) is int) or
                  (kind == 's' and isinstance(value, str))):
            raise ValueError('Invalid preserved SQLite value')
        result.append(value)
    return tuple(result)


def _tuple_cursor(connection):
    cursor = connection.cursor()
    cursor.row_factory = None
    return cursor


def _owned_manifest(connection, tables):
    with closing(_tuple_cursor(connection)) as cursor:
        objects = {(kind, name) for kind, name, table in cursor.execute(
            "SELECT type,name,tbl_name FROM sqlite_master WHERE sql IS NOT NULL")
            if table in tables}
        manifest = _manifest(cursor, schema_objects=objects, table_names=tables)
        sequences = []
        if cursor.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
            sequences = [list(row) for row in cursor.execute('SELECT name,seq FROM sqlite_sequence ORDER BY name')
                         if row[0] in tables]
        return (_digest([manifest[0],sequences]), manifest[1], manifest[2])


def _archive_schema(connection):
    with closing(_tuple_cursor(connection)) as cursor:
        names = {row[0] for row in cursor.execute(
            "SELECT name FROM sqlite_master WHERE name IN (?,?,?)", ARCHIVE_TABLES)}
    if names:
        if len(names) != 3:
            raise ValueError('Incomplete index preservation schema')
        with closing(_tuple_cursor(connection)) as cursor:
            if cursor.execute('SELECT id,version FROM kv_index_archive_meta').fetchall() != [(1, 1)]:
                raise ValueError('Unsupported index preservation version')
    else:
        # Do not use executescript: preservation must retain caller savepoints.
        for sql in ARCHIVE_SCHEMA.split(';'):
            if sql.strip():
                connection.execute(sql)
        connection.execute('INSERT INTO kv_index_archive_meta VALUES(1,1)')


@observed('suite', 'index.preserve_owner')
def preserve_owner(essential, source, owner):
    """Immutable typed source evidence lives with the existing User data backup."""
    if source.in_transaction or essential.in_transaction:
        raise ValueError('Index preservation requires clean connections')
    if not is_combined(source):
        with closing(_tuple_cursor(source)) as check:
            tables = {row[0] for row in check.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if check.execute("SELECT 1 FROM sqlite_master WHERE type='view'").fetchone():
                raise ValueError('Unclassified legacy index view; original evidence retained')
        if not tables <= set(owner.tables) | set(owner.legacy_tables):
            raise ValueError('Unclassified legacy index tables; original evidence retained')
    with closing(_tuple_cursor(source)) as cursor:
        cursor.execute('BEGIN')
        try:
            definitions = list(cursor.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END,name"))
            definitions = [list(row) for row in definitions if row[2] in owner.tables]
            if any(row[0] not in ('table', 'index') for row in definitions):
                raise ValueError('Unsupported index trigger/view; original evidence retained')
            if cursor.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
                definitions.extend(['sequence',name,name,sequence] for name,sequence in
                                   cursor.execute('SELECT name,seq FROM sqlite_sequence ORDER BY name')
                                   if name in owner.tables)
            for table in owner.tables:
                for foreign in cursor.execute('PRAGMA foreign_key_list('+quoted(table)+')'):
                    if foreign[2] not in owner.tables:
                        raise ValueError('Index owner has a cross-owner foreign key')
            manifest = _owned_manifest(source, owner.tables)
            description = dict(owner=owner.slug, version=owner.version,
                               definitions=definitions, manifest=manifest)
            identity = _digest(description)
            with _write(essential):
                _archive_schema(essential)
                with closing(_tuple_cursor(essential)) as target:
                    prior = target.execute('SELECT owner,version,definitions,manifest FROM kv_index_archives WHERE id=?',
                                           (identity,)).fetchone()
                    expected = (owner.slug, owner.version, _json(definitions), _json(manifest))
                    if prior is not None:
                        if prior != expected:
                            raise ValueError('Conflicting index preservation receipt')
                        verify_archive(essential, identity, owner)
                        _select_archive(essential,owner.slug,identity)
                        return identity
                    essential.execute('INSERT INTO kv_index_archives VALUES(?,?,?,?,?)', (identity, *expected))
                    for table in owner.tables:
                        if table not in manifest[1]:
                            continue
                        fields = [row[1] for row in cursor.execute('PRAGMA table_info('+quoted(table)+')')]
                        ordering = ','.join(quoted(field)+' COLLATE BINARY' for field in fields)
                        cursor.execute('SELECT * FROM '+quoted(table)+' ORDER BY '+ordering)
                        position = 0
                        while batch := cursor.fetchmany(64):
                            essential.executemany('INSERT INTO kv_index_archive_rows VALUES(?,?,?,?)',
                                ((identity, table, position+i, _pack(row)) for i, row in enumerate(batch)))
                            position += len(batch)
                    verify_archive(essential, identity, owner)
                    _select_archive(essential,owner.slug,identity)
            return identity
        finally:
            source.rollback()


def _archive(essential, identity, owner):
    with closing(_tuple_cursor(essential)) as cursor:
        row = cursor.execute('SELECT owner,version,definitions,manifest FROM kv_index_archives WHERE id=?',
                             (identity,)).fetchone()
    if row is None or row[:2] != (owner.slug, owner.version):
        raise ValueError('Unknown or incompatible index preservation')
    definitions, manifest = json.loads(row[2]), json.loads(row[3])
    if _digest(dict(owner=row[0], version=row[1], definitions=definitions, manifest=manifest)) != identity:
        raise ValueError('Index preservation receipt changed')
    if any(kind not in ('table', 'index', 'sequence') or table not in owner.tables
           or kind == 'sequence' and (type(sql) is not int or sql < 0)
           for kind, name, table, sql in definitions):
        raise ValueError('Invalid preserved index ownership')
    return definitions, manifest


def restore_owner(essential, destination, identity, owner):
    """Restore a verified owner into a private candidate, preserving unknown columns."""
    definitions, manifest = _archive(essential, identity, owner)
    # Only candidates call this: a failed partial candidate never becomes active.
    for table in reversed(owner.tables):
        destination.execute('DROP TABLE IF EXISTS '+quoted(table))
    for kind, name, table, sql in definitions:
        if kind == 'table':
            destination.execute(sql)
    with closing(_tuple_cursor(essential)) as cursor:
        for table in owner.tables:
            if table not in manifest[1]:
                continue
            columns = list(destination.execute('PRAGMA table_info('+quoted(table)+')'))
            # table_xinfo excludes generated columns from inserts.
            writable = [row for row in destination.execute('PRAGMA table_xinfo('+quoted(table)+')') if row[6] == 0]
            if len(writable) != len(columns):
                raise ValueError('Unsupported generated index columns')
            cursor.execute('SELECT payload FROM kv_index_archive_rows WHERE archive_id=? AND table_name=? ORDER BY position',
                           (identity, table))
            while batch := cursor.fetchmany(64):
                destination.executemany('INSERT INTO '+quoted(table)+' VALUES('+','.join('?' for _ in columns)+')',
                                        (_unpack(row[0]) for row in batch))
    for kind, name, table, sql in definitions:
        if kind == 'index':
            destination.execute(sql)
    for kind, name, table, sequence in definitions:
        if kind == 'sequence':
            destination.execute('DELETE FROM sqlite_sequence WHERE name=?',(name,))
            destination.execute('INSERT INTO sqlite_sequence VALUES(?,?)',(name,sequence))
    if _json(_owned_manifest(destination, owner.tables)) != _json(manifest):
        raise ValueError('Preserved index verification failed')
    return manifest


def verify_archive(essential, identity, owner):
    with closing(sqlite3.connect(':memory:')) as scratch:
        restore_owner(essential, scratch, identity, owner)


def owners_for(registry, enabled):
    selected = []
    for descriptor in registry:
        if descriptor.is_base or descriptor.slug in enabled:
            if descriptor.catalog_index_provider is not None:
                selected.append(descriptor.catalog_index_provider())
    result = []
    while selected:
        ready = [owner for owner in selected if set(owner.requires) <= {v.slug for v in result}]
        if not ready:
            raise ValueError('Missing or cyclic index owner dependencies')
        owner = sorted(ready, key=lambda v: v.slug)[0]
        if any(set(owner.tables) & set(old.tables) or owner.slug == old.slug for old in result):
            raise ValueError('Conflicting index table ownership')
        result.append(owner)
        selected.remove(owner)
    return tuple(result)


def validate_index(connection, owners=None):
    with closing(_tuple_cursor(connection)) as cursor:
        meta = cursor.execute('SELECT id,version,generation,selection FROM kv_index_meta').fetchall()
        if len(meta) != 1 or meta[0][:2] != (1, 1):
            raise ValueError('Unsupported combined index')
        recorded = cursor.execute('SELECT owner,version,tables_json FROM kv_index_owners ORDER BY owner').fetchall()
        if owners is not None:
            expected = sorted((owner.slug, owner.version, _json(owner.tables)) for owner in owners)
            if recorded != expected:
                raise ValueError('Combined index owner selection changed')
        if any(version != 1 for _, version, _ in recorded):
            raise ValueError('Unsupported combined index owner')
        allowed = {'kv_index_meta', 'kv_index_owners', 'sqlite_sequence'}
        required = set()
        for _, _, tables in recorded:
            names = json.loads(tables)
            if (not isinstance(names,list) or not names or any(not isinstance(name,str) for name in names)
                    or len(names) != len(set(names)) or required.intersection(names)):
                raise ValueError('Conflicting combined index ownership')
            required.update(names)
            allowed.update(names)
        present = {row[0] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= present:
            raise ValueError('Incomplete combined index owner schema')
        if not present <= allowed:
            raise ValueError('Unclassified table in combined index')
        if cursor.execute("SELECT 1 FROM sqlite_master WHERE type='view'").fetchone():
            raise ValueError('Unclassified view in combined index')
        return meta[0][2]


def is_combined(connection):
    with closing(_tuple_cursor(connection)) as cursor:
        present = cursor.execute("SELECT 1 FROM sqlite_master WHERE name='kv_index_meta'").fetchone()
    if present:
        validate_index(connection)
    return bool(present)


def _personal_revision(connection):
    with closing(_tuple_cursor(connection)) as cursor:
        objects = {(kind, name) for kind, name, table in cursor.execute(
            'SELECT type,name,tbl_name FROM sqlite_master WHERE sql IS NOT NULL')
            if table not in (*ARCHIVE_TABLES,'kv_index_archive_heads')}
        tables = [row[0] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if row[0] not in (*ARCHIVE_TABLES,'kv_index_archive_heads')]
        return _digest(_manifest(cursor, schema_objects=objects, table_names=tables))


@observed('suite', 'index.prepare')
def prepare_index(attempt, *, essential_database, owners, legacy_paths, sources,
                  suite_home, lock_directory, archives=None, cancelled=lambda: False):
    """Prepare/retry a complete candidate; no runtime paths are selected here."""
    attempt = Path(attempt).absolute()
    if attempt.is_symlink() or attempt.resolve() != attempt:
        raise ValueError('Refused linked index preparation directory')
    attempt.mkdir(parents=True, exist_ok=True)
    lock_directory = Path(lock_directory).absolute()
    if lock_directory.is_symlink() or lock_directory.resolve() != lock_directory:
        raise ValueError('Refused linked profile lock directory')
    lock_directory.mkdir(parents=True, exist_ok=True)
    owners = tuple(owners)
    owned_tables = [table for owner in owners for table in owner.tables]
    if len(owned_tables) != len(set(owned_tables)) or len({o.slug for o in owners}) != len(owners):
        raise ValueError('Conflicting index ownership')
    inputs = dict(owners=[(o.slug,o.version,o.tables,o.source_config) for o in owners],
                  sources=[vars(s) for s in sources], suite_home=str(Path(suite_home).resolve()))
    with _attempt_lock(lock_directory), _attempt_lock(attempt), exclusive_database_access():
        with essential_database() as essential:
            if essential.in_transaction:
                raise ValueError('Open a clean essential connection')
            inputs['observations'] = {owner.slug:owner.observe() for owner in owners if owner.observe is not None}
            personal = _personal_revision(essential)
            chosen = {}
            for owner in owners:
                if archives and owner.slug in archives:
                    identity = archives[owner.slug]
                    verify_archive(essential, identity, owner)
                elif (path := legacy_paths.get(owner.slug)) is not None and Path(path).is_file():
                    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)) as source:
                        identity = preserve_owner(essential, source, owner)
                elif (identity := latest_archive(essential,owner.slug)) is not None:
                    verify_archive(essential,identity,owner)
                else:
                    with closing(sqlite3.connect(':memory:')) as source:
                        owner.initialize(source)
                        source.commit()
                        identity = preserve_owner(essential, source, owner)
                chosen[owner.slug] = identity
            intent = dict(selection=inputs, archives=chosen, personal=personal)
            if (attempt/'intent.json').exists():
                if _receipt(attempt,'intent') != dict(format=1,phase='intent',**json.loads(_json(intent))):
                    raise ValueError('Index preparation source or selection changed')
            else:
                _record_phase(attempt,'intent',**intent)
            if (attempt/'complete.json').exists():
                receipt = _receipt(attempt,'complete')
                candidate = attempt/'index.sqlite'
                if candidate.is_symlink():
                    raise ValueError('Refused linked index candidate')
                with closing(sqlite3.connect(candidate.resolve(strict=True).as_uri()+'?mode=ro',uri=True)) as connection:
                    generation = validate_index(connection,owners)
                    if connection.execute('SELECT selection FROM kv_index_meta').fetchone()[0] != _json(inputs):
                        raise ValueError('Completed index source selection changed')
                    actual = _digest(_owned_manifest(connection, owned_tables))
                if generation != receipt['generation'] or actual != receipt['index_digest']:
                    raise ValueError('Completed index candidate changed')
                final_archives = receipt['archives']
                if set(final_archives) != {owner.slug for owner in owners}:
                    raise ValueError('Completed index preservation selection changed')
                for owner in owners:
                    verify_archive(essential,final_archives[owner.slug],owner)
                return PreparedIndex(candidate,attempt,generation,final_archives,tuple(o.slug for o in owners))
            partial = attempt/('index-'+uuid4().hex+'.partial.sqlite')
            generation = uuid4().hex
            with closing(sqlite3.connect(partial)) as connection:
                connection.execute('PRAGMA journal_mode=WAL')
                # Restore all owners before enabling foreign keys/owner queries.
                for owner in owners:
                    restore_owner(essential,connection,chosen[owner.slug],owner)
                    owner.initialize(connection)
                connection.executescript(INDEX_SCHEMA)
                connection.execute('INSERT INTO kv_index_meta VALUES(1,1,?,?)',(generation,_json(inputs)))
                for owner in owners:
                    connection.execute('INSERT INTO kv_index_owners VALUES(?,?,?)',
                                       (owner.slug,owner.version,_json(owner.tables)))
                connection.commit()
                connection.execute('PRAGMA foreign_keys=ON')
                connection.row_factory = sqlite3.Row
                build = IndexBuild(connection,essential,tuple(sources),Path(suite_home),cancelled,generation,
                                   inputs['observations'])
                for owner in owners:
                    build.check()
                    owner.rebuild(build)
                    connection.commit()
                build.check()
                validate_index(connection,owners)
                with closing(_tuple_cursor(connection)) as check:
                    if check.execute('PRAGMA quick_check').fetchall() != [('ok',)] or check.execute('PRAGMA foreign_key_check').fetchall():
                        raise ValueError('Index candidate failed integrity verification')
                if _personal_revision(essential) != personal:
                    raise ValueError('Essential records changed during index preparation')
                for owner in owners:
                    path = legacy_paths.get(owner.slug)
                    if path is not None and Path(path).is_file() and not (archives and owner.slug in archives):
                        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as current:
                            if _json(_owned_manifest(current,owner.tables)) != _json(_archive(essential,chosen[owner.slug],owner)[1]):
                                raise ValueError('Legacy index changed during preparation')
                for owner in owners:
                    if owner.observe is not None and owner.observe() != inputs['observations'][owner.slug]:
                        raise ValueError('Preserved source files changed during index preparation')
                for owner in owners:
                    if owner.preserve_result:
                        chosen[owner.slug] = preserve_owner(essential,connection,owner)
                digest = _digest(_owned_manifest(connection,owned_tables))
                connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            candidate = attempt/'index.sqlite'
            if candidate.is_symlink():
                raise ValueError('Refused linked index candidate')
            if candidate.exists():
                # A crash after candidate publication, before completion receipt,
                # retains that candidate as evidence instead of replacing it.
                candidate.rename(attempt/('unacknowledged-'+uuid4().hex+'.sqlite'))
            os.link(partial,candidate)
            _record_phase(attempt,'complete',generation=generation,index_digest=digest,archives=chosen)
            return PreparedIndex(candidate,attempt,generation,chosen,tuple(o.slug for o in owners))


def ensure_owner(connection, owner):
    """An explicit enable initializes only its owner's tables, atomically."""
    validate_index(connection)
    with closing(_tuple_cursor(connection)) as cursor:
        row = cursor.execute('SELECT version,tables_json FROM kv_index_owners WHERE owner=?',
                             (owner.slug,)).fetchone()
        if row is not None:
            if row != (owner.version, _json(owner.tables)):
                raise ValueError('Unsupported combined index owner schema')
            return
        registered = {row[0] for row in cursor.execute('SELECT owner FROM kv_index_owners')}
        if not set(owner.requires) <= registered:
            raise ValueError('Required index owner is unavailable')
        existing = {row[0] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if existing & set(owner.tables):
            raise ValueError('Conflicting unregistered index tables')
    if connection.in_transaction:
        raise ValueError('Owner initialization requires a clean index connection')
    with closing(sqlite3.connect(':memory:')) as template:
        owner.initialize(template)
        definitions = [row for row in template.execute(
            "SELECT type,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL "
            "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END,name")]
        if any(kind not in ('table','index') or table not in owner.tables
               for kind,table,sql in definitions):
            raise ValueError('Owner initializer escaped its derived tables')
        with _write(connection):
            for kind, table, sql in definitions:
                connection.execute(sql)
            connection.execute('INSERT INTO kv_index_owners VALUES(?,?,?)',
                               (owner.slug,owner.version,_json(owner.tables)))


def restrict_snapshot(path, owner):
    """Filter a generated backup snapshot, never the live combined index."""
    with closing(sqlite3.connect(path)) as connection:
        validate_index(connection)
        owner.initialize(connection)
        connection.commit()
        connection.execute('PRAGMA foreign_keys=OFF')
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            if table not in owner.tables:
                connection.execute('DROP TABLE '+quoted(table))
        connection.commit()
        connection.execute('VACUUM')


def merge_owner_snapshot(live, restored, destination, owner):
    """Build a full rollback-compatible incoming DB, changing one owner only."""
    from services.data_migrations import snapshot_user_database
    snapshot_user_database(Path(live),Path(destination))
    evidence = Path(destination).with_name(Path(destination).name+'.owner-evidence.sqlite')
    with closing(sqlite3.connect(restored)) as source, closing(sqlite3.connect(evidence)) as preserved:
        identity = preserve_owner(preserved,source,owner)
        preserved.commit()
        with closing(sqlite3.connect(destination)) as target:
            if is_combined(target):
                ensure_owner(target,owner)
            restore_owner(preserved,target,identity,owner)
            target.commit()
            owner.initialize(target)
            target.commit()
            if is_combined(target):
                validate_index(target)
            if target.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise ValueError('Restored owner index failed verification')


def _select_archive(essential,owner,identity):
    essential.execute("CREATE TABLE IF NOT EXISTS kv_index_archive_heads(owner TEXT PRIMARY KEY,archive_id TEXT NOT NULL)")
    essential.execute("INSERT INTO kv_index_archive_heads VALUES(?,?) ON CONFLICT(owner) DO UPDATE SET archive_id=excluded.archive_id",
                      (owner,identity))


def latest_archive(essential,owner):
    with closing(_tuple_cursor(essential)) as cursor:
        if not cursor.execute("SELECT 1 FROM sqlite_master WHERE name='kv_index_archives'").fetchone():return None
        if cursor.execute("SELECT 1 FROM sqlite_master WHERE name='kv_index_archive_heads'").fetchone():
            head=cursor.execute('SELECT archive_id FROM kv_index_archive_heads WHERE owner=?',(owner,)).fetchone()
            if head:return head[0]
        # Compatibility with preserved snapshots written before the head existed.
        row=cursor.execute('SELECT id FROM kv_index_archives WHERE owner=? ORDER BY rowid DESC LIMIT 1',(owner,)).fetchone()
        return row[0] if row else None

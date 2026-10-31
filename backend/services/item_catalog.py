"""Additive item identities, independent of paths, indexes and optional modules.

These primitives operate only on an explicitly supplied connection. Importing
this module neither opens a profile nor selects a new storage authority.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import re
import sqlite3
from uuid import uuid4


SCHEMA_VERSION = 2
MAX_CAPTURE_BYTES = 256 * 1024
SCHEMA = """
CREATE TABLE IF NOT EXISTS kv_catalog_meta (
    id INTEGER PRIMARY KEY CHECK(id = 1), schema_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kv_libraries (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('managed', 'linked')),
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS kv_library_sources (
    namespace TEXT NOT NULL, legacy_key TEXT NOT NULL,
    library_id TEXT NOT NULL REFERENCES kv_libraries(id),
    PRIMARY KEY(namespace, legacy_key)
);
CREATE TABLE IF NOT EXISTS kv_items (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, item_type TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS kv_legacy_keys (
    namespace TEXT NOT NULL, legacy_key TEXT NOT NULL,
    item_id TEXT NOT NULL REFERENCES kv_items(id),
    PRIMARY KEY(namespace, legacy_key)
);
CREATE TABLE IF NOT EXISTS kv_assets (
    id TEXT PRIMARY KEY, media_type TEXT NOT NULL, byte_size INTEGER,
    hash_algorithm TEXT, content_hash TEXT,
    CHECK(byte_size IS NULL OR byte_size >= 0),
    CHECK((hash_algorithm IS NULL) = (content_hash IS NULL))
);
CREATE TABLE IF NOT EXISTS kv_item_assets (
    item_id TEXT NOT NULL REFERENCES kv_items(id),
    asset_id TEXT NOT NULL REFERENCES kv_assets(id), role TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    PRIMARY KEY(item_id, role, position)
);
CREATE INDEX IF NOT EXISTS kv_item_assets_asset ON kv_item_assets(asset_id);
CREATE TABLE IF NOT EXISTS kv_item_relations (
    source_item_id TEXT NOT NULL REFERENCES kv_items(id),
    target_item_id TEXT NOT NULL REFERENCES kv_items(id), kind TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    CHECK(source_item_id <> target_item_id),
    PRIMARY KEY(source_item_id, kind, position)
);
CREATE INDEX IF NOT EXISTS kv_item_relations_target
    ON kv_item_relations(target_item_id);
CREATE TABLE IF NOT EXISTS kv_item_captures (
    item_id TEXT NOT NULL REFERENCES kv_items(id), namespace TEXT NOT NULL,
    capture_key TEXT NOT NULL, format_version INTEGER NOT NULL CHECK(format_version > 0),
    payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(item_id, namespace, capture_key)
);
"""


class CatalogConflict(ValueError):
    """An existing identity or ordered relationship has different meaning."""


def _key(value: str, *, label: str = 'key', limit: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or '\0' in value:
        raise ValueError(f'Invalid {label}')
    return value


def _name(value: str, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9_.:-]{0,63}', value):
        raise ValueError(f'Invalid {label}')
    return value


def _one(connection, query, parameters=()):
    # Preserve the caller's row factory; suite connections normally return dicts.
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    try:
        row = cursor.execute(query, parameters).fetchone()
        return dict(row) if row is not None else None
    finally:
        cursor.close()


@contextmanager
def _write(connection):
    """Serialize standalone writes while retaining an outer caller transaction."""
    own_transaction = not connection.in_transaction
    savepoint = 'kv_' + uuid4().hex
    connection.execute('BEGIN IMMEDIATE' if own_transaction else f'SAVEPOINT {savepoint}')
    try:
        yield
        if own_transaction:
            connection.commit()
        else:
            connection.execute(f'RELEASE {savepoint}')
    except BaseException:
        if own_transaction:
            connection.rollback()
        else:
            connection.execute(f'ROLLBACK TO {savepoint}')
            connection.execute(f'RELEASE {savepoint}')
        raise


def ensure_catalog_schema(connection: sqlite3.Connection) -> None:
    """Add owned tables without changing an existing user's tables or version.

    Initialization requires a clean connection so executescript cannot commit
    unrelated pending writes. An unknown catalog version is never downgraded.
    """
    if connection.in_transaction:
        raise ValueError('Initialize the catalog outside a pending transaction')
    exists = _one(connection, "SELECT name FROM sqlite_master WHERE type='table' AND name='kv_catalog_meta'")
    if exists:
        current = _one(connection, 'SELECT schema_version FROM kv_catalog_meta WHERE id=1')
        if current is not None and (type(current['schema_version']) is not int
                                    or not 1 <= current['schema_version'] <= SCHEMA_VERSION):
            raise ValueError('Unsupported catalog schema version')
    connection.execute('PRAGMA foreign_keys=ON')
    connection.executescript(SCHEMA)
    with _write(connection):
        connection.execute('INSERT OR IGNORE INTO kv_catalog_meta VALUES(1, ?)', (SCHEMA_VERSION,))
        connection.execute('UPDATE kv_catalog_meta SET schema_version=? WHERE id=1 AND schema_version<?',
                           (SCHEMA_VERSION, SCHEMA_VERSION))


def ensure_library(connection, *, namespace: str, legacy_key: str, kind: str,
                   display_name: str, library_id: str | None = None) -> str:
    """Resolve one registered-source alias; relocation remains source-owned."""
    _name(namespace, 'namespace')
    _key(legacy_key)
    _key(display_name, label='display name', limit=1024)
    if kind not in ('managed', 'linked'):
        raise ValueError('Invalid library kind')
    if library_id is not None:
        _key(library_id, label='library ID', limit=256)
    with _write(connection):
        row = _one(connection,
                   'SELECT l.id,l.kind FROM kv_libraries l JOIN kv_library_sources a '
                   'ON a.library_id=l.id WHERE a.namespace=? AND a.legacy_key=?', (namespace, legacy_key))
        if row:
            if row['kind'] != kind or (library_id is not None and row['id'] != library_id):
                raise CatalogConflict('Library alias already has a different identity or kind')
            return row['id']
        identity = library_id or str(uuid4())
        existing = _one(connection, 'SELECT kind FROM kv_libraries WHERE id=?', (identity,))
        if existing and existing['kind'] != kind:
            raise CatalogConflict('Library identity already has a different kind')
        if existing is None:
            connection.execute('INSERT INTO kv_libraries(id,kind,display_name) VALUES(?,?,?)',
                               (identity, kind, display_name))
        connection.execute('INSERT INTO kv_library_sources VALUES(?,?,?)', (namespace, legacy_key, identity))
        return identity


def library_for_legacy(connection, *, namespace: str, legacy_key: str):
    """Read an existing source alias; captured paths never create a library."""
    _name(namespace, 'namespace')
    _key(legacy_key)
    return _one(connection, 'SELECT l.id,l.kind FROM kv_libraries l JOIN kv_library_sources a '
                'ON a.library_id=l.id WHERE a.namespace=? AND a.legacy_key=?', (namespace, legacy_key))


def ensure_item(connection, *, namespace: str, legacy_key: str, owner: str,
                item_type: str, item_id: str | None = None) -> str:
    """Retry an exact legacy identity without conflating titles, paths or hashes."""
    for value, label in ((namespace, 'namespace'), (owner, 'owner'), (item_type, 'item type')):
        _name(value, label)
    _key(legacy_key)
    if item_id is not None:
        _key(item_id, label='item ID', limit=256)
    with _write(connection):
        row = _one(connection,
                   'SELECT i.id,i.owner,i.item_type FROM kv_items i JOIN kv_legacy_keys a '
                   'ON a.item_id=i.id WHERE a.namespace=? AND a.legacy_key=?', (namespace, legacy_key))
        if row:
            if (row['owner'], row['item_type']) != (owner, item_type) or (item_id is not None and item_id != row['id']):
                raise CatalogConflict('Item alias already has a different owner, type or identity')
            return row['id']
        identity = item_id or str(uuid4())
        existing = _one(connection, 'SELECT owner,item_type FROM kv_items WHERE id=?', (identity,))
        if existing and (existing['owner'], existing['item_type']) != (owner, item_type):
            raise CatalogConflict('Item identity already has a different owner or type')
        if existing is None:
            connection.execute('INSERT INTO kv_items(id,owner,item_type) VALUES(?,?,?)', (identity, owner, item_type))
        connection.execute('INSERT INTO kv_legacy_keys VALUES(?,?,?)', (namespace, legacy_key, identity))
        return identity


def item_for_legacy(connection, *, namespace: str, legacy_key: str):
    """Read an exact legacy alias without creating an item or importing owners."""
    _name(namespace, 'namespace')
    _key(legacy_key)
    return _one(connection, 'SELECT i.id,i.owner,i.item_type FROM kv_items i JOIN kv_legacy_keys a '
                'ON a.item_id=i.id WHERE a.namespace=? AND a.legacy_key=?', (namespace, legacy_key))


def _capture_payload(payload):
    if type(payload) is not dict:
        raise ValueError('Capture payload must be a JSON object')
    remaining = 10000
    def visit(value, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 32:
            raise ValueError('Capture payload is too complex')
        kind = type(value)
        if kind is dict:
            for key, child in value.items():
                if type(key) is not str:
                    raise ValueError('Capture JSON keys must be strings')
                visit(child, depth + 1)
        elif kind is list:
            for child in value:
                visit(child, depth + 1)
        elif kind is float:
            if not math.isfinite(value):
                raise ValueError('Capture numbers must be finite')
        elif kind not in (str, int, bool, type(None)):
            raise ValueError('Unsupported capture JSON value')
    visit(payload)
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    encoded = text.encode('utf-8')
    if len(encoded) > MAX_CAPTURE_BYTES:
        raise ValueError('Capture payload is too large')
    return text, hashlib.sha256(encoded).hexdigest()


def record_capture(connection, *, item_id: str, namespace: str, capture_key: str,
                   format_version: int, payload: dict) -> str:
    """Retain one immutable capture; changed facts require a new capture key."""
    _key(item_id, label='item ID', limit=256)
    _name(namespace, 'namespace')
    _key(capture_key, label='capture key')
    if type(format_version) is not int or not 1 <= format_version <= 2**31 - 1:
        raise ValueError('Invalid capture format version')
    text, digest = _capture_payload(payload)
    facts = dict(format_version=format_version, payload_json=text, payload_sha256=digest)
    with _write(connection):
        existing = _one(connection, 'SELECT format_version,payload_json,payload_sha256 FROM kv_item_captures '
                        'WHERE item_id=? AND namespace=? AND capture_key=?', (item_id, namespace, capture_key))
        if existing is not None and existing != facts:
            raise CatalogConflict('Capture key already preserves different facts or format')
        if existing is None:
            connection.execute('INSERT INTO kv_item_captures(item_id,namespace,capture_key,format_version,payload_json,payload_sha256) '
                               'VALUES(?,?,?,?,?,?)', (item_id, namespace, capture_key, format_version, text, digest))
    return digest


def read_capture(connection, *, item_id: str, namespace: str, capture_key: str):
    """Read preserved facts and refuse corrupt/noncanonical payloads."""
    _key(item_id, label='item ID', limit=256)
    _name(namespace, 'namespace')
    _key(capture_key, label='capture key')
    row = _one(connection, 'SELECT format_version,payload_json,payload_sha256,recorded_at FROM kv_item_captures '
               'WHERE item_id=? AND namespace=? AND capture_key=?', (item_id, namespace, capture_key))
    if row is None:
        return None
    text = row['payload_json']
    if (not isinstance(text, str) or len(text.encode('utf-8')) > MAX_CAPTURE_BYTES
            or type(row['format_version']) is not int or not 1 <= row['format_version'] <= 2**31 - 1):
        raise ValueError('Invalid preserved capture record')
    payload = json.loads(text)
    canonical, digest = _capture_payload(payload)
    if canonical != text or digest != row['payload_sha256']:
        raise ValueError('Preserved capture payload changed')
    return dict(format_version=row['format_version'], payload=payload,
                payload_sha256=digest, recorded_at=row['recorded_at'])


def record_asset(connection, *, asset_id: str, media_type: str, byte_size: int | None = None,
                 hash_algorithm: str | None = None, content_hash: str | None = None) -> str:
    """Register supplied facts; never read/hash/deduplicate a file implicitly."""
    _key(asset_id, label='asset ID', limit=256)
    _key(media_type, label='media type', limit=256)
    if byte_size is not None and (type(byte_size) is not int or not 0 <= byte_size <= 2**63 - 1):
        raise ValueError('Invalid asset size')
    if (hash_algorithm is None) != (content_hash is None):
        raise ValueError('Both hash algorithm and value are required')
    if hash_algorithm is not None:
        lengths = {'md5': 32, 'sha256': 64}
        if hash_algorithm not in lengths or not isinstance(content_hash, str) or not re.fullmatch(
                '[0-9a-fA-F]{' + str(lengths[hash_algorithm]) + '}', content_hash):
            raise ValueError('Invalid asset hash')
        content_hash = content_hash.lower()
    facts = dict(media_type=media_type, byte_size=byte_size, hash_algorithm=hash_algorithm, content_hash=content_hash)
    with _write(connection):
        existing = _one(connection, 'SELECT media_type,byte_size,hash_algorithm,content_hash FROM kv_assets WHERE id=?', (asset_id,))
        if existing is not None and existing != facts:
            raise CatalogConflict('Asset identity already describes different bytes or media')
        if existing is None:
            connection.execute('INSERT INTO kv_assets VALUES(?,?,?,?,?)',
                               (asset_id, media_type, byte_size, hash_algorithm, content_hash))
    return asset_id


def record_asset_capture(connection, *, item_id: str, namespace: str, assets: dict,
                         payload: dict, format_version: int = 1) -> str:
    """Atomically retain supplied asset roles and a content-keyed observation.

    Owners validate files and construct their payloads before calling. This
    service does not resolve paths, import owners or change existing facts.
    """
    if type(assets) is not dict or len(assets) > 256:
        raise ValueError('Invalid asset capture batch')
    _, digest = _capture_payload(payload)
    capture_key = str(format_version) + ':' + digest
    with _write(connection):
        for role, facts in assets.items():
            if type(facts) is not dict or set(facts) != {
                    'asset_id', 'media_type', 'byte_size', 'hash_algorithm', 'content_hash'}:
                raise ValueError('Invalid asset capture facts')
            record_asset(connection, **facts)
            link_asset(connection, item_id=item_id, asset_id=facts['asset_id'], role=role)
        record_capture(connection, item_id=item_id, namespace=namespace, capture_key=capture_key,
                       format_version=format_version, payload=payload)
    return capture_key


def _position(value):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError('Invalid position')


def link_asset(connection, *, item_id: str, asset_id: str, role: str, position: int = 0) -> None:
    _name(role, 'asset role')
    _position(position)
    with _write(connection):
        existing = _one(connection, 'SELECT asset_id FROM kv_item_assets WHERE item_id=? AND role=? AND position=?',
                        (item_id, role, position))
        if existing and existing['asset_id'] != asset_id:
            raise CatalogConflict('An asset already occupies this item role and position')
        if existing is None:
            connection.execute('INSERT INTO kv_item_assets VALUES(?,?,?,?)', (item_id, asset_id, role, position))


def relate_items(connection, *, source_item_id: str, target_item_id: str, kind: str, position: int = 0) -> None:
    _name(kind, 'relation kind')
    _position(position)
    with _write(connection):
        existing = _one(connection,
                        'SELECT target_item_id FROM kv_item_relations WHERE source_item_id=? AND kind=? AND position=?',
                        (source_item_id, kind, position))
        if existing and existing['target_item_id'] != target_item_id:
            raise CatalogConflict('A relation already occupies this kind and position')
        if existing is None:
            connection.execute('INSERT INTO kv_item_relations VALUES(?,?,?,?)',
                               (source_item_id, target_item_id, kind, position))

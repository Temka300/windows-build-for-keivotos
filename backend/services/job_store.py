"""Explicit SQLite request receipts for the staged operations store.

Importing this service does not open a profile, select a database or start work.
Owners supply normalized, non-secret input; their adapters own its interpretation.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import re
import sqlite3
from uuid import uuid4

SCHEMA_VERSION = 1
MAX_PAYLOAD_BYTES = 256 * 1024
_TABLES = ('kv_job_store_meta', 'kv_job_requests')
_SCHEMA = (
    '''CREATE TABLE kv_job_store_meta (
        id INTEGER PRIMARY KEY CHECK(id=1), schema_version INTEGER NOT NULL
    )''',
    '''CREATE TABLE kv_job_requests (
        id TEXT PRIMARY KEY NOT NULL, owner TEXT NOT NULL, kind TEXT NOT NULL,
        request_key TEXT NOT NULL, format_version INTEGER NOT NULL CHECK(format_version>0),
        payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        UNIQUE(owner, request_key)
    )''',
)


class JobConflict(ValueError):
    """A request identity already describes different input."""


def _key(value, label, limit=256):
    if type(value) is not str or not value or len(value) > limit or '\0' in value:
        raise ValueError('Invalid ' + label)
    try:
        value.encode('utf-8')
    except UnicodeError as error:
        raise ValueError('Invalid ' + label) from error
    return value


def _name(value, label):
    if type(value) is not str or not re.fullmatch(r'[a-z][a-z0-9_.:-]{0,63}', value):
        raise ValueError('Invalid ' + label)
    return value


def _version(value):
    if type(value) is not int or not 1 <= value <= 2**31 - 1:
        raise ValueError('Invalid request format version')
    return value


def _payload(value):
    if type(value) is not dict:
        raise ValueError('Request input must be a JSON object')
    remaining = 10000

    def visit(child, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 32:
            raise ValueError('Request input is too complex')
        kind = type(child)
        if kind is dict:
            for key, item in child.items():
                if type(key) is not str:
                    raise ValueError('Request JSON keys must be strings')
                visit(item, depth + 1)
        elif kind is list:
            for item in child:
                visit(item, depth + 1)
        elif kind is float:
            if not math.isfinite(child):
                raise ValueError('Request numbers must be finite')
        elif kind not in (str, int, bool, type(None)):
            raise ValueError('Unsupported request JSON value')

    visit(value)
    try:
        text = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        encoded = text.encode('utf-8')
    except (UnicodeError, ValueError) as error:
        raise ValueError('Invalid request JSON') from error
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise ValueError('Request input is too large')
    return text, hashlib.sha256(encoded).hexdigest()


def _one(connection, sql, parameters=()):
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    try:
        row = cursor.execute(sql, parameters).fetchone()
        return dict(row) if row is not None else None
    finally:
        cursor.close()


@contextmanager
def _write(connection):
    own = not connection.in_transaction
    savepoint = 'kv_job_' + uuid4().hex
    connection.execute('BEGIN IMMEDIATE' if own else 'SAVEPOINT ' + savepoint)
    try:
        yield
        connection.execute('COMMIT' if own else 'RELEASE ' + savepoint)
    except BaseException:
        if own:
            connection.rollback()
        else:
            connection.execute('ROLLBACK TO ' + savepoint)
            connection.execute('RELEASE ' + savepoint)
        raise


def ensure_job_schema(connection: sqlite3.Connection) -> None:
    """Add only owned tables on a clean connection; never adopt unknown tables.

    Existing user tables, SQLite header versions and connection settings remain
    the caller's. Refuse unsupported or incomplete stores without repairing them.
    """
    if connection.in_transaction:
        raise ValueError('Initialize the job store outside a pending transaction')
    with _write(connection):
        present = {name for name in _TABLES if _one(connection,
            'SELECT name FROM sqlite_master WHERE name=?', (name,))}
        if present:
            if present != set(_TABLES):
                raise ValueError('Incomplete job store schema')
            meta = _one(connection, 'SELECT id,schema_version FROM kv_job_store_meta')
            if meta != dict(id=1, schema_version=SCHEMA_VERSION):
                raise ValueError('Unsupported job store schema')
            # Check required columns even on an empty store before reporting ready.
            connection.execute('SELECT id,owner,kind,request_key,format_version,payload_json,'
                               'payload_sha256,created_at FROM kv_job_requests LIMIT 0')
        else:
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.execute('INSERT INTO kv_job_store_meta VALUES(1,?)', (SCHEMA_VERSION,))


def _decode(row):
    if row is None:
        return None
    _key(row['id'], 'request ID')
    _name(row['owner'], 'owner')
    _name(row['kind'], 'operation kind')
    _key(row['request_key'], 'request key')
    _version(row['format_version'])
    text = row['payload_json']
    if type(text) is not str:
        raise ValueError('Invalid stored request input')
    try:
        if len(text.encode('utf-8')) > MAX_PAYLOAD_BYTES:
            raise ValueError('Stored request input is too large')
        payload = json.loads(text)
        canonical, digest = _payload(payload)
    except (UnicodeError, RecursionError, ValueError) as error:
        raise ValueError('Invalid stored request input') from error
    if canonical != text or digest != row['payload_sha256']:
        raise ValueError('Stored request input does not match its receipt')
    return {key: value for key, value in row.items() if key != 'payload_json'} | dict(payload=payload)


def read_request(connection, *, owner: str, request_id: str):
    """Read one owner's receipt; missing or other-owner IDs return None."""
    _name(owner, 'owner')
    _key(request_id, 'request ID')
    return _decode(_one(connection, 'SELECT * FROM kv_job_requests WHERE owner=? AND id=?',
                        (owner, request_id)))


def record_request(connection, *, owner: str, kind: str, request_key: str,
                   payload: dict, format_version: int = 1, request_id: str | None = None):
    """Persist normalized intent, or return its exact durable acknowledgement.

    Keys are scoped to an owner, across operation kinds. This records intent
    only: no worker admission, network work, attempt or completion is inferred.
    A caller's outer transaction stays open and may roll this receipt back.
    """
    _name(owner, 'owner')
    _name(kind, 'operation kind')
    _key(request_key, 'request key')
    _version(format_version)
    if request_id is not None:
        _key(request_id, 'request ID')
    text, digest = _payload(payload)
    with _write(connection):
        existing = _one(connection, 'SELECT * FROM kv_job_requests WHERE owner=? AND request_key=?',
                        (owner, request_key))
        if existing is not None:
            receipt = _decode(existing)
            if (existing['kind'] != kind or existing['format_version'] != format_version
                    or existing['payload_json'] != text or existing['payload_sha256'] != digest
                    or (request_id is not None and existing['id'] != request_id)):
                raise JobConflict('Request key already describes different input')
            return receipt
        identity = request_id if request_id is not None else uuid4().hex
        if _one(connection, 'SELECT id FROM kv_job_requests WHERE id=?', (identity,)):
            raise JobConflict('Request ID already belongs to another request')
        connection.execute('INSERT INTO kv_job_requests(id,owner,kind,request_key,format_version,'
                           'payload_json,payload_sha256) VALUES(?,?,?,?,?,?,?)',
                           (identity, owner, kind, request_key, format_version, text, digest))
        return read_request(connection, owner=owner, request_id=identity)

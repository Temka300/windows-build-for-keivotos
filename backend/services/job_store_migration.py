"""Copy-and-verify owner state; never reimport legacy queues after history loss."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from uuid import uuid4

from services import operations
from services.job_store import _one, _write

RECEIPTS = """CREATE TABLE IF NOT EXISTS kv_operational_migrations (
    owner TEXT NOT NULL, generation TEXT NOT NULL, version INTEGER NOT NULL,
    history_lost INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(owner,generation))"""
RUNTIME = """CREATE TABLE kv_operational_store (
    id INTEGER PRIMARY KEY CHECK(id=1), generation TEXT NOT NULL, profile_token TEXT)"""
OWNERS = """CREATE TABLE kv_operational_owners (
    owner TEXT PRIMARY KEY, table_name TEXT NOT NULL, version INTEGER NOT NULL, receipt_token TEXT NOT NULL)"""

def initialize(connection):
    operations.ensure_schema(connection)
    with _write(connection):
        present = operations.rows(connection,"SELECT name FROM sqlite_master WHERE name IN ('kv_operational_store','kv_operational_owners')")
        if present:
            if len(present)!=2:
                raise ValueError('Incomplete operational store')
            row=_one(connection,'SELECT * FROM kv_operational_store')
            if not row or row['id']!=1 or not isinstance(row['generation'],str) or len(row['generation'])!=32:
                raise ValueError('Unsupported operational store identity')
            connection.execute('SELECT owner,table_name,version,receipt_token FROM kv_operational_owners LIMIT 0')
            connection.execute('SELECT profile_token FROM kv_operational_store LIMIT 0')
        else:
            connection.execute(RUNTIME); connection.execute(OWNERS)
            connection.execute('INSERT INTO kv_operational_store(id,generation) VALUES(1,?)',(uuid4().hex,))
    return _one(connection,'SELECT generation FROM kv_operational_store')['generation']

def identifier(value):
    if not isinstance(value,str) or not value or '\0' in value:
        raise ValueError('Invalid operational identifier')
    return '"'+value.replace('"','""')+'"'


def prepare(connection, source, receipts, *, owner, table, preserve=None, empty_schema=None):
    """Source and receipts can be the same connection; each store commits alone.

    Job copy commits first, essential preservation/receipt second. An interrupted
    second commit reuses the verified copy. A previous essential receipt with a
    different generation prohibits any legacy queue import into the new store.
    """
    if connection.in_transaction or source.in_transaction or receipts.in_transaction:
        raise ValueError('Migration requires clean source connections')
    generation=initialize(connection)
    receipts.execute(RECEIPTS)
    receipts.execute('CREATE TABLE IF NOT EXISTS kv_operational_intents(owner TEXT NOT NULL,generation TEXT NOT NULL,token TEXT NOT NULL UNIQUE,PRIMARY KEY(owner,generation))')
    receipts.commit()
    suite=_one(receipts,"SELECT token FROM kv_operational_intents WHERE owner='suite' AND generation=?",(generation,))
    if suite is None:
        receipts.execute("INSERT OR IGNORE INTO kv_operational_intents VALUES('suite',?,?)",(generation,uuid4().hex))
        receipts.commit()
        suite=_one(receipts,"SELECT token FROM kv_operational_intents WHERE owner='suite' AND generation=?",(generation,))
    with _write(connection):
        profile=_one(connection,'SELECT profile_token FROM kv_operational_store WHERE id=1')
        if profile['profile_token'] is None:
            connection.execute('UPDATE kv_operational_store SET profile_token=? WHERE id=1',(suite['token'],))
        elif profile['profile_token']!=suite['token']:
            raise ValueError('Operational store belongs to a different migration source')
    known=_one(connection,'SELECT * FROM kv_operational_owners WHERE owner=?',(owner,))
    intent=_one(receipts,'SELECT token FROM kv_operational_intents WHERE owner=? AND generation=?',(owner,generation))
    if known and (known['table_name']!=table or known['version']!=1):
        raise ValueError('Unsupported owner operational schema')
    if known and (not intent or known['receipt_token']!=intent['token']):
        raise ValueError('Operational store belongs to a different migration source')
    if intent is None:
        token=uuid4().hex
        receipts.execute('INSERT OR IGNORE INTO kv_operational_intents VALUES(?,?,?)',(owner,generation,token))
        receipts.commit()
        intent=_one(receipts,'SELECT token FROM kv_operational_intents WHERE owner=? AND generation=?',(owner,generation))
    receipt=_one(receipts,'SELECT * FROM kv_operational_migrations WHERE owner=? AND generation=?',(owner,generation))
    if known and receipt:
        return
    if receipts.in_transaction or source.in_transaction:
        raise ValueError('Migration requires clean source connections')
    # Serialize migration with source-owner writes and all other processes.
    receipts.execute('BEGIN IMMEDIATE')
    try:
        prior=_one(receipts,'SELECT 1 AS present FROM kv_operational_migrations WHERE owner=? LIMIT 1',(owner,))
        lost=bool(prior and not receipt)
        with _write(connection):
            known=_one(connection,'SELECT * FROM kv_operational_owners WHERE owner=?',(owner,))
            if not known:
                definition=_one(source,"SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(table,))
                if not definition and empty_schema is None:
                    raise ValueError('Missing legacy operational schema: '+table)
                name=identifier(table)
                if source.execute('PRAGMA foreign_key_list('+name+')').fetchone():
                    raise ValueError('Unsupported cross-store operational foreign keys')
                if definition and _one(source,"SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=? LIMIT 1",(table,)):
                    raise ValueError('Unsupported operational trigger; original schema preserved')
                connection.execute(definition['sql'] if definition else empty_schema)
                cursor=(source if definition else connection).cursor(); cursor.row_factory=None
                copied=connection.cursor(); copied.row_factory=None
                try:
                    columns=[r[1] for r in cursor.execute('PRAGMA table_info('+name+')')]
                    if not columns:
                        raise ValueError('Empty operational schema')
                    if not lost and definition:
                        # Reuse the existing typed SQLite verification engine.
                        # Copy in bounded batches, never accumulating job history.
                        from services.data_migrations import _manifest
                        expected=_manifest(cursor,table_names=(table,))[1:]
                        fields=','.join(identifier(c) for c in columns)
                        cursor.execute('SELECT '+fields+' FROM '+name)
                        while batch:=cursor.fetchmany(64):
                            connection.executemany('INSERT INTO '+name+'('+fields+') VALUES('+','.join('?' for _ in columns)+')',batch)
                        if _manifest(copied,table_names=(table,))[1:]!=expected:
                            raise ValueError('Operational copy verification failed')
                    if definition:
                        for index in operations.rows(source,"SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",(table,)):
                            connection.execute(index['sql'])
                finally:
                    cursor.close()
                    copied.close()
                connection.execute('INSERT INTO kv_operational_owners VALUES(?,?,1,?)',(owner,table,intent['token']))
        if preserve is not None and not prior:
            # The owner callback performs no media access; original tables remain.
            preserve(receipts)
        receipts.execute('INSERT OR IGNORE INTO kv_operational_migrations(owner,generation,version,history_lost) VALUES(?,?,1,?)',
            (owner,generation,int(lost)))
        receipts.commit()
    except BaseException:
        receipts.rollback()
        raise

def ready(connection,essential,owner,table):
    if not _one(connection,"SELECT name FROM sqlite_master WHERE name='kv_operational_store'"):
        return False
    generation=_one(connection,'SELECT generation FROM kv_operational_store WHERE id=1')
    if not generation:return False
    if _one(connection,'SELECT * FROM kv_job_store_meta')!={'id':1,'schema_version':1} or _one(connection,'SELECT * FROM kv_operation_schema')!={'id':1,'version':1}:
        raise ValueError('Unsupported operational schema')
    known=_one(connection,'SELECT * FROM kv_operational_owners WHERE owner=?',(owner,))
    if known is None:return False
    if known['table_name']!=table or known['version']!=1:
        raise ValueError('Unsupported owner operational schema')
    if not _one(essential,"SELECT name FROM sqlite_master WHERE name='kv_operational_intents'"):
        raise ValueError('Operational migration intent is unavailable')
    suite=_one(essential,"SELECT token FROM kv_operational_intents WHERE owner='suite' AND generation=?",(generation['generation'],))
    profile=_one(connection,'SELECT profile_token FROM kv_operational_store WHERE id=1')
    if not suite or profile['profile_token']!=suite['token']:
        raise ValueError('Operational store belongs to a different migration source')
    intent=_one(essential,'SELECT token FROM kv_operational_intents WHERE owner=? AND generation=?',(owner,generation['generation']))
    if not intent or intent['token']!=known['receipt_token']:
        raise ValueError('Operational store belongs to a different migration source')
    if not _one(essential,"SELECT name FROM sqlite_master WHERE name='kv_operational_migrations'"):
        return False
    receipt=_one(essential,'SELECT version FROM kv_operational_migrations WHERE owner=? AND generation=?',(owner,generation['generation']))
    if receipt is not None and receipt!={'version':1}:
        raise ValueError('Unsupported operational migration receipt')
    return receipt is not None


@contextmanager
def owner_database(owner, table, legacy_database, *, preserve=None, source_database=None, empty_schema=None):
    from database import get_jobs_db
    with legacy_database() as essential, get_jobs_db() as jobs:
        # The normal read path never takes an essential write lock. A publisher
        # may already hold its independent catalog transaction on this thread.
        if not ready(jobs,essential,owner,table):
            if preserve is not None:
                from services.item_catalog import ensure_catalog_schema
                ensure_catalog_schema(essential)
            if source_database is None:
                prepare(jobs,essential,essential,owner=owner,table=table,preserve=preserve)
            else:
                with source_database() as source:
                    prepare(jobs,source,essential,owner=owner,table=table,preserve=preserve,empty_schema=empty_schema)
        yield jobs


def preserve_sources(connection):
    """Only registered source identities, matching the existing item 02 bridge."""
    from services import item_catalog
    if not _one(connection,"SELECT name FROM sqlite_master WHERE name='files_sources'"):
        return
    columns={row['name'] for row in operations.rows(connection,'PRAGMA table_info(files_sources)')}
    if not {'source_id','display_name'}<=columns:
        raise ValueError('Unsupported registered-source schema')
    for row in operations.rows(connection,"SELECT source_id,display_name FROM files_sources ORDER BY source_id"):
        item_catalog.ensure_library(connection,namespace='files.source',legacy_key=row['source_id'],
            library_id=row['source_id'],kind='linked',display_name=row['display_name'] or row['source_id'])

@contextmanager
def catalog_database(legacy):
    from services.item_catalog import ensure_catalog_schema
    with legacy() as db:
        ensure_catalog_schema(db)
        db.execute('BEGIN IMMEDIATE')
        preserve_sources(db)
        db.commit()
        yield db

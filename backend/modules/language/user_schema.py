"""Additive personal study choices and vocabulary in the suite user database."""
SCHEMA = """
CREATE TABLE IF NOT EXISTS language_study_preferences (
 id INTEGER PRIMARY KEY CHECK(id = 1),
 category TEXT NOT NULL CHECK(category IN ('sino','native','months','dates','hours','minutes')),
 answer_language TEXT NOT NULL CHECK(answer_language IN ('korean','english')),
 minimum INTEGER NOT NULL CHECK(minimum >= 1),
 maximum INTEGER NOT NULL CHECK(maximum >= minimum AND maximum <= 99)
);
CREATE TABLE IF NOT EXISTS language_vocabulary (
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
 korean TEXT NOT NULL, pronunciation TEXT NOT NULL DEFAULT '', english TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS language_study_preferences_v2 (
 id INTEGER PRIMARY KEY CHECK(id = 1),
 category TEXT NOT NULL CHECK(category IN ('sino','native','months','dates','hours','minutes','month_date','hour_minute','date_time')),
 answer_language TEXT NOT NULL CHECK(answer_language IN ('korean','english')),
 minimum INTEGER NOT NULL CHECK(minimum >= 1),
 maximum INTEGER NOT NULL CHECK(maximum >= minimum AND maximum <= 262800)
);
CREATE TABLE IF NOT EXISTS language_vocabulary_revisions (
 entry_id TEXT NOT NULL REFERENCES language_vocabulary(id),
 revision INTEGER NOT NULL CHECK(revision >= 1), request_id TEXT NOT NULL UNIQUE,
 kind TEXT NOT NULL CHECK(kind IN ('edit','archive')),
 korean TEXT NOT NULL, pronunciation TEXT NOT NULL, english TEXT NOT NULL,
 recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 PRIMARY KEY(entry_id,revision)
);
"""


def publish(connection):
    connection.executescript(SCHEMA)
    connection.commit()


def import_catalog_records(connection):
    """Explicit language.essential-records/version 1; no notes/runtime IO.

    Vocabulary IDs and original owner rows remain authoritative. Preference
    generations stay separate; Markdown and its history remain ordinary files.
    """
    import json
    import sqlite3
    from uuid import NAMESPACE_URL, uuid4, uuid5
    from services import item_catalog

    if not connection.in_transaction:
        raise ValueError('Import Language records inside a caller transaction')
    schemas = {
        'language_vocabulary': (('id',), ('id', 'request_id', 'korean', 'pronunciation', 'english', 'created_at')),
        'language_vocabulary_revisions': (('entry_id', 'revision'),
            ('entry_id', 'revision', 'request_id', 'kind', 'korean', 'pronunciation', 'english', 'recorded_at')),
        'language_study_preferences': (('id',), ('id',)),
        'language_study_preferences_v2': (('id',), ('id',)),
    }
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    savepoint = 'language_records_' + uuid4().hex
    connection.execute('SAVEPOINT ' + savepoint)
    try:
        present = {row['name'] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, (keys, required) in schemas.items():
            if table not in present:
                continue
            if not set(required) <= {row['name'] for row in cursor.execute(f'PRAGMA table_info({table})')}:
                raise ValueError('Unsupported Language essential schema')
            if cursor.execute(f'SELECT 1 FROM {table} GROUP BY ' + ','.join(keys) + ' HAVING count(*)>1 LIMIT 1').fetchone():
                raise ValueError('Duplicate Language record identity')
        if 'language_vocabulary_revisions' in present:
            if 'language_vocabulary' not in present:
                orphan = cursor.execute('SELECT 1 FROM language_vocabulary_revisions LIMIT 1').fetchone()
            else:
                orphan = cursor.execute('SELECT 1 FROM language_vocabulary_revisions r LEFT JOIN language_vocabulary v '
                                        'ON v.id=r.entry_id WHERE v.id IS NULL LIMIT 1').fetchone()
            if orphan:
                raise ValueError('Language revision has an unknown vocabulary parent')

        def key(value):
            if not isinstance(value, str) or not value or len(value) > 256 or '\0' in value:
                raise ValueError('Invalid Language record identity')
            return value

        def record(kind, values, existing_id=None):
            encoded = json.dumps(values, ensure_ascii=False, separators=(',', ':'))
            identity = existing_id or str(uuid5(NAMESPACE_URL, 'urn:keivotos:language:' + kind + ':' + encoded))
            item_catalog.ensure_item(connection, namespace='language.' + kind,
                legacy_key=existing_id or encoded, owner='language', item_type=kind, item_id=identity)
            item_catalog.record_capture(connection, item_id=identity, namespace='language.record',
                capture_key='identity', format_version=1, payload=dict(kind=kind, key=values))
            return identity

        def receipt(identity, item_type, row, operation, entry_id, revision):
            request = key(row['request_id'])
            # One token namespace preserves the owner's cross-operation conflict
            # rule. Different operations cannot silently reuse a request UUID.
            item_catalog.ensure_item(connection, namespace='language.request', legacy_key=request,
                owner='language', item_type=item_type, item_id=identity)
            payload = dict(row)
            payload.update(operation=operation, entry_id=entry_id, revision=revision)
            item_catalog.record_capture(connection, item_id=identity, namespace='language.receipt',
                capture_key=request, format_version=1, payload=payload)

        count = 0
        for table, (keys, selected) in schemas.items():
            if table not in present:
                continue
            for row in cursor.execute('SELECT ' + ','.join(selected) + f' FROM {table} ORDER BY ' + ','.join(keys)):
                if table == 'language_vocabulary':
                    entry = key(row['id'])
                    identity = record('vocabulary', [entry], entry)
                    receipt(identity, 'vocabulary', row, 'create', entry, 0)
                elif table == 'language_vocabulary_revisions':
                    entry = key(row['entry_id'])
                    revision = row['revision']
                    if type(revision) is not int or revision < 1 or row['kind'] not in ('edit', 'archive'):
                        raise ValueError('Invalid Language revision identity or kind')
                    identity = record('vocabulary-revision', [entry, revision])
                    receipt(identity, 'vocabulary-revision', row, row['kind'], entry, revision)
                    item_catalog.relate_items(connection, source_item_id=identity,
                        target_item_id=entry, kind='vocabulary', position=0)
                else:
                    if type(row['id']) is not int or row['id'] != 1:
                        raise ValueError('Invalid Language preference identity')
                    record('study-preferences', [table, row['id']])
                count += 1
        connection.execute('RELEASE ' + savepoint)
        return count
    except BaseException:
        connection.execute('ROLLBACK TO ' + savepoint)
        connection.execute('RELEASE ' + savepoint)
        raise
    finally:
        cursor.close()

"""Additive personal reading state and series categories; originals are untouched."""
from services.library_categories import CategoryStore

categories = CategoryStore('manga')
SCHEMA = """
CREATE TABLE IF NOT EXISTS manga_reading_sessions (
 source_id TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL,
 generation INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(source_id, path, kind)
);
CREATE TABLE IF NOT EXISTS manga_reading_progress (
 source_id TEXT NOT NULL, series_path TEXT NOT NULL, path TEXT NOT NULL,
 kind TEXT NOT NULL, revision TEXT NOT NULL, page_count INTEGER NOT NULL,
 page INTEGER NOT NULL, mode TEXT NOT NULL, anchor REAL NOT NULL DEFAULT 0.5,
 generation INTEGER NOT NULL, sequence INTEGER NOT NULL,
 updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 PRIMARY KEY(source_id, path, kind, revision)
);
CREATE INDEX IF NOT EXISTS manga_progress_series
 ON manga_reading_progress(source_id, series_path, updated_at DESC);
""" + categories.schema

from modules.manga.metadata import SCHEMA as METADATA_SCHEMA
SCHEMA += METADATA_SCHEMA


def publish(connection):
    connection.executescript(SCHEMA)
    categories.migrate(connection)
    connection.commit()


def import_catalog_records(connection):
    """Explicit manga.essential-records/version 1, without library/media IO.

    Original owner tables retain all mutable reading and category state. Stable
    aliases describe recorded identities only, never availability or permission.
    Category IDs already belong to the user and are reused without rekeying.
    """
    import hashlib
    import json
    import sqlite3
    from uuid import NAMESPACE_URL, uuid4, uuid5
    from services import item_catalog

    if not connection.in_transaction:
        raise ValueError('Import Manga records inside a caller transaction')
    schemas = {
        'manga_categories': (('id',), ('id',)),
        'manga_reading_sessions': (('source_id', 'path', 'kind'), ('source_id', 'path', 'kind')),
        'manga_reading_progress': (('source_id', 'path', 'kind', 'revision'),
                                   ('source_id', 'path', 'kind', 'revision', 'series_path')),
        'manga_category_memberships': (('category_id', 'source_id', 'path'), ('category_id', 'source_id', 'path')),
        'manga_category_versions': (('source_id', 'path'), ('source_id', 'path')),
        'manga_category_updates': (('request_id',),
            ('request_id', 'source_id', 'path', 'expected_version', 'version', 'category_ids')),
    }
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    savepoint = 'manga_records_' + uuid4().hex
    connection.execute('SAVEPOINT ' + savepoint)
    try:
        present = {row['name'] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, (keys, required) in schemas.items():
            if table not in present:
                continue
            if not set(required) <= {row['name'] for row in cursor.execute(f'PRAGMA table_info({table})')}:
                raise ValueError('Unsupported Manga essential schema')
            if cursor.execute(f'SELECT 1 FROM {table} GROUP BY ' + ','.join(keys) + ' HAVING count(*)>1 LIMIT 1').fetchone():
                raise ValueError('Duplicate Manga record identity')
        if 'manga_category_memberships' in present:
            if 'manga_categories' not in present:
                orphan = cursor.execute('SELECT 1 FROM manga_category_memberships LIMIT 1').fetchone()
            else:
                orphan = cursor.execute('SELECT 1 FROM manga_category_memberships m LEFT JOIN manga_categories c '
                                        'ON c.id=m.category_id WHERE c.id IS NULL LIMIT 1').fetchone()
            if orphan:
                raise ValueError('Manga membership has an unknown category')

        def text(value, maximum=4096, empty=False):
            if not isinstance(value, str) or (not value and not empty) or len(value) > maximum or '\0' in value:
                raise ValueError('Invalid Manga record identity')
            return value

        def scope(row):
            return [text(row['source_id'], 256), text(row['path'], empty=True)]

        def record(kind, values, existing_id=None):
            encoded = json.dumps(values, ensure_ascii=False, separators=(',', ':'))
            # The longest valid source/path/revision tuple exceeds the generic
            # alias limit. Preserve it in full in the identity anchor instead.
            alias = existing_id or 'v1:' + hashlib.sha256(encoded.encode('utf-8')).hexdigest()
            identity = existing_id or str(uuid5(NAMESPACE_URL, 'urn:keivotos:manga:' + kind + ':' + encoded))
            item_catalog.ensure_item(connection, namespace='manga.' + kind, legacy_key=alias,
                owner='manga', item_type=kind, item_id=identity)
            item_catalog.record_capture(connection, item_id=identity, namespace='manga.record',
                capture_key='identity', format_version=1, payload=dict(kind=kind, key=values))
            return identity

        def relate(child, parent, kind, position=0):
            item_catalog.relate_items(connection, source_item_id=child, target_item_id=parent,
                                      kind=kind, position=position)

        count = 0
        for table, (_, selected) in schemas.items():
            if table not in present:
                continue
            keys = schemas[table][0]
            for row in cursor.execute('SELECT ' + ','.join(selected) + f' FROM {table} ORDER BY ' + ','.join(keys)):
                if table == 'manga_categories':
                    category = text(row['id'], 256)
                    record('category', [category], category)
                elif table in ('manga_reading_sessions', 'manga_reading_progress'):
                    values = scope(row)
                    kind = row['kind']
                    if kind not in ('folder', 'archive'):
                        raise ValueError('Unsupported Manga chapter kind')
                    values.append(kind)
                    chapter = record('chapter', values)
                    if table == 'manga_reading_sessions':
                        saved = record('reading-session', values)
                        relate(saved, chapter, 'chapter')
                    else:
                        revision = text(row['revision'])
                        saved = record('reading-progress', [*values, revision])
                        series = record('series', [values[0], text(row['series_path'], empty=True)])
                        relate(saved, chapter, 'chapter')
                        # The series belongs to this saved revision's context;
                        # historical contexts must not silently reparent a chapter.
                        relate(saved, series, 'series')
                else:
                    values = scope(row)
                    series = record('series', values)
                    if table == 'manga_category_memberships':
                        category = text(row['category_id'], 256)
                        saved = record('category-membership', [category, *values])
                        relate(saved, category, 'category')
                        relate(saved, series, 'series')
                    elif table == 'manga_category_versions':
                        saved = record('category-version', values)
                        relate(saved, series, 'series')
                    else:
                        request = text(row['request_id'], 256)
                        raw = row['category_ids']
                        if not isinstance(raw, str) or len(raw.encode('utf-8')) > 64 * 1024:
                            raise ValueError('Invalid Manga category receipt')
                        try:
                            selected_categories = json.loads(raw)
                        except (ValueError, RecursionError) as error:
                            raise ValueError('Invalid Manga category receipt') from error
                        if not isinstance(selected_categories, list) or len(selected_categories) > 256:
                            raise ValueError('Invalid Manga category receipt')
                        for category in selected_categories:
                            text(category, 256)
                        expected, version = row['expected_version'], row['version']
                        if (type(expected) is not int or expected < 0 or type(version) is not int
                                or version not in (expected, expected + 1)):
                            raise ValueError('Invalid Manga category receipt versions')
                        saved = record('category-update', [request])
                        item_catalog.record_capture(connection, item_id=saved, namespace='manga.category-receipt',
                            capture_key=request, format_version=1,
                            payload=dict(source_id=values[0], path=values[1], expected_version=expected,
                                         version=version, category_ids_json=raw))
                        relate(saved, series, 'series')
                        for position, category in enumerate(selected_categories):
                            known = item_catalog.item_for_legacy(connection, namespace='manga.category', legacy_key=category)
                            if known is not None:
                                if (known['owner'], known['item_type'], known['id']) != ('manga', 'category', category):
                                    raise item_catalog.CatalogConflict('Manga receipt category identity changed')
                                relate(saved, known['id'], 'selected-category', position)
                count += 1
        connection.execute('RELEASE ' + savepoint)
        return count
    except BaseException:
        connection.execute('ROLLBACK TO ' + savepoint)
        connection.execute('RELEASE ' + savepoint)
        raise
    finally:
        cursor.close()

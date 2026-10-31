"""Video categories, watch positions and personal details share complete backups."""
from services.library_categories import CategoryStore

store = CategoryStore('video')
from modules.video.progress import SCHEMA as WATCH_SCHEMA

from modules.video.metadata import SCHEMA as METADATA_SCHEMA

SCHEMA = store.schema + WATCH_SCHEMA + METADATA_SCHEMA


def publish(connection):
    connection.executescript(SCHEMA)
    store.migrate(connection)
    connection.commit()


def import_catalog_records(connection):
    """Explicit video.essential-records/version 1; never discover/read media.

    Original owner tables retain mutable categories, memberships and versions.
    Aliases identify recorded personal state, without granting file permission.
    """
    import hashlib
    import json
    import sqlite3
    from uuid import NAMESPACE_URL, uuid4, uuid5
    from services import item_catalog

    if not connection.in_transaction:
        raise ValueError('Import Video records inside a caller transaction')
    schemas = {
        'video_categories': (('id',), ('id',)),
        'video_category_memberships': (('category_id', 'source_id', 'path'), ('category_id', 'source_id', 'path')),
        'video_category_versions': (('source_id', 'path'), ('source_id', 'path')),
        'video_category_updates': (('request_id',),
            ('request_id', 'source_id', 'path', 'expected_version', 'version', 'category_ids')),
    }
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    savepoint = 'video_records_' + uuid4().hex
    connection.execute('SAVEPOINT ' + savepoint)
    try:
        present = {row['name'] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, (keys, required) in schemas.items():
            if table not in present:
                continue
            if not set(required) <= {row['name'] for row in cursor.execute(f'PRAGMA table_info({table})')}:
                raise ValueError('Unsupported Video essential schema')
            if cursor.execute(f'SELECT 1 FROM {table} GROUP BY ' + ','.join(keys) + ' HAVING count(*)>1 LIMIT 1').fetchone():
                raise ValueError('Duplicate Video record identity')
        if 'video_category_memberships' in present:
            if 'video_categories' not in present:
                orphan = cursor.execute('SELECT 1 FROM video_category_memberships LIMIT 1').fetchone()
            else:
                orphan = cursor.execute('SELECT 1 FROM video_category_memberships m LEFT JOIN video_categories c '
                                        'ON c.id=m.category_id WHERE c.id IS NULL LIMIT 1').fetchone()
            if orphan:
                raise ValueError('Video membership has an unknown category')

        def text(value, maximum=4096, empty=False):
            if not isinstance(value, str) or (not value and not empty) or len(value) > maximum or '\0' in value:
                raise ValueError('Invalid Video record identity')
            return value

        def record(kind, values, existing_id=None):
            encoded = json.dumps(values, ensure_ascii=False, separators=(',', ':'))
            # Compound path keys can exceed the generic alias bound. Their full
            # tuple remains in the immutable anchor; this is not an asset hash.
            alias = existing_id or 'v1:' + hashlib.sha256(encoded.encode('utf-8')).hexdigest()
            identity = existing_id or str(uuid5(NAMESPACE_URL, 'urn:keivotos:video:' + kind + ':' + encoded))
            item_catalog.ensure_item(connection, namespace='video.' + kind, legacy_key=alias,
                owner='video', item_type=kind, item_id=identity)
            item_catalog.record_capture(connection, item_id=identity, namespace='video.record',
                capture_key='identity', format_version=1, payload=dict(kind=kind, key=values))
            return identity

        def relate(child, parent, kind, position=0):
            item_catalog.relate_items(connection, source_item_id=child, target_item_id=parent,
                                      kind=kind, position=position)

        count = 0
        for table, (keys, selected) in schemas.items():
            if table not in present:
                continue
            for row in cursor.execute('SELECT ' + ','.join(selected) + f' FROM {table} ORDER BY ' + ','.join(keys)):
                if table == 'video_categories':
                    category = text(row['id'], 256)
                    record('category', [category], category)
                else:
                    values = [text(row['source_id'], 256), text(row['path'], empty=True)]
                    item = record('item', values)
                    if table == 'video_category_memberships':
                        category = text(row['category_id'], 256)
                        saved = record('category-membership', [category, *values])
                        relate(saved, category, 'category')
                        relate(saved, item, 'item')
                    elif table == 'video_category_versions':
                        saved = record('category-version', values)
                        relate(saved, item, 'item')
                    else:
                        request = text(row['request_id'], 256)
                        raw = row['category_ids']
                        if not isinstance(raw, str) or len(raw.encode('utf-8')) > 64 * 1024:
                            raise ValueError('Invalid Video category receipt')
                        try:
                            selected_categories = json.loads(raw)
                        except (ValueError, RecursionError) as error:
                            raise ValueError('Invalid Video category receipt') from error
                        if not isinstance(selected_categories, list) or len(selected_categories) > 256:
                            raise ValueError('Invalid Video category receipt')
                        for category in selected_categories:
                            text(category, 256)
                        expected, version = row['expected_version'], row['version']
                        if (type(expected) is not int or expected < 0 or type(version) is not int
                                or version not in (expected, expected + 1)):
                            raise ValueError('Invalid Video category receipt versions')
                        saved = record('category-update', [request])
                        item_catalog.record_capture(connection, item_id=saved, namespace='video.category-receipt',
                            capture_key=request, format_version=1,
                            payload=dict(source_id=values[0], path=values[1], expected_version=expected,
                                         version=version, category_ids_json=raw))
                        relate(saved, item, 'item')
                        for position, category in enumerate(selected_categories):
                            known = item_catalog.item_for_legacy(connection, namespace='video.category', legacy_key=category)
                            if known is not None:
                                if (known['owner'], known['item_type'], known['id']) != ('video', 'category', category):
                                    raise item_catalog.CatalogConflict('Video receipt category identity changed')
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

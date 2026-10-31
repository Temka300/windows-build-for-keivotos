"""Additive personal preferences and durable capture receipts; originals stay in Files."""
SCHEMA = '''
CREATE TABLE IF NOT EXISTS twitter_preferences (
 id INTEGER PRIMARY KEY CHECK(id=1), source_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS twitter_downloads (
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, url TEXT NOT NULL,
 post_id TEXT NOT NULL, source_id TEXT NOT NULL, root TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', phase TEXT NOT NULL DEFAULT 'accepted',
 progress REAL NOT NULL DEFAULT 0, attempt INTEGER NOT NULL DEFAULT 0,
 capture_id TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '',
 error TEXT NOT NULL DEFAULT '', retry_requests TEXT NOT NULL DEFAULT '[]',
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS twitter_downloads_queue ON twitter_downloads(status,created_at,id);
'''


def publish(connection):
    connection.executescript(SCHEMA)
    connection.commit()


CATALOG_RECEIPT_FIELDS = ('id', 'request_id', 'url', 'post_id', 'source_id', 'root',
                          'capture_id', 'path', 'created_at')


def _catalog_key(source_id, post_id, capture_id, legacy_id=None):
    """Qualify capture identity; retries can reuse a job ID for another capture."""
    import json
    from modules.twitter import archive

    values = [source_id, post_id, capture_id]
    if legacy_id is not None:
        values.insert(0, legacy_id)
    if any(not isinstance(value, str) or not value or len(value) > 4096 or '\0' in value for value in values):
        raise ValueError('Invalid preserved Twitter identity')
    archive._path_parts(f'captures/{post_id}/{capture_id}/manifest.json')
    return json.dumps(values, ensure_ascii=False, separators=(',', ':'))


def _receipt_record(record):
    from modules.twitter import archive

    if (not isinstance(record, dict) or set(record) != set(CATALOG_RECEIPT_FIELDS)
            or any(not isinstance(value, str) or '\0' in value for value in record.values())
            or any(not record[name] for name in CATALOG_RECEIPT_FIELDS if name != 'root')):
        raise ValueError('Invalid preserved Twitter receipt')
    _catalog_key(record['source_id'], record['post_id'], record['capture_id'], record['id'])
    if (archive._path_parts(record['path']) != (record['post_id'], record['capture_id'])
            or archive.canonical_url(record['url'])[1] != record['post_id']):
        raise ValueError('Preserved Twitter receipt relationships changed')
    return dict(record)


def import_catalog_receipts(connection):
    """Explicit importer twitter.saved-receipts/version 1, without media access.

    Caller transactions preserve every original row. A source/post/capture tuple
    owns the saved identity; job IDs are qualified receipt aliases, never a sole
    item identity. Original manifests remain the content/media authority. Root
    strings are frozen provenance, not permission or a current source location.
    """
    import sqlite3
    from uuid import NAMESPACE_URL, uuid5
    from services import item_catalog

    if not connection.in_transaction:
        raise ValueError('Import Twitter receipts inside a caller transaction')
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    try:
        if not cursor.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='twitter_downloads'").fetchone():
            return 0
        columns = {row[1] for row in cursor.execute('PRAGMA table_info(twitter_downloads)')}
        if not {*CATALOG_RECEIPT_FIELDS, 'status'} <= columns:
            raise ValueError('Unsupported Twitter receipt schema')
        count = 0
        for row in cursor.execute("SELECT * FROM twitter_downloads WHERE status='complete' ORDER BY id"):
            record = _receipt_record({name: row[name] for name in CATALOG_RECEIPT_FIELDS})
            key = _catalog_key(record['source_id'], record['post_id'], record['capture_id'])
            identity = str(uuid5(NAMESPACE_URL, 'urn:keivotos:twitter:capture:' + key))
            for namespace, alias in (('twitter.capture', key), ('twitter.download-capture',
                    _catalog_key(record['source_id'], record['post_id'], record['capture_id'], record['id']))):
                item_catalog.ensure_item(connection, namespace=namespace, legacy_key=alias,
                    owner='twitter', item_type='saved-post', item_id=identity)
            item_catalog.record_capture(connection, item_id=identity, namespace='twitter.download',
                capture_key=record['id'], format_version=1, payload=dict(record=record))
            count += 1
        return count
    finally:
        cursor.close()


def preserved_receipt(connection, legacy_id, *, source_id, post_id, capture_id):
    """Read one exact saved receipt without operational history or file access."""
    from services import item_catalog

    if not isinstance(legacy_id, str) or not legacy_id or len(legacy_id) > 4096 or '\0' in legacy_id:
        raise ValueError('Invalid preserved Twitter receipt identity')
    key = _catalog_key(source_id, post_id, capture_id)
    alias = _catalog_key(source_id, post_id, capture_id, legacy_id)
    item = item_catalog.item_for_legacy(connection, namespace='twitter.capture', legacy_key=key)
    receipt = item_catalog.item_for_legacy(connection, namespace='twitter.download-capture', legacy_key=alias)
    if item is None and receipt is None:
        return None
    if item is None or (item['owner'], item['item_type']) != ('twitter', 'saved-post'):
        raise ValueError('Preserved Twitter capture identity changed')
    if receipt is None:
        if item_catalog.read_capture(connection, item_id=item['id'], namespace='twitter.download', capture_key=legacy_id) is not None:
            raise ValueError('Preserved Twitter receipt alias is missing')
        return None
    if receipt != item:
        raise ValueError('Preserved Twitter receipt alias changed')
    captured = item_catalog.read_capture(connection, item_id=item['id'], namespace='twitter.download', capture_key=legacy_id)
    if (captured is None or captured['format_version'] != 1 or set(captured['payload']) != {'record'}):
        raise ValueError('Unsupported preserved Twitter receipt')
    record = _receipt_record(captured['payload']['record'])
    if (record['id'], record['source_id'], record['post_id'], record['capture_id']) != (legacy_id, source_id, post_id, capture_id):
        raise ValueError('Preserved Twitter receipt identity changed')
    return dict(item_id=item['id'], **captured)


def register_catalog_capture(connection, *, source_id, registered, suite_home, path, receipt=None):
    """Explicit manifest/asset registration inside a caller-owned transaction.

    Full post/source bodies remain in their original files. Capture aliases and
    receipt facts match the offline importer; operational tables are not needed.
    """
    from uuid import NAMESPACE_URL, uuid4, uuid5
    from pathlib import Path
    from modules.twitter import archive
    from services import item_catalog

    if not connection.in_transaction:
        raise ValueError('Register Twitter assets inside a caller transaction')
    library = item_catalog.library_for_legacy(connection, namespace='files.source', legacy_key=source_id)
    if library is None:
        raise ValueError('Register the source library before its Twitter assets')
    registered=tuple(registered)
    source=next((value for value in registered if value.source_id==source_id),None)
    if source is None:raise ValueError('Twitter source is not registered')
    captured_item=archive.capture(source,registered,suite_home,path)
    post_id,capture_id=captured_item['post_id'],captured_item['capture_id']
    key = _catalog_key(source_id, post_id, capture_id)
    library = item_catalog.library_for_legacy(connection, namespace='files.source', legacy_key=source_id)
    if library is None:
        raise ValueError('Register the source library before its Twitter assets')
    registered = tuple(registered)
    source = next((value for value in registered if value.source_id == source_id), None)
    if source is None:
        raise ValueError('Twitter source is not registered')
    if receipt is not None:
        if not isinstance(receipt, dict) or not set(CATALOG_RECEIPT_FIELDS) <= receipt.keys():
            raise ValueError('Invalid published Twitter receipt')
        receipt = _receipt_record({name: receipt[name] for name in CATALOG_RECEIPT_FIELDS})
        if (receipt['source_id'], receipt['post_id'], receipt['capture_id'], receipt['path']) != (
                source_id, post_id, capture_id, path):
            raise ValueError('Twitter receipt does not identify this capture')
    observed = archive.verified_assets(source, registered, suite_home, path)
    if receipt is not None and archive.canonical_url(receipt['url'])[0] != observed['capture']['source_url']:
        raise ValueError('Twitter receipt source URL changed')
    identity = str(uuid5(NAMESPACE_URL, 'urn:keivotos:twitter:capture:' + key))
    from modules.twitter.portable import adjacent
    portable_path=Path(path).parent/'.keivotos/item.json'
    try:
        from files_base.serving import resolve_within_source
        target=resolve_within_source(source.path,portable_path.as_posix(),
            archive.forbidden_roots(source,registered,suite_home),must_exist=False)
        if target.exists():
            identity=adjacent(source,registered,suite_home,path)[0]['item']['id']
    except FileNotFoundError:pass
    assets = {}
    for role, facts in observed['files'].items():
        suffix = facts['path'].rsplit('.', 1)[-1]
        media_type = {'json': 'application/json', 'jpg': 'image/jpeg', 'png': 'image/png',
                      'webp': 'image/webp', 'mp4': 'video/mp4'}[suffix]
        assets[role] = dict(asset_id=str(uuid5(NAMESPACE_URL, identity + ':' + role)),
            media_type=media_type, byte_size=facts['size'], hash_algorithm='sha256', content_hash=facts['sha256'])
    payload = dict(library_id=library['id'], source_id=source_id, post_id=post_id,
                   capture_id=capture_id, path=path, files=observed['files'])
    savepoint = 'twitter_assets_' + uuid4().hex
    connection.execute('SAVEPOINT ' + savepoint)
    try:
        item_catalog.ensure_item(connection, namespace='twitter.capture', legacy_key=key,
            owner='twitter', item_type='saved-post', item_id=identity)
        if receipt is not None:
            alias = _catalog_key(source_id, post_id, capture_id, receipt['id'])
            item_catalog.ensure_item(connection, namespace='twitter.download-capture', legacy_key=alias,
                owner='twitter', item_type='saved-post', item_id=identity)
            item_catalog.record_capture(connection, item_id=identity, namespace='twitter.download',
                capture_key=receipt['id'], format_version=1, payload=dict(record=receipt))
        capture_key = item_catalog.record_asset_capture(connection, item_id=identity,
            namespace='twitter.asset-observation', assets=assets, payload=payload)
        archive.recheck_assets(source, registered, suite_home, observed)
        connection.execute('RELEASE ' + savepoint)
        return dict(item_id=identity, capture_key=capture_key, observation=observed)
    except BaseException:
        connection.execute('ROLLBACK TO ' + savepoint)
        connection.execute('RELEASE ' + savepoint)
        raise

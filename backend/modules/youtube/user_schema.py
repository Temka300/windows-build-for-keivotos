"""Personal playlists; additive tables in the shared user database."""
SCHEMA = """
CREATE TABLE IF NOT EXISTS youtube_channel_folders (
 source_id TEXT NOT NULL, channel_id TEXT NOT NULL, directory TEXT NOT NULL,
 PRIMARY KEY(source_id,channel_id), UNIQUE(source_id,directory)
);
CREATE TABLE IF NOT EXISTS youtube_playlists (
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 revision INTEGER NOT NULL DEFAULT 0, deleted_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS youtube_playlist_items (
 playlist_id TEXT NOT NULL, job_id TEXT NOT NULL, position INTEGER NOT NULL,
 added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 PRIMARY KEY(playlist_id,job_id), UNIQUE(playlist_id,position)
);
CREATE INDEX IF NOT EXISTS youtube_playlist_video ON youtube_playlist_items(job_id);
CREATE TABLE IF NOT EXISTS youtube_playlist_versions (
 job_id TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS youtube_playlist_history (
 playlist_id TEXT NOT NULL, revision INTEGER NOT NULL, request_id TEXT NOT NULL,
 operation TEXT NOT NULL, previous_title TEXT NOT NULL, title TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 PRIMARY KEY(playlist_id,revision), UNIQUE(playlist_id,request_id)
);
"""


def migrate(connection):
    """Restartable additive migration; preserve every existing row."""
    columns={row[0] if not isinstance(row,dict) else row['name']
             for row in connection.execute("SELECT name FROM pragma_table_info('youtube_playlists')")}
    if 'revision' not in columns:
        connection.execute('ALTER TABLE youtube_playlists ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
    if 'deleted_at' not in columns:
        connection.execute("ALTER TABLE youtube_playlists ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
    connection.execute("""INSERT OR IGNORE INTO youtube_playlist_history
        (playlist_id,revision,request_id,operation,previous_title,title,created_at)
        SELECT id,0,request_id,'create','',title,created_at FROM youtube_playlists""")
    from modules.youtube.metadata import COLUMNS
    from modules.youtube.layout import COLUMNS as LAYOUT_COLUMNS
    download_columns={row[0] if not isinstance(row,dict) else row['name']
                      for row in connection.execute("SELECT name FROM pragma_table_info('youtube_downloads')")}
    for name,definition in {**COLUMNS,**LAYOUT_COLUMNS}.items():
        if name not in download_columns:
            connection.execute(f'ALTER TABLE youtube_downloads ADD COLUMN {name} {definition}')


def import_catalog_identities(connection):
    """Bridge completed legacy IDs in a supplied candidate transaction only.

    This registers identities, not verified assets. Original job/playlist rows
    and unavailable media stay untouched; no filesystem or runtime imports run.
    """
    from uuid import NAMESPACE_URL, uuid5
    from services import item_catalog

    if not connection.in_transaction:
        raise ValueError('Import catalog identities inside a caller transaction')
    cursor = connection.cursor()
    cursor.row_factory = None
    try:
        if not cursor.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='youtube_downloads'").fetchone():
            return 0
        columns = {row[1] for row in cursor.execute('PRAGMA table_info(youtube_downloads)')}
        if not {'id', 'status'} <= columns:
            raise ValueError('Unsupported YouTube download schema')
        count = 0
        for (legacy_id,) in cursor.execute("SELECT id FROM youtube_downloads WHERE status='complete' ORDER BY id"):
            if not isinstance(legacy_id, str) or not legacy_id or len(legacy_id) > 4096 or '\0' in legacy_id:
                raise ValueError('Invalid legacy YouTube identity')
            identity = str(uuid5(NAMESPACE_URL, 'urn:keivotos:youtube:download:' + legacy_id))
            item_catalog.ensure_item(connection, namespace='youtube.download', legacy_key=legacy_id,
                                     owner='youtube', item_type='saved-media', item_id=identity)
            count += 1
        return count
    finally:
        cursor.close()


def register_catalog_assets(connection, legacy_id, *, source_id, source_root, forbidden_roots):
    """Explicit staged asset registration; caller authorizes the current source.

    Available-byte observations augment immutable captures. Missing optional
    artwork never erases known assets, and changed registered bytes conflict.
    No job lookup, runtime activation, original-file write or acquisition runs.
    """
    import json
    from pathlib import Path
    from uuid import NAMESPACE_URL, uuid5
    from modules.youtube.metadata import preserved_media
    from services import item_catalog

    library = item_catalog.library_for_legacy(connection, namespace='files.source', legacy_key=source_id)
    if library is None:
        raise ValueError('Register the source library before its saved assets')
    observed = preserved_media(connection, legacy_id, source_id=source_id,
                               source_root=source_root, forbidden_roots=forbidden_roots)
    if observed is None:
        return None
    from files_base.serving import inline_media_type
    assets, references = {}, {}
    for role, value in observed['assets'].items():
        if value is None:
            references[role] = None
            continue
        identity = str(uuid5(NAMESPACE_URL, 'urn:keivotos:youtube:asset:' +
                             json.dumps([observed['item_id'], role], separators=(',', ':'))))
        media_type = 'application/json' if role == 'manifest' else 'text/plain' if role == 'metadata' else inline_media_type(Path(value['path']))[0]
        assets[role] = dict(asset_id=identity, media_type=media_type, byte_size=value['byte_size'],
                            hash_algorithm='sha256', content_hash=value['sha256'])
        references[role] = dict(asset_id=identity, **value)
    payload = dict(legacy_id=legacy_id, source_id=source_id, library_id=library['id'], assets=references)
    capture_key = item_catalog.record_asset_capture(connection, item_id=observed['item_id'],
        namespace='youtube.asset-observation', assets=assets, payload=payload)
    return dict(item_id=observed['item_id'], library_id=library['id'], capture_key=capture_key, assets=references)


def _capture_ancestry(connection, job):
    """Preserve the exact compatible retry owners needed by saved manifests."""
    import sqlite3
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    try:
        owners = [job['id']]
        previous = job
        for _ in range(100):
            parent_id = previous.get('retry_of', '')
            if not parent_id:
                return owners
            if parent_id in owners:
                raise ValueError('Cyclic preserved YouTube retry history')
            parent = cursor.execute('SELECT * FROM youtube_downloads WHERE id=?', (parent_id,)).fetchone()
            if parent is None or any(parent[key] != job[key] for key in ('url', 'source_id', 'root', 'video', 'audio')):
                raise ValueError('Missing or incompatible preserved YouTube retry history')
            previous = dict(parent)
            owners.append(parent_id)
        raise ValueError('Preserved YouTube retry history is too deep')
    finally:
        cursor.close()


def import_catalog_captures(connection):
    """Preserve completed-save facts without reading media or removing job rows.

    Use importer name youtube.saved-media/version 2 for this fuller bridge.
    Original root strings remain frozen location evidence, not portable paths
    or permission to serve files. Callers retain the candidate transaction.
    """
    import sqlite3
    from modules.youtube.metadata import COLUMNS
    from services import item_catalog

    count = import_catalog_identities(connection)
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    try:
        if not cursor.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='youtube_downloads'").fetchone():
            return count
        columns = {row[1] for row in cursor.execute('PRAGMA table_info(youtube_downloads)')}
        required = ('id', 'url', 'source_id', 'root', 'video', 'audio', 'title', 'path', 'thumbnail', 'created_at')
        if not set(required) <= columns:
            raise ValueError('Unsupported YouTube captured-record schema')
        captured = (*required, 'retry_of', *COLUMNS, 'layout_version', 'bundle_path', 'manifest_sha256')
        for row in cursor.execute("SELECT * FROM youtube_downloads WHERE status='complete' ORDER BY id"):
            job = dict(row)
            item = item_catalog.item_for_legacy(connection, namespace='youtube.download', legacy_key=job['id'])
            payload = dict(legacy_job_ids=_capture_ancestry(connection, job),
                           record={name: job[name] for name in captured if name in columns})
            item_catalog.record_capture(connection, item_id=item['id'], namespace='youtube.download',
                                        capture_key=job['id'], format_version=1, payload=payload)
        return count
    finally:
        cursor.close()


def import_catalog_library(connection):
    """Preserve the saved-library baseline before separating job history.

    Explicit importer youtube.saved-library/version 1. Membership is migration
    evidence, not a new writable master or inferred retirement from retry paths.
    Caller transactions retain all legacy rows and roll back the whole import.
    """
    import sqlite3
    from services import item_catalog

    count = import_catalog_captures(connection)
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    try:
        if not cursor.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='youtube_downloads'").fetchone():
            return count
        columns = {row[1] for row in cursor.execute('PRAGMA table_info(youtube_downloads)')}
        superseded = 'superseded_by' if 'superseded_by' in columns else "'' AS superseded_by"
        for row in cursor.execute("SELECT id," + superseded + " FROM youtube_downloads WHERE status='complete' ORDER BY id"):
            successor = row['superseded_by']
            if not isinstance(successor, str) or len(successor) > 4096 or '\0' in successor:
                raise ValueError('Unsupported saved-library visibility')
            item = item_catalog.item_for_legacy(connection, namespace='youtube.download', legacy_key=row['id'])
            item_catalog.record_capture(connection, item_id=item['id'], namespace='youtube.library',
                capture_key=row['id'], format_version=1,
                payload=dict(legacy_id=row['id'], listed=successor == '', superseded_by=successor))
        return count
    finally:
        cursor.close()


def register_catalog_publication(connection, record, *, legacy_job_ids, source_id,
                                 source_root, forbidden_roots):
    """Register one published save, independently of operational tables.

    The caller authorizes the current source and owns the outer transaction.
    A savepoint keeps failed registration from leaving partial catalog facts.
    Initial membership and later retry retirement are immutable evidence.
    Authority/recovery coordination must precede normal runtime selection.
    """
    from uuid import NAMESPACE_URL, uuid4, uuid5
    from modules.youtube.metadata import COLUMNS
    from services import item_catalog

    if not connection.in_transaction:
        raise ValueError('Register publication inside a caller transaction')
    required = ('id', 'url', 'source_id', 'root', 'video', 'audio', 'title', 'path', 'thumbnail', 'created_at')
    if (not isinstance(record, dict) or not set(required) <= record.keys()
            or any(not isinstance(record[name], str) or (name!='title' and '\0' in record[name]) for name in required)
            or any(not record[name] for name in ('id', 'source_id', 'path', 'created_at'))
            or record.get('superseded_by', '') != ''):
        raise ValueError('Invalid published YouTube record')
    legacy_id = record['id']
    if (type(legacy_job_ids) is not list or not 1 <= len(legacy_job_ids) <= 100
            or any(not isinstance(owner, str) or not owner or len(owner) > 4096 or '\0' in owner for owner in legacy_job_ids)
            or legacy_job_ids[0] != legacy_id or len(set(legacy_job_ids)) != len(legacy_job_ids)
            or record.get('retry_of', '') != (legacy_job_ids[1] if len(legacy_job_ids) > 1 else '')
            or record['source_id'] != source_id):
        raise ValueError('Invalid published YouTube relationships')
    if item_catalog.library_for_legacy(connection, namespace='files.source', legacy_key=source_id) is None:
        raise ValueError('Register the source library before its published saves')
    captured = (*required, 'retry_of', *COLUMNS, 'layout_version', 'bundle_path', 'manifest_sha256')
    payload = dict(legacy_job_ids=list(legacy_job_ids), record={name: record[name] for name in captured if name in record})
    savepoint = 'youtube_publication_' + uuid4().hex
    connection.execute('SAVEPOINT ' + savepoint)
    try:
        identity = str(uuid5(NAMESPACE_URL, 'urn:keivotos:youtube:download:' + legacy_id))
        item_catalog.ensure_item(connection, namespace='youtube.download', legacy_key=legacy_id,
            owner='youtube', item_type='saved-media', item_id=identity)
        item_catalog.record_capture(connection, item_id=identity, namespace='youtube.download',
            capture_key=legacy_id, format_version=1, payload=payload)
        observed = register_catalog_assets(connection, legacy_id, source_id=source_id,
            source_root=source_root, forbidden_roots=forbidden_roots)
        if observed is None:
            raise ValueError('Published YouTube media is unavailable')
        item_catalog.record_capture(connection, item_id=identity, namespace='youtube.library',
            capture_key=legacy_id, format_version=1,
            payload=dict(legacy_id=legacy_id, listed=True, superseded_by=''))
        for child, parent in zip(legacy_job_ids, legacy_job_ids[1:]):
            replacement = {name: record[name] for name in ('url', 'source_id', 'root', 'video', 'audio')}
            retire_catalog_save(connection, parent, dict(replacement, id=child, retry_of=parent))
        connection.execute('RELEASE ' + savepoint)
        return observed
    except BaseException:
        connection.execute('ROLLBACK TO ' + savepoint)
        connection.execute('RELEASE ' + savepoint)
        raise


def retire_catalog_save(connection, legacy_id, replacement):
    """Preserve a direct accepted retry without rewriting saved facts or jobs.

    An uncatalogued failed attempt has no saved membership to retire. A known
    save requires compatible request identity and complete original evidence.
    The caller owns the transaction and proves the durable acceptance link.
    """
    from modules.youtube.metadata import preserved_capture
    from services import item_catalog

    if not connection.in_transaction:
        raise ValueError('Retire saved membership inside a caller transaction')
    names = ('id', 'retry_of', 'url', 'source_id', 'root', 'video', 'audio')
    if (not isinstance(legacy_id, str) or not legacy_id or len(legacy_id) > 4096 or '\0' in legacy_id
            or not isinstance(replacement, dict) or not set(names) <= replacement.keys()
            or any(not isinstance(replacement[name], str) or not replacement[name]
                   or len(replacement[name]) > 4096 or '\0' in replacement[name] for name in names)
            or replacement['retry_of'] != legacy_id or replacement['id'] == legacy_id):
        raise ValueError('Invalid saved retry relationship')
    item = item_catalog.item_for_legacy(connection, namespace='youtube.download', legacy_key=legacy_id)
    if item is None:
        return None
    captured = preserved_capture(connection, legacy_id)
    if captured is None:
        raise ValueError('Preserve saved facts before retiring membership')
    if any(captured['payload']['record'].get(name) != replacement[name] for name in names[2:]):
        raise item_catalog.CatalogConflict('Saved retry request identity changed')
    membership = item_catalog.read_capture(connection, item_id=item['id'], namespace='youtube.library', capture_key=legacy_id)
    if (membership is None or membership['format_version'] != 1
            or set(membership['payload']) != {'legacy_id', 'listed', 'superseded_by'}):
        raise ValueError('Preserve saved membership before retiring it')
    original = membership['payload']
    if (original['legacy_id'] != legacy_id or type(original['listed']) is not bool
            or not isinstance(original['superseded_by'], str)
            or original['listed'] != (original['superseded_by'] == '')):
        raise ValueError('Invalid preserved saved membership')
    if not original['listed'] and original['superseded_by'] != replacement['id']:
        raise item_catalog.CatalogConflict('Saved membership already has a different successor')
    payload = dict(legacy_id=legacy_id, superseded_by=replacement['id'])
    item_catalog.record_capture(connection, item_id=item['id'], namespace='youtube.retirement',
        capture_key=legacy_id, format_version=1, payload=payload)
    return payload

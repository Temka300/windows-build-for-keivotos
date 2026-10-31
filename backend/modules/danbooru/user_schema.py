"""Danbooru-owned tables and additive identity migrations in the shared user DB."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from database import _ensure_column
from storage_layout import deterministic_root_id

USER_SCHEMA = """
            CREATE TABLE IF NOT EXISTS favorites (
                file_id INTEGER PRIMARY KEY,
                file_path TEXT,
                local_md5 TEXT,
                pinned_at TEXT,
                added_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS collections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                pinned_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS collection_items (
                collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                file_id INTEGER NOT NULL,
                file_path TEXT,
                local_md5 TEXT,
                pinned_at TEXT,
                added_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (collection_id, file_id)
            );

            CREATE INDEX IF NOT EXISTS idx_collection_items_file
                ON collection_items(file_id);

            CREATE TABLE IF NOT EXISTS favorite_tags (
                tag_name TEXT NOT NULL,
                tag_category TEXT NOT NULL,
                pinned_at TEXT,
                added_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (tag_name, tag_category)
            );

            CREATE TABLE IF NOT EXISTS favorite_tag_combos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                tag_key TEXT NOT NULL UNIQUE,
                tags_json TEXT NOT NULL,
                added_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS blacklist_tags (
                tag_name TEXT PRIMARY KEY,
                added_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS user_image_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL,
                file_path TEXT,
                local_md5 TEXT,
                tag_category TEXT NOT NULL DEFAULT 'general',
                tag_name TEXT NOT NULL,
                added_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(file_path, tag_category, tag_name)
            );

            CREATE TABLE IF NOT EXISTS image_views (
                file_id INTEGER PRIMARY KEY,
                file_path TEXT,
                local_md5 TEXT,
                view_count INTEGER NOT NULL DEFAULT 0,
                heart_spam_count INTEGER NOT NULL DEFAULT 0,
                first_viewed_at TEXT,
                last_viewed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS tag_wiki_cache (
                tag_name TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                other_names_json TEXT NOT NULL DEFAULT '[]',
                body TEXT NOT NULL DEFAULT '',
                aliases_json TEXT NOT NULL DEFAULT '[]',
                implications_json TEXT NOT NULL DEFAULT '[]',
                artist_id INTEGER,
                artist_name TEXT,
                artist_group_name TEXT,
                artist_urls_json TEXT NOT NULL DEFAULT '[]',
                artist_urls_checked INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'ok',
                error TEXT,
                fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS artist_follows (
                tag_name TEXT PRIMARY KEY,
                tag_category TEXT NOT NULL DEFAULT 'artist',
                display_name TEXT,
                danbooru_enabled INTEGER NOT NULL DEFAULT 1,
                last_checked_at TEXT,
                notification_initialized_at TEXT,
                last_seen_danbooru_post_id INTEGER,
                notes TEXT NOT NULL DEFAULT '',
                added_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS artist_follow_posts (
                tag_name TEXT NOT NULL REFERENCES artist_follows(tag_name) ON DELETE CASCADE,
                danbooru_post_id INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT 'danbooru',
                seen_at TEXT,
                discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (tag_name, danbooru_post_id)
            );

            CREATE TABLE IF NOT EXISTS artist_profile_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag_name TEXT NOT NULL,
                platform TEXT NOT NULL,
                asset_kind TEXT NOT NULL,
                source_profile_url TEXT NOT NULL,
                source_url TEXT NOT NULL,
                file_path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                captured_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(tag_name, platform, asset_kind, content_hash)
            );

            CREATE TABLE IF NOT EXISTS tag_removals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER,
                file_path TEXT,
                local_md5 TEXT,
                removed_tags_json TEXT NOT NULL DEFAULT '[]',
                checked_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS registered_folders (
                name TEXT PRIMARY KEY,
                added_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

"""


def migrate_user_tables(conn: sqlite3.Connection, data_db_path: Path, data_root: Path) -> None:
    _ensure_column(conn, "favorites", "file_path", "TEXT")
    _ensure_column(conn, "favorites", "local_md5", "TEXT")
    _ensure_column(conn, "favorites", "pinned_at", "TEXT")
    _ensure_column(conn, "collections", "pinned_at", "TEXT")
    _ensure_column(conn, "collection_items", "file_path", "TEXT")
    _ensure_column(conn, "collection_items", "local_md5", "TEXT")
    _ensure_column(conn, "collection_items", "pinned_at", "TEXT")
    _ensure_column(conn, "favorite_tags", "pinned_at", "TEXT")
    _ensure_column(conn, "user_image_tags", "file_path", "TEXT")
    _ensure_column(conn, "user_image_tags", "local_md5", "TEXT")
    _ensure_column(conn, "user_image_tags", "tag_category", "TEXT NOT NULL DEFAULT 'general'")
    _ensure_column(conn, "image_views", "file_path", "TEXT")
    _ensure_column(conn, "image_views", "local_md5", "TEXT")
    _ensure_column(conn, "image_views", "first_viewed_at", "TEXT")
    _ensure_column(conn, "image_views", "last_viewed_at", "TEXT")
    _ensure_column(conn, "image_views", "heart_spam_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "tag_wiki_cache", "artist_id", "INTEGER")
    _ensure_column(conn, "tag_wiki_cache", "artist_name", "TEXT")
    _ensure_column(conn, "tag_wiki_cache", "artist_group_name", "TEXT")
    _ensure_column(conn, "tag_wiki_cache", "artist_urls_json", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "tag_wiki_cache", "artist_urls_checked", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "artist_follows", "tag_category", "TEXT NOT NULL DEFAULT 'artist'")
    _ensure_column(conn, "artist_follows", "display_name", "TEXT")
    _ensure_column(conn, "artist_follows", "danbooru_enabled", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "artist_follows", "last_checked_at", "TEXT")
    _ensure_column(conn, "artist_follows", "notification_initialized_at", "TEXT")
    _ensure_column(conn, "artist_follows", "last_seen_danbooru_post_id", "INTEGER")
    _ensure_column(conn, "artist_follows", "notes", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "artist_follow_posts", "seen_at", "TEXT")
    _ensure_column(conn, "artist_follow_posts", "source", "TEXT NOT NULL DEFAULT 'danbooru'")
    _ensure_column(conn, "registered_folders", "path", "TEXT")
    _ensure_column(conn, "registered_folders", "root_id", "TEXT")
    _ensure_column(conn, "registered_folders", "display_name", "TEXT")
    for row in conn.execute(
        "SELECT name FROM registered_folders WHERE path IS NULL OR path=''"
    ).fetchall():
        conn.execute(
            "UPDATE registered_folders SET path=? WHERE name=?",
            (str(data_root / row["name"]), row["name"]),
        )
    for row in conn.execute(
        "SELECT name, path FROM registered_folders WHERE root_id IS NULL OR root_id=''"
    ).fetchall():
        folder_path = Path(row["path"] or (data_root / row["name"]))
        conn.execute(
            "UPDATE registered_folders SET root_id=? WHERE name=?",
            (deterministic_root_id(folder_path), row["name"]),
        )
    conn.execute(
        "UPDATE registered_folders SET display_name=name "
        "WHERE display_name IS NULL OR display_name=''"
    )
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_favorites_file_path
            ON favorites(file_path);
        CREATE INDEX IF NOT EXISTS idx_favorites_local_md5
            ON favorites(local_md5);
        CREATE INDEX IF NOT EXISTS idx_favorites_pinned_at
            ON favorites(pinned_at);
        CREATE INDEX IF NOT EXISTS idx_collections_pinned_at
            ON collections(pinned_at);
        CREATE INDEX IF NOT EXISTS idx_collection_items_file_path
            ON collection_items(file_path);
        CREATE INDEX IF NOT EXISTS idx_collection_items_local_md5
            ON collection_items(local_md5);
        CREATE INDEX IF NOT EXISTS idx_collection_items_pinned_at
            ON collection_items(collection_id, pinned_at);
        CREATE INDEX IF NOT EXISTS idx_favorite_tags_pinned_at
            ON favorite_tags(pinned_at);
        CREATE INDEX IF NOT EXISTS idx_user_image_tags_file
            ON user_image_tags(file_id);
        CREATE INDEX IF NOT EXISTS idx_user_image_tags_file_path
            ON user_image_tags(file_path);
        CREATE INDEX IF NOT EXISTS idx_user_image_tags_local_md5
            ON user_image_tags(local_md5);
        CREATE INDEX IF NOT EXISTS idx_user_image_tags_name
            ON user_image_tags(tag_name);
        CREATE INDEX IF NOT EXISTS idx_user_image_tags_category
            ON user_image_tags(tag_category);
        CREATE INDEX IF NOT EXISTS idx_user_image_tags_category_name
            ON user_image_tags(tag_category, tag_name);
        CREATE INDEX IF NOT EXISTS idx_image_views_file_path
            ON image_views(file_path);
        CREATE INDEX IF NOT EXISTS idx_image_views_local_md5
            ON image_views(local_md5);
        CREATE INDEX IF NOT EXISTS idx_image_views_last_viewed
            ON image_views(last_viewed_at);
        CREATE INDEX IF NOT EXISTS idx_image_views_heart_spam
            ON image_views(heart_spam_count);
        CREATE INDEX IF NOT EXISTS idx_artist_follows_added
            ON artist_follows(added_at);
        CREATE INDEX IF NOT EXISTS idx_artist_follow_posts_seen
            ON artist_follow_posts(tag_name, seen_at);
        CREATE INDEX IF NOT EXISTS idx_artist_follow_posts_post
            ON artist_follow_posts(danbooru_post_id);
        CREATE INDEX IF NOT EXISTS idx_artist_profile_assets_tag
            ON artist_profile_assets(tag_name, captured_at);
        CREATE INDEX IF NOT EXISTS idx_artist_profile_assets_identity
            ON artist_profile_assets(tag_name, platform, asset_kind, content_hash);
        CREATE INDEX IF NOT EXISTS idx_tag_removals_file_path
            ON tag_removals(file_path);
        CREATE INDEX IF NOT EXISTS idx_tag_removals_local_md5
            ON tag_removals(local_md5);
        CREATE INDEX IF NOT EXISTS idx_tag_removals_checked_at
            ON tag_removals(checked_at);
    """)

    if not data_db_path.exists():
        return

    conn.execute("ATTACH DATABASE ? AS datadb", (str(data_db_path),))
    try:
        conn.executescript("""
            UPDATE favorites
               SET file_path = COALESCE(file_path, (
                       SELECT path FROM datadb.files WHERE datadb.files.id = favorites.file_id
                   )),
                   local_md5 = COALESCE(local_md5, (
                       SELECT local_md5 FROM datadb.files WHERE datadb.files.id = favorites.file_id
                   ));

            UPDATE collection_items
               SET file_path = COALESCE(file_path, (
                       SELECT path FROM datadb.files WHERE datadb.files.id = collection_items.file_id
                   )),
                   local_md5 = COALESCE(local_md5, (
                       SELECT local_md5 FROM datadb.files WHERE datadb.files.id = collection_items.file_id
                   ));

            UPDATE user_image_tags
               SET file_path = COALESCE(file_path, (
                       SELECT path FROM datadb.files WHERE datadb.files.id = user_image_tags.file_id
                   )),
                   local_md5 = COALESCE(local_md5, (
                       SELECT local_md5 FROM datadb.files WHERE datadb.files.id = user_image_tags.file_id
                   ));

            UPDATE image_views
               SET file_path = COALESCE(file_path, (
                       SELECT path FROM datadb.files WHERE datadb.files.id = image_views.file_id
                   )),
                   local_md5 = COALESCE(local_md5, (
                       SELECT local_md5 FROM datadb.files WHERE datadb.files.id = image_views.file_id
                   ));
        """)
    finally:
        conn.commit()
        conn.execute("DETACH DATABASE datadb")


def import_catalog_records(connection: sqlite3.Connection) -> int:
    """Explicit danbooru.essential-records/version 1, without index/media IO.

    The complete essential snapshot remains authoritative for personal values.
    These profile-local aliases identify records, not inferred physical files.
    Existing path/MD5/file-ID matching stays unchanged; references are retained
    as observations for the later index bridge. No legacy row is rewritten.
    """
    import hashlib
    import json
    from uuid import NAMESPACE_URL, uuid4, uuid5
    from services import item_catalog

    if not connection.in_transaction:
        raise ValueError('Import Danbooru records inside a caller transaction')
    records = (
        ('favorites', 'favorite', ('file_id',)),
        ('collections', 'collection', ('id',)),
        ('collection_items', 'collection-item', ('collection_id', 'file_id')),
        ('favorite_tags', 'favorite-tag', ('tag_name', 'tag_category')),
        ('favorite_tag_combos', 'tag-combo', ('id',)),
        ('blacklist_tags', 'blacklist-tag', ('tag_name',)),
        ('user_image_tags', 'user-tag', ('id',)),
        ('image_views', 'image-view', ('file_id',)),
        ('tag_wiki_cache', 'wiki', ('tag_name',)),
        ('artist_follows', 'artist-follow', ('tag_name',)),
        ('artist_follow_posts', 'artist-post', ('tag_name', 'danbooru_post_id')),
        ('artist_profile_assets', 'profile-capture', ('id',)),
        ('tag_removals', 'tag-removal', ('id',)),
        ('registered_folders', 'registered-folder', ('name',)),
    )
    reference_tables = {'favorites', 'collection_items', 'user_image_tags', 'image_views', 'tag_removals'}
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    savepoint = 'danbooru_records_' + uuid4().hex
    connection.execute('SAVEPOINT ' + savepoint)
    try:
        present = {row['name'] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {}
        for table, _, keys in records:
            if table not in present:
                continue
            columns[table] = {row['name'] for row in cursor.execute(f'PRAGMA table_info({table})')}
            required = set(keys)
            if table == 'artist_profile_assets':
                required.update(('tag_name', 'platform', 'asset_kind', 'content_hash'))
            if not required <= columns[table]:
                raise ValueError('Unsupported Danbooru essential schema')
            if cursor.execute(f'SELECT 1 FROM {table} GROUP BY ' + ','.join(keys) + ' HAVING count(*)>1 LIMIT 1').fetchone():
                raise ValueError('Duplicate Danbooru record identity')
        for child, parent, column, parent_key in (
                ('collection_items', 'collections', 'collection_id', 'id'),
                ('artist_follow_posts', 'artist_follows', 'tag_name', 'tag_name')):
            if child not in columns:
                continue
            if parent not in columns:
                orphan = cursor.execute(f'SELECT 1 FROM {child} LIMIT 1').fetchone()
            else:
                orphan = cursor.execute(f'SELECT 1 FROM {child} c LEFT JOIN {parent} p '
                    f'ON p.{parent_key}=c.{column} WHERE p.{parent_key} IS NULL LIMIT 1').fetchone()
            if orphan:
                raise ValueError('Danbooru record has an unknown parent')

        def key_value(name, value):
            if name in ('id', 'file_id', 'collection_id', 'danbooru_post_id'):
                if type(value) is not int or not 0 < value <= 2**63 - 1:
                    raise ValueError('Invalid Danbooru numeric record identity')
            elif not isinstance(value, str) or not value or len(value) > 4096 or '\0' in value:
                raise ValueError('Invalid Danbooru text record identity')
            return value

        def encoded(values):
            return json.dumps(values, ensure_ascii=False, separators=(',', ':'))

        def identity(kind, key):
            return str(uuid5(NAMESPACE_URL, 'urn:keivotos:danbooru:' + kind + ':' + encoded(key)))

        count = 0
        for table, kind, keys in records:
            if table not in columns:
                continue
            selected = list(keys)
            if table in reference_tables:
                selected += [name for name in ('file_id', 'file_path', 'local_md5')
                             if name in columns[table] and name not in selected]
            if table == 'artist_profile_assets':
                selected += ['tag_name', 'platform', 'asset_kind', 'content_hash']
            if table == 'registered_folders' and 'root_id' in columns[table]:
                selected.append('root_id')
            for row in cursor.execute('SELECT ' + ','.join(selected) + f' FROM {table} ORDER BY ' + ','.join(keys)):
                legacy_key = [key_value(name, row[name]) for name in keys]
                canonical = legacy_key
                if table == 'registered_folders' and 'root_id' in selected and row['root_id'] not in (None, ''):
                    canonical = ['root', key_value('root_id', row['root_id'])]
                record_id = identity(kind, canonical)
                item_catalog.ensure_item(connection, namespace='danbooru.' + kind, legacy_key=encoded(legacy_key),
                    owner='danbooru', item_type=kind, item_id=record_id)
                if table == 'registered_folders' and canonical is not legacy_key:
                    item_catalog.ensure_item(connection, namespace='danbooru.root', legacy_key=canonical[1],
                        owner='danbooru', item_type=kind, item_id=record_id)
                anchor = dict(table=table, key=canonical)
                if table == 'artist_profile_assets':
                    anchor.update({name: key_value(name, row[name]) for name in ('tag_name', 'platform', 'asset_kind', 'content_hash')})
                item_catalog.record_capture(connection, item_id=record_id, namespace='danbooru.record',
                    capture_key='identity', format_version=1, payload=anchor)
                if table in reference_tables:
                    values = {name: row[name] for name in ('file_id', 'file_path', 'local_md5') if name in selected}
                    for name in ('file_path', 'local_md5'):
                        value = values.get(name)
                        if value is not None and (not isinstance(value, str) or '\0' in value):
                            raise ValueError('Invalid Danbooru legacy file reference')
                    if values.get('file_id') is not None:
                        key_value('file_id', values['file_id'])
                    mode = ('path' if values.get('file_path') is not None else 'md5' if values.get('local_md5') is not None
                            else 'file-id' if values.get('file_id') is not None else 'unknown')
                    payload = dict(match=mode, values=values)
                    key = '1:' + hashlib.sha256(encoded(payload).encode('utf-8')).hexdigest()
                    item_catalog.record_capture(connection, item_id=record_id, namespace='danbooru.file-reference',
                        capture_key=key, format_version=1, payload=payload)
                if table == 'collection_items':
                    parent_id = identity('collection', [row['collection_id']])
                    item_catalog.relate_items(connection, source_item_id=record_id, target_item_id=parent_id,
                                              kind='in-collection')
                if table == 'artist_follow_posts':
                    parent_id = identity('artist-follow', [row['tag_name']])
                    item_catalog.relate_items(connection, source_item_id=record_id, target_item_id=parent_id,
                                              kind='followed-artist')
                if table == 'artist_profile_assets':
                    asset_id = str(uuid5(NAMESPACE_URL, 'urn:keivotos:danbooru:profile-asset:' + encoded(legacy_key)))
                    item_catalog.record_asset(connection, asset_id=asset_id, media_type='application/octet-stream',
                                              hash_algorithm='sha256', content_hash=row['content_hash'])
                    item_catalog.link_asset(connection, item_id=record_id, asset_id=asset_id, role='profile-image')
                count += 1
        connection.execute('RELEASE ' + savepoint)
        return count
    except BaseException:
        connection.execute('ROLLBACK TO ' + savepoint)
        connection.execute('RELEASE ' + savepoint)
        raise
    finally:
        cursor.close()



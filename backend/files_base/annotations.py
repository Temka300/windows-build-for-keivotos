"""User-authored file/folder annotations for the Files base (V1.1.1).

The base authors no metadata *about the disk* — but the *user* can attach their
own origin notes to any file or folder: a description, one or more source/mirror/
discussion links, and (in a later slice) screenshots or clips. This is
hand-made, irreplaceable data, so it lives in the shared ``user.sqlite`` beside
favorites and collections — never in the disposable ``files.sqlite`` index.
See docs/important/SUITE_MODULE_CONTRACT.md sections 3, 4, and 5.

Identity (SUITE_MODULE_CONTRACT.md section 4):
- A **file** annotation is keyed by the file's **content hash** (MD5), so the
  note follows a rename or move, survives an unplugged drive, and is shared by
  two byte-identical copies. ``source_id``/``relative_path`` are stored only as a
  last-known-location hint, refreshed on write.
- A **folder** annotation is keyed by ``(source_id, relative_path)`` because a
  folder has no content hash. Renaming a folder needs a manual re-attach (v1).

The hash itself is computed by the HTTP layer *when the user annotates a file*
(lazy, on-demand) and passed in here; this module reads no file bytes and never
imports Danbooru or ``core``, matching the ``sources.py`` isolation style.

Nothing here removes an annotation implicitly: not a rescan, not a forget, not a
missing file (missing != deleted, section 4.3). Only ``delete_annotation`` does.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone


FILES_ANNOTATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS files_annotations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_kind   TEXT NOT NULL,
    content_hash   TEXT,
    source_id      TEXT NOT NULL,
    relative_path  TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    author         TEXT NOT NULL DEFAULT '',
    extra_info     TEXT NOT NULL DEFAULT '',
    details        TEXT NOT NULL DEFAULT '[]',
    metadata_applied INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_files_annotations_hash
    ON files_annotations(content_hash) WHERE content_hash IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_files_annotations_path
    ON files_annotations(source_id, relative_path) WHERE content_hash IS NULL;

CREATE TABLE IF NOT EXISTS files_annotation_links (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    annotation_id  INTEGER NOT NULL REFERENCES files_annotations(id) ON DELETE CASCADE,
    url            TEXT NOT NULL,
    label          TEXT NOT NULL DEFAULT '',
    kind           TEXT NOT NULL DEFAULT 'source',
    position       INTEGER NOT NULL DEFAULT 0,
    added_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_files_annotation_links_annotation
    ON files_annotation_links(annotation_id, position);

CREATE TABLE IF NOT EXISTS files_annotation_attachments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    annotation_id  INTEGER NOT NULL REFERENCES files_annotations(id) ON DELETE CASCADE,
    content_hash   TEXT NOT NULL,
    file_name      TEXT NOT NULL,
    media_type     TEXT NOT NULL,
    size           INTEGER NOT NULL,
    width          INTEGER,
    height         INTEGER,
    caption        TEXT NOT NULL DEFAULT '',
    is_cover       INTEGER NOT NULL DEFAULT 0,
    position       INTEGER NOT NULL DEFAULT 0,
    stored_root    TEXT,
    added_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_files_annotation_attachments_annotation
    ON files_annotation_attachments(annotation_id, position);
"""

# The set of link kinds the UI groups by. Stored leniently (any non-empty string
# is accepted) so a future kind never rejects a save; the HTTP layer maps unknown
# values onto this set for display.
LINK_KINDS = ("source", "discussion", "mirror", "author", "other")


@dataclass(frozen=True)
class AnnotationLink:
    id: int
    url: str
    label: str
    kind: str
    position: int


@dataclass(frozen=True)
class AnnotationAttachment:
    id: int
    content_hash: str
    file_name: str
    media_type: str
    size: int
    width: int | None
    height: int | None
    caption: str
    is_cover: bool
    position: int


@dataclass(frozen=True)
class StoredAttachment:
    """An attachment row plus where its bytes live, for serving and cleanup."""

    id: int
    annotation_id: int
    content_hash: str
    file_name: str
    media_type: str
    size: int
    stored_root: str | None


@dataclass(frozen=True)
class Annotation:
    id: int
    subject_kind: str
    content_hash: str | None
    source_id: str
    relative_path: str
    description: str
    author: str
    extra_info: str
    created_at: str | None
    updated_at: str | None
    links: list[AnnotationLink] = field(default_factory=list)
    attachments: list[AnnotationAttachment] = field(default_factory=list)
    details: list[dict[str, str]] = field(default_factory=list)
    metadata_applied: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_columns(user_conn: sqlite3.Connection, table: str) -> set[str]:
    columns: set[str] = set()
    for row in user_conn.execute(f"PRAGMA table_info({table})").fetchall():
        try:
            columns.add(str(row["name"]))
        except (KeyError, TypeError):
            columns.add(str(row[1]))
    return columns


def ensure_annotations_schema(user_conn: sqlite3.Connection) -> None:
    """Create the annotation tables if absent (idempotent, additive)."""
    user_conn.executescript(FILES_ANNOTATIONS_SCHEMA)
    if "stored_root" not in _table_columns(user_conn, "files_annotation_attachments"):
        user_conn.execute(
            "ALTER TABLE files_annotation_attachments ADD COLUMN stored_root TEXT"
        )
    annotation_columns = _table_columns(user_conn, "files_annotations")
    for column in ("author", "extra_info"):
        if column not in annotation_columns:
            user_conn.execute(
                f"ALTER TABLE files_annotations ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
            )
    if "details" not in annotation_columns:
        user_conn.execute("ALTER TABLE files_annotations ADD COLUMN details TEXT NOT NULL DEFAULT '[]'")
    if "metadata_applied" not in annotation_columns:
        user_conn.execute("ALTER TABLE files_annotations ADD COLUMN metadata_applied INTEGER NOT NULL DEFAULT 0")
    user_conn.commit()


# --- Loading -------------------------------------------------------------

def _link_from_row(row: sqlite3.Row) -> AnnotationLink:
    return AnnotationLink(
        id=row["id"],
        url=row["url"],
        label=row["label"],
        kind=row["kind"],
        position=row["position"],
    )


def _attachment_from_row(row: sqlite3.Row) -> AnnotationAttachment:
    return AnnotationAttachment(
        id=row["id"],
        content_hash=row["content_hash"],
        file_name=row["file_name"],
        media_type=row["media_type"],
        size=row["size"],
        width=row["width"],
        height=row["height"],
        caption=row["caption"],
        is_cover=bool(row["is_cover"]),
        position=row["position"],
    )


def list_links(user_conn: sqlite3.Connection, annotation_id: int) -> list[AnnotationLink]:
    rows = user_conn.execute(
        "SELECT id, url, label, kind, position FROM files_annotation_links "
        "WHERE annotation_id = ? ORDER BY position ASC, id ASC",
        (annotation_id,),
    ).fetchall()
    return [_link_from_row(row) for row in rows]


def list_attachments(
    user_conn: sqlite3.Connection, annotation_id: int
) -> list[AnnotationAttachment]:
    rows = user_conn.execute(
        "SELECT id, content_hash, file_name, media_type, size, width, height, "
        "caption, is_cover, position FROM files_annotation_attachments "
        "WHERE annotation_id = ? ORDER BY position ASC, id ASC",
        (annotation_id,),
    ).fetchall()
    return [_attachment_from_row(row) for row in rows]


def _load(user_conn: sqlite3.Connection, row: sqlite3.Row) -> Annotation:
    annotation_id = row["id"]
    return Annotation(
        id=annotation_id,
        subject_kind=row["subject_kind"],
        content_hash=row["content_hash"],
        source_id=row["source_id"],
        relative_path=row["relative_path"],
        description=row["description"],
        author=row["author"],
        extra_info=row["extra_info"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        links=list_links(user_conn, annotation_id),
        attachments=list_attachments(user_conn, annotation_id),
        details=json.loads(row["details"]),
        metadata_applied=bool(row["metadata_applied"]),
    )


def get_annotation(
    user_conn: sqlite3.Connection,
    *,
    content_hash: str | None = None,
    source_id: str,
    relative_path: str,
) -> Annotation | None:
    """Resolve a subject to its annotation, hash-first then path-fallback.

    A file (``content_hash`` given) matches purely by hash, so the note follows
    a rename/move. A folder, or a file with no annotation yet, falls back to the
    path lookup, which only ever matches folder rows (file rows always carry a
    hash). Returns ``None`` when nothing is annotated.
    """
    if content_hash:
        row = user_conn.execute(
            "SELECT * FROM files_annotations WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
        if row is not None:
            return _load(user_conn, row)
    row = user_conn.execute(
        "SELECT * FROM files_annotations "
        "WHERE content_hash IS NULL AND source_id = ? AND relative_path = ?",
        (source_id, relative_path),
    ).fetchone()
    return _load(user_conn, row) if row is not None else None


def get_file_annotation_by_location(
    user_conn: sqlite3.Connection, source_id: str, relative_path: str
) -> Annotation | None:
    """A file note found by its last-known location, when the hash isn't handy.

    Used as a read-time fallback for a file whose index hash is momentarily
    absent. It matches only unmoved files (the stored hint is refreshed on every
    write); a genuinely moved file is re-found by hash once it is re-hashed.
    """
    row = user_conn.execute(
        "SELECT * FROM files_annotations "
        "WHERE content_hash IS NOT NULL AND source_id = ? AND relative_path = ?",
        (source_id, relative_path),
    ).fetchone()
    return _load(user_conn, row) if row is not None else None


def _touch(user_conn: sqlite3.Connection, annotation_id: int) -> None:
    user_conn.execute(
        "UPDATE files_annotations SET updated_at = ? WHERE id = ?",
        (_now(), annotation_id),
    )


def upsert_annotation(
    user_conn: sqlite3.Connection,
    *,
    subject_kind: str,
    source_id: str,
    relative_path: str,
    content_hash: str | None = None,
    description: str | None = None,
    author: str | None = None,
    extra_info: str | None = None,
    details: list[dict[str, str]] | None = None,
    metadata_applied: bool | None = None,
) -> Annotation:
    """Find-or-create the annotation for a subject and optionally set its fields.

    Any field passed as ``None`` is left untouched (and created empty), so a
    caller can guarantee a row exists before attaching a link, or update one
    field without disturbing the rest. Creating a row also refreshes the stored
    last-known-location hint.
    """
    if subject_kind not in ("file", "dir"):
        raise ValueError("subject_kind must be 'file' or 'dir'")
    if subject_kind == "file" and not content_hash:
        raise ValueError("A file annotation requires a content hash")
    if subject_kind == "dir" and content_hash:
        raise ValueError("A folder annotation must not carry a content hash")

    existing = get_annotation(
        user_conn,
        content_hash=content_hash,
        source_id=source_id,
        relative_path=relative_path,
    )
    now = _now()
    if existing is None:
        user_conn.execute(
            "INSERT INTO files_annotations "
            "(subject_kind, content_hash, source_id, relative_path, description, "
            " author, extra_info, details, metadata_applied, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                subject_kind,
                content_hash,
                source_id,
                relative_path,
                (description or "").strip(),
                (author or "").strip(),
                (extra_info or "").strip(),
                json.dumps(details or [], ensure_ascii=False),
                bool(metadata_applied),
                now,
                now,
            ),
        )
    else:
        assignments = ["source_id = ?", "relative_path = ?", "updated_at = ?"]
        params: list[object] = [source_id, relative_path, now]
        if description is not None:
            assignments.append("description = ?")
            params.append(description.strip())
        if author is not None:
            assignments.append("author = ?")
            params.append(author.strip())
        if extra_info is not None:
            assignments.append("extra_info = ?")
            params.append(extra_info.strip())
        if details is not None:
            assignments.append("details = ?")
            params.append(json.dumps(details, ensure_ascii=False))
        if metadata_applied is not None:
            assignments.append("metadata_applied = ?")
            params.append(bool(metadata_applied))
        params.append(existing.id)
        user_conn.execute(
            f"UPDATE files_annotations SET {', '.join(assignments)} WHERE id = ?",
            params,
        )
    user_conn.commit()
    resolved = get_annotation(
        user_conn,
        content_hash=content_hash,
        source_id=source_id,
        relative_path=relative_path,
    )
    assert resolved is not None
    return resolved


def set_links(
    user_conn: sqlite3.Connection,
    annotation_id: int,
    links: list[dict[str, str]],
) -> list[AnnotationLink]:
    """Replace the whole link list in one idempotent write (order preserved).

    Blank-URL entries are skipped; ``kind`` defaults to ``source``. URL scheme
    validation is the HTTP layer's job — this store persists what it is given.
    """
    user_conn.execute(
        "DELETE FROM files_annotation_links WHERE annotation_id = ?",
        (annotation_id,),
    )
    now = _now()
    position = 0
    for link in links:
        url = (link.get("url") or "").strip()
        if not url:
            continue
        kind = (link.get("kind") or "").strip() or "source"
        label = (link.get("label") or "").strip()
        user_conn.execute(
            "INSERT INTO files_annotation_links "
            "(annotation_id, url, label, kind, position, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (annotation_id, url, label, kind, position, now),
        )
        position += 1
    _touch(user_conn, annotation_id)
    user_conn.commit()
    return list_links(user_conn, annotation_id)


def add_attachment(
    user_conn: sqlite3.Connection,
    annotation_id: int,
    *,
    content_hash: str,
    file_name: str,
    media_type: str,
    size: int,
    stored_root: str | None = None,
    width: int | None = None,
    height: int | None = None,
    caption: str = "",
) -> AnnotationAttachment:
    """Record an attachment row (the bytes are written by the router's byte store)."""
    row = user_conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 AS next "
        "FROM files_annotation_attachments WHERE annotation_id = ?",
        (annotation_id,),
    ).fetchone()
    position = int(row["next"] if row is not None else 0)
    cursor = user_conn.execute(
        "INSERT INTO files_annotation_attachments "
        "(annotation_id, content_hash, file_name, media_type, size, width, height, "
        " caption, is_cover, position, stored_root, added_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
        (
            annotation_id,
            content_hash,
            file_name,
            media_type,
            int(size),
            width,
            height,
            caption.strip(),
            position,
            stored_root,
            _now(),
        ),
    )
    _touch(user_conn, annotation_id)
    user_conn.commit()
    stored = user_conn.execute(
        "SELECT id, content_hash, file_name, media_type, size, width, height, "
        "caption, is_cover, position FROM files_annotation_attachments WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return _attachment_from_row(stored)


def get_attachment(
    user_conn: sqlite3.Connection, attachment_id: int
) -> StoredAttachment | None:
    """Fetch one attachment with its byte-store location (serving and cleanup)."""
    row = user_conn.execute(
        "SELECT id, annotation_id, content_hash, file_name, media_type, size, stored_root "
        "FROM files_annotation_attachments WHERE id = ?",
        (attachment_id,),
    ).fetchone()
    if row is None:
        return None
    return StoredAttachment(
        id=row["id"],
        annotation_id=row["annotation_id"],
        content_hash=row["content_hash"],
        file_name=row["file_name"],
        media_type=row["media_type"],
        size=row["size"],
        stored_root=row["stored_root"],
    )


def list_all_attachments(user_conn: sqlite3.Connection) -> list[StoredAttachment]:
    """Every attachment row with its byte-store location (used by store migration)."""
    rows = user_conn.execute(
        "SELECT id, annotation_id, content_hash, file_name, media_type, size, stored_root "
        "FROM files_annotation_attachments ORDER BY id"
    ).fetchall()
    return [
        StoredAttachment(
            id=row["id"],
            annotation_id=row["annotation_id"],
            content_hash=row["content_hash"],
            file_name=row["file_name"],
            media_type=row["media_type"],
            size=row["size"],
            stored_root=row["stored_root"],
        )
        for row in rows
    ]


def update_attachment_stored_root(
    user_conn: sqlite3.Connection, attachment_id: int, stored_root: str
) -> None:
    """Repoint one attachment at a new store root after its bytes were relocated."""
    user_conn.execute(
        "UPDATE files_annotation_attachments SET stored_root = ? WHERE id = ?",
        (stored_root, attachment_id),
    )
    user_conn.commit()


def remove_attachment(
    user_conn: sqlite3.Connection, annotation_id: int, attachment_id: int
) -> bool:
    """Remove one attachment row (metadata only; the router deletes orphaned bytes)."""
    removed = user_conn.execute(
        "DELETE FROM files_annotation_attachments WHERE id = ? AND annotation_id = ?",
        (attachment_id, annotation_id),
    ).rowcount
    if removed:
        _touch(user_conn, annotation_id)
    user_conn.commit()
    return removed > 0


def attachment_hash_refcount(user_conn: sqlite3.Connection, content_hash: str) -> int:
    """How many attachment rows reference a content hash (byte-dedup safety)."""
    row = user_conn.execute(
        "SELECT COUNT(*) AS count FROM files_annotation_attachments WHERE content_hash = ?",
        (content_hash,),
    ).fetchone()
    return int(row["count"] if row is not None else 0)


def prune_if_empty(user_conn: sqlite3.Connection, annotation_id: int) -> bool:
    """Delete an annotation that has no text, no links, and no attachments.

    Keeps the tile badge truthful after the last piece of a note is removed,
    matching the PUT path's empty-note cleanup.
    """
    row = user_conn.execute(
        "SELECT description, author, extra_info, details, metadata_applied FROM files_annotations WHERE id = ?",
        (annotation_id,),
    ).fetchone()
    if row is None:
        return False
    if (row["description"].strip() or row["author"].strip() or row["extra_info"].strip()
            or json.loads(row["details"]) or row["metadata_applied"]):
        return False
    has_link = user_conn.execute(
        "SELECT 1 FROM files_annotation_links WHERE annotation_id = ? LIMIT 1",
        (annotation_id,),
    ).fetchone()
    has_attachment = user_conn.execute(
        "SELECT 1 FROM files_annotation_attachments WHERE annotation_id = ? LIMIT 1",
        (annotation_id,),
    ).fetchone()
    if has_link or has_attachment:
        return False
    return delete_annotation(user_conn, annotation_id)


def delete_annotation(user_conn: sqlite3.Connection, annotation_id: int) -> bool:
    """Delete an annotation and its children. The only implicit-removal-free path.

    Children are deleted explicitly rather than relying on ``ON DELETE CASCADE``,
    because SQLite enforces foreign keys only when ``PRAGMA foreign_keys`` is on,
    which this module does not assume of the shared connection.
    """
    user_conn.execute(
        "DELETE FROM files_annotation_links WHERE annotation_id = ?", (annotation_id,)
    )
    user_conn.execute(
        "DELETE FROM files_annotation_attachments WHERE annotation_id = ?",
        (annotation_id,),
    )
    removed = user_conn.execute(
        "DELETE FROM files_annotations WHERE id = ?", (annotation_id,)
    ).rowcount
    user_conn.commit()
    return removed > 0


def annotated_keys(
    user_conn: sqlite3.Connection,
) -> tuple[set[str], set[tuple[str, str]]]:
    """Return (annotated content hashes, annotated folder ``(source_id, path)``).

    A cheap lookup so the browse grid can badge entries that carry a note without
    a query per tile.
    """
    # A cleared auto-filled note retains only a suppression marker. It must not
    # acquire a visible badge or appear in Copy from's annotated-subject picker.
    visible = """(metadata_applied = 0 OR trim(description) != '' OR trim(author) != '' OR trim(extra_info) != ''
        OR details != '[]'
        OR EXISTS (SELECT 1 FROM files_annotation_links WHERE annotation_id = files_annotations.id)
        OR EXISTS (SELECT 1 FROM files_annotation_attachments WHERE annotation_id = files_annotations.id))"""
    hashes = {
        row["content_hash"]
        for row in user_conn.execute(
            "SELECT content_hash FROM files_annotations WHERE content_hash IS NOT NULL AND " + visible
        )
    }
    folders = {
        (row["source_id"], row["relative_path"])
        for row in user_conn.execute(
            "SELECT source_id, relative_path FROM files_annotations "
            "WHERE content_hash IS NULL AND " + visible
        )
    }
    return hashes, folders


def import_catalog_records(connection: sqlite3.Connection) -> int:
    """Explicit files.annotations/version 1 import, without file access.

    The complete essential snapshot retains authored text, settings, ordering,
    suppression markers and byte-store roots in their existing owner tables.
    This bridge adds opaque record/asset identities and immutable identity
    anchors only. File annotations keep their existing shared-by-hash behavior;
    attachment hashes describe recorded facts, not newly verified local bytes.
    """
    from uuid import NAMESPACE_URL, uuid4, uuid5
    from services import item_catalog

    if not connection.in_transaction:
        raise ValueError('Import Files records inside a caller transaction')
    required = {
        'files_annotations': {'id', 'subject_kind', 'content_hash', 'source_id', 'relative_path'},
        'files_annotation_links': {'id', 'annotation_id'},
        'files_annotation_attachments': {'id', 'annotation_id', 'content_hash', 'media_type', 'size'},
    }
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    savepoint = 'files_records_' + uuid4().hex
    connection.execute('SAVEPOINT ' + savepoint)
    try:
        present = {row['name'] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tables = present & required.keys()
        if tables and 'files_annotations' not in tables:
            raise ValueError('Files annotation owner table is missing')
        for table in tables:
            if not required[table] <= {row['name'] for row in cursor.execute(f'PRAGMA table_info({table})')}:
                raise ValueError('Unsupported Files annotation schema')
            if cursor.execute(f'SELECT id FROM {table} GROUP BY id HAVING count(*)>1 LIMIT 1').fetchone():
                raise ValueError('Duplicate Files record identity')
        for table in tables - {'files_annotations'}:
            if cursor.execute(f'SELECT 1 FROM {table} c LEFT JOIN files_annotations a '
                              'ON a.id=c.annotation_id WHERE a.id IS NULL LIMIT 1').fetchone():
                raise ValueError('Files record has an unknown annotation')

        def legacy_id(value):
            if type(value) is not int or not 0 < value <= 2**63 - 1:
                raise ValueError('Invalid Files legacy record identity')
            return str(value)

        def item_id(value):
            return str(uuid5(NAMESPACE_URL, 'urn:keivotos:files:annotation:' + legacy_id(value)))

        count = 0
        if 'files_annotations' in tables:
            for row in cursor.execute('SELECT id,subject_kind,content_hash,source_id,relative_path '
                                      'FROM files_annotations ORDER BY id'):
                key = legacy_id(row['id'])
                kind, digest = row['subject_kind'], row['content_hash']
                if (kind not in ('file', 'dir')
                        or kind == 'file' and (not isinstance(digest, str) or not digest or '\0' in digest)
                        or kind == 'dir' and digest is not None
                        or any(not isinstance(row[name], str) or '\0' in row[name]
                               for name in ('source_id', 'relative_path'))):
                    raise ValueError('Invalid Files annotation identity')
                identity = item_id(row['id'])
                item_catalog.ensure_item(connection, namespace='files.annotation', legacy_key=key,
                    owner='files', item_type='annotation', item_id=identity)
                anchor = dict(legacy_id=row['id'], subject_kind=kind, content_hash=digest)
                if kind == 'dir':
                    anchor.update(source_id=row['source_id'], relative_path=row['relative_path'])
                item_catalog.record_capture(connection, item_id=identity, namespace='files.annotation',
                    capture_key='identity', format_version=1, payload=anchor)
                count += 1
        if 'files_annotation_attachments' in tables:
            for row in cursor.execute('SELECT id,annotation_id,content_hash,media_type,size '
                                      'FROM files_annotation_attachments ORDER BY id'):
                key = legacy_id(row['id'])
                identity = item_id(row['annotation_id'])
                asset_id = str(uuid5(NAMESPACE_URL, 'urn:keivotos:files:attachment:' + key))
                item_catalog.record_asset(connection, asset_id=asset_id, media_type=row['media_type'],
                    byte_size=row['size'], hash_algorithm='md5', content_hash=row['content_hash'])
                # A stable slot survives display reordering. The existing owner
                # position/id ordering remains authoritative for presentation.
                item_catalog.link_asset(connection, item_id=identity, asset_id=asset_id,
                    role='attachment:' + key)
                item_catalog.record_capture(connection, item_id=identity, namespace='files.attachment',
                    capture_key=key, format_version=1,
                    payload=dict(legacy_id=row['id'], annotation_id=row['annotation_id'], asset_id=asset_id))
        connection.execute('RELEASE ' + savepoint)
        return count
    except BaseException:
        connection.execute('ROLLBACK TO ' + savepoint)
        connection.execute('RELEASE ' + savepoint)
        raise
    finally:
        cursor.close()

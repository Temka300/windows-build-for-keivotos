"""Isolated file-index engine for the Files base (V1.1.0).

Responsibilities:
- open the disposable ``files.sqlite`` index (WAL);
- scan a source folder into the index using only cheap ``stat`` facts — no file
  contents are read and no content hash is computed here (hashing is lazy and
  lives in a later slice);
- answer directory-listing and filename-search queries.

Design constraints (SUITE_MODULE_CONTRACT.md):
- No import of Danbooru modules or ``core``; this engine is fully standalone.
- Files that disappear are marked ``available = 0`` (missing != deleted), never
  removed, so user data referencing them survives a move or an unplugged drive.
- The engine authors no metadata; it records what the disk says and nothing more.
"""
from __future__ import annotations

from services.operation_diagnostics import observed
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Iterable

from files_base.schema import ensure_files_schema
from services.search_text import literal_contains


# Keivotos-generated directories that live inside a user's folder but must never
# be indexed as browsable content (currently the attachment byte store).
EXCLUDED_DIR_NAMES = {".keivotos"}
_source_work_lock = threading.RLock()


@contextmanager
def source_work(*, wait: bool = True):
    """Serialize source snapshots, scans and path changes in this process."""
    if not _source_work_lock.acquire(blocking=wait):
        raise RuntimeError('Wait for folder work to finish before editing folders')
    try:
        yield
    finally:
        _source_work_lock.release()


@dataclass(frozen=True)
class FileEntry:
    """One indexed filesystem entry (a file or a directory)."""

    source_id: str
    path: str
    relative_path: str
    parent: str
    name: str
    ext: str | None
    is_dir: bool
    size: int | None
    mtime: int | None
    content_hash: str | None
    available: bool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_entry(row: sqlite3.Row) -> FileEntry:
    return FileEntry(
        source_id=row["source_id"],
        path=row["path"],
        relative_path=row["relative_path"],
        parent=row["parent"],
        name=row["name"],
        ext=row["ext"],
        is_dir=bool(row["is_dir"]),
        size=row["size"],
        mtime=row["mtime"],
        content_hash=row["content_hash"],
        available=bool(row["available"]),
    )


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards so user input is matched literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@contextmanager
def open_index(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    """Open the file index at ``db_path``, ensuring its schema and WAL mode."""
    from database_connections import _database_access_gate
    from services.catalog_index import is_combined
    with _database_access_gate.read():
        from database_connections import require_storage_ready
        require_storage_ready(db_path)
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            if is_combined(connection) and not connection.execute(
                    "SELECT 1 FROM kv_index_owners WHERE owner='files'").fetchone():
                raise ValueError("Files is not registered in this combined index")
            ensure_files_schema(connection)
            yield connection
        finally:
            connection.close()


@observed('suite', 'files.scan', library='source_id')
def scan_source(
    connection: sqlite3.Connection,
    source_id: str,
    root: Path,
    *,
    excluded_roots: Iterable[Path] = (),
    commit: bool = True,
    cancelled=None,
) -> dict[str, int]:
    """Index every file and directory beneath ``root`` for ``source_id``.

    Records only cheap ``stat`` facts. Entries not seen during this scan are
    marked unavailable rather than deleted. Registered descendant roots are
    indexed by their own source; their directory entry remains visible in the
    parent, but this scan never descends into them. Returns a small counts
    summary.
    """
    root = Path(root).expanduser().resolve(strict=False)
    excluded = {
        os.path.normcase(str(Path(path).expanduser().resolve(strict=False)))
        for path in excluded_roots
        if Path(path).expanduser().resolve(strict=False) != root
    }
    # Reset availability inside the scan transaction, then mark observed rows.
    connection.execute('DELETE FROM files_audio_metadata WHERE source_id=?', (source_id,))
    # Wall clocks can repeat or move backwards, so timestamps cannot identify
    # which rows this scan visited. Keep their last-seen timestamps intact.
    connection.execute(
        "UPDATE files_index SET available = 0 WHERE source_id = ?", (source_id,)
    )
    scan_token = _now()
    added = updated = directories = files = 0

    def upsert(path: Path, is_dir: bool) -> None:
        nonlocal added, updated, directories, files
        if cancelled is not None and cancelled():
            raise InterruptedError('Index discovery cancelled')
        try:
            stat_result = path.stat()
        except OSError:
            from services.operation_diagnostics import count
            count("skipped", reason="unreadable_stat")
            return
        relative = path.relative_to(root).as_posix()
        parent = os.path.dirname(relative)
        name = path.name
        ext = "" if is_dir else path.suffix.lower().lstrip(".")
        size = None if is_dir else int(stat_result.st_size)
        mtime = int(stat_result.st_mtime)
        existing = connection.execute(
            "SELECT id FROM files_index WHERE path = ?", (str(path),)
        ).fetchone()
        if existing is None:
            connection.execute(
                """INSERT INTO files_index
                       (source_id, path, relative_path, parent, name, ext, is_dir,
                        size, mtime, indexed_at, seen_at, available)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (source_id, str(path), relative, parent, name, ext, int(is_dir),
                 size, mtime, scan_token, scan_token),
            )
            added += 1
        else:
            connection.execute(
                """UPDATE files_index
                       SET source_id = ?, relative_path = ?, parent = ?, name = ?,
                           ext = ?, is_dir = ?, size = ?, mtime = ?, seen_at = ?,
                           available = 1
                     WHERE id = ?""",
                (source_id, relative, parent, name, ext, int(is_dir), size, mtime,
                 scan_token, existing["id"]),
            )
            updated += 1
        if is_dir:
            directories += 1
        else:
            files += 1

    if root.is_dir():
        for current_dir, dir_names, file_names in os.walk(root):
            current = Path(current_dir)
            descend_into: list[str] = []
            for dir_name in sorted(dir_names):
                if dir_name in EXCLUDED_DIR_NAMES:
                    continue  # Keivotos-generated storage; never index or descend.
                directory = current / dir_name
                upsert(directory, is_dir=True)
                resolved = os.path.normcase(
                    str(directory.expanduser().resolve(strict=False))
                )
                if resolved not in excluded:
                    descend_into.append(dir_name)
            dir_names[:] = descend_into
            for file_name in sorted(file_names):
                upsert(current / file_name, is_dir=False)

    unavailable = connection.execute(
        "SELECT COUNT(*) FROM files_index WHERE source_id = ? AND available = 0",
        (source_id,),
    ).fetchone()[0]
    if commit:
        connection.commit()
    return {
        "added": added,
        "updated": updated,
        "files": files,
        "directories": directories,
        "unavailable": max(unavailable, 0),
    }


def source_is_indexed(connection: sqlite3.Connection, source_id: str) -> bool:
    """Whether this source has ever been scanned into the index."""
    row = connection.execute(
        "SELECT 1 FROM files_index WHERE source_id = ? LIMIT 1", (source_id,)
    ).fetchone()
    return row is not None


def source_entry_count(connection: sqlite3.Connection, source_id: str) -> int:
    row = connection.execute(
        "SELECT COUNT(*) AS count FROM files_index WHERE source_id=? AND is_dir=0",
        (source_id,),
    ).fetchone()
    return int(row["count"] if row is not None else 0)


def source_size_totals(
    connection: sqlite3.Connection, source_id: str, descendant_roots: Iterable[str] = (),
) -> tuple[int, int, int]:
    """Logical indexed file bytes/count/unknown sizes for one registered source.

    Descendant registered roots own their indexed files even if older parent
    scans retained rows for the same paths. This reads SQLite facts only.
    """
    clauses = ["source_id=?", "is_dir=0", "available=1"]
    parameters: list[object] = [source_id]
    for relative_root in descendant_roots:
        clauses.append("relative_path NOT LIKE ? ESCAPE '\\'")
        parameters.append(_escape_like(relative_root.rstrip("/")) + "/%")
    row = connection.execute(
        "SELECT COUNT(*) AS files, "
        "COALESCE(SUM(CASE WHEN size IS NOT NULL AND size>=0 THEN size ELSE 0 END),0) AS bytes, "
        "COALESCE(SUM(CASE WHEN size IS NULL OR size<0 THEN 1 ELSE 0 END),0) AS unknown "
        "FROM files_index WHERE " + " AND ".join(clauses), parameters,
    ).fetchone()
    return int(row["bytes"]), int(row["files"]), int(row["unknown"])


def refresh_file(connection: sqlite3.Connection, source_id: str, root: Path,
                 name: str, previous_name: str | None = None) -> None:
    """Refresh one root-level file after an owner mutation, without scanning media."""
    root = Path(root).expanduser().resolve(strict=False)
    connection.execute('DELETE FROM files_audio_metadata WHERE source_id=? AND relative_path IN (?,?)',
                       (source_id, name, previous_name or name))
    if previous_name and previous_name != name:
        connection.execute(
            "UPDATE files_index SET available=0 WHERE source_id=? AND path=?",
            (source_id, str(root / previous_name)),
        )
    path = root / name
    if not path.is_file() or path.is_symlink():
        connection.execute(
            "UPDATE files_index SET available=0 WHERE source_id=? AND path=?",
            (source_id, str(path)),
        )
    else:
        stat_result = path.stat()
        timestamp = _now()
        connection.execute(
            """INSERT INTO files_index
               (source_id, path, relative_path, parent, name, ext, is_dir,
                size, mtime, indexed_at, seen_at, available)
               VALUES (?, ?, ?, '', ?, ?, 0, ?, ?, ?, ?, 1)
               ON CONFLICT(path) DO UPDATE SET
                source_id=excluded.source_id, relative_path=excluded.relative_path,
                parent='', name=excluded.name, ext=excluded.ext, is_dir=0,
                size=excluded.size, mtime=excluded.mtime,
                content_hash=NULL, hashed_at=NULL,
                seen_at=excluded.seen_at, available=1""",
            (source_id, str(path), name, name, path.suffix.lower().lstrip('.'),
             int(stat_result.st_size), int(stat_result.st_mtime), timestamp, timestamp),
        )
    connection.commit()


def drop_source(connection: sqlite3.Connection, source_id: str) -> int:
    """Remove all index rows for a source (un-index only; disk is untouched)."""
    connection.execute('DELETE FROM files_audio_metadata WHERE source_id=?', (source_id,))
    removed = connection.execute(
        "DELETE FROM files_index WHERE source_id = ?", (source_id,)
    ).rowcount
    connection.commit()
    return max(removed, 0)


def list_directory(
    connection: sqlite3.Connection,
    source_id: str,
    parent: str = "",
    include_unavailable: bool = False,
) -> list[FileEntry]:
    """List the direct children of ``parent`` within ``source_id``.

    Directories are returned before files; both are name-sorted case-insensitively.
    """
    clauses = ["source_id = ?", "parent = ?"]
    params: list[object] = [source_id, parent]
    if not include_unavailable:
        clauses.append("available = 1")
    query = (
        "SELECT * FROM files_index WHERE "
        + " AND ".join(clauses)
        + " ORDER BY is_dir DESC, name COLLATE NOCASE ASC"
    )
    return [_row_to_entry(row) for row in connection.execute(query, params)]


def source_search_scope(connection: sqlite3.Connection, registered, *, source_ids=None,
                        forbidden_paths=()) -> tuple[str, list[object]]:
    """Fence current owners before filtering/counting/paging retained index rows.

    Source resolution and child ownership use the same boundaries as serving.
    This only filters a read: offline/hidden/previous-owner records stay intact.
    """
    from files_base import sources, serving

    registered = list(registered)
    forbidden_paths = tuple(forbidden_paths)
    selected = set(source_ids) if source_ids is not None else None
    connection.create_function('keivotos_path_key', 1, os.path.normcase, deterministic=True)
    choices = []
    parameters: list[object] = []
    for source in registered:
        if not source.visible or selected is not None and source.source_id not in selected:
            continue
        try:
            root = serving.resolve_within_source(source.path, '', list(forbidden_paths))
            if not root.is_dir():
                continue
            excluded = [Path(child.path) for child in sources.descendant_sources(source, registered)]
            excluded.extend(forbidden_paths)
            clauses = ['source_id=?', 'instr(keivotos_path_key(path),?)=1']
            values: list[object] = [source.source_id, os.path.normcase(str(root) + os.sep)]
            for child in excluded:
                resolved = Path(child).expanduser().resolve(strict=False)
                if not resolved.is_relative_to(root) or resolved == root:
                    continue
                relative = resolved.relative_to(root).as_posix()
                clauses.append('(keivotos_path_key(relative_path)!=? AND instr(keivotos_path_key(relative_path),?)!=1)')
                values.extend([os.path.normcase(relative), os.path.normcase(relative + '/')])
            choices.append('(' + ' AND '.join(clauses) + ')')
            parameters.extend(values)
        except (OSError, ValueError, RuntimeError, serving.ServeDenied):
            continue
    return '(' + ' OR '.join(choices) + ')' if choices else '0', parameters


def search_by_name(
    connection: sqlite3.Connection,
    term: str,
    source_id: str | None = None,
    source_ids: Iterable[str] | None = None,
    limit: int = 200,
    include_unavailable: bool = False,
    *,
    offset: int = 0,
    source_scope: tuple[str, list[object]] | None = None,
) -> list[FileEntry]:
    """Find literal Unicode case-insensitive name matches in a stable page."""
    predicate, folded = literal_contains(connection, 'name', term)
    clauses = [predicate]
    params: list[object] = [folded]
    if source_scope is not None:
        clauses.append(source_scope[0])
        params.extend(source_scope[1])
    if source_id is not None and source_ids is not None:
        raise ValueError("Pass source_id or source_ids, not both")
    if source_id is not None:
        clauses.append("source_id = ?")
        params.append(source_id)
    elif source_ids is not None:
        ids = list(dict.fromkeys(source_ids))
        if not ids:
            return []
        clauses.append(f"source_id IN ({','.join('?' for _ in ids)})")
        params.extend(ids)
    if not include_unavailable:
        clauses.append("available = 1")
    query = (
        "SELECT * FROM files_index WHERE "
        + " AND ".join(clauses)
        + " ORDER BY is_dir DESC, name COLLATE NOCASE ASC, relative_path, source_id LIMIT ? OFFSET ?"
    )
    params.extend([int(limit), int(offset)])
    return [_row_to_entry(row) for row in connection.execute(query, params)]


def rebuild_index(build):
    """Explicit candidate discovery; never runs from index open or startup."""
    from files_base import sources, serving
    connection = build.connection
    connection.execute('UPDATE files_index SET available=0')
    connection.commit()
    for source in build.sources:
        build.check()
        if not source.visible:
            continue
        forbidden = [build.suite_home, *(Path(child.path) for child in
                     sources.descendant_sources(source, build.sources))]
        try:
            root = serving.resolve_within_source(source.path, '', [build.suite_home])
            if not root.is_dir():
                continue
            scan_source(connection, source.source_id, root, excluded_roots=forbidden,
                        cancelled=build.cancelled)
        except InterruptedError:
            raise
        except (OSError, serving.ServeDenied):
            # An offline/hidden source does not erase historical references.
            continue

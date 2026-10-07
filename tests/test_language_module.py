"""Scratch-only Language note tests; no live library is read or modified."""
from __future__ import annotations

from contextlib import contextmanager
import errno
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from files_base import index, sources
from module_registry import build_registry
from modules import language
from modules.language import documents, router


@pytest.fixture
def local(tmp_path, monkeypatch):
    media = tmp_path / "notes"
    media.mkdir()
    user_db = tmp_path / "user.sqlite"

    @contextmanager
    def connect():
        with sqlite3.connect(user_db) as connection:
            connection.row_factory = sqlite3.Row
            yield connection

    with connect() as connection:
        sources.ensure_sources_schema(connection)
        source = sources.register_source(connection, media, role="language")
    home = tmp_path / "suite"
    db = tmp_path / "files.sqlite"
    monkeypatch.setattr(router, "get_user_db", connect)
    monkeypatch.setattr(router, "_enabled", lambda: None)
    monkeypatch.setattr(router.config, "SUITE_HOME", home)
    monkeypatch.setattr(router.config, "FILES_DB_PATH", db)
    monkeypatch.setattr(router.config, "MODULE_REGISTRY", build_registry(home, "test"))
    return source, media, home, db, connect


def test_descriptor_folder_role_and_history_owner(local):
    source, media, home, db, connect = local
    descriptor = router.config.MODULE_REGISTRY.require("language")
    assert descriptor.experimental and descriptor.disableable and descriptor.adopt_hook
    assert descriptor.backup_components()[0].source == home / "modules/language/revisions"
    with connect() as connection:
        sources.update_source(connection, source.source_id, role="files")
    import database
    original = database.get_user_db
    database.get_user_db = connect
    try:
        language.adopt(source.source_id)
        assert router.folders() == [{"source_id": source.source_id, "name": "notes"}]
        language.release(source.source_id)
        assert list(media.iterdir()) == []
    finally:
        database.get_user_db = original


def test_create_collision_rename_save_history_and_index(local, monkeypatch):
    source, media, home, db, connect = local
    monkeypatch.setattr(index, "scan_source", lambda *args, **kwargs: pytest.fail("whole-folder scan"))
    first = router.create_document(router.CreateRequest(source_id=source.source_id))
    second = router.create_document(router.CreateRequest(source_id=source.source_id))
    assert [first["name"], second["name"]] == ["New File.md", "New File (2).md"]
    with pytest.raises(HTTPException) as collision:
        router.rename_document(router.RenameRequest(source_id=source.source_id,
            name=first["name"], new_name=second["name"], revision=first["revision"]))
    assert collision.value.status_code == 409
    renamed = router.rename_document(router.RenameRequest(source_id=source.source_id,
        name=first["name"], new_name="My note.md", revision=first["revision"]))
    assert not (media / first["name"]).exists()
    assert (media / renamed["name"]).read_bytes() == b""
    session = "a" * 32
    saved = router.save_document(router.SaveRequest(source_id=source.source_id,
        name=renamed["name"], content="안녕 **world**", revision=renamed["revision"],
        session_id=session))
    later = router.save_document(router.SaveRequest(source_id=source.source_id,
        name=renamed["name"], content="Second edit", revision=saved["revision"],
        session_id=session))
    assert (media / "My note.md").read_text(encoding="utf-8") == "Second edit"
    history = list((home / "modules/language/revisions").rglob("*.md"))
    assert len(history) == 1 and history[0].read_bytes() == b""
    with index.open_index(db) as connection:
        rows = connection.execute("SELECT name, available, size FROM files_index WHERE source_id=?",
                                  (source.source_id,)).fetchall()
    assert any(row["name"] == "My note.md" and row["available"]
               and row["size"] == len(b"Second edit") for row in rows)
    assert any(row["name"] == "New File.md" and not row["available"] for row in rows)
    assert later["revision"] != saved["revision"]


def test_external_edit_conflict_preserves_both_versions(local):
    source, media, home, db, connect = local
    created = router.create_document(router.CreateRequest(source_id=source.source_id))
    (media / created["name"]).write_text("external edit", encoding="utf-8")
    with pytest.raises(HTTPException) as conflict:
        router.save_document(router.SaveRequest(source_id=source.source_id,
            name=created["name"], content="my edit", revision=created["revision"],
            session_id="b" * 32))
    assert conflict.value.status_code == 409
    assert (media / created["name"]).read_text() == "external edit"
    assert not list((home / "modules/language").rglob("*.md"))


def test_revision_history_link_cannot_escape_suite_home(local, tmp_path):
    source, media, home, db, connect = local
    created = router.create_document(router.CreateRequest(source_id=source.source_id))
    outside = tmp_path / "outside"
    outside.mkdir()
    history_home = home / "modules/language"
    history_home.mkdir(parents=True)
    (history_home / "revisions").symlink_to(outside, target_is_directory=True)
    with pytest.raises(HTTPException) as error:
        router.save_document(router.SaveRequest(source_id=source.source_id,
            name=created["name"], content="local edit", revision=created["revision"],
            session_id="c" * 32))
    assert error.value.status_code == 403
    assert (media / created["name"]).read_bytes() == b""
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("name", ["../escape.md", "a/b.md", "C:\\note.md", "CON.md",
                                   "bad.txt", " space.md", "trailing .md "])
def test_invalid_names_are_rejected(local, name):
    source, media, home, db, connect = local
    with pytest.raises(documents.DocumentError) as error:
        documents.valid_name(name)
    assert error.value.status_code == 400


def test_symlink_non_utf8_and_large_notes_are_not_edited(local, tmp_path):
    source, media, home, db, connect = local
    outside = tmp_path / "outside.md"
    outside.write_text("preserve")
    (media / "linked.md").symlink_to(outside)
    (media / "binary.md").write_bytes(b"\xff")
    (media / "large.md").write_bytes(b"x" * (documents.MAX_BYTES + 1))
    assert [row["name"] for row in router.list_documents(source.source_id)["files"]] == ["binary.md", "large.md"]
    for name, status in (("linked.md", 403), ("binary.md", 422), ("large.md", 413)):
        with pytest.raises(HTTPException) as error:
            router.read_document(source.source_id, name)
        assert error.value.status_code == status
    assert outside.read_text() == "preserve"


def test_role_change_blocks_operations_without_removing_notes(local):
    source, media, home, db, connect = local
    created = router.create_document(router.CreateRequest(source_id=source.source_id))
    with connect() as connection:
        sources.update_source(connection, source.source_id, role="files")
    with pytest.raises(HTTPException) as error:
        router.read_document(source.source_id, created["name"])
    assert error.value.status_code == 404
    assert (media / created["name"]).exists()


def test_interrupted_revision_write_cannot_authorize_unpreserved_retry(local, monkeypatch):
    source, media, home, db, connect = local
    note = media / "Original.md"
    previous = b"# Original\nEvery byte must survive.\n"
    note.write_bytes(previous)
    opened = router.read_document(source.source_id, note.name)
    history_home = home / "modules/language"
    original_open = Path.open
    original_temporary = documents.tempfile.NamedTemporaryFile

    class PartialWriter:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def write(self, data):
            self.stream.write(data[:5])
            self.stream.flush()
            raise OSError(errno.ENOSPC, "injected incomplete revision write")

    def interrupted_open(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return PartialWriter(stream) if mode == "xb" and path.is_relative_to(history_home) else stream

    def interrupted_temporary(*args, **kwargs):
        stream = original_temporary(*args, **kwargs)
        return PartialWriter(stream) if Path(kwargs["dir"]).is_relative_to(history_home) else stream

    payload = router.SaveRequest(source_id=source.source_id, name=note.name,
        content="First edit", revision=opened["revision"], session_id="d" * 32)
    with monkeypatch.context() as fault:
        fault.setattr(Path, "open", interrupted_open)
        fault.setattr(documents.tempfile, "NamedTemporaryFile", interrupted_temporary)
        with pytest.raises(HTTPException) as failure:
            router.save_document(payload)
    assert failure.value.status_code == 422
    assert note.read_bytes() == previous
    # No final revision can signal successful preservation while incomplete.
    assert not list(history_home.rglob("*.md"))
    saved = router.save_document(payload)
    assert saved["content"] == note.read_text() == "First edit"
    histories = list(history_home.rglob("*.md"))
    assert len(histories) == 1 and histories[0].read_bytes() == previous
    assert not list(history_home.rglob("*.tmp"))


def test_process_exit_during_revision_write_keeps_original_and_safe_retry(local):
    source, media, home, db, connect = local
    note = media / "Interrupted.md"
    previous = b"# Original before process exit\n"
    note.write_bytes(previous)
    opened = router.read_document(source.source_id, note.name)
    history_home = home / "modules/language"
    code = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from files_base.sources import Source
from modules.language import documents
source = Source(sys.argv[6], sys.argv[2], 'notes', 'language', True, None)
def stop_before_revision_publication(fd):
    os._exit(73)
documents.os.fsync = stop_before_revision_publication
documents.save_document(source, Path(sys.argv[3]), Path(sys.argv[4]), 'Interrupted.md',
                        'After restart', sys.argv[5], 'e' * 32)
"""
    result = subprocess.run([sys.executable, "-c", code,
        str(Path(__file__).resolve().parents[1] / "backend"), str(media), str(home),
        str(history_home), opened["revision"], source.source_id], capture_output=True, timeout=20)
    assert result.returncode == 73, result.stderr.decode()
    assert note.read_bytes() == previous
    assert not list(history_home.rglob("*.md"))
    # The dead process may leave a scratch candidate; a new save never trusts it.
    saved = documents.save_document(source, home, history_home, note.name,
        "After restart", opened["revision"], "e" * 32)
    assert saved["content"] == "After restart"
    histories = list(history_home.rglob("*.md"))
    assert len(histories) == 1 and histories[0].read_bytes() == previous


def test_legacy_partial_history_is_preserved_but_never_trusted(local):
    source, media, home, db, connect = local
    note = media / "Legacy.md"
    previous = b"Complete original before a legacy failed write"
    note.write_bytes(previous)
    opened = router.read_document(source.source_id, note.name)
    history_home = home / "modules/language"
    identity = hashlib.sha256(f"{source.source_id}\0{note.name}".encode()).hexdigest()
    legacy = history_home / "revisions" / identity / f"{'a' * 32}.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(previous[:5])
    payload = router.SaveRequest(source_id=source.source_id, name=note.name,
        content="First new save", revision=opened["revision"], session_id="a" * 32)
    saved = router.save_document(payload)
    assert legacy.read_bytes() == previous[:5]
    current = legacy.parent / "complete" / legacy.name
    assert current.read_bytes() == previous
    router.save_document(payload.model_copy(update={"content": "Second new save", "revision": saved["revision"]}))
    assert current.read_bytes() == previous
    assert legacy.read_bytes() == previous[:5]
    assert note.read_text() == "Second new save"


def test_revision_flush_failure_blocks_note_replacement_and_retry_is_safe(local, monkeypatch):
    source, media, home, db, connect = local
    note = media / "Flush.md"
    previous = b"Original before failed flush"
    note.write_bytes(previous)
    opened = router.read_document(source.source_id, note.name)
    history_home = home / "modules/language"
    payload = router.SaveRequest(source_id=source.source_id, name=note.name,
        content="After flush recovery", revision=opened["revision"], session_id="f" * 32)
    def failed_flush(fd):
        raise OSError(errno.EIO, "injected revision flush failure")
    with monkeypatch.context() as fault:
        fault.setattr(documents.os, "fsync", failed_flush)
        with pytest.raises(HTTPException) as failure:
            router.save_document(payload)
    assert failure.value.status_code == 422
    assert note.read_bytes() == previous
    assert not list(history_home.rglob("*.md"))
    router.save_document(payload)
    histories = list(history_home.rglob("*.md"))
    assert len(histories) == 1 and histories[0].read_bytes() == previous


def test_revision_publication_failure_keeps_original_without_unsafe_fallback(local, monkeypatch):
    source, media, home, db, connect = local
    note = media / "Publish.md"
    previous = b"Original before unavailable atomic publication"
    note.write_bytes(previous)
    opened = router.read_document(source.source_id, note.name)
    history_home = home / "modules/language"
    def denied(*args):
        raise OSError(errno.EPERM, "injected unavailable atomic publication")
    with monkeypatch.context() as fault:
        fault.setattr(Path, "rename", denied)
        fault.setattr(documents.os, "link", denied)
        with pytest.raises(HTTPException) as failure:
            router.save_document(router.SaveRequest(source_id=source.source_id,
                name=note.name, content="Attempted edit", revision=opened["revision"], session_id="b" * 32))
    assert failure.value.status_code == 422
    assert note.read_bytes() == previous
    assert not list(history_home.rglob("*.md"))
    assert not list(history_home.rglob("*.tmp"))


@pytest.mark.parametrize("kind", ["linked-file", "directory", "linked-directory"])
def test_invalid_complete_revision_targets_preserve_every_file(local, tmp_path, kind):
    source, media, home, db, connect = local
    note = media / "Blocked.md"
    previous = b"Original note that must remain"
    note.write_bytes(previous)
    opened = router.read_document(source.source_id, note.name)
    history_home = home / "modules/language"
    identity = hashlib.sha256(f"{source.source_id}\0{note.name}".encode()).hexdigest()
    history = history_home / "revisions" / identity / "complete" / f"{'c' * 32}.md"
    outside = tmp_path / "unrelated"
    outside.mkdir()
    preserved = outside / "preserved.md"
    preserved.write_bytes(b"Unrelated preserved contents")
    if kind == "linked-directory":
        history.parent.parent.mkdir(parents=True)
        history.parent.symlink_to(outside, target_is_directory=True)
    else:
        history.parent.mkdir(parents=True)
        if kind == "linked-file":
            history.symlink_to(preserved)
        else:
            history.mkdir()
    with pytest.raises(HTTPException) as failure:
        router.save_document(router.SaveRequest(source_id=source.source_id,
            name=note.name, content="Attempted edit", revision=opened["revision"], session_id="c" * 32))
    assert failure.value.status_code == 403
    assert note.read_bytes() == previous
    assert preserved.read_bytes() == b"Unrelated preserved contents"
    assert list(outside.iterdir()) == [preserved]


def test_concurrent_complete_revision_publication_never_overwrites_winner(local, monkeypatch):
    source, media, home, db, connect = local
    note = media / "Race.md"
    previous = b"Current note bytes"
    note.write_bytes(previous)
    opened = router.read_document(source.source_id, note.name)
    history_home = home / "modules/language"
    original_link, original_rename = documents.os.link, Path.rename
    winner = b"First completed session snapshot"
    def competing_link(candidate, target):
        target.write_bytes(winner)
        return original_link(candidate, target)
    def competing_rename(candidate, target):
        target.write_bytes(winner)
        return original_rename(candidate, target)
    with monkeypatch.context() as race:
        race.setattr(documents.os, "link", competing_link)
        race.setattr(Path, "rename", competing_rename)
        saved = router.save_document(router.SaveRequest(source_id=source.source_id,
            name=note.name, content="Next edit", revision=opened["revision"], session_id="d" * 32))
    histories = list(history_home.rglob("*.md"))
    assert len(histories) == 1 and histories[0].read_bytes() == winner
    assert saved["content"] == note.read_text() == "Next edit"
    assert not list(history_home.rglob("*.tmp"))


# Exercise the real ASGI response, without adding a TestClient dependency.
def language_http(monkeypatch, method, path, payload):
    import asyncio
    import json
    from fastapi import FastAPI
    monkeypatch.setattr(router, "require_enabled", lambda slug: None)
    app = FastAPI()
    app.include_router(router.router)

    async def request():
        messages = []
        async def receive():
            return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}
        async def send(message):
            messages.append(message)
        try:
            await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                       "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
                       "root_path": "", "query_string": b"", "headers": [(b"content-type", b"application/json")],
                       "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80)}, receive, send)
        except Exception:
            if not any(m.get("status") == 500 for m in messages):
                raise
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        return status, json.loads(body) if status != 500 else body.decode()
    return asyncio.run(request())


@pytest.mark.parametrize("operation", ["save", "create", "rename"])
def test_committed_mutation_acknowledged_under_real_index_lock(local, monkeypatch, operation):
    source, media, home, db, connect = local
    original = b"# Original before index contention\n"
    (media / "Original.md").write_bytes(original)
    opened = router.read_document(source.source_id, "Original.md")
    documents.refresh_index(source, db, "Original.md")
    payload = {"source_id": source.source_id}
    if operation == "save":
        method, path = "PUT", "/api/language/document"
        payload.update(name="Original.md", content="Saved during index contention", revision=opened["revision"], session_id="f" * 32)
    elif operation == "rename":
        method, path = "POST", "/api/language/document/rename"
        payload.update(name="Original.md", new_name="Renamed.md", revision=opened["revision"])
    else:
        method, path = "POST", "/api/language/documents"
    lock = sqlite3.connect(db)
    lock.execute("BEGIN IMMEDIATE")
    try:
        status, result = language_http(monkeypatch, method, path, payload)
    finally:
        lock.rollback()
        lock.close()
    assert status == 200, result
    assert result["index_refresh"]["status"] == "failed"
    assert (media / result["name"]).read_text() == result["content"]
    assert hashlib.sha256((media / result["name"]).read_bytes()).hexdigest() == result["revision"]
    if operation == "save":
        assert [p.read_bytes() for p in (home / "modules/language/revisions").rglob("*.md")] == [original]
    repaired_status, repaired = language_http(monkeypatch, "POST", "/api/language/document/index", {
        "source_id": source.source_id, "name": result["name"],
        "previous_names": ["Original.md"] if operation == "rename" else []})
    assert repaired_status == 200 and repaired["status"] == "complete"
    with index.open_index(db) as connection:
        row = connection.execute("SELECT size,available FROM files_index WHERE source_id=? AND name=?", (source.source_id, result["name"])).fetchone()
        assert row["available"] and row["size"] == len(result["content"].encode())
        if operation == "rename":
            assert not connection.execute("SELECT available FROM files_index WHERE name='Original.md'").fetchone()[0]
    second = router.save_document(router.SaveRequest(source_id=source.source_id, name=result["name"],
        content="Another edit after repair", revision=result["revision"], session_id="f" * 32))
    assert second["content"] == (media / result["name"]).read_text()


@pytest.mark.parametrize("failure", ["corrupt", "directory"])
def test_index_failure_keeps_original_acknowledgement(local, failure):
    source, media, home, db, connect = local
    if failure == "corrupt":
        db.write_bytes(b"preserve this invalid index")
    else:
        db.mkdir()
    created = router.create_document(router.CreateRequest(source_id=source.source_id))
    assert created["index_refresh"]["status"] == "failed"
    assert (media / created["name"]).read_bytes() == b""
    saved = router.save_document(router.SaveRequest(source_id=source.source_id, name=created["name"],
        content="Original persists", revision=created["revision"], session_id="e" * 32))
    assert saved["index_refresh"]["status"] == "failed"
    assert (media / saved["name"]).read_text() == "Original persists"
    if failure == "corrupt":
        assert db.read_bytes() == b"preserve this invalid index"


def test_index_retry_checks_fresh_original_and_preserves_recreated_name(local, monkeypatch):
    source, media, home, db, connect = local
    (media / "Current.md").write_text("current")
    (media / "Old.md").write_text("recreated externally")
    status, result = language_http(monkeypatch, "POST", "/api/language/document/index", {
        "source_id": source.source_id, "name": "Current.md", "previous_names": ["Old.md"]})
    assert status == 200 and result["status"] == "complete"
    with index.open_index(db) as connection:
        assert connection.execute("SELECT available FROM files_index WHERE name='Old.md'").fetchone()[0]
    for name, previous_names, expected in [("Missing.md", [], 404), ("Current.md", ["../outside.md"], 400)]:
        status, _ = language_http(monkeypatch, "POST", "/api/language/document/index", {
            "source_id": source.source_id, "name": name, "previous_names": previous_names})
        assert status == expected
    with connect() as connection:
        sources.update_source(connection, source.source_id, visible=False)
    assert language_http(monkeypatch, "POST", "/api/language/document/index", {
        "source_id": source.source_id, "name": "Current.md"})[0] == 404
    assert (media / "Current.md").read_text() == "current"
    assert (media / "Old.md").read_text() == "recreated externally"

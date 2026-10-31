from __future__ import annotations

import shutil
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from files_base import sources  # noqa: E402


class SourcesRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = ROOT / "tests" / ".tmp-files-sources"
        shutil.rmtree(self.temp, ignore_errors=True)
        self.temp.mkdir(parents=True)
        self.library = self.temp / "hoard"
        self.library.mkdir()
        self.conn = sqlite3.connect(self.temp / "user.sqlite")
        self.conn.row_factory = sqlite3.Row
        sources.ensure_sources_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_register_is_idempotent_by_path(self) -> None:
        first = sources.register_source(self.conn, self.library, "My Hoard")
        second = sources.register_source(self.conn, self.library)
        self.assertEqual(first.source_id, second.source_id)
        self.assertEqual(second.display_name, "My Hoard")  # not overwritten
        self.assertEqual(second.role, "files")
        self.assertTrue(second.visible)
        self.assertEqual(len(sources.list_sources(self.conn)), 1)

    def test_visibility_and_display_name_upgrade_are_additive(self) -> None:
        source = sources.register_source(self.conn, self.library)
        updated = sources.update_source(
            self.conn,
            source.source_id,
            display_name="Hidden Hoard",
            visible=False,
        )
        self.assertIsNotNone(updated)
        self.assertEqual(updated.display_name, "Hidden Hoard")
        self.assertFalse(updated.visible)
        self.assertEqual(sources.list_sources(self.conn, visible_only=True), [])

    def test_legacy_base_role_is_read_as_files_without_rewriting_it(self) -> None:
        legacy = sqlite3.connect(self.temp / "legacy-user.sqlite")
        legacy.row_factory = sqlite3.Row
        legacy.execute(
            """CREATE TABLE files_sources (
                   source_id TEXT PRIMARY KEY,
                   path TEXT NOT NULL UNIQUE,
                   display_name TEXT NOT NULL,
                   role TEXT NOT NULL DEFAULT 'base',
                   added_at TEXT
               )"""
        )
        legacy.execute(
            "INSERT INTO files_sources VALUES ('legacy', ?, 'Legacy', 'base', NULL)",
            (str(self.library),),
        )
        legacy.commit()
        sources.ensure_sources_schema(legacy)
        loaded = sources.list_sources(legacy)[0]
        self.assertEqual(loaded.role, "files")
        self.assertTrue(loaded.visible)
        self.assertEqual(legacy.execute("SELECT role FROM files_sources").fetchone()[0], "base")
        legacy.close()

    def test_remove_forgets_source(self) -> None:
        source = sources.register_source(self.conn, self.library)
        self.assertTrue(sources.remove_source(self.conn, source.source_id))
        self.assertEqual(sources.list_sources(self.conn), [])
        self.assertFalse(sources.remove_source(self.conn, source.source_id))

    def test_unsafe_reason_blocks_missing_and_fenced_paths(self) -> None:
        self.assertIsNotNone(
            sources.unsafe_source_reason(self.temp / "nope", [])
        )
        # A folder inside a fenced (generated) tree is rejected.
        fenced_child = self.library
        self.assertIsNotNone(
            sources.unsafe_source_reason(fenced_child, [self.temp])
        )
        # A normal folder outside any fence is allowed.
        self.assertIsNone(
            sources.unsafe_source_reason(self.library, [self.temp / "elsewhere"])
        )


class ModuleProjectionTests(unittest.TestCase):
    """Danbooru publishes its folders into the shared browse-list (folder-roles)."""

    def setUp(self) -> None:
        self.temp = ROOT / "tests" / ".tmp-module-projection"
        shutil.rmtree(self.temp, ignore_errors=True)
        self.temp.mkdir(parents=True)
        (self.temp / "booru-a").mkdir()
        (self.temp / "booru-b").mkdir()
        (self.temp / "plain").mkdir()
        self.conn = sqlite3.connect(self.temp / "user.sqlite")
        self.conn.row_factory = sqlite3.Row
        sources.ensure_sources_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_publish_and_withdraw_a_module_folder(self) -> None:
        sources.upsert_module_source(self.conn, self.temp / "booru-a", "Booru A", "danbooru")
        listed = sources.list_sources(self.conn)
        self.assertEqual([(s.display_name, s.role) for s in listed], [("Booru A", "danbooru")])
        self.assertTrue(sources.remove_source_by_path(self.conn, self.temp / "booru-a"))
        self.assertEqual(sources.list_sources(self.conn), [])

    def test_promoting_a_base_folder_changes_its_role(self) -> None:
        sources.register_source(self.conn, self.temp / "plain")
        sources.upsert_module_source(self.conn, self.temp / "plain", None, "danbooru")
        rows = sources.list_sources(self.conn)
        self.assertEqual(len(rows), 1)  # same folder, not duplicated
        self.assertEqual(rows[0].role, "danbooru")

    def test_reconcile_matches_the_module_list_exactly(self) -> None:
        # Stale danbooru row that is no longer registered...
        sources.upsert_module_source(self.conn, self.temp / "booru-a", None, "danbooru")
        # ...a base folder that must be left untouched...
        sources.register_source(self.conn, self.temp / "plain")
        sources.reconcile_module_sources(
            self.conn, "danbooru", [(str(self.temp / "booru-b"), "Booru B")]
        )
        by_role = {s.display_name: s.role for s in sources.list_sources(self.conn)}
        self.assertEqual(by_role, {"Booru B": "danbooru", "plain": "files"})  # booru-a pruned


class FilesApiRouteTests(unittest.TestCase):
    """Exercise the router functions directly (project convention: no TestClient)."""

    def setUp(self) -> None:
        self.temp = ROOT / "tests" / ".tmp-files-api"
        shutil.rmtree(self.temp, ignore_errors=True)
        self.temp.mkdir(parents=True)
        self.suite_home = self.temp / "suite"
        self.suite_home.mkdir()
        self.library = self.temp / "library"
        (self.library / "sub").mkdir(parents=True)
        (self.library / "a.png").write_bytes(b"png")
        (self.library / "notes.txt").write_bytes(b"txt")
        (self.library / "sub" / "clip.mp4").write_bytes(b"mp4")

        import config
        import database
        from routers import files

        self.files = files
        self._patchers = [
            patch.object(files, "get_user_db", database.get_user_db),
            patch.object(config, "SUITE_HOME", self.suite_home),
            patch.object(config, "FILES_DB_PATH", self.suite_home / "base" / "files.sqlite"),
            patch.object(database, "USER_DB_PATH", self.temp / "user.sqlite"),
        ]
        for patcher in self._patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in self._patchers:
            patcher.stop()
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_register_browse_and_search_roundtrip(self) -> None:
        files = self.files
        registered = files.register_source(
            files.SourceRegister(path=str(self.library), display_name="Lib")
        )
        source_id = registered.source_id

        self.assertEqual([s.display_name for s in files.list_sources()], ["Lib"])

        top = files.browse(source_id=source_id, parent="")
        self.assertEqual(top[0].name, "sub")
        self.assertTrue(top[0].is_dir)
        names = [n.name for n in top]
        self.assertIn("a.png", names)
        self.assertIn("notes.txt", names)  # type-agnostic

        found = files.search(q="clip", source_id=None, limit=200)
        self.assertEqual([n.name for n in found], ["clip.mp4"])

        # Forgetting a source is covered in test_folder_roles, against the batch
        # apply path the app actually uses.

    def test_search_offset_preserves_array_and_scope(self) -> None:
        from fastapi import HTTPException
        from files_base import index
        registered = self.files.register_source(self.files.SourceRegister(path=str(self.library)))
        first = self.files.search('.', registered.source_id, 1)
        following = self.files.search('.', registered.source_id, 1, offset=1)
        assert isinstance(first, list) and first and following
        assert first[0] != following[0]
        assert set(first[0].model_dump()) == set(self.files.FileNode.model_fields)
        with self.assertRaises(HTTPException) as unknown:
            self.files.search('.', 'unknown', 200)
        assert unknown.exception.status_code == 404
        with self.files.get_user_db() as connection:
            sources.update_source(connection, registered.source_id, visible=False)
        assert self.files.search('.', registered.source_id, 200) == []
        with index.open_index(self.files.config.FILES_DB_PATH) as connection:
            assert connection.execute('SELECT count(*) FROM files_index').fetchone()[0] == 4
        assert (self.library / 'notes.txt').read_bytes() == b'txt'

    def test_filesystem_browse_keeps_empty_folders_selectable(self) -> None:
        result = self.files.browse_filesystem(str(self.library / "sub"))
        self.assertEqual(result.path, str((self.library / "sub").resolve()))
        self.assertEqual(result.entries, [])  # Files are not directory entries.
        self.assertEqual(result.parent, str(self.library.resolve()))
        self.assertTrue(self.files.browse_filesystem("").is_root)

    def test_filesystem_browse_rejects_missing_folder(self) -> None:
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as caught:
            self.files.browse_filesystem(str(self.library / "missing"))
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(caught.exception.detail, "Folder not found")

    def test_filesystem_browse_rejects_regular_file(self) -> None:
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as caught:
            self.files.browse_filesystem(str(self.library / "notes.txt"))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.detail, "Choose a folder, not a file")

    def test_filesystem_browse_reports_permission_failure(self) -> None:
        from fastapi import HTTPException

        with patch.object(Path, "iterdir", side_effect=PermissionError("denied")):
            with self.assertRaises(HTTPException) as caught:
                self.files.browse_filesystem(str(self.library))
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(caught.exception.detail, "Cannot read that folder: permission denied")

    def test_filesystem_browse_reports_other_io_failures(self) -> None:
        from fastapi import HTTPException

        with patch.object(Path, "iterdir", side_effect=OSError("device unavailable")):
            with self.assertRaises(HTTPException) as caught:
                self.files.browse_filesystem(str(self.library))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.detail, "Cannot browse that folder")

    def test_filesystem_browse_rejects_invalid_path(self) -> None:
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as caught:
            self.files.browse_filesystem("invalid\0path")
        self.assertEqual(caught.exception.status_code, 400)

    def test_register_rejects_fenced_path(self) -> None:
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as caught:
            self.files.register_source(self.files.SourceRegister(path=str(self.suite_home)))
        self.assertEqual(caught.exception.status_code, 400)

    def test_unknown_source_is_404(self) -> None:
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as browse_error:
            self.files.browse(source_id="src-nope", parent="")
        self.assertEqual(browse_error.exception.status_code, 404)
        with self.assertRaises(HTTPException) as scan_error:
            self.files.scan_source(source_id="src-nope")
        self.assertEqual(scan_error.exception.status_code, 404)

    def test_nested_source_ownership_survives_parent_rescan(self) -> None:
        files = self.files
        parent = files.register_source(files.SourceRegister(path=str(self.library)))
        child_path = self.library / "sub"
        child = files.register_source(files.SourceRegister(path=str(child_path)))

        self.assertEqual(
            [node.name for node in files.browse(source_id=parent.source_id, parent="")][0],
            "sub",
        )
        self.assertEqual(
            [node.name for node in files.browse(source_id=child.source_id, parent="")],
            ["clip.mp4"],
        )

        files.scan_source(source_id=parent.source_id)
        self.assertEqual(
            [node.name for node in files.browse(source_id=child.source_id, parent="")],
            ["clip.mp4"],
        )
        self.assertEqual(
            [node.name for node in files.search(q="clip", source_id=parent.source_id, limit=200)],
            ["clip.mp4"],
        )
        # Reclaiming a nested source's files on forget is covered in
        # test_folder_roles, against the batch apply path.

    def test_edit_path_api_accepts_nested_destination_and_preserves_source_id(self):
        from routers import suite
        from services import folder_roles
        files = self.files
        parent = files.register_source(files.SourceRegister(path=str(self.library)))
        old = self.temp / 'old child'; old.mkdir()
        shutil.copy2(self.library / 'sub' / 'clip.mp4', old / 'clip.mp4')
        child = files.register_source(files.SourceRegister(path=str(old), display_name='Child'))
        with patch.object(folder_roles, 'FILES_DB_PATH', self.suite_home / 'base/files.sqlite'), patch.object(folder_roles, 'USER_DB_PATH', self.temp / 'user.sqlite'), patch.object(folder_roles, 'SUITE_HOME', self.suite_home):
            result = suite.relocate_folder_source(child.source_id, suite.FolderRelocatePayload(path=str(self.library / 'sub')))
        self.assertEqual(result.source_id, child.source_id)
        self.assertEqual([node.name for node in files.browse(source_id=child.source_id, parent='')], ['clip.mp4'])
        files.scan_source(parent.source_id)
        self.assertEqual([node.name for node in files.search(q='clip', source_id=parent.source_id, limit=200)], ['clip.mp4'])
        self.assertEqual(next(source for source in files.list_sources() if source.source_id == child.source_id).display_name, 'Child')


import pytest


@pytest.fixture(params=['row', 'dict'])
def stream_fixture(tmp_path, monkeypatch, request):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from fastapi import FastAPI
    from routers import files
    media = tmp_path / 'media'; media.mkdir()
    private = tmp_path / 'private'; private.mkdir()
    child = media / 'child'; child.mkdir()
    grandchild = child / 'grandchild'; grandchild.mkdir()
    data = b'Generated original stream bytes 0123456789'
    for folder in (media, child, grandchild): (folder / 'clip.webm').write_bytes(data)
    database = tmp_path / 'stream.sqlite'
    factory = sqlite3.Row if request.param == 'row' else lambda cursor, row: dict(zip([column[0] for column in cursor.description], row))
    @contextmanager
    def connect():
        connection = sqlite3.connect(database)
        connection.row_factory = factory
        try: yield connection
        finally: connection.close()
    with connect() as connection:
        sources.ensure_sources_schema(connection)
        source = sources.register_source(connection, media)
    monkeypatch.setattr(files, 'get_user_db', connect)
    monkeypatch.setattr(files.config, 'SUITE_HOME', private)
    app = FastAPI(); app.include_router(files.router)
    return SimpleNamespace(app=app, root=media, private=private, child=child, grandchild=grandchild,
                           data=data, database=database, connect=connect, source=source, directory=tmp_path)


def stream_request(case, source_id=None, path='clip.webm', range_header=None):
    import asyncio
    from urllib.parse import urlencode
    async def run():
        messages = []
        async def receive(): return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(message): messages.append(message)
        headers = [(b'host', b'test')]
        if range_header is not None: headers.append((b'range', range_header.encode()))
        await case.app(dict(type='http', asgi=dict(version='3.0', spec_version='2.4'), method='GET', http_version='1.1',
            scheme='http', path='/api/files/file', raw_path=b'/api/files/file',
            query_string=urlencode(dict(source_id=source_id or case.source.source_id, path=path)).encode(),
            headers=headers, server=('test', 80), client=('127.0.0.1', 12345)), receive, send)
        start = next(message for message in messages if message['type'] == 'http.response.start')
        return start['status'], dict(start['headers']), b''.join(message.get('body', b'') for message in messages if message['type'] == 'http.response.body')
    return asyncio.run(run())


@pytest.mark.parametrize('range_header,expected', [(None, 200), ('bytes=2-8', 206), ('bytes=-5', 206), ('bytes=99999-', 416)])
def test_stream_visible_unindexed_file_preserves_bytes_ranges_and_headers(stream_fixture, range_header, expected):
    case = stream_fixture
    before = case.database.read_bytes()
    status, headers, body = stream_request(case, range_header=range_header)
    assert status == expected
    if expected != 416:
        assert body == (case.data if range_header is None else case.data[2:9] if range_header == 'bytes=2-8' else case.data[-5:])
        assert headers[b'content-type'].startswith(b'video/webm') and headers[b'x-content-type-options'] == b'nosniff'
        assert headers[b'accept-ranges'] == b'bytes' and headers[b'cache-control'] == b'private, no-cache'
        assert headers[b'content-disposition'].startswith(b'inline;')
        if expected == 206: assert b'content-range' in headers
    else: assert headers[b'content-range'] == ('bytes */' + str(len(case.data))).encode()
    assert case.database.read_bytes() == before and (case.root / 'clip.webm').read_bytes() == case.data


def test_stream_hidden_and_unknown_sources_are_denied_and_visibility_is_fresh(stream_fixture):
    case = stream_fixture
    assert stream_request(case)[0] == 200
    with case.connect() as connection: sources.update_source(connection, case.source.source_id, visible=False)
    assert stream_request(case)[0] == 404 and stream_request(case, range_header='bytes=0-3')[0] == 404
    assert stream_request(case, source_id='unknown')[0] == 404
    with case.connect() as connection: sources.update_source(connection, case.source.source_id, visible=True)
    assert stream_request(case, range_header='bytes=0-3')[2] == case.data[:4]


@pytest.mark.parametrize('child_visible', [False, True])
def test_stream_parent_cannot_bypass_registered_child_ownership(stream_fixture, child_visible):
    case = stream_fixture
    with case.connect() as connection:
        child = sources.register_source(connection, case.child)
        sources.update_source(connection, child.source_id, role='language', visible=child_visible)
    assert stream_request(case, path='child/clip.webm')[0] == 403
    assert stream_request(case, path='child/clip.webm', range_header='bytes=0-3')[0] == 403
    assert stream_request(case, child.source_id)[0] == (200 if child_visible else 404)
    assert stream_request(case)[0] == 200


def test_stream_grandchild_and_current_registration_changes_are_rechecked(stream_fixture):
    case = stream_fixture
    with case.connect() as connection:
        child = sources.register_source(connection, case.child)
        grandchild = sources.register_source(connection, case.grandchild)
    assert stream_request(case, path='child/grandchild/clip.webm')[0] == 403
    assert stream_request(case, child.source_id, 'grandchild/clip.webm')[0] == 403
    assert stream_request(case, grandchild.source_id)[2] == case.data
    with case.connect() as connection: sources.update_source(connection, grandchild.source_id, visible=False)
    assert stream_request(case, grandchild.source_id)[0] == 404
    with case.connect() as connection: sources.remove_source(connection, grandchild.source_id)
    assert stream_request(case, child.source_id, 'grandchild/clip.webm')[0] == 200


@pytest.mark.parametrize('hazard,status', [('traversal', 400), ('private', 403), ('missing', 404), ('outside-symlink', 403)])
def test_stream_existing_private_path_and_missing_file_guards_remain(stream_fixture, hazard, status):
    import os
    case = stream_fixture
    path = 'clip.webm'
    source_id = case.source.source_id
    if hazard == 'traversal': path = '../outside.webm'
    elif hazard == 'private':
        (case.private / path).write_bytes(case.data)
        with case.connect() as connection:
            # Direct SQL represents a pre-existing legacy registration; normal
            # source registration already refuses the application's private tree.
            connection.execute("INSERT INTO files_sources(source_id,path,display_name,role) VALUES('private',?,'Private','files')", (str(case.private),))
            connection.commit()
        source_id = 'private'
    elif hazard == 'missing': path = 'missing.webm'
    else:
        outside = case.directory / 'outside.webm'; outside.write_bytes(case.data)
        path = 'alias.webm'
        try: (case.root / path).symlink_to(outside)
        except OSError as error:
            if os.name == 'nt' and getattr(error, 'winerror', None) == 1314: pytest.skip('Windows token lacks symbolic-link privilege')
            raise
    assert stream_request(case, source_id, path, range_header='bytes=0-3')[0] == status


if __name__ == "__main__":
    unittest.main()

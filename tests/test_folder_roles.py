from __future__ import annotations

import shutil
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import config  # noqa: E402
import database  # noqa: E402
import suite_modules  # noqa: E402
from files_base import index, sources  # noqa: E402
from services import folder_roles  # noqa: E402


class FolderRoleServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = ROOT / "tests" / ".tmp-folder-roles"
        shutil.rmtree(self.temp, ignore_errors=True)
        self.temp.mkdir(parents=True)
        self.library = self.temp / "library"
        self.library.mkdir()
        (self.library / "one.txt").write_text("one", encoding="utf-8")
        self.user_db = self.temp / "user.sqlite"
        self.files_db = self.temp / "files.sqlite"
        self.patchers = [
            patch.object(database, "USER_DB_PATH", self.user_db),
            patch.object(folder_roles, "FILES_DB_PATH", self.files_db),
            patch.object(folder_roles, "USER_DB_PATH", self.user_db),
            patch.object(folder_roles, "SUITE_HOME", self.temp / "suite"),
        ]
        for patcher in self.patchers:
            patcher.start()
        with database.get_user_db() as connection:
            sources.ensure_sources_schema(connection)
            suite_modules.ensure_schema(connection)

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_non_folder_module_rejected_before_registering(self) -> None:
        descriptor = replace(config.MODULE_REGISTRY.require('youtube'), adopt_hook=None)
        with patch.object(folder_roles.MODULE_REGISTRY, 'get', return_value=descriptor), self.assertRaises(folder_roles.FolderRegistryError) as caught:
            folder_roles._validate_role('youtube', {'youtube'})
        self.assertEqual(caught.exception.status_code, 400)
        with database.get_user_db() as connection:
            self.assertEqual(sources.list_sources(connection), [])

    def test_add_rename_hide_preview_and_forget_preserve_originals(self) -> None:
        added = folder_roles.apply_changes([
            folder_roles.FolderChange(
                source_id=None,
                path=str(self.library),
                display_name="Library",
                role="files",
            )
        ])
        source = added["sources"][0]
        preview = folder_roles.forget_preview(source.source_id)
        self.assertEqual(preview["base_files"], 1)

        changed = folder_roles.apply_changes([
            folder_roles.FolderChange(
                source_id=source.source_id,
                path=source.path,
                display_name="Private library",
                role="files",
                visible=False,
            )
        ])
        self.assertEqual(changed["sources"][0].display_name, "Private library")
        self.assertFalse(changed["sources"][0].visible)

        forgotten = folder_roles.apply_changes([
            folder_roles.FolderChange(
                source_id=source.source_id,
                path=source.path,
                display_name="Private library",
                role="files",
                visible=False,
                forget=True,
            )
        ])
        self.assertEqual(forgotten["sources"], [])
        self.assertTrue((self.library / "one.txt").is_file())
        with index.open_index(self.files_db) as connection:
            self.assertEqual(index.source_entry_count(connection, source.source_id), 0)

    def test_disabled_module_role_is_rejected_before_mutation(self) -> None:
        with self.assertRaises(folder_roles.FolderRegistryError) as caught:
            folder_roles.apply_changes([
                folder_roles.FolderChange(
                    source_id=None,
                    path=str(self.library),
                    display_name="Library",
                    role="danbooru",
                )
            ])
        self.assertEqual(caught.exception.status_code, 409)
        with database.get_user_db() as connection:
            self.assertEqual(sources.list_sources(connection), [])

    def test_existing_disabled_module_folder_allows_presentation_edits(self) -> None:
        with database.get_user_db() as connection:
            source = sources.register_source(connection, self.library, "Library", role="danbooru")
        descriptor = config.MODULE_REGISTRY.require("danbooru")
        update = Mock(side_effect=AssertionError("Disabled hook must not run"))
        registry = config.MODULE_REGISTRY.replacing(replace(
            descriptor,
            folder_update_hook=update,
        ))
        with patch.object(folder_roles, "MODULE_REGISTRY", registry):
            changed = folder_roles.apply_changes([
                folder_roles.FolderChange(
                    source.source_id,
                    source.path,
                    "Hidden module folder",
                    "danbooru",
                    visible=False,
                )
            ])
        self.assertEqual(changed["sources"][0].display_name, "Hidden module folder")
        self.assertFalse(changed["sources"][0].visible)

    def test_disabled_owner_operations_reject_before_any_batch_mutation(self) -> None:
        with database.get_user_db() as connection:
            source = sources.register_source(connection, self.library, "Library", role="danbooru")
        operations = [
            lambda: folder_roles.rescan(source.source_id),
            lambda: folder_roles.relocate(source.source_id, str(self.temp / "moved")),
            lambda: folder_roles.forget_preview(source.source_id),
            lambda: folder_roles.apply_changes([
                folder_roles.FolderChange(source.source_id, source.path, "Changed", "files")]),
            lambda: folder_roles.apply_changes([
                folder_roles.FolderChange(source.source_id, source.path, "Changed", "danbooru", forget=True)]),
        ]
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(folder_roles.FolderRegistryError) as caught:
                operation()
            self.assertEqual(caught.exception.status_code, 409)
            with database.get_user_db() as connection:
                unchanged = sources.get_source(connection, source.source_id)
                self.assertEqual(unchanged.display_name, "Library")
                self.assertEqual(unchanged.role, "danbooru")
        self.assertFalse(self.files_db.exists())
        self.assertTrue((self.library / "one.txt").exists())

    def test_absent_owner_presentation_is_shared_but_operations_are_rejected(self) -> None:
        from module_registry import ModuleRegistry
        with database.get_user_db() as connection:
            source = sources.register_source(connection, self.library, "Library", role="danbooru")
        registry = ModuleRegistry((config.MODULE_REGISTRY.require("files"),))
        with patch.object(folder_roles, "MODULE_REGISTRY", registry), patch.object(suite_modules, "MODULE_REGISTRY", registry):
            changed = folder_roles.apply_changes([
                folder_roles.FolderChange(source.source_id, source.path, "Kept", "danbooru", visible=False)])
            self.assertEqual(changed["sources"][0].display_name, "Kept")
            with self.assertRaises(folder_roles.FolderRegistryError) as caught:
                folder_roles.rescan(source.source_id)
            self.assertEqual(caught.exception.status_code, 409)

    def test_republication_keeps_shared_label_and_synchronizes_owner_label(self) -> None:
        from modules.danbooru import publish_sources
        with database.get_user_db() as connection:
            connection.execute("CREATE TABLE registered_folders(path TEXT, display_name TEXT)")
            connection.execute("INSERT INTO registered_folders VALUES (?, ?)", (str(self.library), "Old"))
            source = sources.register_source(connection, self.library, "Shared label", role="danbooru")
            sources.update_source(connection, source.source_id, visible=False)
            publish_sources(connection)
            self.assertEqual(connection.execute("SELECT display_name FROM registered_folders").fetchone()["display_name"], "Shared label")
            shared = sources.get_source(connection, source.source_id)
            self.assertEqual(shared.display_name, "Shared label")
            self.assertFalse(shared.visible)

    def test_failed_owner_cannot_receive_new_assignment(self) -> None:
        import lifecycle
        with database.get_user_db() as connection:
            suite_modules.set_enabled(connection, "danbooru", True)
        runtime = Mock()
        runtime.status.return_value = {"state": "failed", "error": "fixture"}
        with patch.object(lifecycle, "module_runtime", runtime):
            with self.assertRaises(folder_roles.FolderRegistryError) as caught:
                folder_roles.apply_changes([
                    folder_roles.FolderChange(None, str(self.library), "Library", "danbooru")])
            self.assertEqual(caught.exception.status_code, 503)
        with database.get_user_db() as connection:
            self.assertEqual(sources.list_sources(connection), [])

    def test_role_changes_dispatch_through_descriptor_hooks(self) -> None:
        events: list[tuple[str, str]] = []

        def set_role(source_id: str, role: str) -> None:
            with database.get_user_db() as connection:
                sources.ensure_sources_schema(connection)
                sources.update_source(connection, source_id, role=role)

        danbooru = config.MODULE_REGISTRY.require("danbooru")
        test_registry = config.MODULE_REGISTRY.replacing(replace(
            danbooru,
            adopt_hook=lambda source_id: (events.append(("adopt", source_id)), set_role(source_id, "danbooru"), {})[-1],
            release_hook=lambda source_id, forget=False: (events.append(("release", source_id)), set_role(source_id, "files"), {})[-1],
            folder_update_hook=lambda source_id: None,
        ))
        with database.get_user_db() as connection:
            suite_modules.set_enabled(connection, "danbooru", True)

        with patch.object(folder_roles, "MODULE_REGISTRY", test_registry):
            result = folder_roles.apply_changes([
                folder_roles.FolderChange(None, str(self.library), "Library", "danbooru")
            ])
            source = result["sources"][0]
            self.assertEqual(source.role, "danbooru")
            folder_roles.apply_changes([
                folder_roles.FolderChange(source.source_id, source.path, "Library", "files")
            ])

        self.assertEqual([event[0] for event in events], ["adopt", "release"])

    def test_forgetting_nested_source_returns_ownership_to_parent(self) -> None:
        child = self.library / "child"
        child.mkdir()
        nested_file = child / "nested.txt"
        nested_file.write_text("nested", encoding="utf-8")

        parent_result = folder_roles.apply_changes([
            folder_roles.FolderChange(None, str(self.library), "Library", "files")
        ])
        parent = parent_result["sources"][0]
        nested_result = folder_roles.apply_changes([
            folder_roles.FolderChange(
                parent.source_id,
                parent.path,
                parent.display_name,
                parent.role,
            ),
            folder_roles.FolderChange(None, str(child), "Child", "files"),
        ])
        nested = next(source for source in nested_result["sources"] if source.path == str(child))

        with index.open_index(self.files_db) as connection:
            owner = connection.execute(
                "SELECT source_id FROM files_index WHERE path=?",
                (str(nested_file),),
            ).fetchone()
            self.assertEqual(owner["source_id"], nested.source_id)

        folder_roles.apply_changes([
            folder_roles.FolderChange(
                parent.source_id,
                parent.path,
                parent.display_name,
                parent.role,
            ),
            folder_roles.FolderChange(
                nested.source_id,
                nested.path,
                nested.display_name,
                nested.role,
                forget=True,
            ),
        ])

        with index.open_index(self.files_db) as connection:
            owner = connection.execute(
                "SELECT source_id FROM files_index WHERE path=?",
                (str(nested_file),),
            ).fetchone()
            self.assertEqual(owner["source_id"], parent.source_id)
        self.assertTrue(nested_file.is_file())

    def test_rescan_files_source_refreshes_the_base_index(self) -> None:
        added = folder_roles.apply_changes([
            folder_roles.FolderChange(None, str(self.library), "Library", "files")
        ])
        source = added["sources"][0]
        result = folder_roles.rescan(source.source_id)
        self.assertEqual(result["source_id"], source.source_id)
        # A base (Files) folder has no extra module rescan.
        self.assertEqual(result["module"], {})
        with index.open_index(self.files_db) as connection:
            self.assertEqual(index.source_entry_count(connection, source.source_id), 1)

    def test_rescan_unknown_source_raises_not_found(self) -> None:
        with self.assertRaises(folder_roles.FolderRegistryError) as caught:
            folder_roles.rescan("does-not-exist")
        self.assertEqual(caught.exception.status_code, 404)

    def test_relocate_files_preserves_identity_annotations_and_unavailable_rows(self) -> None:
        added = folder_roles.apply_changes([
            folder_roles.FolderChange(None, str(self.library), "Library", "files")
        ])
        source = added["sources"][0]
        from files_base import annotations
        with database.get_user_db() as connection:
            annotations.ensure_annotations_schema(connection)
            connection.execute("INSERT INTO files_annotations(subject_kind,source_id,relative_path,description) VALUES('folder',?,'','Keep this note')", (source.source_id,))
            connection.execute('UPDATE files_sources SET path=?, visible=0 WHERE source_id=?', ('D:\\Library', source.source_id))
            connection.commit()
        with index.open_index(self.files_db) as connection:
            connection.execute("INSERT INTO files_index(source_id,path,relative_path,name,available) VALUES(?,?,'missing.txt','missing.txt',1)", (source.source_id, 'D:\\Library\\missing.txt'))
            connection.commit()
        target = self.temp / 'New folder 한글'
        target.mkdir(); (target / 'one.txt').write_text('copied file')
        result = folder_roles.relocate(source.source_id, str(target.resolve()))
        self.assertEqual(result['source_id'], source.source_id)
        with database.get_user_db() as connection:
            current = sources.get_source(connection, source.source_id)
            self.assertEqual(current.path, str(target.resolve()))
            self.assertEqual((current.display_name, current.role, current.visible), ('Library', 'files', False))
            self.assertEqual(connection.execute('SELECT description FROM files_annotations').fetchone()['description'], 'Keep this note')
            self.assertEqual(sources.register_source(connection, target).source_id, source.source_id)
            reused_old_path = sources.register_source(connection, self.library, 'Different folder')
            self.assertNotEqual(reused_old_path.source_id, source.source_id)
            self.assertEqual(sources.get_source(connection, source.source_id).path, str(target.resolve()))
        with index.open_index(self.files_db) as connection:
            missing = connection.execute("SELECT * FROM files_index WHERE relative_path='missing.txt'").fetchone()
            self.assertFalse(missing['available'])
            self.assertEqual(missing['source_id'], source.source_id)
        self.assertEqual(folder_roles.relocate(source.source_id, str(target.resolve()))['files_updated'], 0)

    def test_relocate_validation_and_scan_failure_preserve_original_state(self):
        source = folder_roles.apply_changes([folder_roles.FolderChange(None, str(self.library), 'Library', 'files')])['sources'][0]
        target = self.temp / 'target'; target.mkdir()
        for invalid in ('relative/folder', str(self.temp / 'missing'), str(self.library / 'one.txt'), str(self.temp / 'suite')):
            with self.subTest(path=invalid), self.assertRaises(folder_roles.FolderRegistryError):
                folder_roles.relocate(source.source_id, invalid)
        other = self.temp / 'other'; other.mkdir()
        folder_roles.apply_changes([folder_roles.FolderChange(None, str(other), 'Other', 'files')])
        for invalid in (other, self.temp):
            with self.assertRaises(folder_roles.FolderRegistryError):
                folder_roles.relocate(source.source_id, str(invalid.resolve()))
        with patch.object(index, 'scan_source', side_effect=OSError('generated scan failure')):
            with self.assertRaisesRegex(folder_roles.FolderRegistryError, 'path was not changed'):
                folder_roles.relocate(source.source_id, str(target.resolve()))
        with database.get_user_db() as connection:
            self.assertEqual(sources.get_source(connection, source.source_id).path, str(self.library.resolve()))
        with index.open_index(self.files_db) as connection:
            self.assertEqual(connection.execute('SELECT path FROM files_index WHERE source_id=?', (source.source_id,)).fetchone()[0], str((self.library / 'one.txt').resolve()))
        self.assertEqual((self.library / 'one.txt').read_text(), 'one')

    def test_shared_module_relocation_preserves_source_keyed_personal_state(self):
        from modules.manga.user_schema import SCHEMA
        for role in ('video', 'manga', 'ost', 'language', 'youtube', 'twitter'):
            with self.subTest(role=role):
                old = self.temp / role; old.mkdir()
                target = self.temp / (role + '-moved'); target.mkdir()
                with database.get_user_db() as connection:
                    suite_modules.set_enabled(connection, role, True)
                    source = sources.register_source(connection, old, role, role=role)
                    connection.executescript(SCHEMA)
                    connection.execute("INSERT INTO manga_reading_sessions(source_id,path,kind,generation) VALUES(?,'chapter.cbz','archive',7)", (source.source_id,))
                    connection.commit()
                self.assertEqual(folder_roles.relocate(source.source_id, str(target.resolve()))['source_id'], source.source_id)
                with database.get_user_db() as connection:
                    self.assertEqual(sources.get_source(connection, source.source_id).role, role)
                    self.assertEqual(connection.execute('SELECT generation FROM manga_reading_sessions WHERE source_id=?', (source.source_id,)).fetchone()['generation'], 7)

    def test_active_download_refuses_path_edit_without_mutation(self):
        source = folder_roles.apply_changes([folder_roles.FolderChange(None, str(self.library), 'Library', 'files')])['sources'][0]
        target = self.temp / 'target'; target.mkdir()
        with database.get_user_db() as connection:
            connection.execute('CREATE TABLE youtube_downloads(source_id TEXT, status TEXT)')
            connection.execute("INSERT INTO youtube_downloads VALUES(?,'queued')", (source.source_id,))
            connection.commit()
        with self.assertRaisesRegex(folder_roles.FolderRegistryError, 'Wait for downloads'):
            folder_roles.relocate(source.source_id, str(target.resolve()))
        with database.get_user_db() as connection:
            self.assertEqual(sources.get_source(connection, source.source_id).path, str(self.library.resolve()))

    def test_relocation_refuses_concurrent_scan_and_unsafe_index_paths(self):
        import threading
        source = folder_roles.apply_changes([folder_roles.FolderChange(None, str(self.library), 'Library', 'files')])['sources'][0]
        target = self.temp / 'target'; target.mkdir()
        held = threading.Event(); release = threading.Event()
        def scanning():
            with index.source_work():
                held.set(); release.wait(5)
        worker = threading.Thread(target=scanning)
        worker.start(); self.assertTrue(held.wait(3))
        try:
            with self.assertRaisesRegex(folder_roles.FolderRegistryError, 'folder work'):
                folder_roles.relocate(source.source_id, str(target.resolve()))
        finally:
            release.set(); worker.join(5)
        with index.open_index(self.files_db) as connection:
            connection.execute("UPDATE files_index SET relative_path='../escape'")
            connection.commit()
        with self.assertRaisesRegex(folder_roles.FolderRegistryError, 'relinked safely'):
            folder_roles.relocate(source.source_id, str(target.resolve()))
        with database.get_user_db() as connection:
            self.assertEqual(sources.get_source(connection, source.source_id).path, str(self.library.resolve()))

    def copied_nested_roots(self):
        from files_base import annotations
        parent = self.temp / 'copied library'
        child = parent / 'hehetwitter'
        child.mkdir(parents=True)
        (parent / 'parent.txt').write_text('parent original', encoding='utf-8')
        (child / 'post.txt').write_text('child original', encoding='utf-8')
        self.copied_old_parent = 'D:\\Library' if sys.platform != 'win32' else str(self.temp / 'absent windows copy')
        self.copied_old_child = self.copied_old_parent + '\\hehetwitter'
        with database.get_user_db() as connection:
            annotations.ensure_annotations_schema(connection)
            suite_modules.set_enabled(connection, 'twitter', True)
            for source_id, path, role in [('parent-id', self.copied_old_parent, 'files'),
                                          ('child-id', self.copied_old_child, 'twitter')]:
                connection.execute('INSERT INTO files_sources(source_id,path,display_name,role,visible) VALUES(?,?,?,?,1)',
                                   (source_id, path, source_id, role))
                cursor = connection.execute("INSERT INTO files_annotations(subject_kind,source_id,relative_path,description) VALUES('folder',?,'',?)", (source_id, 'note for ' + source_id))
                connection.execute("INSERT INTO files_annotation_links(annotation_id,url,label,kind,position) VALUES(?,'https://example.invalid/original','Keep link','source',0)", (cursor.lastrowid,))
            connection.commit()
        with index.open_index(self.files_db) as connection:
            for source_id, path, relative, is_dir in [
                ('parent-id', self.copied_old_parent + '\\parent.txt', 'parent.txt', 0),
                ('parent-id', self.copied_old_child, 'hehetwitter', 1),
                ('child-id', self.copied_old_child + '\\post.txt', 'post.txt', 0),
                ('child-id', self.copied_old_child + '\\missing.txt', 'missing.txt', 0)]:
                connection.execute('INSERT INTO files_index(source_id,path,relative_path,parent,name,is_dir,available) VALUES(?,?,?,?,?,?,1)',
                                   (source_id, path, relative, '', relative, is_dir))
            connection.commit()
        return parent, child

    def nested_user_records(self):
        with database.get_user_db() as connection:
            return {table: connection.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()
                    for table in ('files_annotations', 'files_annotation_links')}

    def test_parent_first_copied_nested_repair_preserves_ownership_and_metadata(self):
        # Include duplicate derived entries when a corrected parent initially
        # indexes the child, whose registration still has its old Windows path.
        parent, child = self.copied_nested_roots()
        before = self.nested_user_records()
        for source_id, path in [('parent-id', parent), ('child-id', child)]:
            self.assertEqual(folder_roles.relocate(source_id, str(path.resolve()))['source_id'], source_id)
        self.assertEqual(self.nested_user_records(), before)
        with index.open_index(self.files_db) as connection:
            row = connection.execute('SELECT source_id,available FROM files_index WHERE path=?', (str((child / 'post.txt').resolve()),)).fetchone()
            self.assertEqual((row['source_id'], row['available']), ('child-id', 1))
            directory = connection.execute('SELECT source_id FROM files_index WHERE path=?', (str(child.resolve()),)).fetchone()
            self.assertEqual(directory['source_id'], 'parent-id')
            self.assertEqual(connection.execute("SELECT count(*) FROM files_index WHERE source_id='child-id' AND relative_path='missing.txt' AND available=0").fetchone()[0], 1)
        with database.get_user_db() as connection:
            self.assertEqual(sources.get_source(connection, 'child-id').role, 'twitter')
        self.assertEqual(folder_roles.relocate('child-id', str(child.resolve()))['files_updated'], 0)
        self.assertEqual(folder_roles.relocate('parent-id', str(parent.resolve()))['files_updated'], 0)

    def test_child_first_repair_preserves_child_ownership_when_parent_is_corrected(self):
        parent, child = self.copied_nested_roots()
        before = self.nested_user_records()
        folder_roles.relocate('child-id', str(child.resolve()))
        # A legacy parent fact for the same relative file must not replace the
        # child's existing entry when correcting the containing root.
        with index.open_index(self.files_db) as connection:
            child_row = dict(connection.execute('SELECT * FROM files_index WHERE path=?', (str((child / 'post.txt').resolve()),)).fetchone())
            connection.execute("INSERT INTO files_index(source_id,path,relative_path,name,available) VALUES('parent-id',?,'hehetwitter/post.txt','post.txt',1)", (self.copied_old_child + '\\post.txt',))
            connection.commit()
        folder_roles.relocate('parent-id', str(parent.resolve()))
        with index.open_index(self.files_db) as connection:
            row = connection.execute('SELECT * FROM files_index WHERE path=?', (str((child / 'post.txt').resolve()),)).fetchone()
            self.assertEqual((row['source_id'], row['id']), ('child-id', child_row['id']))
            self.assertEqual(connection.execute("SELECT available FROM files_index WHERE source_id='parent-id' AND relative_path='hehetwitter/post.txt'").fetchone()[0], 0)
        self.assertEqual(self.nested_user_records(), before)

    def test_relocating_child_out_rescans_its_old_parent(self):
        parent, child = self.copied_nested_roots()
        folder_roles.relocate('parent-id', str(parent.resolve()))
        folder_roles.relocate('child-id', str(child.resolve()))
        target = self.temp / 'child copy'
        shutil.copytree(child, target)
        before = self.nested_user_records()
        folder_roles.relocate('child-id', str(target.resolve()))
        with index.open_index(self.files_db) as connection:
            for path, owner in [(child / 'post.txt', 'parent-id'), (target / 'post.txt', 'child-id')]:
                row = connection.execute('SELECT source_id,available FROM files_index WHERE path=?', (str(path.resolve()),)).fetchone()
                self.assertEqual((row['source_id'], row['available']), (owner, 1))
        self.assertEqual(self.nested_user_records(), before)
        self.assertEqual((child / 'post.txt').read_text(), 'child original')

    def test_nested_relink_rollback_restores_ancestor_claims_and_user_records(self):
        parent, child = self.copied_nested_roots()
        folder_roles.relocate('parent-id', str(parent.resolve()))
        before = self.nested_user_records()
        with index.open_index(self.files_db) as connection:
            indexed = [dict(row) for row in connection.execute('SELECT * FROM files_index ORDER BY id')]
        original = folder_roles._scan_registered_source
        def fail_child(connection, source, all_sources, **kwargs):
            if source.source_id == 'child-id':
                raise OSError('generated nested scan failure')
            return original(connection, source, all_sources, **kwargs)
        with patch.object(folder_roles, '_scan_registered_source', side_effect=fail_child):
            with self.assertRaisesRegex(folder_roles.FolderRegistryError, 'path was not changed'):
                folder_roles.relocate('child-id', str(child.resolve()))
        with index.open_index(self.files_db) as connection:
            self.assertEqual([dict(row) for row in connection.execute('SELECT * FROM files_index ORDER BY id')], indexed)
        with database.get_user_db() as connection:
            self.assertEqual(sources.get_source(connection, 'child-id').path, self.copied_old_child)
        self.assertEqual(self.nested_user_records(), before)

    def test_nested_relink_keeps_unrelated_index_conflicts_as_refusals(self):
        parent, child = self.copied_nested_roots()
        unrelated = self.temp / 'unrelated'; unrelated.mkdir()
        with database.get_user_db() as connection:
            foreign = sources.register_source(connection, unrelated)
        with index.open_index(self.files_db) as connection:
            connection.execute('INSERT INTO files_index(source_id,path,relative_path,name,available) VALUES(?,?,?,?,1)',
                               (foreign.source_id, str((child / 'post.txt').resolve()), 'post.txt', 'post.txt'))
            connection.commit()
        with self.assertRaisesRegex(folder_roles.FolderRegistryError, 'conflicts with an indexed file'):
            folder_roles.relocate('child-id', str(child.resolve()))
        with database.get_user_db() as connection:
            self.assertEqual(sources.get_source(connection, 'child-id').path, self.copied_old_child)

    def test_nested_relink_refuses_related_active_downloads(self):
        parent, child = self.copied_nested_roots()
        folder_roles.relocate('parent-id', str(parent.resolve()))
        with database.get_user_db() as connection:
            connection.execute('CREATE TABLE youtube_downloads(source_id TEXT, status TEXT)')
            connection.execute("INSERT INTO youtube_downloads VALUES('parent-id','queued')")
            connection.commit()
        with self.assertRaisesRegex(folder_roles.FolderRegistryError, 'Wait for downloads'):
            folder_roles.relocate('child-id', str(child.resolve()))
        with database.get_user_db() as connection:
            self.assertEqual(sources.get_source(connection, 'child-id').path, self.copied_old_child)

    def test_parent_relink_preserves_disabled_child_without_owner_work(self):
        parent, child = self.copied_nested_roots()
        folder_roles.relocate('child-id', str(child.resolve()))
        with database.get_user_db() as connection:
            suite_modules.set_enabled(connection, 'twitter', False)
        before = self.nested_user_records()
        folder_roles.relocate('parent-id', str(parent.resolve()))
        with index.open_index(self.files_db) as connection:
            self.assertEqual(connection.execute('SELECT source_id FROM files_index WHERE path=?', (str((child / 'post.txt').resolve()),)).fetchone()['source_id'], 'child-id')
        self.assertEqual(self.nested_user_records(), before)

    def test_relocate_dispatches_to_module_and_reindexes_at_new_path(self) -> None:
        with database.get_user_db() as connection:
            suite_modules.set_enabled(connection, "danbooru", True)
        with database.get_user_db() as connection:
            source = sources.register_source(connection, self.library, "Library", role="danbooru")
        with index.open_index(self.files_db) as index_connection:
            index.scan_source(index_connection, source.source_id, self.library, excluded_roots=[])

        moved_dir = self.temp / "moved-library"
        shutil.move(str(self.library), str(moved_dir))

        def fake_module_relocate(source_id: str, new_path: str) -> dict:
            # Stand in for the Danbooru relocate + re-publish: the module keeps its
            # own stable identity and re-points the shared source at the new path.
            with database.get_user_db() as connection:
                sources.ensure_sources_schema(connection)
                sources.remove_source(connection, source_id)
                sources.register_source(connection, new_path, "Library", role="danbooru")
            return {"files_updated": 7}

        danbooru = config.MODULE_REGISTRY.require("danbooru")
        registry = config.MODULE_REGISTRY.replacing(replace(
            danbooru,
            relocate_hook=fake_module_relocate,
            folder_update_hook=None,
            publish_hook=lambda connection: None,
        ))
        with patch.object(folder_roles, "MODULE_REGISTRY", registry):
            result = folder_roles.relocate(source.source_id, str(moved_dir))

        new_id = source.source_id
        self.assertEqual(result["source_id"], new_id)
        self.assertEqual(result["files_updated"], 7)
        with index.open_index(self.files_db) as index_connection:
            self.assertEqual(index.source_entry_count(index_connection, new_id), 1)

    def test_rescan_dispatches_to_the_owning_module_hook(self) -> None:
        with database.get_user_db() as connection:
            suite_modules.set_enabled(connection, "danbooru", True)
        calls: list[str] = []
        with database.get_user_db() as connection:
            source = sources.register_source(connection, self.library, "Library", role="danbooru")
        danbooru = config.MODULE_REGISTRY.require("danbooru")
        registry = config.MODULE_REGISTRY.replacing(replace(
            danbooru,
            rescan_hook=lambda source_id: (calls.append(source_id), {"status": "started"})[-1],
        ))
        with patch.object(folder_roles, "MODULE_REGISTRY", registry):
            result = folder_roles.rescan(source.source_id)
        # The module's own rescan ran, and the base index was refreshed too.
        self.assertEqual(calls, [source.source_id])
        self.assertEqual(result["module"], {"status": "started"})
        with index.open_index(self.files_db) as connection:
            self.assertEqual(index.source_entry_count(connection, source.source_id), 1)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from module_registry import build_registry  # noqa: E402


class ModuleRegistryTests(unittest.TestCase):
    def test_static_registry_has_one_required_base_and_danbooru_module(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as temporary:
            home = Path(temporary)
            registry = build_registry(home, "1.1.0")

        files = registry.require("files")
        danbooru = registry.require("danbooru")
        self.assertEqual(registry.base, files)
        self.assertTrue(files.is_base)
        self.assertFalse(files.disableable)
        self.assertEqual(files.database, home.resolve() / "base" / "files.sqlite")
        self.assertEqual(files.api_prefix, "/api/files")
        self.assertFalse(danbooru.is_base)
        self.assertTrue(danbooru.disableable)
        self.assertEqual(danbooru.database, home.resolve() / "modules" / "danbooru" / "danbooru.sqlite")
        self.assertEqual(danbooru.api_prefix, "/api/danbooru")
        self.assertIn("Danbooru", danbooru.user_agent)
        language = registry.require("language")
        self.assertTrue(language.experimental)
        self.assertTrue(language.disableable)
        self.assertIsNotNone(language.adopt_hook)
        self.assertEqual(language.api_prefix, "/api/language")
        ost = registry.require("ost")
        self.assertTrue(ost.experimental)
        self.assertTrue(ost.disableable)
        self.assertIsNotNone(ost.adopt_hook)
        self.assertEqual(ost.api_prefix, "/api/ost")
        self.assertEqual(ost.database, files.database)
        twitter = registry.require("twitter")
        self.assertTrue(twitter.experimental)
        self.assertTrue(twitter.disableable)
        self.assertIsNotNone(twitter.adopt_hook)
        self.assertIsNotNone(twitter.release_hook)
        self.assertEqual(twitter.api_prefix, "/api/twitter")
        self.assertEqual(twitter.database, files.database)
        self.assertEqual(twitter.backup_components(), ())
        self.assertIsNotNone(twitter.startup_hook)
        self.assertIsNotNone(twitter.background_tasks_hook)
        self.assertIn('twitter_downloads', twitter.user_schema_provider())

    def test_registry_rejects_duplicate_slugs(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as temporary:
            registry = build_registry(Path(temporary), "1.1.0")
        with self.assertRaises(ValueError):
            type(registry)((registry.base, registry.base))


if __name__ == "__main__":
    unittest.main()


def test_manga_rescan_and_delivery_registration_do_not_import_the_metadata_runtime(tmp_path):
    import subprocess
    code="""import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from module_registry import build_registry,delivery_contributions
registry=build_registry(Path(sys.argv[2]),'test')
assert callable(registry.require('manga').index_rescan_hook)
for target in ('linux','win32'):
 assert any('defusedxml' in value.collect_packages for value in delivery_contributions(target))
assert 'modules.manga.metadata' not in sys.modules
assert 'modules.manga.library' not in sys.modules
"""
    result=subprocess.run([sys.executable,'-c',code,str(ROOT/'backend'),str(tmp_path/'home')],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr

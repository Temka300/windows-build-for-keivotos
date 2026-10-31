"""Exercise delivery declarations/specs without packaging or live user data."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))


@pytest.mark.parametrize('installed', [False, True])
@pytest.mark.parametrize('target', ['linux', 'win32'])
def test_specs_and_frozen_checks_with_physical_module_absence(tmp_path, installed, target):
    source = tmp_path / 'source'
    source.mkdir()
    omitted = ('__pycache__',) if installed else ('__pycache__', 'danbooru', 'youtube', 'language', 'ost', 'twitter')
    shutil.copytree(ROOT / 'backend', source / 'backend', ignore=shutil.ignore_patterns(*omitted))
    shutil.copytree(ROOT / 'packaging', source / 'packaging')
    shutil.copytree(ROOT / 'assets/branding/keivotos', source / 'assets/branding/keivotos')
    shutil.copy2(ROOT / 'app.py', source / 'app.py')
    shutil.copy2(ROOT / 'config.json', source / 'config.json')
    (source / 'frontend/dist').mkdir(parents=True)
    (source / 'frontend/dist/index.html').write_text('fixture')
    (source / 'scripts/release').mkdir(parents=True)
    shutil.copy2(ROOT / 'scripts/release/delivery_plan.py', source / 'scripts/release/delivery_plan.py')
    # These helper files are intentionally left present for the absent case:
    # registration, not accidental file presence, decides what to package.
    for name in ('windows_folder_picker.py', 'danbooru_gallery_dl.py'):
        (source / 'scripts' / name).touch()
    code = r'''
import json, sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
import app, config
from delivery import delivery_plan
root = Path.cwd()
target, installed = sys.argv[1], sys.argv[2] == 'True'
plan = delivery_plan(target)
assert [t.name for t in plan.tools] == (['gallery-dl', 'ffmpeg'] if installed else ['ffmpeg'])
assert ('modules.danbooru.pipeline' in plan.hidden_imports) == installed
assert bool(tuple(package for package in plan.metadata_packages if package != 'defusedxml')) == installed
assert 'defusedxml' in plan.metadata_packages and 'defusedxml' in plan.collect_packages
assert ('yt_dlp' in plan.collect_packages) == installed
assert 'yt_dlp_ejs' not in plan.collect_packages
assert ('modules.youtube.channels' in plan.hidden_imports) == installed
if installed:
    from modules.youtube import worker, metadata as channel_metadata, channel_art
    assert worker.metadata is channel_metadata and worker.channel_art is channel_art
    assert channel_metadata.ART_NAMES and channel_art.MAX_BYTES == 2 * 1024 * 1024
    from modules.youtube import jobs, layout, saved_item
    assert jobs.layout is layout and jobs.saved_item is saved_item
    assert layout.MAX_PATH == 240 and saved_item.MAX_BYTES == 16384
assert bool(plan.configured_paths) == installed
assert 'modules.danbooru.credentials' not in sys.modules
assert 'modules.danbooru.pipeline' not in sys.modules
assert 'credentials' not in sys.modules
# Evaluate the actual spec with a recording freezer API. No build is invoked.
hooks = ModuleType('PyInstaller.utils.hooks')
calls = []
def collect(name):
    calls.append(('collect', name))
    return [name]
def metadata(name, recursive):
    calls.append(('metadata', name))
    return [(name + '.dist-info', '.')]
hooks.collect_submodules, hooks.copy_metadata = collect, metadata
for name in ('PyInstaller', 'PyInstaller.utils'):
    sys.modules[name] = ModuleType(name)
sys.modules['PyInstaller.utils.hooks'] = hooks
captured = {}
executable = {}
def make_executable(*args, **kwargs):
    executable.update(kwargs)
def analysis(*args, **kwargs):
    captured.update(kwargs)
    return SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[])
platform_dir = 'windows' if target == 'win32' else 'linux'
spec = root / 'packaging' / platform_dir / 'Keivotos.spec'
namespace = {'SPECPATH': str(spec.parent), 'Analysis': analysis,
             'PYZ': lambda *a, **kw: None, 'EXE': make_executable,
             'COLLECT': lambda *a, **kw: None}
exec(compile(spec.read_text(), str(spec), 'exec'), namespace)
assert 'server' in captured['hiddenimports']
assert 'runtime_logging' in captured['hiddenimports']
assert ('modules.danbooru.pipeline' in captured['hiddenimports']) == installed
assert ('collect', 'defusedxml') in calls and ('metadata', 'defusedxml') in calls
assert (('metadata', 'keyring') in calls) == (installed and target == 'linux')
assert (('collect', 'secretstorage') in calls) == (installed and target == 'linux')
helpers = {Path(path).name for path, _ in captured['datas']}
assert 'windows_folder_picker.py' in helpers
asset = 'keivotos.ico' if target == 'win32' else 'keivotos-icon-256.png'
resource = root / 'assets/branding/keivotos' / asset
assert (str(resource), 'assets/branding/keivotos') in captured['datas']
assert resource.is_file()
assert executable['console'] is True
if target == 'win32':
    assert Path(executable['icon']).read_bytes() == resource.read_bytes()
    assert Path(executable['version']).is_file()
assert ('danbooru_gallery_dl.py' in helpers) == installed
# Keep config's source paths isolated, then simulate only frozen resource checks.
asgi_app = app._load_asgi_app()
assert ('language' in [descriptor.slug for descriptor in config.MODULE_REGISTRY]) == installed
assert ('/api/language/documents' in asgi_app.openapi()['paths']) == installed
assert ('ost' in [descriptor.slug for descriptor in config.MODULE_REGISTRY]) == installed
assert ('/api/ost/library' in asgi_app.openapi()['paths']) == installed
assert ('modules.ost.router' in sys.modules) == installed
app._load_asgi_app = lambda: asgi_app
suffix = '.exe' if target == 'win32' else ''
for tool in plan.tools:
    (root / (tool.name + suffix)).touch()
with patch.object(sys, 'platform', target), patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(root / ('Keivotos' + suffix))):
    assert app._portable_check(config) == 0
    for tool in plan.tools:
        path = root / (tool.name + suffix)
        path.unlink()
        assert app._portable_check(config) == 1
        path.touch()
    for _label, relative in plan.helpers:
        path = root / relative
        path.unlink()
        assert app._portable_check(config) == 1
        path.touch()
if not installed:
    assert not (Path(config.SUITE_HOME) / 'modules/danbooru').exists()
    assert not (Path(config.SUITE_HOME) / 'modules/language').exists()
    assert not (Path(config.SUITE_HOME) / 'modules/ost').exists()
'''
    result = subprocess.run([sys.executable, '-c', code, target, str(installed)], cwd=source,
                            env={**os.environ, 'PYTHONPATH': str(source / 'backend'),
                                 'KEIVOTOS_HOME': str(tmp_path / 'home')},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    cli = subprocess.run([sys.executable, 'scripts/release/delivery_plan.py'], cwd=source,
                         capture_output=True, text=True, check=True, timeout=30)
    assert [t['name'] for t in json.loads(cli.stdout)['tools']] == (
        ['gallery-dl', 'ffmpeg'] if installed else ['ffmpeg'])


def test_delivery_composition_accepts_another_owner_without_configuration(monkeypatch):
    import module_registry
    from delivery import delivery_plan
    from delivery_contract import DeliveryContribution, PortableTool

    monkeypatch.setattr(module_registry, '_DELIVERY_PROVIDERS', (
        lambda target: DeliveryContribution(helpers=(('Fixture', 'scripts/fixture.py'),),
            tools=(PortableTool('fixture', '--version', 'fixture_entry.py'),)),
    ))
    plan = delivery_plan('linux')
    assert [tool.name for tool in plan.tools] == ['fixture', 'ffmpeg']
    assert plan.helpers == (('Fixture', 'scripts/fixture.py'), ('Folder picker', 'scripts/windows_folder_picker.py'))


def test_twitter_helper_and_engine_delivery_are_independent_of_other_optional_modules(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    shutil.copytree(ROOT / 'backend', source / 'backend', ignore=shutil.ignore_patterns('__pycache__', 'danbooru', 'youtube', 'language', 'ost'))
    code = '''import sys;from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'backend'))
import module_registry
from delivery import delivery_plan
from unittest.mock import patch
plan=delivery_plan('win32')
assert 'yt_dlp' in plan.collect_packages and 'modules.twitter.worker' in plan.hidden_imports
with patch('modules.twitter.worker.main',return_value=17) as worker:
 assert module_registry.dispatch_helper(['--twitter-worker'],load_configuration=lambda:None,run_helper=lambda *_:0)==17
 worker.assert_called_once_with([])
assert 'modules.twitter.jobs' not in sys.modules and 'yt_dlp' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=source, capture_output=True, text=True,
                            env={**os.environ, 'KEIVOTOS_HOME':str(tmp_path / 'home')}, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr



@pytest.mark.parametrize('manga_installed',[True,False])
def test_xml_parser_delivery_follows_remaining_owners(tmp_path,manga_installed):
    source=tmp_path/'without-video';source.mkdir()
    shutil.copytree(ROOT/'backend',source/'backend',ignore=shutil.ignore_patterns('__pycache__','video',*(() if manga_installed else ('manga',))))
    code="""from delivery import delivery_plan
import sys
for target in ('linux','win32'):
 plan=delivery_plan(target)
 assert ('defusedxml' in plan.collect_packages)==(sys.argv[1]=='yes')
 assert ('defusedxml' in plan.metadata_packages)==(sys.argv[1]=='yes')
assert 'modules.video' not in sys.modules and 'defusedxml' not in sys.modules
"""
    result=subprocess.run([sys.executable,'-c',code,'yes' if manga_installed else 'no'],cwd=source,capture_output=True,text=True,
        env={**os.environ,'PYTHONPATH':str(source/'backend'),'KEIVOTOS_HOME':str(tmp_path/'home')},timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
